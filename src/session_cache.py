"""
会话凭证缓存（凭证亲和）
同一会话（由请求内容派生的稳定 key）绑定同一凭证，命中上游 prompt cache。

双后端:
- Redis（可选）: 设置 REDIS_URL 后启用，多实例共享绑定
- 内存（默认）: 无 Redis 时自动使用，进程内字典 + 过期清理

行为:
- get: 命中时自动续期 TTL（活跃会话保持绑定，空闲会话过期）
- set: 每次成功调用后写入/刷新绑定
- Redis 故障仅跳过该次操作并告警，不影响主流程；内存后端始终可用兜底
"""

import asyncio
import hashlib
import json
import time
from typing import Any, Dict, List, Optional

from log import log

# 绑定 TTL（秒）：会话空闲超过该时长后解绑
SESSION_BINDING_TTL = 3600

_KEY_PREFIX = "agy2api:sess:"

# Redis 客户端（None = 未启用，使用内存后端）
_redis_client = None

# 内存后端: {session_key: (email, expire_at)}
_memory_store: Dict[str, tuple] = {}
_memory_lock = asyncio.Lock()

# 内存后端容量上限（防止无限增长），超限时清理过期项后仍超限则丢弃最旧的
_MEMORY_MAX_ENTRIES = 50000


def _now() -> float:
    return time.time()


def _get_client():
    """返回已初始化的 Redis 客户端；未启用时返回 None"""
    return _redis_client


async def init_redis(redis_url: str) -> bool:
    """初始化 Redis 连接。连接失败返回 False（回落内存后端），不抛异常。"""
    global _redis_client
    if not redis_url:
        return False
    try:
        import redis.asyncio as redis_aiolib

        client = redis_aiolib.from_url(
            redis_url,
            decode_responses=False,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            health_check_interval=30,
        )
        await client.ping()
        _redis_client = client
        log.info("会话凭证缓存已连接 Redis")
        return True
    except Exception as e:
        log.warning(f"Redis 连接失败，使用内存后端: {e}")
        _redis_client = None
        return False


async def close() -> None:
    """关闭 Redis 连接（应用关闭时调用）"""
    global _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception:
            pass
        _redis_client = None
    _memory_store.clear()


# ====================== 会话 key 派生 ======================

def _stable_json(value: Any) -> Optional[str]:
    """稳定序列化（排序键），失败返回 None"""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _anchor_part_feature(part: Any) -> Optional[str]:
    """提取单个 part 的稳定特征（文本或首个 functionCall 的名称+参数）。"""
    if not isinstance(part, dict):
        return None
    if isinstance(part.get("text"), str) and part["text"]:
        return f"text:{part['text']}"
    fc = part.get("functionCall") or part.get("function_call")
    if isinstance(fc, dict):
        name = fc.get("name") or ""
        args_json = _stable_json(fc.get("args"))
        if args_json is not None:
            return f"fc:{name}:{args_json}"
        return f"fc:{name}"
    return None


def _anchor_message_features(content: Any) -> Optional[str]:
    """提取一条消息的稳定特征（按 part 顺序拼接，最多取前 4 个 part）。"""
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    features = []
    for part in parts[:4]:
        feature = _anchor_part_feature(part)
        if feature is not None:
            features.append(feature)
    return "|".join(features) if features else None


def derive_conversation_anchor(
    model_name: str, gemini_request: Dict[str, Any]
) -> Optional[bytes]:
    """
    派生会话联合锚点摘要: SHA256(model + systemInstruction 文本 + 前两条消息特征)。

    联合摘要同时供两处使用，保证锚点口径一致:
    - derive_session_key: 凭证亲和会话 key
    - wrap_cli_request: 上游 sessionId / trajectory_id 派生

    锚点必须包含前两条消息特征，避免共用相同 systemInstruction 的
    不同会话（多 Agent / 主子会话场景）派生出相同的 key 发生撞车。

    必须在格式转换完成后调用（openai2gemini / anthropic2gemini / 原生 gemini 都已
    转成 Gemini 格式），保证不同 API 格式的相同内容派生相同的锚点。

    注意：这是**纯函数**（不含粘性状态）。生产路径请用
    `resolve_sticky_anchor`——systemInstruction 会随实验旗标/功能激活在会话内
    演化（静态逆向 2026-11-21：模板 `{{if .ExperimentEnabled ...}}` 门控），
    纯函数口径会导致同一会话中途换 key/trajectory。

    Returns:
        32 字节 SHA256 摘要；无法派生锚点时返回 None
    """
    if not isinstance(gemini_request, dict):
        return None

    model_part = model_name if isinstance(model_name, str) else ""

    # systemInstruction 文本（对话稳定的锚点之一）
    system_text = ""
    system_instruction = gemini_request.get("systemInstruction")
    if isinstance(system_instruction, dict):
        parts = system_instruction.get("parts")
        if isinstance(parts, list):
            texts = [
                str(part.get("text"))
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            system_text = "\n".join(texts)

    # 前两条消息特征（区分同 systemInstruction 下的不同会话）
    message_features: List[str] = []
    contents = gemini_request.get("contents")
    if isinstance(contents, list):
        for content in contents[:2]:
            feature = _anchor_message_features(content)
            if feature is not None:
                message_features.append(feature)
    messages_part = "\x1f".join(message_features)

    if not system_text and not messages_part:
        return None

    anchor_material = f"{model_part}\x1e{system_text}\x1e{messages_part}"
    return hashlib.sha256(anchor_material.encode("utf-8")).digest()


# ====================== 粘性锚点（首见绑定） ======================
# §10.14:真实客户端的 trajectory/cascade 在会话诞生时确定后不再变化;而
# systemInstruction 会随实验旗标/功能激活在会话内演化(静态逆向 2026-11-21:
# Go text/template 的 {{if .ExperimentEnabled}} 门控,实抓 163547 的 0013 与
# 0080 段落不同)。把 sys 全文放进锚点会使同一会话中途换 key——上游缓存
# 亲和断裂、凭证亲和丢失、step/盖戳等轨迹级状态归零。
# 粘性绑定:锚点键剔除 sys(= 模型 + 前两条消息特征),**首次见到**时按全量
# 锚点(含 sys,保留诞生时的碰撞隔离口径)派生并登记,此后沿用首见绑定。
# 代价:同 (模型,前两条消息) 但不同 sys 的两个会话会合并到首见绑定(旧行为
# 仅在 sys 也相同时才撞车);相比"每个 sys 演化的长会话必断锚"是净收益。
# 进程内 LRU + TTL,与轨迹计数器同一失败模式(重启丢失→按当前 sys 重新
# 绑定一次,此后稳定)。

_STICKY_MEMO_TTL = 12 * 3600
_STICKY_MEMO_MAX = 4096
_sticky_memo: "Dict[str, tuple]" = {}  # sticky_key -> (digest, bound_at)
_sticky_memo_order: List[str] = []  # FIFO 淘汰序(插入序),与 dict 并行维护


def _sticky_base_digest(
    model_name: str, gemini_request: Dict[str, Any]
) -> Optional[bytes]:
    """粘性键基底: SHA256(model + 首条消息特征),不含 systemInstruction。

    取首条(而非前两条)消息:首轮请求只有一条消息,第二轮起 contents[:2]
    变为 [首条 user, 首条 model]——纯函数锚点从第 2 条请求起必然变化
    (test_sys_evolution_keeps_trajectory_continuous 暴露的既有 bug:
    每个会话的第 1 条请求与后续请求本来就派生不同 trajectory)。首条
    消息是会话身份的稳定核心,追加历史/模型回复都不影响它。
    """
    if not isinstance(gemini_request, dict):
        return None
    model_part = model_name if isinstance(model_name, str) else ""
    message_features: List[str] = []
    contents = gemini_request.get("contents")
    if isinstance(contents, list):
        for content in contents[:1]:
            feature = _anchor_message_features(content)
            if feature is not None:
                message_features.append(feature)
    messages_part = "\x1f".join(message_features)
    if not messages_part:
        return None
    return hashlib.sha256(
        f"{model_part}\x1e{messages_part}".encode("utf-8")
    ).digest()


def resolve_sticky_anchor(
    model_name: str, gemini_request: Dict[str, Any], account_salt: str = ""
) -> Optional[bytes]:
    """粘性解析会话锚点摘要:首见按全量锚点派生,此后沿用首见绑定。

    - 无消息特征(空对话)时退化为纯函数口径;
    - ``account_salt`` 混入粘性键:不同凭证不共享绑定(与 trajectory 派生的
      账号隔离一致);返回值仍是**未混盐**的全量摘要,盐由调用方按原口径追加;
    - 绑定项带 TTL(空闲长会话过期后按当前 sys 重新绑定一次)与 FIFO 容量上限。
    """
    full = derive_conversation_anchor(model_name, gemini_request)
    if full is None:
        return None
    base = _sticky_base_digest(model_name, gemini_request)
    if base is None:
        return full
    sticky_key = hashlib.sha256(
        (account_salt or "").encode("utf-8") + b"|" + base
    ).hexdigest()
    now = _now()
    entry = _sticky_memo.get(sticky_key)
    if entry is not None and now - entry[1] < _STICKY_MEMO_TTL:
        # LRU 触碰:移到队尾
        try:
            _sticky_memo_order.remove(sticky_key)
        except ValueError:
            pass
        _sticky_memo_order.append(sticky_key)
        return entry[0]
    if entry is None:
        _sticky_memo[sticky_key] = (full, now)
        _sticky_memo_order.append(sticky_key)
    else:
        # TTL 过期:按当前 sys 重新绑定
        entry = (full, now)
        _sticky_memo[sticky_key] = entry
    # 容量维护:先清过期,再 FIFO 淘汰最旧
    while len(_sticky_memo_order) > _STICKY_MEMO_MAX:
        oldest = _sticky_memo_order.pop(0)
        _sticky_memo.pop(oldest, None)
    return _sticky_memo[sticky_key][0]


def sticky_memo_size() -> int:
    """粘性锚点绑定数(观测用)。"""
    return len(_sticky_memo)


def _reset_sticky_anchor_memo() -> None:
    """清空粘性锚点绑定(仅供测试)。"""
    _sticky_memo.clear()
    _sticky_memo_order.clear()


def derive_session_key(model_name: str, gemini_request: Dict[str, Any]) -> Optional[str]:
    """
    从规范化后的 Gemini 请求派生会话 key:
    hash(模型 + systemInstruction 文本 + 前两条消息特征),经粘性首见绑定
    (sys 会话内演化不断 key,见 `resolve_sticky_anchor`)。

    必须在格式转换完成后调用（openai2gemini / anthropic2gemini / 原生 gemini 都已
    转成 Gemini 格式），保证不同 API 格式的相同内容派生相同的 key。

    Returns:
        "auto:<hash>" 或 None（无法派生时）
    """
    digest = resolve_sticky_anchor(model_name, gemini_request)
    if digest is None:
        return None
    return f"auto:{digest.hex()[:32]}"


# ====================== 绑定读写 ======================

async def get_session_binding(session_key: str) -> Optional[str]:
    """查询会话绑定的凭证 email；命中时续期 TTL。未绑定/后端故障返回 None。"""
    if not session_key:
        return None

    client = _get_client()
    if client is not None:
        redis_key = f"{_KEY_PREFIX}{session_key}"
        try:
            raw = await client.get(redis_key)
            if raw is None:
                return None
            await client.expire(redis_key, SESSION_BINDING_TTL)
            return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        except Exception as e:
            log.warning(f"Redis get 会话绑定失败: {e}")
            # Redis 故障回落内存后端

    return await _memory_get(session_key)


async def set_session_binding(session_key: str, email: str) -> None:
    """写入/刷新会话绑定（凭证亲和成功后调用）。后端故障仅告警。"""
    if not session_key or not email:
        return

    client = _get_client()
    if client is not None:
        try:
            await client.setex(
                f"{_KEY_PREFIX}{session_key}", SESSION_BINDING_TTL, email
            )
        except Exception as e:
            log.warning(f"Redis set 会话绑定失败: {e}")

    await _memory_set(session_key, email)


async def delete_session_binding(session_key: str) -> None:
    """删除会话绑定（绑定凭证不可用时清除）"""
    if not session_key:
        return

    client = _get_client()
    if client is not None:
        try:
            await client.delete(f"{_KEY_PREFIX}{session_key}")
        except Exception as e:
            log.warning(f"Redis delete 会话绑定失败: {e}")

    async with _memory_lock:
        _memory_store.pop(session_key, None)


# ====================== 内存后端 ======================

async def _memory_get(session_key: str) -> Optional[str]:
    async with _memory_lock:
        entry = _memory_store.get(session_key)
        if not entry:
            return None
        email, expire_at = entry
        if _now() >= expire_at:
            _memory_store.pop(session_key, None)
            return None
        # 命中续期
        _memory_store[session_key] = (email, _now() + SESSION_BINDING_TTL)
        return email


async def _memory_set(session_key: str, email: str) -> None:
    async with _memory_lock:
        # 容量控制：超过上限先清理过期项
        if len(_memory_store) >= _MEMORY_MAX_ENTRIES:
            _cleanup_expired_locked()
        # 仍超限则丢弃最旧的一半（按过期时间排序）
        if len(_memory_store) >= _MEMORY_MAX_ENTRIES:
            sorted_items = sorted(_memory_store.items(), key=lambda kv: kv[1][1])
            for k, _ in sorted_items[: len(sorted_items) // 2]:
                _memory_store.pop(k, None)
        _memory_store[session_key] = (email, _now() + SESSION_BINDING_TTL)


def _cleanup_expired_locked() -> None:
    """清理过期项（须持有锁时调用）"""
    now = _now()
    expired = [k for k, (_, exp) in _memory_store.items() if now >= exp]
    for k in expired:
        _memory_store.pop(k, None)


def memory_store_size() -> int:
    """内存后端条目数（诊断用）"""
    return len(_memory_store)
