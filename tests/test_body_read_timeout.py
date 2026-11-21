"""入站请求体读超时（REQUEST_BODY_TIMEOUT）端到端验证。

场景：客户端在网络波动中迟迟传不完请求体，且 FIN/RST 到不了服务器——
服务端既收不到 http.disconnect，也没有任何取消信号。半截请求会占住中间件
的并发名额（§10.16 增强三后串行回合锁已按凭证下沉到 API 层，中间件只持有
并发名额），泄漏同样会让后续请求被 429 卡死。

这里用原始 ASGI 调用真实的 RateLimitMiddleware + FastAPI 应用，
用「永远挂起的 receive」模拟服务端感知不到的半途失联，验证：
- 超时后回合锁/并发名额归还，后续请求正常
- 客户端拿到明确的 408，而不是无响应挂死或 500
- 块间空闲语义：只要请求体还在陆续到达就不误伤
- 设为 0 可禁用（恢复旧的无限等待行为）

用法::

    python -m pytest tests/test_body_read_timeout.py -q
"""

import asyncio
import contextlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import src.rate_limiter as rl_module  # noqa: E402
from src.rate_limiter import RateLimitMiddleware, rate_limiter  # noqa: E402


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.post("/v1/chat/completions")
    async def chat(payload: dict):
        return {"ok": True, "keys": sorted(payload.keys())}

    return app


def _force_serial_mode() -> None:
    """开启限流（高并发上限）并把时钟归零（§10.16 后中间件不再持有回合锁）。

    直接置内部状态而不是走配置系统，避免测试依赖数据库；
    _last_refresh 置为当前时刻，防止测试期间触发 _maybe_refresh 重读配置。
    """
    rate_limiter._lock = asyncio.Lock()
    rate_limiter._enabled = True
    rate_limiter._max_concurrent = 5
    rate_limiter._requests_per_window = 0
    rate_limiter._window_seconds = 60
    rate_limiter._active = 0
    rate_limiter._timestamps.clear()
    rate_limiter._last_refresh = time.monotonic()


def _set_body_timeout(value: float) -> None:
    """替换中间件读取入站超时的配置函数（每次请求都会重新读取）。"""

    async def fake_get_body_timeout() -> float:
        return value

    rl_module.get_request_body_timeout = fake_get_body_timeout


def _make_scope() -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", b"22"),
        ],
        "client": ("127.0.0.1", 54321),
        "server": ("testserver", 80),
    }


async def _stalled_receive():
    """永远挂起的 receive：模拟服务端感知不到的半途失联（无 FIN/RST、无取消）。"""
    await asyncio.Event().wait()


def _chunked_receive(chunks, delays):
    """按 delays 逐块返回请求体；末尾补 http.disconnect（防御后续读取）。"""
    state = {"index": 0}

    async def receive():
        index = state["index"]
        state["index"] += 1
        if index < len(chunks):
            await asyncio.sleep(delays[index])
            return {
                "type": "http.request",
                "body": chunks[index],
                "more_body": index < len(chunks) - 1,
            }
        return {"type": "http.disconnect"}

    return receive


async def _run_and_collect(app, receive, timeout: float = 5.0):
    """调用 ASGI 应用并收集响应消息。"""
    messages = []

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(app(_make_scope(), receive, send), timeout=timeout)
    return messages


def test_body_stall_returns_408_and_releases_resources():
    """核心场景：请求体传到一半服务端永远失联 → 408 + 回合锁/名额归还 + 后续请求正常。"""

    async def case():
        _force_serial_mode()
        _set_body_timeout(0.2)
        app = _make_app()

        # 1. 半截请求失联：receive 永远挂起，无取消、无 disconnect
        messages = await _run_and_collect(app, _stalled_receive)

        assert messages[0]["type"] == "http.response.start"
        assert messages[0]["status"] == 408, messages
        # 由 ExceptionMiddleware 按 HTTPException 发送：带 connection: close
        headers = dict(messages[0]["headers"])
        assert headers.get(b"connection") == b"close", messages[0]
        assert messages[-1]["type"] == "http.response.body"
        assert messages[-1].get("more_body", False) is False
        body = json.loads(messages[-1]["body"])
        assert "detail" in body, body

        assert rate_limiter._active == 0, "超时后并发名额必须归还"

        # 2. 后续请求必须能正常拿到完整响应（旧代码会死锁在 acquire_turn 上）
        full_body = json.dumps({"message": "hi"}).encode()
        normal_messages = await _run_and_collect(
            app, _chunked_receive([full_body], [0.01])
        )
        assert normal_messages[0]["status"] == 200, normal_messages
        assert rate_limiter._active == 0

    asyncio.run(case())


def test_progressing_body_is_not_aborted():
    """块间空闲语义：只要请求体还在陆续到达（间隔 < 超时）就不误伤。"""

    async def case():
        _force_serial_mode()
        _set_body_timeout(0.5)
        app = _make_app()

        # 三个块：间隔 0.05 / 0.35（接近但小于 0.5 的超时）/ 0.05
        chunks = [b'{"me', b'ssage":', b'"hi"}']
        delays = [0.05, 0.35, 0.05]
        messages = await _run_and_collect(app, _chunked_receive(chunks, delays))

        assert messages[0]["status"] == 200, messages
        assert rate_limiter._active == 0

    asyncio.run(case())


def test_body_timeout_disabled_with_zero():
    """设为 0 禁用：恢复旧的无限等待行为，但取消任务时资源仍不泄漏。"""

    async def case():
        _force_serial_mode()
        _set_body_timeout(0.0)
        app = _make_app()

        messages = []
        task = asyncio.create_task(
            app(_make_scope(), _stalled_receive, lambda m: messages.append(m))
        )
        await asyncio.sleep(0.4)

        assert task.done() is False, "禁用后不应有超时保护"
        assert rate_limiter._active == 1, "禁用后并发名额仍被持有"

        # 取消任务（服务停止等场景）：清理路径必须照常归还锁与名额
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert rate_limiter._active == 0, "取消后并发名额必须归还"

    asyncio.run(case())


def test_normal_full_body_request_unaffected():
    """正常一次性到达的请求体不受影响，端点拿到完整 JSON。"""

    async def case():
        _force_serial_mode()
        _set_body_timeout(0.5)
        app = _make_app()

        full_body = json.dumps({"a": 1, "b": 2}).encode()
        messages = await _run_and_collect(
            app, _chunked_receive([full_body], [0.01])
        )

        assert messages[0]["status"] == 200, messages
        assert b'"a"' in messages[-1]["body"], messages[-1]
        assert rate_limiter._active == 0

    asyncio.run(case())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
