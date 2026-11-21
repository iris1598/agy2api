"""Go 信封构造与 Python 参照的字节级等价测试(报文唯一作者架构的核心保证)。

流程:build_envelope_parts → Python 参照作曲(httpx json= 同参序列化出期望
字节)→ 起真实 Go 转发器子进程 + 本地 dummy 上游(明文 http,Go 支持直连
不套 TLS)→ 带 X-Upstream-Build-Envelope 头 POST wrapper → 断言 dummy 收到
的上游报文与期望字节完全相等(仅 requestId 时间戳段按构造规则归一)。

Go 未编译或协议过旧时 skip,不阻塞常规测试。
"""

import asyncio
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

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
