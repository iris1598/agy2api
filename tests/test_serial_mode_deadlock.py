"""串行模式（REQUEST_SERIAL_ENABLED）+ 限流中间件的回合锁泄漏回归测试。

覆盖三类曾导致「回合锁泄漏 → 之后所有请求永久阻塞」的缺陷：

1. ``RequestPacer.acquire_turn()`` 在 ``asyncio.sleep(wait)`` 等待最小间隔/抖动期间
   被取消（CancelledError）或抛异常，回合锁未归还；
2. ``RateLimitMiddleware`` 在 ``rate_limiter.acquire()`` 阶段抛异常时尚未进入
   try/finally，回合锁未归还（``_send_429()`` 阶段旧代码已提前显式释放回合，
   因此那里不是泄漏点，本文件仍保留一条守卫用例）；
3. 兜底清理路径中释放并发名额失败（或再次被取消）时，同步的
   ``release_turn()`` 仍必须执行。

另外包含串行模式「严格排队、不重叠」的正向功能测试，确保修复没有破坏原有语义。

用法（无需 pytest）::

    .venv\\Scripts\\python.exe tests\\test_serial_mode_deadlock.py

也兼容 pytest：每个 ``test_*`` 都是同步函数，内部用 ``asyncio.run`` 驱动。
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 让配置走环境变量分支，测试不依赖数据库里的实际值
os.environ["REQUEST_THROTTLE_ENABLED"] = "true"
os.environ["REQUEST_SERIAL_ENABLED"] = "true"
os.environ["REQUEST_MIN_INTERVAL"] = "5"
os.environ["REQUEST_JITTER"] = "0"

from src.rate_limiter import RateLimitMiddleware, rate_limiter  # noqa: E402
from src.request_pacer import request_pacer  # noqa: E402

SCOPE = {"type": "http", "path": "/v1/chat/completions", "method": "POST"}


def _force_clean() -> None:
    """把两个全局单例恢复到干净状态，保证用例之间互不影响。

    注意：``asyncio.Lock`` 在首次 acquire 时会绑定当时的事件循环，而每个用例都由
    独立的 ``asyncio.run`` 驱动，因此这里必须重建锁对象，否则会报
    "is bound to a different event loop"（这是测试夹具问题，不是产品缺陷）。
    """
    pacer = request_pacer
    pacer._lock = asyncio.Lock()
    pacer._turn_lock = asyncio.Lock()
    pacer._last_finished = None
    pacer._last_sent = None
    pacer._last_refresh = time.monotonic()

    rate_limiter._lock = asyncio.Lock()
    rate_limiter._active = 0
    rate_limiter._timestamps.clear()
    rate_limiter._last_refresh = time.monotonic()


async def _enable_serial(min_interval: float = 0.0, jitter: float = 0.0) -> None:
    await request_pacer.refresh_config()
    request_pacer._enabled = True
    request_pacer._serial_mode = True
    request_pacer._min_interval = min_interval
    request_pacer._jitter = jitter
    request_pacer._last_finished = None
    request_pacer._last_refresh = time.monotonic()

    rate_limiter._enabled = False
    rate_limiter._max_concurrent = 0
    rate_limiter._requests_per_window = 0
    rate_limiter._active = 0
    rate_limiter._last_refresh = time.monotonic()


async def _noop_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok", "more_body": False})


async def _acquire_turn_must_succeed(what: str) -> None:
    """在给定超时内必须能拿到回合锁；拿不到说明锁已泄漏（死锁）。"""
    try:
        ok = await asyncio.wait_for(request_pacer.acquire_turn(), timeout=1.0)
    except asyncio.TimeoutError:
        raise AssertionError(f"{what}: 回合锁已泄漏，后续请求永久阻塞（死锁）")
    assert ok is True, f"{what}: 串行模式下 acquire_turn 应返回 True"
    request_pacer.release_turn()


# ---------------------------------------------------------------------------
# 缺陷 1：acquire_turn 在 sleep 期间被取消
# ---------------------------------------------------------------------------
async def _case_acquire_turn_cancel() -> None:
    _force_clean()
    await _enable_serial(min_interval=5.0, jitter=0.0)
    # 让 wait = min_interval = 5s，从而稳定停在 asyncio.sleep() 上
    request_pacer._last_finished = time.monotonic()

    task = asyncio.create_task(request_pacer.acquire_turn())
    await asyncio.sleep(0.2)  # 等它拿到锁并进入 sleep
    assert request_pacer._turn_lock.locked(), "前置条件：请求应已持有回合锁"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("acquire_turn 应把 CancelledError 继续抛出")

    request_pacer._last_finished = None  # 复位，使下一次 acquire 无需等待
    await _acquire_turn_must_succeed("acquire_turn 被取消后")


def test_acquire_turn_releases_lock_on_cancel() -> None:
    asyncio.run(_case_acquire_turn_cancel())


# ---------------------------------------------------------------------------
# 缺陷 1b：acquire_turn 等待期间被取消时，不应把 _last_finished 刷成当前时间
# （该请求从未真正执行，串行时钟必须仍以“上一个真正完成的请求”为准）
# ---------------------------------------------------------------------------
async def _case_cancel_does_not_reset_serial_clock() -> None:
    _force_clean()
    await _enable_serial(min_interval=5.0, jitter=0.0)

    # 模拟上一个请求在 4.9s 前结束（距 min_interval=5s 还差 0.1s）
    original_last_finished = time.monotonic() - 4.9
    request_pacer._last_finished = original_last_finished

    task = asyncio.create_task(request_pacer.acquire_turn())
    await asyncio.sleep(0.2)  # 拿到锁并进入 sleep（约需再等 0.1s+）
    assert request_pacer._turn_lock.locked()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # 取消路径归还了锁，但串行时钟必须保持原值，而不是被刷新为“现在”
    assert request_pacer._last_finished == original_last_finished, (
        "取消未执行的请求不应重置 _last_finished（串行时钟被取消风暴重置）"
    )


def test_cancel_does_not_reset_serial_clock() -> None:
    asyncio.run(_case_cancel_does_not_reset_serial_clock())


# ---------------------------------------------------------------------------
# 守卫用例：429 分支（旧代码在 _send_429 之前显式释放回合，所以那里不是泄漏点；
# 改为只依赖外层 try/finally 后，这条用例保证 429 路径依然会归还回合锁）
# ---------------------------------------------------------------------------
async def _case_middleware_429_send_failure() -> None:
    _force_clean()
    await _enable_serial()

    # 并发已满 → 必然走 429 分支
    rate_limiter._enabled = True
    rate_limiter._max_concurrent = 1
    rate_limiter._active = 1
    rate_limiter._last_refresh = time.monotonic()

    reached_app = False

    async def app(scope, receive, send):
        nonlocal reached_app
        reached_app = True
        await _noop_app(scope, receive, send)

    async def send_raising(message):
        raise RuntimeError("client disconnected while sending 429")

    middleware = RateLimitMiddleware(app)
    try:
        await middleware(SCOPE, None, send_raising)
    except RuntimeError:
        pass
    else:
        raise AssertionError("send 抛错时应向外传播")

    assert not reached_app, "并发已满时不应进入业务 app"
    assert request_pacer._turn_lock.locked() is False, "429 发送失败后回合锁应已释放"

    rate_limiter._active = 0
    await _acquire_turn_must_succeed("429 发送失败后")


def test_middleware_releases_turn_when_429_send_fails() -> None:
    asyncio.run(_case_middleware_429_send_failure())


# ---------------------------------------------------------------------------
# 缺陷 2b：中间件在 rate_limiter.acquire() 阶段抛异常
# ---------------------------------------------------------------------------
async def _case_middleware_rate_acquire_failure() -> None:
    _force_clean()
    await _enable_serial()

    original_acquire = rate_limiter.acquire

    async def broken_acquire():
        raise RuntimeError("rate limiter exploded")

    rate_limiter.acquire = broken_acquire
    try:
        middleware = RateLimitMiddleware(_noop_app)
        try:
            await middleware(SCOPE, None, lambda message: asyncio.sleep(0))
        except RuntimeError:
            pass
        else:
            raise AssertionError("rate_limiter.acquire 抛错时应向外传播")
    finally:
        rate_limiter.acquire = original_acquire

    await _acquire_turn_must_succeed("rate_limiter.acquire 抛错后")


def test_middleware_releases_turn_when_rate_acquire_fails() -> None:
    asyncio.run(_case_middleware_rate_acquire_failure())


# ---------------------------------------------------------------------------
# 缺陷 3：清理阶段释放并发名额失败时，同步的回合锁归还仍须执行
# ---------------------------------------------------------------------------
async def _case_second_cancel_in_finally() -> None:
    _force_clean()
    await _enable_serial()

    original_release = rate_limiter.release_nowait

    def release_that_fails():
        raise RuntimeError("释放并发名额失败")

    rate_limiter.release_nowait = release_that_fails
    try:
        async def app(scope, receive, send):
            raise RuntimeError("app failed before sending anything")

        middleware = RateLimitMiddleware(app)
        try:
            await middleware(SCOPE, None, lambda message: asyncio.sleep(0))
        except RuntimeError:
            pass
        else:
            raise AssertionError("清理阶段的异常应向外传播")
    finally:
        rate_limiter.release_nowait = original_release

    assert request_pacer._turn_lock.locked() is False, (
        "并发名额释放失败时，回合锁也必须已归还"
    )
    await _acquire_turn_must_succeed("并发名额释放失败后")


def test_turn_released_even_if_rate_release_fails() -> None:
    asyncio.run(_case_second_cancel_in_finally())


# ---------------------------------------------------------------------------
# 缺陷 2c：业务 app 抛异常（客户端断开最常见形态）
# ---------------------------------------------------------------------------
async def _case_app_exception_releases_all() -> None:
    _force_clean()
    await _enable_serial()
    rate_limiter._enabled = True
    rate_limiter._max_concurrent = 4
    rate_limiter._active = 0
    rate_limiter._last_refresh = time.monotonic()

    async def app(scope, receive, send):
        raise RuntimeError("upstream boom")

    middleware = RateLimitMiddleware(app)
    try:
        await middleware(SCOPE, None, lambda message: asyncio.sleep(0))
    except RuntimeError:
        pass
    else:
        raise AssertionError("app 抛错时应向外传播")

    assert rate_limiter._active == 0, "并发名额应已释放"
    await _acquire_turn_must_succeed("app 抛错后")


def test_app_exception_releases_turn_and_slot() -> None:
    asyncio.run(_case_app_exception_releases_all())


# ---------------------------------------------------------------------------
# 正向功能：串行模式严格排队，请求之间不重叠
# ---------------------------------------------------------------------------
async def _case_serial_requests_do_not_overlap() -> None:
    _force_clean()
    await _enable_serial(min_interval=0.0, jitter=0.0)

    events = []
    running = 0
    max_running = 0

    async def app(scope, receive, send):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        events.append("enter")
        await asyncio.sleep(0.1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})
        events.append("exit")
        running -= 1

    middleware = RateLimitMiddleware(app)
    await asyncio.gather(
        middleware(dict(SCOPE), None, lambda message: asyncio.sleep(0)),
        middleware(dict(SCOPE), None, lambda message: asyncio.sleep(0)),
        middleware(dict(SCOPE), None, lambda message: asyncio.sleep(0)),
    )

    assert max_running == 1, f"串行模式下不应重叠执行，实际最大并发 {max_running}"
    assert events == ["enter", "exit"] * 3, f"排队顺序异常: {events}"
    assert request_pacer._turn_lock.locked() is False, "全部请求结束后回合锁应释放"


def test_serial_requests_do_not_overlap() -> None:
    asyncio.run(_case_serial_requests_do_not_overlap())


# ---------------------------------------------------------------------------
# 正向功能：普通流式响应结束即释放回合锁与并发名额
# ---------------------------------------------------------------------------
async def _case_normal_request_releases() -> None:
    _force_clean()
    await _enable_serial()
    rate_limiter._enabled = True
    rate_limiter._max_concurrent = 2
    rate_limiter._active = 0
    rate_limiter._last_refresh = time.monotonic()

    middleware = RateLimitMiddleware(_noop_app)
    await middleware(dict(SCOPE), None, lambda message: asyncio.sleep(0))

    assert rate_limiter._active == 0, "请求结束后并发名额应为 0"
    assert request_pacer._turn_lock.locked() is False, "请求结束后回合锁应释放"


def test_normal_request_releases_resources() -> None:
    asyncio.run(_case_normal_request_releases())


# ---------------------------------------------------------------------------
# 正向功能：串行开关独立生效（只勾「串行模式」、不勾「请求节流」也应排队）
# ---------------------------------------------------------------------------
async def _run_concurrent_requests(count: int):
    events = []
    running = 0
    max_running = 0

    async def app(scope, receive, send):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        events.append("enter")
        await asyncio.sleep(0.05)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})
        events.append("exit")
        running -= 1

    middleware = RateLimitMiddleware(app)
    await asyncio.gather(
        *(
            middleware(dict(SCOPE), None, lambda message: asyncio.sleep(0))
            for _ in range(count)
        )
    )
    return events, max_running


async def _case_serial_without_throttle_switch() -> None:
    _force_clean()
    await _enable_serial(min_interval=0.0, jitter=0.0)
    request_pacer._enabled = False  # 只开串行模式，关掉请求节流总开关

    events, max_running = await _run_concurrent_requests(2)
    assert max_running == 1, "串行开关应独立生效，不应依赖请求节流总开关"
    assert events == ["enter", "exit"] * 2, f"排队顺序异常: {events}"


def test_serial_mode_works_without_throttle_switch() -> None:
    asyncio.run(_case_serial_without_throttle_switch())


async def _case_parallel_when_serial_off() -> None:
    _force_clean()
    await _enable_serial(min_interval=0.0, jitter=0.0)
    request_pacer._serial_mode = False  # 关闭串行 → 应恢复并发

    _, max_running = await _run_concurrent_requests(2)
    assert max_running == 2, "关闭串行模式后请求应可并发，不应被排队"


def test_requests_run_in_parallel_when_serial_disabled() -> None:
    asyncio.run(_case_parallel_when_serial_off())


def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, func in tests:
        try:
            func()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS  {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
