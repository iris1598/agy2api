"""thoughtSignature 缓存与回注。

真实 Antigravity 客户端会把上一轮 model part 的 ``thoughtSignature`` 原样带回；
而 Gemini 兼容客户端通常在序列化时丢弃未知字段。丢掉签名会让多轮工具调用
的报文形态与真实客户端不同，甚至影响可用性。

这里按 (会话轨迹, part 文本) 缓存上游签名，下一轮遇到同样文本但缺签名的
model part 时自动补回。只回注"确实见过"的签名，不做任何伪造。
"""

import hashlib
from collections import OrderedDict
from typing import Any, Optional

_MAX_ENTRIES = 4096

# key = (trajectory_id, text_digest) → signature
_SIGNATURES: "OrderedDict[tuple, str]" = OrderedDict()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _key(trajectory_id: str, text: str) -> tuple:
    return (trajectory_id or "", _digest(text))


def remember(trajectory_id: str, text: str, signature: str) -> None:
    if not signature:
        return
    key = _key(trajectory_id, text)
    _SIGNATURES[key] = signature
    _SIGNATURES.move_to_end(key)
    while len(_SIGNATURES) > _MAX_ENTRIES:
        _SIGNATURES.popitem(last=False)


def lookup(trajectory_id: str, text: str) -> Optional[str]:
    return _SIGNATURES.get(_key(trajectory_id, text))


def clear() -> None:
    """清空缓存（测试使用）。"""
    _SIGNATURES.clear()


def replay_into_contents(contents: Any, trajectory_id: str) -> int:
    """遍历 contents：记录已有签名；给缺失签名的 model part 补回已知签名。

    返回补回的 part 数量。只处理 ``role == "model"`` 且带 ``text`` 的 part。
    """
    if not isinstance(contents, list):
        return 0
    restored = 0
    for content in contents:
        if not isinstance(content, dict) or content.get("role") != "model":
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str) or not text:
                continue
            signature = part.get("thoughtSignature") or part.get("thought_signature")
            if signature:
                remember(trajectory_id, text, signature)
                continue
            cached = lookup(trajectory_id, text)
            if cached:
                part["thoughtSignature"] = cached
                restored += 1
    return restored
