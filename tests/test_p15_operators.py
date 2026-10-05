"""P15 算子地基（算术/数学 + 专用窗口）验收。

「多资产 ETF 轮动」研报复现的**阶段 1**：只补策略表达式 DSL 的一等算子，
不碰数据契约 / 组合 / 指标。本文件钉住：

    · evaluate 层：add/sub/mul/div/neg/log/exp（逐元素）、
      linreg_slope/linreg_r2/llt/zscore/ewm_std（全部因果，只向后看）
    · 因果性：扰动未来不改变过去的输出（每个新算子）
    · lint 层：G5 lookback 覆盖新窗口算子、G6 参数校验（window/alpha/span 三口径）
    · DSL 层：13 个新算子的 parse、非法参数拒绝、白名单同步

全部用**本地合成面板**，零 fixture / 数据层 / 联网。

口径（与 emit.py / dsl.py / lint.py 一致）：
    · 算术/数学是**逐元素**算子，无窗口，不参与 G5/G6。
    · 窗口算子统一自动 shift(1)：linreg_slope/linreg_r2/zscore 用整数 window，
      llt 用 alpha ∈ (0,1)，ewm_std 用 span ≥ 1（可为浮点）。
    · linreg 对 log(close) 做**等权**滚动 OLS（论文用时间加权，此处等权为近似）。
    · llt 是二阶低滞后滤波，直流增益为 1（恒定输入收敛到该常数）。
    · ewm_std 是收益率的指数加权波动率（pct_change 后 EWM std）。

运行：
    $env:PYTHONIOENCODING='utf-8'
    python -m unittest tests.test_p15_operators -v
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from quantlab.contract.emit import _SUPPORTED_OPS, evaluate
from quantlab.contract.lint import lint_spec
from quantlab.contract.types import (
    F8_SCENARIOS,
    ContractViolation,
    DataRequirement,
    Expr,
    SizingSpec,
    StrategySpec,
)
from quantlab.x2.dsl import (
    DSL_ALL_OPS,
    DslParseError,
    dsl_to_spec,
    parse_dsl_node,
)


# --------------------------------------------------------------------------- #
# 合成数据
# --------------------------------------------------------------------------- #
def _panel() -> pd.DataFrame:
    """2 symbol × 3 日的小面板，用于手算逐元素断言。"""
    idx = pd.bdate_range("2024-01-01", periods=3, name="ts")
    return pd.DataFrame({1: [10.0, 20.0, 30.0], 2: [1.0, 2.0, 3.0]}, index=idx)


def _geometric_prices(n: int = 60) -> pd.DataFrame:
    """两列几何级数：log(close) 是线性，滚动 OLS 斜率/拟合优度可手算。"""
    idx = pd.bdate_range("2020-01-01", periods=n, name="ts")
    return pd.DataFrame({
        1: 1.01 ** np.arange(n),      # log 斜率 = log(1.01)
        2: 1.03 ** np.arange(n),      # log 斜率 = log(1.03)
    }, index=idx)


def _zscore_panel() -> pd.DataFrame:
    """一升一降两列，zscore(window=2) 的手算值为 ±√2/2。"""
    idx = pd.bdate_range("2024-01-01", periods=5, name="ts")
    return pd.DataFrame({1: [1.0, 2.0, 3.0, 4.0, 5.0],
                         2: [5.0, 4.0, 3.0, 2.0, 1.0]}, index=idx)


def _trending_prices(days: int = 120, symbols=(1, 2, 3), seed: int = 11) -> pd.DataFrame:
    """确定性趋势 + 噪声面板（因果性测试用）。"""
    idx = pd.bdate_range("2020-01-01", periods=days, name="ts")
    rng = np.random.default_rng(seed)
    data = {}
    for offset, symbol in enumerate(symbols):
        steps = 0.0005 * (offset + 1) + 0.01 * rng.standard_normal(days)
        data[symbol] = 100.0 * np.exp(np.cumsum(steps))
    return pd.DataFrame(data, index=idx)


def _field() -> Expr:
    return Expr("field", ("close",))


def _win(op: str, param: float) -> Expr:
    """窗口算子（DSL 口径：自动 shift(1)），供 lint 测试。"""
    return Expr(op, (Expr("shift", (_field(), 1)), param))


def _spec(entry: Expr, *, lookback: int = 60, universe=(1,)) -> StrategySpec:
    return StrategySpec(
        name="ops", universe=universe, entry=entry,
        sizing=SizingSpec(top_n=1, rebalance="W-MON"),
        costs=F8_SCENARIOS[0], lookback=lookback,
        data_requirements=(DataRequirement("bars_daily", ("ts", "close", "available_utc")),),
    )


# --------------------------------------------------------------------------- #
# 算术 / 数学（逐元素）
# --------------------------------------------------------------------------- #
class TestArithmetic(unittest.TestCase):
    def test_add_field_const(self) -> None:
        got = evaluate(Expr("add", (_field(), Expr("const", (5.0,)))), _panel())
        pd.testing.assert_frame_equal(got, _panel() + 5.0)

    def test_add_two_fields(self) -> None:
        got = evaluate(Expr("add", (_field(), _field())), _panel())
        pd.testing.assert_frame_equal(got, _panel() * 2.0)

    def test_sub_respects_left_right_order(self) -> None:
        got = evaluate(Expr("sub", (Expr("const", (10.0,)), _field())), _panel())
        pd.testing.assert_frame_equal(got, 10.0 - _panel())

    def test_mul(self) -> None:
        got = evaluate(Expr("mul", (_field(), Expr("const", (2.0,)))), _panel())
        pd.testing.assert_frame_equal(got, _panel() * 2.0)

    def test_div(self) -> None:
        got = evaluate(Expr("div", (_field(), Expr("const", (2.0,)))), _panel())
        pd.testing.assert_frame_equal(got, _panel() / 2.0)

    def test_neg(self) -> None:
        got = evaluate(Expr("neg", (_field(),)), _panel())
        pd.testing.assert_frame_equal(got, -_panel())

    def test_log(self) -> None:
        got = evaluate(Expr("log", (_field(),)), _panel())
        pd.testing.assert_frame_equal(got, np.log(_panel()))

    def test_exp_const(self) -> None:
        got = evaluate(Expr("exp", (Expr("const", (1.0,)),)), _panel())
        expected = pd.DataFrame(np.e, index=_panel().index, columns=_panel().columns)
        pd.testing.assert_frame_equal(got, expected)

    def test_wrong_arity_is_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            evaluate(Expr("add", (_field(),)), _panel())
        with self.assertRaises(ContractViolation):
            evaluate(Expr("neg", ()), _panel())


# --------------------------------------------------------------------------- #
# 专用窗口算子
# --------------------------------------------------------------------------- #
class TestLinreg(unittest.TestCase):
    def test_slope_matches_known_log_linear_growth(self) -> None:
        prices = _geometric_prices()
        slope = evaluate(Expr("linreg_slope", (_field(), 5)), prices)
        self.assertTrue(np.allclose(slope.iloc[4:, 0], np.log(1.01), rtol=1e-6))
        self.assertTrue(np.allclose(slope.iloc[4:, 1], np.log(1.03), rtol=1e-6))

    def test_r2_is_one_for_perfect_fit(self) -> None:
        prices = _geometric_prices()
        r2 = evaluate(Expr("linreg_r2", (_field(), 5)), prices)
        self.assertTrue(np.allclose(r2.iloc[4:], 1.0, atol=1e-6))

    def test_slope_and_r2_are_nan_before_warmup(self) -> None:
        prices = _geometric_prices()
        slope = evaluate(Expr("linreg_slope", (_field(), 5)), prices)
        self.assertTrue(slope.iloc[:4].isna().all().all())


class TestLlt(unittest.TestCase):
    def test_first_outputs_match_hand_computation(self) -> None:
        idx = pd.bdate_range("2024-01-01", periods=6, name="ts")
        prices = pd.DataFrame({1: [1.0] * 6}, index=idx)
        out = evaluate(Expr("llt", (_field(), 0.10)), prices)
        self.assertTrue(np.isnan(out.iloc[0, 0]))
        self.assertTrue(np.isnan(out.iloc[1, 0]))
        self.assertAlmostEqual(out.iloc[2, 0], 0.01, places=9)    # c0+c1+c2 = α²
        self.assertAlmostEqual(out.iloc[3, 0], 0.028, places=9)   # α² + 2(1−α)·α²

    def test_dc_gain_converges_to_constant(self) -> None:
        idx = pd.bdate_range("2020-01-01", periods=200, name="ts")
        prices = pd.DataFrame({1: [100.0] * 200}, index=idx)
        out = evaluate(Expr("llt", (_field(), 0.10)), prices)
        self.assertTrue(np.isclose(out.iloc[-1, 0], 100.0, rtol=1e-6))

    def test_alpha_must_be_in_unit_interval(self) -> None:
        for bad in (0.0, 1.0, 1.5, -0.1):
            with self.subTest(alpha=bad):
                with self.assertRaises(ContractViolation):
                    evaluate(Expr("llt", (_field(), bad)), _panel())


class TestZscore(unittest.TestCase):
    def test_monotonic_series_has_constant_zscore(self) -> None:
        out = evaluate(Expr("zscore", (_field(), 2)), _zscore_panel())
        expected_up = np.sqrt(2.0) / 2.0
        self.assertTrue(np.allclose(out.iloc[1:, 0], expected_up))
        self.assertTrue(np.allclose(out.iloc[1:, 1], -expected_up))

    def test_nan_before_warmup(self) -> None:
        out = evaluate(Expr("zscore", (_field(), 2)), _zscore_panel())
        self.assertTrue(np.isnan(out.iloc[0, 0]))

    def test_window_must_be_positive_int(self) -> None:
        with self.assertRaises(ContractViolation):
            evaluate(Expr("zscore", (_field(), 0)), _panel())


class TestEwmStd(unittest.TestCase):
    def test_constant_returns_have_zero_volatility(self) -> None:
        idx = pd.bdate_range("2020-01-01", periods=40, name="ts")
        prices = pd.DataFrame({1: 1.01 ** np.arange(40)}, index=idx)
        span = 20
        out = evaluate(Expr("ewm_std", (_field(), span)), prices)
        self.assertTrue(out.iloc[:int(span)].isna().all().all())
        self.assertTrue(np.allclose(out.iloc[int(span):].to_numpy().ravel(), 0.0, atol=1e-10))

    def test_span_must_be_at_least_one(self) -> None:
        for bad in (0.0, 0.5, -2.0):
            with self.subTest(span=bad):
                with self.assertRaises(ContractViolation):
                    evaluate(Expr("ewm_std", (_field(), bad)), _panel())


# --------------------------------------------------------------------------- #
# 因果性：扰动未来不改变过去
# --------------------------------------------------------------------------- #
class TestCausality(unittest.TestCase):
    def _check(self, expr: Expr, prices: pd.DataFrame) -> None:
        cut = len(prices) // 2
        base = evaluate(expr, prices)
        perturbed = prices.copy()
        perturbed.iloc[cut + 1:] *= 3.0
        after = evaluate(expr, perturbed)
        past = base.index[: cut + 1]
        pd.testing.assert_frame_equal(base.loc[past], after.loc[past], check_dtype=False)

    def test_arithmetic_is_causal(self) -> None:
        prices = _trending_prices()
        self._check(Expr("add", (_field(), Expr("const", (1.0,)))), prices)
        self._check(Expr("neg", (_field(),)), prices)
        self._check(Expr("log", (_field(),)), prices)

    def test_window_ops_are_causal(self) -> None:
        prices = _trending_prices()
        self._check(Expr("linreg_slope", (_field(), 20)), prices)
        self._check(Expr("linreg_r2", (_field(), 20)), prices)
        self._check(Expr("zscore", (_field(), 20)), prices)
        self._check(Expr("llt", (_field(), 0.10)), prices)
        self._check(Expr("ewm_std", (_field(), 20)), prices)


# --------------------------------------------------------------------------- #
# lint：G5 lookback 覆盖 + G6 参数校验
# --------------------------------------------------------------------------- #
class TestLintValidation(unittest.TestCase):
    def test_new_window_ops_count_toward_lookback(self) -> None:
        for op, param, lookback in (("zscore", 63, 63), ("linreg_slope", 63, 63),
                                    ("llt", 0.10, 20), ("ewm_std", 20, 20)):
            with self.subTest(op=op):
                short = lint_spec(_spec(_win(op, param), lookback=lookback - 1))
                self.assertIn("G5.lookback_sufficient", {f.rule for f in short.errors})
                enough = lint_spec(_spec(_win(op, param), lookback=lookback))
                self.assertTrue(enough.passed, f"{op} 应通过: {enough.errors}")

    def test_bad_window_is_rejected(self) -> None:
        for op, param in (("linreg_slope", 0), ("linreg_r2", -3), ("zscore", 2.5)):
            with self.subTest(op=op):
                report = lint_spec(_spec(_win(op, param)))
                self.assertIn("G6.windows_positive_int", {f.rule for f in report.errors})

    def test_bad_alpha_is_rejected(self) -> None:
        for alpha in (0.0, 1.0, 1.5):
            with self.subTest(alpha=alpha):
                report = lint_spec(_spec(_win("llt", alpha)))
                self.assertIn("G6.windows_positive_int", {f.rule for f in report.errors})

    def test_bad_span_is_rejected(self) -> None:
        for span in (0.0, 0.5):
            with self.subTest(span=span):
                report = lint_spec(_spec(_win("ewm_std", span)))
                self.assertIn("G6.windows_positive_int", {f.rule for f in report.errors})


# --------------------------------------------------------------------------- #
# DSL：parse / 拒绝 / 白名单同步
# --------------------------------------------------------------------------- #
class TestDslOperators(unittest.TestCase):
    def test_arithmetic_nodes_parse(self) -> None:
        self.assertEqual(
            parse_dsl_node({"op": "add", "args": [
                {"op": "field", "field": "close"}, {"op": "const", "value": 5.0}]}),
            Expr("add", (_field(), Expr("const", (5.0,)))))
        self.assertEqual(
            parse_dsl_node({"op": "neg", "args": [{"op": "field", "field": "close"}]}),
            Expr("neg", (_field(),)))
        self.assertEqual(
            parse_dsl_node({"op": "log", "args": [{"op": "field", "field": "close"}]}),
            Expr("log", (_field(),)))

    def test_window_nodes_parse_and_auto_shift(self) -> None:
        shifted = Expr("shift", (_field(), 1))
        self.assertEqual(
            parse_dsl_node({"op": "linreg_slope", "field": "close", "window": 20}),
            Expr("linreg_slope", (shifted, 20)))
        self.assertEqual(
            parse_dsl_node({"op": "llt", "field": "close", "alpha": 0.10}),
            Expr("llt", (shifted, 0.10)))
        self.assertEqual(
            parse_dsl_node({"op": "ewm_std", "field": "close", "span": 20}),
            Expr("ewm_std", (shifted, 20.0)))

    def test_bad_arithmetic_arity_is_rejected(self) -> None:
        with self.assertRaises(DslParseError):
            parse_dsl_node({"op": "add", "args": [{"op": "field", "field": "close"}]})

    def test_bad_window_params_are_rejected(self) -> None:
        with self.assertRaises(DslParseError):
            parse_dsl_node({"op": "linreg_slope", "field": "close", "window": 0})
        with self.assertRaises(DslParseError):
            parse_dsl_node({"op": "llt", "field": "close", "alpha": 1.5})
        with self.assertRaises(DslParseError):
            parse_dsl_node({"op": "ewm_std", "field": "close", "span": 0.5})

    def test_rotation_with_zscore_ranking_passes_the_gate(self) -> None:
        dsl = {
            "name": "z-rotation",
            "universe_assets": [],
            "entry": {"op": "const", "value": 1.0},
            "ranking": {"op": "zscore", "field": "close", "window": 20},
            "sizing": {"top_n": 1, "rebalance": "W-MON"},
            "lookback": 20,
            "needs_human_review": False,
        }
        spec = dsl_to_spec(dsl, universe=(1, 2, 3)).spec
        self.assertIsNotNone(spec)
        report = lint_spec(spec)
        self.assertTrue(report.passed, f"DSL 产出的规格未过闸门: {report.errors}")

    def test_whitelist_sync(self) -> None:
        """DSL 白名单必须与 evaluate 支持一一对应（新增算子后同步）。"""
        evaluate_ops = {op for op in _SUPPORTED_OPS.split("/") if op}
        self.assertEqual(DSL_ALL_OPS, evaluate_ops)


if __name__ == "__main__":
    unittest.main(verbosity=2)
