r"""P6.2 收益指标与年化口径验收（LOCAL_DEPLOYMENT_PLAN.md §P6.2）。

核心验收「年化口径按所用日历推导并全局统一，不写死 365/252」：

    V1  `derive_periods_per_year` 由数据**自身会话密度**推导：自然日日频 ≈ 365、
        交易日日频显著低于 365（≈ 250s），两个样本**出自同一入口**且值不同。
    V2  CAGR / 波动率 / Sharpe 的 `periods_per_year` 缺省一律走 `derive_periods_per_year`，
        不各自硬编码 252/365。
    V3  累计收益 / 最大回撤（带符号）手算对拍；Sharpe 无风险利率默认 0 并披露。

运行：
    $env:PYTHONIOENCODING='utf-8'
    uv run --project D:\project\quant python -m unittest tests.test_p6_metrics -v
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from quantlab.eval.metrics import (
    annualized_return,
    annualized_volatility,
    calmar_ratio,
    derive_periods_per_year,
    max_drawdown,
    performance_metrics,
    rebalance_count,
    sharpe_ratio,
    total_return,
    turnover,
)


class TestDerivePeriodsPerYear(unittest.TestCase):
    """V1：年化期数由数据自身会话密度推导，不写死 365/252。"""

    def test_calendar_daily_density_is_about_365(self) -> None:
        idx = pd.date_range("2015-01-01", "2024-12-31", freq="D")
        ppy = derive_periods_per_year(idx)
        # 含闰年的 10 年自然日：平均每年 ≈ 365.25
        self.assertAlmostEqual(ppy, 365.25, delta=2.0, msg=f"ppy={ppy}")

    def test_trading_day_density_is_well_below_365(self) -> None:
        idx = pd.bdate_range("2015-01-01", "2024-12-31")   # 仅剔除周末
        ppy = derive_periods_per_year(idx)
        self.assertGreater(ppy, 200.0, msg=f"ppy={ppy}")
        self.assertLess(ppy, 300.0, msg=f"交易日密度 {ppy} 竟接近自然日 365")

    def test_density_is_derived_not_constant(self) -> None:
        """若写死 365/252，两种输入会得到同一条硬编码值 —— 此断言会立刻红。"""
        cal = derive_periods_per_year(pd.date_range("2020-01-01", "2023-12-31", freq="D"))
        bus = derive_periods_per_year(pd.bdate_range("2020-01-01", "2023-12-31"))
        self.assertNotEqual(cal, bus)

    def test_too_few_sessions_yield_nan(self) -> None:
        self.assertTrue(np.isnan(derive_periods_per_year(pd.DatetimeIndex(["2020-01-01"]))))


class TestDrawdownAndReturn(unittest.TestCase):
    def test_max_drawdown_is_signed(self) -> None:
        equity = pd.Series([1.0, 1.2, 0.9, 1.1])
        # 峰值 1.2 → 谷底 0.9：回撤 = 0.9/1.2 - 1 = -0.25
        self.assertAlmostEqual(max_drawdown(equity), -0.25)
        self.assertLessEqual(max_drawdown(equity), 0.0)

    def test_total_return(self) -> None:
        equity = pd.Series([1.0, 1.1, 0.99, 1.1])
        self.assertAlmostEqual(total_return(equity), 0.1)

    def test_return_and_drawdown_cross_check(self) -> None:
        # 累计收益与带符号回撤量纲一致；回撤按「峰值」计、收益按「首值」计，
        # 此处钉住「回撤 ≤ 0 且有限」这一符号约定，供报告层交叉核验。
        equity = pd.Series([1.0, 1.2, 0.9, 1.1])
        self.assertTrue(np.isfinite(max_drawdown(equity)))
        self.assertAlmostEqual(max_drawdown(equity), 0.9 / 1.2 - 1.0)


class TestAnnualizationIsSingleEntry(unittest.TestCase):
    def test_annualized_return_matches_hand_cagr(self) -> None:
        returns = [0.10, -0.10, 0.20, 0.0]
        equity = pd.Series(np.cumprod(1.0 + np.array(returns)), name="equity")
        total = equity.iloc[-1] / equity.iloc[0]
        years = len(equity) / 4.0                       # 显式 4 期/年 → 1 年
        expected = total ** (1.0 / years) - 1.0
        self.assertAlmostEqual(annualized_return(equity, periods_per_year=4), expected)

    def test_default_uses_derive_periods_per_year(self) -> None:
        """缺省 `periods_per_year` 时，CAGR 必须与显式传 `derive_periods_per_year` 一致。"""
        idx = pd.bdate_range("2020-01-02", periods=200)
        rng = np.random.default_rng(0)
        equity = pd.Series(np.cumprod(1.0 + rng.normal(0.0005, 0.01, 200)), index=idx)
        derived = derive_periods_per_year(idx)
        self.assertAlmostEqual(
            annualized_return(equity),
            annualized_return(equity, periods_per_year=derived))

    def test_annualized_volatility_hand_check(self) -> None:
        r = pd.Series([0.01, -0.02, 0.03, 0.005])
        expected = float(r.std(ddof=1)) * np.sqrt(4.0)
        self.assertAlmostEqual(annualized_volatility(r, periods_per_year=4), expected)

    def test_sharpe_hand_check_and_risk_free(self) -> None:
        r = pd.Series([0.01, -0.02, 0.03, 0.005])
        ppy = 4.0
        excess = r - 0.0
        expected0 = float(excess.mean() / excess.std(ddof=1)) * np.sqrt(ppy)
        self.assertAlmostEqual(sharpe_ratio(r, periods_per_year=ppy), expected0)

        # 年化无风险利率按期化后从超额收益中扣除
        rf_annual = 0.04
        excess_rf = r - rf_annual / ppy
        expected_rf = float(excess_rf.mean() / excess_rf.std(ddof=1)) * np.sqrt(ppy)
        self.assertAlmostEqual(sharpe_ratio(r, periods_per_year=ppy, risk_free_rate=rf_annual),
                               expected_rf)


class TestTurnoverAndRebalance(unittest.TestCase):
    def test_turnover_is_sum_of_abs_delta(self) -> None:
        w = pd.DataFrame({"a": [0.0, 1.0, 0.0], "b": [0.0, 0.0, 1.0]})
        # |Δ|：第 1 行 0（首行相对无持仓）、第 2 行 |1|+|0|=1、第 3 行 |-1|+|1|=2
        self.assertAlmostEqual(turnover(w), 3.0)

    def test_rebalance_count_counts_changed_days(self) -> None:
        w = pd.DataFrame({"a": [0.0, 1.0, 1.0, 0.0]})
        self.assertEqual(rebalance_count(w), 2)          # 建仓一次、清仓一次


class TestPerformanceMetrics(unittest.TestCase):
    def test_always_declares_annualization(self) -> None:
        idx = pd.bdate_range("2020-01-02", periods=120)
        rng = np.random.default_rng(1)
        equity = pd.Series(np.cumprod(1.0 + rng.normal(0.0004, 0.01, 120)), index=idx)
        weights = pd.DataFrame({"1": [1.0] * 3}, index=idx[:3])

        metrics = performance_metrics(equity, weights=weights)
        self.assertIn("annualization_periods", metrics)
        self.assertIn("annualization_note", metrics)
        self.assertIn("total_return", metrics)
        self.assertIn("turnover", metrics)
        self.assertIn("n_rebalances", metrics)
        self.assertEqual(metrics["risk_free_rate"], 0.0)
        self.assertTrue(metrics["annualization_note"], "口径声明不得为空")


class TestCalmarRatio(unittest.TestCase):
    """Calmar 比率 = 年化收益 / |最大回撤|（研报核心指标）。"""

    def test_normal_positive_value(self) -> None:
        # 净值先涨后跌再涨：带符号回撤 = 0.9/1.2 - 1 = -0.25 → |回撤| = 0.25
        equity = pd.Series([1.0, 1.1, 1.2, 0.9, 1.1, 1.3])
        ppy = 4.0
        expected_ann = 1.3 ** (1.0 / (len(equity) / ppy)) - 1.0   # 年化收益 = 1.3^(1/1.5) − 1
        expected = expected_ann / 0.25
        value = calmar_ratio(equity, periods_per_year=ppy)
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, expected)
        self.assertGreater(value, 0.0)

    def test_zero_drawdown_returns_none(self) -> None:
        # 净值单调上涨 → max_drawdown = 0 → 分母为 0，比率无定义返回 None
        equity = pd.Series([1.0, 1.1, 1.2, 1.3])
        self.assertIsNone(calmar_ratio(equity, periods_per_year=4.0))

    def test_metric_name_present_in_performance_metrics(self) -> None:
        idx = pd.bdate_range("2020-01-02", periods=120)
        rng = np.random.default_rng(2)
        equity = pd.Series(np.cumprod(1.0 + rng.normal(0.0004, 0.01, 120)), index=idx)
        metrics = performance_metrics(equity)
        self.assertIn("calmar_ratio", metrics)
        # 与独立函数一致，钉住 performance_metrics 的接线
        self.assertEqual(metrics["calmar_ratio"], calmar_ratio(equity))


if __name__ == "__main__":
    unittest.main(verbosity=2)
