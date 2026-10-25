"""
Rate Limiter - 并发控制与速率控制

全局请求限流，支持：
- 最大并发数控制（同一时刻最多处理多少个请求）
- 滑动窗口速率限制（固定时间窗口内最多处理多少个请求）

配置项可在控制面板中设置并热更新，超过限制的请求会返回 HTTP 429。
"""

import asyncio
import json
import time
from collections import deque

from config import (
    get_rate_limit_enabled,
    get_rate_limit_max_concurrent,
    get_rate_limit_requests_per_window,
    get_rate_limit_window_seconds,
)
from log import log
from src.request_pacer import request_pacer


class RateLimiter:
    """全局速率限制器（并发控制 + 滑动窗口速率）"""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._active = 0                 # 当前并发请求数
        self._timestamps: deque = deque()  # 滑动窗口内的请求时间戳
        self._enabled = False
        self._max_concurrent = 0
        self._requests_per_window = 0
        self._window_seconds = 0
        self._refresh_interval = 5.0
        self._last_refresh = 0.0

    async def refresh_config(self):
        """从配置系统读取最新配置（支持控制面板热更新）"""
        try:
            enabled = await get_rate_limit_enabled()
            max_concurrent = await get_rate_limit_max_concurrent()
            requests_per_window = await get_rate_limit_requests_per_window()
            window_seconds = await get_rate_limit_window_seconds()
        except Exception as e:
            log.debug(f"[RATE_LIMIT] 读取配置失败: {e}")
            return

        self._enabled = bool(enabled)
        self._max_concurrent = max(0, int(max_concurrent))
        self._requests_per_window = max(0, int(requests_per_window))
        self._window_seconds = max(1, int(window_seconds))
        self._last_refresh = time.monotonic()

    async def _maybe_refresh(self):
        now = time.monotonic()
        if now - self._last_refresh >= self._refresh_interval:
            await self.refresh_config()

    async def acquire(self) -> bool:
        """尝试获取一个请求配额。成功返回 True，失败返回 False（应返回 429）。"""
        await self._maybe_refresh()

        if not self._enabled:
            return True

        async with self._lock:
            # 并发控制：同一时刻最多 max_concurrent 个请求在途
            if self._max_concurrent > 0 and self._active >= self._max_concurrent:
                log.info(
                    f"[RATE_LIMIT] 并发已满 ({self._active}/{self._max_concurrent})，拒绝请求"
                )
                return False

            # 速率控制：滑动窗口内最多 requests_per_window 个请求
            if self._requests_per_window > 0:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] > self._window_seconds:
                    self._timestamps.popleft()
                if len(self._timestamps) >= self._requests_per_window:
                    log.info(
                        f"[RATE_LIMIT] 窗口内请求数已达上限 "
                        f"({len(self._timestamps)}/{self._requests_per_window}/{self._window_seconds}s)，拒绝请求"
                    )
                    return False
                self._timestamps.append(now)

            self._active += 1
            return True

    async def release(self):
        """释放一个请求配额。"""
        async with self._lock:
            if self._active > 0:
                self._active -= 1

    def release_nowait(self) -> None:
        """同步释放一个请求配额。

        供 ASGI 中间件的清理路径使用：清理阶段不能出现 await 点，否则任务被取消时
        可能既没释放并发名额、也没释放串行回合锁（回合锁会永久 locked）。
        单线程事件循环下这里只是一次自减，不需要加锁。
        """
        if self._active > 0:
            self._active -= 1

    def is_relevant(self, path: str, method: str) -> bool:
        """判断请求是否参与限流。

        仅对聊天 API（POST 请求）限流，不对模型列表、面板页面、静态资源限流。
        """
        if method != "POST":
            return False
        return (
            path.startswith("/v1/")
            or path.startswith("/v1beta/")
            or path.startswith("/antigravity/")
        )


# 全局限流器实例
rate_limiter = RateLimiter()


class RateLimitMiddleware:
    """ASGI 中间件：对所有聊天 API 请求执行并发/速率限制。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")

        if not rate_limiter.is_relevant(path, method):
            await self.app(scope, receive, send)
            return

        turn_held = False
        rate_held = False
        released_turn = False
        released_rate = False

        def release_turn_once():
            """同步归还回合锁：不包含 await，因此在被取消的清理路径上也一定会执行。"""
            nonlocal released_turn
            if turn_held and not released_turn:
                released_turn = True
                request_pacer.release_turn()

        def release_rate_once():
            """同步归还并发名额：理由同 release_turn_once。"""
            nonlocal released_rate
            if rate_held and not released_rate:
                released_rate = True
                rate_limiter.release_nowait()

        async def send_wrapper(message):
            await send(message)
            # 响应体发送完成后释放（包含流式响应的最后一帧）
            if message["type"] == "http.response.body" and not message.get("more_body"):
                # 先释放并发名额，再释放回合：避免下个排队请求醒来时并发仍满
                release_rate_once()
                release_turn_once()

        try:
            # 1. 串行模式排队：整个流程都在 try 保护之下，任何异常/取消都不会泄漏回合锁
            turn_held = await request_pacer.acquire_turn()

            # 2. 并发/速率检查
            if not await rate_limiter.acquire():
                await self._send_429(send)
                return
            rate_held = True

            await self.app(scope, receive, send_wrapper)
        finally:
            # 兜底释放：客户端断开、任务被取消或任何未捕获异常都不能让名额/回合锁泄漏。
            # 两个释放动作都是同步的（没有 await 点），因此清理过程不可能被取消打断。
            try:
                release_rate_once()
            finally:
                release_turn_once()

    async def _send_429(self, send):
        body = json.dumps(
            {
                "error": {
                    "message": "Too Many Requests - 请求过于频繁，请稍后重试",
                    "type": "rate_limit_error",
                    "code": 429,
                }
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"retry-after", str(max(1, rate_limiter._window_seconds)).encode()),
        ]
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})