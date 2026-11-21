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
        monkeypatch.setattr(upstream_proxy, "_BIN_DIR", tmp_path)
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
        monkeypatch.setattr(upstream_proxy, "_BIN_DIR", __import__("pathlib").Path("/nonexistent-agy2api-bin"))
        # 用一个必然空闲的端口，避免命中外层监听
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        assert upstream_proxy.ensure_running(f"http://127.0.0.1:{port}") is False


class TestPlatformAsset:
    """平台后缀产物名与 build-all.sh 产物命名对齐（release 下载路径）。"""

    def test_windows_amd64(self, monkeypatch):
        monkeypatch.setattr(upstream_proxy.sys, "platform", "win32")
        monkeypatch.setattr(upstream_proxy.platform, "machine", lambda: "AMD64")
        assert upstream_proxy._platform_asset_name() == "upstream-proxy-windows-amd64.exe"

    def test_linux_arm64(self, monkeypatch):
        monkeypatch.setattr(upstream_proxy.sys, "platform", "linux")
        monkeypatch.setattr(upstream_proxy.platform, "machine", lambda: "aarch64")
        assert upstream_proxy._platform_asset_name() == "upstream-proxy-linux-arm64"

    def test_linux_armv7(self, monkeypatch):
        monkeypatch.setattr(upstream_proxy.sys, "platform", "linux")
        monkeypatch.setattr(upstream_proxy.platform, "machine", lambda: "armv7l")
        assert upstream_proxy._platform_asset_name() == "upstream-proxy-linux-arm"

    def test_darwin_arm64(self, monkeypatch):
        monkeypatch.setattr(upstream_proxy.sys, "platform", "darwin")
        monkeypatch.setattr(upstream_proxy.platform, "machine", lambda: "arm64")
        assert upstream_proxy._platform_asset_name() == "upstream-proxy-darwin-arm64"

    def test_unknown_arch_returns_none(self, monkeypatch):
        monkeypatch.setattr(upstream_proxy.sys, "platform", "linux")
        monkeypatch.setattr(upstream_proxy.platform, "machine", lambda: "sparc")
        assert upstream_proxy._platform_asset_name() is None

    def test_find_binary_platform_suffix(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UPSTREAM_TLS_PROXY_BIN", str(tmp_path / "nope"))
        monkeypatch.setattr(upstream_proxy, "_BINARY_CANDIDATES", ())
        monkeypatch.setattr(upstream_proxy, "_BIN_DIR", tmp_path)
        monkeypatch.setattr(upstream_proxy.sys, "platform", "win32")
        monkeypatch.setattr(upstream_proxy.platform, "machine", lambda: "AMD64")
        binary = tmp_path / "upstream-proxy-windows-amd64.exe"
        binary.write_bytes(b"stub")
        assert upstream_proxy.find_binary() == binary


class TestHealthMonitor:
    async def test_status_shape(self):
        state = upstream_proxy.status()
        assert set(state) == {"listen", "binary", "managed_process", "pid", "monitor_running"}

    async def test_start_stop_monitor(self):
        upstream_proxy.start_monitor()
        task = upstream_proxy._monitor_task
        assert task is not None and not task.done()
        await upstream_proxy.stop_monitor()
        assert upstream_proxy._monitor_task is None or upstream_proxy._monitor_task.done()
