"""Google 生成式 AI 政策拦截检测

Gemini CLI / Antigravity 后端在内容触发 Google 的《Generative AI Prohibited Use
policy》时，不会返回标准的 4xx 错误或 blockReason，而是把固定的拒绝文案作为
一条"正常"的模型回复（HTTP 200、finishReason=STOP）返回。下游转换器如果不加
区分，就会把这段话当成正文输出给客户端。

本模块负责识别这类响应，让各转换层可以将其转换为显式错误：

- 文本级检测：``is_policy_block_text``
- 响应级检测（非流式 / 假流式）：``is_policy_block_response``
- 跨 chunk 流式检测：``PolicyBlockStreamDetector``
- 各协议错误体构建：``build_openai_error`` / ``build_anthropic_error`` /
  ``build_gemini_error``
"""

from typing import Any, Dict, List, Optional, Tuple

# Google 政策拒绝文案的固定前缀。
# 只匹配开头前缀而不是全文：Google 可能微调后半段措辞（链接、句式），
# 但开头句式非常稳定。
POLICY_BLOCK_MARKERS: Tuple[str, ...] = (
    # 输入文本在生成前被拦截
    "The prompt could not be submitted. The prompt contains sensitive words",
    # 输入图片被拦截
    "The model could not generate output because the input image violates",
    # 防御性变体：输入提示词在生成阶段被拦截
    "The model could not generate output because the input prompt violates",
)

# 最长标记长度，用于限制流式缓冲的上限
_MAX_MARKER_LEN = max(len(m) for m in POLICY_BLOCK_MARKERS)


def is_policy_block_text(text: Optional[str]) -> bool:
    """判断一段完整文本是否是 Google 政策拒绝文案

    只检查开头（lstrip 后 startswith），忽略后续的链接与措辞变化。
    """
    if not text:
        return False
    stripped = text.lstrip()
    return any(stripped.startswith(marker) for marker in POLICY_BLOCK_MARKERS)


def contains_policy_marker(text: Optional[str]) -> bool:
    """原文级检测：标记是否出现在文本任意位置（最后防线）

    仅在响应体结构未知（如上游直接吐 SSE 文本、json 解析失败的兜底分支）
    时使用。正常结构化响应请用 ``is_policy_block_response``，
    它只做开头前缀匹配，误报风险更低。
    """
    if not text:
        return False
    return any(marker in text for marker in POLICY_BLOCK_MARKERS)


def _iter_candidate_texts(gemini_response: Dict[str, Any]) -> List[str]:
    """从 Gemini 响应中提取所有候选的非思考文本（已展开 response 包装）"""
    texts: List[str] = []
    if not isinstance(gemini_response, dict):
        return texts
    data = gemini_response
    # GeminiCLI 的 {"response": {...}} 包装格式
    if "response" in data and "candidates" not in data:
        data = data["response"]
    if not isinstance(data, dict):
        return texts
    for candidate in data.get("candidates", []) or []:
        if not isinstance(candidate, dict):
            continue
        for part in candidate.get("content", {}).get("parts", []) or []:
            if not isinstance(part, dict):
                continue
            if part.get("thought", False):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return texts


def is_policy_block_response(gemini_response: Any) -> bool:
    """判断一个 Gemini 响应（非流式完整响应）是否是政策拦截响应

    兼容裸 Gemini 格式和 GeminiCLI 的 ``{"response": {...}}`` 包装格式。
    """
    if not isinstance(gemini_response, dict):
        return False
    texts = _iter_candidate_texts(gemini_response)
    if not texts:
        return False
    full_text = "".join(texts)
    return is_policy_block_text(full_text)


class PolicyBlockStreamDetector:
    """跨 chunk 的流式政策拦截检测器

    政策拒绝文案可能被上游拆成多个 chunk 送达，无法逐 chunk 判断。
    本检测器在流的开头扣留文本，直到能判定为止：

    - 文本一旦与所有标记前缀分叉 → 正常文本，释放扣留的内容；
    - 文本完整命中某个标记前缀 → 政策拦截；
    - 流结束仍未判定（缓冲始终是某标记的严格前缀）→ 释放缓冲。

    使用方式::

        detector = PolicyBlockStreamDetector()
        state, released = detector.feed(chunk_text)

        if state == "blocked":   # 命中拦截，输出错误并停止
        elif state == "hold":    # 尚未判定，扣留此段文本（不要输出）
        else:                    # "emit"，输出 released（含之前扣留的内容）

    注意：只喂入非思考（非 thought）文本。
    """

    _STATE_EMIT = "emit"
    _STATE_HOLD = "hold"
    _STATE_BLOCKED = "blocked"

    def __init__(self) -> None:
        self._buffer = ""
        self._decided = False
        self._blocked = False

    @property
    def blocked(self) -> bool:
        """是否已判定为政策拦截"""
        return self._decided and self._blocked

    def feed(self, text: str) -> Tuple[str, Optional[str]]:
        """喂入一段非思考文本，返回 (状态, 应输出文本)

        - ``("emit", text)``：判定为正常文本，输出 text（含此前扣留的缓冲）
        - ``("hold", None)``：尚不能判定，扣留此段文本
        - ``("blocked", None)``：命中政策拦截
        """
        if self._decided:
            if self._blocked:
                return self._STATE_BLOCKED, None
            return self._STATE_EMIT, text

        if not text:
            # 空文本无法推进判定；如果已有缓冲，维持 hold 状态
            if self._buffer:
                return self._STATE_HOLD, None
            return self._STATE_EMIT, text

        self._buffer += text
        stripped = self._buffer.lstrip()

        # 完整命中某个标记前缀 → 政策拦截
        if any(stripped.startswith(marker) for marker in POLICY_BLOCK_MARKERS):
            self._decided = True
            self._blocked = True
            return self._STATE_BLOCKED, None

        # 仍未分叉（缓冲是某个标记的严格前缀）→ 继续扣留
        if any(marker.startswith(stripped) for marker in POLICY_BLOCK_MARKERS):
            return self._STATE_HOLD, None

        # 与所有标记分叉 → 正常文本，释放全部缓冲
        self._decided = True
        self._blocked = False
        return self._STATE_EMIT, self._buffer

    def flush(self) -> Tuple[str, Optional[str]]:
        """流结束时调用，释放仍在扣留的缓冲（始终判定为正常文本）

        仅在流意外结束且未判定时需要调用；正常路径下 ``feed`` 会自行判定。
        """
        if self._decided:
            if self._blocked:
                return self._STATE_BLOCKED, None
            return self._STATE_EMIT, None
        self._decided = True
        self._blocked = False
        return self._STATE_EMIT, self._buffer


def build_openai_error(original_text: Optional[str] = None) -> Dict[str, Any]:
    """构建 OpenAI 格式的政策拦截错误体"""
    message = (
        "Request blocked by Google's Generative AI Prohibited Use policy "
        "(content filter). The prompt or input image was rejected by the "
        "upstream model. Try rephrasing the prompt."
    )
    if original_text:
        message = f"{message} Upstream message: {original_text.strip()}"
    return {
        "error": {
            "message": message,
            "type": "content_filter_error",
            "code": "content_filter",
            "param": None,
        }
    }


def build_anthropic_error(original_text: Optional[str] = None) -> Dict[str, Any]:
    """构建 Anthropic 格式的政策拦截错误体"""
    message = (
        "Request blocked by Google's Generative AI Prohibited Use policy "
        "(content filter). The prompt or input image was rejected by the "
        "upstream model. Try rephrasing the prompt."
    )
    if original_text:
        message = f"{message} Upstream message: {original_text.strip()}"
    return {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": message,
        },
    }


def build_gemini_error(original_text: Optional[str] = None) -> Dict[str, Any]:
    """构建 Gemini 原生格式的政策拦截错误体"""
    message = (
        "Request blocked by Google's Generative AI Prohibited Use policy "
        "(content filter). The prompt or input image was rejected by the "
        "upstream model. Try rephrasing the prompt."
    )
    if original_text:
        message = f"{message} Upstream message: {original_text.strip()}"
    return {
        "error": {
            "code": 400,
            "message": message,
            "status": "INVALID_ARGUMENT",
        }
    }
