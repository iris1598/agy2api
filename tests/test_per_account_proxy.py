"""每账号独立出口（proxy）与共享客户端缓存回归测试。

覆盖两件事：

1. 代理 URL 校验（全局 PROXY 与面板「设置代理」共用同一规则）；
2. AsyncClient 缓存按代理区分 + 全局代理热更新不误伤账号代理客户端。

第 2 点是关键修复：旧实现只要指纹不匹配就关闭**所有**其他客户端，
多账号多代理交替使用时每次请求都会销毁重建连接池（TLS 握手风暴），
反而更像机器人，直接抵消「每账号独立出口」的收益。

用法::

    .venv\\Scripts\\python.exe -m pytest tests\\test_per_account_proxy.py -v
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import validate_proxy_url  # noqa: E402
from src import httpx_client  # noqa: E402
from src.httpx_client import (  # noqa: E402
    _get_shared_client,
    cached_client_count,
    close_shared_clients,
)

PROXY_A = "socks5://127.0.0.1:1080"
PROXY_B = "http://127.0.0.1:8080"


def _reset() -> None:
    asyncio.run(close_shared_clients())


# ---------------------------------------------------------------------------
# 1. 代理 URL 校验
# ---------------------------------------------------------------------------
def test_validate_proxy_schemes_accepted() -> None:
    for url in (
        "http://127.0.0.1:8080",
        "https://127.0.0.1:8443",
        "socks5://127.0.0.1:1080",
        "socks5h://127.0.0.1:1080",
        "socks5://user:pass@10.0.0.1:1080",
    ):
        assert validate_proxy_url(url) == url, f"应接受 {url}"


def test_validate_proxy_rejects_unsupported_scheme() -> None:
    assert validate_proxy_url("ftp://127.0.0.1:21") is None


def test_validate_proxy_rejects_empty_and_garbage() -> None:
    assert validate_proxy_url("") is None
    assert validate_proxy_url("   ") is None
    assert validate_proxy_url(None) is None
    # 无 scheme 的裸串无法判定代理类型
    assert validate_proxy_url("127.0.0.1:1080") is None


def test_validate_proxy_strips_whitespace() -> None:
    assert validate_proxy_url("  socks5://127.0.0.1:1080  ") == PROXY_A


# ---------------------------------------------------------------------------
# 2. 客户端缓存
# ---------------------------------------------------------------------------
def test_distinct_proxies_get_distinct_clients() -> None:
    async def run():
        try:
            client_a = await _get_shared_client(PROXY_A)
            client_b = await _get_shared_client(PROXY_B)
            assert client_a is not client_b, "不同代理必须使用不同客户端"

            # 重复取同一代理应复用同一实例
            again_a = await _get_shared_client(PROXY_A)
            assert again_a is client_a, "同一代理应复用同一客户端"
        finally:
            await close_shared_clients()

    asyncio.run(run())


def test_global_proxy_refresh_keeps_account_clients() -> None:
    """全局代理热更新时，账号代理客户端必须保持不变（核心修复断言）。"""

    async def run():
        original = httpx_client.get_proxy_config
        current = {"value": None}

        async def fake_get_proxy_config():
            return current["value"]

        httpx_client.get_proxy_config = fake_get_proxy_config
        try:
            account_a = await _get_shared_client(PROXY_A)

            # 全局代理从「无」切到 B，再切到 A
            current["value"] = PROXY_B
            await _get_shared_client(None)
            current["value"] = PROXY_A
            await _get_shared_client(None)

            # 账号代理客户端仍在缓存中，且与全局客户端互不干扰
            # （即使全局也用了 PROXY_A，两者仍分属不同命名空间）
            assert await _get_shared_client(PROXY_A) is account_a
            global_client = await _get_shared_client(None)
            assert account_a is not global_client
            assert httpx_client._global_proxy_key == "g:" + PROXY_A

            # 全局切回直连，账号代理客户端依旧存活
            current["value"] = None
            await _get_shared_client(None)
            assert await _get_shared_client(PROXY_A) is account_a
        finally:
            httpx_client.get_proxy_config = original
            await close_shared_clients()

    asyncio.run(run())


def test_global_proxy_change_closes_old_global_client() -> None:
    """全局代理变化必须淘汰旧全局客户端（避免连接池泄漏）。"""

    async def run():
        original = httpx_client.get_proxy_config
        current = {"value": None}

        async def fake_get_proxy_config():
            return current["value"]

        httpx_client.get_proxy_config = fake_get_proxy_config
        try:
            first = await _get_shared_client(None)
            current["value"] = "http://127.0.0.1:9999"
            second = await _get_shared_client(None)
            assert first is not second, "全局代理变化后必须重建客户端"
        finally:
            httpx_client.get_proxy_config = original
            await close_shared_clients()

    asyncio.run(run())


def test_cache_evicts_lru_beyond_limit() -> None:
    """超过上限时淘汰最久未用的非保护条目。"""

    async def run():
        original_max = httpx_client._MAX_CACHED_CLIENTS
        httpx_client._MAX_CACHED_CLIENTS = 3
        try:
            # 用直连 + 3 个账号代理，共 4 条，应淘汰最久未用的一条
            await _get_shared_client(None)
            for i in range(3):
                await _get_shared_client(f"http://127.0.0.1:{10000 + i}")

            assert cached_client_count() <= 3, (
                f"缓存不应超过上限，实际 {cached_client_count()}"
            )
        finally:
            httpx_client._MAX_CACHED_CLIENTS = original_max
            await close_shared_clients()

    asyncio.run(run())


def test_close_shared_clients_clears_cache() -> None:
    async def run():
        await _get_shared_client(PROXY_A)
        await _get_shared_client(PROXY_B)
        assert cached_client_count() > 0

        await close_shared_clients()
        assert cached_client_count() == 0, "关闭后缓存应为空"

    asyncio.run(run())
