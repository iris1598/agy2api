"""端到端（ASGI 层）验证：串行模式 + 限流中间件在「客户端中途取消」后不再卡死。

用 ``httpx.ASGITransport`` 把真实的 ``RateLimitMiddleware`` 挂到真实的 FastAPI 应用上，
并取消客户端任务来模拟「用户点停止生成 / 刷新页面 / 客户端超时断开」。
取消会以 ``asyncio.CancelledError`` 形式穿过中间件，正是此前泄漏回合锁的场景。

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


async def _case_client_cancel_then_next_request_succeeds() -> None:
    _force_clean(min_interval=0.3)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 流式响应进行到一半时取消（回合锁与并发名额都在持有中）
        await _cancel_after(client, 0.05)
        assert request_pacer._turn_lock.locked() is False, (
            "客户端取消后回合锁应已归还（旧代码会永久 locked）"
        )

        # 2. 在 acquire_turn() 等待最小间隔/抖动期间取消（旧代码泄漏锁的另一个窗口）
        await _cancel_after(client, 0.02)

        # 3. 后续请求必须能正常拿到完整响应
        response = await asyncio.wait_for(
            client.post("/v1/chat/completions", json={}), timeout=3.0
        )
        assert response.status_code == 200, response.status_code
        assert response.text.count("data:") == STREAM_CHUNKS

    assert request_pacer._turn_lock.locked() is False, "全部请求结束后回合锁应释放"
    assert rate_limiter._active == 0, "并发名额应归零"


def test_client_cancel_then_next_request_succeeds() -> None:
    asyncio.run(_case_client_cancel_then_next_request_succeeds())


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
