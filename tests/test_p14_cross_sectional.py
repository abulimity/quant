"""P14 横截面算子族（rank / cross_sectional_rank / condition）验收。

LOCAL_DEPLOYMENT_PLAN.md #14：把横截面排名从 `emit_weights` 里**硬编码**的 63 日动量，
升级为一等 `Expr` 算子 + spec 驱动的 `StrategySpec.ranking`。本文件钉住：

    · evaluate 层：rank / cross_sectional_rank / condition 的语义与失败模式
    · spec 层：ranking 的 JSON 往返与结构校验
    · lint 层：G4/G5/G6 扩展到 ranking
    · 发射层：ranking 路径与动量兜底对拍；top-n 选择；未来扰动
    · DSL 层：三个算子的 parse 与白名单同步

全部用**本地合成面板**，零 fixture / 数据层 / 联网。

口径（与计划一致）：
    · `rank` / `cross_sectional_rank` 是同一原语的两个正式名，签名 `(child, ascending=False)`，
      `ascending=False` ⇒ rank 1 = 最大值。
    · `condition(pred, a, b)` = 逐元素 `np.where`。
    · 动量兜底是 close-time **未 shift** 的 `prices/prices.shift(63)-1`；DSL 的 `momentum`
      则自动补 `shift(field,1)`。故对拍用手写未 shift 动量（机制层），e2e 用 shift 语义。

运行：
    $env:PYTHONIOENCODING='utf-8'
    python -m unittest tests.test_p14_cross_sectional -v
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from quantlab.contract.emit import (
    _SUPPORTED_OPS,
    MarketData,
    emit_signals,
    emit_weights,
    evaluate,
    spec2weights,
)
from quantlab.contract.lint import lint_spec
from quantlab.contract.types import (
    F8_SCENARIOS,
    ContractViolation,
    DataRequirement,
    Expr,
    SizingSpec,
    StrategySpec,
    validate_spec,
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
    """4 symbol × 4 日的小面板，含一个 NaN 格（symbol 4 首日），用于手算断言。"""
    idx = pd.bdate_range("2024-01-01", periods=4, name="ts")
    return pd.DataFrame({
        1: [10.0, 20.0, 30.0, 40.0],
        2: [40.0, 30.0, 20.0, 10.0],
        3: [25.0, 25.0, 25.0, 25.0],
        4: [np.nan, 15.0, 35.0, 45.0],
    }, index=idx)


def _cond_panel() -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-01", periods=3, name="ts")
    return pd.DataFrame({
        1: [10.0, 30.0, np.nan],
        2: [40.0, 20.0, 50.0],
    }, index=idx)


def _trending_prices(days: int = 300, symbols=(1, 2, 3, 4, 5), seed: int = 7) -> pd.DataFrame:
    """合成的复权收盘价矩阵（确定性的趋势 + 噪声）。"""
    idx = pd.bdate_range("2020-01-01", periods=days, name="ts")
    rng = np.random.default_rng(seed)
    data = {}
    for offset, symbol in enumerate(symbols):
        steps = 0.0006 * (offset + 1) + 0.012 * rng.standard_normal(days)
        data[symbol] = 100.0 * np.exp(np.cumsum(steps))
    return pd.DataFrame(data, index=idx)


def _rotation_spec(ranking: Expr | None = None, *, lookback: int = 63,
                   universe: tuple[int, ...] = (1, 2, 3, 4, 5)) -> StrategySpec:
    """纯横截面轮动：`entry=const(1.0)`（始终合格），由 `ranking` 决定选谁。"""
    return StrategySpec(
        name="rotation", universe=universe,
        entry=Expr("const", (1.0,)),
        ranking=ranking,
        sizing=SizingSpec(top_n=3, rebalance="W-MON"),
        costs=F8_SCENARIOS[0], lookback=lookback,
        data_requirements=(DataRequirement("bars_daily", ("ts", "close", "available_utc")),),
    )


def _momentum_unshifted(n: int = 63) -> Expr:
    """手写**未 shift** 的动量 —— 与 `emit_weights` 的兜底逐格一致（对拍用）。"""
    return Expr("momentum", (Expr("field", ("close",)), n))


def _momentum_shifted(n: int = 63) -> Expr:
    """`momentum(shift(close,1), n)` —— DSL 窗口算子自动 shift 语义（e2e 用）。"""
    return Expr("momentum", (Expr("shift", (Expr("field", ("close",)), 1)), n))


# --------------------------------------------------------------------------- #
# evaluate：横截面算子的语义
# --------------------------------------------------------------------------- #
class TestEvaluateCrossSectional(unittest.TestCase):
    def test_rank_defaults_to_descending(self) -> None:
        """ascending 缺省 False ⇒ rank 1 = 最大值；NaN 保留。"""
        ranked = evaluate(Expr("rank", (Expr("field", ("close",)),)), _panel())
        self.assertEqual(ranked.iloc[0][1], 3.0)
        self.assertEqual(ranked.iloc[0][2], 1.0)
        self.assertEqual(ranked.iloc[0][3], 2.0)
        self.assertTrue(np.isnan(ranked.iloc[0][4]), "NaN 分数应保留在 rank 外")

    def test_rank_ascending(self) -> None:
        ranked = evaluate(Expr("rank", (Expr("field", ("close",)), True)), _panel())
        self.assertEqual(ranked.iloc[0][1], 1.0)
        self.assertEqual(ranked.iloc[0][2], 3.0)
        self.assertEqual(ranked.iloc[0][3], 2.0)

    def test_cross_sectional_rank_is_synonym_of_rank(self) -> None:
        a = evaluate(Expr("rank", (Expr("field", ("close",)), False)), _panel())
        b = evaluate(Expr("cross_sectional_rank", (Expr("field", ("close",)), False)), _panel())
        pd.testing.assert_frame_equal(a, b)

    def test_non_bool_ascending_is_rejected(self) -> None:
        for bad in ("false", 1, 0.0, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ContractViolation):
                    evaluate(Expr("rank", (Expr("field", ("close",)), bad)), _panel())

    def test_rank_without_child_is_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            evaluate(Expr("rank", ()), _panel())

    def test_condition_is_where(self) -> None:
        """condition(pred, a, b) 逐元素选择；pred 的 NaN 视为 False。"""
        expr = Expr("condition", (
            Expr("gt", (Expr("field", ("close",)), Expr("const", (25.0,)))),
            Expr("const", (100.0,)),
            Expr("const", (0.0,)),
        ))
        got = evaluate(expr, _cond_panel())
        expected = pd.DataFrame({
            1: [0.0, 100.0, 0.0],
            2: [100.0, 0.0, 100.0],
        }, index=_cond_panel().index)
        pd.testing.assert_frame_equal(got, expected)

    def test_condition_branches_may_be_expressions(self) -> None:
        """a/b 也可以是表达式（不是只能 const）—— 走 _as_frame 的 Expr 分支。"""
        expr = Expr("condition", (
            Expr("gt", (Expr("field", ("close",)), Expr("const", (25.0,)))),
            Expr("field", ("close",)),
            Expr("const", (0.0,)),
        ))
        got = evaluate(expr, _cond_panel())
        self.assertEqual(got.iloc[0][1], 0.0)
        self.assertEqual(got.iloc[0][2], 40.0)
        self.assertEqual(got.iloc[1][1], 30.0)

    def test_condition_wrong_arity_is_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            evaluate(Expr("condition", (Expr("field", ("close",)),)), _panel())


# --------------------------------------------------------------------------- #
# spec：ranking 的往返与结构校验
# --------------------------------------------------------------------------- #
class TestSpecRoundtripRanking(unittest.TestCase):
    def test_ranking_defaults_to_none(self) -> None:
        spec = StrategySpec(name="x", universe=(1,), entry=Expr("const", (1.0,)))
        self.assertIsNone(spec.ranking)

    def test_json_roundtrip_preserves_ranking_and_ascending_bool(self) -> None:
        ranking = Expr("cross_sectional_rank", (_momentum_shifted(), False))
        spec = _rotation_spec(ranking=ranking)
        restored = StrategySpec.from_json(spec.to_json())
        self.assertEqual(restored.ranking, ranking)
        # 布尔 ascending 必须原样往返（不是 0/1、不是字符串）
        self.assertIs(restored.ranking.args[1], False)

    def test_validate_spec_accepts_expr_ranking(self) -> None:
        validate_spec(_rotation_spec(ranking=_momentum_shifted()))

    def test_validate_spec_rejects_non_expr_ranking(self) -> None:
        with self.assertRaises(ContractViolation):
            validate_spec(StrategySpec(
                name="bad", universe=(1,), entry=Expr("const", (1.0,)),
                ranking="not-an-expr",  # type: ignore[arg-type]
            ))


# --------------------------------------------------------------------------- #
# lint：G4/G5/G6 扩展到 ranking
# --------------------------------------------------------------------------- #
class TestLintRanking(unittest.TestCase):
    def test_unshifted_ranking_field_is_rejected(self) -> None:
        ranking = Expr("cross_sectional_rank", (Expr("field", ("close",)), False))
        report = lint_spec(_rotation_spec(ranking=ranking))
        self.assertFalse(report.passed)
        rules = {f.rule for f in report.errors}
        self.assertIn("G4.no_lookahead", rules)
        self.assertTrue(any("ranking" in f.location for f in report.errors),
                        "G4 定位应在 ranking 分支")

    def test_shifted_ranking_field_passes(self) -> None:
        ranking = Expr("cross_sectional_rank", (_momentum_shifted(), False))
        report = lint_spec(_rotation_spec(ranking=ranking, lookback=63))
        self.assertTrue(report.passed, f"闸门未通过: {report.errors}")

    def test_condition_with_unshifted_leaf_is_rejected(self) -> None:
        ranking = Expr("condition", (
            Expr("gt", (Expr("field", ("close",)), Expr("const", (25.0,)))),
            Expr("const", (1.0,)),
            Expr("const", (0.0,)),
        ))
        report = lint_spec(_rotation_spec(ranking=ranking))
        self.assertIn("G4.no_lookahead", {f.rule for f in report.errors})

    def test_ranking_window_counts_toward_lookback(self) -> None:
        ranking = Expr("cross_sectional_rank", (_momentum_shifted(), False))
        short = lint_spec(_rotation_spec(ranking=ranking, lookback=60))
        self.assertIn("G5.lookback_sufficient", {f.rule for f in short.errors})
        enough = lint_spec(_rotation_spec(ranking=ranking, lookback=63))
        self.assertTrue(enough.passed)

    def test_ranking_window_must_be_positive_int(self) -> None:
        ranking = Expr("cross_sectional_rank", (
            Expr("momentum", (Expr("shift", (Expr("field", ("close",)), 1)), 0)), False))
        report = lint_spec(_rotation_spec(ranking=ranking))
        self.assertIn("G6.windows_positive_int", {f.rule for f in report.errors})

    def test_ranking_none_passes_gate(self) -> None:
        """向后兼容：无 ranking 的规格照旧通过。"""
        report = lint_spec(_rotation_spec(ranking=None))
        self.assertTrue(report.passed, f"闸门未通过: {report.errors}")


# --------------------------------------------------------------------------- #
# 发射：ranking 路径与动量兜底对拍
# --------------------------------------------------------------------------- #
class TestSpec2WeightsRanking(unittest.TestCase):
    def test_ranking_path_matches_momentum_fallback(self) -> None:
        """parity：手写未 shift 动量 = 兜底动量，权重必须逐格一致。

        ⚠️ 这里**故意未 shift**：`emit_weights` 不跑 G4，动量是 close-time 计算，
        与兜底 `prices/prices.shift(63)-1` 同口径。G4 的 shift 要求另由 TestLintRanking 钉住。
        """
        prices = _trending_prices()
        data = MarketData(prices=prices)
        fallback = emit_weights(_rotation_spec(ranking=None), data, momentum_window=63)
        ranked = emit_weights(_rotation_spec(ranking=_momentum_unshifted()), data)
        pd.testing.assert_frame_equal(fallback, ranked)

    def test_cross_sectional_rank_selects_same_top_n_as_raw_score(self) -> None:
        """rank 单调于分数，故排序（进而 top_n 选择）应与直接用原始分数一致。

        ascending=True：rank 值随动量单调**递增**（越高越优），与 ranking 字段
        「越高越优」的降序排序口径一致；False 则反相（rank 1 = 最大动量）。
        """
        prices = _trending_prices()
        data = MarketData(prices=prices)
        raw = emit_weights(_rotation_spec(ranking=_momentum_unshifted()), data)
        ranked = emit_weights(
            _rotation_spec(ranking=Expr("cross_sectional_rank",
                                        (_momentum_unshifted(), True))), data)
        pd.testing.assert_frame_equal(raw, ranked)

    def test_weights_are_valid_and_at_most_top_n(self) -> None:
        prices = _trending_prices()
        w = emit_weights(_rotation_spec(ranking=_momentum_shifted()),
                         MarketData(prices=prices))
        self.assertTrue((w.sum(axis=1) <= 1.0 + 1e-9).all())
        self.assertTrue(((w > 0).sum(axis=1) <= 3).all())

    def test_perturbing_the_future_does_not_move_past_weights(self) -> None:
        prices = _trending_prices()
        spec = _rotation_spec(ranking=_momentum_shifted())
        base = emit_weights(spec, MarketData(prices=prices))

        cut = len(prices) // 2
        perturbed = prices.copy()
        perturbed.iloc[cut + 1:] *= 3.0
        after = emit_weights(spec, MarketData(prices=perturbed))

        past = base.index[base.index <= prices.index[cut]]
        self.assertTrue(len(past) > 0)
        pd.testing.assert_frame_equal(base.loc[past], after.loc[past], check_dtype=False)


# --------------------------------------------------------------------------- #
# E2E：sample-momentum（63 日动量轮动，买前 3）
# --------------------------------------------------------------------------- #
def sample_momentum_spec() -> StrategySpec:
    return StrategySpec(
        name="sample-momentum", universe=(1, 2, 3, 4, 5),
        entry=Expr("const", (1.0,)),                       # 轮动没有时间性入场条件
        # ranking 是「越高越优」的分数，故用 ascending=True：rank 1 = 最低动量、
        # rank N = 最高动量 → 降序排序恰好选出动量最高的 top-3。
        ranking=Expr("cross_sectional_rank", (_momentum_shifted(), True)),
        sizing=SizingSpec(top_n=3, rebalance="W-MON"),
        costs=F8_SCENARIOS[0], lookback=63,
        data_requirements=(DataRequirement("bars_daily", ("ts", "close", "available_utc")),),
    )


class TestSampleMomentumE2E(unittest.TestCase):
    def test_spec_passes_the_gate(self) -> None:
        report = lint_spec(sample_momentum_spec())
        self.assertTrue(report.passed, f"闸门未通过: {report.errors}")

    def test_entry_const_one_is_always_eligible(self) -> None:
        prices = _trending_prices()
        spec = sample_momentum_spec()
        signals = emit_signals(spec, MarketData(prices=prices))
        self.assertTrue((signals.to_numpy()[spec.lookback:] == 1.0).all(),
                        "预热后 entry=const(1.0) 应恒为 +1")

    def test_weights_select_top3_by_shifted_momentum(self) -> None:
        """独立手算 shift 语义动量 `px.shift(1)/px.shift(64)-1`，验证选出的恰为前三。"""
        prices = _trending_prices()
        spec = sample_momentum_spec()
        w = spec2weights(spec, MarketData(prices=prices))

        shifted_mom = prices.shift(1) / prices.shift(64) - 1.0
        full = [d for d in w.index if (w.loc[d] > 0).sum() == 3]
        self.assertTrue(full, "应存在恰好选满 top-3 的调仓日")
        for date in full:
            top3 = set(shifted_mom.loc[date].sort_values(ascending=False).head(3).index)
            held = set(w.loc[date][w.loc[date] > 0].index)
            self.assertEqual(held, top3, f"{date.date()} 未选 shift 语义动量前三")


# --------------------------------------------------------------------------- #
# DSL：三个算子的 parse 与白名单同步
# --------------------------------------------------------------------------- #
class TestDslCrossSectional(unittest.TestCase):
    def test_rank_node_parses(self) -> None:
        got = parse_dsl_node({"op": "rank", "args": [{"op": "field", "field": "close"}],
                              "ascending": False})
        self.assertEqual(got, Expr("rank", (Expr("field", ("close",)), False)))

    def test_cross_sectional_rank_defaults_ascending_false(self) -> None:
        got = parse_dsl_node({"op": "cross_sectional_rank",
                              "args": [{"op": "field", "field": "close"}]})
        self.assertEqual(got, Expr("cross_sectional_rank", (Expr("field", ("close",)), False)))

    def test_condition_node_parses_three_children(self) -> None:
        got = parse_dsl_node({"op": "condition", "args": [
            {"op": "field", "field": "close"},
            {"op": "const", "value": 1.0},
            {"op": "const", "value": 0.0},
        ]})
        self.assertEqual(got.op, "condition")
        self.assertEqual(len(got.args), 3)

    def test_non_bool_ascending_is_rejected(self) -> None:
        for bad in ("false", 1, None):
            with self.subTest(bad=bad):
                with self.assertRaises(DslParseError):
                    parse_dsl_node({"op": "rank",
                                    "args": [{"op": "field", "field": "close"}],
                                    "ascending": bad})

    def test_cross_sectional_wrong_arity_is_rejected(self) -> None:
        with self.assertRaises(DslParseError):
            parse_dsl_node({"op": "rank", "args": []})

    def test_condition_wrong_arity_is_rejected(self) -> None:
        with self.assertRaises(DslParseError):
            parse_dsl_node({"op": "condition",
                            "args": [{"op": "field", "field": "close"}]})

    def test_dsl_with_ranking_builds_auto_shifted_spec(self) -> None:
        dsl = {
            "name": "sample-momentum",
            "universe_assets": [],
            "entry": {"op": "const", "value": 1.0},
            "ranking": {"op": "cross_sectional_rank",
                        "args": [{"op": "momentum", "field": "close", "window": 63}],
                        "ascending": True},
            "sizing": {"top_n": 3, "rebalance": "W-MON"},
            "lookback": 63,
            "needs_human_review": False,
        }
        spec = dsl_to_spec(dsl, universe=(1, 2, 3, 4, 5)).spec
        self.assertIsNotNone(spec)
        self.assertEqual(spec.ranking, Expr("cross_sectional_rank", (_momentum_shifted(), True)))
        report = lint_spec(spec)
        self.assertTrue(report.passed, f"DSL 产出的规格未过闸门: {report.errors}")

    def test_whitelist_sync(self) -> None:
        """DSL 白名单必须与 evaluate 支持一一对应（新增算子后同步）。"""
        evaluate_ops = {op for op in _SUPPORTED_OPS.split("/") if op}
        self.assertEqual(DSL_ALL_OPS, evaluate_ops)


if __name__ == "__main__":
    unittest.main(verbosity=2)
