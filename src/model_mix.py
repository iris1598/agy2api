"""模型分布约束（基础版）：限制单账号高价模型每日用量 + 恒定速率告警。

防封思路：单账号长时间高频打高价模型（Claude / Gemini 高档）是明显的
自动化特征。这里做两件事：

- 每账号每日高价模型请求计数，达到上限后该账号当天不再承接高价模型请求
  （``credential_manager.get_valid_credential`` 前置过滤；进程内计数，
  重启清零，按 UTC 日窗口滚动）；
- 记录每账号最近请求时间戳，出现"恒定速率"模式（间隔高度均匀）时打
  warning——只告警，不自动停用，由人工决定是否处置。

默认关闭（MODEL_MIX_ENABLED=false），开启后生效。
"""

import math
import time
from collections import deque
from typing import Any, Dict, Optional, Set

from log import log

from config import get_model_mix_daily_limit, get_model_mix_enabled

# 高价模型判定：Anthropic 家族 + Gemini 高档位（高价、长输出、特征明显）。
# 片段匹配与 src/model_enum.py 的模型命名对齐（如 gemini-4-hydrogen-high）。
PREMIUM_MODEL_FRAGMENTS = ("hydrogen-high", "hydrogen-xhigh", "hydrogen-tiered")

# email → {"date": int, "premium": int, "total": int, "ts": deque, "warned_at": float}
_state: Dict[str, Dict[str, Any]] = {}


def is_premium_model(model_name: Optional[str]) -> bool:
    """是否属于分布约束要限制的高价模型。"""
    if not model_name:
        return False
    lowered = model_name.lower()
    return lowered.startswith("claude") or any(f in lowered for f in PREMIUM_MODEL_FRAGMENTS)


def _today() -> int:
    return int(time.time() // 86400)  # UTC 日窗口；进程内计数不需要本地时区精度


def _entry(email: str) -> Dict[str, Any]:
    entry = _state.get(email)
    if entry is None or entry["date"] != _today():
        # warned_at=None 表示从未告警过（monotonic 在开机首小时可能小于 3600，
        # 不能用 0.0 当哨兵值）
        entry = {"date": _today(), "premium": 0, "total": 0, "ts": deque(maxlen=120), "warned_at": None}
        _state[email] = entry
    return entry


def note_request(email: str, model_name: Optional[str]) -> None:
    """每次实际出站请求前记录（凭证选定之后、发送之前调用）。"""
    if not email:
        return
    entry = _entry(email)
    entry["total"] += 1
    if is_premium_model(model_name):
        entry["premium"] += 1
    entry["ts"].append(time.monotonic())
    _detect_constant_rate(email, entry)


def _detect_constant_rate(email: str, entry: Dict[str, Any]) -> None:
    """近 30 次请求间隔高度均匀时告警（每小时每账号最多一次）。"""
    ts = list(entry["ts"])
    if len(ts) < 30:
        return
    now = time.monotonic()
    if entry["warned_at"] is not None and now - entry["warned_at"] < 3600:
        return

    # 相邻请求的间隔（ts[-31:] 与 ts[-30:] 在 len==30 时是同一份列表，不能直接 zip）
    intervals = [b - a for a, b in zip(ts, ts[1:])][-30:]
    if not intervals:
        return
    mean = sum(intervals) / len(intervals)
    if mean <= 0 or mean > 6.0:
        return
    stdev = math.sqrt(sum((x - mean) ** 2 for x in intervals) / len(intervals))
    if stdev / mean < 0.1:
        entry["warned_at"] = now
        log.warning(
            f"[MODEL_MIX] 账号 {email} 近 30 次请求间隔高度均匀"
            f"（均值 {mean:.1f}s，标准差 {stdev:.2f}s），疑似恒定速率自动化模式，"
            f"建议降低频率或分散账号"
        )


async def over_limit_emails(model_name: Optional[str]) -> Set[str]:
    """当天高价模型用量已达上限的账号集合（仅当目标模型也是高价模型时非空）。"""
    if not is_premium_model(model_name):
        return set()
    if not await get_model_mix_enabled():
        return set()
    limit = await get_model_mix_daily_limit()
    if limit <= 0:
        return set()

    today = _today()
    return {
        email
        for email, entry in _state.items()
        if entry["date"] == today and entry["premium"] >= limit
    }


def reset_state() -> None:
    """清空进程内状态（测试用）。"""
    _state.clear()
