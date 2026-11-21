"""§10.16 增强项单元测试：注册表学习扩容 / 每模型额度 / countTokens。

实抓依据：163547/0004 fetchAvailableModels 响应（39 模型，每条带
quotaInfo/thinkingLevel/disabled 等）与 prediction_service.proto 的
CountTokensRequest schema。
"""
import json
from pathlib import Path

import pytest

from src import quota_budget
from src.api.antigravity import fetch_upstream_token_count
from src.model_enum import (
    clear_learned_enums,
    merge_model_params,
    parse_model_params,
    parse_model_quota,
)
from src.fidelity import FidelityError, _thinking_level_from_model, validate_client_request

_TESTS = Path(__file__).resolve().parent.parent
_CAPTURE = _TESTS / "tools" / "antigravity_capture" / "captures" / "20261121-163547" / "0004_fetchAvailableModels.response.txt"


@pytest.fixture(autouse=True)
def _clean_learned():
    clear_learned_enums()
    quota_budget.reset_state()
    yield
    clear_learned_enums()
    quota_budget.reset_state()


def _real_response():
    return json.load(open(_CAPTURE, encoding="utf-8"))


@pytest.mark.skipif(not _CAPTURE.exists(), reason="需要本地实抓 0004 响应")
class TestRegistryLearning:
    def test_params_parse_top_level_not_fooled_by_nested(self):
        """thinkingLevel/disabled 从条目顶层取（modelExperiments 嵌套不误命中）。"""
        data = _real_response()
        params = parse_model_params(data)
        m526 = params["gemini-4-hydrogen-medium"]
        assert m526["maxOutputTokens"] == 262144
        assert m526["thinkingLevel"] == 2
        # 正常模型不携带 disabled 键（缺失的键不记录）
        assert m526.get("disabled") is not True

        m670 = params["gemini-4-carbon-disabled"]
        assert m670["disabled"] is True

        m528 = params["gemini-4-hydrogen-xhigh"]
        assert m528["thinkingLevel"] == 100

    def test_quota_parse(self):
        data = _real_response()
        quota = parse_model_quota(data)
        assert "claude-sonnet-5-5-medium" in quota
        entry = quota["claude-sonnet-5-5-medium"]
        assert isinstance(entry["remainingFraction"], float)
        assert "resetTime" in entry

    def test_learned_thinking_level_wire_mapping(self):
        """注册表 thinkingLevel 数值 → 线格式：1→LOW / 2→MEDIUM / 3→HIGH / 100→100。"""
        merge_model_params({
            "test-model-low": {"thinkingLevel": 1},
            "test-model-medium": {"thinkingLevel": 2},
            "test-model-high": {"thinkingLevel": 3},
            "test-model-xhigh": {"thinkingLevel": 100},
            "test-model-unknown": {"thinkingLevel": 7},
        })
        assert _thinking_level_from_model("test-model-low") == "LOW"
        assert _thinking_level_from_model("test-model-medium") == "MEDIUM"
        assert _thinking_level_from_model("test-model-high") == "HIGH"
        assert _thinking_level_from_model("test-model-xhigh") == 100
        # 4/5/6/7 线格式无实抓证据 → 不注入
        assert _thinking_level_from_model("test-model-unknown") is None

    def test_learned_level_overrides_suffix_when_mapped(self):
        """注册表值优先于名字后缀（映射成功时）。"""
        merge_model_params({"weird-named-model": {"thinkingLevel": 3}})
        assert _thinking_level_from_model("weird-named-model") == "HIGH"

    def test_disabled_model_rejected(self):
        """注册表 disabled=True 的模型 fail-closed 拒绝（实抓：carbon-disabled）。"""
        merge_model_params({"gemini-4-carbon-disabled": {"disabled": True}})
        with pytest.raises(FidelityError) as exc:
            validate_client_request(
                {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
                "gemini-4-carbon-disabled",
            )
        assert exc.value.reason == "model_disabled"

    def test_enabled_model_not_rejected(self):
        merge_model_params({"gemini-4-hydrogen-medium": {"disabled": False}})
        validate_client_request(
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            "gemini-4-hydrogen-medium",
        )


class TestModelQuotaState:
    def test_set_and_min_remaining(self):
        quota_budget.set_model_quota("a@x.com", {
            "model-1": {"remainingFraction": 0.9},
            "model-2": {"remainingFraction": 0.05},
        })
        assert quota_budget._model_min_remaining("a@x.com") == 0.05

    def test_empty_mapping_clears(self):
        quota_budget.set_model_quota("a@x.com", {"m": {"remainingFraction": 0.5}})
        quota_budget.set_model_quota("a@x.com", {})
        assert quota_budget._model_min_remaining("a@x.com") is None

    def test_no_data_returns_none(self):
        assert quota_budget._model_min_remaining("nobody@x.com") is None


class TestUpstreamTokenCount:
    async def test_success(self, monkeypatch):
        captured = {}

        async def fake_post(url="", json=None, headers=None, timeout=None, proxy=None):
            captured["url"] = url
            captured["json"] = json
            captured["proxy"] = proxy

            class R:
                status_code = 200

                def json(self):
                    return {"totalTokens": 4242}

                text = ""

            return R()

        async def fake_cred(model_name=None, session_key=None, exclude_emails=None):
            return ("a@x.com", {"access_token": "tok", "proxy": "http://p:1"})

        monkeypatch.setattr("src.api.antigravity.post_async", fake_post)
        monkeypatch.setattr("src.api.antigravity.get_valid_credential", fake_cred)
        result = await fetch_upstream_token_count(
            {
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "systemInstruction": {"parts": [{"text": "sys"}]},
            },
            "gemini-4-hydrogen-medium",
        )
        assert result == 4242
        assert captured["url"].endswith("/v1internal:countTokens")
        assert captured["json"] == {
            "request": {
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "systemInstruction": {"parts": [{"text": "sys"}]},
            }
        }
        assert captured["proxy"] == "http://p:1"

    async def test_no_contents_returns_none(self, monkeypatch):
        async def fake_cred(model_name=None, session_key=None, exclude_emails=None):
            raise AssertionError("无 contents 时不应选取凭证")

        monkeypatch.setattr("src.api.antigravity.get_valid_credential", fake_cred)
        assert await fetch_upstream_token_count({"generationConfig": {}}) is None

    async def test_upstream_failure_returns_none(self, monkeypatch):
        async def fake_post(url="", json=None, headers=None, timeout=None, proxy=None):
            class R:
                status_code = 503
                text = "boom"

                def json(self):
                    return {}

            return R()

        async def fake_cred(model_name=None, session_key=None, exclude_emails=None):
            return ("a@x.com", {"access_token": "tok"})

        monkeypatch.setattr("src.api.antigravity.post_async", fake_post)
        monkeypatch.setattr("src.api.antigravity.get_valid_credential", fake_cred)
        assert await fetch_upstream_token_count(
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        ) is None

    async def test_non_int_total_tokens_rejected(self, monkeypatch):
        async def fake_post(url="", json=None, headers=None, timeout=None, proxy=None):
            class R:
                status_code = 200
                text = ""

                def json(self):
                    return {"totalTokens": "42"}

            return R()

        async def fake_cred(model_name=None, session_key=None, exclude_emails=None):
            return ("a@x.com", {"access_token": "tok"})

        monkeypatch.setattr("src.api.antigravity.post_async", fake_post)
        monkeypatch.setattr("src.api.antigravity.get_valid_credential", fake_cred)
        assert await fetch_upstream_token_count(
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        ) is None
