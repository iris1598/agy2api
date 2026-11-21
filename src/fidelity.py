"""保真度严格模式：统一错误类型与请求校验。

原则：无法证明"与真实 Antigravity 一致"的情况直接失败，不做静默降级。
所有拒绝都带稳定的 reason 码，便于按证据补规则。
"""

from typing import Any, Dict, Optional

from src.model_enum import get_model_param, has_model_enum

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


# antigravity_fix 的官方标配 maxOutputTokens 家族兜底值（学习值缺失时的回落，
# 客户端未指定或指定 64k 档时统一替换）。豁免这两个值不参与上限拒绝：
# 正常情况下注入值已与注册表上限一致（§10.16 学习值接入注入点），豁免只剩
# 一个防御作用——学习并发竞态（注入时无学习值、校验时恰好合并进上限）下
# 不误杀兜底默认请求。
_PROXY_DEFAULT_MAX_OUTPUT = (65536, 64000)

# 注册表 thinkingLevel 数值 → agent 路径线格式。实抓证据链：
# 注册表 thinkingLevel=2（hydrogen-medium）→ 线格式 "MEDIUM"（163547/0013）；
# 注册表 1 → "LOW"（flash-lite checkpoint 193455/0011）；注册表 100 → 数值 100
# （193455/0012）。4/5/6/7 的线格式无实抓证据，不映射（无证据不注入）。
_REGISTRY_LEVEL_WIRE = {1: "LOW", 2: "MEDIUM", 3: "HIGH", 100: 100}


def _thinking_level_from_model(model: str):
    """按模型解析官方 thinkingLevel 线格式（返回值即注入的 JSON 值）。

    优先级：注册表学习值（fetchAvailableModels 的 thinkingLevel 字段经
    _REGISTRY_LEVEL_WIRE 映射）→ 模型名后缀（-low/-medium/-high → 字符串、
    -xhigh → 数值 100）。两者都无证据返回 None（不校验不注入）。
    """
    learned = get_model_param(model or "", "thinkingLevel")
    if learned is not None and not isinstance(learned, bool):
        wire = _REGISTRY_LEVEL_WIRE.get(learned)
        if wire is not None:
            return wire
    lowered = (model or "").lower()
    if lowered.endswith("-xhigh"):
        return 100
    for suffix in ("low", "medium", "high"):
        if lowered.endswith("-" + suffix):
            return suffix.upper()
    return None


def _thinking_level_key(value) -> str:
    """thinkingLevel 的比较键：字符串档位大小写归一，数值档位取十进制文本。"""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(int(value))
    return str(value or "").upper()


def _validate_generation_config(request: Dict[str, Any], model: str) -> None:
    """校验/补齐 thinkingConfig，使其与模型档位一致。

    实抓样本里 thinkingLevel 与 ``gemini-4-*-(low|medium|high)`` 后缀对应
    （字符串），xhigh 档位为数值 100；始终带 includeThoughts。客户端给出
    冲突值即拒绝。
    """
    wanted = _thinking_level_from_model(model)
    if wanted is None:
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
        existing = thinking.get("thinkingLevel")
        if (
            existing is not None
            and _thinking_level_key(existing) != _thinking_level_key(wanted)
        ):
            raise FidelityError(
                "generation_config_mismatch",
                f"模型 {model} 要求 thinkingLevel={wanted}，客户端给出 {existing}",
            )
        thinking["thinkingLevel"] = wanted
        thinking.setdefault("includeThoughts", True)
        config["thinkingConfig"] = thinking

    # maxOutputTokens 上限只校验有实抓证据的模型；代理默认值豁免（见常量注释），
    # 客户端显式超限值直接拒绝（宁可报错，不静默降级）。
    limit = get_model_param(model, "maxOutputTokens")
    value = config.get("maxOutputTokens")
    if (
        limit is not None
        and value not in _PROXY_DEFAULT_MAX_OUTPUT
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > limit
    ):
        raise FidelityError(
            "generation_config_mismatch",
            f"模型 {model} 的 maxOutputTokens 实抓上限为 {limit}，客户端给出 {value}",
        )
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

    # 注册表标记 disabled 的模型（实抓：gemini-4-carbon-disabled）直接拒绝，
    # 宁可报错不发出一个真实客户端根本不可用的请求（fail-closed）。
    if model and get_model_param(model, "disabled") is True:
        raise FidelityError(
            "model_disabled",
            f"模型 {model} 在上游 fetchAvailableModels 注册表中标记为 disabled",
        )

    _validate_generation_config(request, model)
