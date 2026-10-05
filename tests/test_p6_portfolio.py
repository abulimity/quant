r"""P6.1 多策略净值组合与收益归因验收（LOCAL_DEPLOYMENT_PLAN.md §P6.1）。

组合语义：再平衡日重置目标权重，其余日按子策略当日收益漂移；单策略时是**恒等**。
验证锚点 = **手工复算**（子 NAV × 权重的加权收益），见各测试里的逐日手算表。

运行：
    $env:PYTHONIOENCODING='utf-8'
    uv run --project D:\project\quant python -m unittest tests.test_p6_portfolio -v
"""

from __future__ import annotations

import unittest

import pandas as pd

from quantlab.portfolio.compose import ComposeResult, attribute_returns, compose


class TestSingleStrategyIdentity(unittest.TestCase):
    def test_single_full_weight_is_identity(self) -> None:
        dates = pd.bdate_range("2020-01-02", periods=6)
        nav = pd.Series([1.0, 1.1, 0.99, 1.089, 1.2, 1.32], index=dates, name="A")
        tw = pd.DataFrame({"A": [1.0]}, index=dates[:1])

        result = compose({"A": nav}, tw, initial_equity=1.0)

        self.assertIsInstance(result, ComposeResult)
        # 单标的满仓时权重漂移恒为 1，组合净值 = 子策略净值
        # （freq 是 bdate_range 的元数据提示；compose 由 Timestamp 键重建索引，值/日期一致即可）
        pd.testing.assert_series_equal(result.equity, nav, check_names=False, check_freq=False)


class TestRebalanceHandRecompute(unittest.TestCase):
    def test_rebalance_then_drift_matches_manual_walk(self) -> None:
        dates = pd.bdate_range("2020-01-02", periods=4)       # d0..d3
        a = pd.Series([1.0, 1.1, 0.99, 0.99], index=dates)     # r: _, +0.1, -0.1, 0
        b = pd.Series([1.0, 1.0, 1.2, 1.2], index=dates)       # r: _, 0, +0.2, 0
        tw = pd.DataFrame({"A": [1.0, 0.0], "B": [0.0, 1.0]}, index=dates[[0, 2]])

        result = compose({"A": a, "B": b}, tw, initial_equity=1.0)

        # 手算：
        #   d0 再平衡 w=(1,0) → r=(0,0) → 1.0
        #   d1 漂移    w=(1,0) → r=(0.1,0) → 1.0*1.1 = 1.1
        #   d2 再平衡 w=(0,1) → r=(-0.1,0.2) → 1.1*1.2 = 1.32
        #   d3 漂移    w=(0,1) → r=(0,0) → 1.32
        expected = pd.Series([1.0, 1.1, 1.32, 1.32], index=dates)
        pd.testing.assert_series_equal(result.equity, expected, check_names=False,
                                       check_freq=False, atol=1e-12, rtol=1e-12)

    def test_weights_used_recorded_per_day(self) -> None:
        dates = pd.bdate_range("2020-01-02", periods=4)
        a = pd.Series([1.0, 1.1, 0.99, 0.99], index=dates)
        b = pd.Series([1.0, 1.0, 1.2, 1.2], index=dates)
        tw = pd.DataFrame({"A": [1.0, 0.0], "B": [0.0, 1.0]}, index=dates[[0, 2]])
        result = compose({"A": a, "B": b}, tw)
        self.assertEqual(list(result.weights_used.index), list(dates))
        self.assertAlmostEqual(result.weights_used.loc[dates[0], "A"], 1.0)
        self.assertAlmostEqual(result.weights_used.loc[dates[2], "B"], 1.0)


class TestComposeRejectsLeverage(unittest.TestCase):
    def test_row_sum_over_one_raises(self) -> None:
        dates = pd.bdate_range("2020-01-02", periods=3)
        a = pd.Series([1.0, 1.0, 1.0], index=dates)
        b = pd.Series([1.0, 1.0, 1.0], index=dates)
        tw = pd.DataFrame({"A": [1.1], "B": [0.1]}, index=dates[:1])   # 行和 1.2
        with self.assertRaises(ValueError):
            compose({"A": a, "B": b}, tw)


class TestAttributeReturns(unittest.TestCase):
    def test_splits_local_fx_interaction(self) -> None:
        local = pd.Series([0.1])
        fx = pd.Series([0.2])
        l, f, inter = attribute_returns(local, fx)
        self.assertAlmostEqual(float(l.iloc[0]), 0.1)
        self.assertAlmostEqual(float(f.iloc[0]), 0.2)
        self.assertAlmostEqual(float(inter.iloc[0]), 0.02)

    def test_exact_compound_identity(self) -> None:
        """F.6：1+R = (1+R_local)(1+R_fx)，三项之和只是**一阶近似**。"""
        local = pd.Series([0.1])
        fx = pd.Series([0.2])
        l, f, inter = attribute_returns(local, fx)
        exact = (1.0 + float(l.iloc[0])) * (1.0 + float(f.iloc[0])) - 1.0
        self.assertAlmostEqual(exact, 0.32)
        # 交互项恰好等于「精确值 − 本币 − 汇率」仅在本例（单项、无高阶交叉）
        self.assertAlmostEqual(exact, float(l.iloc[0]) + float(f.iloc[0]) + float(inter.iloc[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
