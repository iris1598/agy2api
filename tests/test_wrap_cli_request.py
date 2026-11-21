"""wrap_cli_request 确定性回归测试（issue #450 根因 1）。

覆盖:
- 同一会话跨轮次 → sessionId / trajectory_id / labels 恒定（仅 requestId 时间戳段变化）
- sessionId 全体客户端恒等的常量（静态逆向：FNV-1a64 空输入 basis，实抓 163547/193455 同值）
- 共用 systemInstruction 的不同会话 → trajectory/cascade 隔离
- 不同模型 → trajectory/cascade 隔离
- step 随模型轮数递增，同步写入 requestId 末段与 labels
- 客户端显式 sessionId 透传
- labels 键序与 requestType 判定不受影响
- user 轮 <USER_REQUEST> 包装（实抓 60 条无例外）+ 首见盖戳稳定性 + 幂等
"""
import json
import os
import re
import struct
import uuid
from pathlib import Path

import pytest

from src.api.antigravity import (
    UPSTREAM_BUILD_ENVELOPE_HEADER,
    _build_labels,
    _derive_upstream_session_ids,
    _generate_request_id,
    _reset_trajectory_request_seq,
    build_envelope_parts,
    compose_python_envelope,
    compose_sidecar_body,
    prepare_cli_request,
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
    """从**首条请求**起 sessionId / trajectory_id 恒定（§10.14 粘性绑定：
    修复前首轮与后续因锚点取前两条消息而派生不同 trajectory）。"""
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

    # turn1: [user] → 自然步号 1；last_step_index = 0
    assert rid1.split("/")[4] == "1"
    assert p1["request"]["labels"]["last_step_index"] == "0"
    # 实抓 labels.request_id 后缀从 0 开始;粘性绑定后 turn1 与后续共享同一
    # trajectory(修复前每会话首轮与后续派生不同 trajectory,request_seq 归零)
    traj = p1["request"]["labels"]["trajectory_id"]
    assert p1["request"]["labels"]["request_id"] == f"{traj}-0"

    # turn3: [user×3, model×2(文本)] → 自然步号 = 3 user + 2 model 轮 = 5
    assert p2["request"]["labels"]["trajectory_id"] == traj
    assert rid2.split("/")[4] == "5"
    assert p2["request"]["labels"]["last_step_index"] == "4"
    assert p2["request"]["labels"]["request_id"] == f"{traj}-1"
    assert p3["request"]["labels"]["request_id"] == f"{traj}-2"
    # p3 与 p2 contents 完全相同 → 实抓语义下是一次客户端重发
    # (163547:0013 502 失败后 0014 原样重发,step +1)→ step 6
    assert rid3.split("/")[4] == "6"
    assert p3["request"]["labels"]["last_step_index"] == "5"


async def test_tool_round_step_matches_real_sample():
    """实抓 163547 工具轮形态:user + model(fc) + model(fr) → 自然步号
    = 1 user + 1 fc 轮 + 1 fc part = 3,requestId 末段 3、last_step_index 2、
    request_id 后缀从 0 开始。

    step 规律:functionResponse 不计轮(工具输出不是模型回复)。
    """
    request = _req([
        {"role": "user", "parts": [{"text": "读一下探测文件"}]},
        {
            "role": "model",
            "parts": [{"functionCall": {"name": "read_file", "args": {"Path": "x"}}}],
        },
        {
            "role": "model",
            "parts": [{"functionResponse": {"name": "read_file", "response": {"output": "ok"}}}],
        },
    ])
    payload, rid = await wrap_cli_request(request, MODEL, "proj")
    assert rid.split("/")[4] == "3"
    assert payload["request"]["labels"]["last_step_index"] == "2"
    assert payload["request"]["labels"]["request_id"].endswith("-0")


async def test_step_counts_parallel_calls_per_part():
    """实抓 163547/0020 形态:user + model(fc×2) + model(fr×2) → 自然步号
    = 1 user + 1 fc 轮 + 2 fc part = 4(并行调用按 part 数计,fr 不计)。

    旧公式 1+2×model轮=3 会漏掉并行调用的差异;该差异只在真实转发会话暴露。
    """
    request = _req([
        {"role": "user", "parts": [{"text": "并行读两个文件"}]},
        {
            "role": "model",
            "parts": [
                {"functionCall": {"name": "read_file", "args": {"Path": "a"}}},
                {"functionCall": {"name": "read_file", "args": {"Path": "b"}}},
            ],
        },
        {
            "role": "model",
            "parts": [
                {"functionResponse": {"name": "read_file", "response": {"output": "a"}}},
                {"functionResponse": {"name": "read_file", "response": {"output": "b"}}},
            ],
        },
    ])
    payload, rid = await wrap_cli_request(request, MODEL, "proj")
    assert rid.split("/")[4] == "4"
    assert payload["request"]["labels"]["last_step_index"] == "3"


async def test_step_excludes_function_response_rounds():
    """实抓 163547 文本收尾形态:[user, model(fc), model(fr), model(text), user]
    → 自然步号 = 2 user + 1 fc 轮 + 1 text 轮 + 1 fc part = 5(fr 不计轮)。
    若裸数 role==model 会错算成 7。"""
    request = _req([
        {"role": "user", "parts": [{"text": "查一下"}]},
        {"role": "model", "parts": [{"functionCall": {"name": "read_file", "args": {"Path": "x"}}}]},
        {"role": "model", "parts": [{"functionResponse": {"name": "read_file", "response": {"output": "ok"}}}]},
        {"role": "model", "parts": [{"text": "结果是……"}]},
        {"role": "user", "parts": [{"text": "继续"}]},
    ])
    payload, rid = await wrap_cli_request(request, MODEL, "proj")
    assert rid.split("/")[4] == "5"
    assert payload["request"]["labels"]["last_step_index"] == "4"


async def test_shared_system_sessions_isolated():
    """sessionId 装机级恒定（实抓 163547/193455 跨会话同值）；不同会话靠
    trajectory/cascade 隔离。"""
    pa, _ = await wrap_cli_request(_session_a_turn1(), MODEL, "proj")
    pb, _ = await wrap_cli_request(_session_b(), MODEL, "proj")
    assert pa["request"]["sessionId"] == pb["request"]["sessionId"]
    assert pa["request"]["labels"]["trajectory_id"] != pb["request"]["labels"]["trajectory_id"]


def _tool_cycle_history(*, with_text_tail: bool):
    """构造一个完整工具周期历史:user → fc → fr → [text]。"""
    contents = [
        {"role": "user", "parts": [{"text": "查一下"}]},
        {"role": "model", "parts": [{"functionCall": {"name": "read_file", "id": "c1", "args": {"Path": "x"}}}]},
        {"role": "model", "parts": [{"functionResponse": {"name": "read_file", "id": "c1", "response": {"output": "ok"}}}]},
    ]
    if with_text_tail:
        contents.append({"role": "model", "parts": [{"text": "结果是……"}]})
    return contents


async def test_last_execution_id_absent_without_fr_before_last_user():
    """无工具执行 → 九键;fr 在最后一个 user 轮之后(纯工具续跑)→ 九键。

    实抓 163547:0013-0067(第一个 user 周期内的工具续跑)全部九键,
    0074(新 user 轮开启且此前有工具执行)才出现 last_execution_id。
    """
    p, _ = await wrap_cli_request(_req(_session_a_turn1()["contents"]), MODEL, "proj")
    assert "last_execution_id" not in p["request"]["labels"]

    tool_run = _req([
        {"role": "user", "parts": [{"text": "查一下"}]},
        {"role": "model", "parts": [{"functionCall": {"name": "read_file", "id": "c1", "args": {"Path": "x"}}}]},
        {"role": "model", "parts": [{"functionResponse": {"name": "read_file", "id": "c1", "response": {"output": "ok"}}}]},
    ])
    p2, _ = await wrap_cli_request(tool_run, MODEL, "proj")
    assert "last_execution_id" not in p2["request"]["labels"]


async def test_last_execution_id_appears_after_tool_cycle():
    """工具周期结束 + 新 user 轮 → 十键,键序在 cascade_id 之后(实抓 0074 形态)。"""
    contents = _tool_cycle_history(with_text_tail=True)
    contents.append({"role": "user", "parts": [{"text": "继续"}]})
    p, _ = await wrap_cli_request(_req(contents), MODEL, "proj")
    labels = p["request"]["labels"]
    assert "last_execution_id" in labels
    assert list(labels.keys())[:2] == ["cascade_id", "last_execution_id"]
    # 值为合法 uuid v4 形态
    value = uuid.UUID(labels["last_execution_id"])
    assert value.version == 4


async def test_last_execution_id_stable_within_cycle_then_refreshes():
    """同一工具轮在同一 user 周期内保持同值;新 user 轮引用新工具轮后更替。

    实抓 163547:0074/0077/0078 同值 X,0080/0083/0084/0085 同值 Z,X≠Z。
    """
    contents_a = _tool_cycle_history(with_text_tail=True)
    contents_a.append({"role": "user", "parts": [{"text": "继续"}]})
    pa, _ = await wrap_cli_request(_req(contents_a), MODEL, "proj")
    lei_a = pa["request"]["labels"]["last_execution_id"]

    # 同一周期内工具续跑(fr 结尾):值保持不变(实抓 0077/0078)
    contents_b = _tool_cycle_history(with_text_tail=True)
    contents_b.append({"role": "user", "parts": [{"text": "继续"}]})
    contents_b.append({"role": "model", "parts": [{"functionCall": {"name": "write_file", "id": "c2", "args": {"Path": "y"}}}]})
    contents_b.append({"role": "model", "parts": [{"functionResponse": {"name": "write_file", "id": "c2", "response": {"output": "ok"}}}]})
    pb, _ = await wrap_cli_request(_req(contents_b), MODEL, "proj")
    assert pb["request"]["labels"]["last_execution_id"] == lei_a

    # 新 user 轮引用新工具轮(fr 轮在最后一个 user 轮之前更替):值更替(实抓 0080)
    contents_c = contents_b + [{"role": "model", "parts": [{"text": "改好了"}]},
                               {"role": "user", "parts": [{"text": "再看一遍"}]}]
    pc, _ = await wrap_cli_request(_req(contents_c), MODEL, "proj")
    lei_c = pc["request"]["labels"]["last_execution_id"]
    assert lei_c != lei_a

    # 更替后的周期内续跑再次保持同值(实抓 0083-0085)
    pd, _ = await wrap_cli_request(_req(contents_c), MODEL, "proj")
    assert pd["request"]["labels"]["last_execution_id"] == lei_c


async def test_different_model_isolated():
    """不同模型：sessionId 装机级恒定，trajectory/cascade 隔离（锚点含模型名）。"""
    pa, _ = await wrap_cli_request(_session_a_turn1(), MODEL, "proj")
    pb, _ = await wrap_cli_request(_session_a_turn1(), "claude-sonnet-5-5-medium", "proj")
    assert pa["request"]["sessionId"] == pb["request"]["sessionId"]
    assert pa["request"]["labels"]["trajectory_id"] != pb["request"]["labels"]["trajectory_id"]


def test_install_session_id_is_official_constant():
    """sessionId 全体客户端恒等的常量（静态逆向 2026-11-21：真实 LS 对空输入做
    FNV-1a64 → offset basis 0xCBF29CE484222325 = int64 -3750763034362895579；
    实抓 163547/193455 同值）。换账号盐也不变，会话隔离由 trajectory/cascade 承担。"""
    ids_a = _derive_upstream_session_ids(_session_a_turn1(), MODEL, "cred-1")
    ids_b = _derive_upstream_session_ids(_session_b(), MODEL, "cred-1")
    ids_other = _derive_upstream_session_ids(_session_a_turn1(), MODEL, "cred-2")
    expected = struct.unpack("<q", struct.pack("<Q", 0xCBF29CE484222325))[0]
    assert ids_a.session_id == ids_b.session_id == ids_other.session_id
    assert ids_a.session_id == str(expected) == "-3750763034362895579"
    # 无账号盐同样得到该常量
    ids_no_salt = _derive_upstream_session_ids(_session_a_turn1(), MODEL, "")
    assert ids_no_salt.session_id == "-3750763034362895579"
    # 隔离信号在 trajectory/cascade：同内容不同盐 → 不同轨迹
    assert ids_a.trajectory_id != ids_other.trajectory_id
    uuid.UUID(ids_a.trajectory_id)
    uuid.UUID(ids_a.cascade_id)


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
    """labels 键序（官方字母序）与 requestType 判定保持实抓行为。"""
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
    # 无工具执行轮的会话(纯文本)为九键形态:该键整体不出现
    # (实抓 163547:0013-0067 九键,0074 起工具执行后十键)
    assert "last_execution_id" not in labels
    assert labels["used_claude"] == "false"
    assert labels["used_non_gemini_model"] == "false"
    # 无 tools 的纯文本请求也是 agent(实抓 193455:纯文本问候同样 agent+16 工具
    # 声明,chat 在实抓中 0 次出现)
    assert p["requestType"] == "agent"

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
    # sessionId 装机级稳定（实抓 163547/193455 跨会话同值），不再取自对话锚点摘要；
    # trajectory/cascade 仍与 derive_session_key 联合锚点同源（无账号盐时摘要原样）
    from src.session_cache import derive_conversation_anchor
    digest = derive_conversation_anchor(MODEL, _session_a_turn1())
    assert str(uuid.UUID(bytes=digest[8:24])) == ids1.trajectory_id


def test_generate_request_id_format():
    rid = _generate_request_id("afb38f6a-74b6-459f-8edd-a75d8d00bc19", 5, "d3a2e3d2-21f9-411b-967f-53811898c2cd")
    parts = rid.split("/")
    assert parts[0] == "agent"
    assert parts[1] == "afb38f6a-74b6-459f-8edd-a75d8d00bc19"
    assert parts[3] == "d3a2e3d2-21f9-411b-967f-53811898c2cd"
    assert parts[4] == "5"
    assert len(parts) == 5


_REPO_ROOT = Path(__file__).resolve().parent.parent
# 唯一实抓基准:163547 = 转发模式真实开发会话(owner 2026-11-21 指正,
# 114847 等旧会话为离线模拟,不作地面真值)。0080 为十键 labels 形态。
_CAPTURE_SAMPLE = (
    _REPO_ROOT
    / "tools"
    / "antigravity_capture"
    / "captures"
    / "20261121-163547"
    / "0080_streamGenerateContent.request.bin"
)


@pytest.mark.skipif(
    not _CAPTURE_SAMPLE.is_file(),
    reason="缺少实抓样本（tools/antigravity_capture/captures 已被 gitignore）",
)
async def test_envelope_matches_captured_sample_shape():
    """与实抓报文（163547/0080，工具周期后新 user 轮的十键形态）做结构级
    黄金对照（不比较 UUID / 时间戳等易变值）。"""
    sample = json.loads(_CAPTURE_SAMPLE.read_text(encoding="utf-8"))
    # 构造与实抓同构的十键形态:工具周期 + 文本收尾 + 新 user 轮
    contents = _tool_cycle_history(with_text_tail=True)
    contents.append({"role": "user", "parts": [{"text": "继续"}]})
    payload, _rid = await wrap_cli_request(_req(contents), MODEL, "proj")

    # 顶层信封键序一致
    assert list(payload.keys()) == list(sample.keys())
    # labels 键序与键集一致(十键)
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


class TestEnvelopePartsAndSidecar:
    """信封下沉 Go 的结构化输入与双作曲架构。"""

    def _set_impersonation(self, monkeypatch, enabled: bool):
        async def _flag():
            return enabled

        monkeypatch.setattr(
            "src.api.antigravity.get_upstream_tls_impersonation_enabled", _flag
        )

    async def test_parts_shape_and_labels_placeholder(self):
        parts = await build_envelope_parts(_session_a_turn3(), MODEL, "proj")
        keys = [key for key, _value in parts.inner_entries]
        # 无 tools 纯文本请求(实抓同形态)内层键序:contents → systemInstruction → labels → sessionId
        assert keys == ["contents", "systemInstruction", "labels", "sessionId"]
        # labels 位是 None 占位(字节由 Go 产出)
        assert dict(parts.inner_entries)["labels"] is None
        assert parts.anchors["step"] == 5
        assert parts.anchors["lastStepIndex"] == "4"
        assert parts.anchors["requestIdLabel"] == f"{parts.trajectory_id}-0"
        assert parts.anchors["cascadeId"] == parts.anchors["rootCascadeId"]
        # 纯文本请求也是 agent(实抓 193455;chat 0 次出现)
        assert parts.request_type == "agent"

    async def test_compose_python_envelope_matches_wrap(self):
        """参照作曲与 wrap_cli_request 完全等价(同一序列计数下)。"""
        _reset_trajectory_request_seq()
        payload, rid = await wrap_cli_request(_session_a_turn3(), MODEL, "proj")
        _reset_trajectory_request_seq()
        parts = await build_envelope_parts(_session_a_turn3(), MODEL, "proj")
        payload2, rid2 = compose_python_envelope(parts)
        assert payload == payload2
        assert rid == rid2

    async def test_compose_sidecar_body_shape(self):
        parts = await build_envelope_parts(_session_a_turn3(), MODEL, "proj")
        body = compose_sidecar_body(parts)
        assert body["model"] == MODEL
        assert body["requestType"] == parts.request_type
        assert body["project"] == "proj"
        assert body["anchors"]["trajectoryId"] == parts.trajectory_id

        entries = {key: value for key, value in body["innerEntries"]}
        assert list(entries) == ["contents", "systemInstruction", "labels", "sessionId"]
        assert entries["labels"] is None
        # 值片是与 httpx json= 逐字节相同的紧凑 JSON 文本
        contents_value = next(v for k, v in parts.inner_entries if k == "contents")
        assert entries["contents"] == json.dumps(
            contents_value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )

    async def test_prepare_cli_request_go_mode(self, monkeypatch):
        self._set_impersonation(monkeypatch, True)
        prepared = await prepare_cli_request(_session_a_turn3(), MODEL, "proj")
        assert prepared.extra_headers == {UPSTREAM_BUILD_ENVELOPE_HEADER: "1"}
        assert prepared.body["anchors"]["step"] == 5
        assert prepared.body["project"] == "proj"
        assert prepared.request_id == ""  # requestId 由 Go 构造

    async def test_prepare_cli_request_python_mode(self, monkeypatch):
        self._set_impersonation(monkeypatch, False)
        prepared = await prepare_cli_request(_session_a_turn3(), MODEL, "proj")
        assert prepared.extra_headers == {}
        assert prepared.request_id.startswith("agent/")
        assert prepared.body["request"]["labels"]["trajectory_id"] == prepared.trajectory_id
        # 两种模式 project 改写位置统一(换凭证场景)
        prepared.body["project"] = "proj2"
        assert prepared.body["project"] == "proj2"


# ==================== user 轮 <USER_REQUEST> 包装(§10.13) ====================


async def test_user_turn_wrapped_like_real_client():
    """user 轮按实抓格式包装(163547/193455 全部 60 条 agent 请求,无例外):

    <USER_REQUEST>\n{原文}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\n
    The current local time is: {ISO8601 本地时区}.\n</ADDITIONAL_METADATA>
    逐字节对照实抓 0013/0074;USER_SETTINGS_CHANGE 是 UI 事件,代理永不注入。
    """
    p, _ = await wrap_cli_request(_session_a_turn1(), MODEL, "proj")
    text = p["request"]["contents"][0]["parts"][0]["text"]
    m = re.fullmatch(
        r"<USER_REQUEST>\n帮我重构这个模块\n</USER_REQUEST>\n"
        r"<ADDITIONAL_METADATA>\nThe current local time is: "
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})\.\n"
        r"</ADDITIONAL_METADATA>",
        text,
    )
    assert m, repr(text)


async def test_user_wrap_stable_across_requests():
    """历史 user 轮包装逐字节稳定(实抓 0080:contents[0] 时间仍是首轮 0013 值)。"""
    p1, _ = await wrap_cli_request(_session_a_turn1(), MODEL, "proj")
    first_wrapped = p1["request"]["contents"][0]["parts"][0]["text"]
    p3, _ = await wrap_cli_request(_session_a_turn3(), MODEL, "proj")
    assert p3["request"]["contents"][0]["parts"][0]["text"] == first_wrapped
    # 三个 user 轮各自有包装;时间戳随首次见到定格,不同轮时间可不同
    texts = [
        c["parts"][0]["text"]
        for c in p3["request"]["contents"]
        if c["role"] == "user"
    ]
    assert all(t.startswith("<USER_REQUEST>\n") for t in texts)
    assert len({t for t in texts}) == 3


async def test_user_wrap_idempotent_with_captured_sample():
    """实抓重放(已包装文本)原样通过,绝不二次包装(黄金样本 163547/0080 形态)。"""
    captured = (
        "<USER_REQUEST>\n你好\n</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\n"
        "The current local time is: 2026-11-21T19:35:25+08:00.\n"
        "</ADDITIONAL_METADATA>\n"
        "<USER_SETTINGS_CHANGE>\n"
        "The user changed setting `Model Selection` from None to "
        "Gemini 4 Hydrogen (XHigh). No need to comment on this change if the "
        "user doesn't ask about it. If reporting what model you are, please use "
        "a human readable name instead of the exact string.\n"
        "</USER_SETTINGS_CHANGE>"
    )
    p, _ = await wrap_cli_request(_req([{"role": "user", "parts": [{"text": captured}]}]), MODEL, "proj")
    assert p["request"]["contents"][0]["parts"][0]["text"] == captured


async def test_user_wrap_skipped_for_image_models():
    """图片生成模型无实抓包装证据,整体跳过(无证据不注入)。"""
    p, _ = await wrap_cli_request(
        _session_a_turn1(), "gemini-2.5-flash-image", "proj"
    )
    assert p["request"]["contents"][0]["parts"][0]["text"] == "帮我重构这个模块"


async def test_user_wrap_edit_restamp_and_delete_keeps_stamp():
    """调用方改写某轮文本 → 该轮重新盖戳;删除中间轮 → 其余轮戳值不变
    (键 = sha256(text),与索引无关)。"""
    p1, _ = await wrap_cli_request(_session_a_turn1(), MODEL, "proj")
    stamp1 = p1["request"]["contents"][0]["parts"][0]["text"]

    # turn3 历史含同一首轮文本 → 逐字节稳定(回归 0080 口径)
    p3a, _ = await wrap_cli_request(_session_a_turn3(), MODEL, "proj")
    p3b, _ = await wrap_cli_request(_session_a_turn3(), MODEL, "proj")
    assert p3a["request"]["contents"][0]["parts"][0]["text"] == stamp1
    assert (
        p3b["request"]["contents"][0]["parts"][0]["text"] == stamp1
    ), "同轨迹同文本跨请求戳值必须恒定"


async def test_sys_evolution_keeps_trajectory_continuous():
    """§10.9.6 修复回归:sys 会话内演化(0013↔0080 段落差异)不得换 trajectory
    ——trajectory/step 计数/user 轮盖戳全部连续(真实客户端 trajectory 诞生即
    定格,真实 sys 由实验旗标驱动演化)。"""
    p1, rid1 = await wrap_cli_request(_session_a_turn1(), MODEL, "proj")
    traj1 = p1["request"]["labels"]["trajectory_id"]
    first_wrapped = p1["request"]["contents"][0]["parts"][0]["text"]

    evolved = _session_a_turn3()
    evolved["systemInstruction"] = {"parts": [{"text": "You are Antigravity with <artifacts> 段落"}]}
    p2, rid2 = await wrap_cli_request(evolved, MODEL, "proj")
    assert p2["request"]["labels"]["trajectory_id"] == traj1, "sys 演化不得换 trajectory"
    # step 连续(无归零):turn3 自然步号 5,无重发偏移
    assert rid2.split("/")[4] == "5"
    # 历史首轮包装文本逐字节保留(盖戳注册表同样按 trajectory 连续)
    assert p2["request"]["contents"][0]["parts"][0]["text"] == first_wrapped
