"""P3 契约层验收（LOCAL_DEPLOYMENT_PLAN.md §P3）。

覆盖：
    P3.1  V1 `StrategySpec` JSON 往返一致
          V3 `TargetWeights` 校验器拒绝行和 > 1 / 负权重 / 未来日期
          V3 `Signals` 校验器拒绝越界取值
    P3.2  V3 **故意含未来函数的规格必须被拒**（最关键）
          V3 引用未上市标的被拒
          V3 合法规格通过并生成可追溯报告
          V3 x2strategy 来源在 x2 规则未接通时**默认阻断**（fail-closed）
          V3 手写规格带 origin=handwritten 可通过，且标记可在 run 元数据查到
    P3.3  V3 夹具上的 SMA 交叉 `emit_signals` 与手算一致
          V3 **未来扰动测试**：改信号时刻之后的价格，此前信号与权重完全不变
          V3 `emit_weights` 行和约束与调仓日对齐正确

运行：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -m unittest discover -t . -s tests
"""

from __future__ import annotations

import json
import unittest
from datetime import date

import numpy as np
import pandas as pd

from quantlab.contract.emit import MarketData, emit_signals, emit_weights, evaluate
from quantlab.contract.lint import lint_spec
from quantlab.contract.types import (
    F8_SCENARIOS,
    ORIGIN_HANDWRITTEN,
    ORIGIN_X2STRATEGY,
    ContractViolation,
    CostModel,
    DataRequirement,
    Expr,
    SizingSpec,
    StrategySpec,
    validate_signals,
    validate_spec,
    validate_target_weights,
)


# --------------------------------------------------------------------------- #
# 夹具：一个合法的 SMA 交叉规格（手写）
# --------------------------------------------------------------------------- #
def sma(field: str, n: int, shift: int = 1) -> Expr:
    """`sma(shift(field, shift), n)` —— 已解除未来函数。"""
    return Expr("sma", (Expr("shift", (Expr("field", (field,)), shift)), n))


def cross_spec(**overrides) -> StrategySpec:
    base = dict(
        name="sma-cross",
        universe=(1, 2, 3),
        entry=Expr("cross_above", (sma("close", 20), sma("close", 60))),
        exit=Expr("cross_below", (sma("close", 20), sma("close", 60))),
        sizing=SizingSpec(top_n=3, rebalance="W-MON"),
        costs=F8_SCENARIOS[10],
        lookback=60,
        origin=ORIGIN_HANDWRITTEN,
        data_requirements=(DataRequirement("bars_daily", ("ts", "close", "available_utc")),),
    )
    base.update(overrides)
    return StrategySpec(**base)


def trending_prices(days: int = 400, symbols=(1, 2, 3), seed: int = 7) -> pd.DataFrame:
    """合成的复权收盘价矩阵（含确定性的趋势与噪声）。"""
    index = pd.bdate_range("2020-01-01", periods=days, name="ts")
    rng = np.random.default_rng(seed)
    data = {}
    for offset, symbol in enumerate(symbols):
        steps = 0.0006 * (offset + 1) + 0.012 * rng.standard_normal(days)
        data[symbol] = 100.0 * np.exp(np.cumsum(steps))
    return pd.DataFrame(data, index=index)


# --------------------------------------------------------------------------- #
# P3.1 契约类型
# --------------------------------------------------------------------------- #
class TestStrategySpecRoundtrip(unittest.TestCase):
    """V1：JSON 往返一致。"""

    def test_json_roundtrip_is_identical(self) -> None:
        original = cross_spec()
        restored = StrategySpec.from_json(original.to_json())
        self.assertEqual(restored.to_dict(), original.to_dict())
        self.assertEqual(restored, original)

    def test_roundtrip_preserves_nested_expr_structure(self) -> None:
        original = cross_spec()
        restored = StrategySpec.from_json(original.to_json())
        self.assertEqual(restored.entry, original.entry)
        ops = [node.op for node in restored.entry.walk()]
        self.assertIn("cross_above", ops)
        self.assertIn("sma", ops)

    def test_roundtrip_preserves_cost_and_sizing(self) -> None:
        original = cross_spec(costs=CostModel.scenario(30, fx_cost_bps=5),
                              sizing=SizingSpec(top_n=2, cash_floor=0.1))
        restored = StrategySpec.from_json(original.to_json())
        self.assertEqual(restored.costs, original.costs)
        self.assertEqual(restored.sizing, original.sizing)

    def test_json_is_stable_across_dumps(self) -> None:
        spec = cross_spec()
        self.assertEqual(spec.to_json(), spec.to_json())

    def test_spec_is_immutable(self) -> None:
        spec = cross_spec()
        with self.assertRaises(Exception):
            spec.name = "changed"          # frozen dataclass

    def test_with_params_does_not_mutate_original(self) -> None:
        spec = cross_spec()
        derived = spec.with_params(fast=10)
        self.assertEqual(spec.params, {})
        self.assertEqual(derived.params, {"fast": 10})


class TestSignalsValidator(unittest.TestCase):
    """V3：拒绝越界取值。"""

    @staticmethod
    def _frame(values) -> pd.DataFrame:
        index = pd.DatetimeIndex(pd.bdate_range("2024-01-01", periods=len(values)), name="ts")
        return pd.DataFrame({"1": values}, index=index)

    def test_legal_values_pass(self) -> None:
        validate_signals(self._frame([1.0, 0.0, -1.0, np.nan]))

    def test_out_of_range_value_is_rejected(self) -> None:
        for bad in (0.5, 2.0, -1.5, 3.0):
            with self.subTest(value=bad):
                with self.assertRaises(ContractViolation) as ctx:
                    validate_signals(self._frame([1.0, bad]))
                self.assertIn("非法取值", str(ctx.exception))

    def test_non_numeric_column_is_rejected(self) -> None:
        frame = pd.DataFrame({"1": ["buy"]}, index=pd.DatetimeIndex(["2024-01-02"]))
        with self.assertRaises(ContractViolation):
            validate_signals(frame)

    def test_non_datetime_index_is_rejected(self) -> None:
        with self.assertRaises(ContractViolation) as ctx:
            validate_signals(pd.DataFrame({"1": [1.0]}, index=[0]))
        self.assertIn("DatetimeIndex", str(ctx.exception))

    def test_duplicate_dates_are_rejected(self) -> None:
        index = pd.DatetimeIndex(["2024-01-02", "2024-01-02"])
        with self.assertRaises(ContractViolation):
            validate_signals(pd.DataFrame({"1": [1.0, 0.0]}, index=index))

    def test_non_monotonic_index_is_rejected(self) -> None:
        index = pd.DatetimeIndex(["2024-01-03", "2024-01-02"])
        with self.assertRaises(ContractViolation):
            validate_signals(pd.DataFrame({"1": [1.0, 0.0]}, index=index))


class TestTargetWeightsValidator(unittest.TestCase):
    """V3：拒绝行和 > 1、负权重、未来日期。"""

    @staticmethod
    def _frame(rows, dates=None) -> pd.DataFrame:
        dates = dates if dates is not None else pd.bdate_range("2024-01-01", periods=len(rows))
        return pd.DataFrame(rows, index=pd.DatetimeIndex(dates, name="ts"), columns=[1, 2, 3])

    def test_legal_weights_pass(self) -> None:
        validate_target_weights(self._frame([[0.5, 0.5, 0.0], [0.34, 0.33, 0.33]]))

    def test_all_cash_row_is_legal(self) -> None:
        """无合格标的时持有现金 —— 全 0 行必须合法（F.8）。"""
        validate_target_weights(self._frame([[0.0, 0.0, 0.0]]))

    def test_row_sum_above_one_is_rejected(self) -> None:
        with self.assertRaises(ContractViolation) as ctx:
            validate_target_weights(self._frame([[0.7, 0.7, 0.0]]))
        self.assertIn("行和超过", str(ctx.exception))
        self.assertIn("杠杆", str(ctx.exception))

    def test_row_sum_exactly_one_passes(self) -> None:
        validate_target_weights(self._frame([[0.5, 0.3, 0.2]]))

    def test_negative_weight_is_rejected(self) -> None:
        with self.assertRaises(ContractViolation) as ctx:
            validate_target_weights(self._frame([[0.5, -0.2, 0.0]]))
        self.assertIn("负权重", str(ctx.exception))

    def test_short_is_allowed_only_when_explicitly_enabled(self) -> None:
        validate_target_weights(self._frame([[0.5, -0.2, 0.0]]), allow_short=True)

    def test_gross_exposure_is_capped_even_when_shorting_is_allowed(self) -> None:
        """允许做空时约束的是**总敞口**而非净敞口。

        `+0.8 / -0.8` 的净和是 0，但占用 1.6 倍资金 —— 若用 nansum 会被当成
        「没用杠杆」而放过，那正是典型的静默错误。
        """
        with self.assertRaises(ContractViolation) as ctx:
            validate_target_weights(self._frame([[0.8, -0.8, 0.0]]), allow_short=True)
        self.assertIn("总敞口", str(ctx.exception))

    def test_future_date_is_rejected(self) -> None:
        dates = pd.bdate_range("2024-01-01", periods=1)
        with self.assertRaises(ContractViolation) as ctx:
            validate_target_weights(self._frame([[0.5, 0.0, 0.0]], dates=dates),
                                    as_of=date(2023, 12, 31))
        self.assertIn("未来日期", str(ctx.exception))

    def test_date_equal_to_as_of_is_allowed(self) -> None:
        dates = pd.bdate_range("2024-01-01", periods=1)
        validate_target_weights(self._frame([[0.5, 0.0, 0.0]], dates=dates),
                                as_of=dates[0].date())

    def test_nan_treated_as_no_holding(self) -> None:
        validate_target_weights(self._frame([[np.nan, 0.9, np.nan]]))


class TestSpecStructuralValidation(unittest.TestCase):
    """规格自身的结构校验。"""

    def test_valid_spec_passes(self) -> None:
        validate_spec(cross_spec())

    def test_empty_name_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            validate_spec(cross_spec(name=""))

    def test_unknown_origin_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            validate_spec(cross_spec(origin="made-up"))

    def test_non_daily_timeframe_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            validate_spec(cross_spec(timeframe="1m"))

    def test_duplicate_universe_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            validate_spec(cross_spec(universe=(1, 1, 2)))

    def test_negative_cost_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            validate_spec(cross_spec(costs=CostModel(commission_bps=-1.0)))

    def test_bad_sizing_rejected(self) -> None:
        for sizing in (SizingSpec(top_n=0), SizingSpec(cash_floor=1.0),
                       SizingSpec(method="magic")):
            with self.subTest(sizing=sizing):
                with self.assertRaises(ContractViolation):
                    validate_spec(cross_spec(sizing=sizing))

    def test_f8_scenarios_are_the_documented_ones(self) -> None:
        self.assertEqual(sorted(F8_SCENARIOS), [0, 10, 30])
        self.assertEqual(F8_SCENARIOS[10].one_way_bps, 10)


# --------------------------------------------------------------------------- #
# P3.2 闸门
# --------------------------------------------------------------------------- #
class TestLintGate(unittest.TestCase):
    """§P3.2：fail-closed，且能捕获未来函数。"""

    def test_legal_handwritten_spec_passes(self) -> None:
        report = lint_spec(cross_spec())
        self.assertTrue(report.passed, "\n".join(str(f) for f in report.errors))

    def test_lookahead_spec_is_rejected(self) -> None:
        """**最关键**：故意含未来函数（用当日 close，未 shift）必须被拒。"""
        spec = cross_spec(
            name="lookahead",
            entry=Expr("cross_above", (
                Expr("sma", (Expr("field", ("close",)), 20)),      # 未 shift
                Expr("sma", (Expr("field", ("close",)), 60)),
            )),
        )
        report = lint_spec(spec)
        self.assertFalse(report.passed, "含未来函数的规格竟然通过了")
        self.assertIn("G4.no_lookahead", {f.rule for f in report.errors})
        self.assertIn("未来函数", str(report.errors[0]))

    def test_lookahead_in_exit_is_also_caught(self) -> None:
        spec = cross_spec(exit=Expr("cross_below", (
            Expr("sma", (Expr("field", ("close",)), 20)),
            Expr("sma", (Expr("field", ("close",)), 60)))))
        self.assertIn("G4.no_lookahead", {f.rule for f in lint_spec(spec).errors})

    def test_shift_of_zero_does_not_clear_the_future_function(self) -> None:
        """shift(0) 等于没 shift —— 不得被当成「已处理」。"""
        spec = cross_spec(entry=Expr("cross_above", (
            sma("close", 20, shift=0), sma("close", 60, shift=0))))
        self.assertFalse(lint_spec(spec).passed,
                         "shift(0) 被错误地当成了解除未来函数")

    def test_shift_deep_inside_nested_expr_still_clears_it(self) -> None:
        """shift 在**更深**的嵌套里也应生效（遍历必须递归）。"""
        spec = cross_spec(entry=Expr("gt", (
            Expr("sma", (Expr("shift", (Expr("field", ("close",)), 1)), 20)),
            Expr("const", (0.0,)))))
        self.assertTrue(lint_spec(spec).passed)

    def test_unlisted_symbol_is_rejected(self) -> None:
        """引用未上市标的必须被拒（F.7：上市前没有行情与收益）。"""
        spec = cross_spec(universe=(5,))
        report = lint_spec(spec, listing_dates={5: date(2022, 1, 10)},
                           as_of=date(2019, 1, 1))
        self.assertFalse(report.passed)
        self.assertIn("G8.symbols_listed", {f.rule for f in report.errors})

    def test_listed_symbol_passes(self) -> None:
        report = lint_spec(cross_spec(universe=(1,)),
                           listing_dates={1: date(2010, 1, 4)}, as_of=date(2019, 1, 1))
        self.assertTrue(report.passed)

    def test_unknown_dataset_is_rejected(self) -> None:
        spec = cross_spec(data_requirements=(DataRequirement("nope", ("ts",)),))
        self.assertIn("G1.dataset_known", {f.rule for f in lint_spec(spec).errors})

    def test_unknown_field_is_rejected(self) -> None:
        spec = cross_spec(data_requirements=(
            DataRequirement("bars_daily", ("ts", "close", "available_utc", "magic")),))
        self.assertIn("G2.fields_exist", {f.rule for f in lint_spec(spec).errors})

    def test_missing_available_utc_is_rejected(self) -> None:
        """读行情却不声明可用时间 → 无从判断「信号产生时数据是否可得」。"""
        spec = cross_spec(data_requirements=(DataRequirement("bars_daily", ("ts", "close")),))
        self.assertIn("G3.availability_declared", {f.rule for f in lint_spec(spec).errors})

    def test_insufficient_lookback_is_rejected(self) -> None:
        self.assertIn("G5.lookback_sufficient",
                      {f.rule for f in lint_spec(cross_spec(lookback=5)).errors})

    def test_non_positive_window_is_rejected(self) -> None:
        spec = cross_spec(entry=Expr("gt", (
            Expr("sma", (Expr("shift", (Expr("field", ("close",)), 1)), 0)),
            Expr("const", (0.0,)))))
        self.assertIn("G6.windows_positive_int", {f.rule for f in lint_spec(spec).errors})

    def test_undeclared_cost_model_is_rejected(self) -> None:
        """裸默认成本 = 全 0 → 回测系统性偏乐观，必须显式选定。"""
        self.assertIn("G7.cost_model_declared",
                      {f.rule for f in lint_spec(cross_spec(costs=CostModel())).errors})

    def test_explicitly_zero_cost_is_accepted(self) -> None:
        """但**显式**选 0 bps 是合法的 —— 区别在于「想过」还是「忘了」。"""
        self.assertTrue(lint_spec(cross_spec(costs=F8_SCENARIOS[0])).passed)

    def test_raise_if_failed_raises_with_actionable_message(self) -> None:
        report = lint_spec(cross_spec(lookback=1))
        with self.assertRaises(ContractViolation) as ctx:
            report.raise_if_failed()
        self.assertIn("不得", str(ctx.exception))


class TestLintGateOriginPolicy(unittest.TestCase):
    """§P3.2 闸门口径：x2 来源 fail-closed；手写规格不阻塞 P3/P4。"""

    def test_x2_origin_is_blocked_while_x2_rules_unavailable(self) -> None:
        """**fail-closed**：x2 规则未接通时，x2 来源规格不得入库（而非默认放行）。

        用**显式覆盖** `x2_rules_available=False`，而不是依赖模块常量的当前值 ——
        否则 P5.2 接通 x2 规则后这条会「自动失效」，看起来像测试坏了，
        实际是它本来就没锁定住要测的东西。
        """
        report = lint_spec(cross_spec(origin=ORIGIN_X2STRATEGY), x2_rules_available=False)
        self.assertFalse(report.passed)
        self.assertIn("X2.rules_unavailable", {f.rule for f in report.errors})
        self.assertIn("x2strategy", report.rules_run)

    def test_x2_origin_passes_once_rules_are_available(self) -> None:
        """（模拟 P5 接通后）同一份规格应当通过 —— 证明阻断的原因确实是「规则没接」。"""
        report = lint_spec(cross_spec(origin=ORIGIN_X2STRATEGY), x2_rules_available=True)
        self.assertTrue(report.passed)

    def test_x2_rules_are_actually_wired_on(self) -> None:
        """P5.2 后 x2 规则应**已接通**：不再因「规则没接」阻断 x2 来源规格。"""
        from quantlab.contract.lint import X2_RULES_AVAILABLE, X2_RULE_SEVERITY

        self.assertTrue(X2_RULES_AVAILABLE, "x2 规则仍未接通")
        report = lint_spec(cross_spec(origin=ORIGIN_X2STRATEGY))
        self.assertTrue(report.passed)
        self.assertNotIn("X2.rules_unavailable", {f.rule for f in report.errors})
        self.assertIn(X2_RULE_SEVERITY, ("note", "warning"),
                      "算子提示应为咨询性，不应阻断入库（经人工确认的取舍）")

    def test_operator_notes_are_consultative_not_blocking(self) -> None:
        """算子提示命中时**不得**阻断 —— 它讲的是数值陷阱，与未来函数无关。"""
        report = lint_spec(cross_spec(origin=ORIGIN_X2STRATEGY),
                           operator_notes=["second_moment (score=0.83): 注意数值稳定性"])
        self.assertTrue(report.passed, "咨询性算子提示竟然阻断了入库")
        notes = [f for f in report.findings if f.rule.startswith("X2.operator_notes")]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].severity, "note")

    def test_handwritten_origin_is_not_blocked(self) -> None:
        report = lint_spec(cross_spec(origin=ORIGIN_HANDWRITTEN))
        self.assertTrue(report.passed)
        self.assertNotIn("x2strategy", report.rules_run)

    def test_origin_is_recorded_in_run_metadata(self) -> None:
        """`origin` 必须可在 run 元数据中查到（§P3.2 可审计要求）。"""
        metadata = lint_spec(cross_spec(origin=ORIGIN_HANDWRITTEN)).run_metadata()
        self.assertEqual(metadata["origin"], "handwritten")
        self.assertTrue(metadata["passed"])
        self.assertIn("generic", metadata["rules_run"])
        json.dumps(metadata)                 # 必须可 JSON 序列化（要写进 run 清单）

    def test_report_is_traceable(self) -> None:
        report = lint_spec(cross_spec())
        self.assertEqual(report.spec_name, "sma-cross")
        self.assertIn("generic", report.rules_run)
        self.assertTrue(any(f.rule == "G7.cost_model_declared" for f in report.findings))


# --------------------------------------------------------------------------- #
# P3.3 发射器
# --------------------------------------------------------------------------- #
class TestEmitSignals(unittest.TestCase):
    """V3：与手算一致；回看期不足处为 NaN。"""

    def setUp(self) -> None:
        self.prices = trending_prices()
        self.data = MarketData(prices=self.prices)
        self.spec = cross_spec(universe=tuple(self.prices.columns))

    def test_signals_match_hand_calculation(self) -> None:
        """独立手算 SMA 交叉（同样 shift(1)），逐格比对。"""
        signals = emit_signals(self.spec, self.data)

        fast = self.prices.shift(1).rolling(20, min_periods=20).mean()
        slow = self.prices.shift(1).rolling(60, min_periods=60).mean()
        above = fast > slow
        prev_above = fast.shift(1) > slow.shift(1)
        # 注意：**不能**用 `~above.shift(1).fillna(False)` —— 那会把「前一日状态未知」
        # 当成「前一日在上方之下」，于是第一个可评估日会凭空产生一次穿越（幻影信号）。
        # 正确口径是「前一日明确在下方」，即 `prev_fast <= prev_slow`（NaN 参与比较恒为 False）。
        expected_entry = above & (fast.shift(1) <= slow.shift(1))
        expected_exit = (~above) & prev_above

        warm = np.arange(len(signals)) >= self.spec.lookback
        for symbol in self.prices.columns:
            got = signals[symbol]
            self.assertTrue(((got[warm] == 1.0) == expected_entry[symbol][warm]).all(),
                            f"symbol {symbol} 入场信号与手算不一致")
            self.assertTrue(((got[warm] == -1.0) == expected_exit[symbol][warm]).all(),
                            f"symbol {symbol} 出场信号与手算不一致")
            self.assertTrue(got[~warm].isna().all(), "回看期不足处应为 NaN（预热期）")

    def test_prewarm_rows_are_nan_not_zero(self) -> None:
        """NaN（还看不出）与 0（看过，没信号）语义不同，不可互换。"""
        self.assertTrue(np.isnan(emit_signals(self.spec, self.data).iloc[0]).all())

    def test_signals_are_contract_valid(self) -> None:
        validate_signals(emit_signals(self.spec, self.data))

    def test_untradable_rows_get_no_signal(self) -> None:
        """停牌/缺失处不得出现信号。"""
        traded = pd.DataFrame(True, index=self.prices.index, columns=self.prices.columns)
        traded.iloc[200:210, 0] = False
        signals = emit_signals(self.spec, MarketData(prices=self.prices, traded=traded))
        self.assertTrue(signals.iloc[200:210, 0].isna().all())

    def test_unknown_operator_is_rejected(self) -> None:
        with self.assertRaises(ContractViolation) as ctx:
            evaluate(Expr("magic", ()), self.prices)
        self.assertIn("未知算子", str(ctx.exception))

    def test_shift_zero_is_rejected_at_evaluation(self) -> None:
        """与 lint.G4 呼应：求值层也拒绝 shift(0)，不只依赖闸门。"""
        with self.assertRaises(ContractViolation):
            evaluate(Expr("shift", (Expr("field", ("close",)), 0)), self.prices)


class TestFuturePerturbation(unittest.TestCase):
    """V3（**强验收**，附录 F.9 核心项）：

        「修改信号时刻**之后**的价格，此前已产生的信号与权重**完全不变**」
    """

    def setUp(self) -> None:
        self.prices = trending_prices()
        self.spec = cross_spec(universe=tuple(self.prices.columns))
        self.cut = 250                      # 扰动分界：只改这个位置之后的价格

    def _perturbed(self, factor: float = 3.0) -> MarketData:
        prices = self.prices.copy()
        prices.iloc[self.cut + 1:] = prices.iloc[self.cut + 1:] * factor
        return MarketData(prices=prices)

    def test_future_change_does_not_alter_past_signals(self) -> None:
        base = emit_signals(self.spec, MarketData(prices=self.prices))
        perturbed = emit_signals(self.spec, self._perturbed())
        pd.testing.assert_frame_equal(base.iloc[: self.cut + 1],
                                      perturbed.iloc[: self.cut + 1], check_dtype=False)
        np.testing.assert_array_equal(
            np.isnan(base.iloc[: self.cut + 1].to_numpy()),
            np.isnan(perturbed.iloc[: self.cut + 1].to_numpy()))

    def test_future_change_does_not_alter_past_weights(self) -> None:
        base = emit_weights(self.spec, MarketData(prices=self.prices))
        perturbed = emit_weights(self.spec, self._perturbed())
        past = base.index[base.index <= self.prices.index[self.cut]]
        self.assertTrue(len(past) > 0)
        pd.testing.assert_frame_equal(base.loc[past], perturbed.loc[past], check_dtype=False)

    def test_perturbation_is_actually_material(self) -> None:
        """**反证**：扰动必须真的改变了未来 —— 否则上一条测试是空的。"""
        base = emit_signals(self.spec, MarketData(prices=self.prices))
        perturbed = emit_signals(self.spec, self._perturbed())
        future = slice(self.cut + 1, None)
        changed = not np.array_equal(np.nan_to_num(base.iloc[future].to_numpy()),
                                     np.nan_to_num(perturbed.iloc[future].to_numpy()))
        self.assertTrue(changed, "扰动没有产生任何差异 —— 该测试无法证明因果性")

    def test_other_symbol_future_does_not_affect_this_symbol_past(self) -> None:
        """改**另一个**标的的未来，不得影响本标的过去的信号。"""
        prices = self.prices.copy()
        prices.iloc[self.cut + 1:, 0] = prices.iloc[self.cut + 1:, 0] * 5.0
        base = emit_signals(self.spec, MarketData(prices=self.prices))
        perturbed = emit_signals(self.spec, MarketData(prices=prices))
        pd.testing.assert_frame_equal(base.iloc[: self.cut + 1, 1:],
                                      perturbed.iloc[: self.cut + 1, 1:], check_dtype=False)


class TestEmitWeights(unittest.TestCase):
    """V3：行和约束、调仓日对齐正确。"""

    def setUp(self) -> None:
        self.prices = trending_prices()
        self.spec = cross_spec(universe=tuple(self.prices.columns))
        self.weights = emit_weights(self.spec, MarketData(prices=self.prices))

    def test_weights_are_contract_valid(self) -> None:
        validate_target_weights(self.weights)

    def test_row_sums_never_exceed_one(self) -> None:
        self.assertTrue((self.weights.sum(axis=1) <= 1.0 + 1e-9).all())

    def test_rebalance_dates_are_real_trading_days_and_weekly(self) -> None:
        dates = self.weights.index
        self.assertTrue(dates.isin(self.prices.index).all(), "调仓日必须是真实交易日")
        # 「每周一次」不等于「间隔恰好 7 天」：若周一休市，决策顺延到周二
        # （F.4.5），于是相邻两次可以只隔 6 天。正确的判据是**每个自然周至多一次**。
        weeks = [(d.isocalendar().year, d.isocalendar().week) for d in dates]
        self.assertEqual(len(weeks), len(set(weeks)), "出现了同一周内多次调仓")
        gaps = pd.Series(dates).diff().dropna().dt.days
        self.assertTrue((gaps <= 8).all(), f"调仓间隔应 ≤ 8 天，实际最大 {gaps.max()}")

    def test_at_most_top_n_holdings_per_row(self) -> None:
        self.assertTrue(((self.weights > 0).sum(axis=1) <= self.spec.sizing.top_n).all())

    def test_non_zero_rows_are_equally_weighted(self) -> None:
        for date, row in self.weights.iterrows():
            positive = row[row > 0]
            if len(positive) > 1:
                self.assertTrue(np.allclose(positive.to_numpy(), positive.iloc[0]),
                                f"{date.date()} 非等权")

    def test_no_eligible_symbols_means_all_cash(self) -> None:
        """无合格标的 → 持有现金（F.8）：该行必须全 0。"""
        traded = pd.DataFrame(False, index=self.prices.index, columns=self.prices.columns)
        weights = emit_weights(self.spec, MarketData(prices=self.prices, traded=traded))
        self.assertTrue((weights.to_numpy() == 0.0).all())

    def test_weights_only_use_past_data_on_rebalance_dates(self) -> None:
        """调仓日 t 的权重，在只保留 ≤ t 的数据时应完全相同（因果性）。"""
        truncated = MarketData(prices=self.prices.iloc[:300])
        weights_truncated = emit_weights(self.spec, truncated)
        pd.testing.assert_frame_equal(self.weights.loc[weights_truncated.index],
                                      weights_truncated, check_dtype=False)

    def test_midweek_entry_signal_is_carried_into_the_next_rebalance(self) -> None:
        """**信号是事件、持仓是状态**：周中金叉必须在下一个调仓日生效。

        若 `emit_weights` 直接要求「调仓日当天恰好 +1」，周中出现的金叉会被整条丢掉，
        对任何事件型策略结果都是**永远空仓** —— 回测看起来正常、收益恒为 0、不报错。
        这是本阶段最危险的一类静默失败，故单列一条用例钉住。
        """
        days = 300
        shape = np.concatenate([np.linspace(100, 70, 100), np.linspace(70, 130, days - 100)])
        prices = pd.DataFrame({1: shape},
                              index=pd.bdate_range("2020-01-01", periods=days, name="ts"))
        spec = cross_spec(universe=(1,), lookback=60, costs=F8_SCENARIOS[0])
        signals = emit_signals(spec, MarketData(prices=prices))

        entries = signals.index[signals[1] == 1.0]
        self.assertTrue(len(entries) > 0, "该形态应产生至少一次金叉")
        self.assertNotIn(entries[0].weekday(), (0,), "本例特意让金叉发生在周中")

        weights = emit_weights(spec, MarketData(prices=prices), signals=signals)
        held = weights.index[(weights[1] > 0)]
        self.assertTrue(len(held) > 0, "周中金叉未被带入调仓日 —— 策略会静默空仓")
        self.assertGreater(held[0], entries[0], "建仓应发生在信号**之后**的调仓日")

    def test_position_state_reduction(self) -> None:
        """直接钉住归约语义：+1 建仓、−1 平仓、0/NaN 维持前值。"""
        from quantlab.contract.emit import position_state

        index = pd.DatetimeIndex(pd.bdate_range("2024-01-01", periods=6), name="ts")
        signals = pd.DataFrame({1: [np.nan, 0.0, 1.0, 0.0, 0.0, -1.0]}, index=index)
        state = position_state(signals)
        self.assertEqual(state[1].tolist(), [0.0, 0.0, 1.0, 1.0, 1.0, 0.0])

    def test_tie_break_is_by_symbol_id(self) -> None:
        """分数相同时按**内部 ID 稳定排序**（F.8 明文要求）。

        构造：三个标的共享**同一条** V 形价格。该形态**恰好**产生一次 SMA20 上穿
        SMA60 —— 若用单调上升的直线，均线永不交叉，压根不会有信号，测试就是空的。
        决策日的 63 日动量完全相同（各标的都在第 100 根见底后同幅回升），
        故排序只能由 symbol_id 决定。
        """
        days = 300
        shape = np.concatenate([np.linspace(100, 70, 100), np.linspace(70, 130, days - 100)])
        prices = pd.DataFrame({5: shape, 2: shape, 9: shape},
                              index=pd.bdate_range("2020-01-01", periods=days, name="ts"))
        spec = cross_spec(universe=(2, 5, 9),
                          sizing=SizingSpec(top_n=1, rebalance="W-MON"),
                          lookback=60, costs=F8_SCENARIOS[0])
        weights = emit_weights(spec, MarketData(prices=prices))

        self.assertTrue(len(weights) > 0, "未产生任何调仓日")
        held = weights.columns[(weights > 0).any(axis=0)].tolist()
        self.assertEqual(held, [2], f"动量完全相同时应选 symbol_id 最小者，实际 {held}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
