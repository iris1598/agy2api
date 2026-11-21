#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TLS ClientHello / ServerHello 解析与 JA3 指纹计算（纯函数，无 IO）。

用于 ``sniff_cli.py tlsproxy``：在本地 CONNECT 隧道里读出客户端发出的
明文 ClientHello 字节（TLS 握手在客户端与 Google 之间端到端进行，本地
TCP 中继看到的只是原始 TLS 记录），提取 SNI / ALPN / 密码套件并计算 JA3。

JA3 规范（Salesforce ja3）：五段十进制串
``TLSVersion,Ciphers,Extensions,SupportedGroups,ECPointFormats``
（段内用 ``-`` 连接），对整串取 MD5。Go 客户端不使用 GREASE，因此这里
不做 GREASE 过滤，与 Wireshark 对非 GREASE ClientHello 的结果一致。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

NEED_MORE = "need_more"
BAD = "bad"
OK = "ok"

# 单个 ClientHello 解析所需的字节上限（超过即放弃）
MAX_SNIFF_BYTES = 65536

# ---------------------------------------------------------------------------
# 真实 Antigravity language_server（Go crypto/tls）对本机实测的 ClientHello 指纹
# 2026-11-20，language_server 2.33.0，tlsproxy 实抓（见 README 第 0 节）。
# 全部 API 域名（daily-cloudcode-pa / oauth2 / www / aicode）JA3 完全一致，
# ALPN 只提供 http/1.1（不开 h2）。代理侧对齐时以此为唯一基准。
# ---------------------------------------------------------------------------
REAL_ANTIGRAVITY_JA3_FULL = (
    "771,"
    "49195-49199-49196-49200-52393-52392-49161-49171-49162-49172-4865-4866-4867,"
    "0-11-65281-23-18-5-10-13-50-16-43-51,"
    "4588-4587-4589-29-23-24-25,"
    "0"
)
REAL_ANTIGRAVITY_JA3_MD5 = "03117a8ed39ef02427ebbc39f121275c"
REAL_ANTIGRAVITY_ALPN = ["http/1.1"]


def parse_ja3_string(ja3: str) -> Dict[str, Any]:
    """把 JA3 五段串拆成可比较的结构。空段返回空列表。"""

    def _ints(segment: str) -> List[int]:
        return [int(v) for v in segment.split("-") if v.strip()]

    parts = (ja3 or "").split(",")
    while len(parts) < 5:
        parts.append("")
    return {
        "legacy_version": int(parts[0]) if parts[0].strip() else 0,
        "ciphers": _ints(parts[1]),
        "extensions": _ints(parts[2]),
        "groups": _ints(parts[3]),
        "formats": _ints(parts[4]),
    }


def diff_clienthello(
    info: Optional[Dict[str, Any]],
    expected_ja3: str = REAL_ANTIGRAVITY_JA3_FULL,
    expected_alpn: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """将解析出的 ClientHello 与期望 JA3/ALPN 逐段比对（纯函数，无 IO）。

    返回各段的 matched 布尔值与缺失/多出的具体 ID，供 verify 命令与报告复用。
    alpn 期望默认 ``REAL_ANTIGRAVITY_ALPN``（仅 http/1.1）。
    """
    expected = parse_ja3_string(expected_ja3)
    expected_alpn = REAL_ANTIGRAVITY_ALPN if expected_alpn is None else expected_alpn

    result: Dict[str, Any] = {
        "expected_ja3": expected_ja3,
        "expected_md5": hashlib.md5(expected_ja3.encode("ascii")).hexdigest(),
        "expected_alpn": list(expected_alpn),
        "ok": False,
    }
    if not info:
        result["error"] = "no ClientHello captured"
        return result

    actual_alpn = list(info.get("alpn") or [])
    segments = {}
    for key in ("ciphers", "extensions", "groups", "formats"):
        actual = [int(v) for v in info.get(key) or []]
        want = expected[key]
        segments[key] = {
            "match": actual == want,
            "missing": [v for v in want if v not in actual],
            "extra": [v for v in actual if v not in want],
        }
    result["segments"] = segments
    result["md5_match"] = info.get("ja3_md5") == result["expected_md5"]
    result["ja3_full"] = info.get("ja3_full", "")
    result["ja3_md5"] = info.get("ja3_md5", "")
    result["alpn"] = actual_alpn
    result["alpn_match"] = actual_alpn == list(expected_alpn)
    for key in ("ciphers", "extensions", "groups", "formats"):
        result[f"{key}_match"] = segments[key]["match"]
    result["ok"] = (
        result["md5_match"]
        and result["alpn_match"]
        and all(segments[k]["match"] for k in segments)
    )
    return result


def _u16(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 2], "big")


def _u24(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 3], "big")


def _parse_extensions(data: bytes, start: int, end: int) -> List[Tuple[int, bytes]]:
    exts: List[Tuple[int, bytes]] = []
    p = start
    while p + 4 <= end:
        etype = _u16(data, p)
        elen = _u16(data, p + 2)
        if p + 4 + elen > end:
            return exts  # 截断，尽力而为
        exts.append((etype, data[p + 4:p + 4 + elen]))
        p += 4 + elen
    return exts


def _sni_from_ext(data: bytes) -> str:
    # server_name_list: u16 总长；entry: type(1)=0 host_name, u16 len, name
    if len(data) < 5:
        return ""
    p = 2  # 跳过 list 长度
    if data[p] != 0:
        return ""
    nlen = _u16(data, p + 1)
    try:
        return data[p + 3:p + 3 + nlen].decode("idna")
    except Exception:  # noqa: BLE001 - 非法编码按原样降级
        return data[p + 3:p + 3 + nlen].decode("latin-1", "replace")


def _alpn_from_ext(data: bytes) -> List[str]:
    # protocol_name_list: u16 总长；entries: u8 len + name
    names: List[str] = []
    if len(data) < 2:
        return names
    p = 2
    end = len(data)
    while p + 1 <= end:
        nlen = data[p]
        if p + 1 + nlen > end:
            break
        names.append(data[p + 1:p + 1 + nlen].decode("latin-1"))
        p += 1 + nlen
    return names


def _u16_list(data: bytes, skip_prefix: int) -> List[int]:
    values: List[int] = []
    p = skip_prefix
    while p + 2 <= len(data):
        values.append(_u16(data, p))
        p += 2
    return values


def _tls_version_name(u: int) -> str:
    return {0x0304: "TLS 1.3", 0x0303: "TLS 1.2", 0x0302: "TLS 1.1", 0x0301: "TLS 1.0"}.get(
        u, f"0x{u:04x}"
    )


def parse_client_hello(data: bytes) -> Tuple[str, Optional[Dict[str, Any]]]:
    """解析 ClientHello。返回 (status, info)；数据不足返回 need_more。"""
    total = len(data)
    if total < 5:
        return NEED_MORE, None
    if data[0] != 0x16:  # 非 TLS handshake record
        return BAD, None
    rec_len = _u16(data, 3)
    if total < 5 + rec_len:
        return NEED_MORE, None
    body = data[5:5 + rec_len]
    if len(body) < 4 or body[0] != 0x01:
        return BAD, None
    hs_len = _u24(body, 1)
    if len(body) < 4 + hs_len:
        return NEED_MORE, None  # 握手跨记录（极少见），调用方继续累积
    ch = body[4:4 + hs_len]

    if len(ch) < 35:
        return BAD, None
    legacy_version = _u16(ch, 0)
    p = 2 + 32  # version + random
    sid_len = ch[p]
    p += 1 + sid_len
    if p + 2 > len(ch):
        return BAD, None
    cs_len = _u16(ch, p)
    p += 2
    if p + cs_len > len(ch):
        return BAD, None
    ciphers = [int.from_bytes(ch[p + i:p + i + 2], "big") for i in range(0, cs_len, 2)]
    p += cs_len
    if p + 1 > len(ch):
        return BAD, None
    comp_len = ch[p]
    p += 1 + comp_len

    ext_types: List[int] = []
    sni = ""
    alpn: List[str] = []
    groups: List[int] = []
    formats: List[int] = []
    supported_versions: List[int] = []
    if p + 2 <= len(ch):
        ext_len = _u16(ch, p)
        ext_end = min(p + 2 + ext_len, len(ch))
        for etype, edata in _parse_extensions(ch, p + 2, ext_end):
            ext_types.append(etype)
            if etype == 0 and not sni:
                sni = _sni_from_ext(edata)
            elif etype == 10:
                groups = _u16_list(edata, 2)
            elif etype == 11:
                formats = list(edata[1:]) if len(edata) >= 1 else []
            elif etype == 16:
                alpn = _alpn_from_ext(edata)
            elif etype == 43:
                supported_versions = _u16_list(edata, 1)

    info: Dict[str, Any] = {
        "legacy_version": legacy_version,
        "ciphers": ciphers,
        "extensions": ext_types,
        "groups": groups,
        "formats": formats,
        "sni": sni,
        "alpn": alpn,
        "tls_versions": [_tls_version_name(v) for v in supported_versions] or [
            _tls_version_name(legacy_version)
        ],
    }
    ja3_string, ja3_md5 = compute_ja3(info)
    info["ja3_full"] = ja3_string
    info["ja3_md5"] = ja3_md5
    return OK, info


def compute_ja3(info: Dict[str, Any]) -> Tuple[str, str]:
    """按 JA3 规范计算指纹串与 MD5。"""
    ja3 = ",".join(
        [
            str(info["legacy_version"]),
            "-".join(str(c) for c in info["ciphers"]),
            "-".join(str(e) for e in info["extensions"]),
            "-".join(str(g) for g in info["groups"]),
            "-".join(str(f) for f in info["formats"]),
        ]
    )
    return ja3, hashlib.md5(ja3.encode("ascii")).hexdigest()


def parse_server_hello(data: bytes) -> Tuple[str, Optional[Dict[str, Any]]]:
    """解析 ServerHello（取 ALPN 协商结果 / 版本 / 密码套件）。"""
    total = len(data)
    if total < 6:
        return NEED_MORE, None
    if data[0] != 0x16:
        return BAD, None
    rec_len = _u16(data, 3)
    if total < 5 + rec_len:
        return NEED_MORE, None
    body = data[5:5 + rec_len]
    if len(body) < 4 or body[0] != 0x02:
        return BAD, None
    hs_len = _u24(body, 1)
    if len(body) < 4 + hs_len:
        return NEED_MORE, None
    sh = body[4:4 + hs_len]
    if len(sh) < 39:
        return BAD, None

    version = _u16(sh, 0)
    p = 2 + 32
    sid_len = sh[p]
    p += 1 + sid_len
    if p + 3 > len(sh):
        return BAD, None
    cipher = _u16(sh, p)
    p += 2 + 1  # cipher + compression

    alpn: List[str] = []
    versions: List[int] = []
    if p + 2 <= len(sh):
        ext_len = _u16(sh, p)
        ext_end = min(p + 2 + ext_len, len(sh))
        for etype, edata in _parse_extensions(sh, p + 2, ext_end):
            if etype == 16:
                alpn = _alpn_from_ext(edata)
            elif etype == 43 and len(edata) >= 1:
                versions = _u16_list(edata, 1)

    info: Dict[str, Any] = {
        "version": _tls_version_name(versions[0] if versions else version),
        "cipher": cipher,
        "alpn": alpn,
    }
    return OK, info
