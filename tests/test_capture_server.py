"""离线脚本化响应（--offline-script）的单元与集成测试。

安全约束：只允许 read_file（本会话临时文件）与 invoke_subagent(research)，
绝不合成 run_command / write_file 这类有副作用的工具。
"""

import json
import os
import sys
import threading
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "antigravity_capture"))

import capture_server  # noqa: E402

TOOLS = [
    {
        "functionDeclarations": [
            {"name": "read_file", "parameters": {"type": "OBJECT"}},
            {"name": "run_command", "parameters": {"type": "OBJECT"}},
            {"name": "invoke_subagent", "parameters": {"type": "OBJECT"}},
        ]
    }
]


def _request(contents=None, tools=True):
    request = {
        "contents": contents
        or [{"role": "user", "parts": [{"text": "hi"}]}],
    }
    if tools:
        request["tools"] = TOOLS
    return request


class TestScriptedCallBuilder:
    def test_tool_call_uses_read_file_with_probe_path(self, tmp_path):
        call = capture_server.build_scripted_function_call(
            "tool-call", _request(), tmp_path / "probe.txt"
        )
        assert call["name"] == "read_file"
        assert call["args"]["Path"].endswith("probe.txt")
        assert call["name"] != "run_command"

    def test_subagent_uses_research(self, tmp_path):
        call = capture_server.build_scripted_function_call(
            "subagent", _request(), tmp_path / "probe.txt"
        )
        assert call["name"] == "invoke_subagent"
        assert call["args"]["TypeName"] == "research"

    def test_requires_declared_tool(self, tmp_path):
        request = _request(tools=False)
        assert (
            capture_server.build_scripted_function_call(
                "tool-call", request, tmp_path / "probe.txt"
            )
            is None
        )

    def test_skips_after_tool_interaction(self, tmp_path):
        request = _request(
            contents=[
                {"role": "user", "parts": [{"text": "hi"}]},
                {
                    "role": "user",
                    "parts": [{"functionResponse": {"name": "read_file", "response": {}}}],
                },
            ]
        )
        assert (
            capture_server.build_scripted_function_call(
                "tool-call", request, tmp_path / "probe.txt"
            )
            is None
        )

    def test_wrapped_envelope_is_supported(self, tmp_path):
        """真实请求是 {"request": {...}} 信封，工具声明在信封内层。"""
        wrapped = {"project": "p", "request": _request()}
        call = capture_server.build_scripted_function_call(
            "tool-call", wrapped, tmp_path / "probe.txt"
        )
        assert call is not None and call["name"] == "read_file"

    def test_unknown_mode(self, tmp_path):
        assert (
            capture_server.build_scripted_function_call(
                "none", _request(), tmp_path / "probe.txt"
            )
            is None
        )

    def test_declared_tool_names_and_tool_parts(self):
        assert "read_file" in capture_server.declared_tool_names(_request())
        assert not capture_server.contents_have_tool_parts(_request())
        assert capture_server.contents_have_tool_parts(
            {
                "contents": [
                    {"role": "model", "parts": [{"functionCall": {"name": "x"}}]}
                ]
            }
        )


class _ServerHarness:
    def __init__(self, tmp_path, script):
        self.store = capture_server.CaptureStore(tmp_path, redact=True)
        self.server = capture_server.CaptureHTTPServer(
            ("127.0.0.1", 0),
            capture_server.CaptureHandler,
            store=self.store,
            upstream_url="https://example.invalid",
            capture_all=True,
            capture_methods=[],
            offline_mode="all",
        )
        self.server.offline_script = script
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def post(self, body):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1internal:streamGenerateContent?alt=sse",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestOfflineScriptIntegration:
    def test_tool_call_fires_once(self, tmp_path):
        harness = _ServerHarness(tmp_path, "tool-call")
        try:
            first = harness.post(_request())
            assert b'"functionCall"' in first
            assert b'"read_file"' in first
            assert b"_probe_read_file.txt" in first
            assert (harness.store.session_dir / "_probe_read_file.txt").is_file()

            second = harness.post(_request())
            assert b'"functionCall"' not in second
            assert "离线模式".encode("utf-8") in second
        finally:
            harness.close()

    def test_wrapped_envelope_fires(self, tmp_path):
        harness = _ServerHarness(tmp_path, "tool-call")
        try:
            first = harness.post({"project": "p", "request": _request()})
            assert b'"functionCall"' in first
            assert b'"read_file"' in first
        finally:
            harness.close()

    def test_subagent_fires_once(self, tmp_path):
        harness = _ServerHarness(tmp_path, "subagent")
        try:
            first = harness.post(_request())
            assert b'"invoke_subagent"' in first
            assert b'"research"' in first
            second = harness.post(_request())
            assert b'"functionCall"' not in second
        finally:
            harness.close()
