"""
Request Pacer - 单账号请求节流与随机抖动（防封禁）

在每次向 Google API 发送聊天请求前，确保与上一次请求保持最小间隔，
并加上随机的抖动延迟，将请求节奏抹平滑，降低被识别为自动化突发流量的风险。

配置项可在控制面板设置并热更新。
"""

import asyncio
import random
import time
from collections import OrderedDict
from typing import Any, Dict

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
        # 按凭证维度的节流状态（key → {lock, last_sent}）
        self._keyed: Dict[str, Dict[str, Any]] = {}
        # 按凭证维度的串行回合状态（§10.16 增强三，key → {lock, last_finished}）。
        # 真实客户端的串行语义是"每个客户端实例内串行"——回合锁必须按凭证取用；
        # key="__global__" 的条目即旧的全局回合锁（acquire_turn/release_turn
        # 的薄包装保留给兼容调用方与测试）。
        self._keyed_turns: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._KEYED_TURNS_MAX = 4096

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

    async def pace_for(self, key: str) -> None:
        """按凭证维度的最小间隔 + 随机抖动。

        与全局 ``pace()`` 的区别：多个账号之间不再互相拖慢，也不会因为别的账号
        空闲而让某个账号产生突发。串行模式下由中间件的回合锁统一控制，这里跳过。
        """
        await self._maybe_refresh()

        if not self._enabled or self._serial_mode:
            return

        state = self._keyed.setdefault(
            key or "default", {"lock": asyncio.Lock(), "last_sent": None}
        )
        async with state["lock"]:
            now = time.monotonic()
            wait = 0.0
            last_sent = state["last_sent"]
            if last_sent is not None:
                elapsed = now - last_sent
                if elapsed < self._min_interval:
                    wait = self._min_interval - elapsed
            wait += random.uniform(0.0, self._jitter)
            if wait > 0:
                await asyncio.sleep(wait)
            state["last_sent"] = time.monotonic()

    async def acquire_turn(self) -> bool:
        """[遗留兼容] 全局串行回合锁——API 层已改用 ``acquire_turn_for``（§10.16
        增强三：串行语义是每客户端实例内串行，全局锁会让一个账号的慢请求阻塞
        所有账号）。本方法保留给测试与外部调用方，行为不变。

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

    # ==================== 按键回合锁（§10.16 增强三） ====================

    def _keyed_turn_state(self, key: str) -> Dict[str, Any]:
        """取出（或创建）按键回合状态并做容量维护（持锁调用不必要：单线程事件
        循环下 setdefault/淘汰是原子的；淘汰只清"未持有"的条目）。"""
        state = self._keyed_turns.get(key)
        if state is None:
            state = {"lock": asyncio.Lock(), "last_finished": None}
            self._keyed_turns[key] = state
            while len(self._keyed_turns) > self._KEYED_TURNS_MAX:
                for stale in list(self._keyed_turns):
                    if not self._keyed_turns[stale]["lock"].locked():
                        self._keyed_turns.pop(stale)
                        break
                else:
                    break  # 全部在持有中，放弃淘汰
        return state

    async def acquire_turn_for(self, key: str) -> bool:
        """串行模式（按键/按凭证）：同一 key 同一时刻只有一个在途请求。

        与全局版语义一致：按上一个【同 key 请求结束时刻】起算最小间隔 + 抖动。
        返回 True 表示已持有回合，请求结束后必须 release_turn_for(key)；
        串行模式关闭时返回 False（无需释放）。

        取消安全：等待间隔/抖动期间被取消（客户端断开、服务停止）时立即归还
        该 key 的回合锁，且不更新 last_finished（该请求从未真正执行，不应
        重置串行时钟）。
        """
        await self._maybe_refresh()

        if not self._serial_mode:
            return False

        state = self._keyed_turn_state(key or "__global__")
        try:
            await state["lock"].acquire()
        except RuntimeError:
            # 锁绑定的事件循环已更换（测试的 asyncio.run 每次新建 loop、或嵌入式
            # 场景）：重建该 key 的状态（保留串行计时）后重试一次。
            state = {
                "lock": asyncio.Lock(),
                "last_finished": state.get("last_finished"),
            }
            self._keyed_turns[key or "__global__"] = state
            await state["lock"].acquire()

        try:
            wait = 0.0
            now = time.monotonic()
            if state["last_finished"] is not None:
                elapsed = now - state["last_finished"]
                if elapsed < self._min_interval:
                    wait = self._min_interval - elapsed
            wait += random.uniform(0.0, self._jitter)

            if wait > 0:
                last_delta = (
                    (now - state["last_finished"]) if state["last_finished"] else 0.0
                )
                log.debug(
                    f"[REQUEST_PACER][SERIAL:{key}] 距上次结束 {last_delta:.2f}s，"
                    f"等待 {wait:.2f}s (最小间隔 {self._min_interval}s, 抖动 {self._jitter}s)"
                )
                await asyncio.sleep(wait)

            return True
        except BaseException:
            # 与全局版同一取消安全语义：只归还锁本身，不更新 last_finished
            log.debug(f"[REQUEST_PACER][SERIAL:{key}] 等待间隔期间被取消/异常，已归还回合锁")
            if state["lock"].locked():
                state["lock"].release()
            raise

    def release_turn_for(self, key: str) -> None:
        """串行模式（按键）：请求【完成】（含流式结束/失败/客户端断开）后调用。"""
        state = self._keyed_turns.get(key or "__global__")
        if state is None:
            return
        state["last_finished"] = time.monotonic()
        if state["lock"].locked():
            state["lock"].release()

    def turn_locked_for(self, key: str) -> bool:
        """该 key 的回合锁当前是否被持有（观测/测试用）。"""
        state = self._keyed_turns.get(key or "__global__")
        return bool(state and state["lock"].locked())

    def reset_keyed_turns(self) -> None:
        """清空全部按键回合状态（测试用）。"""
        self._keyed_turns.clear()


# 全局节流器实例
request_pacer = RequestPacer()