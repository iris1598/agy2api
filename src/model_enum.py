"""Antigravity 模型枚举（``MODEL_PLACEHOLDER_*``）映射。

上游 ``labels.model_enum`` 用的是内部枚举值而不是模型名。已知映射来自
``tools/antigravity_capture`` 的实抓数据（用 ``sniff_cli.py models`` 可从
``fetchAvailableModels`` 响应导出）。未收录的模型回退为模型名，保持既有行为，
避免未知枚举触发上游 400。
"""

import re
from typing import Any, Dict, Optional

# 实抓确认的映射（2026-11-21，language_server 2.34.0，fetchAvailableModels 全量响应）
MODEL_ENUM_MAP: Dict[str, str] = {
    "gemini-2.5-flash": "MODEL_GOOGLE_GEMINI_2_5_FLASH",
    "gemini-2.5-flash-thinking": "MODEL_GOOGLE_GEMINI_2_5_FLASH_THINKING",
    "gemini-2.5-flash-lite": "MODEL_GOOGLE_GEMINI_2_5_FLASH_LITE",
    "gemini-2.5-pro": "MODEL_GOOGLE_GEMINI_2_5_PRO",
    "gemini-4-hydrogen-low": "MODEL_PLACEHOLDER_M527",
    "gemini-4-hydrogen-medium": "MODEL_PLACEHOLDER_M526",
    "gemini-4-hydrogen-high": "MODEL_PLACEHOLDER_M525",
    "gemini-4-hydrogen-xhigh": "MODEL_PLACEHOLDER_M528",
    "gemini-4-hydrogen-tiered-high": "MODEL_PLACEHOLDER_M530",
    "gemini-4-hydrogen-tiered-xhigh": "MODEL_PLACEHOLDER_M531",
    "gemini-4-hydrogen-sonar": "MODEL_PLACEHOLDER_M578",
    "gemini-flash-worker-agy": "MODEL_PLACEHOLDER_M669",
    "gemini-4-neon-low": "MODEL_PLACEHOLDER_M534",
    "gemini-4-neon-medium": "MODEL_PLACEHOLDER_M533",
    "gemini-4-neon-high": "MODEL_PLACEHOLDER_M532",
    "gemini-4-neon-xhigh": "MODEL_PLACEHOLDER_M535",
    "gemini-4-argon-low": "MODEL_PLACEHOLDER_M419",
    "gemini-4-argon-medium": "MODEL_PLACEHOLDER_M418",
    "gemini-4-argon-high": "MODEL_PLACEHOLDER_M417",
    "gemini-4-carbon-disabled": "MODEL_PLACEHOLDER_M670",
    "gemini-3-flash": "MODEL_PLACEHOLDER_M18",
    "gemini-3.5-flash-lite": "MODEL_PLACEHOLDER_M198",
    "gemini-3.8-flash-low": "MODEL_PLACEHOLDER_M320",
    "gemini-3.8-flash-medium": "MODEL_PLACEHOLDER_M319",
    "gemini-3.8-flash-high": "MODEL_PLACEHOLDER_M318",
    "gemini-3.8-flash-tiered": "MODEL_PLACEHOLDER_M322",
    "gemini-3.1-flash-lite": "MODEL_PLACEHOLDER_M50",
    "gemini-3.1-flash-image": "MODEL_PLACEHOLDER_M21",
    "gemini-pro-orchestrator-neon": "MODEL_PLACEHOLDER_M537",
    "gemini-pro-orchestrator-g1-pro": "MODEL_PLACEHOLDER_M668",
    "claude-opus-5-5-low": "MODEL_PLACEHOLDER_M400",
    "claude-opus-5-5-medium": "MODEL_PLACEHOLDER_M401",
    "claude-opus-5-5-high": "MODEL_PLACEHOLDER_M402",
    "claude-sonnet-5-5-low": "MODEL_PLACEHOLDER_M403",
    "claude-sonnet-5-5-medium": "MODEL_PLACEHOLDER_M404",
    "claude-sonnet-5-5-high": "MODEL_PLACEHOLDER_M405",
    "gpt-oss-120b-medium": "MODEL_OPENAI_GPT_OSS_120B_MEDIUM",
    "tab_flash_lite_preview": "MODEL_PLACEHOLDER_M19",
    "tab_jump_flash_lite_preview": "MODEL_PLACEHOLDER_M28",
}

# 枚举值形如 MODEL_PLACEHOLDER_M526 / MODEL_GOOGLE_GEMINI_2_5_FLASH；
# 排除 fetchAvailableModels 里同级的 MODEL_PROVIDER_* 字段。
_MODEL_ENUM_RE = re.compile(r"\bMODEL_(?!PROVIDER_)[A-Z][A-Z0-9_]*\b")

# 实抓确认的模型参数（2026-11-21）：gemini-2.5-flash 的 metadata
# maxOutputTokens=65535；gemini-4-hydrogen 主力档（low/medium/high/xhigh）
# 的 agent 请求 maxOutputTokens=262144。仅收录有实抓证据的条目，
# 没有证据的模型不参与上限校验（保持透传）。
MODEL_PARAMS: Dict[str, Dict[str, Any]] = {
    "gemini-2.5-flash": {"maxOutputTokens": 65535},
    "gemini-4-hydrogen-low": {"maxOutputTokens": 262144},
    "gemini-4-hydrogen-medium": {"maxOutputTokens": 262144},
    "gemini-4-hydrogen-high": {"maxOutputTokens": 262144},
    "gemini-4-hydrogen-xhigh": {"maxOutputTokens": 262144},
}

# fetchAvailableModels 响应里关注的参数键；缺失的键不记录。
_PARAM_KEYS = ("maxOutputTokens", "supportsThinking", "minThinkingBudget")

# 进程内学习到的映射（fetchAvailableModels 探测时刷新，覆盖内置表）
_LEARNED_ENUM_MAP: Dict[str, str] = {}
# 进程内学习到的模型参数（按模型整体合并，覆盖内置表）
_LEARNED_PARAMS: Dict[str, Dict[str, Any]] = {}


def _find_enum(node: Any, depth: int = 0) -> str:
    """在任意嵌套结构里找 ``MODEL_PLACEHOLDER_*`` 字符串（优先显式键）。"""
    if depth > 6:
        return ""
    if isinstance(node, str):
        found = _MODEL_ENUM_RE.search(node)
        return found.group(0) if found else ""
    if isinstance(node, dict):
        for key in ("modelEnum", "model_enum", "model", "enum", "id", "name"):
            if key in node:
                found = _find_enum(node[key], depth + 1)
                if found:
                    return found
        for value in node.values():
            found = _find_enum(value, depth + 1)
            if found:
                return found
        return ""
    if isinstance(node, list):
        for value in node:
            found = _find_enum(value, depth + 1)
            if found:
                return found
    return ""


def parse_model_enums(data: Any) -> Dict[str, str]:
    """从 ``fetchAvailableModels`` 响应中提取 ``model_id -> 枚举值`` 映射。

    响应形状未固化，这里对 ``models`` 字段做宽松遍历（dict / list 均可）。
    """
    if not isinstance(data, dict):
        return {}
    models = data.get("models")
    items: list = []
    if isinstance(models, dict):
        items = list(models.items())
    elif isinstance(models, list):
        for entry in models:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id") or entry.get("modelId") or entry.get("name") or ""
            items.append((str(model_id), entry))

    mapping: Dict[str, str] = {}
    for model_id, info in items:
        if not model_id:
            continue
        enum = _find_enum(info)
        if enum:
            mapping[str(model_id)] = enum
    return mapping


def merge_model_enums(mapping: Optional[Dict[str, str]]) -> None:
    """合并探测到的映射到进程内缓存。"""
    if not mapping:
        return
    _LEARNED_ENUM_MAP.update(mapping)


def _find_value(node: Any, key: str, depth: int = 0) -> Any:
    """在任意嵌套结构里找指定键的值（响应形状未固化，宽松遍历）。"""
    if depth > 6:
        return None
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find_value(value, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_value(value, key, depth + 1)
            if found is not None:
                return found
    return None


def parse_model_params(data: Any) -> Dict[str, Dict[str, Any]]:
    """从 ``fetchAvailableModels`` 响应中提取 ``model_id -> 参数元数据``。

    目前关注 maxOutputTokens / supportsThinking / minThinkingBudget；
    只记录响应里真实出现过的键。
    """
    if not isinstance(data, dict):
        return {}
    models = data.get("models")
    items: list = []
    if isinstance(models, dict):
        items = list(models.items())
    elif isinstance(models, list):
        for entry in models:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id") or entry.get("modelId") or entry.get("name") or ""
            items.append((str(model_id), entry))

    params: Dict[str, Dict[str, Any]] = {}
    for model_id, info in items:
        if not model_id or not isinstance(info, dict):
            continue
        found: Dict[str, Any] = {}
        for key in _PARAM_KEYS:
            value = _find_value(info, key)
            if value is not None:
                found[key] = value
        if found:
            params[str(model_id)] = found
    return params


def merge_model_params(mapping: Optional[Dict[str, Dict[str, Any]]]) -> None:
    """合并学习到的模型参数到进程内缓存（按键覆盖，不整体替换）。"""
    if not mapping:
        return
    for model, info in mapping.items():
        _LEARNED_PARAMS.setdefault(model, {}).update(info)


def get_model_param(model: str, key: str) -> Any:
    """查询模型参数（如 maxOutputTokens）；无实抓/学习值返回 None。"""
    if not model:
        return None
    learned = _LEARNED_PARAMS.get(model)
    if learned and key in learned:
        return learned[key]
    return MODEL_PARAMS.get(model, {}).get(key)


def clear_learned_enums() -> None:
    """清空进程内缓存（测试用）。"""
    _LEARNED_ENUM_MAP.clear()
    _LEARNED_PARAMS.clear()


def has_model_enum(model: str) -> bool:
    """该模型是否已有实抓/学习到的枚举值（严格模式的前置条件）。"""
    if not model:
        return False
    return model in _LEARNED_ENUM_MAP or model in MODEL_ENUM_MAP


def resolve_model_enum(model: str) -> str:
    """模型名 → 上游枚举值；未收录时回退为模型名。"""
    if not model:
        return ""
    return _LEARNED_ENUM_MAP.get(model) or MODEL_ENUM_MAP.get(model, model)
