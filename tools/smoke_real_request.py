"""真实端到端冒烟测试:通过本地代理的 OpenAI 兼容接口发真实模型请求。

按交接文档 P0 设计,覆盖四类场景(每条 max_tokens 都很小,额度消耗可忽略):

  basic   普通流式请求(默认最低价模型)
  tools   带 function 定义的流式请求(工具信封路径)
  suffix  -low 后缀模型(thinkingConfig 注入路径)
  claude  Claude 模型(used_claude / used_non_gemini_model 标签路径)

用法(需要已配置凭证;默认要求 web.py 已启动,或用 --spawn 自动拉起):

    .venv/Scripts/python.exe tools/smoke_real_request.py
    .venv/Scripts/python.exe tools/smoke_real_request.py --scenario basic
    .venv/Scripts/python.exe tools/smoke_real_request.py --spawn

断言:HTTP 200、SSE data 行逐块可解析、finish_reason 正常收尾;响应中出现
``"type": "fidelity_rejected"`` 即失败并打印 reason/detail,便于按证据补规则。
"""

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# 脚本必须先定位仓库根目录才能复用应用配置(get_port / get_api_password)
from config import get_api_password, get_server_port  # noqa: E402

DEFAULT_MODEL = "gemini-2.5-flash-lite"
SUFFIX_MODEL = "gemini-3.8-flash-low"
CLAUDE_MODEL = "claude-sonnet-5-5-low"
MAX_TOKENS = 16
PROMPT = "用一句话回答:1+1等于几?"
TOOL_PROMPT = "调用 get_weather 查询北京今天的天气。"
TOOL_DEFINITION: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的当前天气",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名"}
            },
            "required": ["city"],
        },
    },
}
SCENARIOS = ("basic", "tools", "suffix", "claude")
FIDELITY_MARKER = "fidelity_rejected"


def _build_payload(model: str, max_tokens: int, tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": TOOL_PROMPT if tools else PROMPT}],
        "max_tokens": max_tokens,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
    return payload


def _check_chunk(chunk: Dict[str, Any], state: Dict[str, Any]) -> Optional[str]:
    """检查单个 SSE chunk;返回错误消息(无问题返回 None)。"""
    if "error" in chunk or FIDELITY_MARKER in json.dumps(chunk, ensure_ascii=False):
        err = chunk.get("error") or {}
        return (
            f"fidelity/上游错误: type={err.get('type')} reason={err.get('reason')} "
            f"detail={err.get('detail') or err.get('message')}"
        )
    choices = chunk.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict):
                finish = choice.get("finish_reason")
                if finish:
                    state["finish_reason"] = finish
                delta = choice.get("delta")
                if isinstance(delta, dict) and delta.get("content"):
                    state["content_chars"] += len(delta["content"])
    usage = chunk.get("usage")
    if isinstance(usage, dict):
        state["usage"] = usage
    return None


async def _run_scenario(
    client: httpx.AsyncClient,
    base_url: str,
    headers: Dict[str, str],
    name: str,
    model: str,
    max_tokens: int,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[bool, str]:
    url = f"{base_url}/v1/chat/completions"
    payload = _build_payload(model, max_tokens, tools)
    state: Dict[str, Any] = {"finish_reason": None, "content_chars": 0, "usage": None}

    try:
        async with client.stream(
            "POST", url, headers=headers, json=payload, timeout=httpx.Timeout(30.0, read=120.0)
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                if FIDELITY_MARKER in body:
                    return False, f"HTTP {resp.status_code} 且被严格模式拒绝: {body[:500]}"
                return False, f"HTTP {resp.status_code}: {body[:500]}"

            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                return False, f"content-type 不是 SSE: {content_type}"

            chunk_count = 0
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError as exc:
                    return False, f"SSE data 行不是合法 JSON(第 {chunk_count + 1} 块): {exc}"
                chunk_count += 1
                error = _check_chunk(chunk, state)
                if error:
                    return False, f"第 {chunk_count} 块报错: {error}"
    except httpx.HTTPError as exc:
        return False, f"连接/读流失败: {type(exc).__name__}: {exc}"

    if chunk_count == 0:
        return False, "SSE 流未收到任何 data 块"
    if not state["finish_reason"]:
        return False, f"流收尾异常: 收到 {chunk_count} 块但没有 finish_reason"

    usage_note = "无 usage"
    if state["usage"]:
        usage_note = f"usage 可解析(total_tokens={state['usage'].get('total_tokens')})"
    return True, (
        f"{chunk_count} 块 SSE, finish_reason={state['finish_reason']}, "
        f"内容 {state['content_chars']} 字符, {usage_note}"
    )


async def _resolve_defaults() -> Tuple[str, str]:
    """从应用配置解析默认端口与 API 密码(ENV > 数据库 > 默认值)。"""
    port = await get_server_port()
    password = await get_api_password()
    return f"http://127.0.0.1:{port}", password


def _wait_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


async def _verify_models(
    client: httpx.AsyncClient, base_url: str, headers: Dict[str, str], models: List[str]
) -> Optional[str]:
    """预检 /v1/models;返回错误消息(全部存在返回 None)。不发模型请求,不消耗额度。"""
    try:
        resp = await client.get(f"{base_url}/v1/models", headers=headers, timeout=15.0)
    except httpx.HTTPError as exc:
        return f"无法访问 {base_url}/v1/models: {type(exc).__name__}: {exc}(先启动 python web.py 或加 --spawn)"
    if resp.status_code != 200:
        return f"/v1/models 返回 HTTP {resp.status_code}: {resp.text[:300]}"

    served: set = set()
    for item in resp.json().get("data", []):
        if isinstance(item, dict) and item.get("id"):
            served.add(str(item["id"]))

    missing = [m for m in models if m not in served]
    if missing:
        return (
            f"以下模型不在 /v1/models 列表中: {', '.join(missing)};"
            f"可能是凭证未开通(fetchAvailableModels 未学习到),先抓包补 src/model_enum.py 或换模型"
        )
    return None


async def main_async(args: argparse.Namespace) -> int:
    base_url, password = await _resolve_defaults()
    if args.base_url:
        base_url = args.base_url.rstrip("/")
    api_key = args.api_key or password

    spawned: Optional[subprocess.Popen] = None
    if args.spawn:
        host_part = base_url.split("//", 1)[-1]
        host, _, port_s = host_part.partition(":")
        if _wait_port(host, int(port_s or "7861"), timeout=1.0):
            print(f"[spawn] {base_url} 已有服务在监听,直接复用")
        else:
            print(f"[spawn] 启动 python web.py (cwd={_REPO_ROOT}) ...")
            spawned = subprocess.Popen(
                [sys.executable, "web.py"],
                cwd=str(_REPO_ROOT),
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            if not _wait_port(host, int(port_s or "7861"), timeout=60.0):
                print("[spawn] web.py 60 秒内未监听端口,放弃", file=sys.stderr)
                spawned.terminate()
                return 2
            print(f"[spawn] web.py 已就绪: {base_url}")

    headers = {"Authorization": f"Bearer {api_key}"}
    plan = [
        ("basic", args.model or DEFAULT_MODEL, args.max_tokens, None),
        ("tools", args.model or DEFAULT_MODEL, args.max_tokens, [TOOL_DEFINITION]),
        ("suffix", args.suffix_model or SUFFIX_MODEL, args.max_tokens, None),
        ("claude", args.claude_model or CLAUDE_MODEL, args.max_tokens, None),
    ]
    if args.scenario != "all":
        plan = [p for p in plan if p[0] == args.scenario]

    results: List[Tuple[str, bool, str]] = []
    try:
        async with httpx.AsyncClient() as client:
            models = [p[1] for p in plan]
            precheck = await _verify_models(client, base_url, headers, models)
            if precheck:
                print(f"\n[预检失败] {precheck}")
                return 2

            for name, model, max_tokens, tools in plan:
                started = time.monotonic()
                ok, message = await _run_scenario(
                    client, base_url, headers, name, model, max_tokens, tools
                )
                elapsed = time.monotonic() - started
                results.append((name, ok, message))
                print(
                    f"\n[{name}] model={model} {'PASS' if ok else 'FAIL'} ({elapsed:.1f}s)\n"
                    f"  {message}"
                )
    finally:
        if spawned is not None:
            print("\n[spawn] 停止 web.py ...")
            spawned.terminate()
            try:
                spawned.wait(timeout=10)
            except subprocess.TimeoutExpired:
                spawned.kill()

    failed = [name for name, ok, _ in results if not ok]
    print("\n========== 冒烟结果 ==========")
    for name, ok, _ in results:
        print(f"  {name:<8} {'✓ PASS' if ok else '✗ FAIL'}")
    if failed:
        print(f"\n结论: {len(failed)} 个场景失败: {', '.join(failed)}")
        print("fidelity_rejected 出现时按 reason 修复: 补模型枚举 / 修转换器 / 加白名单。")
        return 1
    print(f"\n结论: 全部 {len(results)} 个场景通过,无回退/拒绝。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="真实端到端冒烟测试(消耗少量模型额度;默认需 web.py 已启动)"
    )
    parser.add_argument("--base-url", help="覆盖服务地址(默认取配置端口,如 http://127.0.0.1:7861)")
    parser.add_argument("--api-key", help="覆盖 API 密码(默认取配置 api_password)")
    parser.add_argument("--model", help="覆盖 basic/tools 场景模型(默认 gemini-2.5-flash-lite)")
    parser.add_argument("--suffix-model", help="覆盖 suffix 场景模型(默认 gemini-3.8-flash-low)")
    parser.add_argument("--claude-model", help="覆盖 claude 场景模型(默认 claude-sonnet-5-5-low)")
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS, help=f"max_tokens(默认 {MAX_TOKENS})")
    parser.add_argument(
        "--scenario", choices=list(SCENARIOS) + ["all"], default="all", help="只跑指定场景(默认全部)"
    )
    parser.add_argument(
        "--spawn", action="store_true", help="自动拉起 python web.py(结束后停止;端口已占用则复用)"
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
