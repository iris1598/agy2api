#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Antigravity 真实客户端被动抓包 + 指纹报告工具。

三个子命令
----------
passive   在 Clash TUN 虚拟网卡上被动抓包（dumpcap），采集真实客户端发出的
          TLS ClientHello（SNI / JA3 / JA4 / ALPN）与 DNS 域名清单。
          纯监听：不修改客户端、不安装 CA、不产生任何发往 Google 的新流量，
          Google 服务端对此完全无感知。
analyze   对抓包目录跑 tshark 后处理，产出 tls_report.md / ja3_summary.json /
          connections.csv / domains.txt。
report    合并被动会话（TLS 指纹）与 ``run_capture.ps1 -Offline`` 的明文抓包
          （header 顺序、UA、body envelope 键序），生成 fingerprint_report.md，
          并与 ``src/utils.py`` / ``src/api/antigravity.py`` 的当前代理指纹 diff。

安全边界
--------
- passive 只监听网卡，不注入、不重放、不修改客户端进程；
- 所有产物只写入本地 ``captures/`` 目录（已被 .gitignore 排除），不上传；
- report 不自动修改任何源码，只输出建议常量块供人工核对后粘贴。

前置条件
--------
- passive 需要 Wireshark（tshark/dumpcap）+ Npcap + 管理员权限；
  缺失时可经确认调用 winget 自动安装（``--no-install`` 跳过）；
- Clash TUN 需开启：抓 TUN 网卡才能看到客户端的原始 TLS ClientHello；
  抓物理网卡只能看到代理节点的加密隧道，无分析价值；
- 出口节点必须为非 CN，否则 Google 上游直接返回 403 LOCATION_NOT_ELIGIBLE。

注意：Go 编写的 language_server 忽略 SSLKEYLOGFILE，被动抓包拿不到它的
明文 header/body——那部分由 run_capture.ps1 的 CLOUD_CODE_URL 重定向负责。
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import gzip
import json
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tls_fingerprint import (
    BAD,
    MAX_SNIFF_BYTES,
    OK,
    REAL_ANTIGRAVITY_ALPN,
    REAL_ANTIGRAVITY_JA3_FULL,
    parse_client_hello,
    parse_server_hello,
)

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent.parent
DEFAULT_CAPTURE_ROOT = TOOL_DIR / "captures"

WIRESHARK_INSTALL_DIRS = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Wireshark",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Wireshark",
)

# 按 TUN 网卡名称/描述匹配的关键字（Clash Verge Rev 的 mihomo 内核默认设备名 Mihomo）
TUN_ADAPTER_HINTS = ("mihomo", "clash", "wintun", "tun")

# 同时覆盖 TCP/TLS、QUIC（Google 域名可能经 alt-svc 走 h3）与 DNS 查询
CAPTURE_FILTER = "tcp port 443 or udp port 443 or udp port 53"
RING_FILESIZE_KB = 51200  # 50MB
RING_FILE_COUNT = 64

CLASH_DIR_NAME = "io.github.clash-verge-rev.clash-verge-rev"

# 已知 Antigravity / Google 相关域名：精确匹配 + 后缀匹配
KNOWN_EXACT_DOMAINS = (
    "daily-cloudcode-pa.googleapis.com",
    "cloudcode-pa.googleapis.com",
    "preprod-daily-cloudcode-pa.sandbox.googleapis.com",
    "aicode.googleapis.com",
    "oauth2.googleapis.com",
    "www.googleapis.com",
    "accounts.google.com",
    "antigravity-unleash.goog",
    "api.anthropic.com",
    "firebaseinstallations.googleapis.com",
)
KNOWN_DOMAIN_SUFFIXES = (
    ".googleusercontent.com",
    ".googleapis.com",
    ".goog",
    ".anthropic.com",
)

# 真实 UA 的宽松解析（形状未实测，允许字段缺失）
_UA_VERSION_RE = re.compile(r"antigravity/(?:hub|cli)/([0-9][^\s(]*)")
_UA_CL_RE = re.compile(r"cl=(\d+)")
_UA_OS_RE = re.compile(r"os_type=([A-Za-z0-9_]+)")
_UA_ARCH_RE = re.compile(r"arch=([A-Za-z0-9_]+)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _run(
    cmd: Sequence[str],
    timeout: Optional[int] = None,
    quiet: bool = False,
) -> Tuple[int, str, str]:
    """执行外部命令，返回 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "", f"executable not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(cmd)}"
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    if not quiet and proc.returncode != 0 and err.strip():
        print(f"[sniff] 命令失败 (exit {proc.returncode}): {' '.join(cmd)}")
        for line in err.strip().splitlines()[:20]:
            print(f"        {line}")
    return proc.returncode, out, err


def is_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 探测失败按未提权处理
        return False


def find_wireshark_tool(name: str) -> Optional[Path]:
    """在 PATH 和常见安装目录里找 tshark/dumpcap。"""
    from shutil import which

    hit = which(name)
    if hit:
        return Path(hit)
    for base in WIRESHARK_INSTALL_DIRS:
        candidate = base / f"{name}.exe"
        if candidate.is_file():
            return candidate
    return None


def print_manual_install_guide() -> None:
    print(
        "请手动安装（装完重新运行本命令）：\n"
        "  1. Wireshark: https://www.wireshark.org/download.html\n"
        "     或 winget install --id Wireshark.Wireshark -e\n"
        "  2. Npcap:     https://npcap.com/#download\n"
        "     （安装时保持默认的「Restrict Npcap driver's access to "
        "Administrators only」即可，抓包需管理员运行）"
    )


def try_install_wireshark(auto_yes: bool) -> bool:
    """用 winget 静默安装 Wireshark（内置 Npcap）。"""
    if auto_yes or input("是否现在用 winget 安装 Wireshark（含 Npcap）? [y/N] ").strip().lower() in ("y", "yes"):
        winget = find_wireshark_tool("winget") or Path("winget")
        cmd = [
            str(winget), "install", "--id", "WiresharkFoundation.Wireshark", "--exact",
            "--silent", "--accept-package-agreements", "--accept-source-agreements",
        ]
        print("[sniff] 执行:", " ".join(cmd), "（可能需要几分钟，请留意 UAC 弹窗）")
        proc = subprocess.run(cmd)
        if proc.returncode == 0:
            print("[sniff] winget 安装完成。")
            return True
        print(f"[sniff] winget 安装失败 (exit {proc.returncode})。")
        print_manual_install_guide()
        return False
    return False


def probe_tls_fields(tshark: Path) -> Dict[str, bool]:
    """探测当前 tshark 版本支持的 TLS 指纹字段（JA4 等随版本变化）。"""
    code, out, _ = _run([str(tshark), "-G", "fields"], timeout=120, quiet=True)
    names = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == "F":
            names.add(parts[1])
    return {
        "ja3_full": "tls.handshake.ja3_full" in names,
        "ja3_md5": "tls.handshake.ja3_md5_hash" in names,
        "alpn": "tls.handshake.extensions_alpn_str" in names,
        "supported_version": "tls.handshake.extensions_supported_version" in names,
        "ja4": sorted(
            n for n in names
            if n.startswith("tls.handshake.ja4") or n.startswith("tls.handshake.ja4s")
        ),
    }


# ---------------------------------------------------------------------------
# Clash TUN / 网卡探测
# ---------------------------------------------------------------------------

def clash_tun_state() -> Dict[str, Optional[bool]]:
    """读取 Clash Verge Rev 配置中的 TUN 开关（用户配置 + 运行配置）。"""
    appdata = Path(os.environ.get("APPDATA", ""))
    clash_dir = appdata / CLASH_DIR_NAME

    def _read_verge(path: Path) -> Optional[bool]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        m = re.search(r"^\s*enable_tun_mode:\s*(true|false)\s*$", text, re.M)
        return m.group(1) == "true" if m else None

    def _read_runtime(path: Path) -> Optional[bool]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        in_tun = False
        for line in lines:
            if re.match(r"^tun:\s*$", line):
                in_tun = True
                continue
            if in_tun and re.match(r"^\S", line):
                in_tun = False
            if in_tun and re.match(r"^\s+enable:\s*true\s*$", line):
                return True
        return False

    return {
        "verge": _read_verge(clash_dir / "verge.yaml"),
        "runtime": _read_runtime(clash_dir / "clash-verge.yaml"),
    }


def list_adapters() -> List[Dict[str, str]]:
    """通过 PowerShell 枚举网卡（Name/InterfaceDescription/Status/ifIndex）。"""
    code, out, _ = _run(
        [
            "powershell", "-NoProfile", "-Command",
            "ConvertTo-Json -InputObject "
            "@(Get-NetAdapter | Select-Object Name, InterfaceDescription, Status, ifIndex)",
        ],
        timeout=60,
        quiet=True,
    )
    if code != 0:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):  # 单张网卡时 ConvertTo-Json 输出对象
        data = [data]
    result = []
    for item in data:
        lower = {k.lower(): v for k, v in item.items() if isinstance(v, str) or isinstance(v, int)}
        result.append(
            {
                "name": str(lower.get("name", "")),
                "description": str(lower.get("interfacedescription", "")),
                "status": str(lower.get("status", "")),
                "ifindex": str(lower.get("ifindex", "")),
            }
        )
    return result


def dumpcap_interfaces(dumpcap: Path) -> List[Tuple[str, str, str]]:
    """解析 `dumpcap -D`：返回 [(编号, 设备路径, 友好名)]。"""
    code, out, _ = _run([str(dumpcap), "-D"], timeout=60, quiet=True)
    if code != 0:
        return []
    result = []
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+)\.\s+(\S+)\s+\((.*)\)\s*$", line)
        if m:
            result.append((m.group(1), m.group(2), m.group(3)))
            continue
        m = re.match(r"^\s*(\d+)\.\s+(.+)$", line)
        if m:
            result.append((m.group(1), m.group(2), m.group(2)))
    return result


def _tun_score(text: str) -> int:
    lowered = text.lower()
    score = 0
    if "mihomo" in lowered:
        score += 4
    if "clash" in lowered:
        score += 3
    if "wintun" in lowered:
        score += 2
    if "tun" in lowered:
        score += 1
    return score


def pick_tun_interface(
    dumpcap: Path,
    manual_index: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """选择要抓的 TUN 网卡，返回 (dumpcap 接口编号, 展示名)。"""
    ifaces = dumpcap_interfaces(dumpcap)
    if manual_index is not None:
        for idx, _device, friendly in ifaces:
            if idx == manual_index:
                return idx, friendly
        print(f"[sniff] 指定的接口编号 {manual_index} 不在 dumpcap -D 列表中。")
        return None

    adapters = list_adapters()
    scored: List[Tuple[int, str, str]] = []
    for adapter in adapters:
        score = max(
            _tun_score(adapter["name"]),
            _tun_score(adapter["description"]),
        )
        if score <= 0:
            continue
        if adapter["status"].lower() not in ("up", ""):
            continue  # TUN 未激活的适配器抓不到包
        scored.append((score, adapter["name"], adapter["status"]))

    # 优先用网卡名映射到 dumpcap 条目
    for score, name, _status in sorted(scored, reverse=True):
        for idx, _device, friendly in ifaces:
            if name.lower() in friendly.lower() or friendly.lower() in name.lower():
                return idx, friendly

    # 回退：直接在 dumpcap 列表里按关键字匹配
    fallback = [
        (max(_tun_score(friendly), _tun_score(device)), idx, friendly)
        for idx, device, friendly in ifaces
        if _tun_score(friendly) > 0 or _tun_score(device) > 0
    ]
    if fallback:
        fallback.sort(reverse=True)
        _, idx, friendly = fallback[0]
        return idx, friendly
    return None


# ---------------------------------------------------------------------------
# passive：被动抓包编排
# ---------------------------------------------------------------------------

def resolve_antigravity_exe() -> Optional[Path]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for name in ("Antigravity", "antigravity"):
        candidate = local / "Programs" / name / "Antigravity.exe"
        if candidate.is_file():
            return candidate
    return None


def relaunch_with_keylog(session_dir: Path, auto_yes: bool) -> Optional[Path]:
    """重启 Antigravity 并注入 SSLKEYLOGFILE（仅影响本次启动的进程，不改客户端）。

    只对 Chromium/Node 侧流量有效；Go language_server 忽略该变量。
    """
    exe = resolve_antigravity_exe()
    if exe is None:
        print("[sniff] 未找到 Antigravity.exe，跳过 --launch。")
        return None
    if not auto_yes:
        answer = input("需要关闭并重启 Antigravity（注入 SSLKEYLOGFILE），继续? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            return None
    subprocess.run(["taskkill", "/IM", "Antigravity.exe", "/F", "/T"], capture_output=True)
    subprocess.run(["taskkill", "/IM", "language_server.exe", "/F", "/T"], capture_output=True)
    time.sleep(2)

    keylog = session_dir / "keylog.txt"
    env = os.environ.copy()
    env["SSLKEYLOGFILE"] = str(keylog)
    env["NODE_OPTIONS"] = f"--tls-keylog={keylog}"
    try:
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env)
    except OSError as exc:
        print(f"[sniff] 启动 Antigravity 失败: {exc}")
        return None
    print(f"[sniff] Antigravity 已重启 (pid {proc.pid})，keylog: {keylog}")
    return keylog


def stop_dumpcap(proc: subprocess.Popen) -> None:
    """优雅停止 dumpcap：先 CTRL_BREAK，超时再强杀。"""
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        proc.wait(timeout=6)
        return
    except (ValueError, OSError, subprocess.TimeoutExpired):
        pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def cmd_passive(args: argparse.Namespace) -> int:
    tshark = find_wireshark_tool("tshark")
    dumpcap = find_wireshark_tool("dumpcap")
    if tshark is None or dumpcap is None:
        print("[sniff] 未找到 tshark/dumpcap（Wireshark 未安装）。")
        if args.no_install:
            print_manual_install_guide()
            return 2
        if not try_install_wireshark(auto_yes=args.yes):
            return 2
        tshark = find_wireshark_tool("tshark")
        dumpcap = find_wireshark_tool("dumpcap")
        if tshark is None or dumpcap is None:
            print_manual_install_guide()
            return 2

    if not is_admin():
        print("[sniff] 当前不是管理员权限，Npcap 会拒绝抓包。")
        print("       请改用 Start-Sniff.ps1 运行（会自动弹出 UAC 提权），")
        print("       例如: powershell -ExecutionPolicy Bypass -File "
              "tools\\antigravity_capture\\Start-Sniff.ps1 passive")
        return 3

    tun_state = clash_tun_state()
    if tun_state["verge"] is False or tun_state["runtime"] is False:
        print("[sniff] 警告: Clash TUN 未开启（verge.yaml=%s, 运行配置=%s）。" % (
            tun_state["verge"], tun_state["runtime"]))
        print("       TUN 关闭时抓不到客户端的原始 TLS ClientHello，请先在 Clash Verge 打开 TUN 模式。")
        if not args.allow_no_tun:
            return 4
    elif tun_state["verge"] is None and tun_state["runtime"] is None:
        print("[sniff] 提示: 未找到 Clash Verge 配置，无法确认 TUN 状态。")

    picked = pick_tun_interface(dumpcap, manual_index=args.interface)
    if picked is None:
        print("[sniff] 未找到 TUN 虚拟网卡。当前 dumpcap -D 列表:")
        for idx, _device, friendly in dumpcap_interfaces(dumpcap):
            print(f"        {idx}. {friendly}")
        print("       请确认 Clash TUN 已开启，或用 --interface <编号> 手动指定。")
        return 4
    iface_idx, iface_name = picked
    print(f"[sniff] 抓包网卡: {iface_name} (dumpcap 接口 {iface_idx})")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    session_dir = DEFAULT_CAPTURE_ROOT / f"passive-{stamp}"
    session_dir.mkdir(parents=True, exist_ok=True)
    pcap_path = session_dir / "capture.pcapng"

    keylog: Optional[Path] = None
    if args.launch:
        keylog = relaunch_with_keylog(session_dir, auto_yes=args.yes)

    cmd = [
        str(dumpcap), "-i", iface_idx, "-f", CAPTURE_FILTER,
        "-n", "-Q", "-w", str(pcap_path),
    ]
    if args.rotate:
        cmd += ["-b", f"filesize:{RING_FILESIZE_KB}", "-b", f"files:{RING_FILE_COUNT}"]
    print(f"[sniff] dumpcap 启动: {' '.join(cmd)}")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(cmd, creationflags=creationflags)
    except OSError as exc:
        print(f"[sniff] dumpcap 启动失败: {exc}")
        print("       常见原因：Npcap 未安装 / 未以管理员运行。")
        return 5
    time.sleep(1.5)
    if proc.poll() is not None:
        print(f"[sniff] dumpcap 提前退出 (exit {proc.returncode})，可能无抓包权限或网卡无效。")
        return 5

    print()
    print("=" * 64)
    print("被动抓包进行中（纯监听，Google 无感知）。")
    print("请在 Antigravity 里正常操作：发一条消息、切换模型等。")
    if args.duration:
        print(f"--duration {args.duration} 秒后自动停止。")
    else:
        print("完成后回到本窗口按回车停止。")
    print("=" * 64)
    print()

    if args.duration:
        try:
            proc.wait(timeout=args.duration)
        except subprocess.TimeoutExpired:
            stop_dumpcap(proc)
    else:
        try:
            input()
        except KeyboardInterrupt:
            print("\n[sniff] 收到 Ctrl+C，正在停止...")
        stop_dumpcap(proc)

    pcap_exists = pcap_path.is_file()
    if not pcap_exists and not args.rotate:
        print(f"[sniff] 未生成 pcap 文件: {pcap_path}")
        return 6

    print()
    return cmd_analyze_impl(
        session_dir=session_dir,
        tshark=tshark,
        auto_keylog=keylog,
    )


# ---------------------------------------------------------------------------
# analyze：tshark 后处理
# ---------------------------------------------------------------------------

def _tshark_fields_pass(
    tshark: Path,
    pcap: Path,
    display_filter: str,
    fields: Sequence[str],
    keylog: Optional[Path] = None,
) -> List[List[str]]:
    cmd = [str(tshark), "-r", str(pcap), "-n", "-Q", "-Y", display_filter, "-T", "fields",
           "-E", "separator=/t", "-E", "quote=d", "-E", "occurrence=a"]
    for field in fields:
        cmd += ["-e", field]
    if keylog:
        cmd += ["-o", f"tls.keylog_file:{keylog}"]
    code, out, _ = _run(cmd, timeout=600, quiet=True)
    if code != 0:
        return []
    rows = []
    for row in csv.reader(out.splitlines(), delimiter="\t", quotechar='"'):
        if row:
            rows.append(row)
    return rows


def _cell(row: Sequence[str], idx: int) -> str:
    return row[idx] if idx < len(row) else ""


def _first(value: str, sep: str = ",") -> str:
    return value.split(sep, 1)[0] if value else ""


def analyze_pcapng(
    tshark: Path,
    pcap: Path,
    keylog: Optional[Path],
    fields_avail: Dict[str, Any],
) -> Dict[str, Any]:
    """对单个 pcapng 提取 ClientHello / ServerHello / DNS / （可选）HTTP2 明文。"""
    ch_fields = [
        "frame.number", "frame.time_epoch", "ip.src", "tcp.srcport",
        "ip.dst", "tcp.dstport", "udp.dstport",
        "tls.handshake.extensions_server_name",
    ]
    if fields_avail["ja3_full"]:
        ch_fields.append("tls.handshake.ja3_full")
    if fields_avail["ja3_md5"]:
        ch_fields.append("tls.handshake.ja3_md5_hash")
    if fields_avail["alpn"]:
        ch_fields.append("tls.handshake.extensions_alpn_str")
    if fields_avail["supported_version"]:
        ch_fields.append("tls.handshake.extensions_supported_version")
    if fields_avail["ja4"]:
        ch_fields.append(fields_avail["ja4"][0])
    ch_fields.append("tls.handshake.ciphersuite")

    hellos: List[Dict[str, Any]] = []
    # 字段按启用情况动态编号，避免可选字段缺失时索引漂移
    field_index = {name: idx for idx, name in enumerate(ch_fields)}
    for row in _tshark_fields_pass(tshark, pcap, "tls.handshake.type == 1", ch_fields, keylog):
        def col(name: str) -> str:
            pos = field_index.get(name)
            return _cell(row, pos) if pos is not None else ""

        hellos.append(
            {
                "frame": col("frame.number"),
                "time": col("frame.time_epoch"),
                "src": col("ip.src"),
                "sport": col("tcp.srcport"),
                "dst": col("ip.dst"),
                "tcp_dport": col("tcp.dstport"),
                "udp_dport": col("udp.dstport"),
                "sni": _first(col("tls.handshake.extensions_server_name")),
                "ja3_full": col("tls.handshake.ja3_full"),
                "ja3_md5": col("tls.handshake.ja3_md5_hash"),
                "alpn": col("tls.handshake.extensions_alpn_str"),
                "tls_versions": col("tls.handshake.extensions_supported_version"),
                "ja4": col(fields_avail["ja4"][0]) if fields_avail["ja4"] else "",
                "ciphersuites": col("tls.handshake.ciphersuite"),
                "transport": "udp" if col("udp.dstport") else "tcp",
                "dstport": _first(col("udp.dstport")) or _first(col("tcp.dstport")),
            }
        )

    server_alpn: Dict[str, set] = {}
    sh_fields = ["ip.src", "tls.handshake.extensions_alpn_str", "tls.handshake.version"]
    for row in _tshark_fields_pass(tshark, pcap, "tls.handshake.type == 2", sh_fields, keylog):
        server = _cell(row, 0)
        alpn = _cell(row, 1)
        if server:
            server_alpn.setdefault(server, set()).update(a for a in alpn.split(",") if a)

    dns_names: Dict[str, int] = OrderedDict()
    for row in _tshark_fields_pass(tshark, pcap, "dns.flags.response == 0", ["dns.qry.name"]):
        name = _cell(row, 0)
        if name:
            dns_names[name] = dns_names.get(name, 0) + 1

    http2_rows: List[Dict[str, str]] = []
    if keylog:
        h2_fields = [
            "tcp.stream", "http2.headers.method", "http2.headers.path",
            "http2.headers.authority", "http2.headers.status",
        ]
        for row in _tshark_fields_pass(tshark, pcap, "http2.headers.method", h2_fields, keylog):
            http2_rows.append(
                {
                    "stream": _cell(row, 0), "method": _cell(row, 1),
                    "path": _cell(row, 2), "authority": _cell(row, 3),
                    "status": _cell(row, 4),
                }
            )
    return {
        "pcap": str(pcap),
        "client_hellos": hellos,
        "server_alpn": {k: sorted(v) for k, v in server_alpn.items()},
        "dns_names": dns_names,
        "http2_rows": http2_rows,
    }


def classify_domain(domain: str) -> str:
    lowered = domain.lower().rstrip(".")
    if lowered in KNOWN_EXACT_DOMAINS:
        return "antigravity"
    for suffix in KNOWN_DOMAIN_SUFFIXES:
        if lowered.endswith(suffix):
            return "related"
    return "other"


def write_analyze_outputs(session_dir: Path, results: List[Dict[str, Any]]) -> Path:
    """把 analyze 结果写盘，返回 tls_report.md 路径。"""
    hellos: List[Dict[str, Any]] = []
    server_alpn: Dict[str, set] = {}
    dns_names: Dict[str, int] = OrderedDict()
    http2_rows: List[Dict[str, str]] = []
    for result in results:
        hellos.extend(result["client_hellos"])
        for server, alpns in result["server_alpn"].items():
            server_alpn.setdefault(server, set()).update(alpns)
        for name, count in result["dns_names"].items():
            dns_names[name] = dns_names.get(name, 0) + count
        http2_rows.extend(result["http2_rows"])

    # 按 (sni, ja3_md5, ja4) 聚合
    aggregated: "OrderedDict[Tuple[str, str, str], Dict[str, Any]]" = OrderedDict()
    for hello in hellos:
        key = (hello["sni"] or "<no-sni>", hello["ja3_md5"], hello["ja4"])
        entry = aggregated.setdefault(
            key,
            {"count": 0, "transports": set(), "dsts": set(), "alpns": set(),
             "versions": set(), "ja3_full": hello["ja3_full"], "first": hello["time"]},
        )
        entry["count"] += 1
        entry["transports"].add(hello["transport"])
        if hello["dst"]:
            entry["dsts"].add(f"{hello['dst']}:{hello['dstport']}")
        entry["alpns"].update(a for a in hello["alpn"].split(",") if a)
        entry["versions"].update(v for v in hello["tls_versions"].split(",") if v)

    summary = {
        "generated_at": utc_now(),
        "pcaps": [r["pcap"] for r in results],
        "client_hello_count": len(hellos),
        "entries": [
            {
                "sni": k[0], "ja3_md5": k[1], "ja4": k[2],
                "ja3_full": v["ja3_full"], "count": v["count"],
                "transports": sorted(v["transports"]), "dsts": sorted(v["dsts"]),
                "alpn": sorted(v["alpns"]), "tls_versions": sorted(v["versions"]),
                "category": classify_domain(k[0]),
            }
            for k, v in aggregated.items()
        ],
    }
    (session_dir / "ja3_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with (session_dir / "connections.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["frame", "time", "transport", "src:sport", "dst:dport", "sni",
                         "ja3_md5", "ja4", "alpn", "tls_versions"])
        for hello in hellos:
            writer.writerow([
                hello["frame"], hello["time"], hello["transport"],
                f"{hello['src']}:{hello['sport']}",
                f"{hello['dst']}:{hello['dstport']}",
                hello["sni"], hello["ja3_md5"], hello["ja4"], hello["alpn"],
                hello["tls_versions"],
            ])

    all_domains = sorted(set(list(dns_names) + [h["sni"] for h in hellos if h["sni"]]))
    (session_dir / "domains.txt").write_text("\n".join(all_domains) + "\n", encoding="utf-8")

    if http2_rows:
        with (session_dir / "http2_headers.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["tcp.stream", "method", "authority", "path", "status"])
            for row in http2_rows:
                writer.writerow([row["stream"], row["method"], row["authority"],
                                 row["path"], row["status"]])

    md = build_tls_report_markdown(summary, server_alpn, dns_names, http2_rows)
    report_path = session_dir / "tls_report.md"
    report_path.write_text(md, encoding="utf-8")
    return report_path


def build_tls_report_markdown(
    summary: Dict[str, Any],
    server_alpn: Dict[str, set],
    dns_names: Dict[str, int],
    http2_rows: List[Dict[str, str]],
) -> str:
    lines = [
        "# Antigravity 被动抓包 TLS 报告",
        "",
        f"- 生成时间: {summary['generated_at']}",
        f"- pcap 文件: {', '.join(Path(p).name for p in summary['pcaps'])}",
        f"- ClientHello 总数: {summary['client_hello_count']}"
        f"（唯一指纹组合 {len(summary['entries'])} 种）",
        "",
        "## Antigravity 相关域名（模型流量看这里）",
        "",
    ]
    antigravity = [e for e in summary["entries"] if e["category"] == "antigravity"]
    related = [e for e in summary["entries"] if e["category"] == "related"]
    other = sorted(
        (e for e in summary["entries"] if e["category"] == "other"),
        key=lambda e: -e["count"],
    )
    if antigravity:
        lines += [
            "| 域名 | 传输 | 次数 | JA3 md5 | JA4 | ALPN | TLS 版本 | 目标 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for e in antigravity:
            lines.append(
                f"| {e['sni']} | {'/'.join(e['transports'])} | {e['count']} "
                f"| {e['ja3_md5'] or '-'} | {e['ja4'] or '-'} "
                f"| {','.join(e['alpn']) or '-'} | {','.join(e['tls_versions']) or '-'} "
                f"| {', '.join(e['dsts'][:3])} |"
            )
        lines.append("")
        for e in antigravity:
            if e["ja3_full"]:
                lines.append(f"<details><summary>JA3 完整串（{e['sni']}）</summary>")
                lines.append("")
                lines.append(f"`{e['ja3_full']}`")
                lines.append("")
                lines.append("</details>")
                lines.append("")
    else:
        lines += [
            "> 没有抓到 Antigravity 相关域名的 ClientHello。排查：",
            "> 1. Clash TUN 是否开启、Antigravity 是否在抓包期间发过请求；",
            "> 2. 抓的是否为 TUN 网卡（物理网卡只能看到代理节点的加密隧道）；",
            "> 3. `dumpcap -D` 是否能看到网卡（Npcap 是否装好）；",
            "> 4. TUN 网卡未激活时 Get-NetAdapter 可能查不到，确认 TUN 打开后再跑。",
            "",
        ]

    if related:
        lines += ["## 相关联域名（Google/Anthropic 家族）", ""]
        for e in related:
            lines.append(
                f"- {e['sni']} ×{e['count']}（{','.join(e['alpn']) or '-'}）"
            )
        lines.append("")

    if other:
        lines += ["## 其它域名（Top 20）", ""]
        for e in other[:20]:
            lines.append(f"- {e['sni']} ×{e['count']}")
        lines.append("")

    if server_alpn:
        alpn_count: Dict[str, int] = OrderedDict()
        for alpns in server_alpn.values():
            for alpn in alpns:
                alpn_count[alpn] = alpn_count.get(alpn, 0) + 1
        lines += [
            "## ServerHello 协商结果",
            "",
            ", ".join(f"{alpn}×{n}" for alpn, n in alpn_count.items()) or "-",
            "",
        ]

    if dns_names:
        top_dns = sorted(dns_names.items(), key=lambda kv: -kv[1])[:30]
        lines += ["## DNS 查询（Top 30）", ""]
        lines += [f"- {name} ×{count}" for name, count in top_dns]
        lines.append("")

    if http2_rows:
        lines += [
            "## HTTP/2 明文（keylog 解密，Chromium/Node 侧流量）",
            "",
            "| stream | method | authority | path | status |",
            "|---|---|---|---|---|",
        ]
        for row in http2_rows[:50]:
            lines.append(
                f"| {row['stream']} | {row['method']} | {row['authority']} "
                f"| {row['path']} | {row['status']} |"
            )
        lines.append("")
        lines.append("> 注意：Go language_server 的 TLS 不读 SSLKEYLOGFILE，这里看不到它的明文；")
        lines.append("> 模型请求的 header/body 请用 run_capture.ps1 的 CLOUD_CODE_URL 重定向抓取。")
        lines.append("")

    return "\n".join(lines) + "\n"


def cmd_analyze(args: argparse.Namespace) -> int:
    tshark = find_wireshark_tool("tshark")
    if tshark is None:
        print("[sniff] 未找到 tshark，请先安装 Wireshark。")
        print_manual_install_guide()
        return 2
    session_dir = Path(args.session_dir)
    if not session_dir.is_dir():
        print(f"[sniff] 会话目录不存在: {session_dir}")
        return 2
    return cmd_analyze_impl(session_dir=session_dir, tshark=tshark, auto_keylog=None)


def cmd_analyze_impl(
    session_dir: Path,
    tshark: Path,
    auto_keylog: Optional[Path],
) -> int:
    pcaps = sorted(
        p for p in session_dir.glob("*.pcapng") if p.is_file()
    ) or sorted(p for p in session_dir.glob("*.pcap") if p.is_file())
    if not pcaps:
        print(f"[sniff] {session_dir} 下没有 pcapng/pcap 文件。")
        return 6
    keylog = auto_keylog if (auto_keylog and auto_keylog.is_file()) else None
    if keylog is None:
        candidate = session_dir / "keylog.txt"
        if candidate.is_file() and candidate.stat().st_size > 0:
            keylog = candidate

    fields_avail = probe_tls_fields(tshark)
    print(f"[sniff] tshark 字段支持: ja3={fields_avail['ja3_full']}, "
          f"ja4={bool(fields_avail['ja4'])}, "
          f"keylog={'有' if keylog else '无'}")

    results = []
    for pcap in pcaps:
        print(f"[sniff] 解析 {pcap.name} ...")
        results.append(analyze_pcapng(tshark, pcap, keylog, fields_avail))

    report_path = write_analyze_outputs(session_dir, results)
    total = sum(len(r["client_hellos"]) for r in results)
    print(f"[sniff] ClientHello 共 {total} 条。")
    print(f"[sniff] TLS 报告: {report_path}")
    print(f"[sniff] 指纹汇总: {session_dir / 'ja3_summary.json'}")
    if not keylog and session_dir.name.startswith("passive-"):
        print("[sniff] 提示: 本次没有 keylog，Chromium 侧流量未解密（Go LS 本来也解不了）。")
    print("[sniff] 下一步: 跑 run_capture.ps1 -Offline -CaptureAll 后执行 report 生成指纹报告。")
    return 0


# ---------------------------------------------------------------------------
# tlsproxy：无管理员的被动 TLS 指纹抓取（本地 CONNECT 中继）
# ---------------------------------------------------------------------------

TLS_PROXY_DEFAULT_PORT = 7898


def detect_clash_mixed_port() -> str:
    """从 clash-verge.yaml 读 mixed-port，读不到就退回 7897。"""
    path = Path(os.environ.get("APPDATA", "")) / CLASH_DIR_NAME / "clash-verge.yaml"
    try:
        m = re.search(r"^mixed-port:\s*(\d+)\s*$", path.read_text(encoding="utf-8", errors="replace"), re.M)
    except OSError:
        m = None
    return m.group(1) if m else "7897"


class _HelloSniffer:
    """在转发字节流里累积并解析 TLS 握手（ClientHello/ServerHello）。"""

    def __init__(self, kind: str, on_result) -> None:
        self.kind = kind
        self.on_result = on_result
        self.buf = bytearray()
        self.done = False

    def feed(self, data: bytes) -> None:
        if self.done:
            return
        self.buf.extend(data)
        parser = parse_client_hello if self.kind == "client" else parse_server_hello
        try:
            status, info = parser(bytes(self.buf))
        except Exception:  # noqa: BLE001 - 解析异常按放弃处理
            status, info = BAD, None
        if status == OK:
            self.done = True
            self.on_result(info)
        elif status != "need_more" or len(self.buf) > MAX_SNIFF_BYTES:
            self.done = True


def _connect_via_upstream(target_host: str, target_port: int, upstream: str) -> socket.socket:
    """向上游代理（Clash）发起 CONNECT，返回建好的隧道 socket。"""
    upstream_host, _, upstream_port = upstream.rpartition(":")
    sock = socket.create_connection((upstream_host, int(upstream_port)), timeout=20)
    req = f"CONNECT {target_host}:{target_port} HTTP/1.1\r\nHost: {target_host}:{target_port}\r\n\r\n"
    sock.sendall(req.encode("latin-1"))
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise OSError("upstream closed during CONNECT")
        resp += chunk
    status_line = resp.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if " 200" not in status_line:
        sock.close()
        raise OSError(f"upstream CONNECT failed: {status_line}")
    return sock


def _pump(src: socket.socket, dst: socket.socket, sniffer: Optional[_HelloSniffer]) -> None:
    """单向字节转发；边转边喂给嗅探器。"""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            if sniffer is not None:
                sniffer.feed(data)
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class TlsProxyServer(socketserver.ThreadingTCPServer):
    """本地 CONNECT 中继：嗅探 TLS 握手后原样转发到上游代理。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, upstream: str, store: Dict[str, Any]) -> None:
        self.upstream = upstream
        self.store = store
        super().__init__(address, TlsProxyHandler)


def _append_jsonl(store: Dict[str, Any], name: str, record: Dict[str, Any]) -> None:
    with store["lock"]:
        with (store["session_dir"] / name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


class TlsProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: TlsProxyServer = self.server  # type: ignore[assignment]
        sock = self.request
        sock.settimeout(30)
        head = b""
        try:
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = sock.recv(8192)
                if not chunk:
                    return
                head += chunk
        except OSError:
            return

        first_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = first_line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            _append_jsonl(server.store, "connects.jsonl",
                          {"time": utc_now(), "first_line": first_line, "relayed": False})
            return
        target = parts[1]
        host, _, port_s = target.rpartition(":")
        port = int(port_s) if port_s.isdigit() else 443

        try:
            sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            upstream = _connect_via_upstream(host, port, server.upstream)
        except OSError as exc:
            _append_jsonl(server.store, "connects.jsonl",
                          {"time": utc_now(), "target": target, "relayed": False, "error": str(exc)})
            return

        client_sniffer: Optional[_HelloSniffer] = None
        server_sniffer: Optional[_HelloSniffer] = None
        if port == 443:
            record: Dict[str, Any] = {"time": utc_now(), "target": target}

            def _on_client(info: Dict[str, Any]) -> None:
                record.update(
                    {
                        "sni": info["sni"],
                        "ja3_md5": info["ja3_md5"],
                        "ja3_full": info["ja3_full"],
                        "alpn": info["alpn"],
                        "tls_versions": info["tls_versions"],
                        "ciphers_count": len(info["ciphers"]),
                        # 扩展 41 = pre_shared_key，35 = session_ticket：
                        # 恢复握手的 ClientHello 与首次握手不同，必须能区分。
                        "has_psk": 41 in info["extensions"],
                        "has_session_ticket": 35 in info["extensions"],
                    }
                )
                with server.store["lock"]:
                    server.store["records"].append(record)
                    (server.store["session_dir"] / "client_hellos.jsonl").write_text(
                        "".join(
                            json.dumps(r, ensure_ascii=False) + "\n"
                            for r in server.store["records"]
                        ),
                        encoding="utf-8",
                    )
                alpn_text = ",".join(info["alpn"]) or "-"
                print(
                    f"[tlsproxy] {target}  sni={info['sni'] or '-'}  "
                    f"ja3_md5={info['ja3_md5']}  alpn={alpn_text}",
                    flush=True,
                )

            def _on_server(info: Dict[str, Any]) -> None:
                record["server_alpn"] = info["alpn"]
                record["server_version"] = info["version"]
                with server.store["lock"]:
                    (server.store["session_dir"] / "client_hellos.jsonl").write_text(
                        "".join(
                            json.dumps(r, ensure_ascii=False) + "\n"
                            for r in server.store["records"]
                        ),
                        encoding="utf-8",
                    )

            client_sniffer = _HelloSniffer("client", _on_client)
            server_sniffer = _HelloSniffer("server", _on_server)

        _append_jsonl(server.store, "connects.jsonl",
                      {"time": utc_now(), "target": target, "relayed": True, "port": port})
        try:
            up = threading.Thread(
                target=_pump, args=(sock, upstream, client_sniffer), daemon=True
            )
            down = threading.Thread(
                target=_pump, args=(upstream, sock, server_sniffer), daemon=True
            )
            up.start()
            down.start()
            up.join()
            down.join()
        finally:
            for s in (sock, upstream):
                try:
                    s.close()
                except OSError:
                    pass


def aggregate_tlsproxy_summary(session_dir: Path) -> Optional[Dict[str, Any]]:
    """把 client_hellos.jsonl 聚合成 report 兼容的 ja3_summary.json。"""
    path = session_dir / "client_hellos.jsonl"
    if not path.is_file():
        return None
    hellos: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            hellos.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not hellos:
        return None

    aggregated: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
    for hello in hellos:
        key = (hello.get("sni") or "<no-sni>", hello.get("ja3_md5", ""))
        entry = aggregated.setdefault(
            key,
            {"count": 0, "transports": {"tcp"}, "dsts": set(), "alpns": set(),
             "versions": set(), "ja3_full": hello.get("ja3_full", ""),
             "server_alpn": set(), "first": hello.get("time", ""),
             "resumed": 0, "full": 0, "session_ticket": 0},
        )
        entry["count"] += 1
        entry["dsts"].add(hello.get("target", ""))
        entry["alpns"].update(a for a in hello.get("alpn", []) if a)
        entry["versions"].update(hello.get("tls_versions", []))
        entry["server_alpn"].update(a for a in hello.get("server_alpn", []) if a)
        if hello.get("has_psk"):
            entry["resumed"] += 1
        else:
            entry["full"] += 1
        if hello.get("has_session_ticket"):
            entry["session_ticket"] += 1

    summary = {
        "generated_at": utc_now(),
        "pcaps": [str(session_dir / "client_hellos.jsonl")],
        "capture_mode": "tlsproxy",
        "client_hello_count": len(hellos),
        "entries": [
            {
                "sni": k[0], "ja3_md5": k[1], "ja4": "",
                "ja3_full": v["ja3_full"], "count": v["count"],
                "transports": sorted(v["transports"]), "dsts": sorted(v["dsts"]),
                "alpn": sorted(v["alpns"]), "tls_versions": sorted(v["versions"]),
                "server_alpn": sorted(v["server_alpn"]),
                "resumed": v["resumed"],
                "full": v["full"],
                "session_ticket": v["session_ticket"],
                "category": classify_domain(k[0]),
            }
            for k, v in aggregated.items()
        ],
        "session_resumption": {
            "full_handshakes": sum(v["full"] for v in aggregated.values()),
            "resumed_handshakes": sum(v["resumed"] for v in aggregated.values()),
        },
    }
    (session_dir / "ja3_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def cmd_tlsproxy(args: argparse.Namespace) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    session_dir = DEFAULT_CAPTURE_ROOT / f"tlsproxy-{stamp}"
    session_dir.mkdir(parents=True, exist_ok=True)
    store = {"session_dir": session_dir, "lock": threading.Lock(), "records": []}

    upstream = args.upstream or f"127.0.0.1:{detect_clash_mixed_port()}"
    try:
        server = TlsProxyServer(("127.0.0.1", args.listen_port), upstream, store)
    except OSError as exc:
        print(f"[tlsproxy] 监听 127.0.0.1:{args.listen_port} 失败: {exc}")
        return 2

    print(f"[tlsproxy] 本地代理 : 127.0.0.1:{args.listen_port}（无需管理员）")
    print(f"[tlsproxy] 上游     : {upstream}（只转发字节，不解析 TLS，不落盘业务数据）")
    print(f"[tlsproxy] 会话目录 : {session_dir}")

    if args.launch:
        exe = resolve_antigravity_exe()
        if exe is None:
            print("[tlsproxy] 未找到 Antigravity.exe，请手动启动并注入 HTTPS_PROXY。")
        elif not args.yes:
            answer = input("重启 Antigravity（注入 HTTPS_PROXY 指向本代理）? [y/N] ")
            if answer.strip().lower() in ("y", "yes"):
                subprocess.run(["taskkill", "/IM", "Antigravity.exe", "/F", "/T"], capture_output=True)
                subprocess.run(["taskkill", "/IM", "language_server.exe", "/F", "/T"], capture_output=True)
                time.sleep(2)
                env = os.environ.copy()
                env["HTTPS_PROXY"] = env["HTTP_PROXY"] = f"http://127.0.0.1:{args.listen_port}"
                subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env)
                print(f"[tlsproxy] Antigravity 已重启（HTTPS_PROXY=127.0.0.1:{args.listen_port}）。")
        else:
            subprocess.run(["taskkill", "/IM", "Antigravity.exe", "/F", "/T"], capture_output=True)
            subprocess.run(["taskkill", "/IM", "language_server.exe", "/F", "/T"], capture_output=True)
            time.sleep(2)
            env = os.environ.copy()
            env["HTTPS_PROXY"] = env["HTTP_PROXY"] = f"http://127.0.0.1:{args.listen_port}"
            subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env)
            print(f"[tlsproxy] Antigravity 已重启（HTTPS_PROXY=127.0.0.1:{args.listen_port}）。")

    print()
    print("=" * 64)
    print("TLS 指纹捕获进行中（端到端 TLS 不被解析/修改，Google 无感知）。")
    if args.duration:
        print(f"--duration {args.duration} 秒后自动停止。")
    else:
        print("完成后回到本窗口按回车停止。")
    print("=" * 64)
    print()

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        if args.duration:
            time.sleep(args.duration)
        else:
            input()
    except KeyboardInterrupt:
        print("\n[tlsproxy] 收到 Ctrl+C，正在停止...")
    finally:
        server.shutdown()
        server.server_close()

    summary = aggregate_tlsproxy_summary(session_dir)
    if summary is None:
        print("[tlsproxy] 没有抓到 ClientHello。排查：")
        print("  - Antigravity 是否经本代理启动（HTTPS_PROXY 是否注入）；")
        print("  - 上游 Clash 代理是否可用（canary: curl -x http://127.0.0.1:7897 https://www.google.com）；")
        print("  - LS 若对部分域名绕过代理直连，这些连接不会经过本工具。")
        return 6
    print(f"[tlsproxy] ClientHello 共 {summary['client_hello_count']} 条，"
          f"唯一指纹 {len(summary['entries'])} 种。")
    print(f"[tlsproxy] 汇总: {session_dir / 'ja3_summary.json'}")
    print("[tlsproxy] 下一步: sniff_cli.py report 会自动合并本会话。")
    return 0


# ---------------------------------------------------------------------------
# report：合并指纹报告（纯函数，便于测试）
# ---------------------------------------------------------------------------

def parse_hub_constants(utils_py_text: str) -> Dict[str, str]:
    """从 src/utils.py 提取 agy2api 当前使用的 Hub 指纹常量。"""

    def _grab(pattern: str) -> str:
        m = re.search(pattern, utils_py_text)
        return m.group(1) if m else ""

    return {
        "version": _grab(r'OFFICIAL_HUB_VERSION\s*=\s*"([^"]*)"'),
        "cl": _grab(r'OFFICIAL_HUB_CL\s*=\s*"([^"]*)"'),
        "os": _grab(r'_HUB_FINGERPRINT_OS\s*=\s*"([^"]*)"'),
        "arch": _grab(r'_HUB_FINGERPRINT_ARCH\s*=\s*"([^"]*)"'),
    }


def proxy_user_agent(constants: Dict[str, str]) -> str:
    if not constants.get("version"):
        return ""
    return (
        f"antigravity/hub/{constants['version']} "
        f"(aidev_client; os_type={constants.get('os') or 'windows'}; "
        f"arch={constants.get('arch') or 'amd64'}; cl={constants.get('cl') or ''})"
    )


def parse_proxy_header_order(antigravity_py_text: str) -> List[str]:
    """从 build_antigravity_headers 的 dict 字面量提取代理当前 header 顺序。

    注意不能按 ``}`` 截断——dict 值里的 f-string（如 ``f"Bearer {token}"``）
    花括号会提前终结匹配，这里改用函数体边界 + ``return`` 截断。
    """
    m = re.search(
        r"def build_antigravity_headers\(.*?\n(.*?)(?=\ndef |\Z)",
        antigravity_py_text,
        re.S,
    )
    if not m:
        return []
    body = m.group(1)
    stop = body.find("return")
    if stop != -1:
        body = body[:stop]
    return re.findall(r'"([A-Za-z0-9_-]+)"\s*:', body)


def parse_proxy_envelope_order(antigravity_py_text: str) -> Tuple[List[str], List[str]]:
    """提取顶层信封与内层 request 的键序。

    信封只在 ``wrap_cli_request`` 的 payload dict 里找，避免命中源码其它位置的
    同名字段；内层键序取 ``_align_inner_request_topology`` 的 key_order 列表。
    """
    envelope: List[str] = []
    m = re.search(
        r"def wrap_cli_request\(.*?payload\s*=\s*\{(.*?)\}\s*\n\s*return",
        antigravity_py_text,
        re.S,
    )
    if m:
        envelope = re.findall(r'"([A-Za-z0-9_-]+)"\s*:', m.group(1))
    inner: List[str] = []
    m = re.search(r"key_order\s*=\s*\[(.*?)\]", antigravity_py_text, re.S)
    if m:
        inner = re.findall(r'"([A-Za-z0-9_-]+)"', m.group(1))
    return envelope, inner


def extract_ua_parts(ua: str) -> Dict[str, str]:
    """从 UA 字符串宽松提取版本/CL/平台；解析失败时保留原始串。"""
    version = _UA_VERSION_RE.search(ua)
    cl = _UA_CL_RE.search(ua)
    return {
        "raw": ua,
        "version": version.group(1) if version else "",
        "cl": cl.group(1) if cl else "",
        "os": (_UA_OS_RE.search(ua).group(1) if _UA_OS_RE.search(ua) else ""),
        "arch": (_UA_ARCH_RE.search(ua).group(1) if _UA_ARCH_RE.search(ua) else ""),
    }


def endpoint_of(target: str) -> str:
    clean = target.split("?")[0].strip("/")
    if ":" in clean:
        return clean.rsplit(":", 1)[1]
    return clean.rsplit("/", 1)[-1] if "/" in clean else clean


def summarize_request_semantics(body: Dict[str, Any]) -> Dict[str, Any]:
    """从请求 envelope 提取 labels 键值与 requestId 语义（纯函数，无 IO）。

    返回字段全部为新增字段，供报告做"真实 vs 代理"的 labels 结构比对：
    - labels_keys / labels_values：labels 的键序与原值；
    - request_id_segments / request_id_step：requestId 拆段与末段步号；
    - last_step_index：labels.last_step_index 原值；
    - cascade_matches_request_id / trajectory_matches_request_id：交叉一致性。
    """
    result: Dict[str, Any] = {
        "labels_keys": [],
        "labels_values": {},
        "request_id_segments": [],
        "request_id_step": None,
        "last_step_index": "",
        "cascade_matches_request_id": False,
        "trajectory_matches_request_id": False,
        "project": "",
        "model": "",
        "user_agent_field": "",
        "request_type": "",
        "generation_config": {},
        "contents_count": 0,
    }
    if not isinstance(body, dict):
        return result

    result["project"] = str(body.get("project", ""))
    result["model"] = str(body.get("model", ""))
    result["user_agent_field"] = str(body.get("userAgent", ""))
    result["request_type"] = str(body.get("requestType", ""))

    request_id = body.get("requestId")
    if isinstance(request_id, str) and request_id:
        segments = request_id.split("/")
        result["request_id_segments"] = segments
        if len(segments) >= 5:
            try:
                result["request_id_step"] = int(segments[4])
            except ValueError:
                result["request_id_step"] = None

    inner = body.get("request")
    if not isinstance(inner, dict):
        return result
    contents = inner.get("contents")
    if isinstance(contents, list):
        result["contents_count"] = len(contents)
    generation_config = inner.get("generationConfig")
    if isinstance(generation_config, dict):
        result["generation_config"] = generation_config

    labels = inner.get("labels")
    if not isinstance(labels, dict):
        return result

    result["labels_keys"] = list(labels.keys())
    result["labels_values"] = {str(k): str(v) for k, v in labels.items()}
    result["last_step_index"] = str(labels.get("last_step_index", ""))

    segments = result["request_id_segments"]
    if len(segments) >= 5:
        result["cascade_matches_request_id"] = (
            str(labels.get("cascade_id", "")) == segments[1]
        )
        result["trajectory_matches_request_id"] = (
            str(labels.get("trajectory_id", "")) == segments[3]
        )
    return result


def framing_from_headers(headers: Sequence[Sequence[str]]) -> str:
    """从请求头推断分帧方式（chunked / content-length / none）。"""
    has_length = False
    for pair in headers:
        if len(pair) < 2:
            continue
        name = str(pair[0]).lower()
        value = str(pair[1]).lower()
        if name == "transfer-encoding" and "chunked" in value:
            return "chunked"
        if name == "content-length":
            has_length = True
    return "content-length" if has_length else "none"


def load_redirect_meta(session_dir: Path) -> List[Dict[str, Any]]:
    """读取离线重定向会话的 *.request.meta.json（保序）+ envelope 键序。"""
    metas: List[Dict[str, Any]] = []
    for meta_path in sorted(session_dir.glob("*.request.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        body_json_path = meta_path.with_name(meta_path.name.replace(
            ".request.meta.json", ".request.json"
        ))
        envelope_keys: List[str] = []
        inner_keys: List[str] = []
        semantics = summarize_request_semantics({})
        if body_json_path.is_file():
            try:
                body = json.loads(body_json_path.read_text(encoding="utf-8"))
                if isinstance(body, dict):
                    envelope_keys = list(body.keys())
                    inner = body.get("request")
                    if isinstance(inner, dict):
                        inner_keys = list(inner.keys())
                    semantics = summarize_request_semantics(body)
            except (OSError, json.JSONDecodeError):
                pass
        metas.append(
            {
                "seq": meta.get("seq", 0),
                "received_at": meta.get("received_at", ""),
                "method": meta.get("method", ""),
                "target": meta.get("target", ""),
                "endpoint": endpoint_of(meta.get("target", "")),
                "http_version": meta.get("http_version", ""),
                "headers": meta.get("headers", []),
                "body_bytes": meta.get("body_bytes", 0),
                "client_port": meta.get("client_port", 0),
                "framing": meta.get("framing")
                or framing_from_headers(meta.get("headers", [])),
                "envelope_keys": envelope_keys,
                "inner_keys": inner_keys,
                **semantics,
            }
        )
    _annotate_connection_reuse(metas)
    return metas


def _annotate_connection_reuse(metas: List[Dict[str, Any]]) -> None:
    """按 client_port 分组，标注同一 TCP 连接内的请求序号与到达间隔。"""
    by_port: "OrderedDict[int, List[Dict[str, Any]]]" = OrderedDict()
    for meta in metas:
        by_port.setdefault(int(meta.get("client_port") or 0), []).append(meta)

    for group in by_port.values():
        group.sort(key=lambda m: str(m.get("received_at", "")))
        previous = None
        for index, meta in enumerate(group):
            meta["connection_request_index"] = index
            meta["connection_request_count"] = len(group)
            if previous is None:
                meta["arrival_gap_ms"] = None
            else:
                meta["arrival_gap_ms"] = _gap_ms(
                    previous.get("received_at", ""), meta.get("received_at", "")
                )
            previous = meta


def _gap_ms(earlier: str, later: str) -> Optional[float]:
    try:
        start = datetime.fromisoformat(str(earlier).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(later).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((end - start).total_seconds() * 1000, 1)


def load_ja3_summary(session_dir: Path) -> Optional[Dict[str, Any]]:
    path = session_dir / "ja3_summary.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def parse_proxy_labels_keys(antigravity_py_text: str) -> List[str]:
    """从 _build_labels 的 return 字典中提取 labels 键序。"""
    match = re.search(
        r"def _build_labels[\s\S]*?return \{([\s\S]*?)\n    \}",
        antigravity_py_text,
    )
    if not match:
        return []
    return re.findall(r'"([A-Za-z0-9_]+)"\s*:', match.group(1))


def load_proxy_fingerprint(repo_root: Path | str) -> Dict[str, Any]:
    """解析 agy2api 当前源码中的出站指纹。"""
    repo_root = Path(repo_root)
    utils_text = ""
    antigravity_text = ""
    utils_path = repo_root / "src" / "utils.py"
    antigravity_path = repo_root / "src" / "api" / "antigravity.py"
    if utils_path.is_file():
        utils_text = utils_path.read_text(encoding="utf-8", errors="replace")
    if antigravity_path.is_file():
        antigravity_text = antigravity_path.read_text(encoding="utf-8", errors="replace")
    constants = parse_hub_constants(utils_text)
    envelope, inner = parse_proxy_envelope_order(antigravity_text)
    return {
        "constants": constants,
        "user_agent": proxy_user_agent(constants),
        "header_order": parse_proxy_header_order(antigravity_text),
        "envelope_order": envelope,
        "inner_order": inner,
        "labels_keys": parse_proxy_labels_keys(antigravity_text),
    }


def latest_session(capture_root: Path, pattern: str, required_file: str) -> Optional[Path]:
    """取 captures/ 下最新一个含 required_file 的会话目录。"""
    if not capture_root.is_dir():
        return None
    candidates = sorted(
        (d for d in capture_root.iterdir()
         if d.is_dir() and re.match(pattern, d.name) and (d / required_file).is_file()),
        key=lambda d: d.name,
    )
    return candidates[-1] if candidates else None


def _header_names(headers: Sequence[Sequence[str]]) -> List[str]:
    return [str(pair[0]) for pair in headers if len(pair) >= 2]


def _header_value(headers: Sequence[Sequence[str]], name: str) -> str:
    lowered = name.lower()
    for pair in headers:
        if len(pair) >= 2 and str(pair[0]).lower() == lowered:
            return str(pair[1])
    return ""


HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "keep-alive"}


def real_header_orders(redirect_metas: Sequence[Dict[str, Any]]) -> "OrderedDict[str, List[str]]":
    """按 endpoint 取真实 header 顺序（剔除逐跳头，保留业务头原序）。"""
    orders: "OrderedDict[str, List[str]]" = OrderedDict()
    for meta in redirect_metas:
        endpoint = meta.get("endpoint", "")
        if not endpoint or endpoint in orders:
            continue
        orders[endpoint] = [
            name for name in _header_names(meta.get("headers", []))
            if name.lower() not in HOP_HEADERS
        ]
    return orders


def compare_header_orders(
    proxy_headers: Sequence[str],
    real_orders: "OrderedDict[str, List[str]]",
) -> List[Dict[str, Any]]:
    """逐 endpoint 比对代理 header 构造与真实顺序（纯函数，无 IO）。"""
    proxy_lower = [h.lower() for h in proxy_headers]
    results: List[Dict[str, Any]] = []
    for endpoint, real_names in real_orders.items():
        real_lower = [n.lower() for n in real_names]
        missing = [n for n in real_names if n.lower() not in proxy_lower]
        # Content-Type 只随带 body 的 POST 发送；GET 端点（如 cascadeNuxes）
        # 没有它，不能因此判定"代理多出头"。
        extra = [
            h
            for h in proxy_headers
            if h.lower() not in real_lower
            and not (h.lower() == "content-type" and "content-type" not in real_lower)
        ]
        ordered = [h for h in proxy_headers if h.lower() in real_lower]
        order_match = [h.lower() for h in ordered] == real_lower
        results.append(
            {
                "endpoint": endpoint,
                "real_order": list(real_names),
                "proxy_order": list(proxy_headers),
                "missing": missing,
                "extra": extra,
                "order_match": order_match,
                "ok": not missing and not extra and order_match,
            }
        )
    return results


MODEL_ENUM_RE = re.compile(r"\bMODEL_(?!PROVIDER_)[A-Z][A-Z0-9_]*\b")


def _find_model_enum(node: Any, depth: int = 0) -> str:
    """在任意嵌套结构里找 MODEL_PLACEHOLDER_* 字符串（优先显式键）。"""
    if depth > 6:
        return ""
    if isinstance(node, str):
        found = MODEL_ENUM_RE.search(node)
        return found.group(0) if found else ""
    if isinstance(node, dict):
        for key in ("modelEnum", "model_enum", "model", "enum", "id", "name"):
            if key in node:
                found = _find_model_enum(node[key], depth + 1)
                if found:
                    return found
        for value in node.values():
            found = _find_model_enum(value, depth + 1)
            if found:
                return found
        return ""
    if isinstance(node, list):
        for value in node:
            found = _find_model_enum(value, depth + 1)
            if found:
                return found
    return ""


def parse_model_enum_map(data: Any) -> Dict[str, str]:
    """从 fetchAvailableModels 响应中提取 model_id -> MODEL_PLACEHOLDER 映射。"""
    if not isinstance(data, dict):
        return {}
    models = data.get("models")
    items: List[Tuple[str, Any]] = []
    if isinstance(models, dict):
        items = [(str(k), v) for k, v in models.items()]
    elif isinstance(models, list):
        for entry in models:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id") or entry.get("modelId") or entry.get("name") or ""
            items.append((str(model_id), entry))
    mapping: Dict[str, str] = {}
    for model_id, info in items:
        if not model_id:
            continue
        enum = _find_model_enum(info)
        if enum:
            mapping[model_id] = enum
    return mapping


def _load_response_json(text: str) -> Any:
    """把 response.txt 解析成 JSON：兼容纯 JSON 与 SSE 单帧。"""
    stripped = (text or "").strip()
    if stripped.startswith("data:"):
        for line in stripped.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                stripped = line[len("data:"):].strip()
                break
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(stripped[start:end + 1])
            except json.JSONDecodeError:
                return None
        return None


def load_model_enum_map(session_dir: Path) -> Dict[str, str]:
    """读取会话目录里所有 fetchAvailableModels 响应，合并成枚举映射。

    跳过 ``synthetic: true`` 的离线合成响应：那些枚举值不是真实上游数据。
    """
    # 优先读取完整的 .response.full.txt（大响应会被预览截断，截断后无法解析 JSON）
    candidates = sorted(
        set(session_dir.glob("*fetchAvailableModels*.response.full.txt"))
        | set(session_dir.glob("*fetchAvailableModels*.response.txt"))
    )
    mapping: Dict[str, str] = {}
    for path in candidates:
        if path.name.endswith(".response.txt"):
            full = path.with_name(path.name.replace(".response.txt", ".response.full.txt"))
            if full.is_file():
                continue  # 由完整文件负责
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = _load_response_json(text)
        if parsed is None:
            continue
        if isinstance(parsed, dict) and parsed.get("synthetic"):
            continue
        mapping.update(parse_model_enum_map(parsed))

    # 兼容旧会话：当 text 文件被截断或缺失时，直接从 .response.bin 解出完整 JSON。
    if not mapping:
        for bin_path in sorted(session_dir.glob("*fetchAvailableModels*.response.bin")):
            meta_path = bin_path.with_name(
                bin_path.name.replace(".response.bin", ".response.meta.json")
            )
            parsed = _load_response_bin(bin_path, meta_path)
            if isinstance(parsed, dict) and not parsed.get("synthetic"):
                mapping.update(parse_model_enum_map(parsed))
    return mapping


def _load_response_bin(bin_path: Path, meta_path: Path) -> Any:
    """从 .response.bin（按 meta 里的 Content-Encoding 解压）解析 JSON。"""
    try:
        raw = bin_path.read_bytes()
    except OSError:
        return None
    encodings: List[str] = []
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            encodings = [
                str(value)
                for name, value in meta.get("headers", [])
                if str(name).lower() == "content-encoding"
            ]
        except (OSError, json.JSONDecodeError, ValueError):
            encodings = []
    if any("gzip" in value.lower() for value in encodings):
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


class _HelloCatcher(socketserver.ThreadingTCPServer):
    """只读取 ClientHello 就关闭（不完成握手，因此不需要证书）。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: Tuple[str, int]) -> None:
        super().__init__(address, _HelloCaptureHandler)
        self.capture: List[Dict[str, Any]] = []


class _HelloCaptureHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = b""
        self.request.settimeout(5.0)
        while len(data) < MAX_SNIFF_BYTES:
            try:
                chunk = self.request.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
            status, info = parse_client_hello(data)
            if status == OK and info:
                self.server.capture.append(info)  # type: ignore[attr-defined]
                return
            if status == BAD:
                break


class _RawHeaderRecorder(socketserver.ThreadingTCPServer):
    """记录到它的原始 HTTP/1.1 请求头（用于校验 Go 转发器的 header 顺序）。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: Tuple[str, int]) -> None:
        super().__init__(address, _RawHeaderHandler)
        self.headers: List[str] = []


class _RawHeaderHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(5.0)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            try:
                chunk = self.request.recv(8192)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        if b"\r\n\r\n" in data:
            head = data.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
            self.server.headers.append(head)  # type: ignore[attr-defined]
        try:
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"
            )
        except OSError:
            pass


def _sidecar_binary() -> Optional[Path]:
    base = REPO_ROOT / "tools" / "upstream_tls_proxy" / "bin"
    for name in ("upstream-proxy.exe", "upstream-proxy"):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _start_sidecar(timeout: float = 8.0):
    binary = _sidecar_binary()
    if binary is None:
        return None, None
    port = _free_port()
    proc = subprocess.Popen(
        [str(binary), "--listen", f"127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_port(port, timeout):
        proc.terminate()
        return None, None
    return proc, port


def _send_forward_request(
    proxy_port: int,
    target_url: str,
    timeout: float,
    sni: str = "",
) -> None:
    body = b'{"probe":true}'
    extra = f"X-Upstream-SNI: {sni}\r\n" if sni else ""
    head = (
        f"POST /__probe HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{proxy_port}\r\n"
        f"X-Upstream-URL: {target_url}\r\n"
        f"{extra}"
        "User-Agent: antigravity/hub/2.33.0 "
        "(aidev_client; os_type=windows; arch=amd64; cl=1023160026)\r\n"
        "Content-Type: application/json\r\n"
        "Accept-Encoding: gzip\r\n"
        # httpx 会自动补这个头；转发器必须把它丢掉（真实客户端不发）。
        "Accept: */*\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode()
    try:
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=timeout) as conn:
            conn.sendall(head + body)
            try:
                conn.recv(4096)
            except OSError:
                pass
    except OSError:
        pass


def _probe_go(catcher_port: int, timeout: float) -> None:
    proc, port = _start_sidecar(timeout)
    if proc is None or port is None:
        raise RuntimeError("Go 转发器启动失败（tools/upstream_tls_proxy/bin 下缺少二进制？）")
    try:
        # 目标写成 IP，但注入真实 SNI，让 ClientHello 带上 server_name 扩展（与实测一致）。
        _send_forward_request(
            port,
            f"https://127.0.0.1:{catcher_port}/__probe",
            timeout,
            sni="daily-cloudcode-pa.googleapis.com",
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def capture_proxy_clienthello(timeout: float = 8.0) -> Dict[str, Any]:
    """用代理自己的出站实现（Go 转发器）连本地监听器，抓它实际发出的 ClientHello。

    出站 TLS 由 tools/upstream_tls_proxy 承担（与真实客户端同栈）；
    二进制缺失时返回 skipped，不影响 header 阶段的校验。
    """
    if _sidecar_binary() is None:
        return {
            "skipped": "Go 转发器未编译：运行 tools/upstream_tls_proxy/build.ps1（或 build.sh）"
        }

    thread_error: List[Exception] = []

    def _run() -> None:
        try:
            _probe_go(catcher_port, timeout)
        except Exception as exc:  # noqa: BLE001 - 握手被服务端主动掐断属预期
            thread_error.append(exc)

    with _HelloCatcher(("127.0.0.1", 0)) as server:
        catcher_port = int(server.server_address[1])
        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        server.timeout = 0.5
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not server.capture:
            server.handle_request()
        worker.join(timeout=timeout)

    if not server.capture:
        return {
            "error": "未捕获到 ClientHello",
            "client_error": str(thread_error[0]) if thread_error else "",
        }
    info = dict(server.capture[0])
    info["transport"] = "go"
    return {"info": info}


def compare_upstream_header_order(
    actual_headers: Sequence[str], expected_headers: Sequence[str]
) -> Dict[str, Any]:
    """比对 Go 转发器实际写出的 header 顺序（只比较双方都有的头）。"""
    actual_lower = [h.lower() for h in actual_headers]
    expected_lower = [h.lower() for h in expected_headers]
    common_expected = [h for h in expected_lower if h in actual_lower]
    common_actual = [h for h in actual_lower if h in expected_lower]
    return {
        "actual": list(actual_headers),
        "expected_present": common_expected,
        "ok": bool(common_expected) and common_actual == common_expected,
    }


def capture_go_upstream_headers(timeout: float = 8.0) -> Dict[str, Any]:
    """让 Go 转发器向本地明文记录器发一次请求，抓它写出的 header 顺序。"""
    if _sidecar_binary() is None:
        return {"skipped": "Go 转发器未编译"}
    with _RawHeaderRecorder(("127.0.0.1", 0)) as recorder:
        rec_port = int(recorder.server_address[1])
        proc, port = _start_sidecar(timeout)
        if proc is None or port is None:
            return {"error": "Go 转发器启动失败"}
        try:
            _send_forward_request(port, f"http://127.0.0.1:{rec_port}/__probe", timeout)
            recorder.timeout = 1.0
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not recorder.headers:
                recorder.handle_request()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    if not recorder.headers:
        return {"error": "未捕获到上游请求头"}
    head = recorder.headers[0]
    names = [line.split(":", 1)[0].strip() for line in head.splitlines()[1:] if ":" in line]
    return {"header_order": names, "raw": head}


def cmd_verify(args: argparse.Namespace) -> int:
    """闭环校验：代理实际发出的 ClientHello + header 构造 vs 实测真实值。"""
    from tls_fingerprint import diff_clienthello  # noqa: PLC0415 - 同目录工具模块

    expected_ja3 = args.ja3 or load_expected_ja3()
    proxy = load_proxy_fingerprint(REPO_ROOT)

    redirect_dir = Path(args.redirect_dir) if args.redirect_dir else latest_session(
        DEFAULT_CAPTURE_ROOT, r"^\d{8}-\d{6}", "index.jsonl"
    )
    redirect_metas = load_redirect_meta(redirect_dir) if redirect_dir else []
    header_results = compare_header_orders(
        proxy.get("header_order", []), real_header_orders(redirect_metas)
    )

    tls_stage = capture_proxy_clienthello(args.timeout)
    tls_result: Dict[str, Any]
    if "info" in tls_stage:
        tls_result = diff_clienthello(tls_stage["info"], expected_ja3)
        tls_result["transport"] = tls_stage["info"].get("transport", "")
    else:
        tls_result = tls_stage

    # Go 转发器的上游 header 顺序（只有 Go 传输可用时才有意义）。
    upstream_headers: Dict[str, Any] = {"skipped": "未使用 Go 转发器"}
    if tls_result.get("transport") == "go" or _sidecar_binary() is not None:
        captured = capture_go_upstream_headers(args.timeout)
        if "header_order" in captured:
            expected_order: List[str] = []
            if header_results:
                expected_order = list(header_results[0]["real_order"])
            if not expected_order:
                expected_order = ["User-Agent", "Authorization", "Content-Type", "Accept-Encoding"]
            comparison = compare_upstream_header_order(captured["header_order"], expected_order)
            comparison["raw"] = captured["raw"]
            upstream_headers = comparison
        else:
            upstream_headers = captured

    envelope_result: Dict[str, Any] = {"skipped": "未启用（加 --envelope 或 --schema）"}
    envelope_requested = bool(args.envelope or args.schema)
    if envelope_requested:
        schema_path = Path(args.schema) if args.schema else (REPO_ROOT / "fidelity_schema.json")
        envelope_result = check_envelope_against_schema(schema_path, args.model or "")

    # 没有抓包会话时 header 阶段跳过（CI 环境常见），不作为失败判据。
    headers_ok = all(r["ok"] for r in header_results) if header_results else True
    tls_ok = bool(tls_result.get("ok"))
    upstream_ok = ("skipped" in upstream_headers) or bool(upstream_headers.get("ok"))
    envelope_ok = bool(envelope_result.get("ok")) if envelope_requested else True
    overall = headers_ok and tls_ok and upstream_ok and envelope_ok

    if args.json:
        print(json.dumps(
            {
                "ok": overall,
                "expected_ja3": expected_ja3,
                "tls": tls_result,
                "headers": header_results,
                "upstream_headers": upstream_headers,
                "envelope": envelope_result,
                "session": str(redirect_dir) if redirect_dir else "",
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0 if overall else 1

    print(f"[verify] 抓包会话: {redirect_dir or '（无，header 阶段跳过）'}")
    print(f"[verify] 期望 JA3 : {expected_ja3}")
    if "skipped" in tls_result or "error" in tls_result:
        reason = tls_result.get("skipped") or tls_result.get("error")
        client_error = tls_result.get("client_error")
        print(f"[verify] TLS 阶段  : SKIP ({reason})")
        if client_error:
            print(f"[verify]             客户端报错(预期): {client_error[:160]}")
    else:
        print(f"[verify] 实测 JA3 : {tls_result.get('ja3_full')}")
        print(f"[verify] JA3 md5   : {tls_result.get('ja3_md5')} "
              f"({'匹配' if tls_result.get('md5_match') else '不匹配'})")
        print(f"[verify] ALPN      : {tls_result.get('alpn')} "
              f"({'匹配' if tls_result.get('alpn_match') else '不匹配'})")
        for key in ("ciphers", "extensions", "groups", "formats"):
            seg = tls_result.get("segments", {}).get(key, {})
            flag = "匹配" if seg.get("match") else (
                f"缺失={seg.get('missing')} 多出={seg.get('extra')}"
            )
            print(f"[verify] {key.ljust(10)}: {flag}")

    if not header_results:
        print("[verify] header 阶段: SKIP (没有离线重定向会话)")
    for res in header_results:
        print(f"[verify] {res['endpoint']}: "
              f"{'OK' if res['ok'] else '差异'} "
              f"(缺失={res['missing'] or '无'}, 多出={res['extra'] or '无'}, "
              f"顺序={'一致' if res['order_match'] else '不一致'})")

    if "skipped" in upstream_headers:
        print(f"[verify] 上游 header: SKIP ({upstream_headers['skipped']})")
    elif "error" in upstream_headers:
        print(f"[verify] 上游 header: ERROR ({upstream_headers['error']})")
    else:
        print(f"[verify] 上游 header: {'OK' if upstream_headers['ok'] else '顺序不一致'}")
        print(f"[verify]   实际顺序: {' -> '.join(upstream_headers['actual'])}")
        print(f"[verify]   期望子集: {' -> '.join(upstream_headers['expected_present'])}")

    if "skipped" in envelope_result:
        print(f"[verify] 信封 schema: SKIP ({envelope_result['skipped']})")
    elif "error" in envelope_result:
        print(f"[verify] 信封 schema: ERROR ({envelope_result['error']})")
    else:
        print(f"[verify] 信封 schema: {'OK' if envelope_result['ok'] else '不一致'} "
              f"(model={envelope_result.get('model', '')})")
        for name, check in envelope_result.get("checks", {}).items():
            if not check.get("ok"):
                print(f"[verify]   {name}: 期望={check.get('expected') or check.get('allowed')} "
                      f"实际={check.get('actual')}")

    print(f"[verify] 结果: {'通过' if overall else '存在差异'}")
    return 0 if overall else 1


def load_expected_ja3() -> str:
    """默认期望 JA3：优先用抓包里最新的实测值，退回内置常量。"""
    from tls_fingerprint import REAL_ANTIGRAVITY_JA3_FULL  # noqa: PLC0415

    session = latest_session(DEFAULT_CAPTURE_ROOT, r"^(passive|tlsproxy)-", "ja3_summary.json")
    summary = load_ja3_summary(session) if session else None
    if summary:
        for entry in summary.get("entries", []):
            if entry.get("category") == "antigravity" and entry.get("ja3_full"):
                return str(entry["ja3_full"])
    return REAL_ANTIGRAVITY_JA3_FULL


def cmd_models(args: argparse.Namespace) -> int:
    """导出 model_id -> MODEL_PLACEHOLDER 映射（供 src/model_enum.py 使用）。"""
    session_dir = Path(args.session_dir) if args.session_dir else latest_session(
        DEFAULT_CAPTURE_ROOT, r"^\d{8}-\d{6}", "index.jsonl"
    )
    if not session_dir:
        print("[sniff] 没有可用的重定向会话；先运行 run_capture.ps1 -Offline -CaptureAll。")
        return 2
    mapping = load_model_enum_map(session_dir)
    if not mapping:
        print(f"[sniff] {session_dir} 里没有 fetchAvailableModels 响应，或响应中无枚举字段。")
        print("        用 run_capture.ps1 -Offline -CaptureAll 重新抓一次。")
        return 2

    print(f"[sniff] 会话: {session_dir}")
    print(f"[sniff] 提取到 {len(mapping)} 个模型枚举映射：")
    width = max(len(k) for k in mapping)
    for model_id, enum in sorted(mapping.items()):
        print(f"  {model_id.ljust(width)}  ->  {enum}")

    block = "MODEL_ENUM_MAP: dict[str, str] = {\n" + "".join(
        f'    "{k}": "{v}",\n' for k, v in sorted(mapping.items())
    ) + "}\n"
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(block, encoding="utf-8")
        print(f"[sniff] 已写入: {out_path}")
    print("\n# 可直接粘进 src/model_enum.py：\n")
    print(block)
    return 0


def build_fidelity_schema(redirect_metas: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """从离线重定向会话生成保真度基准（供运行时严格校验与 CI 使用）。

    只收录"可复现的结构事实"：端点 header 顺序/分帧、信封键序、requestType
    取值、labels 键序、每模型的 model_enum 与 generationConfig。
    不收录任何账号标识、project 值或 prompt 内容。
    """
    schema: Dict[str, Any] = {
        "version": 1,
        "generated_at": utc_now(),
        "endpoints": {},
        "models": {},
    }
    for meta in redirect_metas:
        endpoint = meta.get("endpoint", "")
        if not endpoint:
            continue
        entry = schema["endpoints"].setdefault(
            endpoint,
            {
                "methods": [],
                "header_order": [],
                "framing": [],
                "envelope_keys": [],
                "inner_keys": [],
                "request_types": [],
                "labels_keys": [],
                "inner_orders": [],
                "labels_orders": [],
                "user_agent_field": [],
                "contents_count": [],
            },
        )
        _append_unique(entry["methods"], meta.get("method", ""))
        header_names = _header_names(meta.get("headers", []))
        if header_names and not entry["header_order"]:
            entry["header_order"] = header_names
        _append_unique(entry["framing"], meta.get("framing", ""))
        # 同一端点可能有多形态（如 2.34.0 的 checkpoint 与 agent），
        # 键序取"按首次出现顺序合并后的超集"，避免以第一个样本为准导致误判。
        entry["envelope_keys"] = _merge_unique(
            entry["envelope_keys"], meta.get("envelope_keys", [])
        )
        entry["inner_keys"] = _merge_unique(entry["inner_keys"], meta.get("inner_keys", []))
        _append_unique(entry["request_types"], meta.get("request_type", ""))
        entry["labels_keys"] = _merge_unique(
            entry["labels_keys"], meta.get("labels_keys", [])
        )
        # 同一端点可能有多种合法键序（checkpoint vs agent），按"完整键序"分别记录，
        # 校验时只需命中其中一种，而不是被合并后的错误顺序误判。
        _append_order(entry["inner_orders"], meta.get("inner_keys", []))
        _append_order(entry["labels_orders"], meta.get("labels_keys", []))
        _append_unique(entry["user_agent_field"], meta.get("user_agent_field", ""))
        count = meta.get("contents_count") or 0
        if count:
            entry["contents_count"].append(count)

        model = meta.get("model", "")
        if model:
            model_entry = schema["models"].setdefault(
                model,
                {"model_enum": "", "generation_config": {}, "request_types": []},
            )
            enum = (meta.get("labels_values") or {}).get("model_enum", "")
            if enum:
                model_entry["model_enum"] = enum
            if meta.get("generation_config"):
                model_entry["generation_config"] = meta["generation_config"]
            _append_unique(model_entry["request_types"], meta.get("request_type", ""))

    for entry in schema["endpoints"].values():
        entry["header_order"] = [h for h in entry["header_order"] if h]
        entry["framing"] = [f for f in entry["framing"] if f]
        entry["request_types"] = [t for t in entry["request_types"] if t]
        entry["user_agent_field"] = [u for u in entry["user_agent_field"] if u]
        counts = entry["contents_count"]
        entry["contents_count"] = [min(counts), max(counts)] if counts else []
    for model_entry in schema["models"].values():
        model_entry["request_types"] = [t for t in model_entry["request_types"] if t]
    return schema


def _append_unique(target: List[Any], value: Any) -> None:
    if value and value not in target:
        target.append(value)


def _merge_unique(target: List[str], values: Sequence[str]) -> List[str]:
    """按首次出现顺序合并，保留已有顺序（用于多形态样本的键序超集）。"""
    for value in values:
        if value and value not in target:
            target.append(value)
    return target


def _append_order(target: List[List[str]], values: Sequence[str]) -> None:
    """记录一种完整的键序（去重，保持首次出现的样本顺序）。"""
    order = [value for value in values if value]
    if order and order not in target:
        target.append(order)


def _matches_any_order(actual: Sequence[str], orders: Sequence[Sequence[str]]) -> bool:
    """actual 是否命中任一种已知键序：所有键都在该序中，且相对顺序一致。"""
    actual_list = list(actual)
    for order in orders:
        order_list = list(order)
        if all(key in order_list for key in actual_list) and _is_subsequence(
            actual_list, order_list
        ):
            return True
    return False


def _is_subsequence(needles: Sequence[str], haystack: Sequence[str]) -> bool:
    iterator = iter(haystack)
    return all(any(item == candidate for candidate in iterator) for item in needles)


def compare_envelope_to_schema(
    payload: Dict[str, Any], endpoint_schema: Dict[str, Any]
) -> Dict[str, Any]:
    """把当前实现产出的信封与 schema 比对（纯函数）。"""
    checks: Dict[str, Any] = {}
    inner = payload.get("request") if isinstance(payload.get("request"), dict) else {}

    expected_envelope = endpoint_schema.get("envelope_keys") or []
    checks["envelope_keys"] = {
        "expected": expected_envelope,
        "actual": list(payload.keys()),
        "ok": not expected_envelope or list(payload.keys()) == expected_envelope,
    }

    expected_inner = endpoint_schema.get("inner_keys") or []
    actual_inner = list(inner.keys())
    # 同一端点可能有多种合法键序（2.34.0 的 checkpoint 与 agent），
    # 只要命中其中一种即可；未要求的键（如无 tools）允许缺省。
    expected_inner_orders = endpoint_schema.get("inner_orders") or (
        [expected_inner] if expected_inner else []
    )
    checks["inner_keys"] = {
        "expected": expected_inner,
        "actual": actual_inner,
        "ok": not expected_inner_orders
        or _matches_any_order(actual_inner, expected_inner_orders),
    }

    expected_labels = endpoint_schema.get("labels_keys") or []
    actual_labels = list((inner.get("labels") or {}).keys())
    expected_label_orders = endpoint_schema.get("labels_orders") or (
        [expected_labels] if expected_labels else []
    )
    checks["labels_keys"] = {
        "expected": expected_labels,
        "actual": actual_labels,
        # 无 labels 的形态（checkpoint）不作为约束；有 labels 时必须完整命中一种已知键序。
        "ok": not actual_labels
        or not expected_label_orders
        or any(list(order) == actual_labels for order in expected_label_orders),
    }

    allowed_types = endpoint_schema.get("request_types") or []
    actual_type = payload.get("requestType", "")
    has_tools = bool(inner.get("tools"))
    # 实抓样本只覆盖"带 tools 的 agent 请求"；无 tools 的形态缺少证据，
    # 因此只在有 tools 时把 requestType 当作硬判据，其余仅记录。
    checks["request_type"] = {
        "allowed": allowed_types,
        "actual": actual_type,
        "ok": not allowed_types or not has_tools or actual_type in allowed_types,
        "note": "" if has_tools else "无 tools 场景缺少实抓证据，不作为判据",
    }

    return {
        "ok": all(check["ok"] for check in checks.values()),
        "checks": checks,
    }


def _build_probe_payload(model: str) -> Dict[str, Any]:
    """用当前实现构造一个最小信封（供 verify --envelope 使用）。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import asyncio  # noqa: PLC0415 - 仅校验命令使用

    from src.api.antigravity import wrap_cli_request  # noqa: PLC0415

    request = {
        "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
        "systemInstruction": {"parts": [{"text": "You are a helpful assistant."}]},
    }
    payload, _rid = asyncio.run(wrap_cli_request(request, model, "probe-project"))
    return payload


def check_envelope_against_schema(
    schema_path: Path, model: str = ""
) -> Dict[str, Any]:
    """加载 schema 并用当前实现构造信封做比对；异常一律降级为 skipped。"""
    if not schema_path.is_file():
        return {"skipped": f"schema 不存在: {schema_path}"}
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": f"schema 读取失败: {exc}"}

    endpoints = schema.get("endpoints") or {}
    endpoint_schema = endpoints.get("streamGenerateContent") or (
        next(iter(endpoints.values())) if endpoints else {}
    )
    if not endpoint_schema:
        return {"error": "schema 中没有可用端点"}

    chosen_model = model
    if not chosen_model:
        models = schema.get("models") or {}
        if models:
            chosen_model = next(iter(models))
    if not chosen_model:
        return {"error": "schema 中没有模型，无法构造信封"}

    try:
        payload = _build_probe_payload(chosen_model)
    except Exception as exc:  # noqa: BLE001 - 环境缺依赖时降级
        return {"skipped": f"无法构造信封: {type(exc).__name__}: {exc}"}

    result = compare_envelope_to_schema(payload, endpoint_schema)
    result["model"] = chosen_model
    result["schema"] = str(schema_path)
    return result


def cmd_schema(args: argparse.Namespace) -> int:
    """从抓包会话导出 fidelity_schema.json。"""
    session_dir = Path(args.session_dir) if args.session_dir else latest_session(
        DEFAULT_CAPTURE_ROOT, r"^\d{8}-\d{6}", "index.jsonl"
    )
    if not session_dir:
        print("[sniff] 没有可用的重定向会话；先运行 run_capture.ps1 -Offline -CaptureAll。")
        return 2
    metas = load_redirect_meta(session_dir)
    if not metas:
        print(f"[sniff] {session_dir} 中没有请求报文。")
        return 2
    schema = build_fidelity_schema(metas)
    out_path = Path(args.out) if args.out else (REPO_ROOT / "fidelity_schema.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[sniff] schema: {out_path}")
    print(f"[sniff] 端点 {len(schema['endpoints'])} 个，模型 {len(schema['models'])} 个")
    for endpoint, entry in schema["endpoints"].items():
        print(f"  - {endpoint}: framing={entry['framing']} "
              f"requestType={entry['request_types']} "
              f"header={len(entry['header_order'])} 项")
    return 0


def build_fingerprint_report(
    ja3_summary: Optional[Dict[str, Any]],
    redirect_metas: List[Dict[str, Any]],
    proxy: Dict[str, Any],
    sources: Dict[str, str],
) -> str:
    """生成 fingerprint_report.md 正文。所有结论只基于传入数据，无 IO。"""
    lines: List[str] = [
        "# Antigravity 真实客户端指纹报告",
        "",
        f"- 生成时间: {utc_now()}",
        f"- 被动会话: {sources.get('passive') or '（无，未运行 passive）'}",
        f"- 离线重定向会话: {sources.get('redirect') or '（无，未运行 run_capture.ps1 -Offline）'}",
        "",
        "> 结论均以本机实抓数据为准；抓包模式见 README 的风险矩阵。",
        "",
    ]

    # ---- 1. TLS 指纹 ----
    lines += ["## 1. 真实客户端 TLS 指纹（被动抓包）", ""]
    lines += [
        "对齐基准（内置常量，`sniff_cli.py verify` 默认用它做判据）：",
        "",
        f"- JA3 完整串: `{REAL_ANTIGRAVITY_JA3_FULL}`",
        f"- ALPN: `{','.join(REAL_ANTIGRAVITY_ALPN)}`",
        "",
    ]
    real_api_alpns: set = set()
    if ja3_summary and ja3_summary.get("entries"):
        targets = [
            e for e in ja3_summary["entries"]
            if e["category"] in ("antigravity", "related")
        ]
        for e in ja3_summary["entries"]:
            if e.get("category") == "antigravity" and e.get("sni", "").startswith("daily-cloudcode-pa"):
                real_api_alpns.update(e.get("alpn") or [])
        if targets:
            lines += [
                "| 域名 | 传输 | 次数 | JA3 md5 | JA4 | ALPN | TLS 版本 |",
                "|---|---|---|---|---|---|---|",
            ]
            for e in targets:
                lines.append(
                    f"| {e['sni']} | {'/'.join(e['transports'])} | {e['count']} "
                    f"| {e['ja3_md5'] or '-'} | {e['ja4'] or '-'} "
                    f"| {','.join(e['alpn']) or '-'} | {','.join(e['tls_versions']) or '-'} |"
                )
            lines.append("")
            for e in targets:
                if e["ja3_full"]:
                    lines.append(f"- `{e['sni']}` JA3 完整串: `{e['ja3_full']}`")
            lines.append("")
            resumption = ja3_summary.get("session_resumption") or {}
            if resumption:
                lines.append(
                    f"- 握手统计（被动会话）: 完整握手 {resumption.get('full_handshakes', 0)} 次，"
                    f"恢复握手 {resumption.get('resumed_handshakes', 0)} 次"
                    "（恢复握手会带 pre_shared_key，JA3 与首次握手不同）"
                )
                lines.append("")
            if real_api_alpns == {"http/1.1"}:
                lines.append(
                    "> 对齐建议：实测真实客户端对 Cloud Code API **只提供 http/1.1 的 ALPN**"
                    "（连接经代理与否不影响 ClientHello 内容），代理侧保持 HTTP/1.1 即可，"
                    "**不要启用 h2**；TLS 层改用支持指纹伪装的客户端"
                    "（curl_cffi 自定义 JA3 / tls-client）把 ClientHello 对齐到上方 JA3 完整串。"
                )
            else:
                lines.append(
                    "> 对齐建议：代理侧需要产出相同 ALPN 与相近的 ClientHello。"
                    "httpx 默认 Python OpenSSL 指纹与真实客户端差异巨大，"
                    "建议改用支持指纹伪装的客户端（如 curl_cffi 自定义 JA3 / tls-client）。"
                )
            lines.append("")
        else:
            lines += [
                "被动会话里没有 Antigravity 相关域名的 ClientHello，"
                "请确认抓的是 TUN 网卡（或 tlsproxy 已注入 HTTPS_PROXY）且抓包期间客户端有网络活动。",
                "",
            ]
    else:
        lines += [
            "（无被动抓包数据。运行 `Start-Sniff.ps1 passive` 或 `sniff_cli.py tlsproxy` 后重跑本报告。）",
            "",
        ]

    # ---- 2. 真实 HTTP 报文 ----
    lines += ["## 2. 真实 HTTP 报文（离线重定向捕获）", ""]
    real_uas: "OrderedDict[str, int]" = OrderedDict()
    real_header_orders: "OrderedDict[str, List[str]]" = OrderedDict()
    real_http_versions: "OrderedDict[str, int]" = OrderedDict()
    envelopes: "OrderedDict[str, Tuple[List[str], List[str]]]" = OrderedDict()
    if redirect_metas:
        grouped: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
        for meta in redirect_metas:
            grouped.setdefault(meta["endpoint"], []).append(meta)
        for endpoint, metas in grouped.items():
            first = metas[0]
            real_http_versions[first["http_version"]] = (
                real_http_versions.get(first["http_version"], 0) + len(metas)
            )
            lines.append(f"### {endpoint}（{len(metas)} 次，HTTP {first['http_version']}）")
            lines.append("")
            names = _header_names(first["headers"])
            real_header_orders[endpoint] = names
            lines.append("Header 顺序（首个样本，`<redacted>` 为捕获服务器默认脱敏）：")
            lines.append("")
            lines.append("```")
            for pair in first["headers"]:
                name = str(pair[0])
                value = str(pair[1]) if len(pair) > 1 else ""
                if len(value) > 120:
                    value = value[:120] + "…"
                lines.append(f"{name}: {value}")
            lines.append("```")
            lines.append("")
            if first["envelope_keys"]:
                envelopes[endpoint] = (first["envelope_keys"], first["inner_keys"])
                lines.append(f"Body envelope 键序: {' -> '.join(first['envelope_keys'])}")
                if first["inner_keys"]:
                    lines.append(f"内层 request 键序: {' -> '.join(first['inner_keys'])}")
                lines.append("")
        ua_values = [
            _header_value(m["headers"], "User-Agent")
            for m in redirect_metas
        ]
        for ua in ua_values:
            if ua and ua != "<redacted>":
                real_uas[ua] = real_uas.get(ua, 0) + 1
        lines.append("")
    else:
        lines += [
            "（无离线重定向数据。运行 `run_capture.ps1 -Offline -CaptureAll` 后重跑本报告。）",
            "",
        ]

    # ---- 3. agy2api 当前指纹 ----
    lines += ["## 3. agy2api 当前指纹（源码解析）", ""]
    proxy_ua = proxy.get("user_agent", "")
    proxy_headers = proxy.get("header_order", [])
    lines.append(f"- User-Agent: `{proxy_ua or '（未解析到）'}`")
    lines.append(f"- Header 构造顺序: {' -> '.join(proxy_headers) or '（未解析到）'}")
    if proxy.get("envelope_order"):
        lines.append(f"- 顶层信封键序: {' -> '.join(proxy['envelope_order'])}")
    if proxy.get("inner_order"):
        lines.append(f"- 内层 request 键序: {' -> '.join(proxy['inner_order'])}")
    lines.append("")

    # ---- 4. 差异与建议 ----
    lines += ["## 4. 差异与建议", ""]
    if real_uas:
        lines.append("### User-Agent")
        lines.append("")
        for ua, count in real_uas.items():
            real_parts = extract_ua_parts(ua)
            proxy_parts = extract_ua_parts(proxy_ua)
            lines.append(f"真实（×{count}）: `{ua}`")
            lines.append(
                f"- 版本: real={real_parts['version'] or '?'} vs proxy={proxy_parts['version'] or '?'}"
            )
            lines.append(
                f"- CL:    real={real_parts['cl'] or '?'} vs proxy={proxy_parts['cl'] or '?'}"
            )
            if real_parts["raw"] != proxy_ua:
                lines.append("")
                lines.append("建议常量块（人工核对后替换 src/utils.py，不要盲抄 CL——")
                lines.append("CL 与版本必须成对，且以真实抓包为准）：")
                lines.append("")
                lines.append("```python")
                lines.append(f'OFFICIAL_HUB_VERSION = "{real_parts["version"] or "<见真实 UA>"}"')
                lines.append(f'OFFICIAL_HUB_CL = "{real_parts["cl"] or "<见真实 UA>"}"')
                lines.append("```")
                if real_parts["os"] and real_parts["os"] != (proxy.get("constants", {}).get("os") or "windows"):
                    lines.append(
                        f"- 注意 os_type/arch 差异: real={real_parts['os']}/{real_parts['arch']}"
                    )
            lines.append("")
    else:
        lines += ["### User-Agent", "", "（无真实 UA 数据）", ""]

    if real_header_orders:
        lines.append("### Header 顺序与覆盖")
        lines.append("")
        proxy_lower = {h.lower() for h in proxy_headers}
        all_real_names = set()
        for endpoint, names in real_header_orders.items():
            real_pairs = [(n, n.lower()) for n in names if n.lower() not in HOP_HEADERS]
            missing = [n for n, lo in real_pairs if lo not in proxy_lower]
            real_lower = {lo for _n, lo in real_pairs}
            extra = [h for h in proxy_headers if h.lower() not in real_lower]
            all_real_names.update(real_lower)
            lines.append(f"- **{endpoint}**")
            lines.append(f"  - 真实顺序: {' -> '.join(names)}")
            lines.append(f"  - 代理缺失: {', '.join(missing) or '无'}")
            lines.append(f"  - 代理多出: {', '.join(extra) or '无'}")
        lines.append("")
        if "x-goog-api-client" in all_real_names:
            lines.append(
                "> 实测真实客户端携带 `x-goog-api-client`：这是 Google 风控的重点指纹头，"
                "代理必须带上且值要与真实客户端一致。"
            )
        else:
            lines.append(
                "> 实测真实客户端这些请求**未携带** `x-goog-api-client`/"
                "`x-server-timeout` 等额外指纹头（README 旧说法与实测不符）；"
                "代理侧也不要凭空伪造该头，多发一个头同样是差异。"
            )
        lines.append("")
    else:
        lines += ["### Header 顺序与覆盖", "", "（无真实 header 数据）", ""]

    # ---- labels 与 requestId 语义 ----
    label_metas = [m for m in redirect_metas if m.get("labels_keys")]
    lines += ["### labels 与 requestId 语义", ""]
    if label_metas:
        first = label_metas[0]
        lines.append(f"- 真实 labels 键序: {' -> '.join(first['labels_keys'])}")
        for key, value in first["labels_values"].items():
            shown = value if len(value) <= 80 else value[:80] + "…"
            lines.append(f"  - `{key}` = `{shown}`")
        lines.append(
            f"- requestId 拆段: `{first['request_id_segments']}` "
            f"（末段 step={first['request_id_step']}, "
            f"labels.last_step_index={first['last_step_index']}）"
        )
        lines.append(
            f"- cascade_id 与 requestId 第 2 段一致: "
            f"{first['cascade_matches_request_id']}；"
            f"trajectory_id 与第 4 段一致: {first['trajectory_matches_request_id']}"
        )
        proxy_labels = proxy.get("labels_keys", [])
        missing = [k for k in first["labels_keys"] if k not in proxy_labels]
        extra = [k for k in proxy_labels if k not in first["labels_keys"]]
        lines.append(f"- 代理 labels 键序: {' -> '.join(proxy_labels) or '（未解析到）'}")
        lines.append(f"- 代理缺失: {', '.join(missing) or '无'}")
        lines.append(f"- 代理多出: {', '.join(extra) or '无'}")
        enums = {
            m["labels_values"].get("model_enum", "")
            for m in label_metas
            if m.get("labels_values")
        }
        if enums and all(v.startswith("MODEL_PLACEHOLDER") for v in enums if v):
            lines.append(
                "> 实测 `model_enum` 是 `MODEL_PLACEHOLDER_*` 枚举值，**不是模型名**；"
                "代理需维护 model→enum 映射（用 `sniff_cli.py models` 从 "
                "fetchAvailableModels 响应导出）。"
            )
        step_pairs = [
            (m["last_step_index"], m["request_id_step"])
            for m in label_metas
            if m.get("last_step_index", "").isdigit() and m.get("request_id_step") is not None
        ]
        if step_pairs and all(int(step) + 1 == rid for step, rid in step_pairs):
            lines.append(
                "> 实测 `requestId` 末段 = `labels.last_step_index` + 1，两者不是同一个值。"
            )
        lines.append("")
    else:
        lines += ["（无 labels 数据。运行 `run_capture.ps1 -Offline -CaptureAll` 后重跑本报告。）", ""]

    if real_http_versions:
        lines.append("### HTTP 协议版本")
        lines.append("")
        for version, count in real_http_versions.items():
            lines.append(
                f"- 本地捕获链路: HTTP {version}（×{count}）——这是 LS→127.0.0.1 回环的产物，"
                "不代表真实上游协议"
            )
        if real_api_alpns == {"http/1.1"}:
            lines.append(
                "- 实测真实客户端对 API 域名的 ALPN 只提供 http/1.1 → 代理侧保持 HTTP/1.1"
                "（httpx 默认即可），不要启用 h2。"
            )
        elif "h2" in real_api_alpns:
            lines.append("- 实测真实客户端 ALPN 提供 h2 → 建议代理侧启用 HTTP/2。")
        else:
            lines.append("- 真实上游协议版本以被动抓包 ALPN 为准（本次无该数据）。")
        lines.append("")

    # ---- 5. 注意事项 ----
    lines += [
        "## 5. 注意事项",
        "",
        "- 被动抓包纯监听、离线重定向零转发，两者都不会向 Google 暴露异常指纹；",
        "- 转发模式（run_capture.ps1 不带 -Offline）会把请求经 Python TLS 转给 Google，",
        "  只在需要真实回复时使用；",
        "- `CLOUD_CODE_URL` 是非公开环境变量，Antigravity 升级后可能失效，",
        "  以 language_server.log 中 `Overriding CloudCodeServerURL` 为准；",
        "- Go language_server 忽略 SSLKEYLOGFILE，其明文只能通过重定向获得；",
        "- 抓包文件含提示词/令牌等隐私，只存本地 captures/，用完删除。",
        "",
    ]
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    capture_root = DEFAULT_CAPTURE_ROOT
    passive_dir = Path(args.passive_dir) if args.passive_dir else latest_session(
        capture_root, r"^(passive|tlsproxy)-", "ja3_summary.json"
    )
    redirect_dir = Path(args.redirect_dir) if args.redirect_dir else latest_session(
        capture_root, r"^\d{8}-\d{6}", "index.jsonl"
    )

    ja3_summary = load_ja3_summary(passive_dir) if passive_dir else None
    redirect_metas = load_redirect_meta(redirect_dir) if redirect_dir else []
    if ja3_summary is None and not redirect_metas:
        print("[sniff] 没有任何可用的抓包会话（先跑 passive 和 run_capture.ps1 -Offline）。")
        return 2

    proxy = load_proxy_fingerprint(REPO_ROOT)
    sources = {
        "passive": str(passive_dir) if passive_dir else "",
        "redirect": str(redirect_dir) if redirect_dir else "",
    }
    markdown = build_fingerprint_report(ja3_summary, redirect_metas, proxy, sources)

    out_path = Path(args.out) if args.out else (
        capture_root / f"fingerprint_report-{time.strftime('%Y%m%d-%H%M%S')}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    print(f"[sniff] 指纹报告: {out_path}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sniff_cli.py",
        description="Antigravity 真实客户端被动抓包 + 指纹报告工具",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_passive = sub.add_parser("passive", help="在 TUN 网卡上被动抓包")
    p_passive.add_argument("--duration", type=int, default=0, help="N 秒后自动停止（默认回车停止）")
    p_passive.add_argument("--interface", default=None, help="手动指定 dumpcap 接口编号")
    p_passive.add_argument("--launch", action="store_true",
                           help="重启 Antigravity 并注入 SSLKEYLOGFILE（可选，默认不重启）")
    p_passive.add_argument("--rotate", action="store_true", help="启用 50MB×64 环形轮转")
    p_passive.add_argument("--no-install", action="store_true", help="缺 Wireshark 时只给指引，不自动安装")
    p_passive.add_argument("--yes", action="store_true", help="所有交互确认默认 yes")
    p_passive.add_argument("--allow-no-tun", action="store_true", help="TUN 未开启时仍继续（不推荐）")

    p_analyze = sub.add_parser("analyze", help="解析抓包目录生成 TLS 报告")
    p_analyze.add_argument("session_dir", help="passive 会话目录（含 pcapng）")

    p_tls = sub.add_parser(
        "tlsproxy",
        help="无管理员的被动 TLS 指纹抓取（本地 CONNECT 中继，端到端 TLS 不改动）",
    )
    p_tls.add_argument("--listen-port", type=int, default=TLS_PROXY_DEFAULT_PORT,
                       help=f"本地监听端口，默认 {TLS_PROXY_DEFAULT_PORT}")
    p_tls.add_argument("--upstream", default=None,
                       help="上游代理 host:port（默认自动读 Clash mixed-port）")
    p_tls.add_argument("--duration", type=int, default=0, help="N 秒后自动停止（默认回车停止）")
    p_tls.add_argument("--launch", action="store_true",
                       help="重启 Antigravity 并注入 HTTPS_PROXY 指向本代理")
    p_tls.add_argument("--yes", action="store_true", help="所有交互确认默认 yes")

    p_report = sub.add_parser("report", help="合并被动+重定向会话生成指纹报告")
    p_report.add_argument("--passive-dir", default=None, help="passive 会话目录（默认取最新）")
    p_report.add_argument("--redirect-dir", default=None, help="离线重定向会话目录（默认取最新）")
    p_report.add_argument("--out", default=None, help="输出 md 路径")

    p_verify = sub.add_parser(
        "verify",
        help="闭环校验：代理实际发出的 ClientHello + header 构造 vs 实测真实值",
    )
    p_verify.add_argument("--timeout", type=float, default=8.0, help="探测超时秒数")
    p_verify.add_argument("--ja3", default=None, help="覆盖期望 JA3 串（默认取最新抓包/内置常量）")
    p_verify.add_argument("--redirect-dir", default=None,
                          help="离线重定向会话目录（默认取最新）")
    p_verify.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p_verify.add_argument("--envelope", action="store_true",
                          help="按 fidelity_schema.json 校验当前实现产出的信封")
    p_verify.add_argument("--schema", default=None,
                          help="指定 schema 路径（默认仓库根 fidelity_schema.json）")
    p_verify.add_argument("--model", default=None,
                          help="校验信封时使用的模型（默认取 schema 中第一个）")

    p_schema = sub.add_parser("schema", help="从抓包会话导出 fidelity_schema.json")
    p_schema.add_argument("--session-dir", default=None, help="重定向会话目录（默认取最新）")
    p_schema.add_argument("--out", default=None, help="输出路径（默认仓库根 fidelity_schema.json）")

    p_models = sub.add_parser(
        "models", help="从 fetchAvailableModels 响应导出 model→MODEL_PLACEHOLDER 映射",
    )
    p_models.add_argument("--session-dir", default=None, help="重定向会话目录（默认取最新）")
    p_models.add_argument("--out", default=None, help="输出 Python 字典块到文件")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    # 中文输出 + tshark 输出编码兜底
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - 编码配置失败不致命
                pass

    args = build_parser().parse_args(argv)
    if args.command == "passive":
        return cmd_passive(args)
    if args.command == "tlsproxy":
        return cmd_tlsproxy(args)
    if args.command == "analyze":
        return cmd_analyze(args)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "verify":
        return cmd_verify(args)
    if args.command == "models":
        return cmd_models(args)
    if args.command == "schema":
        return cmd_schema(args)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[sniff] interrupted")
        raise SystemExit(130)
