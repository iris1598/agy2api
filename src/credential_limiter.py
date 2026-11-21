"""按凭证的并发闸门。

真实用户不会同时对同一账号发起大量 agent 流；反代场景下多个客户端可能共用
一个凭证，因此必须在本地按凭证排队，而不是靠"换号继续冲"。

与 ``request_pacer``（间隔+抖动）互补：这里只限制在途数量。
"""

import asyncio
from typing import Dict

from config import get_credential_max_concurrent
from log import log


class CredentialLimiter:
    """按凭证维护的 Semaphore 池（上限可热更新）。"""

    def __init__(self) -> None:
        self._sems: Dict[str, asyncio.Semaphore] = {}
        self._limits: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def _get_semaphore(self, key: str) -> asyncio.Semaphore:
        limit = await get_credential_max_concurrent()
        if limit <= 0:
            return None  # type: ignore[return-value]

        async with self._lock:
            sem = self._sems.get(key)
            if sem is not None and self._limits.get(key) == limit:
                return sem
            # 上限变化时重建（旧 Semaphore 上已持有的名额会自然释放到旧对象，
            # 影响仅限过渡期，可接受）。
            sem = asyncio.Semaphore(limit)
            self._sems[key] = sem
            self._limits[key] = limit
            return sem

    async def acquire(self, key: str) -> bool:
        """获取一个在途名额；返回 False 表示未启用限制。"""
        sem = await self._get_semaphore(key or "default")
        if sem is None:
            return False
        if sem.locked():
            log.debug(f"[CREDENTIAL LIMIT] 凭证 {key} 已达并发上限，排队等待")
        await sem.acquire()
        return True

    def release(self, key: str) -> None:
        sem = self._sems.get(key or "default")
        if sem is not None:
            sem.release()


credential_limiter = CredentialLimiter()
