"""每凭证额度预算：定期刷新 retrieveUserQuotaSummary，低于阈值提前剔除候选。

防封思路：不要等 429 才发现额度耗尽——接近阈值就把该账号从凭证选取里
剔除，降低单账号被打穿的频率。额度数据是"防封建议"而非硬约束：

- 只有明确解析到 remainingFraction 且低于阈值才标记耗尽；
- 刷新失败或数据过期时按未耗尽处理（fail-open），不影响可用性；
- 后台任务由 web.py lifespan 启动，不阻塞请求路径。
"""

import asyncio
import time
from typing import Any, Dict, Optional, Set

from log import log

from config import (
    get_quota_budget_enabled,
    get_quota_budget_refresh_minutes,
    get_quota_budget_threshold,
)
from src.storage.database import get_database

# email → {"min_remaining": float|None, "checked_at": float, "exhausted": bool}
_state: Dict[str, Dict[str, Any]] = {}
_task: Optional[asyncio.Task] = None


def _min_remaining(quota_models: Dict[str, Any]) -> Optional[float]:
    """取所有配额桶里最小的 remainingFraction；没有数值时返回 None。"""
    values = [
        bucket.get("remaining")
        for bucket in quota_models.values()
        if isinstance(bucket, dict) and isinstance(bucket.get("remaining"), (int, float))
    ]
    return min(values) if values else None


async def refresh_all() -> Dict[str, int]:
    """刷新所有启用凭证的额度摘要，更新进程内状态。返回简单统计。"""
    # 循环依赖：antigravity → credential_manager → quota_budget → antigravity，
    # 不能在模块顶部导入 fetch_quota_info
    from src.api.antigravity import fetch_quota_info

    database = await get_database()
    threshold = await get_quota_budget_threshold()
    summary = {"checked": 0, "exhausted": 0, "failed": 0}

    for email in await database.list_credentials():
        credential = await database.get_credential(email)
        if not credential or credential.get("disabled"):
            _state.pop(email, None)
            continue

        token = credential.get("access_token") or credential.get("token")
        if not token:
            continue
        summary["checked"] += 1

        quota = await fetch_quota_info(
            token,
            credential.get("project_id", "") or "",
            proxy=credential.get("proxy") or None,
        )
        if not quota.get("success"):
            summary["failed"] += 1
            # 保留旧状态（exhausted_emails 会按 checked_at 过期 fail-open）
            continue

        remaining = _min_remaining(quota.get("models", {}))
        exhausted = remaining is not None and remaining <= threshold
        _state[email] = {
            "min_remaining": remaining,
            "checked_at": time.monotonic(),
            "exhausted": exhausted,
        }
        if exhausted:
            summary["exhausted"] += 1
            log.warning(
                f"[QUOTA_BUDGET] 账号 {email} 额度接近耗尽"
                f"（最小剩余 {remaining:.2%} ≤ 阈值 {threshold:.2%}），已从候选剔除"
            )

    return summary


async def exhausted_emails() -> Set[str]:
    """当前额度耗尽的账号集合（数据过期视为未耗尽，避免把请求卡死）。"""
    if not await get_quota_budget_enabled():
        return set()

    interval_s = max(60.0, (await get_quota_budget_refresh_minutes()) * 60.0)
    stale_after = interval_s * 2 + 600
    now = time.monotonic()
    return {
        email
        for email, state in _state.items()
        if state.get("exhausted") and now - state.get("checked_at", 0.0) <= stale_after
    }


async def quota_budget_loop() -> None:
    """后台刷新循环：启动即刷一次，之后按配置间隔轮询。"""
    while True:
        try:
            summary = await refresh_all()
            log.info(f"[QUOTA_BUDGET] 额度刷新完成: {summary}")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 后台任务失败不影响请求
            log.warning(f"[QUOTA_BUDGET] 额度刷新失败: {e}")
        await asyncio.sleep(max(60.0, (await get_quota_budget_refresh_minutes()) * 60.0))


def start() -> None:
    """启动后台刷新任务（web.py lifespan 调用；重复调用幂等）。"""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(quota_budget_loop())
        log.info("[QUOTA_BUDGET] 后台额度刷新任务已启动")


async def stop() -> None:
    """停止后台刷新任务（应用关闭时调用）。"""
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None


def reset_state() -> None:
    """清空进程内状态（测试用）。"""
    global _task
    _state.clear()
    _task = None
