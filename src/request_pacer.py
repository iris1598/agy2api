"""
Request Pacer - 单账号请求节流与随机抖动（防封禁）

在每次向 Google API 发送聊天请求前，确保与上一次请求保持最小间隔，
并加上随机的抖动延迟，将请求节奏抹平滑，降低被识别为自动化突发流量的风险。

配置项可在控制面板设置并热更新。
"""

import asyncio
import random
import time

from config import (
    get_request_throttle_enabled,
    get_request_min_interval,
    get_request_jitter,
    get_request_serial_enabled,
)
from log import log


class RequestPacer:
    """单账号请求节流器（最小间隔 + 随机抖动 + 可选串行模式）"""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._enabled = False
        self._serial_mode = False
        self._min_interval = 0.0
        self._jitter = 0.0
        self._last_sent = None
        self._last_finished = None
        self._turn_lock = asyncio.Lock()
        self._refresh_interval = 5.0
        self._last_refresh = 0.0

    async def refresh_config(self):
        """从配置系统读取最新配置（支持控制面板热更新）"""
        try:
            enabled = await get_request_throttle_enabled()
            serial_enabled = await get_request_serial_enabled()
            min_interval = await get_request_min_interval()
            jitter = await get_request_jitter()
        except Exception as e:
            log.debug(f"[REQUEST_PACER] 读取配置失败: {e}")
            return

        self._enabled = bool(enabled)
        self._serial_mode = bool(serial_enabled)
        self._min_interval = max(0.0, float(min_interval))
        self._jitter = max(0.0, float(jitter))
        self._last_refresh = time.monotonic()

    async def _maybe_refresh(self):
        now = time.monotonic()
        if now - self._last_refresh >= self._refresh_interval:
            await self.refresh_config()

    async def pace(self):
        """在发送请求前调用（非串行模式）：按上一个请求的发送时刻起算最小间隔 + 随机抖动。"""
        await self._maybe_refresh()

        if not self._enabled:
            return
        # 串行模式下间隔由 acquire_turn 按“上一个请求结束时刻”起算，这里不再重复节流
        if self._serial_mode:
            return

        # 加锁让并发请求串行排队，保证请求间隔真正生效
        async with self._lock:
            now = time.monotonic()
            wait = 0.0
            if self._last_sent is not None:
                elapsed = now - self._last_sent
                if elapsed < self._min_interval:
                    wait = self._min_interval - elapsed
            wait += random.uniform(0.0, self._jitter)

            if wait > 0:
                last_delta = (now - self._last_sent) if self._last_sent else 0.0
                log.debug(
                    f"[REQUEST_PACER] 距上次发送 {last_delta:.2f}s，"
                    f"等待 {wait:.2f}s (最小间隔 {self._min_interval}s, 抖动 {self._jitter}s)"
                )
                await asyncio.sleep(wait)

            self._last_sent = time.monotonic()

    async def acquire_turn(self) -> bool:
        """串行模式：请求开始前调用，等待上一个请求结束后再进入。

        按上一个请求的【结束时刻】起算最小间隔 + 随机抖动。
        返回 True 表示已持有“回合”（调用方需在请求结束后调用 release_turn）。

        只取决于串行开关本身：控制面板里「串行模式」是独立勾选项，
        之前要求同时打开「请求节流」才会生效，导致只勾串行时静默无效。
        """
        await self._maybe_refresh()

        if not self._serial_mode:
            return False

        await self._turn_lock.acquire()

        try:
            # 距上一个请求结束的间隔 + 随机抖动
            wait = 0.0
            now = time.monotonic()
            if self._last_finished is not None:
                elapsed = now - self._last_finished
                if elapsed < self._min_interval:
                    wait = self._min_interval - elapsed
            wait += random.uniform(0.0, self._jitter)

            if wait > 0:
                last_delta = (now - self._last_finished) if self._last_finished else 0.0
                log.debug(
                    f"[REQUEST_PACER][SERIAL] 距上次结束 {last_delta:.2f}s，"
                    f"等待 {wait:.2f}s (最小间隔 {self._min_interval}s, 抖动 {self._jitter}s)"
                )
                await asyncio.sleep(wait)

            return True
        except BaseException:
            # 关键：在等待间隔/抖动期间被取消（客户端断开、服务停止、任务被 cancel）
            # 或发生任何异常时，必须立刻归还回合锁；否则 _turn_lock 永久 locked，
            # 后续所有请求都会卡死在 acquire() 上，只能重启进程。
            # 注意：这里只归还锁本身，不更新 _last_finished —— 该请求从未真正执行，
            # 不应被记作“刚有一个请求结束”，否则串行时钟会被取消风暴不断重置。
            log.debug("[REQUEST_PACER][SERIAL] 等待间隔期间被取消/异常，已归还回合锁")
            self._release_turn_lock()
            raise

    def _release_turn_lock(self):
        """仅归还回合锁本身（不更新串行计时），供 acquire 失败/被取消的清理路径使用。"""
        if self._turn_lock.locked():
            self._turn_lock.release()

    def release_turn(self):
        """串行模式：请求【完成】（含流式结束/失败/客户端断开）后调用。"""
        self._last_finished = time.monotonic()
        self._release_turn_lock()


# 全局节流器实例
request_pacer = RequestPacer()