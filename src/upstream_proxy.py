"""Go 出站 TLS 转发器的生命周期管理。

真实 Antigravity language_server 是 Go 写的；用 Go 的 ``crypto/tls`` 出站时
ClientHello 与实抓逐段一致（JA3 md5 ``03117a8ed39ef02427ebbc39f121275c``，
含后量子曲线组 & 仅 http/1.1 ALPN）。本模块只负责找到并拉起本机二进制，
实际的 TLS / HTTP 转发在 ``tools/upstream_tls_proxy``。

二进制缺失、启动失败或端口不可用时返回 False。调用方在严格模式（默认）
下会直接抛 FidelityError / 拒绝启动，不做 httpx 回退（宁可报错，不静默降级）。
"""

import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from log import log

_REPO_ROOT = Path(__file__).resolve().parent.parent

_BINARY_CANDIDATES = (
    _REPO_ROOT / "tools" / "upstream_tls_proxy" / "bin" / "upstream-proxy.exe",
    _REPO_ROOT / "tools" / "upstream_tls_proxy" / "bin" / "upstream-proxy",
)

_START_TIMEOUT = 8.0

_lock = threading.Lock()
_process: Optional[subprocess.Popen] = None
_disabled_reason: Optional[str] = None


def find_binary() -> Optional[Path]:
    """定位转发器二进制（可用 UPSTREAM_TLS_PROXY_BIN 覆盖）。"""
    override = os.getenv("UPSTREAM_TLS_PROXY_BIN")
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
    for candidate in _BINARY_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(host, port, timeout=0.2):
            return True
        time.sleep(0.05)
    return False


def ensure_running(listen_url: str) -> bool:
    """确保转发器在 ``listen_url`` 上可用；成功返回 True。

    已在该端口监听（例如用户手动启动）时直接复用，不重复拉起。
    """
    global _process, _disabled_reason

    parts = urlsplit(listen_url)
    host = parts.hostname or "127.0.0.1"
    try:
        port = parts.port or 17890
    except ValueError:
        log.warning(f"[UPSTREAM TLS] 无效的转发器地址: {listen_url}")
        return False

    if _port_open(host, port):
        return True

    with _lock:
        if _process is not None and _process.poll() is None and _port_open(host, port):
            return True

        binary = find_binary()
        if binary is None:
            if _disabled_reason is None:
                _disabled_reason = "未找到 tools/upstream_tls_proxy/bin/upstream-proxy（先编译）"
                log.warning(f"[UPSTREAM TLS] {_disabled_reason}，严格模式下相关请求将失败")
            return False

        try:
            _process = subprocess.Popen(
                [str(binary), "--listen", f"{host}:{port}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning(f"[UPSTREAM TLS] 启动转发器失败: {exc}")
            _process = None
            return False

        if not _wait_port(host, port, _START_TIMEOUT):
            log.warning("[UPSTREAM TLS] 转发器启动后端口未就绪，严格模式下相关请求将失败")
            _terminate_locked()
            return False

        log.info(f"[UPSTREAM TLS] Go 转发器已就绪: http://{host}:{port}")
        return True


def _terminate_locked() -> None:
    global _process
    if _process is None:
        return
    try:
        _process.terminate()
        _process.wait(timeout=5)
    except Exception as exc:  # noqa: BLE001 - 关闭失败不影响主流程
        log.debug(f"[UPSTREAM TLS] 关闭转发器失败: {exc}")
        try:
            _process.kill()
        except Exception:  # noqa: BLE001
            pass
    finally:
        _process = None


def stop() -> None:
    """停止本进程拉起的转发器（应用关闭时调用）。"""
    with _lock:
        _terminate_locked()
