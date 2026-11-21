#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Antigravity Hub 请求捕获服务器。

原理
----
Antigravity 桌面版的 ``language_server.exe`` 支持 ``CLOUD_CODE_URL`` 环境变量。
把该变量指向本机 ``http://127.0.0.1:<port>`` 后，Hub 会把 ``/v1internal:*``
请求发到本进程。本进程记录请求，并可选择原样转发到真实上游。

隐私边界
--------
- 只监听回环地址，拒绝非本机客户端；
- 默认只落盘 ``generateContent`` / ``streamGenerateContent``；
- ``Authorization`` / ``Cookie`` / ``Set-Cookie`` / ``x-goog-api-key`` 等
  默认脱敏，不写入磁盘；
- 抓包数据只写入本地目录，不做任何上传；
- ``--offline``（默认推荐）只让模型生成端点返回合成响应，启动/元数据请求
  （loadCodeAssist / fetchAvailableModels / listExperiments / ...）仍经 Go 转发器
  正常取回，否则客户端会显示"没有可用模型"；
- ``--offline-all`` 所有 /v1internal:* 都不转发（完全不联系 Google，但模型列表可能为空）；
- ``--offline-script tool-call|subagent`` 离线时主动合成一次 functionCall：
  ``tool-call`` 让客户端调用 ``read_file``（只读本会话创建的临时文件），
  ``subagent`` 让客户端调用 ``invoke_subagent``（仅 research、只读、无网络）。
  这样可以抓到"工具调用 → functionResponse → 下一步"以及子 agent 的请求，
  全程不向 Google 发送模型请求。绝不合成 run_command / write_file 等有副作用工具。

这个工具只用于分析自己账号的客户端行为，请不要把抓包目录提交到仓库或发给他人。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

DEFAULT_PORT = 18080
DEFAULT_UPSTREAM = "https://daily-cloudcode-pa.googleapis.com"
# fetchAvailableModels / listExperiments 的响应里带有模型元数据（含
# MODEL_PLACEHOLDER_* 枚举映射），体积可接受，默认一并落盘供 `sniff_cli.py models` 使用。
DEFAULT_CAPTURE_METHODS = (
    "generateContent",
    "streamGenerateContent",
    "loadCodeAssist",
    "fetchUserInfo",
    "fetchAvailableModels",
    "listExperiments",
    "cascadeNuxes",
    "retrieveUserQuotaSummary",
    "writeTrajectoryAcls",
)

# 这些头只用于本地认证/会话，不落盘。
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-goog-api-key",
    "x-goog-api-key-bin",
    "proxy-authorization",
}

# 查询参数里可能出现的密钥/令牌。
SENSITIVE_QUERY_KEYS = {
    "key",
    "api_key",
    "apikey",
    "access_token",
    "token",
    "id_token",
    "refresh_token",
    "code",
    "signature",
}

# 逐跳头，不能转发。
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

# 交给 Go 转发器时去掉的头。
# 注意保留 Accept-Encoding：真实客户端会发它，删掉就与实抓不一致；
# Go 会把压缩响应原样流回，落盘时由 preview_text 解压。
SIDECAR_STRIP_HEADERS = HOP_BY_HOP | {
    "host",
    "content-length",
}

# 回给 Hub 时额外去掉的头。
DOWNSTREAM_STRIP_HEADERS = HOP_BY_HOP | {
    "content-length",  # 用 Connection: close 标记响应结束
    "alt-svc",  # 防止客户端切换到 HTTP/3 绕过本进程
}

REDACTED = "<redacted>"
PRETTY_JSON_LIMIT = 4 * 1024 * 1024
PREVIEW_LIMIT = 20_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def endpoint_name(path: str) -> str:
    """从 ``/v1internal:streamGenerateContent`` 提取 ``streamGenerateContent``。"""
    clean = path.strip("/")
    if not clean:
        return ""
    if ":" in clean:
        return clean.rsplit(":", 1)[1]
    return clean.rsplit("/", 1)[-1]


def safe_stem(path: str) -> str:
    name = endpoint_name(path) or "request"
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
    return name or "request"


def redact_header_pairs(
    pairs: Iterable[Tuple[str, str]], enabled: bool
) -> List[List[str]]:
    saved: List[List[str]] = []
    for name, value in pairs:
        if enabled and name.lower() in SENSITIVE_HEADERS:
            saved.append([name, REDACTED])
        else:
            saved.append([name, value])
    return saved


def redact_target(target: str) -> str:
    """脱敏 URL 查询参数，用于落盘和日志。"""
    parts = urlsplit(target)
    if not parts.query:
        return target

    redacted_pairs: List[str] = []
    for pair in parts.query.split("&"):
        if not pair:
            continue
        key, sep, value = pair.partition("=")
        if key.lower() in SENSITIVE_QUERY_KEYS:
            redacted_pairs.append(f"{key}{sep}{REDACTED}")
        else:
            redacted_pairs.append(pair)

    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(redacted_pairs), parts.fragment)
    )


def decode_for_preview(data: bytes, headers: Sequence[Tuple[str, str]]) -> bytes:
    encoding = ""
    for name, value in headers:
        if name.lower() == "content-encoding":
            encoding = value.lower()
            break

    if "gzip" in encoding:
        try:
            return gzip.decompress(data)
        except OSError:
            return data
    return data


def preview_text(data: bytes, headers: Sequence[Tuple[str, str]]) -> str:
    text = decode_for_preview(data, headers).decode("utf-8", "replace")
    if len(text) > PREVIEW_LIMIT:
        return text[:PREVIEW_LIMIT] + "\n... [preview truncated]\n"
    return text


def build_sidecar_headers(headers: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    """构造发给本机 Go 转发器的请求头（保留真实客户端会发的业务头）。"""
    result: Dict[str, str] = {}
    for name, value in headers:
        if name.lower() in SIDECAR_STRIP_HEADERS:
            continue
        result[name] = value
    return result


# ---------------------------------------------------------------------------
# 离线脚本化响应：主动合成一次 functionCall，驱动客户端执行工具并回传
# functionResponse。仅允许只读、无网络、无写入的两个工具；绝不合成 run_command。
# ---------------------------------------------------------------------------

SCRIPT_TOOL_BY_MODE = {
    "tool-call": "read_file",
    "subagent": "invoke_subagent",
}


def _parse_json_bytes(body: bytes) -> Optional[Dict[str, Any]]:
    if not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _inner_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """兼容两种形态：顶层内层请求，或完整的 v1internal 信封 {"request": {...}}。"""
    inner = request.get("request")
    return inner if isinstance(inner, dict) else request


def declared_tool_names(request: Dict[str, Any]) -> List[str]:
    """列出请求里声明的函数工具名。"""
    names: List[str] = []
    tools = _inner_request(request).get("tools")
    if not isinstance(tools, list):
        return names
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        declarations = tool.get("functionDeclarations") or tool.get("function_declarations")
        if not isinstance(declarations, list):
            continue
        for decl in declarations:
            if isinstance(decl, dict) and decl.get("name"):
                names.append(str(decl["name"]))
    return names


def contents_have_tool_parts(request: Dict[str, Any]) -> bool:
    """contents 里是否已经出现工具调用/工具结果（用于只触发一次、避免递归）。"""
    contents = _inner_request(request).get("contents")
    if not isinstance(contents, list):
        return False
    for content in contents:
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and (
                "functionCall" in part or "functionResponse" in part
            ):
                return True
    return False


def build_scripted_function_call(
    mode: str, request: Dict[str, Any], probe_path: Path
) -> Optional[Dict[str, Any]]:
    """构造一次安全的 functionCall；不满足条件时返回 None。"""
    tool = SCRIPT_TOOL_BY_MODE.get(mode)
    if not tool:
        return None
    if tool not in declared_tool_names(request):
        return None
    if contents_have_tool_parts(request):
        return None
    if tool == "read_file":
        # 只读我们创建的临时文件，内容固定为 ok
        return {"name": "read_file", "args": {"Path": str(probe_path)}}
    # invoke_subagent：只允许只读、无网络的 research agent
    return {
        "name": "invoke_subagent",
        "args": {
            "TypeName": "research",
            "Role": "Capture Probe",
            "Prompt": "只回复 OK，不要调用任何工具。",
        },
    }


def _probe_file_path(server: "CaptureHTTPServer") -> Path:
    path = server.store.session_dir / "_probe_read_file.txt"
    if not path.exists():
        path.write_text("ok\n", encoding="utf-8")
    return path


def _function_call_payload(call: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"functionCall": call}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 0,
            "candidatesTokenCount": 0,
            "totalTokenCount": 0,
        },
    }


# ---------------------------------------------------------------------------
# Go 出站转发器（tools/upstream_tls_proxy）
#
# 转发模式不再由 Python 重新建 TLS（那会暴露 Python 指纹，与真实客户端差异明显），
# 而是把请求交给 Go 转发器：与真实 language_server 同一 TLS 栈，JA3/ALPN 一致。
# 二进制缺失时直接拒绝启动，绝不回退到 Python TLS。
# ---------------------------------------------------------------------------

_SIDECAR_CANDIDATES = (
    Path(__file__).resolve().parent.parent / "upstream_tls_proxy" / "bin" / "upstream-proxy.exe",
    Path(__file__).resolve().parent.parent / "upstream_tls_proxy" / "bin" / "upstream-proxy",
)


def find_sidecar_binary() -> Optional[Path]:
    override = os.environ.get("UPSTREAM_TLS_PROXY_BIN")
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
    for candidate in _SIDECAR_CANDIDATES:
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


def start_sidecar() -> Optional[Tuple[subprocess.Popen, int]]:
    """拉起 Go 转发器；返回 (进程, 端口)，不可用时返回 None。"""
    binary = find_sidecar_binary()
    if binary is None:
        return None
    port = _free_port()
    try:
        proc = subprocess.Popen(
            [str(binary), "--listen", f"127.0.0.1:{port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if not _wait_port(port, 8.0):
        proc.terminate()
        return None
    return proc, port


class CaptureStore:
    """管理单次运行产生的抓包目录。"""

    def __init__(self, root: Path, redact: bool) -> None:
        root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        session_dir = root / stamp
        suffix = 1
        while session_dir.exists():
            suffix += 1
            session_dir = root / f"{stamp}-{suffix}"
        session_dir.mkdir(parents=True)

        self.root = root
        self.session_dir = session_dir
        self.redact = redact
        self._index_path = session_dir / "index.jsonl"
        self._lock = threading.Lock()
        self._sequence = 0

    def next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def append_index(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self._index_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class CaptureHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: Tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        store: CaptureStore,
        upstream_url: str,
        capture_all: bool,
        capture_methods: Sequence[str],
        offline_mode: str = "none",
    ) -> None:
        super().__init__(address, handler)
        self.store = store
        self.upstream_url = upstream_url
        # "none" = 全部经 Go 转发；"model" = 仅模型生成端点离线；"all" = 全部合成
        self.offline_mode = offline_mode
        self.capture_all = capture_all
        self.capture_methods = {name.lower() for name in capture_methods}
        # 转发模式下的 Go 出站转发器（离线模式保持 0/空）
        self.sidecar_port = 0
        self.forward_proxy = ""
        # 离线脚本化：none / tool-call / subagent（每次运行只触发一次，避免递归）
        self.offline_script = "none"
        self.script_fired = False
        self.script_lock = threading.Lock()


class CaptureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "antigravity-capture"
    sys_version = ""

    # 模型生成端点：唯一需要"保持离线"的部分（其余请求转发后客户端才能正常工作）
    MODEL_ENDPOINTS = frozenset({"generatecontent", "streamgeneratecontent"})

    # 不使用父类的 stderr 日志格式，所有输出由本类自己控制。
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    @staticmethod
    def _should_forward(server: "CaptureHTTPServer", name: str) -> bool:
        mode = getattr(server, "offline_mode", "none")
        if mode == "all":
            return False
        if mode == "model":
            return (name or "").lower() not in CaptureHandler.MODEL_ENDPOINTS
        return True

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    # ------------------------------------------------------------------
    # 请求入口
    # ------------------------------------------------------------------
    def _handle(self) -> None:
        started = time.monotonic()
        client_ip = self.client_address[0]
        if client_ip not in ("127.0.0.1", "::1"):
            self._send_json(403, {"error": "only loopback clients are allowed"})
            return

        raw_target = self.path
        path = urlsplit(raw_target).path
        if not self._is_allowed_path(path):
            self._send_json(
                404,
                {
                    "error": (
                        "antigravity-capture only proxies /v1internal:*; "
                        f"blocked {path!r}"
                    )
                },
            )
            return

        try:
            body = self._read_request_body()
        except (ValueError, OSError) as exc:
            self._send_json(400, {"error": f"failed to read request body: {exc}"})
            return

        server: CaptureHTTPServer = self.server  # type: ignore[assignment]
        name = endpoint_name(path)
        should_capture = server.capture_all or name.lower() in server.capture_methods
        sequence = server.store.next_sequence()
        stem = f"{sequence:04d}_{safe_stem(path)}"
        client_port = self.client_address[1]
        framing = self._request_framing()

        if should_capture:
            self._save_request(server, stem, raw_target, body, framing, client_port)

        print(
            f"[capture] #{sequence:04d} {self.command} {redact_target(raw_target)} "
            f"({len(body)} bytes){'' if should_capture else ' [forward only]'}",
            flush=True,
        )

        if not self._should_forward(server, name):
            status, response_headers, response_body = self._offline_response(path, body)
            # 离线模式支持下游 keep-alive：让客户端复用同一 TCP 连接，
            # 以便从 client_port 观察真实客户端的连接复用行为。
            self._send_buffered(
                status,
                response_headers,
                response_body,
                keep_alive=self._client_wants_keep_alive(),
            )
            error = None
        else:
            status, response_headers, response_body, error = self._proxy(body)

        duration_ms = round((time.monotonic() - started) * 1000, 1)

        if should_capture:
            self._save_response(
                server,
                stem,
                status,
                response_headers,
                response_body,
                duration_ms,
                error,
                raw_target,
                body,
            )

        server.store.append_index(
            {
                "seq": sequence,
                "time": utc_now(),
                "method": self.command,
                "path": redact_target(raw_target),
                "endpoint": name,
                "captured": should_capture,
                "offline_mode": getattr(server, "offline_mode", "none"),
                "forwarded": self._should_forward(server, name),
                "status": status,
                "request_bytes": len(body),
                "response_bytes": len(response_body),
                "duration_ms": duration_ms,
                "client_port": client_port,
                "framing": framing,
                "error": error,
            }
        )

    @staticmethod
    def _is_allowed_path(path: str) -> bool:
        return path == "/v1internal" or path.startswith("/v1internal:") or path.startswith(
            "/v1internal/"
        )

    def _request_framing(self) -> str:
        """请求体的分帧方式：chunked / content-length / none（真实客户端对比要点）。"""
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            return "chunked"
        if self.headers.get("Content-Length") is not None:
            return "content-length"
        return "none"

    def _client_wants_keep_alive(self) -> bool:
        connection = (self.headers.get("Connection") or "").lower()
        if "close" in connection:
            return False
        if self.request_version == "HTTP/1.0":
            return "keep-alive" in connection
        return True

    # ------------------------------------------------------------------
    # 请求体 / 落盘
    # ------------------------------------------------------------------
    def _read_request_body(self) -> bytes:
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            chunks: List[bytes] = []
            while True:
                line = self.rfile.readline(65536).strip()
                if not line:
                    break
                size = int(line.split(b";", 1)[0], 16)
                if size == 0:
                    # 吃掉 trailer
                    while True:
                        trailer = self.rfile.readline(65536)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # CRLF
            return b"".join(chunks)

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _save_request(
        self,
        server: CaptureHTTPServer,
        stem: str,
        raw_target: str,
        body: bytes,
        framing: str,
        client_port: int,
    ) -> None:
        session_dir = server.store.session_dir
        (session_dir / f"{stem}.request.bin").write_bytes(body)

        if body and len(body) <= PRETTY_JSON_LIMIT:
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            if parsed is not None:
                (session_dir / f"{stem}.request.json").write_text(
                    json.dumps(parsed, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        saved_headers = redact_header_pairs(
            self.headers.items(), enabled=server.store.redact
        )
        meta = {
            "seq": int(stem.split("_", 1)[0]),
            "received_at": utc_now(),
            "client": f"{self.client_address[0]}:{self.client_address[1]}",
            "client_port": client_port,
            "framing": framing,
            "method": self.command,
            "target": redact_target(raw_target),
            "http_version": self.request_version,
            "headers": saved_headers,
            "body_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        (session_dir / f"{stem}.request.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _save_response(
        self,
        server: CaptureHTTPServer,
        stem: str,
        status: int,
        headers: Sequence[Tuple[str, str]],
        body: bytes,
        duration_ms: float,
        error: Optional[str],
        raw_target: str,
        request_body: bytes,
    ) -> None:
        session_dir = server.store.session_dir
        (session_dir / f"{stem}.response.bin").write_bytes(body)

        # 完整响应另存一份：preview 有长度上限，截断后的 JSON 无法解析
        # （fetchAvailableModels 这类大响应必须完整落盘，models 命令才能导出枚举）。
        text = decode_for_preview(body, headers).decode("utf-8", "replace")
        if text:
            if len(text) > PRETTY_JSON_LIMIT:
                (session_dir / f"{stem}.response.full.txt").write_text(text, encoding="utf-8")
                (session_dir / f"{stem}.response.txt").write_text(
                    text[:PREVIEW_LIMIT]
                    + "\n... [preview truncated; full: "
                    + f"{stem}.response.full.txt]\n",
                    encoding="utf-8",
                )
            else:
                (session_dir / f"{stem}.response.txt").write_text(text, encoding="utf-8")

        response_meta = {
            "seq": int(stem.split("_", 1)[0]),
            "received_at": utc_now(),
            "target": redact_target(raw_target),
            "request_body_bytes": len(request_body),
            "status": status,
            "headers": redact_header_pairs(headers, enabled=server.store.redact),
            "body_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "duration_ms": duration_ms,
            "error": error,
        }
        (session_dir / f"{stem}.response.meta.json").write_text(
            json.dumps(response_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # 转发 / 离线响应
    # ------------------------------------------------------------------
    def _proxy(
        self, body: bytes
    ) -> Tuple[int, List[Tuple[str, str]], bytes, Optional[str]]:
        server: CaptureHTTPServer = self.server  # type: ignore[assignment]
        upstream = urlsplit(server.upstream_url)
        if upstream.scheme not in ("http", "https") or not upstream.hostname:
            payload = json.dumps(
                {"error": f"invalid upstream URL: {server.upstream_url}"},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send_buffered(
                500, [("Content-Type", "application/json; charset=utf-8")], payload
            )
            return 500, [], payload, "invalid upstream URL"

        target = self.path
        if "://" in target:
            parsed_target = urlsplit(target)
            target = parsed_target.path
            if parsed_target.query:
                target += "?" + parsed_target.query
        upstream_target = urlunsplit(
            (upstream.scheme, upstream.netloc, upstream.path.rstrip("/") + target, "", "")
        )

        sidecar_port = int(getattr(server, "sidecar_port", 0) or 0)
        if not sidecar_port:
            payload = json.dumps(
                {"error": "forward mode requires tools/upstream_tls_proxy (Go)"},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send_buffered(
                503, [("Content-Type", "application/json; charset=utf-8")], payload
            )
            return 503, [], payload, "go sidecar unavailable"

        headers = build_sidecar_headers(self.headers.items())
        headers["X-Upstream-URL"] = upstream_target
        forward_proxy = str(getattr(server, "forward_proxy", "") or "")
        if forward_proxy:
            headers["X-Upstream-Proxy"] = forward_proxy

        connection: Optional[HTTPConnection] = None
        headers_sent = False
        captured = bytearray()

        try:
            # 只连本机回环（明文）；真正的 TLS 由 Go 转发器以与真实客户端
            # 相同的 crypto/tls 栈完成，避免暴露 Python 指纹。
            connection = HTTPConnection("127.0.0.1", sidecar_port, timeout=600)
            connection.request(self.command, "/", body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            response_headers = response.getheaders()

            self.close_connection = True
            self.send_response_only(status, response.reason)
            for name, value in response_headers:
                if name.lower() in DOWNSTREAM_STRIP_HEADERS:
                    continue
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True

            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                captured.extend(chunk)
                if self.command != "HEAD":
                    self.wfile.write(chunk)
                    self.wfile.flush()

            return status, response_headers, bytes(captured), None

        except (BrokenPipeError, ConnectionResetError) as exc:
            return 499, [], bytes(captured), f"client disconnected: {exc}"
        except Exception as exc:  # noqa: BLE001 - 需要把上游错误反馈给客户端
            error_text = f"{type(exc).__name__}: {exc}"
            if not headers_sent:
                payload = json.dumps(
                    {"error": f"upstream request failed: {error_text}"},
                    ensure_ascii=False,
                ).encode("utf-8")
                self._send_buffered(
                    502,
                    [("Content-Type", "application/json; charset=utf-8")],
                    payload,
                )
                return 502, [], payload, error_text
            return 502, [], bytes(captured), error_text
        finally:
            if connection is not None:
                connection.close()

    def _maybe_scripted_call(self, body: bytes) -> Optional[Dict[str, Any]]:
        """按 --offline-script 合成一次 functionCall（每次运行最多一次）。"""
        server: CaptureHTTPServer = self.server  # type: ignore[assignment]
        mode = getattr(server, "offline_script", "none")
        if mode not in SCRIPT_TOOL_BY_MODE:
            return None
        with server.script_lock:
            if server.script_fired:
                return None
            request = _parse_json_bytes(body)
            if request is None:
                return None
            call = build_scripted_function_call(mode, request, _probe_file_path(server))
            if call is None:
                return None
            server.script_fired = True
            print(
                f"[capture] offline script: 合成 {call['name']} 调用（单次）",
                flush=True,
            )
            return call

    def _offline_response(
        self, path: str, body: bytes = b""
    ) -> Tuple[int, List[Tuple[str, str]], bytes]:
        """离线模式：返回最小可用响应，保证请求能被观察到。"""
        name = endpoint_name(path).lower()
        marker = [("X-Antigravity-Capture", "offline")]

        if name in self.MODEL_ENDPOINTS:
            scripted_call = self._maybe_scripted_call(body)
            if scripted_call is not None:
                payload = _function_call_payload(scripted_call)
                envelope = {
                    "response": payload,
                    "traceId": "antigravity-capture-offline",
                    "metadata": {},
                }
                encoded = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
                if name == "streamgeneratecontent":
                    return (
                        200,
                        [
                            ("Content-Type", "text/event-stream; charset=utf-8"),
                            ("Cache-Control", "no-cache"),
                            *marker,
                        ],
                        b"data: " + encoded + b"\n\n",
                    )
                return (
                    200,
                    [("Content-Type", "application/json; charset=utf-8"), *marker],
                    encoded,
                )

        if name == "streamgeneratecontent":
            payload = {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "text": (
                                        "[antigravity-capture] 离线模式：请求已在本地记录，"
                                        "未转发到 Google。"
                                    )
                                }
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 0,
                    "candidatesTokenCount": 0,
                    "totalTokenCount": 0,
                },
            }
            # 与实抓 SSE 帧结构一致：{"response": {...}, "traceId": ..., "metadata": {}}。
            # 缺少这层包装时新版客户端会解析失败并反复重试（抓包里会出现数百次 stream 请求）。
            envelope = {
                "response": payload,
                "traceId": "antigravity-capture-offline",
                "metadata": {},
            }
            body = (
                b"data: "
                + json.dumps(envelope, ensure_ascii=False).encode("utf-8")
                + b"\n\n"
            )
            return (
                200,
                [
                    ("Content-Type", "text/event-stream; charset=utf-8"),
                    ("Cache-Control", "no-cache"),
                    *marker,
                ],
                body,
            )

        if name == "generatecontent":
            payload = {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "text": (
                                        "[antigravity-capture] 离线模式：请求已在本地记录，"
                                        "未转发到 Google。"
                                    )
                                }
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 0,
                    "candidatesTokenCount": 0,
                    "totalTokenCount": 0,
                },
            }
            envelope = {
                "response": payload,
                "traceId": "antigravity-capture-offline",
                "metadata": {},
            }
            return (
                200,
                [("Content-Type", "application/json; charset=utf-8"), *marker],
                json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
            )

        if name == "fetchavailablemodels":
            # 离线模式没有任何真实上游数据，模型枚举值是**合成的**（只保留已实测确认的
            # gemini-4-hydrogen → M526 一组），因此顶层标注 synthetic，供
            # sniff_cli.py models 跳过，避免把合成值当成真实映射。
            model_ids = [
                "gemini-4-hydrogen-medium",
                "gemini-3.1-pro-preview",
                "gemini-3-flash-preview",
                "gemini-2.5-pro",
                "gemini-2.5-flash",
                "claude-sonnet-4-6",
                "claude-opus-4-6",
            ]
            known_enums = {"gemini-4-hydrogen-medium": "MODEL_PLACEHOLDER_M526"}
            payload = {
                "synthetic": True,
                "models": {
                    model_id: {
                        "displayName": model_id,
                        **(
                            {"modelEnum": known_enums[model_id]}
                            if model_id in known_enums
                            else {}
                        ),
                    }
                    for model_id in model_ids
                },
                "defaultAgentModelId": model_ids[0],
            }
            return (
                200,
                [("Content-Type", "application/json; charset=utf-8"), *marker],
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )

        if name == "loadcodeassist":
            payload = {
                "currentTier": {"id": "free-tier", "name": "Free"},
                "cloudaicompanionProject": "antigravity-capture-offline",
            }
            return (
                200,
                [("Content-Type", "application/json; charset=utf-8"), *marker],
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )

        return (
            200,
            [("Content-Type", "application/json; charset=utf-8"), *marker],
            b"{}",
        )

    # ------------------------------------------------------------------
    # 响应写出
    # ------------------------------------------------------------------
    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_buffered(
            status, [("Content-Type", "application/json; charset=utf-8")], body
        )

    def _send_buffered(
        self,
        status: int,
        headers: Sequence[Tuple[str, str]],
        body: bytes,
        keep_alive: bool = False,
    ) -> None:
        self.close_connection = not keep_alive
        self.send_response_only(status)
        for name, value in headers:
            if name.lower() in DOWNSTREAM_STRIP_HEADERS:
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "keep-alive" if keep_alive else "close")
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "捕获 Antigravity 桌面版 Hub 发往 /v1internal:* 的请求。"
            "只监听 127.0.0.1，数据只写本地。"
        )
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"监听端口，默认 {DEFAULT_PORT}"
    )
    parser.add_argument(
        "--upstream-url",
        default=DEFAULT_UPSTREAM,
        help=f"真实上游地址，默认 {DEFAULT_UPSTREAM}",
    )
    parser.add_argument(
        "--offline",
        dest="offline_mode",
        action="store_const",
        const="model",
        help=(
            "只让 generateContent/streamGenerateContent 返回合成响应；"
            "启动/元数据请求仍经 Go 转发器正常取回（推荐，否则客户端会没有可用模型）"
        ),
    )
    parser.add_argument(
        "--offline-all",
        "--no-forward",
        dest="offline_mode",
        action="store_const",
        const="all",
        help="所有 /v1internal:* 都不转发（完全不联系 Google；可能显示没有可用模型）",
    )
    parser.set_defaults(offline_mode="none")
    parser.add_argument(
        "--offline-script",
        choices=("none", "tool-call", "subagent"),
        default="none",
        help=(
            "离线时主动合成一次 functionCall，抓取工具调用（read_file）或"
            "子 agent（invoke_subagent/research）的后续报文；每次运行只触发一次"
        ),
    )
    parser.add_argument(
        "--forward-proxy",
        default=os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY") or "",
        help="转发模式下的上游代理（默认取 HTTPS_PROXY/ALL_PROXY；由 Go 转发器使用）",
    )
    parser.add_argument(
        "--capture-all",
        action="store_true",
        help="保存所有 /v1internal:* 请求（默认只保存模型生成请求）",
    )
    parser.add_argument(
        "--capture-methods",
        default=",".join(DEFAULT_CAPTURE_METHODS),
        help=(
            "要保存的 endpoint 名称，逗号分隔；"
            f"默认 {','.join(DEFAULT_CAPTURE_METHODS)}"
        ),
    )
    parser.add_argument(
        "--capture-auth",
        action="store_true",
        help="不脱敏 Authorization/Cookie/x-goog-api-key（危险，仅在需要重放时使用）",
    )
    parser.add_argument(
        "--log-dir",
        default=str(Path(__file__).resolve().parent / "captures"),
        help="抓包输出根目录",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    capture_methods = [
        item.strip() for item in args.capture_methods.split(",") if item.strip()
    ]

    store = CaptureStore(Path(args.log_dir), redact=not args.capture_auth)
    server = CaptureHTTPServer(
        ("127.0.0.1", args.port),
        CaptureHandler,
        store=store,
        upstream_url=args.upstream_url,
        capture_all=args.capture_all,
        capture_methods=capture_methods,
        offline_mode=args.offline_mode,
    )
    server.offline_script = args.offline_script
    if args.offline_script != "none" and args.offline_mode == "none":
        print(
            "[capture] 警告: --offline-script 只在离线响应时生效（请配合 -Offline 使用）",
            flush=True,
        )

    sidecar_proc: Optional[subprocess.Popen] = None
    if args.offline_mode != "all":
        # 任何转发都必须经 Go 转发器出站；缺失就拒绝启动，绝不退回 Python TLS。
        if find_sidecar_binary() is None:
            print(
                "[capture] 拒绝启动：需要 tools/upstream_tls_proxy（Go >= 1.26，"
                "build.ps1 / build.sh）。\n"
                "          -Offline 仍需要它转发启动/元数据请求（否则客户端没有可用模型）；\n"
                "          若坚持完全离线，请改用 --offline-all。",
                flush=True,
            )
            return 2
        started = start_sidecar()
        if started is None:
            print("[capture] 拒绝启动：Go 转发器启动失败。", flush=True)
            return 2
        sidecar_proc, sidecar_port = started
        server.sidecar_port = sidecar_port
        server.forward_proxy = args.forward_proxy

    print(f"[capture] session dir : {store.session_dir}", flush=True)
    print(f"[capture] listening   : http://127.0.0.1:{args.port}", flush=True)
    mode_note = {
        "none": "FORWARD via Go TLS",
        "model": "OFFLINE(model only) + forward metadata via Go TLS",
        "all": "OFFLINE ALL (no forwarding)",
    }[args.offline_mode]
    print(
        f"[capture] upstream    : {args.upstream_url} ({mode_note})",
        flush=True,
    )
    if sidecar_proc is not None:
        print(
            f"[capture] egress      : Go 转发器 127.0.0.1:{server.sidecar_port}"
            f"{' via ' + args.forward_proxy if args.forward_proxy else ' (direct)'}",
            flush=True,
        )
    if args.offline_script != "none":
        print(
            f"[capture] offline script: {args.offline_script} "
            f"（合成 {SCRIPT_TOOL_BY_MODE[args.offline_script]}，仅一次）",
            flush=True,
        )
    print(
        f"[capture] save methods: "
        f"{'ALL /v1internal:*' if args.capture_all else ','.join(capture_methods)}",
        flush=True,
    )
    print(
        f"[capture] redaction   : "
        f"{'OFF (auth headers will be saved!)' if args.capture_auth else 'ON'}",
        flush=True,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[capture] interrupted", flush=True)
    finally:
        server.server_close()
        if sidecar_proc is not None:
            sidecar_proc.terminate()
            try:
                sidecar_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sidecar_proc.kill()
        print(f"[capture] saved to   : {store.session_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
