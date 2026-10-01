"""P5.2 paper2spec 封装验收（LOCAL_DEPLOYMENT_PLAN.md §P5.2）。

覆盖：
    V3  产出物**必须**通过 P3.2 的 lint 闸门
    V3  保留 `source_paper` 元信息（可追溯）
    另：x2 原始规格 → 契约的**保守映射**（未映射项如实记录，不猜）
    另：x2 算子的**咨询性**提示（命中不阻断）

**不调用 LLM、不联网**：全部走离线路径 —— `map_to_contract()` 与 `lint_spec()` 都是纯函数。
只有 `extract_raw_spec()` 需要真跑 paper2spec（要 LLM），故**不在本文件调用**。

运行：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -m unittest tests.test_p5_paper2spec -v
"""

from __future__ import annotations

import unittest

from quantlab.contract.lint import lint_spec
from quantlab.contract.types import ORIGIN_X2STRATEGY, Expr
from quantlab.x2.paper2spec import OP_ALIASES, map_to_contract

# 一份「像 x2strategy 会产出」的原始规格（字段名取自 paper2spec.models.StrategySpec）
RAW_SPEC = {
    "strategy_name": "ETF Momentum Rotation",
    "universe_assets": ["SPY", "QQQ", "TLT"],
    "lookback_period": 63,
    "indicators": [{"name": "momentum", "indicator_id": "IND1",
                    "executable_explanation": "过去 63 日收益率"}],
    "logic_pipeline": [
        {"step_id": "S1", "output": "score", "expression": "momentum(close, 63)",
         "executable_explanation": "按动量排序取前 3"},
    ],
    "position_sizing": {"top_n": 3},
    "needs_human_review": False,
}

_WINDOW_TARGETS = ("sma", "ema", "std", "momentum")


def _has_shift_ancestor(root: Expr, target) -> bool:
    """`target` 这个 `field` 叶子是否有 shift/lag ≥ 1 的祖先。"""
    def walk(node: Expr, shifted: bool) -> bool:
        if node.op in ("shift", "lag"):
            n = node.args[1] if len(node.args) > 1 else 1
            shifted = shifted or (isinstance(n, (int, float)) and not isinstance(n, bool)
                                  and n >= 1)
        if node is target:
            return shifted
        return any(walk(a, shifted) for a in node.args if isinstance(a, Expr))

    return walk(root, False)


class TestMapping(unittest.TestCase):
    """x2 原始规格 → 我们的契约（保守映射）。"""

    def setUp(self) -> None:
        self.result = map_to_contract(RAW_SPEC, universe=(1, 2, 3),
                                      source_paper="papers/sample.md", model="anthropic/x")

    def test_maps_to_our_contract(self) -> None:
        spec = self.result.spec
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "ETF Momentum Rotation")
        self.assertEqual(spec.universe, (1, 2, 3))          # 内部 symbol_id
        self.assertEqual(spec.origin, ORIGIN_X2STRATEGY)
        self.assertEqual(spec.sizing.top_n, 3)
        self.assertEqual(spec.lookback, 63)

    def test_source_paper_is_preserved(self) -> None:
        """V3：`source_paper` 元信息必须保留（可追溯）。"""
        self.assertEqual(self.result.spec.source_paper, "papers/sample.md")

    def test_lookahead_is_structurally_removed(self) -> None:
        """映射出的 `entry` 必须**自带** `shift(1)`，否则过不了 G4。"""
        entry = self.result.spec.entry
        self.assertIsNotNone(entry)
        shifts = [n for n in entry.walk() if n.op in ("shift", "lag")]
        self.assertTrue(shifts, "映射结果没有 shift —— G4 会判为未来函数")
        self.assertTrue(all(n.args[1] >= 1 for n in shifts))

    def test_cost_model_is_explicit_not_bare_default(self) -> None:
        """必须取 F.8 的显式情景，否则闸门 G7（成本未显式指定）会拒。"""
        costs = self.result.spec.costs
        self.assertNotEqual(costs.label, "custom")
        self.assertGreater(costs.one_way_bps, 0)

    def test_data_requirements_declare_available_utc(self) -> None:
        """必须声明 `available_utc`，否则 G3 会拒。"""
        self.assertIn("available_utc", self.result.spec.data_requirements[0].fields)

    def test_universe_is_not_guessed(self) -> None:
        """未给 universe 时**不得猜**，应留空并在 notes 里说明。"""
        result = map_to_contract(RAW_SPEC)
        self.assertEqual(result.spec.universe, ())
        self.assertTrue(any("symbol_id" in n for n in result.notes),
                        f"未提示 universe 需要人工映射: {result.notes}")


class TestGateIntegration(unittest.TestCase):
    """V3：映射产物**必须**能过 P3.2 闸门。"""

    def test_mapped_spec_passes_the_lint_gate(self) -> None:
        result = map_to_contract(RAW_SPEC, universe=(1, 2, 3),
                                 source_paper="papers/sample.md")
        report = lint_spec(result.spec)
        self.assertTrue(report.passed,
                        "映射产物未过闸门:\n" + "\n".join(str(f) for f in report.errors))

    def test_mapped_spec_is_accepted_as_x2_origin(self) -> None:
        """P5.2 后 x2 规则已接通，x2 来源规格**不再**因「规则没接」被挡。"""
        result = map_to_contract(RAW_SPEC, universe=(1, 2, 3))
        report = lint_spec(result.spec)
        self.assertNotIn("X2.rules_unavailable", {f.rule for f in report.errors})

    def test_operator_notes_do_not_block(self) -> None:
        """算子提示命中时**仍应通过**（咨询性，经人工确认的取舍）。"""
        result = map_to_contract(RAW_SPEC, universe=(1, 2, 3))
        report = lint_spec(result.spec, operator_notes=["second_moment: 注意数值稳定性"])
        self.assertTrue(report.passed)


class TestEnvelopeAndEntryGate(unittest.TestCase):
    """P5.6 端到端暴露的两处问题（均已修）。"""

    def test_real_paper2spec_envelope_is_unwrapped(self) -> None:
        """真实产出是**信封** `{num_detected, paper_title, strategies:[…]}`，须拆开取用。

        不修的话，整份产出会被当成「没有 logic_pipeline」→ **静默映射成空 entry**。
        """
        inner = dict(RAW_SPEC)
        envelope = {"num_detected": 1, "paper_title": "X", "strategies": [inner]}
        result = map_to_contract(envelope, universe=(1, 2, 3), source_paper="p.md")
        self.assertEqual(result.spec.name, "ETF Momentum Rotation")
        self.assertIsNotNone(result.spec.entry, "信封没有被拆开 —— entry 又静默变 None")

    def test_multiple_strategies_are_not_silently_dropped(self) -> None:
        """一封多策略：取第 1 个，但必须**如实记录**（不静默丢）。"""
        envelope = {"paper_title": "Y",
                    "strategies": [dict(RAW_SPEC), dict(RAW_SPEC)]}
        result = map_to_contract(envelope, universe=(1,))
        self.assertTrue(any("2 个策略" in n for n in result.notes), result.notes)

    def test_spec_without_entry_is_rejected_by_the_gate(self) -> None:
        """**静默全现金**防线：没有 entry 的规格必须被闸门拒。

        没有 entry → `emit_weights` 的资格筛选恒为空 → 权重恒 0 → 策略永远空仓，
        回测跑得出、收益恒 0、**不报任何错**。P5.6 实测：闸门**曾放行**。
        """
        raw = {k: v for k, v in RAW_SPEC.items() if k != "logic_pipeline"}
        result = map_to_contract(raw, universe=(1, 2, 3))
        self.assertIsNone(result.spec.entry)                # 前提：确实没映射出入口
        report = lint_spec(result.spec)
        self.assertFalse(report.passed, "没有 entry 的规格竟然过了闸门 → 会静默全现金")
        self.assertIn("G9.entry_present", {f.rule for f in report.errors})


class TestConservativeMapping(unittest.TestCase):
    """拿不准的**不猜**：如实记入 `unmapped` 并标为待人工复核。"""

    def test_unknown_operator_is_recorded_not_guessed(self) -> None:
        raw = dict(RAW_SPEC)
        raw["logic_pipeline"] = [{"name": "totally_made_up_operator", "params": {"period": 5}}]
        result = map_to_contract(raw, universe=(1,))
        self.assertTrue(result.unmapped, "未知算子没有被记录")
        self.assertTrue(any("totally_made_up_operator" in u for u in result.unmapped))
        self.assertTrue(result.needs_human_review, "有未映射项时应当标记待人工复核")

    def test_known_aliases_are_applied(self) -> None:
        """别名表覆盖的算子应当被正常映射。"""
        for alias, target in OP_ALIASES.items():
            if target not in _WINDOW_TARGETS and target not in ("shift", "lag"):
                continue
            with self.subTest(alias=alias):
                raw = dict(RAW_SPEC)
                raw["logic_pipeline"] = [{"name": alias, "field": "close",
                                          "params": {"period": 20}}]
                result = map_to_contract(raw, universe=(1,))
                entry = result.spec.entry
                self.assertIsNotNone(entry, f"{alias} 未被映射")
                self.assertIn(target, [n.op for n in entry.walk()])

    def test_missing_logic_pipeline_is_noted(self) -> None:
        raw = {k: v for k, v in RAW_SPEC.items() if k != "logic_pipeline"}
        result = map_to_contract(raw, universe=(1,))
        self.assertIsNone(result.spec.entry)
        self.assertTrue(any("logic_pipeline" in n for n in result.notes))

    def test_result_is_json_serialisable(self) -> None:
        """产出要能落盘存档（P5.3 的 registry 会用）。"""
        import json

        json.dumps(map_to_contract(RAW_SPEC, universe=(1, 2, 3)).as_dict(),
                   ensure_ascii=False)


class TestLookaheadSafety(unittest.TestCase):
    """未来函数**不能**靠映射侥幸过关 —— 映射出的表达式必须结构性安全。"""

    def test_all_price_leaves_have_a_shift_ancestor(self) -> None:
        entry = map_to_contract(RAW_SPEC, universe=(1,)).spec.entry
        self.assertIsNotNone(entry)
        leaves = [n for n in entry.walk() if n.op == "field"]
        self.assertTrue(leaves, "表达式里没有字段叶子，本用例失去意义")
        for leaf in leaves:
            with self.subTest(field=leaf.args):
                self.assertTrue(_has_shift_ancestor(entry, leaf),
                                f"价格叶子 {leaf.args} 没有 shift 祖先")


if __name__ == "__main__":
    unittest.main(verbosity=2)
