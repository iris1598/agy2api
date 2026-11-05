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
    get_request_body_timeout,
)
from log import log
from src.request_pacer import request_pacer
from starlette.exceptions import HTTPException as StarletteHTTPException


class InboundBodyTimeout(StarletteHTTPException):
    """入站请求体读取超时：客户端长时间未发送完整请求体（块间空闲超过上限）。

    继承 HTTPException(408) 而不是裸 Exception：FastAPI 对「声明式请求体解析」
    阶段的异常有宽泛的 except Exception 兜底（转成 400 "error parsing the
    body"），但会原样放行 HTTPException，由 ExceptionMiddleware 转成标准 408
    响应。端点内手动读体（如 count_tokens）时该异常则由本中间件捕获后补发 408。
    """


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
        # 请求体阶段结束的标志：读完整（more_body=False）、感知到断开、或响应已开始。
        # 超时保护只覆盖请求体阶段；响应阶段 receive 的语义完全不同（断开探测）。
        body_phase_done = False
        response_started = False

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
            nonlocal response_started, body_phase_done
            if message["type"] == "http.response.start":
                response_started = True
                # 防御：若端点未读体就直接响应（或后续用 receive 探测断开），
                # 请求体阶段视为已结束，禁止再触发入站超时
                body_phase_done = True
            await send(message)
            # 响应体发送完成后释放（包含流式响应的最后一帧）
            if message["type"] == "http.response.body" and not message.get("more_body"):
                # 先释放并发名额，再释放回合：避免下个排队请求醒来时并发仍满
                release_rate_once()
                release_turn_once()

        # 入站请求体分块读超时（默认 30s，0=禁用）：语义等价于 nginx 的
        # client_body_timeout——只要请求体还在陆续到达就不断重置计时，
        # 仅在链路停滞时中断，防止串行回合锁/并发名额被半截请求永久占用。
        body_timeout = await get_request_body_timeout()

        async def receive_with_body_timeout():
            nonlocal body_phase_done
            if body_phase_done or body_timeout <= 0:
                return await receive()
            try:
                message = await asyncio.wait_for(receive(), timeout=body_timeout)
            except asyncio.TimeoutError:
                log.warning(
                    f"[RATE_LIMIT] 入站请求体读取超时（{body_timeout}s 无数据到达），"
                    f"中断请求: {method} {path}"
                )
                # HTTPException 子类：声明式 body 解析路径下由 ExceptionMiddleware
                # 发送 408；其余路径由下方 except 捕获后经 _send_408 发送
                raise InboundBodyTimeout(
                    status_code=408,
                    detail="Request Timeout - 客户端长时间未发送完整请求体，连接已中断",
                    headers={"connection": "close"},
                ) from None
            if message["type"] == "http.disconnect" or (
                message["type"] == "http.request" and not message.get("more_body", False)
            ):
                body_phase_done = True
            return message

        try:
            # 1. 串行模式排队：整个流程都在 try 保护之下，任何异常/取消都不会泄漏回合锁
            turn_held = await request_pacer.acquire_turn()

            # 2. 并发/速率检查
            if not await rate_limiter.acquire():
                await self._send_429(send)
                return
            rate_held = True

            try:
                await self.app(scope, receive_with_body_timeout, send_wrapper)
            except InboundBodyTimeout:
                # 声明式 body 解析路径下 FastAPI 会放行 HTTPException，由
                # ExceptionMiddleware 先发 408（响应已开始）；仅当异常未被内层
                # 处理（如端点手动读体且未吞掉）时才在这里补发 408
                if not response_started:
                    await self._send_408(send)
                else:  # pragma: no cover - 防御路径：响应已开始时不可再发响应头
                    raise
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

    async def _send_408(self, send):
        body = json.dumps(
            {
                "error": {
                    "message": "Request Timeout - 客户端长时间未发送完整请求体，连接已中断",
                    "type": "request_timeout",
                    "code": 408,
                }
            },
            ensure_ascii=False,
        ).encode("utf-8")
        # connection: close：请求体不完整时该连接无法复用，明确告知客户端关闭
        headers = [
            (b"content-type", b"application/json"),
            (b"connection", b"close"),
        ]
        await send({"type": "http.response.start", "status": 408, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})