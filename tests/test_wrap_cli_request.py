"""wrap_cli_request 确定性回归测试（issue #450 根因 1）。

覆盖:
- 同一会话跨轮次 → sessionId / trajectory_id / labels 恒定（仅 requestId 时间戳段变化）
- 共用 systemInstruction 的不同会话 → sessionId 隔离
- 不同模型 → sessionId 隔离
- step 随模型轮数递增，同步写入 requestId 末段与 labels
- 客户端显式 sessionId 透传
- labels 键序与 requestType 判定不受影响
"""
import json
import os
import uuid
from pathlib import Path

import pytest

from src.api.antigravity import (
    _build_labels,
    _derive_upstream_session_ids,
    _generate_request_id,
    _reset_trajectory_request_seq,
    wrap_cli_request,
)


@pytest.fixture(autouse=True)
def _clean_trajectory_counters(monkeypatch):
    """labels.request_id 后缀是进程内自增计数，逐用例重置保证断言确定。

    这些用例使用未收录进枚举表的模型，因此关闭严格模式；严格模式本身
    由 tests/test_fidelity.py 覆盖。
    """

    async def _strict_off():
        return False

    monkeypatch.setattr("src.api.antigravity.get_strict_fidelity_enabled", _strict_off)
    _reset_trajectory_request_seq()
    yield
    _reset_trajectory_request_seq()

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

    p3, rid3 = await wrap_cli_request(_session_a_turn3(), MODEL, "proj")

    # turn1: 0 条 model 消息 → step = 1 + 2*0 = 1；last_step_index = 0
    assert rid1.split("/")[4] == "1"
    assert p1["request"]["labels"]["last_step_index"] == "0"
    # 实抓 labels.request_id 后缀从 0 开始
    assert p1["request"]["labels"]["request_id"] == f"{p1['request']['labels']['trajectory_id']}-0"

    # turn3: 2 条 model 消息 → step = 1 + 2*2 = 5；同一 trajectory 内后缀自增
    traj = p2["request"]["labels"]["trajectory_id"]
    assert rid2.split("/")[4] == "5"
    assert rid3.split("/")[4] == "5"
    assert p2["request"]["labels"]["last_step_index"] == "4"
    assert p2["request"]["labels"]["request_id"] == f"{traj}-0"
    assert p3["request"]["labels"]["request_id"] == f"{traj}-1"


async def test_tool_round_step_matches_real_sample():
    """实抓 20261121-112701/0011：user + model(functionCall) + user(functionResponse)
    → requestId 末段 3、last_step_index 2、request_id 后缀从 0 开始。"""
    request = _req([
        {"role": "user", "parts": [{"text": "读一下探测文件"}]},
        {
            "role": "model",
            "parts": [{"functionCall": {"name": "read_file", "args": {"Path": "x"}}}],
        },
        {
            "role": "user",
            "parts": [{"functionResponse": {"name": "read_file", "response": {"output": "ok"}}}],
        },
    ])
    payload, rid = await wrap_cli_request(request, MODEL, "proj")
    assert rid.split("/")[4] == "3"
    assert payload["request"]["labels"]["last_step_index"] == "2"
    assert payload["request"]["labels"]["request_id"].endswith("-0")


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
        "cascade_id",
        "last_step_index",
        "model_enum",
        "request_id",
        "root_cascade_id",
        "trajectory_id",
        "used_claude",
        "used_claude_conservative",
        "used_non_gemini_model",
    ]
    # 未收录进枚举表的模型回退为模型名（保持既有行为）
    assert labels["model_enum"] == MODEL
    # cascade_id 与 root_cascade_id 同值，且等于 requestId 第 2 段
    assert labels["cascade_id"] == labels["root_cascade_id"]
    assert labels["cascade_id"] == p["requestId"].split("/")[1]
    assert labels["trajectory_id"] == p["requestId"].split("/")[3]
    # 2.34.0 起真实客户端不再发送 last_execution_id
    assert "last_execution_id" not in labels
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
    ids1 = _derive_upstream_session_ids(_session_a_turn1(), MODEL)
    ids2 = _derive_upstream_session_ids(_session_a_turn1(), MODEL)
    assert ids1 == ids2
    assert ids1.session_id.startswith("-") and int(ids1.session_id[1:]) >= 0
    uuid.UUID(ids1.trajectory_id)
    uuid.UUID(ids1.cascade_id)
    # cascade 与 trajectory 不同源，取值必须不同
    assert ids1.cascade_id != ids1.trajectory_id
    # 与 derive_session_key 联合锚点同源：摘要前缀即 sessionId int
    from src.session_cache import derive_conversation_anchor
    digest = derive_conversation_anchor(MODEL, _session_a_turn1())
    assert int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF == int(ids1.session_id[1:])


def test_generate_request_id_format():
    rid = _generate_request_id("afb38f6a-74b6-459f-8edd-a75d8d00bc19", 5, "d3a2e3d2-21f9-411b-967f-53811898c2cd")
    parts = rid.split("/")
    assert parts[0] == "agent"
    assert parts[1] == "afb38f6a-74b6-459f-8edd-a75d8d00bc19"
    assert parts[3] == "d3a2e3d2-21f9-411b-967f-53811898c2cd"
    assert parts[4] == "5"
    assert len(parts) == 5


_REPO_ROOT = Path(__file__).resolve().parent.parent
_CAPTURE_SAMPLE = (
    _REPO_ROOT
    / "tools"
    / "antigravity_capture"
    / "captures"
    / "20261120-125431"
    / "0009_streamGenerateContent.request.json"
)


@pytest.mark.skipif(
    not _CAPTURE_SAMPLE.is_file(),
    reason="缺少实抓样本（tools/antigravity_capture/captures 已被 gitignore）",
)
async def test_envelope_matches_captured_sample_shape():
    """与实抓报文做结构级黄金对照（不比较 UUID / 时间戳等易变值）。"""
    sample = json.loads(_CAPTURE_SAMPLE.read_text(encoding="utf-8"))
    payload, _rid = await wrap_cli_request(_session_a_turn3(), "gemini-3-pro-preview", "proj")

    # 顶层信封键序一致
    assert list(payload.keys()) == list(sample.keys())
    # labels 键序与键集一致
    assert list(payload["request"]["labels"].keys()) == list(
        sample["request"]["labels"].keys()
    )
    # 内层键序：我们生成的键必须是实抓键序的子序列（缺省项如 toolConfig 允许不出现）
    sample_inner = [k for k in sample["request"].keys()]
    ours_inner = list(payload["request"].keys())
    it = iter(sample_inner)
    assert all(any(k == s for s in it) for k in ours_inner)
    # requestId 五段且第 2/4 段与 labels 对齐
    parts = payload["requestId"].split("/")
    assert len(parts) == 5 and parts[0] == "agent"
    assert parts[1] == payload["request"]["labels"]["cascade_id"]
    assert parts[3] == payload["request"]["labels"]["trajectory_id"]
