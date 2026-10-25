"""Google 政策拦截检测的单元测试

覆盖：
- 文本级 / 响应级 / 原文级检测
- 跨 chunk 流式检测器（单 chunk 命中、多 chunk 拆分、正常文本、扣留后分叉、flush）
- OpenAI 非流式 / 流式转换器的拦截接入
- Anthropic 非流式转换器的拦截接入
- 端到端（TestClient + mock 上游）：Gemini 原生非流式端点对
  stream2nostream 聚合响应（无包装/带包装）与 SSE 文本响应体的拦截
"""

import asyncio
import json

from fastapi import FastAPI
from fastapi.responses import Response as FastAPIResponse
from fastapi.testclient import TestClient

from src.converter.policy_block import (
    POLICY_BLOCK_MARKERS,
    PolicyBlockStreamDetector,
    build_anthropic_error,
    build_gemini_error,
    build_openai_error,
    contains_policy_marker,
    is_policy_block_response,
    is_policy_block_text,
)
from src.converter.openai2gemini import (
    _POLICY_BLOCK_DETECTORS,
    convert_gemini_to_openai_response,
    convert_gemini_to_openai_stream,
)
from src.converter.anthropic2gemini import gemini_to_anthropic_response


PROMPT_BLOCK_MSG = (
    "The prompt could not be submitted. The prompt contains sensitive words "
    "that violate Google's Generative AI Prohibited Use policy. Try rephrasing "
    "the prompt."
)

IMAGE_BLOCK_MSG = (
    "The model could not generate output because the input image violates "
    "Google's Generative AI Prohibited Use policy."
)


# ==================== 文本级检测 ====================

def test_policy_block_text_matches_known_messages():
    assert is_policy_block_text(PROMPT_BLOCK_MSG)
    assert is_policy_block_text(IMAGE_BLOCK_MSG)
    # 带前导空白也应命中
    assert is_policy_block_text("\n\n" + PROMPT_BLOCK_MSG)


def test_policy_block_text_rejects_normal_text():
    assert not is_policy_block_text("Hello, how can I help you?")
    assert not is_policy_block_text("")
    assert not is_policy_block_text(None)
    # 只出现"敏感词"字样但不是拒绝文案开头
    assert not is_policy_block_text("The prompt contains sensitive words is a phrase.")


# ==================== 响应级检测 ====================

def _gemini_response(text, finish_reason="STOP", wrapped=False):
    resp = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": finish_reason,
                "index": 0,
            }
        ]
    }
    if wrapped:
        return {"response": resp, "traceId": "test"}
    return resp


def test_policy_block_response_detects_bare_and_wrapped_formats():
    assert is_policy_block_response(_gemini_response(PROMPT_BLOCK_MSG))
    assert is_policy_block_response(_gemini_response(IMAGE_BLOCK_MSG, wrapped=True))


def test_policy_block_response_ignores_normal_response():
    assert not is_policy_block_response(_gemini_response("正常回复内容"))
    # 只有思考内容不应误判
    resp = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": PROMPT_BLOCK_MSG, "thought": True}],
                },
                "finishReason": "STOP",
            }
        ]
    }
    assert not is_policy_block_response(resp)
    # 无 candidates
    assert not is_policy_block_response({})


# ==================== 原文级检测（兜底防线） ====================

def test_contains_policy_marker_matches_anywhere():
    # 标记出现在文本任意位置都命中（用于结构未知响应体的兜底扫描）
    assert contains_policy_marker(PROMPT_BLOCK_MSG)
    assert contains_policy_marker("前导垃圾数据 " + PROMPT_BLOCK_MSG + " 后续内容")
    # SSE 文本响应体（坑2场景）也应命中
    sse_body = "data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": PROMPT_BLOCK_MSG}]}}]})
    assert contains_policy_marker(sse_body)


def test_contains_policy_marker_rejects_normal_text():
    assert not contains_policy_marker("The prompt could not be submitted partially is fine here")
    assert not contains_policy_marker("正常回复内容")
    assert not contains_policy_marker("")
    assert not contains_policy_marker(None)


# ==================== 错误体构建 ====================

def test_error_builders():
    openai_err = build_openai_error()
    assert openai_err["error"]["code"] == "content_filter"
    assert openai_err["error"]["type"] == "content_filter_error"

    anthropic_err = build_anthropic_error()
    assert anthropic_err["type"] == "error"
    assert anthropic_err["error"]["type"] == "invalid_request_error"

    gemini_err = build_gemini_error()
    assert gemini_err["error"]["code"] == 400


# ==================== 流式检测器 ====================

def test_stream_detector_whole_message_in_one_chunk():
    detector = PolicyBlockStreamDetector()
    state, _ = detector.feed(PROMPT_BLOCK_MSG)
    assert state == "blocked"


def test_stream_detector_split_across_chunks():
    detector = PolicyBlockStreamDetector()
    # 按 10 个字符一组拆分，模拟上游分片
    states = []
    for i in range(0, len(IMAGE_BLOCK_MSG), 10):
        state, _ = detector.feed(IMAGE_BLOCK_MSG[i:i + 10])
        states.append(state)
        if state == "blocked":
            break
    # 命中前一直是 hold，最终 blocked；命中后检测器粘性返回 blocked
    assert states[-1] == "blocked"
    assert all(s == "hold" for s in states[:-1])
    assert detector.feed("more")[0] == "blocked"


def test_stream_detector_normal_text_emits_immediately():
    detector = PolicyBlockStreamDetector()
    state, released = detector.feed("今天天气不错，我们出去走走吧。")
    assert state == "emit"
    assert released == "今天天气不错，我们出去走走吧。"
    # 已判定后，后续文本直接透传
    state, released = detector.feed("再来一段。")
    assert state == "emit"
    assert released == "再来一段。"


def test_stream_detector_hold_then_diverge():
    """首 chunk 恰好是标记前缀（如 'The '），分叉后需释放完整缓冲。"""
    detector = PolicyBlockStreamDetector()
    state, _ = detector.feed("The ")
    assert state == "hold"
    state, released = detector.feed("quick brown fox jumps over the lazy dog")
    assert state == "emit"
    assert released == "The quick brown fox jumps over the lazy dog"


def test_stream_detector_flush_releases_pending_buffer():
    detector = PolicyBlockStreamDetector()
    detector.feed("The prompt cou")  # hold
    state, released = detector.flush()
    assert state == "emit"
    assert released == "The prompt cou"


# ==================== OpenAI 非流式转换 ====================

def test_convert_gemini_to_openai_response_policy_block():
    result = convert_gemini_to_openai_response(_gemini_response(PROMPT_BLOCK_MSG), "gemini-2.5-pro")
    assert "error" in result
    assert result["error"]["code"] == "content_filter"


def test_convert_gemini_to_openai_response_normal_untouched():
    result = convert_gemini_to_openai_response(_gemini_response("正常回复"), "gemini-2.5-pro")
    assert "error" not in result
    assert result["choices"][0]["message"]["content"] == "正常回复"


# ==================== OpenAI 流式转换 ====================

def _sse(text=None, finish_reason=None):
    parts = []
    if text is not None:
        parts.append({"text": text})
    candidate = {"content": {"role": "model", "parts": parts}}
    if finish_reason:
        candidate["finishReason"] = finish_reason
    return "data: " + json.dumps({"candidates": [candidate]})


def test_convert_gemini_stream_policy_block_in_single_chunk():
    rid = "pb-single"
    out = convert_gemini_to_openai_stream(_sse(PROMPT_BLOCK_MSG), "gemini-2.5-pro", rid)
    payload = json.loads(out[len("data: "):])
    assert "error" in payload
    assert payload["error"]["code"] == "content_filter"
    # 命中后进入抑制状态
    assert _POLICY_BLOCK_DETECTORS.get(rid) is not None


def test_convert_gemini_stream_policy_block_split_chunks_then_suppressed():
    rid = "pb-split"
    chunks = [_sse(IMAGE_BLOCK_MSG[i:i + 12]) for i in range(0, len(IMAGE_BLOCK_MSG), 12)]
    outputs = []
    for i, chunk in enumerate(chunks):
        out = convert_gemini_to_openai_stream(chunk, "gemini-2.5-pro", rid)
        if out is not None:
            outputs.append(out)
        if out is not None and "error" in out:
            break
    # 必须命中拦截并输出错误
    assert outputs and "error" in outputs[-1]
    # 命中前的 chunk（hold 状态）正文绝不能透传：delta 中不得出现 content
    for out in outputs[:-1]:
        payload = json.loads(out[len("data: "):])
        assert "content" not in payload["choices"][0]["delta"]
    # 命中后后续 chunk 全部被抑制（含收尾块）
    assert convert_gemini_to_openai_stream(_sse("more"), "gemini-2.5-pro", rid) is None
    finish_out = convert_gemini_to_openai_stream(_sse(finish_reason="STOP"), "gemini-2.5-pro", rid)
    assert finish_out is None
    # 收尾块后状态清理
    assert rid not in _POLICY_BLOCK_DETECTORS


def test_convert_gemini_stream_normal_text_unaffected():
    rid = "pb-normal"
    out1 = convert_gemini_to_openai_stream(_sse("你好，"), "gemini-2.5-pro", rid)
    payload1 = json.loads(out1[len("data: "):])
    assert payload1["choices"][0]["delta"]["content"] == "你好，"
    out2 = convert_gemini_to_openai_stream(_sse("The answer is 42.", "STOP"), "gemini-2.5-pro", rid)
    payload2 = json.loads(out2[len("data: "):])
    assert payload2["choices"][0]["delta"]["content"] == "The answer is 42."
    assert payload2["choices"][0]["finish_reason"] == "stop"
    assert rid not in _POLICY_BLOCK_DETECTORS


def test_convert_gemini_stream_flush_pending_text_on_finish():
    """流在未完成判定时收尾，扣留的文本必须随收尾块释放。"""
    rid = "pb-flush"
    convert_gemini_to_openai_stream(_sse("The prompt cou"), "gemini-2.5-pro", rid)
    out = convert_gemini_to_openai_stream(_sse(finish_reason="STOP"), "gemini-2.5-pro", rid)
    payload = json.loads(out[len("data: "):])
    assert payload["choices"][0]["delta"].get("content") == "The prompt cou"
    assert rid not in _POLICY_BLOCK_DETECTORS


# ==================== Anthropic 非流式转换 ====================

def test_gemini_to_anthropic_response_policy_block():
    result = gemini_to_anthropic_response(_gemini_response(PROMPT_BLOCK_MSG), "gemini-2.5-pro")
    assert result["type"] == "error"
    assert result["error"]["type"] == "invalid_request_error"


def test_gemini_to_anthropic_response_normal_untouched():
    result = gemini_to_anthropic_response(_gemini_response("正常回复"), "gemini-2.5-pro")
    assert result["type"] == "message"
    assert result["content"] == [{"type": "text", "text": "正常回复"}]


# ==================== Anthropic 流式转换 ====================

def test_gemini_stream_to_anthropic_stream_policy_block():
    from src.converter.anthropic2gemini import gemini_stream_to_anthropic_stream

    async def _gen():
        # 前 20 字符 + 剩余部分，模拟拆分
        yield _sse(PROMPT_BLOCK_MSG[:20]).encode()
        yield _sse(PROMPT_BLOCK_MSG[20:]).encode()

    events = list(asyncio.run(_collect(gemini_stream_to_anthropic_stream(_gen(), "gemini-2.5-pro"))))
    assert any('"type": "error"' in e or '"type":"error"' in e for e in events)
    # 不应输出任何 text_delta 正文
    assert not any("text_delta" in e for e in events)


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item.decode() if isinstance(item, bytes) else str(item))
    return out


def test_gemini_stream_to_anthropic_stream_normal_untouched():
    from src.converter.anthropic2gemini import gemini_stream_to_anthropic_stream

    async def _gen():
        yield _sse("你好，").encode()
        yield _sse("The quick brown fox", "STOP").encode()

    events = list(asyncio.run(_collect(gemini_stream_to_anthropic_stream(_gen(), "gemini-2.5-pro"))))
    deltas = "".join(e for e in events if "text_delta" in e)
    assert "你好，" in deltas
    assert "The quick brown fox" in deltas


# ==================== 端到端：Gemini 原生非流式端点（坑1/坑2） ====================

def _aggregated_block_response():
    """模拟 collect_streaming_response（stream2nostream）的聚合输出：

    包装已被剥掉，返回裸的 {"candidates": [...]}——坑1的真实泄漏场景。
    """
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": PROMPT_BLOCK_MSG}],
                },
                "finishReason": "STOP",
                "safetyRatings": [],
            }
        ],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 20, "totalTokenCount": 21},
    }


def _aggregated_normal_response():
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "正常回复内容"}]},
                "finishReason": "STOP",
                "safetyRatings": [],
            }
        ],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 6, "totalTokenCount": 7},
    }


def _patch_upstream(monkeypatch, mode, resp_body: bytes, status_code: int):
    """mock API 层的 non_stream_request，返回指定响应体

    mode 参数保留以兼容调用方，上游删除 geminicli 后仅剩 antigravity 一种。
    """
    async def fake_non_stream_request(body=None, headers=None, log_ctx=None):
        return FastAPIResponse(
            content=resp_body, status_code=status_code, media_type="application/json"
        )

    async def identity_normalize(request):
        return request

    monkeypatch.setattr(
        "src.api.antigravity.non_stream_request", fake_non_stream_request
    )
    monkeypatch.setattr(
        "src.converter.antigravity_fix.normalize_antigravity_request",
        identity_normalize,
    )


def _make_client():
    from src.utils import authenticate_gemini_flexible

    from src.router.antigravity import gemini as antigravity_gemini

    app = FastAPI()
    app.include_router(antigravity_gemini.router)
    app.dependency_overrides[authenticate_gemini_flexible] = lambda: "test-key"
    return TestClient(app)


_GEMINI_REQ_BODY = {
    "contents": [{"role": "user", "parts": [{"text": "hi"}]}]
}


def test_e2e_unwrapped_aggregated_block_response_returns_400(monkeypatch):
    """坑1：stream2nostream 聚合的无包装响应，拒绝文案必须转为显式错误"""
    for mode, path in (("antigravity", "/v1beta/models/gemini-2.5-pro:generateContent"),):
        _patch_upstream(
            monkeypatch, mode,
            json.dumps(_aggregated_block_response()).encode("utf-8"), 200,
        )
        client = _make_client()
        resp = client.post(path, json=_GEMINI_REQ_BODY)
        assert resp.status_code == 400, f"[{mode}] status={resp.status_code} body={resp.text[:200]}"
        data = resp.json()
        assert "error" in data, f"[{mode}] 应返回错误体"
        # 拒绝文案不能泄漏到响应正文
        assert PROMPT_BLOCK_MSG not in resp.text, f"[{mode}] 拒绝文案泄漏"


def test_e2e_wrapped_block_response_returns_400(monkeypatch):
    """带 response 包装的拦截响应同样要被拦截"""
    wrapped = {"response": _aggregated_block_response(), "traceId": "t"}
    for mode, path in (("antigravity", "/v1beta/models/gemini-2.5-pro:generateContent"),):
        _patch_upstream(
            monkeypatch, mode,
            json.dumps(wrapped).encode("utf-8"), 200,
        )
        client = _make_client()
        resp = client.post(path, json=_GEMINI_REQ_BODY)
        assert resp.status_code == 400, f"[{mode}] status={resp.status_code}"
        assert PROMPT_BLOCK_MSG not in resp.text


def test_e2e_sse_text_body_block_returns_400(monkeypatch):
    """坑2：上游直接吐 SSE 文本（非 JSON），兜底扫描必须拦截"""
    sse_body = (
        "data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": PROMPT_BLOCK_MSG}]}}]}) + "\n\n"
        "data: [DONE]\n\n"
    )
    for mode, path in (("antigravity", "/v1beta/models/gemini-2.5-pro:generateContent"),):
        _patch_upstream(monkeypatch, mode, sse_body.encode("utf-8"), 200)
        client = _make_client()
        resp = client.post(path, json=_GEMINI_REQ_BODY)
        assert resp.status_code == 400, f"[{mode}] status={resp.status_code} body={resp.text[:200]}"
        assert PROMPT_BLOCK_MSG not in resp.text


def test_e2e_normal_response_passthrough(monkeypatch):
    """正常回复（无包装聚合响应）必须照常透传，不受检测影响"""
    for mode, path in (("antigravity", "/v1beta/models/gemini-2.5-pro:generateContent"),):
        _patch_upstream(
            monkeypatch, mode,
            json.dumps(_aggregated_normal_response()).encode("utf-8"), 200,
        )
        client = _make_client()
        resp = client.post(path, json=_GEMINI_REQ_BODY)
        assert resp.status_code == 200, f"[{mode}] status={resp.status_code} body={resp.text[:200]}"
        data = resp.json()
        text = "".join(
            p.get("text", "")
            for p in data["candidates"][0]["content"]["parts"]
        )
        assert text == "正常回复内容", f"[{mode}] 正文不符: {text!r}"
        assert "error" not in data
