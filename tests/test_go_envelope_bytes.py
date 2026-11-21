"""Go 信封构造与 Python 参照的字节级等价测试(报文唯一作者架构的核心保证)。

地面真值是实抓报文(captures 目录,gitignore,本地留存);Python 参照作曲
只是**确定性夹具**——逐字节 diff 需要固定文本,而实抓里的 uuid/时间戳/
sessionId 每次都不同,无法直接逐字节比对。因此验证链是:

  实抓数据 ──(fidelity_schema.json / verify --envelope / 本文件实抓对照)──▶ Python 参照
  Python 参照 ──(字节等价测试)──▶ Go

本文件三个测试分别守住链的每一环:
1. test_captured_raw_body_is_compact       实抓原始字节 ≡ 紧凑序列化重建(格式钉死)
2. test_go_envelope_matches_captured_sample 把实抓静态内容喂进管线,Go 构造后
   与实抓报文归一化逐字节对照(键序/静态值/格式)
3. test_go_envelope_bytes_match_python_reference Go ≡ Python(确定性夹具)

Go 未编译或协议过旧时 skip,不阻塞常规测试。
"""

import asyncio
import copy
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

from src import upstream_proxy  # noqa: E402
from src.api.antigravity import (  # noqa: E402
    UPSTREAM_BUILD_ENVELOPE_HEADER,
    _reset_trajectory_request_seq,
    build_envelope_parts,
    compose_python_envelope,
    compose_sidecar_body,
)

MODEL = "gemini-4-carbon-medium"

# 实抓黄金样本:2.34.0 真实 agent 形态信封(gemini-4-hydrogen-medium,
# 工具轮 [user, model(fc), model(fr)] → step=3,labels 九键,
# generationConfig 含 MEDIUM thinking;owner 指正:取工具正常的最新会话,
# 101457/101904 长序列为重试风暴不作数)
_CAPTURE_SAMPLE = (
    REPO_ROOT
    / "tools"
    / "antigravity_capture"
    / "captures"
    / "20261121-114847"
    / "0014_streamGenerateContent.request.bin"
)

# 特殊字符覆盖:中文、引号、反斜杠、换行、<>&(Go 默认 HTML 转义,必须关闭)
_SPECIAL_TEXT = "你好\"world\"\n第二行\\slash<>&tail"

# 特殊字符覆盖:中文、引号、反斜杠、换行、<>&(Go 默认 HTML 转义,必须关闭)
_SPECIAL_TEXT = "你好\"world\"\n第二行\\slash<>&tail"


def _request():
    return {
        "systemInstruction": {"parts": [{"text": _SPECIAL_TEXT}]},
        "contents": [
            {"role": "user", "parts": [{"text": "帮我重构模块"}]},
            {"role": "model", "parts": [{"text": "好的"}]},
            {"role": "user", "parts": [{"text": "再补测试"}]},
        ],
        "tools": [
            {"functionDeclarations": [{"name": "read_file", "parameters": {"type": "object"}}]}
        ],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 65536},
    }


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _Recorder(BaseHTTPRequestHandler):
    """记录 Go 转发来的最终上游报文(请求体为 chunked,手工解帧)。"""

    received: list = []

    def do_POST(self):  # noqa: N802 - http.server 约定
        body = self._read_chunked()
        _Recorder.received.append((self.path, body))
        payload = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_chunked(self) -> bytes:
        chunks = []
        while True:
            size = int(self.rfile.readline().strip().split(b";")[0], 16)
            if size == 0:
                self.rfile.readline()
                break
            chunks.append(self.rfile.read(size))
            self.rfile.readline()
        return b"".join(chunks)

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture()
def forwarder():
    """拉起真实 Go 转发器(空闲端口);二进制缺失或协议过旧则 skip。"""
    if upstream_proxy.find_binary() is None:
        pytest.skip("缺少已编译的 Go 转发器二进制(tools/upstream_tls_proxy/bin)")
    listen = f"http://127.0.0.1:{_free_port()}"
    if not upstream_proxy.ensure_running(listen):
        pytest.skip("Go 转发器不可用(启动失败或协议版本过旧)")
    yield listen
    upstream_proxy.stop()


@pytest.fixture()
def dummy_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    async def _strict_off():
        return False

    monkeypatch.setattr("src.api.antigravity.get_strict_fidelity_enabled", _strict_off)
    _Recorder.received.clear()
    _reset_trajectory_request_seq()
    yield
    _reset_trajectory_request_seq()


@pytest.mark.skipif(
    not _CAPTURE_SAMPLE.is_file(),
    reason="缺少实抓样本(tools/antigravity_capture/captures 已被 gitignore)",
)
def test_captured_raw_body_is_compact():
    """格式环:真实客户端的原始报文 ≡ 紧凑序列化重建(逐字节)。

    证明真实 language_server 的 JSON 就是紧凑分隔符 + 非 ASCII 原样——
    两个作曲方使用的 json.dumps 参数(ensure_ascii=False, separators=(",", ":"))由此钉死。
    """
    raw = _CAPTURE_SAMPLE.read_bytes()
    env = json.loads(raw.decode("utf-8"))
    rebuilt = json.dumps(env, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert rebuilt == raw, "实抓原始字节与紧凑重建不一致——真实客户端的序列化格式与作曲方参数有差异"


def _normalize_envelope(env: dict) -> str:
    """实抓对照的动态值归一(uuid/时间戳/sessionId),键序与其余值逐字节保留。"""
    env = copy.deepcopy(env)
    env["requestId"] = "<REQID>"
    request = env["request"]
    if "sessionId" in request:
        request["sessionId"] = "<SID>"
    labels = request.get("labels")
    if isinstance(labels, dict):
        for key in ("cascade_id", "request_id", "root_cascade_id", "trajectory_id"):
            if key in labels:
                labels[key] = "<UUID>"
    return json.dumps(env, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@pytest.mark.skipif(
    not _CAPTURE_SAMPLE.is_file(),
    reason="缺少实抓样本(tools/antigravity_capture/captures 已被 gitignore)",
)
def test_go_envelope_matches_captured_sample(forwarder, dummy_upstream):
    """实抓环:真实报文的静态内容经管线由 Go 构造,与实抓归一化逐字节对照。

    键序(三层)、静态值(contents/systemInstruction/tools/generationConfig 原样)、
    labels 静态值(last_step_index/model_enum/used_*)、requestId 形态与 step
    全部以实抓为基准;仅 uuid/时间戳类动态值归一。
    """
    sample = json.loads(_CAPTURE_SAMPLE.read_text(encoding="utf-8"))
    captured_request = sample["request"]

    # 静态内容原样喂入(labels/sessionId 由管线重新生成,不取实抓动态值)
    client_request = {
        key: value
        for key, value in captured_request.items()
        if key not in ("labels", "sessionId")
    }
    parts = asyncio.run(build_envelope_parts(client_request, sample["model"], sample["project"]))
    wrapper = json.dumps(
        compose_sidecar_body(parts), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")

    resp = httpx.post(
        f"{forwarder}/v1internal:generateContent",
        content=wrapper,
        headers={
            "Content-Type": "application/json",
            "X-Upstream-URL": f"{dummy_upstream}/v1internal:generateContent",
            UPSTREAM_BUILD_ENVELOPE_HEADER: "1",
        },
        timeout=30.0,
    )
    assert resp.status_code == 200, resp.text
    assert len(_Recorder.received) == 1
    _path, actual = _Recorder.received[0]
    go_env = json.loads(actual)

    # 三层键序逐层对照实抓
    assert list(go_env.keys()) == list(sample.keys()), "顶层六键序与实抓不一致"
    assert list(go_env["request"].keys()) == list(captured_request.keys()), "内层键序与实抓不一致"
    assert list(go_env["request"]["labels"].keys()) == list(
        captured_request["labels"].keys()
    ), "labels 九键序与实抓不一致"

    # requestId:第 1 段 agent、末段 step 与实抓完全一致(实抓 593 = 1 + 2×296)
    go_segments = go_env["requestId"].split("/")
    sample_segments = sample["requestId"].split("/")
    assert go_segments[0] == sample_segments[0] == "agent"
    assert go_segments[-1] == sample_segments[-1]
    assert go_segments[2].isdigit() and sample_segments[2].isdigit()

    # 归一化逐字节对照(动态值归一,其余含全部静态值与格式必须相等)
    assert _normalize_envelope(go_env) == _normalize_envelope(sample), (
        f"\n--- Go 实际 ---\n{_normalize_envelope(go_env)[:1200]}"
        f"\n--- 实抓期望 ---\n{_normalize_envelope(sample)[:1200]}"
    )


def test_go_envelope_bytes_match_python_reference(forwarder, dummy_upstream):
    """Go 构造的上游报文 ≡ Python 参照字节(仅 requestId 归一)。"""
    parts = asyncio.run(build_envelope_parts(_request(), MODEL, "proj-x"))
    wrapper = compose_sidecar_body(parts)
    reference_payload, reference_rid = compose_python_envelope(parts)

    expected = json.dumps(
        reference_payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    # requestId 时间戳由 Go 构造时生成(每次请求一次,重试复用),按占位归一
    expected = expected.replace(
        f'"requestId":"{reference_rid}"'.encode(), b'"requestId":"<REQID>"'
    )

    resp = httpx.post(
        f"{forwarder}/v1internal:generateContent",
        content=json.dumps(
            wrapper, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Upstream-URL": f"{dummy_upstream}/v1internal:generateContent",
            UPSTREAM_BUILD_ENVELOPE_HEADER: "1",
        },
        timeout=30.0,
    )
    assert resp.status_code == 200, resp.text

    assert len(_Recorder.received) == 1
    path, actual = _Recorder.received[0]
    assert path == "/v1internal:generateContent"

    # requestId 形态与锚点对齐(五段、agent 前缀、cascade/trajectory/step 与 labels 一致)
    recorded = json.loads(actual)
    segments = recorded["requestId"].split("/")
    assert len(segments) == 5
    assert segments[0] == "agent"
    assert segments[1] == parts.anchors["cascadeId"]
    assert segments[2].isdigit()
    assert segments[3] == parts.anchors["trajectoryId"]
    assert segments[4] == str(parts.anchors["step"])

    # 字节级等价:json.loads 保持文档键序,归一 requestId 后必须逐字节相等
    recorded["requestId"] = "<REQID>"
    actual_normalized = json.dumps(
        recorded, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    assert actual_normalized == expected, (
        f"\n--- Go 实际 ---\n{actual_normalized[:800].decode('utf-8', 'replace')}"
        f"\n--- Python 期望 ---\n{expected[:800].decode('utf-8', 'replace')}"
    )


def test_go_envelope_deterministic_per_body(forwarder, dummy_upstream):
    """同一 wrapper 重复发送:除时间戳外完全一致(重试路径复用同一 body 的前提)。"""
    parts = asyncio.run(build_envelope_parts(_request(), MODEL, "proj-x"))
    wrapper = json.dumps(
        compose_sidecar_body(parts),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Upstream-URL": f"{dummy_upstream}/v1internal:generateContent",
        UPSTREAM_BUILD_ENVELOPE_HEADER: "1",
    }

    for _ in range(2):
        resp = httpx.post(
            f"{forwarder}/v1internal:generateContent", content=wrapper, headers=headers,
            timeout=30.0,
        )
        assert resp.status_code == 200

    assert len(_Recorder.received) == 2
    body1, body2 = _Recorder.received[0][1], _Recorder.received[1][1]
    parsed1, parsed2 = json.loads(body1), json.loads(body2)
    # requestId 时间戳段允许不同,其余字段(含内层与 labels)必须完全一致
    parsed1.pop("requestId"), parsed2.pop("requestId")
    assert parsed1 == parsed2
