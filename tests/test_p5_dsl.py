r"""P5.6a「受控 DSL + fail-closed parser」验收 —— E2E-B 的**确定性**部分。

LOCAL_DEPLOYMENT_PLAN.md §P5.6a：prompt 只负责「逼出结构化」，parser 只负责
「吃掉结构化」。本文件钉住 parser 的行为，**零 LLM、零联网**：

    V3  parser 接受合法 DSL → 合规 `StrategySpec`；非法 DSL → 明确拒绝（不静默兜底）
    V3  `sample-ma-cross.md` 的 golden DSL → parser → 闸门 → `spec2weights`，
        权重与 E2E-A 手写规格**逐格一致**（对拍）
    V3  语义偏离可见：LLM 把 `cross_above` 写成 `gt`（结构合法、语义错）→
        parser 收下（不猜语义），但**权重与参考显著不同**（偏离可观测），
        且 `needs_human_review` 标记机制生效

参考规格 `_reference_spec()` 与 `tests/test_p5_ma_cross_e2e.py::ma_cross_spec`
**逐字段一致**（同一份 20/60 均线金叉/死叉）。

运行：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -m unittest tests.test_p5_dsl -v
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from quantlab.contract.emit import _SUPPORTED_OPS, MarketData, spec2weights
from quantlab.contract.lint import lint_spec
from quantlab.contract.types import Expr, F8_SCENARIOS, SizingSpec, StrategySpec
from quantlab.engines.base import DataBundle, load_bundle_from_fixture, register_builtin
from quantlab.x2.dsl import (
    DSL_ALL_OPS,
    DslParseError,
    dsl_to_spec,
    parse_dsl_node,
)

SYMBOL = 1


# --------------------------------------------------------------------------- #
# 参考规格（与 E2E-A 手写规格一致）
# --------------------------------------------------------------------------- #
def _sma(field: str, n: int) -> Expr:
    return Expr("sma", (Expr("shift", (Expr("field", (field,)), 1)), n))


def _reference_spec(symbol: int = SYMBOL) -> StrategySpec:
    return StrategySpec(
        name="sample-ma-cross", universe=(symbol,),
        entry=Expr("cross_above", (_sma("close", 20), _sma("close", 60))),
        exit=Expr("cross_below", (_sma("close", 20), _sma("close", 60))),
        sizing=SizingSpec(top_n=1, rebalance="W-MON"),
        costs=F8_SCENARIOS[0], lookback=60,
    )


def golden_dsl() -> dict:
    """sample-ma-cross.md 的忠实 DSL（LLM「应当」产出的结构）。"""
    sma20 = {"op": "sma", "field": "close", "window": 20}
    sma60 = {"op": "sma", "field": "close", "window": 60}
    return {
        "name": "sample-ma-cross",
        "universe_assets": ["SYN-CN-A"],
        "entry": {"op": "cross_above", "args": [sma20, sma60]},
        "exit": {"op": "cross_below", "args": [sma20, sma60]},
        "sizing": {"top_n": 1, "rebalance": "W-MON"},
        "lookback": 60,
        "needs_human_review": False,
    }


def deviated_dsl() -> dict:
    """「语义偏离」样例：把穿越写成连续比较（结构合法、语义错）。"""
    sma20 = {"op": "sma", "field": "close", "window": 20}
    sma60 = {"op": "sma", "field": "close", "window": 60}
    return {
        "name": "sample-ma-cross-deviated",
        "universe_assets": [],
        "entry": {"op": "gt", "args": [sma20, sma60]},
        "exit": {"op": "lt", "args": [sma20, sma60]},
        "sizing": {"top_n": 1, "rebalance": "W-MON"},
        "lookback": 60,
        "needs_human_review": False,
    }


# --------------------------------------------------------------------------- #
# V3 #1a：parser 接受合法 DSL → 正确 Expr 树
# --------------------------------------------------------------------------- #
class TestParserAccepts(unittest.TestCase):
    def test_window_node_auto_shifts(self) -> None:
        got = parse_dsl_node({"op": "sma", "field": "close", "window": 20})
        self.assertEqual(got, _sma("close", 20))

    def test_cross_above_parses_two_children(self) -> None:
        got = parse_dsl_node({
            "op": "cross_above",
            "args": [{"op": "sma", "field": "close", "window": 20},
                     {"op": "sma", "field": "close", "window": 60}],
        })
        self.assertEqual(got, _reference_spec().entry)

    def test_golden_dsl_builds_reference_tree(self) -> None:
        spec = dsl_to_spec(golden_dsl(), universe=(SYMBOL,),
                           source_paper="papers/sample-ma-cross.md").spec
        self.assertIsNotNone(spec)
        self.assertEqual(spec.entry, _reference_spec().entry)
        self.assertEqual(spec.exit, _reference_spec().exit)
        self.assertEqual(spec.sizing, _reference_spec().sizing)
        self.assertEqual(spec.lookback, 60)

    def test_golden_dsl_passes_the_gate(self) -> None:
        spec = dsl_to_spec(golden_dsl(), universe=(SYMBOL,)).spec
        report = lint_spec(spec)
        self.assertTrue(report.passed, f"闸门未通过: {report.errors}")

    def test_exit_null_is_allowed(self) -> None:
        dsl = golden_dsl()
        dsl["exit"] = None
        spec = dsl_to_spec(dsl, universe=(SYMBOL,)).spec
        self.assertIsNone(spec.exit)


# --------------------------------------------------------------------------- #
# V3 #1b：parser 拒绝非法 DSL（fail-closed，不静默兜底）
# --------------------------------------------------------------------------- #
class TestParserRejects(unittest.TestCase):
    def assert_rejected(self, dsl_or_node, needle: str = "") -> None:
        with self.assertRaises(DslParseError) as ctx:
            if isinstance(dsl_or_node, dict) and "entry" in dsl_or_node:
                dsl_to_spec(dsl_or_node, universe=(SYMBOL,))
            else:
                parse_dsl_node(dsl_or_node)
        if needle:
            self.assertIn(needle, str(ctx.exception))

    def test_unknown_operator(self) -> None:
        self.assert_rejected({"op": "totally_made_up"}, "未知算子")

    def test_missing_op(self) -> None:
        self.assert_rejected({"field": "close"}, "'op'")

    def test_node_not_dict(self) -> None:
        self.assert_rejected("sma(close,20)", "对象")

    def test_window_not_positive_int(self) -> None:
        for bad in ("20", True, 0, -3, 20.5, None):
            with self.subTest(bad=bad):
                self.assert_rejected({"op": "sma", "field": "close", "window": bad})

    def test_unknown_field(self) -> None:
        self.assert_rejected({"op": "sma", "field": "high", "window": 20}, "不受支持")

    def test_binary_wrong_arity(self) -> None:
        self.assert_rejected({"op": "cross_above", "args": [{"op": "field", "field": "close"}]},
                             "恰好 2 个")

    def test_binary_args_not_a_list(self) -> None:
        self.assert_rejected({"op": "gt", "args": "nope"}, "args")

    def test_shift_needs_positive_n(self) -> None:
        self.assert_rejected({"op": "shift", "field": "close", "n": 0}, "≥1")

    def test_missing_entry_rejected(self) -> None:
        dsl = golden_dsl()
        del dsl["entry"]
        with self.assertRaises(DslParseError):
            dsl_to_spec(dsl, universe=(SYMBOL,))

    def test_top_level_not_dict(self) -> None:
        with self.assertRaises(DslParseError):
            dsl_to_spec(["not", "a", "dict"])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# V3：DSL 算子白名单与 evaluate 严格同步（不得超集）
# --------------------------------------------------------------------------- #
class TestWhitelistSync(unittest.TestCase):
    def test_dsl_whitelist_matches_evaluate(self) -> None:
        evaluate_ops = {op for op in _SUPPORTED_OPS.split("/") if op}
        self.assertEqual(DSL_ALL_OPS, evaluate_ops,
                         "DSL 白名单必须与 emit.evaluate 支持的算子一一对应")


# --------------------------------------------------------------------------- #
# V3 #2（确定性对拍）：golden DSL → parser → 闸门 → spec2weights
# --------------------------------------------------------------------------- #
class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        register_builtin()
        cls.full = load_bundle_from_fixture()

    def data(self) -> DataBundle:
        return self.full.subset([SYMBOL])

    @staticmethod
    def weights(spec: StrategySpec, data: DataBundle) -> pd.DataFrame:
        return spec2weights(spec, MarketData(prices=data.closes, traded=data.traded),
                            momentum_window=spec.lookback)


class TestGoldenDslParity(_Base):
    def test_golden_dsl_weights_match_reference_exactly(self) -> None:
        data = self.data()
        spec = dsl_to_spec(golden_dsl(), universe=(SYMBOL,),
                           source_paper="papers/sample-ma-cross.md").spec
        got = self.weights(spec, data)
        ref = self.weights(_reference_spec(), data)
        pd.testing.assert_frame_equal(got, ref)


# --------------------------------------------------------------------------- #
# V3 #3：语义偏离可见（不静默入库）
# --------------------------------------------------------------------------- #
class TestSemanticDeviationVisible(_Base):
    def test_gt_vs_cross_is_structurally_accepted(self) -> None:
        """结构合法（gt 在白名单）→ parser 收下 —— parser 不也不该猜语义。"""
        spec = dsl_to_spec(deviated_dsl(), universe=(SYMBOL,)).spec
        self.assertIsNotNone(spec)
        self.assertEqual(spec.entry.op, "gt")

    def test_gt_vs_cross_weights_diverge_observably(self) -> None:
        """语义偏离必须**可观测**：权重与参考显著不同（而非悄悄一致）。"""
        data = self.data()
        dev = dsl_to_spec(deviated_dsl(), universe=(SYMBOL,)).spec
        w_dev = self.weights(dev, data)
        w_ref = self.weights(_reference_spec(), data)
        diff = float((w_dev[SYMBOL] - w_ref[SYMBOL]).abs().max())
        self.assertGreater(diff, 0.5,
                           "gt/lt 与 cross_above/cross_below 的权重应当显著不同")

    def test_needs_human_review_flag_is_preserved(self) -> None:
        dsl = golden_dsl()
        dsl["needs_human_review"] = True
        self.assertTrue(dsl_to_spec(dsl, universe=(SYMBOL,)).needs_human_review)
        dsl["needs_human_review"] = False
        self.assertFalse(dsl_to_spec(dsl, universe=(SYMBOL,)).needs_human_review)


if __name__ == "__main__":
    unittest.main(verbosity=2)
