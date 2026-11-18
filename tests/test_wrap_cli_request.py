"""wrap_cli_request 确定性回归测试（issue #450 根因 1）。

覆盖:
- 同一会话跨轮次 → sessionId / trajectory_id / labels 恒定（仅 requestId 时间戳段变化）
- 共用 systemInstruction 的不同会话 → sessionId 隔离
- 不同模型 → sessionId 隔离
- step 随模型轮数递增，同步写入 requestId 末段与 labels
- 客户端显式 sessionId 透传
- labels 键序与 requestType 判定不受影响
"""
import uuid

import pytest

from src.api.antigravity import (
    _build_labels,
    _derive_upstream_session_ids,
    _generate_request_id,
    wrap_cli_request,
)

MODEL = "gemini-4-carbon-medium"
SYSTEM_PARTS = [{"text": "You are a helpful coding assistant."}]


def _req(contents, system=True):
    req = {"contents": contents}
    if system:
        req["systemInstruction"] = {"parts": SYSTEM_PARTS}
    return req


def _session_a_turn1():
    return _req([
        {"role": "user", "parts": [{"text": "帮我重构这个模块"}]},
    ])


def _session_a_turn3():
    return _req([
        {"role": "user", "parts": [{"text": "帮我重构这个模块"}]},
        {"role": "model", "parts": [{"text": "好的，我先看一下……"}]},
        {"role": "user", "parts": [{"text": "先从 session_cache 开始"}]},
        {"role": "model", "parts": [{"text": "已完成。"}]},
        {"role": "user", "parts": [{"text": "再补几个测试"}]},
    ])


def _session_b():
    return _req([
        {"role": "user", "parts": [{"text": "给前端写个组件"}]},
    ])


async def test_session_id_and_trajectory_stable_across_turns():
    """从第二条消息出现起（第 2 轮及以后），sessionId / trajectory_id 恒定。"""
    p2a, rid2a = await wrap_cli_request(_session_a_turn3(), MODEL, "proj")
    p2b, rid2b = await wrap_cli_request(_session_a_turn3(), MODEL, "proj")

    sid_a = p2a["request"]["sessionId"]
    sid_b = p2b["request"]["sessionId"]
    assert sid_a == sid_b, "同一会话跨轮次 sessionId 必须恒定"
    assert sid_a.startswith("-")

    traj_a = p2a["request"]["labels"]["trajectory_id"]
    traj_b = p2b["request"]["labels"]["trajectory_id"]
    assert traj_a == traj_b, "同一会话跨轮次 trajectory_id 必须恒定"
    # trajectory_id 是合法 UUID（16 字节确定性派生）
    uuid.UUID(traj_a)

    # requestId 的会话/轨迹段稳定，仅时间戳段变化
    seg_a = rid2a.split("/")
    seg_b = rid2b.split("/")
    assert len(seg_a) == 5 and len(seg_b) == 5
    assert seg_a[1] == seg_b[1]  # conversationId
    assert seg_a[3] == seg_b[3]  # trajectoryId


async def test_step_increments_with_model_turns():
    p1, rid1 = await wrap_cli_request(_session_a_turn1(), MODEL, "proj")
    p2, rid2 = await wrap_cli_request(_session_a_turn3(), MODEL, "proj")

    # turn1: 0 条 model 消息 → step 1
    assert rid1.split("/")[4] == "1"
    assert p1["request"]["labels"]["last_step_index"] == "1"
    assert p1["request"]["labels"]["request_id"] == f"{p1['request']['labels']['trajectory_id']}-1"

    # turn3: 2 条 model 消息 → step 3
    assert rid2.split("/")[4] == "3"
    assert p2["request"]["labels"]["last_step_index"] == "3"
    assert p2["request"]["labels"]["request_id"] == f"{p2['request']['labels']['trajectory_id']}-3"


async def test_shared_system_sessions_isolated():
    pa, _ = await wrap_cli_request(_session_a_turn1(), MODEL, "proj")
    pb, _ = await wrap_cli_request(_session_b(), MODEL, "proj")
    assert pa["request"]["sessionId"] != pb["request"]["sessionId"]
    assert pa["request"]["labels"]["trajectory_id"] != pb["request"]["labels"]["trajectory_id"]


async def test_different_model_isolated():
    pa, _ = await wrap_cli_request(_session_a_turn1(), MODEL, "proj")
    pb, _ = await wrap_cli_request(_session_a_turn1(), "claude-sonnet-5-5-medium", "proj")
    assert pa["request"]["sessionId"] != pb["request"]["sessionId"]


async def test_client_session_id_passthrough():
    req = _session_a_turn1()
    req["sessionId"] = "-123456789"
    p, rid = await wrap_cli_request(req, MODEL, "proj")
    assert p["request"]["sessionId"] == "-123456789"


async def test_empty_contents_fallback_session_id():
    """无任何锚点时 sessionId 仍为 -{int} 形态（随机兜底），请求可正常包装。"""
    p, rid = await wrap_cli_request({"contents": []}, MODEL, "proj")
    assert p["request"]["sessionId"].startswith("-")
    assert rid.split("/")[4] == "1"


async def test_labels_and_request_type_unchanged():
    """labels 键序（官方字母序）与 requestType 判定保持既有行为。"""
    p, _ = await wrap_cli_request(_session_a_turn1(), MODEL, "proj")
    labels = p["request"]["labels"]
    assert list(labels.keys()) == [
        "last_step_index",
        "model_enum",
        "request_id",
        "trajectory_id",
        "used_claude",
        "used_claude_conservative",
        "used_non_gemini_model",
    ]
    assert labels["model_enum"] == MODEL
    assert labels["used_claude"] == "false"
    assert labels["used_non_gemini_model"] == "false"
    # 无 tools 且无工具交互 → chat
    assert p["requestType"] == "chat"

    # 带 tools → agent，且 used_claude 正确
    req = _session_a_turn1()
    req["tools"] = [{"functionDeclarations": [{"name": "search", "parameters": {"type": "object"}}]}]
    p2, _ = await wrap_cli_request(req, "claude-sonnet-5-5-medium", "proj")
    assert p2["requestType"] == "agent"
    assert p2["request"]["labels"]["used_claude"] == "true"


async def test_payload_topology_unchanged():
    """顶层信封与内部 request 键序保持官方拓扑。"""
    p, _ = await wrap_cli_request(_session_a_turn1(), MODEL, "proj")
    assert list(p.keys()) == ["project", "requestId", "request", "model", "userAgent", "requestType"]
    inner_keys = list(p["request"].keys())
    assert inner_keys[0] == "contents"
    if "systemInstruction" in inner_keys:
        assert inner_keys.index("systemInstruction") == 1
        assert inner_keys.index("labels") in (2, 3)  # tools/toolConfig 缺省时 labels 前移


def test_derive_upstream_session_ids_deterministic():
    s1, t1 = _derive_upstream_session_ids(_session_a_turn1(), MODEL)
    s2, t2 = _derive_upstream_session_ids(_session_a_turn1(), MODEL)
    assert s1 == s2 and t1 == t2
    assert s1.startswith("-") and int(s1[1:]) >= 0
    uuid.UUID(t1)
    # 与 derive_session_key 联合锚点同源：摘要前缀即 sessionId int
    from src.session_cache import derive_conversation_anchor
    digest = derive_conversation_anchor(MODEL, _session_a_turn1())
    assert int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF == int(s1[1:])


def test_generate_request_id_format():
    rid = _generate_request_id("-123", 5, "d3a2e3d2-21f9-411b-967f-53811898c2cd")
    parts = rid.split("/")
    assert parts[0] == "agent"
    assert parts[3] == "d3a2e3d2-21f9-411b-967f-53811898c2cd"
    assert parts[4] == "5"
    assert len(parts) == 5
