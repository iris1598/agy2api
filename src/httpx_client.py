"""
通用的HTTP客户端模块
为所有需要使用httpx的模块提供统一的客户端配置和方法
保持通用性，不与特定业务逻辑耦合

- 共享 AsyncClient（按键缓存：代理 URL，空串表示直连），复用 TCP/TLS 连接，
  避免每请求新建客户端造成的握手开销
- 支持每账号独立出口：请求级传入 proxy 时使用该代理对应的客户端，
  全局 PROXY 配置变化不会影响账号代理客户端（反之亦然）
- 缓存按 LRU 淘汰，避免多账号多代理场景下反复重建连接池
- 超时等请求级参数在每次请求时传入，不受客户端实例缓存影响
"""

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx
import json
from fastapi import Response

from config import (
    get_antigravity_api_url,
    get_proxy_config,
    get_stream_read_timeout,
    get_upstream_tls_impersonation_enabled,
    get_upstream_tls_proxy_url,
)
from log import log
from src import upstream_proxy
from src.fidelity import FidelityError

# ====================== 共享客户端 ======================

# 客户端缓存：key 为命名空间前缀 + 代理 URL（"" 表示直连）
#   "g:" 全局 PROXY 配置对应的客户端（同一时刻只有一个）
#   "a:" 账号级代理对应的客户端（可多个）
# 两个命名空间刻意分开：同一代理 URL 既被全局又被某账号使用时，
# 全局热更新不得连带关闭该账号的客户端。
_clients: "OrderedDict[str, httpx.AsyncClient]" = OrderedDict()
_clients_lock = asyncio.Lock()

# 当前全局代理对应的 key（全局代理热更新时只淘汰该条目）
_global_proxy_key: Optional[str] = None

# 缓存的客户端数量上限（超过后按 LRU 淘汰，全局条目始终受保护）
_MAX_CACHED_CLIENTS = 64

_GLOBAL_PREFIX = "g:"
_ACCOUNT_PREFIX = "a:"


def _client_fingerprint(proxy_url: Optional[str]) -> str:
    """客户端缓存键的代理部分：代理 URL 不同则不能复用同一客户端"""
    return proxy_url or ""


def _build_key(proxy_url: Optional[str], is_global: bool) -> str:
    prefix = _GLOBAL_PREFIX if is_global else _ACCOUNT_PREFIX
    return prefix + _client_fingerprint(proxy_url)


async def _build_client(proxy_url: Optional[str]) -> httpx.AsyncClient:
    """按代理配置构建客户端"""
    client_kwargs: Dict[str, Any] = {"timeout": None}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    return httpx.AsyncClient(**client_kwargs)


async def _safe_aclose(client: httpx.AsyncClient) -> None:
    """安全关闭客户端，异常仅告警"""
    try:
        await client.aclose()
    except Exception as e:
        log.warning(f"关闭旧 httpx 客户端失败: {e}")


def _schedule_close(clients: List[httpx.AsyncClient]) -> None:
    """惰性关闭一批客户端，不阻塞当前请求"""
    for old in clients:
        try:
            asyncio.create_task(_safe_aclose(old))
        except Exception as e:
            log.warning(f"调度旧客户端关闭失败: {e}")


def _evict_lru_locked() -> List[httpx.AsyncClient]:
    """超限时淘汰最久未用的条目（须持有锁）。返回被淘汰的客户端列表。

    全局条目始终受保护（它会被每个未配置账号代理的请求复用）；
    账号条目按 LRU 淘汰，下次用到时重建（代价是一次握手）。
    """
    evicted: List[httpx.AsyncClient] = []
    while len(_clients) > _MAX_CACHED_CLIENTS:
        victim_key = None
        for key in _clients:
            if key == _global_proxy_key or key == _SIDECAR_KEY:
                continue
            victim_key = key
            break
        if victim_key is None:
            break
        evicted.append(_clients.pop(victim_key))
    return evicted


async def _get_shared_client(proxy: Optional[str] = None) -> httpx.AsyncClient:
    """
    获取共享的 AsyncClient（按键缓存复用）。

    Args:
        proxy: None 表示沿用全局 PROXY 配置；非 None 表示使用该账号代理
               （空串等同直连）。

    调用方不得关闭该客户端（由模块统一管理生命周期，
    web.py 应用关闭时调用 close_shared_clients）。
    """
    global _global_proxy_key

    if proxy is None:
        proxy_url = await get_proxy_config()
        is_global = True
    else:
        proxy_url = proxy or None
        is_global = False

    key = _build_key(proxy_url, is_global)

    # 快路径：命中直接复用（无锁读）
    client = _clients.get(key)
    if client is not None:
        _clients.move_to_end(key)
        return client

    async with _clients_lock:
        # 双重检查（并发下可能已被其他请求创建）
        client = _clients.get(key)
        if client is not None:
            _clients.move_to_end(key)
            return client

        stale: List[httpx.AsyncClient] = []
        if is_global:
            # 全局代理热更新：只淘汰旧的全局条目，账号条目不受影响
            old_global = _global_proxy_key
            if old_global is not None and old_global != key and old_global in _clients:
                stale.append(_clients.pop(old_global))
            _global_proxy_key = key

        new_client = await _build_client(proxy_url)
        _clients[key] = new_client
        _clients.move_to_end(key)

        stale.extend(_evict_lru_locked())

        # 惰性关闭淘汰的客户端，不阻塞当前请求
        if stale:
            _schedule_close(stale)

        return new_client


# ====================== Go 出站 TLS 转发器 ======================

_SIDECAR_KEY = "s:"


def _build_sidecar_request(
    url: str, sidecar_base: str, proxy: Optional[str]
) -> Tuple[str, Dict[str, str]]:
    """把原始上游 URL 改写成发往本机 Go 转发器的 URL + 透传头（纯函数）。"""
    target = urlsplit(url)
    base = urlsplit(sidecar_base)
    path = target.path or "/"
    new_url = f"{base.scheme}://{base.netloc}{path}"
    if target.query:
        new_url += "?" + target.query
    extra = {"X-Upstream-URL": url}
    if proxy:
        extra["X-Upstream-Proxy"] = proxy
    return new_url, extra


async def _get_sidecar_client() -> httpx.AsyncClient:
    """访问本机转发器的客户端（禁用环境变量代理，避免把回环请求绕出去）。"""
    client = _clients.get(_SIDECAR_KEY)
    if client is not None:
        _clients.move_to_end(_SIDECAR_KEY)
        return client

    async with _clients_lock:
        client = _clients.get(_SIDECAR_KEY)
        if client is not None:
            _clients.move_to_end(_SIDECAR_KEY)
            return client
        new_client = httpx.AsyncClient(timeout=None, trust_env=False)
        _clients[_SIDECAR_KEY] = new_client
        _clients.move_to_end(_SIDECAR_KEY)
        stale = _evict_lru_locked()
        if stale:
            _schedule_close(stale)
        return new_client


async def _impersonation_request(
    url: str, proxy: Optional[str]
) -> Optional[Tuple[str, Dict[str, str]]]:
    """若启用 Go 转发器且目标是 Antigravity API，返回改写后的请求参数。"""
    if not await get_upstream_tls_impersonation_enabled():
        return None
    target = urlsplit(url)
    if target.scheme != "https":
        return None
    api_host = urlsplit(await get_antigravity_api_url()).hostname
    if not api_host or target.hostname != api_host:
        return None

    sidecar = await get_upstream_tls_proxy_url()
    if not await asyncio.to_thread(upstream_proxy.ensure_running, sidecar):
        # 严格模式：没有可用的 Go 出站核心就直接失败，不回退 httpx。
        raise FidelityError(
            "tls_sidecar_unavailable",
            "Go 转发器未就绪（未编译/启动失败/端口不可用）",
        )
    return _build_sidecar_request(url, sidecar, proxy)


async def close_shared_clients() -> None:
    """关闭所有共享客户端（应用关闭时调用）"""
    global _global_proxy_key
    async with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
        _global_proxy_key = None
    for client in clients:
        await _safe_aclose(client)


def cached_client_count() -> int:
    """缓存中的客户端数量（诊断用）"""
    return len(_clients)


# ====================== 客户端获取（兼容旧接口） ======================

@asynccontextmanager
async def get_client(
    timeout: float = 30.0, proxy: Optional[str] = None, **kwargs
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """获取配置好的异步HTTP客户端（共享复用）"""
    yield await _get_shared_client(proxy)


async def _get_stream_timeout() -> Optional[httpx.Timeout]:
    """构建流式请求的分阶段超时（防上游挂起死锁）。

    - read 超时来自配置 STREAM_READ_TIMEOUT（默认 300s，0=禁用），
      作用于「相邻两次网络读取之间」的空闲时长。上游流中途卡死
      （连接保持但不再发数据）超过该时长即抛出 httpx.ReadTimeout，
      否则串行模式下的回合锁 / 限流并发名额会被永久占用，形成死锁。
    - connect/write/pool 使用固定 30s。
    只要上游持续有数据到达，read 计时就会不断重置，
    正常的超长流式生成不受影响。

    read_timeout <= 0：用户显式禁用，返回 None（无限等待）。
    """
    read_timeout = await get_stream_read_timeout()
    if read_timeout <= 0:
        return None
    return httpx.Timeout(
        connect=30.0,
        read=read_timeout,
        write=30.0,
        pool=30.0,
    )


@asynccontextmanager
async def get_streaming_client(
    timeout: float = None, proxy: Optional[str] = None, **kwargs
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """获取用于流式请求的HTTP客户端（共享复用）"""
    yield await _get_shared_client(proxy)


# ====================== 异步请求方法 ======================

async def get_async(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
    proxy: Optional[str] = None,
    **kwargs,
) -> httpx.Response:
    """通用异步GET请求"""
    client = await _get_shared_client(proxy)
    return await client.get(url, headers=headers, timeout=timeout, **kwargs)


async def post_async(
    url: str,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 900.0,
    proxy: Optional[str] = None,
    **kwargs,
) -> httpx.Response:
    """通用异步POST请求"""
    impersonated = await _impersonation_request(url, proxy)
    if impersonated is not None:
        new_url, extra = impersonated
        merged = {**(headers or {}), **extra}
        sidecar = await _get_sidecar_client()
        try:
            return await sidecar.post(
                new_url, data=data, json=json, headers=merged, timeout=timeout, **kwargs
            )
        except httpx.HTTPError as exc:
            # 严格模式：不做直连回退（换出口/换 TLS 栈都会破坏一致性）。
            raise FidelityError(
                "tls_sidecar_unavailable", f"转发器请求失败: {type(exc).__name__}: {exc}"
            ) from exc

    client = await _get_shared_client(proxy)
    return await client.post(
        url, data=data, json=json, headers=headers, timeout=timeout, **kwargs
    )


# 调试用：设为 True 时所有流式请求都返回 429
_MOCK_STREAM_429 = False

async def stream_post_async(
    url: str,
    body: Dict[str, Any],
    native: bool = False,
    headers: Optional[Dict[str, str]] = None,
    proxy: Optional[str] = None,
    **kwargs,
):
    """流式异步POST请求"""
    if _MOCK_STREAM_429:
        log.warning(f"[MOCK] stream_post_async: 返回模拟429错误")
        yield Response(
            content=json.dumps({"error": {"code": 429, "message": "mock rate limit", "status": "RESOURCE_EXHAUSTED"}}),
            status_code=429,
        )
        return

    # 流式请求默认启用分阶段超时（防上游挂起导致回合锁/并发名额永久占用）；
    # 调用方显式传入 timeout 时以调用方为准。
    if "timeout" not in kwargs:
        kwargs["timeout"] = await _get_stream_timeout()

    # 优先经 Go 转发器出站（指纹对齐）；转发器不可用时抛 FidelityError，
    # 不做 httpx 回退（宁可报错，不静默降级）。
    impersonated = await _impersonation_request(url, proxy)
    if impersonated is not None:
        new_url, extra = impersonated
        merged = {**(headers or {}), **extra}
        sidecar = await _get_sidecar_client()
        stream_ctx = sidecar.stream("POST", new_url, json=body, headers=merged, **kwargs)
        try:
            r = await stream_ctx.__aenter__()
        except httpx.HTTPError as exc:
            raise FidelityError(
                "tls_sidecar_unavailable",
                f"转发器流式连接失败: {type(exc).__name__}: {exc}",
            ) from exc
        try:
            async for item in _iter_stream(r, native):
                yield item
        finally:
            await stream_ctx.__aexit__(None, None, None)
        return

    client = await _get_shared_client(proxy)
    async with client.stream(
        "POST", url, json=body, headers=headers, **kwargs
    ) as r:
        async for item in _iter_stream(r, native):
            yield item


async def _iter_stream(r: httpx.Response, native: bool) -> AsyncGenerator[Any, None]:
    """把一个上游流响应转换成 antigravity 层期望的产出（错误 Response / bytes / 文本行）。"""
    if r.status_code != 200:
        yield Response(await r.aread(), r.status_code, dict(r.headers))
        return

    if native:
        async for chunk in r.aiter_bytes():
            yield chunk
    else:
        async for line in r.aiter_lines():
            yield line

