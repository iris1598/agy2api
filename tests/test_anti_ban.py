"""P2 防封特性的单元测试：额度预算、模型分布约束、指数退避。"""

import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest  # noqa: E402

from src import model_mix, quota_budget  # noqa: E402
from src.api import utils as api_utils  # noqa: E402


def _flag(value):
    async def _get():
        return value

    return _get


class TestQuotaBudget:
    """每凭证额度预算：阈值判定 + 数据过期 fail-open。"""

    def setup_method(self):
        quota_budget.reset_state()

    def teardown_method(self):
        quota_budget.reset_state()

    def test_min_remaining_ignores_non_numeric(self):
        models = {
            "b1": {"remaining": 0.5},
            "b2": {"remaining": 0.05},
            "b3": {"remaining": "n/a"},
        }
        assert quota_budget._min_remaining(models) == 0.05
        assert quota_budget._min_remaining({}) is None
        assert quota_budget._min_remaining({"b": {"remaining": "x"}}) is None

    async def test_exhausted_emails_and_fail_open(self, monkeypatch):
        monkeypatch.setattr("src.quota_budget.get_quota_budget_enabled", _flag(True))
        monkeypatch.setattr(
            "src.quota_budget.get_quota_budget_refresh_minutes", _flag(30)
        )
        quota_budget._state["a@x"] = {
            "min_remaining": 0.01,
            "checked_at": time.monotonic(),
            "exhausted": True,
        }
        assert "a@x" in await quota_budget.exhausted_emails()

        # 数据过期（超过 2×间隔 + 600s）→ 按未耗尽处理，不卡死请求
        quota_budget._state["a@x"]["checked_at"] = time.monotonic() - 30 * 60 * 2 - 601
        assert await quota_budget.exhausted_emails() == set()

    async def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setattr("src.quota_budget.get_quota_budget_enabled", _flag(False))
        quota_budget._state["a@x"] = {
            "min_remaining": 0.0,
            "checked_at": time.monotonic(),
            "exhausted": True,
        }
        assert await quota_budget.exhausted_emails() == set()


class TestModelMix:
    """模型分布约束：高价模型判定、每日上限、恒定速率告警。"""

    def setup_method(self):
        model_mix.reset_state()

    def teardown_method(self):
        model_mix.reset_state()

    def test_premium_model_detection(self):
        assert model_mix.is_premium_model("claude-sonnet-5-5-low")
        assert model_mix.is_premium_model("gemini-4-hydrogen-high")
        assert model_mix.is_premium_model("gemini-4-hydrogen-tiered-xhigh")
        assert not model_mix.is_premium_model("gemini-2.5-flash")
        assert not model_mix.is_premium_model("gemini-4-hydrogen-medium")
        assert not model_mix.is_premium_model(None)

    async def test_over_limit_emails(self, monkeypatch):
        monkeypatch.setattr("src.model_mix.get_model_mix_enabled", _flag(True))
        monkeypatch.setattr("src.model_mix.get_model_mix_daily_limit", _flag(3))
        for _ in range(3):
            model_mix.note_request("busy@x", "claude-opus-5-5-high")
        model_mix.note_request("busy@x", "gemini-2.5-flash")  # 不计入高价

        assert "busy@x" in await model_mix.over_limit_emails("claude-opus-5-5-high")
        # 非高价目标模型不受约束
        assert await model_mix.over_limit_emails("gemini-2.5-flash") == set()

    async def test_disabled_never_limits(self, monkeypatch):
        monkeypatch.setattr("src.model_mix.get_model_mix_enabled", _flag(False))
        for _ in range(10):
            model_mix.note_request("a@x", "claude-opus-5-5-high")
        assert await model_mix.over_limit_emails("claude-opus-5-5-high") == set()

    async def test_daily_window_reset(self, monkeypatch):
        monkeypatch.setattr("src.model_mix.get_model_mix_enabled", _flag(True))
        monkeypatch.setattr("src.model_mix.get_model_mix_daily_limit", _flag(0))
        model_mix.note_request("a@x", "claude-opus-5-5-high")
        assert await model_mix.over_limit_emails("claude-opus-5-5-high") == set()

    def test_constant_rate_warning(self):
        entry = model_mix._entry("steady@x")
        now = time.monotonic()
        for i in range(30):
            entry["ts"].append(now - (30 - i) * 2.0)  # 均匀 2s 间隔
        model_mix._detect_constant_rate("steady@x", entry)
        assert entry["warned_at"] is not None

    def test_no_warning_for_jittered_intervals(self):
        entry = model_mix._entry("jitter@x")
        now = time.monotonic()
        offsets = [
            0, 2, 5, 6, 12, 14, 20, 21, 30, 31, 40, 42, 50, 51, 60,
            63, 70, 71, 80, 82, 90, 91, 100, 103, 110, 111, 120, 122, 130, 131,
        ]
        for off in offsets:
            entry["ts"].append(now - (140 - off))
        model_mix._detect_constant_rate("jitter@x", entry)
        assert entry["warned_at"] is None


class TestBackoff:
    """指数退避 + 抖动：首次固定、连败指数增长、封顶、成功复位。"""

    def setup_method(self):
        api_utils._consecutive_failures.clear()

    def teardown_method(self):
        api_utils._consecutive_failures.clear()

    async def test_first_failure_keeps_base_interval(self, monkeypatch):
        monkeypatch.setattr("src.api.utils.get_backoff_enabled", _flag(True))
        monkeypatch.setattr("src.api.utils.get_backoff_max_delay", _flag(60))
        delay = await api_utils._backoff_delay(1.0, 1)
        assert 1.0 <= delay <= 2.0  # interval + 抖动（≤1s）

    async def test_exponential_growth_with_cap(self, monkeypatch):
        monkeypatch.setattr("src.api.utils.get_backoff_enabled", _flag(True))
        monkeypatch.setattr("src.api.utils.get_backoff_max_delay", _flag(60))
        delay5 = await api_utils._backoff_delay(1.0, 5)
        assert delay5 >= 16.0  # 1 × 2^4
        delay_big = await api_utils._backoff_delay(1.0, 12)
        assert delay_big <= 61.0  # 封顶 60s + 抖动 ≤1s

    async def test_disabled_uses_fixed_interval(self, monkeypatch):
        monkeypatch.setattr("src.api.utils.get_backoff_enabled", _flag(False))
        assert await api_utils._backoff_delay(1.5, 8) == 1.5

    async def test_success_resets_counter(self):
        api_utils._note_retry_failure("a@x")
        api_utils._note_retry_failure("a@x")
        assert api_utils._consecutive_failures.get("a@x") == 2
        api_utils.note_retry_success("a@x")
        assert api_utils._consecutive_failures.get("a@x") is None
