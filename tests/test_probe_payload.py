"""委派探活报文（/creds/test）同构性回归测试。

根因：/creds/test 曾手写 payload:
    {"model": ..., "project": ..., "request": {"contents": [...]}}
缺 requestId / userAgent / requestType / sessionId / labels，键序也不同，
与真实请求（wrap_cli_request 产出的六段信封）差异极大，是明显的风控特征。

修复：检测端点改为复用 wrap_cli_request，与真实非流式请求完全同构。
本文件守护该前提：wrap_cli_request 必须产出完整官方信封。

用法::

    .venv\\Scripts\\python.exe -m pytest tests\\test_probe_payload.py -v
"""

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.antigravity import wrap_cli_request  # noqa: E402

PROBE_TEXT = "你好，请回复一个字。"
TEST_MODEL = "gemini-2.5-flash-lite"
PROJECT_ID = "test-project"

ENVELOPE_KEYS = {"project", "requestId", "request", "model", "userAgent", "requestType"}

# requestId 五段式：agent/{conversationId}/{unixMs}/{trajectoryId}/{step}
REQUEST_ID_RE = re.compile(r"^agent/[0-9a-f]{16}/\d{9,}/[0-9a-f]+/\d+$")


def _build() -> dict:
    gemini_request = {
        "contents": [{"role": "user", "parts": [{"text": PROBE_TEXT}]}]
    }
    payload, _ = asyncio.run(
        wrap_cli_request(gemini_request, TEST_MODEL, PROJECT_ID)
    )
    return payload


# ---------------------------------------------------------------------------
# 信封结构
# ---------------------------------------------------------------------------
def test_envelope_keys_match_official_shape() -> None:
    """信封键集合必须恰为官方六段，不多不少"""
    payload = _build()
    assert set(payload.keys()) == ENVELOPE_KEYS, (
        f"信封键不匹配，实际: {sorted(payload.keys())}"
    )


def test_project_and_model_passthrough() -> None:
    payload = _build()
    assert payload["project"] == PROJECT_ID
    assert payload["model"] == TEST_MODEL
    assert payload["userAgent"] == "antigravity"


def test_no_tools_request_type_is_chat() -> None:
    """无工具交互应判定为 chat（纯文本问答）"""
    payload = _build()
    assert payload["requestType"] == "chat"


def test_request_has_session_id_and_labels() -> None:
    """request 内必须具备 sessionId 与 labels（手写 payload 缺失的字段）"""
    inner = _build()["request"]
    assert inner.get("sessionId"), "request.sessionId 缺失"
    assert isinstance(inner["sessionId"], str)
    assert inner["sessionId"].startswith("-")

    labels = inner.get("labels")
    assert isinstance(labels, dict) and labels, "request.labels 缺失"
    for key in ("trajectory_id", "request_id", "model_enum", "last_step_index"):
        assert key in labels, f"labels 缺少 {key}"


def test_chat_request_strips_tools() -> None:
    """chat 请求不得携带 tools / toolConfig"""
    inner = _build()["request"]
    assert "tools" not in inner
    assert "toolConfig" not in inner


# ---------------------------------------------------------------------------
# requestId 形态与幂等隔离
# ---------------------------------------------------------------------------
def test_request_id_five_segments() -> None:
    payload = _build()
    request_id = payload["requestId"]
    assert REQUEST_ID_RE.match(request_id), f"requestId 形态不符: {request_id}"


def test_repeated_probe_not_identical() -> None:
    """同一文本两次调用：unixMs / trajectoryId 必须不同（幂等隔离），
    但 conversationId 段由 sessionId 派生，应保持一致。"""
    first = _build()["requestId"]
    second = _build()["requestId"]

    assert first != second, "两次探活的 requestId 不应完全相同"

    first_parts = first.split("/")
    second_parts = second.split("/")
    assert first_parts[1] == second_parts[1], "相同文本的 conversationId 段应稳定"
    assert first_parts[3] != second_parts[3], "trajectoryId 每请求应唯一"


def test_session_id_stable_for_same_text() -> None:
    """同文本派生的 sessionId 稳定（利于上游前缀缓存）"""
    first = _build()["request"]["sessionId"]
    second = _build()["request"]["sessionId"]
    assert first == second
