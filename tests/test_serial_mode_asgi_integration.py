"""端到端（ASGI 层 + pacer 层）验证：§10.16 增强三后的串行语义。

中间件不再持有全局串行回合锁（并发名额 + 滑动窗口仍在中间件）；
串行回合锁按凭证下沉到 API 层（``_stream_with_credential_slot`` /
``acquire_turn_for``）。本文件验证两件事：

1. **pacer 层**：按键回合锁在「客户端中途取消」后不泄漏——取消会以
   ``asyncio.CancelledError`` 形式穿过等待，正是此前泄漏全局回合锁的场景；
   同 key 严格串行、跨 key 并行。
2. **ASGI 层**：中间件只管并发名额——客户端取消后名额归还、后续请求正常，
   串行不再由中间件提供。

用法::

    .venv\\Scripts\\python.exe tests\\test_serial_mode_asgi_integration.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["REQUEST_THROTTLE_ENABLED"] = "true"
os.environ["REQUEST_SERIAL_ENABLED"] = "true"
os.environ["REQUEST_MIN_INTERVAL"] = "0.3"
os.environ["REQUEST_JITTER"] = "0"

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402

from src.rate_limiter import RateLimitMiddleware, rate_limiter  # noqa: E402
from src.request_pacer import request_pacer  # noqa: E402

app = FastAPI()
app.add_middleware(RateLimitMiddleware)

STREAM_CHUNKS = 8
STREAM_DELAY = 0.02


@app.post("/v1/chat/completions")
async def chat():
    async def gen():
        for index in range(STREAM_CHUNKS):
            yield f"data: {index}\n\n"
            await asyncio.sleep(STREAM_DELAY)

    return StreamingResponse(gen(), media_type="text/event-stream")


def _force_clean(min_interval: float) -> None:
    request_pacer._lock = asyncio.Lock()
    request_pacer._turn_lock = asyncio.Lock()
    request_pacer._enabled = True
    request_pacer._serial_mode = True
    request_pacer._min_interval = min_interval
    request_pacer._jitter = 0.0
    request_pacer._last_finished = None
    request_pacer._last_sent = None
    request_pacer._last_refresh = time.monotonic()
    request_pacer.reset_keyed_turns()

    rate_limiter._lock = asyncio.Lock()
    rate_limiter._enabled = False
    rate_limiter._max_concurrent = 0
    rate_limiter._requests_per_window = 0
    rate_limiter._active = 0
    rate_limiter._timestamps.clear()
    rate_limiter._last_refresh = time.monotonic()


async def _cancel_after(client, delay: float) -> None:
    """发起一个流式请求，delay 秒后取消（模拟客户端断开）。"""
    task = asyncio.create_task(client.post("/v1/chat/completions", json={}))
    await asyncio.sleep(delay)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # 给应用侧留出清理时间（中间件 finally 执行）
    await asyncio.sleep(0.05)


async def _case_client_cancel_releases_rate_slot() -> None:
    """ASGI 层：客户端中途取消 → 并发名额归还 → 后续请求正常。"""
    _force_clean(min_interval=0.3)
    rate_limiter._enabled = True
    rate_limiter._max_concurrent = 1
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 流式响应进行到一半时取消（并发名额在持有中）
        await _cancel_after(client, 0.05)
        assert rate_limiter._active == 0, "客户端取消后并发名额应已归还"

        # 2. 后续请求必须能正常拿到完整响应（名额泄漏时会 429/挂死）
        response = await asyncio.wait_for(
            client.post("/v1/chat/completions", json={}), timeout=3.0
        )
        assert response.status_code == 200, response.status_code
        assert response.text.count("data:") == STREAM_CHUNKS

    assert rate_limiter._active == 0, "全部请求结束后并发名额应归零"


def test_client_cancel_then_next_request_succeeds() -> None:
    asyncio.run(_case_client_cancel_releases_rate_slot())


# ---------------------------------------------------------------------------
# pacer 层：按键回合锁的取消安全（旧全局回合锁泄漏场景的按键版回归）
# ---------------------------------------------------------------------------
async def _case_keyed_turn_cancel_then_next_succeeds() -> None:
    _force_clean(min_interval=0.3)

    # 0. 先完成一次完整回合，给 "cred-a" 种下 last_finished（下一条请求会
    #    等待 min_interval=0.3s，制造「等待期间被取消」的窗口）
    held = await request_pacer.acquire_turn_for("cred-a")
    assert held is True
    request_pacer.release_turn_for("cred-a")

    # 1. 在等待最小间隔期间取消（sleep 中的 CancelledError）
    task = asyncio.create_task(request_pacer.acquire_turn_for("cred-a"))
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.02)
    assert request_pacer.turn_locked_for("cred-a") is False, (
        "等待期间取消后按键回合锁应已归还（不更新 last_finished）"
    )

    # 2. 持有回合后正常释放，下一请求立即拿到
    held = await request_pacer.acquire_turn_for("cred-a")
    assert held is True
    request_pacer.release_turn_for("cred-a")
    held = await request_pacer.acquire_turn_for("cred-a")
    assert held is True
    request_pacer.release_turn_for("cred-a")
    assert request_pacer.turn_locked_for("cred-a") is False


def test_keyed_turn_cancel_then_next_succeeds() -> None:
    asyncio.run(_case_keyed_turn_cancel_then_next_succeeds())


async def _case_keyed_turn_serial_and_parallel() -> None:
    _force_clean(min_interval=0.0)

    async def worker(key: str, state: dict):
        held = await request_pacer.acquire_turn_for(key)
        try:
            state["running"] += 1
            state["max"] = max(state["max"], state["running"])
            await asyncio.sleep(0.05)
        finally:
            state["running"] -= 1
            if held:
                request_pacer.release_turn_for(key)

    # 同 key 串行
    state = {"running": 0, "max": 0}
    await asyncio.gather(worker("cred-a", state), worker("cred-a", state))
    assert state["max"] == 1, f"同 key 串行，实际最大并发 {state['max']}"

    # 跨 key 并行
    state = {"running": 0, "max": 0}
    await asyncio.gather(worker("cred-a", state), worker("cred-b", state))
    assert state["max"] == 2, f"跨 key 并行，实际最大并发 {state['max']}"


def test_keyed_turn_serial_and_parallel() -> None:
    asyncio.run(_case_keyed_turn_serial_and_parallel())


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
