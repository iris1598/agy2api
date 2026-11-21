"""Go 出站 TLS 转发器接入层的单元测试（不联网、不依赖 Go 二进制）。

覆盖：
- httpx_client 的 URL 改写（纯函数）与启用/降级判定
- upstream_proxy 的二进制定位与"端口已在监听则复用"
"""

import os
import socket
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest  # noqa: E402

import src.httpx_client as hc  # noqa: E402
from src import upstream_proxy  # noqa: E402

API_URL = "https://daily-cloudcode-pa.googleapis.com"
CHAT_URL = f"{API_URL}/v1internal:streamGenerateContent?alt=sse"
SIDECAR = "http://127.0.0.1:17890"


class TestSidecarRequestRewrite:
    def test_rewrites_host_keeps_path_and_query(self):
        new_url, extra = hc._build_sidecar_request(CHAT_URL, SIDECAR, None)
        assert new_url == (
            "http://127.0.0.1:17890/v1internal:streamGenerateContent?alt=sse"
        )
        assert extra == {"X-Upstream-URL": CHAT_URL}

    def test_passes_account_proxy(self):
        _url, extra = hc._build_sidecar_request(
            CHAT_URL, SIDECAR, "socks5://127.0.0.1:1080"
        )
        assert extra["X-Upstream-Proxy"] == "socks5://127.0.0.1:1080"

    def test_no_path_defaults_to_root(self):
        new_url, _extra = hc._build_sidecar_request(API_URL, SIDECAR, None)
        assert new_url == "http://127.0.0.1:17890/"


class TestImpersonationDecision:
    async def test_disabled_returns_none(self, monkeypatch):
        async def _false():
            return False

        monkeypatch.setattr(hc, "get_upstream_tls_impersonation_enabled", _false)
        assert await hc._impersonation_request(CHAT_URL, None) is None

    async def test_non_api_host_returns_none(self, monkeypatch):
        async def _true():
            return True

        async def _api():
            return API_URL

        monkeypatch.setattr(hc, "get_upstream_tls_impersonation_enabled", _true)
        monkeypatch.setattr(hc, "get_antigravity_api_url", _api)
        assert await hc._impersonation_request(
            "https://oauth2.googleapis.com/token", None
        ) is None

    async def test_http_scheme_returns_none(self, monkeypatch):
        async def _true():
            return True

        async def _api():
            return API_URL

        monkeypatch.setattr(hc, "get_upstream_tls_impersonation_enabled", _true)
        monkeypatch.setattr(hc, "get_antigravity_api_url", _api)
        assert await hc._impersonation_request(
            "http://daily-cloudcode-pa.googleapis.com/v1internal:x", None
        ) is None

    async def test_sidecar_unavailable_raises(self, monkeypatch):
        """严格模式：转发器不可用时直接失败，不回退 httpx。"""
        from src.fidelity import FidelityError

        async def _true():
            return True

        async def _api():
            return API_URL

        monkeypatch.setattr(hc, "get_upstream_tls_impersonation_enabled", _true)
        monkeypatch.setattr(hc, "get_antigravity_api_url", _api)
        monkeypatch.setattr(hc.upstream_proxy, "ensure_running", lambda _url: False)

        with pytest.raises(FidelityError) as exc_info:
            await hc._impersonation_request(CHAT_URL, None)
        assert exc_info.value.reason == "tls_sidecar_unavailable"
        assert exc_info.value.status_code == 503

    async def test_enabled_and_ready_returns_rewrite(self, monkeypatch):
        async def _true():
            return True

        async def _api():
            return API_URL

        async def _sidecar():
            return SIDECAR

        monkeypatch.setattr(hc, "get_upstream_tls_impersonation_enabled", _true)
        monkeypatch.setattr(hc, "get_antigravity_api_url", _api)
        monkeypatch.setattr(hc, "get_upstream_tls_proxy_url", _sidecar)
        monkeypatch.setattr(hc.upstream_proxy, "ensure_running", lambda _url: True)

        new_url, extra = await hc._impersonation_request(CHAT_URL, None)
        assert new_url.startswith(SIDECAR)
        assert extra["X-Upstream-URL"] == CHAT_URL


class TestBinaryDiscovery:
    def test_env_override(self, monkeypatch, tmp_path):
        binary = tmp_path / "upstream-proxy.exe"
        binary.write_bytes(b"stub")
        monkeypatch.setenv("UPSTREAM_TLS_PROXY_BIN", str(binary))
        assert upstream_proxy.find_binary() == binary

    def test_missing_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UPSTREAM_TLS_PROXY_BIN", str(tmp_path / "nope"))
        monkeypatch.setattr(upstream_proxy, "_BINARY_CANDIDATES", ())
        assert upstream_proxy.find_binary() is None

    def test_ensure_running_reuses_existing_listener(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            assert upstream_proxy.ensure_running(f"http://127.0.0.1:{port}") is True
        finally:
            server.close()

    def test_ensure_running_without_binary_fails(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_TLS_PROXY_BIN", "")
        monkeypatch.setattr(upstream_proxy, "_BINARY_CANDIDATES", ())
        # 用一个必然空闲的端口，避免命中外层监听
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        assert upstream_proxy.ensure_running(f"http://127.0.0.1:{port}") is False
