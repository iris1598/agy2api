"""Go 出站 TLS 转发器的生命周期管理。

真实 Antigravity language_server 是 Go 写的；用 Go 的 ``crypto/tls`` 出站时
ClientHello 与实抓逐段一致（JA3 md5 ``03117a8ed39ef02427ebbc39f121275c``，
含后量子曲线组 & 仅 http/1.1 ALPN）。本模块只负责找到并拉起本机二进制，
实际的 TLS / HTTP 转发在 ``tools/upstream_tls_proxy``。

二进制缺失、启动失败或端口不可用时返回 False。调用方在严格模式（默认）
下会直接抛 FidelityError / 拒绝启动，不做 httpx 回退（宁可报错，不静默降级）。

后台健康检查（``health_monitor``，web.py lifespan 启动）：本进程拉起的
转发器进程意外退出时自动重启，并可通过 ``status()`` 查看当前状态。
"""

import asyncio
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from log import log

_REPO_ROOT = Path(__file__).resolve().parent.parent

_BIN_DIR = _REPO_ROOT / "tools" / "upstream_tls_proxy" / "bin"

# 无后缀的常规产物名（build.ps1 / build.sh / CI 原生构建的输出）
_BINARY_CANDIDATES = (
    _BIN_DIR / "upstream-proxy.exe",
    _BIN_DIR / "upstream-proxy",
)

_START_TIMEOUT = 8.0
_MONITOR_INTERVAL = 15.0

_lock = threading.Lock()
_process: Optional[subprocess.Popen] = None
_disabled_reason: Optional[str] = None
_listen_url: Optional[str] = None
_binary_path: Optional[Path] = None
_monitor_task: Optional[asyncio.Task] = None


def _platform_asset_name() -> Optional[str]:
    """当前平台对应的交叉编译产物名（与 build-all.sh 命名一致）。

    release.yml / 安装脚本下载的就是这些带平台后缀的产物；
    直接保留原始文件名（不改名）也能被 find_binary 找到。
    """
    if sys.platform == "win32":
        goos, ext = "windows", ".exe"
    elif sys.platform == "darwin":
        goos, ext = "darwin", ""
    else:
        goos, ext = "linux", ""

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        goarch = "amd64"
    elif machine in ("aarch64", "arm64"):
        goarch = "arm64"
    elif machine.startswith("armv"):
        goarch = "arm"
    else:
        return None
    return f"upstream-proxy-{goos}-{goarch}{ext}"


def find_binary() -> Optional[Path]:
    """定位转发器二进制（可用 UPSTREAM_TLS_PROXY_BIN 覆盖）。

    查找顺序：环境变量覆盖 → 常规名（本机构建）→ 当前平台后缀名
    （release 产物未改名时）。
    """
    override = os.getenv("UPSTREAM_TLS_PROXY_BIN")
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
    for candidate in _BINARY_CANDIDATES:
        if candidate.is_file():
            return candidate
    asset = _platform_asset_name()
    if asset:
        candidate = _BIN_DIR / asset
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
    global _process, _disabled_reason, _listen_url, _binary_path

    _listen_url = listen_url
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
                _disabled_reason = "未找到 tools/upstream_tls_proxy/bin/upstream-proxy（先编译或安装）"
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

        _binary_path = binary

        if not _wait_port(host, port, _START_TIMEOUT):
            log.warning("[UPSTREAM TLS] 转发器启动后端口未就绪，严格模式下相关请求将失败")
            _terminate_locked()
            return False

        log.info(f"[UPSTREAM TLS] Go 转发器已就绪: http://{host}:{port}")
        return True


def status() -> Dict[str, Any]:
    """转发器当前状态（面板/健康端点展示用）。"""
    process = _process
    return {
        "listen": _listen_url,
        "binary": str(_binary_path) if _binary_path else None,
        "managed_process": process is not None and process.poll() is None,
        "pid": process.pid if process is not None else None,
        "monitor_running": _monitor_task is not None and not _monitor_task.done(),
    }


async def health_monitor(interval: float = _MONITOR_INTERVAL) -> None:
    """后台健康检查：本进程拉起的转发器进程退出后自动重启。

    只管理本进程拥有的进程句柄；端口被外部实例占用时无需干预。
    """
    global _process
    while True:
        await asyncio.sleep(interval)
        try:
            url = _listen_url
            if not url:
                continue
            with _lock:
                process = _process
                dead = process is not None and process.poll() is not None
                if dead:
                    # 清掉死进程句柄，让 ensure_running 重新拉起
                    _process = None
            if dead:
                log.warning("[UPSTREAM TLS] Go 转发器进程意外退出，自动重启...")
                ok = await asyncio.to_thread(ensure_running, url)
                if ok:
                    log.info("[UPSTREAM TLS] 转发器已自动重启")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 后台任务失败不影响请求
            log.debug(f"[UPSTREAM TLS] 健康检查异常: {e}")


def start_monitor() -> None:
    """启动后台健康检查（web.py lifespan 调用；重复调用幂等）。"""
    global _monitor_task
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(health_monitor())
        log.info("[UPSTREAM TLS] 后台健康检查已启动")


async def stop_monitor() -> None:
    """停止后台健康检查（应用关闭时调用）。"""
    global _monitor_task
    if _monitor_task is not None:
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
        _monitor_task = None


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
