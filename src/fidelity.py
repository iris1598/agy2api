"""保真度严格模式：统一错误类型与请求校验。

原则：无法证明"与真实 Antigravity 一致"的情况直接失败，不做静默降级。
所有拒绝都带稳定的 reason 码，便于按证据补规则。
"""

from typing import Any, Dict, Optional

from src.model_enum import has_model_enum

# reason → HTTP 状态码
_STATUS_BY_REASON = {
    "unknown_model_enum": 400,
    "unsupported_client_field": 400,
    "unsupported_content": 400,
    "generation_config_mismatch": 400,
    "envelope_schema_mismatch": 500,
    "tls_sidecar_unavailable": 503,
}

# 实抓样本中出现过的内层请求字段（白名单）；labels 由代理生成，客户端不该传。
ALLOWED_REQUEST_KEYS = frozenset(
    {
        "contents",
        "systemInstruction",
        "tools",
        "toolConfig",
        "generationConfig",
        "sessionId",
    }
)


class FidelityError(Exception):
    """无法保证与真实客户端一致时抛出（默认映射为 4xx/5xx 响应）。"""

    def __init__(
        self,
        reason: str,
        detail: str = "",
        status_code: Optional[int] = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.status_code = status_code or _STATUS_BY_REASON.get(reason, 500)
        super().__init__(f"{reason}: {detail}" if detail else reason)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "error": {
                "type": "fidelity_rejected",
                "reason": self.reason,
                "detail": self.detail,
            }
        }


def _thinking_level_from_model(model: str) -> str:
    lowered = (model or "").lower()
    for suffix in ("low", "medium", "high"):
        if lowered.endswith("-" + suffix):
            return suffix.upper()
    return ""


def _validate_generation_config(request: Dict[str, Any], model: str) -> None:
    """校验/补齐 thinkingConfig，使其与模型档位一致。

    实抓样本里 thinkingLevel 与 ``gemini-4-*-(low|medium|high)`` 后缀一一对应，
    且始终带 includeThoughts。客户端给出冲突值即拒绝。
    """
    wanted = _thinking_level_from_model(model)
    if not wanted:
        return

    config = request.get("generationConfig")
    if not isinstance(config, dict):
        request["generationConfig"] = {
            "thinkingConfig": {"includeThoughts": True, "thinkingLevel": wanted}
        }
        return

    # 浅拷贝后写回，避免修改调用方共享的嵌套 dict
    config = dict(config)
    thinking = config.get("thinkingConfig")
    if not isinstance(thinking, dict):
        config["thinkingConfig"] = {
            "includeThoughts": True,
            "thinkingLevel": wanted,
        }
    else:
        thinking = dict(thinking)
        existing = str(thinking.get("thinkingLevel") or "").upper()
        if existing and existing != wanted:
            raise FidelityError(
                "generation_config_mismatch",
                f"模型 {model} 要求 thinkingLevel={wanted}，客户端给出 {existing}",
            )
        thinking["thinkingLevel"] = wanted
        thinking.setdefault("includeThoughts", True)
        config["thinkingConfig"] = thinking
    request["generationConfig"] = config


def validate_client_request(request: Dict[str, Any], model: str) -> None:
    """严格模式下的入站请求校验（就地修正 generationConfig，其余只校验）。"""
    unknown = [key for key in request.keys() if key not in ALLOWED_REQUEST_KEYS]
    if unknown:
        raise FidelityError(
            "unsupported_client_field",
            f"真实客户端不会发送这些字段: {', '.join(sorted(unknown))}",
        )

    if model and not has_model_enum(model):
        raise FidelityError(
            "unknown_model_enum",
            f"模型 {model} 没有 MODEL_PLACEHOLDER 映射；先抓包并更新 src/model_enum.py",
        )

    _validate_generation_config(request, model)
