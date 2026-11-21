"""严格模式、按凭证限流与签名回注的单元测试。"""

import asyncio
import os
import sys
import time
from types import SimpleNamespace

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest  # noqa: E402

from src import thought_signature_cache  # noqa: E402
from src.credential_limiter import CredentialLimiter  # noqa: E402
from src.fidelity import FidelityError, validate_client_request  # noqa: E402
from src.request_pacer import RequestPacer  # noqa: E402

KNOWN_MODEL = "gemini-4-hydrogen-medium"


class TestModelEnumMap:
    """模型枚举表来自 2.34.0 fetchAvailableModels 真实响应。"""

    def test_placeholder_and_google_enums(self):
        from src.model_enum import has_model_enum, resolve_model_enum

        assert resolve_model_enum("gemini-4-hydrogen-medium") == "MODEL_PLACEHOLDER_M526"
        assert resolve_model_enum("gemini-4-neon-medium") == "MODEL_PLACEHOLDER_M533"
        assert resolve_model_enum("gemini-2.5-flash") == "MODEL_GOOGLE_GEMINI_2_5_FLASH"
        assert has_model_enum("gemini-2.5-flash")
        assert not has_model_enum("totally-unknown-model")

    def test_parse_ignores_model_provider(self):
        from src.model_enum import parse_model_enums

        data = {
            "models": {
                "gemini-2.5-flash": {
                    "model": "MODEL_GOOGLE_GEMINI_2_5_FLASH",
                    "modelProvider": "MODEL_PROVIDER_GOOGLE",
                }
            }
        }
        assert parse_model_enums(data) == {
            "gemini-2.5-flash": "MODEL_GOOGLE_GEMINI_2_5_FLASH"
        }


class TestModelParams:
    """模型参数表（maxOutputTokens 等）与 fetchAvailableModels 学习模式。"""

    def setup_method(self):
        from src.model_enum import clear_learned_enums

        clear_learned_enums()

    def teardown_method(self):
        from src.model_enum import clear_learned_enums

        clear_learned_enums()

    def test_builtin_params_seeded_from_capture(self):
        from src.model_enum import get_model_param

        assert get_model_param("gemini-2.5-flash", "maxOutputTokens") == 65535
        assert get_model_param("gemini-4-hydrogen-medium", "maxOutputTokens") == 262144
        # 无实抓证据的模型不参与上限校验
        assert get_model_param("gemini-2.5-pro", "maxOutputTokens") is None
        assert get_model_param("", "maxOutputTokens") is None

    def test_parse_model_params_flat_and_nested(self):
        from src.model_enum import parse_model_params

        data = {
            "models": {
                "gemini-3-flash": {"maxOutputTokens": 32768},
                "gemini-3-pro": {"metadata": {"supportsThinking": True}},
            }
        }
        params = parse_model_params(data)
        assert params["gemini-3-flash"] == {"maxOutputTokens": 32768}
        assert params["gemini-3-pro"] == {"supportsThinking": True}

    def test_learn_params_override_builtin(self):
        from src.model_enum import get_model_param, merge_model_params, parse_model_params

        merge_model_params(parse_model_params({"models": {"gemini-2.5-flash": {"maxOutputTokens": 40960}}}))
        assert get_model_param("gemini-2.5-flash", "maxOutputTokens") == 40960


class TestFidelityError:
    def test_status_mapping(self):
        assert FidelityError("unknown_model_enum").status_code == 400
        assert FidelityError("tls_sidecar_unavailable").status_code == 503
        assert FidelityError("envelope_schema_mismatch").status_code == 500

    def test_payload_shape(self):
        payload = FidelityError("unsupported_client_field", "safetySettings").to_payload()
        assert payload["error"]["type"] == "fidelity_rejected"
        assert payload["error"]["reason"] == "unsupported_client_field"
        assert "safetySettings" in payload["error"]["detail"]


class TestValidateClientRequest:
    def test_accepts_whitelisted_fields(self):
        request = {"contents": [], "systemInstruction": {"parts": []},
                   "tools": [], "generationConfig": {}}
        validate_client_request(request, KNOWN_MODEL)

    def test_rejects_unknown_field(self):
        request = {"contents": [], "safetySettings": [{"category": "HARM"}]}
        with pytest.raises(FidelityError) as exc_info:
            validate_client_request(request, KNOWN_MODEL)
        assert exc_info.value.reason == "unsupported_client_field"

    def test_rejects_model_without_enum(self):
        with pytest.raises(FidelityError) as exc_info:
            validate_client_request({"contents": []}, "gemini-9-unknown")
        assert exc_info.value.reason == "unknown_model_enum"

    def test_injects_thinking_level_from_suffix(self):
        request = {"contents": []}
        validate_client_request(request, KNOWN_MODEL)
        thinking = request["generationConfig"]["thinkingConfig"]
        assert thinking["thinkingLevel"] == "MEDIUM"
        assert thinking["includeThoughts"] is True

    def test_conflicting_thinking_level_rejected(self):
        request = {
            "contents": [],
            "generationConfig": {"thinkingConfig": {"thinkingLevel": "HIGH"}},
        }
        with pytest.raises(FidelityError) as exc_info:
            validate_client_request(request, KNOWN_MODEL)
        assert exc_info.value.reason == "generation_config_mismatch"

    def test_does_not_mutate_caller_generation_config(self):
        original = {"thinkingConfig": {"thinkingLevel": "MEDIUM"}}
        request = {"contents": [], "generationConfig": original}
        validate_client_request(request, KNOWN_MODEL)
        assert request["generationConfig"] is not original
        assert original == {"thinkingConfig": {"thinkingLevel": "MEDIUM"}}

    def test_minimal_suffix_injects_minimal(self):
        from src.model_enum import merge_model_enums

        # 表内暂无 -minimal 后缀模型，用学习表临时注册（真实枚举值以抓包为准）
        merge_model_enums({"gemini-x-minimal": "MODEL_PLACEHOLDER_M000"})
        request = {"contents": []}
        validate_client_request(request, "gemini-x-minimal")
        thinking = request["generationConfig"]["thinkingConfig"]
        assert thinking["thinkingLevel"] == "MINIMAL"
        assert thinking["includeThoughts"] is True


class TestMaxOutputTokensValidation:
    """严格模式 maxOutputTokens 上限校验（只拦有实抓证据的超限显式值）。"""

    def test_explicit_over_limit_rejected(self):
        request = {"contents": [], "generationConfig": {"maxOutputTokens": 999999}}
        with pytest.raises(FidelityError) as exc_info:
            validate_client_request(request, KNOWN_MODEL)
        assert exc_info.value.reason == "generation_config_mismatch"

    def test_at_limit_accepted(self):
        request = {"contents": [], "generationConfig": {"maxOutputTokens": 262144}}
        validate_client_request(request, KNOWN_MODEL)

    def test_proxy_default_values_exempt(self):
        # 65536/64000 是转换器官方标配默认值（含客户端未指定时被替换的情形）
        for value in (65536, 64000):
            request = {"contents": [], "generationConfig": {"maxOutputTokens": value}}
            validate_client_request(request, KNOWN_MODEL)

    def test_model_without_evidence_passes_through(self):
        request = {"contents": [], "generationConfig": {"maxOutputTokens": 10 ** 9}}
        validate_client_request(request, "gemini-2.5-pro")


class TestNonStrictClamp:
    """非严格模式下转换器对超限显式值做钳制；严格模式保留原值交给 fidelity 拒绝。"""

    async def _normalize(self, monkeypatch, strict: bool) -> dict:
        from src.converter import antigravity_fix
        from src.model_enum import clear_learned_enums

        async def _flag():
            return strict

        monkeypatch.setattr(antigravity_fix, "get_strict_fidelity_enabled", _flag)
        clear_learned_enums()
        profile = SimpleNamespace(family="gemini")
        generation_config = {"maxOutputTokens": 999999}
        result = await antigravity_fix._normalize_generation_config(
            {}, generation_config, profile, "gemini-2.5-flash"
        )
        clear_learned_enums()
        return result

    async def test_clamps_over_limit_when_not_strict(self, monkeypatch):
        result = await self._normalize(monkeypatch, strict=False)
        assert result["maxOutputTokens"] == 65535

    async def test_keeps_value_when_strict(self, monkeypatch):
        result = await self._normalize(monkeypatch, strict=True)
        assert result["maxOutputTokens"] == 999999


class TestThoughtSignatureCache:
    def setup_method(self):
        thought_signature_cache.clear()

    def test_replays_signature_for_same_text(self):
        contents = [
            {"role": "model", "parts": [{"text": "hello", "thoughtSignature": "sig-1"}]}
        ]
        thought_signature_cache.replay_into_contents(contents, "traj-1")
        # 下一轮里同一段文本没有签名
        next_turn = [{"role": "model", "parts": [{"text": "hello"}]}]
        restored = thought_signature_cache.replay_into_contents(next_turn, "traj-1")
        assert restored == 1
        assert next_turn[0]["parts"][0]["thoughtSignature"] == "sig-1"

    def test_isolated_by_trajectory(self):
        thought_signature_cache.replay_into_contents(
            [{"role": "model", "parts": [{"text": "x", "thoughtSignature": "sig"}]}],
            "traj-a",
        )
        other = [{"role": "model", "parts": [{"text": "x"}]}]
        assert thought_signature_cache.replay_into_contents(other, "traj-b") == 0
        assert "thoughtSignature" not in other[0]["parts"][0]


class TestCredentialLimiter:
    async def test_limits_per_credential(self, monkeypatch):
        async def _limit_one():
            return 1

        monkeypatch.setattr(
            "src.credential_limiter.get_credential_max_concurrent", _limit_one
        )
        limiter = CredentialLimiter()
        await limiter.acquire("cred-a")

        blocked = asyncio.create_task(limiter.acquire("cred-a"))
        await asyncio.sleep(0)
        assert not blocked.done(), "同一凭证第二个请求应排队"

        other = asyncio.create_task(limiter.acquire("cred-b"))
        await asyncio.wait_for(other, 1.0)

        limiter.release("cred-a")
        await asyncio.wait_for(blocked, 1.0)
        limiter.release("cred-a")
        limiter.release("cred-b")


class TestPerCredentialPacing:
    async def test_pace_for_isolated_between_credentials(self):
        pacer = RequestPacer()
        pacer._enabled = True
        pacer._serial_mode = False
        pacer._min_interval = 0.2
        pacer._jitter = 0.0
        pacer._last_refresh = time.monotonic()

        await pacer.pace_for("cred-a")
        started = time.monotonic()
        await pacer.pace_for("cred-b")
        assert time.monotonic() - started < 0.1, "不同凭证不应互相节流"

        started = time.monotonic()
        await pacer.pace_for("cred-a")
        assert time.monotonic() - started >= 0.15, "同一凭证应等待最小间隔"
