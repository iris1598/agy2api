"""上游流式挂起 → 回合锁/并发名额永久占用（死锁）的回归测试。

根因：stream_post_async 使用的流式 httpx 客户端 timeout=None（完全无超时）。
上游连接保持但不发数据时，`async for line in r.aiter_lines()` 永远挂起：
- 串行模式下回合锁（_turn_lock）被该请求永久持有，后续所有请求死锁；
- 限流开启时并发名额泄漏，逐渐耗尽后全部 429；
- 只能重启进程恢复。

修复：get_streaming_client 在 timeout=None 时启用分阶段超时，
read 超时来自 STREAM_READ_TIMEOUT（默认 300s，0=禁用），作用于
「相邻两次网络读取之间」的空闲时长。本文件验证：

1. 配置项解析（默认值 / 环境变量 / 禁用值）；
2. 流式客户端确实带上了分阶段超时；
3. 真实挂起上游（发一半后沉默）在 read 超时内抛出 httpx.ReadTimeout；
4. 持续有数据到达时不会误杀（计时不断重置）。

用法::

    .venv\\Scripts\\python.exe tests\\test_stream_read_timeout.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from config import get_stream_read_timeout  # noqa: E402
from src.httpx_client import get_streaming_client  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 配置项解析
# ---------------------------------------------------------------------------
def test_stream_read_timeout_default() -> None:
    """默认 300s"""
    os.environ.pop("STREAM_READ_TIMEOUT", None)
    value = asyncio.run(get_stream_read_timeout())
    assert value == 300.0, f"默认应为 300.0，实际 {value}"


def test_stream_read_timeout_env_override() -> None:
    """环境变量优先，可热更新"""
    os.environ["STREAM_READ_TIMEOUT"] = "60"
    try:
        value = asyncio.run(get_stream_read_timeout())
        assert value == 60.0, f"环境变量 60 应生效，实际 {value}"
    finally:
        os.environ.pop("STREAM_READ_TIMEOUT", None)


def test_stream_read_timeout_zero_disables() -> None:
    """0 = 显式禁用（恢复旧的无限等待行为）"""
    os.environ["STREAM_READ_TIMEOUT"] = "0"
    try:
        value = asyncio.run(get_stream_read_timeout())
        assert value == 0.0, f"0 应保持为 0（禁用），实际 {value}"
    finally:
        os.environ.pop("STREAM_READ_TIMEOUT", None)


# ---------------------------------------------------------------------------
# 2. 流式客户端带上分阶段超时
# ---------------------------------------------------------------------------
def test_streaming_client_gets_phased_timeout() -> None:
    """get_streaming_client(timeout=None) 应构造 httpx.Timeout 且 read 来自配置"""

    async def run():
        os.environ["STREAM_READ_TIMEOUT"] = "120"
        try:
            async with get_streaming_client() as client:
                t = client.timeout
                assert isinstance(t, httpx.Timeout), f"应为 httpx.Timeout，实际 {type(t)}"
                assert t.connect == 30.0, f"connect 应为 30，实际 {t.connect}"
                assert t.read == 120.0, f"read 应为 120，实际 {t.read}"
        finally:
            os.environ.pop("STREAM_READ_TIMEOUT", None)

    asyncio.run(run())


def test_streaming_client_disabled_when_config_zero() -> None:
    """配置为 0 时保持无超时（旧行为）。httpx 会把 timeout=None
    归一化为 Timeout(timeout=None)，因此检查 read 为 None。"""

    async def run():
        os.environ["STREAM_READ_TIMEOUT"] = "0"
        try:
            async with get_streaming_client() as client:
                t = client.timeout
                read = getattr(t, "read", None)
                assert read is None, f"禁用时 read 超时应为 None，实际 {t}"
        finally:
            os.environ.pop("STREAM_READ_TIMEOUT", None)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. 核心：上游发一半后永久沉默 → ReadTimeout 及时抛出
# ---------------------------------------------------------------------------
async def _start_hanging_server():
    """启动一个 TCP 服务器：响应头 + 两条数据后永久沉默（不断开连接）。"""
    writers = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        writers.append(writer)
        # 读掉请求（尽力）
        try:
            await asyncio.wait_for(reader.read(65536), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        # 发送合法 HTTP 响应头和两行完整数据，然后永久沉默
        # （每行带 \n，确保 aiter_lines 能立即产出）
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"7\r\ndata:0\n\r\n"
            b"7\r\ndata:1\n\r\n"
        )
        await writer.drain()
        # 永远挂着不发送、不断开 —— 模拟上游流卡死
        await asyncio.Event().wait()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, writers


async def _case_hanging_upstream_times_out() -> None:
    server, port, writers = await _start_hanging_server()
    try:
        os.environ["STREAM_READ_TIMEOUT"] = "1"
        async with get_streaming_client() as client:
            started = time.monotonic()
            received = []
            async with client.stream(
                "POST", f"http://127.0.0.1:{port}/v1:stream", json={}
            ) as r:
                assert r.status_code == 200
                async for line in r.aiter_lines():
                    received.append(line)
                    # 收到部分数据后上游沉默
            # 不应走到这里
            raise AssertionError("上游挂起时应抛出 ReadTimeout，而不是正常结束")
    except httpx.ReadTimeout:
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, f"ReadTimeout 应在 read 超时（1s）附近抛出，实际 {elapsed:.1f}s"
        assert len(received) >= 2, "应已收到部分数据后才超时（模拟流中途卡死）"
    finally:
        os.environ.pop("STREAM_READ_TIMEOUT", None)
        # 注意：不能用 wait_closed()——Python 3.12+ 会等所有 handler 结束，
        # 而挂起 handler 永远不结束。直接强关连接再关服务器。
        for w in writers:
            try:
                w.close()
            except Exception:
                pass
        server.close()


def test_hanging_upstream_times_out() -> None:
    asyncio.run(_case_hanging_upstream_times_out())


# ---------------------------------------------------------------------------
# 4. 持续有数据到达时不误杀（read 计时不断重置）
# ---------------------------------------------------------------------------
async def _start_trickling_server():
    """每 0.2s 发一条数据、持续 2s 的慢速服务器：总时长超过 read 超时，
    但每两次读取之间的空闲小于 read 超时，不应被误杀。"""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await asyncio.wait_for(reader.read(65536), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
        )
        await writer.drain()
        for i in range(10):  # 10 条 × 0.2s = 2s，远超 0.5s 的 read 超时
            line = f"chunk-{i}".encode()
            writer.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
            await writer.drain()
            await asyncio.sleep(0.2)
        writer.write(b"0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _case_trickling_stream_not_killed() -> None:
    server, port = await _start_trickling_server()
    try:
        os.environ["STREAM_READ_TIMEOUT"] = "0.5"  # 块间空闲上限 0.5s
        async with get_streaming_client() as client:
            lines = []
            async with client.stream(
                "POST", f"http://127.0.0.1:{port}/v1:stream", json={}
            ) as r:
                async for line in r.aiter_lines():
                    lines.append(line)
        assert any("chunk-9" in line for line in lines), (
            f"慢速但持续的数据流不应被 read 超时误杀，收到: {lines}"
        )
    finally:
        os.environ.pop("STREAM_READ_TIMEOUT", None)
        server.close()


def test_trickling_stream_not_killed() -> None:
    asyncio.run(_case_trickling_stream_not_killed())


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
