"""
Antigravity API Client - Handles communication with Google's Antigravity API
处理与 Google Antigravity API 的通信
"""

import asyncio
import hashlib
import json
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Dict, List, NamedTuple, Optional, Callable, Set, Tuple

from fastapi import Response
from config import (
    get_antigravity_api_url,
    get_auto_ban_error_codes,
    get_request_log_enabled,
    get_request_log_error_codes,
    get_strict_fidelity_enabled,
    get_usage_log_enabled,
)
from log import log

from src.credential_manager import (
    add_credential,
    get_valid_credential,
    refresh_token,
    should_refresh_token,
    update_credential_state,
)
from src.httpx_client import stream_post_async, post_async
from src.request_pacer import request_pacer
from src import thought_signature_cache
from src.credential_limiter import credential_limiter
from src.fidelity import FidelityError, validate_client_request
from src.model_enum import merge_model_enums, parse_model_enums, resolve_model_enum
from src.models import Model, model_to_dict
from src.storage.database import get_database
from src.utils import ANTIGRAVITY_USER_AGENT
from src.session_cache import derive_conversation_anchor, derive_session_key
from src.usage_log import (
    extract_usage_tokens,
    new_request_id,
    record_usage_log,
)
from src.request_body_log import record_request_body

# 导入共同的基础功能
from src.api.utils import (
    handle_error_with_retry,
    get_retry_config,
    record_api_call_success,
    record_api_call_error,
    parse_and_log_cooldown,
)

# ==================== 全局凭证管理器 ====================


class UpstreamSessionIds(NamedTuple):
    """同一会话确定性派生的三个上游标识。"""

    session_id: str
    trajectory_id: str
    cascade_id: str


def _derive_upstream_session_ids(
    gemini_request: Dict[str, Any], model: str, account_salt: str = ""
) -> UpstreamSessionIds:
    """
    从联合锚点摘要确定性派生上游 (sessionId, trajectory_id, cascade_id)。

    联合锚点 = SHA256(model + systemInstruction 文本 + 前两条消息特征)，
    与 derive_session_key 同口径，保证:
    - 同一会话跨轮次/跨请求三者恒定（KV 缓存路由亲和）；
    - 共用相同 systemInstruction 的不同会话（多 Agent / 主子会话）不会撞车；
    - 摘要前 8 字节 → sessionId（沿用既有 -{int} 形态）；
    - 摘要 8~24 字节 → 16 字节确定性 UUID 形态 trajectory_id；
    - ``sha256("cascade:" + 摘要)[:16]`` → cascade_id（实测 cascade_id 与
      root_cascade_id 同值，且等于 requestId 第 2 段；与 trajectory_id 不同源）。

    无锚点（如空对话）时返回全空，由调用方兜底。
    """
    digest = derive_conversation_anchor(model, gemini_request)
    if digest is None:
        return UpstreamSessionIds("", "", "")
    if account_salt:
        # 账号盐：避免不同凭证对同一段内容派生出相同的 session/trajectory，
        # 否则上游会看到"两个账号在跑同一条轨迹"这种强关联信号。
        digest = hashlib.sha256(account_salt.encode("utf-8") + b"|" + digest).digest()
    session_int = int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
    session_id = f"-{session_int}"
    trajectory_id = str(uuid.UUID(bytes=digest[8:24]))
    cascade_id = str(uuid.UUID(bytes=hashlib.sha256(b"cascade:" + digest).digest()[:16]))
    return UpstreamSessionIds(session_id, trajectory_id, cascade_id)


def _generate_request_id(cascade_id: str, step: int, trajectory_id: str) -> str:
    """构造官方形态的 requestId：`agent/{cascadeId}/{unixMs}/{trajectoryId}/{step}`。

    实抓样本（2 份报文交叉核对）：
        agent/afb38f6a-74b6-459f-8edd-a75d8d00bc19/1795150502265/3ca3a2de-ad5c-4bbd-94f2-0a6cdca924c0/8

    - 第 2 段就是 labels.cascade_id / root_cascade_id 的原值（实测一致），
      不是 sessionId 的哈希；
    - unixMs 段每次请求都不同 → 天然的幂等隔离，避免重试命中上一次的 429 / 旧缓存；
    - trajectoryId 段同一会话恒定（确定性派生自会话锚点），上游据此关联同一轨迹的
      连续 step，保证隐式 KV 缓存的路由亲和；不同会话天然隔离；
    - 末段 step = last_step_index + 1（实测 7→8、9→10）。
    """
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    conversation = cascade_id or str(uuid.uuid4())
    if not trajectory_id:
        trajectory_id = str(uuid.uuid4())
    return f"agent/{conversation}/{ts_ms}/{trajectory_id}/{step}"


# 轨迹请求计数（labels.request_id 后缀）：实测同一 trajectory 内逐请求自增，
# 与 step 无关。进程内维护，重启归零可接受；LRU 限制避免长期运行时无限增长。
_TRAJECTORY_REQUEST_SEQ: "OrderedDict[str, int]" = OrderedDict()
_TRAJECTORY_REQUEST_SEQ_LOCK = threading.Lock()
_MAX_TRACKED_TRAJECTORIES = 2048


def _reset_trajectory_request_seq() -> None:
    """清空轨迹请求计数（仅供测试）。"""
    with _TRAJECTORY_REQUEST_SEQ_LOCK:
        _TRAJECTORY_REQUEST_SEQ.clear()


def _next_trajectory_request_seq(trajectory_id: str) -> int:
    """同一 trajectory 的 labels.request_id 自增计数（实抓从 0 开始）。"""
    with _TRAJECTORY_REQUEST_SEQ_LOCK:
        seq = _TRAJECTORY_REQUEST_SEQ.get(trajectory_id, -1) + 1
        _TRAJECTORY_REQUEST_SEQ[trajectory_id] = seq
        _TRAJECTORY_REQUEST_SEQ.move_to_end(trajectory_id)
        while len(_TRAJECTORY_REQUEST_SEQ) > _MAX_TRACKED_TRAJECTORIES:
            _TRAJECTORY_REQUEST_SEQ.popitem(last=False)
        return seq


def _build_labels(
    model: str,
    trajectory_id: str,
    step: int,
    *,
    cascade_id: str,
    request_seq: int,
) -> Dict[str, str]:
    """构建官方上下文标签元数据（键序按官方字母序排布）。

    实抓样本（2026-11-21，language_server 2.34.0，streamGenerateContent）：
    {
      "cascade_id": "5c6b5422-381c-406b-ab6b-f22fad9f7bff",
      "last_step_index": "8",
      "model_enum": "MODEL_PLACEHOLDER_M526",
      "request_id": "3ca3a2de-ad5c-4bbd-94f2-0a6cdca924c0-2",
      "root_cascade_id": "afb38f6a-74b6-459f-8edd-a75d8d00bc19",
      "trajectory_id": "3ca3a2de-ad5c-4bbd-94f2-0a6cdca924c0",
      "used_claude": "false",
      "used_claude_conservative": "false",
      "used_non_gemini_model": "false"
    }

    - ``last_step_index`` = 当前步索引 - 1（实测 requestId 末段 = 它 + 1）；
    - ``model_enum`` 是内部枚举值而非模型名（见 src/model_enum.py）；
    - ``request_id`` 后缀是 trajectory 内逐请求自增计数，不是 step。
    """
    model_lower = model.lower() if model else ""
    used_claude = "claude" in model_lower
    # used_non_gemini_model：Claude / gpt-oss 等非 Gemini 家族模型为 true
    used_non_gemini = used_claude or "gpt-oss" in model_lower or "grok" in model_lower
    root_cascade_id = cascade_id
    return {
        "cascade_id": cascade_id,
        "last_step_index": str(max(step - 1, 0)),
        "model_enum": resolve_model_enum(model),
        "request_id": f"{trajectory_id}-{request_seq}",
        "root_cascade_id": root_cascade_id,
        "trajectory_id": trajectory_id,
        "used_claude": str(used_claude).lower(),
        "used_claude_conservative": str(used_claude).lower(),
        "used_non_gemini_model": str(used_non_gemini).lower(),
    }


def _has_function_tool(tools: Any) -> bool:
    """判断 tools 列表中是否存在函数类工具声明。"""
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if isinstance(tool, dict) and (
            tool.get("functionDeclarations") or tool.get("function_declarations")
        ):
            return True
    return False


def _contents_have_tool_interactions(contents: Any) -> bool:
    """判断 contents 历史中是否存在工具调用上下文。"""
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
                "functionCall" in part
                or "function_call" in part
                or "functionResponse" in part
                or "function_response" in part
            ):
                return True
    return False


def _should_forward_antigravity_header(header_name: str) -> bool:
    """判断是否向 Antigravity 上游透传该请求头。

    对齐官方 Antigravity Hub 出站请求头（100% 同构）：
    - 严格过滤 x-session-id / x-client-name / x-client-version / x-machine-id /
      x-vscode-sessionid 等客户端或前端内部识别标头，规避 WAF 异常标记；
    - 仅放行官方链路使用的追踪类标头。
    """
    normalized = header_name.strip().lower()
    if not normalized:
        return False
    if normalized.startswith("x-b3-"):
        return True
    return normalized in {
        "traceparent",
        "tracestate",
        "x-cloud-trace-context",
        "x-goog-request-params",
        "x-goog-user-project",
        "x-request-id",
    }


def _sanitize_antigravity_headers(extra_headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    if not extra_headers:
        return {}
    sanitized: Dict[str, str] = {}
    for key, value in extra_headers.items():
        if _should_forward_antigravity_header(key):
            sanitized[key] = value
    return sanitized


async def wrap_cli_request(
    gemini_request: Dict[str, Any],
    model: str,
    project_id: str,
    account_salt: str = "",
) -> Tuple[Dict[str, Any], str]:
    """
    将 Gemini 格式请求包装成 Antigravity CLI 格式（对齐官方 v1internal 信封）。
    返回 (payload, request_id)。

    顶层信封严格遵循官方键序: project -> requestId -> request -> model -> userAgent -> requestType。

    严格模式（默认开启）下，入站请求先经 src/fidelity.py 校验：字段白名单、
    模型枚举存在性、thinkingConfig 与模型档位一致；不满足直接抛 FidelityError。
    """
    # 浅拷贝 + 只重建需要修改的层（toolConfig/functionCallingConfig），
    # 避免对大 payload（长对话历史）做整体 deepcopy 的热路径开销；
    # contents 等未修改字段与调用方共享引用（调用方随后不再修改）
    inner = dict(gemini_request)

    if await get_strict_fidelity_enabled():
        validate_client_request(inner, model)

    # 注入 sessionId（联合锚点 = model + systemInstruction + 前两条消息特征，
    # 与 derive_session_key 同口径）：同会话跨轮次恒定，共 system 的不同会话不撞车，
    # 上游据此做隐式 KV 缓存的路由亲和；客户端显式携带 sessionId 时原样透传
    session_id = str(inner.get("sessionId") or "").strip()
    derived_ids = _derive_upstream_session_ids(inner, model, account_salt)
    if not session_id:
        session_id = (
            derived_ids.session_id
            or f"-{uuid.uuid4().int % 9_000_000_000_000_000_000}"
        )
        inner["sessionId"] = session_id

    # step：实抓规律为 step = 1 + 2 * (contents 中 model 消息数)。
    # 依据 2.34.0 样本（20261121-112701 / 101904 全序列）：
    #   model 消息 0 条 → step=1；1 条（含工具调用）→ step=3；2 条 → step=5；
    #   101904 连续重试序列 193 条 contents / 192 条 model → step=385，完全吻合。
    # labels.last_step_index = step - 1。
    model_turns = 0
    contents = inner.get("contents")
    if isinstance(contents, list):
        model_turns = sum(
            1
            for content in contents
            if isinstance(content, dict) and content.get("role") == "model"
        )
    step = 1 + 2 * model_turns

    # requestId：官方 5 段形态 agent/{cascadeId}/{unixMs}/{trajectoryId}/{step}
    # cascadeId / trajectoryId 与 sessionId 同源确定性派生（无锚点时各自随机兜底），
    # 第 2 段与 labels.cascade_id 严格一致
    trajectory_id = derived_ids.trajectory_id or str(uuid.uuid4())
    cascade_id = derived_ids.cascade_id or str(uuid.uuid4())
    request_id = _generate_request_id(cascade_id, step, trajectory_id)

    # thoughtSignature 回注：真实客户端会把上一轮签名带回；兼容客户端常丢弃。
    # 只补回本轨迹确实见过的签名，不伪造。
    thought_signature_cache.replay_into_contents(inner.get("contents"), trajectory_id)

    # 注入 labels（键序/键集完全对齐实抓样本；last_step_index = step - 1）
    inner["labels"] = _build_labels(
        model,
        trajectory_id,
        step,
        cascade_id=cascade_id,
        request_seq=_next_trajectory_request_seq(trajectory_id),
    )

    # requestType 与 toolConfig 对齐官方 agent 状态机：
    # - 常规智能体交互（有 tools 或含工具调用上下文）→ agent；
    # - 纯文本问答 / 补全 → chat，且绝不携带 tools/toolConfig（避免 Agent 资源池限流）。
    tools = inner.get("tools")
    has_tools = _has_function_tool(tools)
    has_tool_interactions = _contents_have_tool_interactions(inner.get("contents"))
    is_agent_request = has_tools or has_tool_interactions
    is_image_request = "image" in (model or "").lower()

    if is_agent_request and not is_image_request:
        # toolConfig 纯透传：客户端有就传，没有就不传（对齐官方 Antigravity 报文，
        # 绝不主动注入任何默认 mode），连同 allowedFunctionNames 等字段完整保留。
        # tools 兜底拆分为官方单函数独立切片形态（上游 normalize 已拆，此为最终出口保证）
        # 循环依赖，延迟导入
        from src.converter.antigravity_fix import _split_tools_single_function
        if isinstance(inner.get("tools"), list):
            inner["tools"] = _split_tools_single_function(inner["tools"])
        # 孤立悬挂的 toolConfig（无对应 tools）会引发上游 400 INVALID_ARGUMENT
        if not has_tools:
            inner.pop("toolConfig", None)
    else:
        # 无工具交互 / 图片生成：剥离客户端可能携带的 tools/toolConfig，对齐官方 chat 报文
        inner.pop("tools", None)
        inner.pop("toolConfig", None)

    # 内部 request 拓扑对齐官方键序:
    # contents -> systemInstruction -> tools -> toolConfig -> labels -> generationConfig -> sessionId -> 其余
    inner = _align_inner_request_topology(inner)

    payload = {
        "project": project_id,
        "requestId": request_id,
        "request": inner,
        "model": model,
        "userAgent": "antigravity",
        "requestType": "image_gen" if is_image_request else ("agent" if is_agent_request else "chat"),
    }
    return payload, request_id


def _align_inner_request_topology(inner: Dict[str, Any]) -> Dict[str, Any]:
    """内部 request 严格按官方键序重组:
    contents -> systemInstruction -> tools -> toolConfig -> labels -> generationConfig -> sessionId -> 其余
    """
    key_order = [
        "contents",
        "systemInstruction",
        "tools",
        "toolConfig",
        "labels",
        "generationConfig",
        "sessionId",
    ]
    reordered: Dict[str, Any] = {}
    for key in key_order:
        if key in inner:
            reordered[key] = inner.pop(key)
    # 其余未知/扩展字段保留在末尾
    for key, value in inner.items():
        if key not in reordered:
            reordered[key] = value
    return reordered


# ==================== 辅助函数 ====================

def build_antigravity_headers(
    access_token: str,
    extra_headers: Optional[Dict[str, str]] = None,
    model_name: str = "",
) -> Dict[str, str]:
    """构建 Antigravity CLI API 请求头（对齐官方 Hub 出站指纹）。

    实测真实 Hub 只发送 User-Agent / Authorization / Content-Type / Accept-Encoding，
    **不发 Accept**——多一个头同样是可识别的差异，故这里不再注入。
    """
    headers = {
        "User-Agent": ANTIGRAVITY_USER_AGENT,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
    }

    for key, value in _sanitize_antigravity_headers(extra_headers).items():
        headers.setdefault(key, value)

    return headers


def _is_retryable_status(status_code: int, disable_error_codes: List[int]) -> bool:
    """统一判断是否属于可重试状态码。"""
    return status_code in (429, 503) or status_code in disable_error_codes


def _remember_signatures_from_data(data: Any, trajectory_id: str) -> None:
    """从上游响应里提取 thoughtSignature 并按文本缓存（供后续轮次回注）。"""
    if not trajectory_id or not isinstance(data, dict):
        return
    inner = data.get("response") if isinstance(data.get("response"), dict) else data
    candidates = inner.get("candidates")
    if not isinstance(candidates, list):
        return
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            signature = part.get("thoughtSignature")
            if isinstance(text, str) and text and isinstance(signature, str) and signature:
                thought_signature_cache.remember(trajectory_id, text, signature)


def _remember_signatures_from_line(line: str, trajectory_id: str) -> None:
    """解析单条 SSE data 帧并缓存签名（仅在命中子串时调用）。"""
    if not line.startswith("data:"):
        return
    try:
        data = json.loads(line[len("data:"):].strip())
    except (json.JSONDecodeError, ValueError):
        return
    _remember_signatures_from_data(data, trajectory_id)


async def _stream_with_credential_slot(
    credential_key: str, **kwargs: Any
) -> AsyncGenerator[Any, None]:
    """在"按凭证并发名额"内桥接 stream_post_async。

    名额从流开始持有到流结束（含客户端提前断开），确保同一凭证的在途流数量
    受控；异常或提前关闭都会在 finally 中归还。
    """
    key = credential_key or "default"
    await credential_limiter.acquire(key)
    try:
        async for chunk in stream_post_async(**kwargs):
            yield chunk
    finally:
        credential_limiter.release(key)


def _fidelity_response(exc: FidelityError) -> Response:
    """把保真度拒绝转成固定结构的错误响应（不重试、不计入上游调用）。"""
    log.error(f"[FIDELITY] 拒绝请求: {exc.reason} - {exc.detail}")
    return Response(
        content=json.dumps(exc.to_payload(), ensure_ascii=False),
        status_code=exc.status_code,
        media_type="application/json",
    )


async def _switch_credential_for_retry(
    *,
    next_cred_task: Optional[asyncio.Task],
    retry_interval: float,
    refresh_credential_fast: Callable[[], Any],
    apply_cred_result: Callable[[Tuple[str, Dict[str, Any]]], bool],
    log_prefix: str,
) -> Tuple[bool, Optional[asyncio.Task]]:
    """优先使用预热凭证，失败后退回同步刷新。"""
    if next_cred_task is not None:
        try:
            cred_result = await next_cred_task
            next_cred_task = None
            if cred_result and apply_cred_result(cred_result):
                await asyncio.sleep(retry_interval)
                return True, next_cred_task
        except Exception as e:
            log.warning(f"{log_prefix} 预热凭证任务失败: {e}")
            next_cred_task = None

    await asyncio.sleep(retry_interval)
    if await refresh_credential_fast():
        return True, next_cred_task

    return False, next_cred_task


# ==================== 新的流式和非流式请求函数 ====================

def _derive_session_key(model_name: str, body: Dict[str, Any]) -> Optional[str]:
    """从 API 请求体派生会话 key（凭证亲和 cache 模式使用）

    body 格式: {"model": ..., "request": <Gemini 规范化请求>}
    hash(模型 + systemInstruction 或 第一条 user 消息) —— 后者必须是经过所有格式处理后的内容
    """
    inner = body.get("request") if isinstance(body.get("request"), dict) else body
    return derive_session_key(model_name, inner)


def _base_log_entry(
    model_name: str,
    email: str,
    is_stream: bool,
    log_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建请求日志条目的公共字段

    model_name: 后端实际使用的真实模型名（映射后）
    log_ctx.client_model: 客户端请求的原始模型名（可能带功能前缀）
    """
    ctx = log_ctx or {}
    return {
        "request_id": ctx.get("request_id") or new_request_id(),
        "api_format": ctx.get("api_format", ""),
        "client_ip": ctx.get("client_ip", ""),
        "model_name": ctx.get("client_model") or model_name,
        "upstream_model_name": model_name,
        "credential_email": email,
        "is_stream": is_stream,
    }


async def _record_request_body_detail(
    status_code: int,
    error_message: str,
    log_entry: Dict[str, Any],
    *,
    url: str,
    headers: Optional[Dict[str, str]],
    payload: Any,
    response_body: str,
    is_stream: bool,
) -> str:
    """
    请求体详细日志：开关开启且状态码命中配置错误码时写盘（JSON 文件），
    数据库只保存文件索引路径。任何异常不影响主流程。
    """
    try:
        if not await get_request_log_enabled():
            return ""
        if status_code not in await get_request_log_error_codes():
            return ""

        rel_path = await record_request_body(
            request_id=str(log_entry.get("request_id", "")),
            model_name=str(log_entry.get("model_name", "")),
            upstream_model_name=str(log_entry.get("upstream_model_name", "")),
            credential_email=str(log_entry.get("credential_email", "")),
            is_stream=is_stream,
            status_code=status_code,
            url=url,
            headers=headers,
            payload=payload,
            response_body=response_body,
            error_message=error_message,
        )
        return rel_path or ""
    except Exception as e:
        log.debug(f"请求体详细日志写入失败（不影响主流程）: {e}")
        return ""


async def stream_request(
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
    log_ctx: Optional[Dict[str, Any]] = None,
):
    """
    流式请求函数

    Args:
        body: 请求体
        native: 是否返回原生bytes流，False则返回str流
        headers: 额外的请求头

    Yields:
        Response对象（错误时）或 bytes流/str流（成功时）
    """
    model_name = body.get("model", "")

    # 请求日志：开始时间与基础条目（成功/失败均会记录一次）
    log_entry = _base_log_entry(model_name, "", True, log_ctx)
    log_start_time = time.time()

    # 凭证亲和: 从请求内容派生会话 key
    session_key = _derive_session_key(model_name, body)

    # 1. 获取有效凭证
    cred_result = await get_valid_credential(
        model_name=model_name, session_key=session_key
    )

    if not cred_result:
        # 如果返回值是None，直接返回错误500
        log.error("[ANTIGRAVITY STREAM] 当前无可用凭证")
        log_entry.update({
            "status_code": 500, "success": False,
            "error_message": "当前无可用凭证", "use_time": time.time() - log_start_time,
        })
        await record_usage_log(log_entry)
        yield Response(
            content=json.dumps({"error": "当前无可用凭证"}),
            status_code=500,
            media_type="application/json"
        )
        return

    current_file, credential_data = cred_result
    log_entry["credential_email"] = current_file
    access_token = credential_data.get("access_token") or credential_data.get("token")
    project_id = credential_data.get("project_id", "")
    # 每账号独立出口：该账号配置了代理则走自己的出口，否则沿用全局 PROXY
    cred_proxy = credential_data.get("proxy") or None

    if not access_token:
        log.error(f"[ANTIGRAVITY STREAM] No access token in credential: {current_file}")
        # 记录失败日志（含涉及的凭证）
        log_entry.update({
            "status_code": 500, "success": False,
            "error_message": "凭证中没有访问令牌", "use_time": time.time() - log_start_time,
        })
        await record_usage_log(log_entry)
        yield Response(
            content=json.dumps({"error": "凭证中没有访问令牌"}),
            status_code=500,
            media_type="application/json"
        )
        return

    # 2. 构建URL和请求头（端点来自配置，支持面板热更新）
    target_url = f"{await get_antigravity_api_url()}/v1internal:streamGenerateContent?alt=sse"

    auth_headers = build_antigravity_headers(access_token, headers, model_name)

    # 构建 CLI 格式请求体（严格模式下可能直接拒绝）
    inner_request = body.get("request", body)
    try:
        final_payload, _ = await wrap_cli_request(
            inner_request, model_name, project_id, account_salt=current_file
        )
    except FidelityError as exc:
        log_entry.update({
            "status_code": exc.status_code, "success": False,
            "error_message": f"{exc.reason}: {exc.detail}",
            "use_time": time.time() - log_start_time,
        })
        await record_usage_log(log_entry)
        yield _fidelity_response(exc)
        return

    trajectory_id = final_payload["request"]["labels"]["trajectory_id"]

    # 3. 调用stream_post_async进行请求
    retry_config = await get_retry_config()
    max_retries = retry_config["max_retries"]
    retry_interval = retry_config["retry_interval"]

    DISABLE_ERROR_CODES = await get_auto_ban_error_codes()  # 禁用凭证的错误码
    last_error_response = None  # 记录最后一次的错误响应
    last_error_body = ""  # 详细日志：最后一次的错误响应体
    next_cred_task = None  # 预热的下一个凭证任务
    excluded_emails: Set[str] = {current_file} if current_file else set()

    # 内部函数：快速更新凭证(只更新token和project_id,避免重建整个请求)
    async def refresh_credential_fast(strict_exclude: bool = False):
        nonlocal current_file, access_token, auth_headers, project_id, final_payload, cred_proxy
        cred_result = await get_valid_credential(
            model_name=model_name,
            session_key=session_key,
            exclude_emails=excluded_emails,
        )
        if not cred_result and not strict_exclude:
            # 若排除已尝试凭证后无候选，退回不排除（例如仅有一个可用凭证时的普通重试）
            cred_result = await get_valid_credential(
                model_name=model_name, session_key=session_key
            )
        if not cred_result:
            return None
        current_file, credential_data = cred_result
        if current_file:
            excluded_emails.add(current_file)
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token:
            return None
        # 只更新token和project_id,不重建整个headers和payload
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        # 切换账号后同步切换出口，避免继续沿用上一个账号的代理
        cred_proxy = credential_data.get("proxy") or None
        return True

    async def refresh_credential_fast_strict():
        return await refresh_credential_fast(strict_exclude=True)

    def apply_cred_result(cred_result: Tuple[str, Dict[str, Any]]) -> bool:
        nonlocal current_file, access_token, project_id, auth_headers, final_payload, cred_proxy
        current_file, credential_data = cred_result
        if current_file:
            excluded_emails.add(current_file)
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token or not project_id:
            return False
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        cred_proxy = credential_data.get("proxy") or None
        return True

    for attempt in range(max_retries + 1):
        # 防封禁：请求节流 + 随机抖动（每次发送前调用）
        await request_pacer.pace_for(current_file or "default")

        success_recorded = False  # 标记是否已记录成功
        need_retry = False  # 标记是否需要重试
        usage_tokens: Dict[str, int] = {}  # 从流中提取的 token 用量
        attempt_start_time = time.time()  # 本次上游调用开始时间

        async def _log_stream_result(status_code: int, success: bool, error_message: str = "") -> None:
            """
            写入一次上游调用的请求日志（调用一次谷歌 API 产生一条日志；
            重试会在后续 attempt 各自再记一条，属于正常行为）。
            """
            # 请求体详细日志：命中配置错误码时写盘，数据库只存文件索引
            request_log_file = await _record_request_body_detail(
                status_code,
                error_message,
                log_entry,
                url=target_url,
                headers=auth_headers,
                payload=final_payload,
                response_body=last_error_body,
                is_stream=True,
            )

            # 每条日志记录本次调用实际使用的凭证
            await record_usage_log({
                **log_entry,
                "credential_email": current_file,
                "status_code": status_code,
                "success": success,
                "use_time": time.time() - attempt_start_time,
                "error_message": error_message[:500],
                "request_log_file": request_log_file,
                **usage_tokens,
            })

        try:
            async for chunk in _stream_with_credential_slot(
                current_file,
                url=target_url,
                body=final_payload,
                native=native,
                headers=auth_headers,
                proxy=cred_proxy,
            ):
                # 判断是否是Response对象
                if isinstance(chunk, Response):
                    status_code = chunk.status_code
                    last_error_response = chunk  # 记录最后一次错误

                    # 缓存错误解析结果,避免重复decode
                    error_body = None
                    try:
                        error_body = chunk.body.decode('utf-8') if isinstance(chunk.body, bytes) else str(chunk.body)
                    except Exception:
                        error_body = ""
                    last_error_body = error_body or ""

                    # 404 模型不可用：立即更新该凭证的模型列表，并尝试切换到其他支持该模型的凭证重试
                    if status_code == 404:
                        log.warning(
                            f"[ANTIGRAVITY STREAM] 模型 {model_name} 在凭证 {current_file} 上返回 404，更新凭证模型列表并尝试切换凭证"
                        )
                        await _handle_model_not_found_for_credential(current_file, model_name)
                        await _log_stream_result(status_code, False, error_body[:500] or "HTTP 404")

                        if attempt < max_retries:
                            switched, next_cred_task = await _switch_credential_for_retry(
                                next_cred_task=None,
                                retry_interval=0.0,
                                refresh_credential_fast=lambda: refresh_credential_fast_strict(),
                                apply_cred_result=apply_cred_result,
                                log_prefix="[ANTIGRAVITY STREAM]",
                            )
                            if switched:
                                log.info(
                                    f"[ANTIGRAVITY STREAM] 404 已切换至新凭证 {current_file} 重试 (attempt {attempt + 2}/{max_retries + 1})"
                                )
                                continue
                        log.error(f"[ANTIGRAVITY STREAM] 无其他支持模型 {model_name} 的凭证，返回 404")
                        yield chunk
                        return

                    # 如果错误码是429、503或者在禁用码当中，做好记录后进行重试
                    if _is_retryable_status(status_code, DISABLE_ERROR_CODES):
                        log.warning(f"[ANTIGRAVITY STREAM] 流式请求失败 (status={status_code}), 凭证: {current_file}, 响应: {error_body[:500] if error_body else '无'}")

                        # 解析冷却时间
                        cooldown_until = None
                        if (status_code == 429 or status_code == 503) and error_body:
                            try:
                                cooldown_until = await parse_and_log_cooldown(error_body)
                            except Exception:
                                pass

                        # 预热下一个凭证
                        if next_cred_task is None and attempt < max_retries:
                            next_cred_task = asyncio.create_task(
                                get_valid_credential(
                                    model_name=model_name,
                                    session_key=session_key,
                                    exclude_emails=excluded_emails,
                                )
                            )

                        # 记录错误并切换凭证
                        await record_api_call_error(current_file, status_code, cooldown_until, model_name=model_name,
                            error_message=error_body
                        )
                        # 本次上游调用产生一条日志（重试会再各记一条）
                        await _log_stream_result(status_code, False, error_body[:500] or f"HTTP {status_code}")

                        # 检查是否应该重试
                        should_retry = await handle_error_with_retry(status_code, current_file, retry_config["retry_enabled"], attempt, max_retries, retry_interval)

                        if should_retry and attempt < max_retries:
                            need_retry = True
                            break  # 跳出内层循环，准备重试
                        else:
                            # 不重试，直接返回原始错误
                            log.error(f"[ANTIGRAVITY STREAM] 达到最大重试次数或不应重试，返回原始错误")
                            yield chunk
                            return
                    else:
                        # 错误码不在禁用码当中，直接返回，无需重试
                        log.error(f"[ANTIGRAVITY STREAM] 流式请求失败，非重试错误码 (status={status_code}), 凭证: {current_file}, 响应: {error_body[:500] if error_body else '无'}")
                        await record_api_call_error(current_file, status_code, None, model_name=model_name,
                            error_message=error_body
                        )
                        await _log_stream_result(status_code, False, error_body[:500] or f"HTTP {status_code}")
                        yield chunk
                        return
                else:
                    # 不是Response，说明是真流，直接yield返回
                    # 只在第一个chunk时记录成功
                    if not success_recorded:
                        await record_api_call_success(current_file, model_name=model_name)
                        success_recorded = True
                        log.debug(f"[ANTIGRAVITY STREAM] 开始接收流式响应，模型: {model_name}")

                    # 从流块中提取 token 用量（热路径优化）：
                    # usageMetadata 几乎只出现在流尾块。先用低开销的子串探测，
                    # 命中才做完整 JSON 解析，避免对每个 chunk 两次 json.loads
                    if native is False and "usageMetadata" in chunk:
                        extracted = extract_usage_tokens(chunk)
                        if extracted:
                            usage_tokens = extracted

                    # 缓存上游返回的 thoughtSignature，供后续轮次回注
                    if native is False and "thoughtSignature" in chunk:
                        _remember_signatures_from_line(chunk, trajectory_id)

                    yield chunk

            # 流式请求完成，检查结果
            if success_recorded:
                log.debug(f"[ANTIGRAVITY STREAM] 流式响应完成，模型: {model_name}")
                await _log_stream_result(200, True)
                return
            elif not need_retry:
                # 没有收到任何数据（空回复），需要重试
                log.warning(f"[ANTIGRAVITY STREAM] 收到空回复，无任何内容，凭证: {current_file}")
                await record_api_call_error(current_file, 200, None, model_name=model_name,
                    error_message="Empty response from API"
                )
                # 本次上游调用（空回复）产生一条日志
                await _log_stream_result(200, False, "Empty response from API")

                if attempt < max_retries:
                    need_retry = True
                else:
                    log.error(f"[ANTIGRAVITY STREAM] 空回复达到最大重试次数")
                    yield Response(
                        content=json.dumps({"error": "服务返回空回复"}),
                        status_code=500,
                        media_type="application/json"
                    )
                    return
            
            # 统一处理重试
            if need_retry:
                log.info(f"[ANTIGRAVITY STREAM] 重试请求 (attempt {attempt + 2}/{max_retries + 1})...")

                switched, next_cred_task = await _switch_credential_for_retry(
                    next_cred_task=next_cred_task,
                    retry_interval=retry_interval,
                    refresh_credential_fast=refresh_credential_fast,
                    apply_cred_result=apply_cred_result,
                    log_prefix="[ANTIGRAVITY STREAM]",
                )
                if not switched:
                    # 换凭证失败，本次未调用谷歌 API，不产生日志
                    log.error("[ANTIGRAVITY STREAM] 重试时无可用凭证或令牌")
                    yield Response(
                        content=json.dumps({"error": "当前无可用凭证"}),
                        status_code=500,
                        media_type="application/json"
                    )
                    return
                continue  # 重试

        except FidelityError as exc:
            # 保真度拒绝不重试：换凭证/换出口都不会让它变得"像"。
            await _log_stream_result(exc.status_code, False, f"{exc.reason}: {exc.detail}")
            yield _fidelity_response(exc)
            return

        except GeneratorExit:
            # 下游提前关闭了本生成器（转换器断流、抗截断续传、客户端断开等）。
            # 已收到上游数据却未走到正常收尾时，补记一条成功日志，避免请求
            # 日志丢失（issue #438）；GeneratorExit 不可吞，必须 re-raise
            if success_recorded:
                log.debug("[ANTIGRAVITY STREAM] 生成器被提前关闭，补记成功日志")
                await _log_stream_result(200, True)
            raise

        except Exception as e:
            log.error(f"[ANTIGRAVITY STREAM] 流式请求异常: {e}, 凭证: {current_file}")
            if success_recorded:
                # 已经向客户端发送过部分内容，重试会导致整个响应从头重发（内容重复）。
                # 只能结束当前流（客户端表现为截断，可配合抗截断前缀使用）。
                log.error("[ANTIGRAVITY STREAM] 流中途异常且已发送部分内容，结束流而不重试")
                await _log_stream_result(500, False, f"异常(已发送部分内容,不重试): {e}")
                return
            if attempt < max_retries:
                log.info(f"[ANTIGRAVITY STREAM] 异常后重试 (attempt {attempt + 2}/{max_retries + 1})...")
                # 本次上游调用（异常）产生一条日志
                await _log_stream_result(500, False, f"异常: {e}")
                await asyncio.sleep(retry_interval)
                continue
            else:
                # 所有重试都失败，返回最后一次的错误（如果有）
                log.error(f"[ANTIGRAVITY STREAM] 所有重试均失败，最后异常: {e}")
                await _log_stream_result(500, False, f"异常: {e}")
                if last_error_response:
                    yield last_error_response
                else:
                    # 如果没有记录到错误响应，返回500错误
                    yield Response(
                        content=json.dumps({"error": f"流式请求异常: {str(e)}"}),
                        status_code=500,
                        media_type="application/json"
                    )
                return

    # 所有重试均已耗尽（for循环正常结束）——每次上游调用均已各自记录日志，
    # 这里不额外记录，直接返回最后一次的错误响应
    log.error("[ANTIGRAVITY STREAM] 所有重试均失败")
    if last_error_response:
        yield last_error_response
    else:
        yield Response(
            content=json.dumps({"error": "请求失败，所有重试均已耗尽"}),
            status_code=429,
            media_type="application/json"
        )


async def non_stream_request(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    log_ctx: Optional[Dict[str, Any]] = None,
) -> Response:
    """
    非流式请求函数

    Args:
        body: 请求体
        headers: 额外的请求头
        log_ctx: 请求日志上下文（api_format / client_ip / request_id）

    Returns:
        Response对象
    """
    model_name = body.get("model", "")

    # 请求日志：开始时间与基础条目（成功/失败均会记录一次）
    log_entry = _base_log_entry(model_name, "", False, log_ctx)
    log_start_time = time.time()

    # 详细日志上下文（发往上游的 URL/请求头/请求体；请求派发前为空）
    target_url = ""
    auth_headers: Optional[Dict[str, str]] = None
    final_payload: Dict[str, Any] = {}
    last_error_body = ""  # 详细日志：最后一次的错误响应体
    current_file = ""  # 当前使用的凭证（未获取前为空）
    attempt_start_time = time.time()  # 本次上游调用开始时间（循环内每次重置）

    async def _log_result(status_code: int, success: bool, response_content: Any = None, error_message: str = "") -> None:
        """
        写入一次上游调用的请求日志（调用一次谷歌 API 产生一条日志；
        重试会在后续 attempt 各自再记一条，属于正常行为）。
        """
        # 请求体详细日志：命中配置错误码时写盘，数据库只存文件索引
        request_log_file = await _record_request_body_detail(
            status_code,
            error_message,
            log_entry,
            url=target_url,
            headers=auth_headers,
            payload=final_payload,
            response_body=last_error_body,
            is_stream=False,
        )

        # usage_log 关闭时跳过 token 解析（避免无谓的整包 JSON 解析）
        tokens = {}
        if success and await get_usage_log_enabled():
            tokens = extract_usage_tokens(response_content)
        # 每条日志记录本次调用实际使用的凭证
        await record_usage_log({
            **log_entry,
            "credential_email": current_file,
            "status_code": status_code,
            "success": success,
            "use_time": time.time() - attempt_start_time,
            "error_message": error_message[:500],
            "request_log_file": request_log_file,
            **tokens,
        })

    # 凭证亲和: 从请求内容派生会话 key
    session_key = _derive_session_key(model_name, body)

    # 1. 获取有效凭证
    cred_result = await get_valid_credential(
        model_name=model_name, session_key=session_key
    )

    if not cred_result:
        # 如果返回值是None，直接返回错误500
        log.error("[ANTIGRAVITY] 当前无可用凭证")
        await _log_result(500, False, error_message="当前无可用凭证")
        return Response(
            content=json.dumps({"error": "当前无可用凭证"}),
            status_code=500,
            media_type="application/json"
        )

    current_file, credential_data = cred_result
    log_entry["credential_email"] = current_file
    access_token = credential_data.get("access_token") or credential_data.get("token")
    project_id = credential_data.get("project_id", "")
    # 每账号独立出口：该账号配置了代理则走自己的出口，否则沿用全局 PROXY
    cred_proxy = credential_data.get("proxy") or None

    if not access_token:
        log.error(f"[ANTIGRAVITY] No access token in credential: {current_file}")
        # 记录失败日志（含涉及的凭证）
        await _log_result(500, False, error_message="凭证中没有访问令牌")
        return Response(
            content=json.dumps({"error": "凭证中没有访问令牌"}),
            status_code=500,
            media_type="application/json"
        )

    # 2. 构建URL和请求头（端点来自配置，支持面板热更新）
    target_url = f"{await get_antigravity_api_url()}/v1internal:generateContent"

    auth_headers = build_antigravity_headers(access_token, headers, model_name)

    # 构建 CLI 格式请求体（严格模式下可能直接拒绝）
    inner_request = body.get("request", body)
    try:
        final_payload, _ = await wrap_cli_request(
            inner_request, model_name, project_id, account_salt=current_file
        )
    except FidelityError as exc:
        await _log_result(exc.status_code, False, error_message=f"{exc.reason}: {exc.detail}")
        return _fidelity_response(exc)

    trajectory_id = final_payload["request"]["labels"]["trajectory_id"]

    # 3. 调用post_async进行请求
    retry_config = await get_retry_config()
    max_retries = retry_config["max_retries"]
    retry_interval = retry_config["retry_interval"]

    DISABLE_ERROR_CODES = await get_auto_ban_error_codes()  # 禁用凭证的错误码
    last_error_response = None  # 记录最后一次的错误响应
    next_cred_task = None  # 预热的下一个凭证任务
    excluded_emails: Set[str] = {current_file} if current_file else set()

    # 内部函数：快速更新凭证(只更新token和project_id,避免重建整个请求)
    async def refresh_credential_fast(strict_exclude: bool = False):
        nonlocal current_file, access_token, auth_headers, project_id, final_payload, cred_proxy
        cred_result = await get_valid_credential(
            model_name=model_name,
            session_key=session_key,
            exclude_emails=excluded_emails,
        )
        if not cred_result and not strict_exclude:
            cred_result = await get_valid_credential(
                model_name=model_name, session_key=session_key
            )
        if not cred_result:
            return None
        current_file, credential_data = cred_result
        if current_file:
            excluded_emails.add(current_file)
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token:
            return None
        # 只更新token和project_id,不重建整个headers和payload
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        # 切换账号后同步切换出口，避免继续沿用上一个账号的代理
        cred_proxy = credential_data.get("proxy") or None
        return True

    async def refresh_credential_fast_strict():
        return await refresh_credential_fast(strict_exclude=True)

    def apply_cred_result(cred_result: Tuple[str, Dict[str, Any]]) -> bool:
        nonlocal current_file, access_token, project_id, auth_headers, final_payload, cred_proxy
        current_file, credential_data = cred_result
        if current_file:
            excluded_emails.add(current_file)
        access_token = credential_data.get("access_token") or credential_data.get("token")
        project_id = credential_data.get("project_id", "")
        if not access_token or not project_id:
            return False
        auth_headers["Authorization"] = f"Bearer {access_token}"
        final_payload["project"] = project_id
        cred_proxy = credential_data.get("proxy") or None
        return True

    for attempt in range(max_retries + 1):
        # 防封禁：请求节流 + 随机抖动（每次发送前调用）
        await request_pacer.pace_for(current_file or "default")

        need_retry = False  # 标记是否需要重试
        attempt_start_time = time.time()  # 本次上游调用开始时间
        
        try:
            _cred_key = current_file or "default"
            await credential_limiter.acquire(_cred_key)
            try:
                response = await post_async(
                    url=target_url,
                    json=final_payload,
                    headers=auth_headers,
                    proxy=cred_proxy,
                )
            finally:
                credential_limiter.release(_cred_key)

            status_code = response.status_code

            # 成功
            if status_code == 200:
                # 检查是否为空回复
                if not response.content or len(response.content) == 0:
                    log.warning(f"[ANTIGRAVITY] 收到200响应但内容为空，凭证: {current_file}")
                    
                    # 记录错误
                    await record_api_call_error(current_file, 200, None, model_name=model_name,
                        error_message="Empty response from API"
                    )
                    # 本次上游调用（空回复）产生一条日志
                    await _log_result(200, False, error_message="Empty response from API")
                    
                    if attempt < max_retries:
                        need_retry = True
                    else:
                        log.error(f"[ANTIGRAVITY] 空回复达到最大重试次数")
                        return Response(
                            content=json.dumps({"error": "服务返回空回复"}),
                            status_code=500,
                            media_type="application/json"
                        )
                else:
                    # 正常响应
                    if b"thoughtSignature" in response.content:
                        try:
                            _remember_signatures_from_data(response.json(), trajectory_id)
                        except Exception:  # noqa: BLE001 - 签名缓存失败不影响主流程
                            pass
                    await record_api_call_success(current_file, model_name=model_name)
                    await _log_result(200, True, response_content=response.content)
                    return Response(
                        content=response.content,
                        status_code=200,
                        headers=dict(response.headers)
                    )

            # 失败 - 记录最后一次错误
            if status_code != 200:
                last_error_response = Response(
                    content=response.content,
                    status_code=status_code,
                    headers=dict(response.headers)
                )

                # 判断是否需要重试
                # 缓存错误文本,避免重复解析
                error_text = ""
                try:
                    error_text = response.text
                except Exception:
                    pass
                last_error_body = error_text

                # 404 模型不可用：立即更新该凭证的模型列表，并尝试切换到其他支持该模型的凭证重试
                if status_code == 404:
                    log.warning(
                        f"[ANTIGRAVITY] 模型 {model_name} 在凭证 {current_file} 上返回 404，更新凭证模型列表并尝试切换凭证"
                    )
                    await _handle_model_not_found_for_credential(current_file, model_name)
                    await _log_result(status_code, False, error_message=error_text[:500] or "HTTP 404")

                    if attempt < max_retries:
                        switched, next_cred_task = await _switch_credential_for_retry(
                            next_cred_task=None,
                            retry_interval=0.0,
                            refresh_credential_fast=refresh_credential_fast_strict,
                            apply_cred_result=apply_cred_result,
                            log_prefix="[ANTIGRAVITY]",
                        )
                        if switched:
                            log.info(
                                f"[ANTIGRAVITY] 404 已切换至新凭证 {current_file} 重试 (attempt {attempt + 2}/{max_retries + 1})"
                            )
                            continue
                    log.error(f"[ANTIGRAVITY] 无其他支持模型 {model_name} 的凭证，返回 404")
                    return last_error_response

                if _is_retryable_status(status_code, DISABLE_ERROR_CODES):
                    log.warning(f"[ANTIGRAVITY] 非流式请求失败 (status={status_code}), 凭证: {current_file}, 响应: {error_text[:500] if error_text else '无'}")

                    # 解析冷却时间
                    cooldown_until = None
                    if (status_code == 429 or status_code == 503) and error_text:
                        try:
                            cooldown_until = await parse_and_log_cooldown(error_text)
                        except Exception:
                            pass

                    # 并行预热下一个凭证,不阻塞当前处理
                    if next_cred_task is None and attempt < max_retries:
                        next_cred_task = asyncio.create_task(
                                get_valid_credential(
                                    model_name=model_name,
                                    session_key=session_key,
                                    exclude_emails=excluded_emails,
                                )
                            )

                    # 记录错误并切换凭证
                    await record_api_call_error(current_file, status_code, cooldown_until, model_name=model_name,
                        error_message=error_text
                    )
                    # 本次上游调用产生一条日志（重试会再各记一条）
                    await _log_result(status_code, False, error_message=error_text[:500] or f"HTTP {status_code}")

                    # 检查是否应该重试
                    should_retry = await handle_error_with_retry(status_code, current_file, retry_config["retry_enabled"], attempt, max_retries, retry_interval)

                    if should_retry and attempt < max_retries:
                        need_retry = True
                    else:
                        # 不重试，直接返回原始错误
                        log.error(f"[ANTIGRAVITY] 达到最大重试次数或不应重试，返回原始错误")
                        return last_error_response
                else:
                    # 错误码不在禁用码当中，直接返回，无需重试
                    log.error(f"[ANTIGRAVITY] 非流式请求失败，非重试错误码 (status={status_code}), 凭证: {current_file}, 响应: {error_text[:500] if error_text else '无'}")
                    await record_api_call_error(current_file, status_code, None, model_name=model_name,
                        error_message=error_text
                    )
                    await _log_result(status_code, False, error_message=error_text[:500] or f"HTTP {status_code}")
                    return last_error_response
            
            # 统一处理重试
            if need_retry:
                log.info(f"[ANTIGRAVITY] 重试请求 (attempt {attempt + 2}/{max_retries + 1})...")

                switched, next_cred_task = await _switch_credential_for_retry(
                    next_cred_task=next_cred_task,
                    retry_interval=retry_interval,
                    refresh_credential_fast=refresh_credential_fast,
                    apply_cred_result=apply_cred_result,
                    log_prefix="[ANTIGRAVITY]",
                )
                if not switched:
                    # 换凭证失败，本次未调用谷歌 API，不产生日志
                    log.error("[ANTIGRAVITY] 重试时无可用凭证或令牌")
                    return Response(
                        content=json.dumps({"error": "当前无可用凭证"}),
                        status_code=500,
                        media_type="application/json"
                    )
                continue  # 重试

        except FidelityError as exc:
            # 保真度拒绝不重试
            await _log_result(exc.status_code, False, error_message=f"{exc.reason}: {exc.detail}")
            return _fidelity_response(exc)

        except Exception as e:
            log.error(f"[ANTIGRAVITY] 非流式请求异常: {e}, 凭证: {current_file}")
            if attempt < max_retries:
                log.info(f"[ANTIGRAVITY] 异常后重试 (attempt {attempt + 2}/{max_retries + 1})...")
                # 本次上游调用（异常）产生一条日志
                await _log_result(500, False, error_message=f"异常: {e}")
                await asyncio.sleep(retry_interval)
                continue
            else:
                # 所有重试都失败，返回最后一次的错误（如果有）或500错误
                log.error(f"[ANTIGRAVITY] 所有重试均失败，最后异常: {e}")
                await _log_result(500, False, error_message=f"异常: {e}")
                if last_error_response:
                    return last_error_response
                else:
                    return Response(
                        content=json.dumps({"error": f"非流式请求异常: {str(e)}"}),
                        status_code=500,
                        media_type="application/json"
                    )

    # 所有重试都失败（for循环正常结束）——每次上游调用均已各自记录日志，
    # 这里不额外记录，直接返回最后一次的原始错误
    log.error("[ANTIGRAVITY] 所有重试均失败")
    if last_error_response:
        return last_error_response
    else:
        return Response(
            content=json.dumps({"error": "所有重试均失败"}),
            status_code=500,
            media_type="application/json"
        )


# ==================== 模型和配额查询 ====================

# 模型列表白名单家族（名字包含即放行）
_MODEL_FAMILY_WHITELIST = ("claude", "gemini", "gpt", "argon")


def _is_listable_model(model_id: str) -> bool:
    """模型列表过滤：剔除 disabled 模型与白名单家族之外的杂牌模型"""
    lowered = (model_id or "").lower()
    if "disabled" in lowered:
        return False
    return any(family in lowered for family in _MODEL_FAMILY_WHITELIST)


def _extract_model_ids_from_raw(data: Dict[str, Any]) -> List[str]:
    """从上游 fetchAvailableModels 响应字典中提取并过滤模型 ID 列表（含衍生模型）"""
    model_ids: List[str] = []
    seen: Set[str] = set()
    raw_models = data.get("models") if isinstance(data, dict) else None
    if isinstance(raw_models, dict):
        for model_id in raw_models.keys():
            if not _is_listable_model(model_id):
                continue
            if model_id not in seen:
                seen.add(model_id)
                model_ids.append(model_id)

        # 添加额外的 claude-sonnet-4-6-thinking 模型
        if "claude-sonnet-4-6" in raw_models and "claude-sonnet-4-6-thinking" not in seen:
            seen.add("claude-sonnet-4-6-thinking")
            model_ids.append("claude-sonnet-4-6-thinking")

        # 添加额外的 claude-opus-4-6 模型
        if "claude-opus-4-6-thinking" in raw_models and "claude-opus-4-6" not in seen:
            seen.add("claude-opus-4-6")
            model_ids.append("claude-opus-4-6")

    return model_ids


async def fetch_credential_models(
    access_token: str,
    proxy: Optional[str] = None,
) -> Optional[List[str]]:
    """
    获取单个凭证支持的模型 ID 列表（经过白名单过滤并补全衍生模型）。

    Args:
        access_token: 凭证访问令牌
        proxy: 该账号的独立出口代理（None = 沿用全局 PROXY 配置）

    Returns:
        成功返回 List[str]，请求失败返回 None
    """
    if not access_token:
        return None

    headers = build_antigravity_headers(access_token, model_name="agent")
    try:
        response = await post_async(
            url=f"{await get_antigravity_api_url()}/v1internal:fetchAvailableModels",
            json={},
            headers=headers,
            timeout=20.0,
            proxy=proxy,
        )
        if response.status_code == 200:
            data = response.json()
            # 顺带学习 model → MODEL_PLACEHOLDER 枚举映射（labels.model_enum 用）
            merge_model_enums(parse_model_enums(data))
            return _extract_model_ids_from_raw(data)
        log.warning(
            f"[ANTIGRAVITY] fetch_credential_models failed ({response.status_code}): "
            f"{response.text[:300]}"
        )
        return None
    except Exception as e:
        log.warning(f"[ANTIGRAVITY] fetch_credential_models exception: {e}")
        return None


async def refresh_credential_models(
    email: str,
    credential_data: Optional[Dict[str, Any]] = None,
) -> Optional[List[str]]:
    """
    刷新指定凭证支持的模型列表并持久化到数据库。

    Returns:
        成功返回更新后的模型 ID 列表，失败返回 None
    """
    if not email:
        return None
    database = await get_database()
    if credential_data is None:
        credential_data = await database.get_credential(email)
    if not credential_data:
        return None

    if await should_refresh_token(credential_data):
        refreshed = await refresh_token(credential_data, email)
        if not refreshed:
            return None
        credential_data = refreshed

    access_token = credential_data.get("access_token") or credential_data.get("token")
    if not access_token:
        return None

    models = await fetch_credential_models(
        access_token, proxy=credential_data.get("proxy") or None
    )
    if models is not None:
        await update_credential_state(email, {"supported_models": models})
        log.info(f"[ANTIGRAVITY] 已更新凭证 {email} 支持的模型列表 ({len(models)} 个)")
    return models


async def _handle_model_not_found_for_credential(email: str, model_name: str) -> None:
    """
    当某凭证请求特定模型返回 404 时：
    1. 立即从该凭证的 supported_models 中剔除该模型（如果已有记录）
    2. 异步触发重新拉取该凭证的真实可用模型列表并落库
    """
    if not email:
        return
    try:
        database = await get_database()
        await database.remove_model_from_credential(email, model_name)
    except Exception as e:
        log.debug(f"[ANTIGRAVITY] 移除凭证模型失败 {email}: {e}")

    asyncio.create_task(refresh_credential_models(email))


async def fetch_available_models() -> List[Dict[str, Any]]:
    """
    获取所有可用凭证支持的模型并集，返回符合 OpenAI API 规范的格式。
    - 优先汇总数据库中所有启用凭证的 supported_models 并集；
    - 若存在尚未探测过模型列表的启用凭证，并发探测并写入数据库后再汇总；
    - 若数据库并集仍为空，回退到使用单个可用凭证实时请求。
    """
    database = await get_database()
    union_models, unprobed_emails = await database.get_enabled_credentials_models_info()

    # 对尚未探测过模型列表的启用凭证进行并发探测（限制并发避免瞬时突发过多请求）
    if unprobed_emails:
        sem = asyncio.Semaphore(5)

        async def _probe_one(target_email: str) -> Optional[List[str]]:
            async with sem:
                return await refresh_credential_models(target_email)

        probe_Targets = unprobed_emails[:20]  # 单次最多探测 20 个未初始化凭证，防止阻塞过久
        await asyncio.gather(*(_probe_one(e) for e in probe_Targets), return_exceptions=True)
        union_models, _ = await database.get_enabled_credentials_models_info()

    # 若仍为空（如所有凭证首次请求均失败），兜底用单个有效凭证请求一次
    if not union_models:
        cred_result = await get_valid_credential()
        if not cred_result:
            log.error("[ANTIGRAVITY] No valid credentials available for fetching models")
            return []

        current_file, credential_data = cred_result
        models = await refresh_credential_models(current_file, credential_data)
        if models:
            union_models = models
        else:
            return []

    current_timestamp = int(datetime.now(timezone.utc).timestamp())
    model_list = [
        model_to_dict(
            Model(
                id=model_id,
                object="model",
                created=current_timestamp,
                owned_by="google",
            )
        )
        for model_id in union_models
    ]

    log.info(f"[ANTIGRAVITY] Fetched {len(model_list)} available models (union across credentials)")
    return model_list


async def fetch_quota_info(
    access_token: str,
    project_id: str = "",
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """
    获取指定凭证的额度信息（使用 retrieveUserQuotaSummary 获取 weekly+5h 分组配额）

    Args:
        access_token: Antigravity 访问令牌
        project_id: 凭证对应的项目 ID

    Returns:
        包含额度信息的字典，格式为：
        {
            "success": True/False,
            "models": {
                "bucket_id": {
                    "remaining": 0.95,
                    "resetTime": "12-20 10:30",
                    "resetTimeRaw": "2025-12-20T02:30:00Z",
                    "resetDays": 5,
                    "window": "weekly",
                    "group": "gemini 3 pro"
                }
            },
            "minResetDays": 5,
            "accountTier": "pro"/"normal",
            "isPro": True/False,
            "quotaGroups": [...],
            "source": "retrieveUserQuotaSummary",
            "error": "错误信息" (仅在失败时)
        }
    """

    headers = build_antigravity_headers(access_token, model_name="agent")

    url = f"{await get_antigravity_api_url()}/v1internal:retrieveUserQuotaSummary"
    payload: Dict[str, Any] = {}
    if project_id:
        payload["project"] = project_id

    log.info(f"[ANTIGRAVITY QUOTA] fetch_quota_info: project={project_id}, url={url}")

    try:
        response = await post_async(
            url=url,
            json=payload,
            headers=headers,
            timeout=30.0,
            proxy=proxy,
        )

        if response.status_code != 200:
            error_text = response.text[:500]
            log.error(f"[ANTIGRAVITY QUOTA] fetch_quota_info 失败: {response.status_code} - {error_text}")
            return {"success": False, "error": f"API返回错误: {response.status_code} - {error_text}"}

        data = response.json()
        log.debug(f"[ANTIGRAVITY QUOTA] Raw response: {json.dumps(data, ensure_ascii=False)[:500]}")

        # 解析分组配额（weekly + 5h 双窗口）
        quota_groups: List[Dict[str, Any]] = []
        for g in data.get("groups", []):
            buckets = []
            for b in g.get("buckets", []):
                buckets.append({
                    "bucketId": b.get("bucketId", ""),
                    "displayName": b.get("displayName", ""),
                    "window": b.get("window", ""),
                    "resetTime": b.get("resetTime", ""),
                    "remainingFraction": b.get("remainingFraction", 0),
                    "description": b.get("description"),
                })
            quota_groups.append({
                "displayName": g.get("displayName", ""),
                "description": g.get("description"),
                "buckets": buckets,
            })

        # 从分组配额中推断账号类型和重置天数
        min_reset_days = None
        for group in quota_groups:
            for bucket in group.get("buckets", []):
                reset_time = bucket.get("resetTime", "")
                if reset_time:
                    try:
                        reset_dt = datetime.fromisoformat(reset_time.replace('Z', '+00:00'))
                        now = datetime.now(timezone.utc)
                        delta = reset_dt - now
                        reset_days = max(0, delta.days)
                        if min_reset_days is None or reset_days < min_reset_days:
                            min_reset_days = reset_days
                    except Exception:
                        pass

        # 判断账号类型: 根据 weekly 窗口重置周期推断
        is_pro = min_reset_days is not None and min_reset_days <= 1
        account_tier = "pro" if is_pro else "normal"

        # 构建配额映射（从分组数据展开，兼容旧格式）
        quota_info: Dict[str, Dict[str, Any]] = {}
        for group in quota_groups:
            group_name = group.get("displayName", "").lower()
            for bucket in group.get("buckets", []):
                bucket_id = bucket.get("bucketId", "")
                remaining = bucket.get("remainingFraction", 0)
                reset_time = bucket.get("resetTime", "")
                window = bucket.get("window", "")

                # 转换为北京时间
                reset_time_beijing = 'N/A'
                reset_days = None
                if reset_time:
                    try:
                        utc_date = datetime.fromisoformat(reset_time.replace('Z', '+00:00'))
                        # 转换为北京时间 (UTC+8)
                        beijing_date = utc_date + timedelta(hours=8)
                        reset_time_beijing = beijing_date.strftime('%m-%d %H:%M')
                        now = datetime.now(timezone.utc)
                        delta = utc_date - now
                        reset_days = max(0, delta.days)
                    except Exception as e:
                        log.warning(f"[ANTIGRAVITY QUOTA] Failed to parse reset time: {e}")

                quota_info[bucket_id] = {
                    "remaining": remaining,
                    "resetTime": reset_time_beijing,
                    "resetTimeRaw": reset_time,
                    "resetDays": reset_days,
                    "window": window,
                    "group": group_name,
                }

        log.info(
            f"[ANTIGRAVITY QUOTA] 解析到 {len(quota_groups)} 个分组, "
            f"{len(quota_info)} 个配额桶, tier={account_tier}"
        )
        return {
            "success": True,
            "models": quota_info,
            "minResetDays": min_reset_days,
            "accountTier": account_tier,
            "isPro": is_pro,
            "quotaGroups": quota_groups,
            "source": "retrieveUserQuotaSummary",
        }
    except Exception as e:
        log.error(f"[ANTIGRAVITY QUOTA] Failed to fetch quota: {e}")
        log.error(f"[ANTIGRAVITY QUOTA] Traceback: {traceback.format_exc()}")
        return {
            "success": False,
            "error": str(e)
        }