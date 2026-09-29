"""P5.3 规格注册与闸门串联验收（LOCAL_DEPLOYMENT_PLAN.md §P5.3）。

覆盖：
    V3  **绕过 lint 的入库路径不存在**（结构性保证，不只是「记得调用」）
    V3  同一规格重复注册**幂等**
    另：未过闸门**不落盘**（负向）；落盘内容捆绑 lint 报告（可审计）

运行：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -m unittest tests.test_p5_registry -v
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

from quantlab.contract.types import (
    ORIGIN_HANDWRITTEN,
    ORIGIN_X2STRATEGY,
    ContractViolation,
    CostModel,
    DataRequirement,
    Expr,
    SizingSpec,
    StrategySpec,
)
from quantlab.x2.registry import (
    SpecRejected,
    list_specs,
    load_spec,
    register_spec,
    spec_id,
)


def good_spec(**overrides) -> StrategySpec:
    base = dict(
        name="momentum-20-60",
        universe=(1, 2, 3),
        entry=Expr("cross_above", (
            Expr("sma", (Expr("shift", (Expr("field", ("close",)), 1)), 20)),
            Expr("sma", (Expr("shift", (Expr("field", ("close",)), 1)), 60)))),
        sizing=SizingSpec(top_n=3),
        costs=CostModel.scenario(10),          # 显式情景（过 G7）
        lookback=60,
        origin=ORIGIN_HANDWRITTEN,
        data_requirements=(DataRequirement("bars_daily",
                                           ("ts", "close", "available_utc")),),
    )
    base.update(overrides)
    return StrategySpec(**base)


def broken_spec() -> StrategySpec:
    """过不了闸门的规格：价格字段没有 shift（触发 G4 未来函数）。"""
    return good_spec(name="lookahead",
                     entry=Expr("sma", (Expr("field", ("close",)), 20)),
                     costs=CostModel.scenario(10), lookback=60)


class _Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "specs"

    def tearDown(self) -> None:
        self.tmp.cleanup()


class TestGateCannotBeBypassed(_Scratch):
    """V3：**绕过 lint 的入库路径不存在**（结构性）。"""

    def test_register_api_has_no_skip_switch(self) -> None:
        """**结构性保证**：`register_spec` 不得存在任何绕过闸门的参数。

        这条比「用例里记得先 lint」强得多 —— 它断言**接口本身**没有可开的开关。
        将来有人加 `skip_lint=False` 之类的参数，这条会立刻红。
        """
        params = set(inspect.signature(register_spec).parameters)
        forbidden = {"skip_lint", "force", "no_lint", "bypass", "allow_invalid",
                     "unsafe", "skip_gate", "check", "lint"}
        leaked = params & forbidden
        self.assertEqual(leaked, set(),
                         f"register_spec 出现了可用于绕过闸门的参数: {sorted(leaked)}")

    def test_rejected_spec_is_not_written_to_disk(self) -> None:
        """未过闸门 → 抛错**且不落盘**（负向）。"""
        before = list(self.dir.glob("*")) if self.dir.is_dir() else []
        with self.assertRaises(SpecRejected) as ctx:
            register_spec(broken_spec(), specs_dir=self.dir)
        self.assertIn("拒绝入库", str(ctx.exception))
        self.assertIn("旁路", str(ctx.exception))          # 明说没有旁路
        after = list(self.dir.glob("*")) if self.dir.is_dir() else []
        self.assertEqual(after, before, "被拒的规格竟然落盘了")

    def test_good_spec_passes_and_is_written(self) -> None:
        """对照：合法规格能入库 —— 证明上一条拒的是「不合规」而非「全都拒」。"""
        result = register_spec(good_spec(), specs_dir=self.dir)
        self.assertTrue(result.path.is_file())
        self.assertFalse(result.already_present)
        self.assertTrue(result.report.passed)

    def test_structurally_invalid_spec_is_rejected_before_lint(self) -> None:
        """连结构都不合法的（如空 name）→ 直接拒绝。"""
        with self.assertRaises(ContractViolation):
            register_spec(good_spec(name=""), specs_dir=self.dir)


class TestIdempotence(_Scratch):
    """V3：同一规格重复注册幂等。"""

    def test_same_spec_registers_once(self) -> None:
        first = register_spec(good_spec(), specs_dir=self.dir)
        second = register_spec(good_spec(), specs_dir=self.dir)

        self.assertEqual(first.spec_id, second.spec_id)
        self.assertFalse(first.already_present)
        self.assertTrue(second.already_present, "第二次应识别为已存在")
        self.assertEqual(len(list(self.dir.glob("*.json"))), 1, "产生了重复文件")

    def test_id_is_content_addressed(self) -> None:
        """规格变了 → id 变；改回来 → id 复原（内容哈希，与注册次序无关）。"""
        base = spec_id(good_spec())
        self.assertEqual(base, spec_id(good_spec()))
        self.assertNotEqual(base, spec_id(good_spec(name="renamed")))
        self.assertEqual(base, spec_id(good_spec()))          # 复原

    def test_amending_a_spec_creates_a_new_record_not_overwrite(self) -> None:
        """修正规格 → **新记录**，旧记录原封不动（同 P2.5 的快照纪律）。"""
        first = register_spec(good_spec(), specs_dir=self.dir)
        original = first.path.read_bytes()

        amended = register_spec(good_spec(lookback=90), specs_dir=self.dir)
        self.assertNotEqual(amended.spec_id, first.spec_id)
        self.assertTrue(first.path.is_file(), "旧记录被删了")
        self.assertEqual(first.path.read_bytes(), original, "旧记录被覆盖了")
        self.assertEqual(len(list(self.dir.glob("*.json"))), 2)

    def test_conflicting_content_under_same_id_is_refused(self) -> None:
        """同 id 但内容不同（人为篡改）→ 拒绝覆盖，交人工核查。"""
        result = register_spec(good_spec(), specs_dir=self.dir)
        payload = json.loads(result.path.read_text(encoding="utf-8"))
        payload["spec"]["name"] = "tampered"
        result.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        with self.assertRaises(SpecRejected) as ctx:
            register_spec(good_spec(), specs_dir=self.dir)
        self.assertIn("冲突", str(ctx.exception))


class TestAuditability(_Scratch):
    """落盘内容必须可审计：捆绑当时的 lint 报告。"""

    def test_lint_report_is_bundled_with_the_spec(self) -> None:
        result = register_spec(good_spec(), specs_dir=self.dir)
        payload = json.loads(result.path.read_text(encoding="utf-8"))
        self.assertIn("lint", payload, "入库记录里没有 lint 报告 —— 事后无从审计")
        self.assertIn("rules_run", payload["lint"])
        self.assertTrue(payload["lint"]["passed"])
        self.assertIn("registered_at_utc", payload)

    def test_load_round_trips_the_spec(self) -> None:
        result = register_spec(good_spec(), specs_dir=self.dir)
        loaded = load_spec(result.spec_id, specs_dir=self.dir)
        self.assertEqual(loaded.spec.to_json(), result.spec.to_json())
        self.assertEqual(loaded.spec_id, result.spec_id)

    def test_list_specs_is_deterministic(self) -> None:
        register_spec(good_spec(), specs_dir=self.dir)
        register_spec(good_spec(name="other"), specs_dir=self.dir)
        ids = [s.spec_id for s in list_specs(specs_dir=self.dir)]
        self.assertEqual(ids, sorted(ids), "列表顺序不确定")

    def test_list_specs_on_missing_dir_is_empty_not_an_error(self) -> None:
        self.assertEqual(list_specs(specs_dir=Path(self.tmp.name) / "nope"), [])


class TestX2OriginRegistration(_Scratch):
    """x2 来源规格经 P5.2 接通后应可入库（不再被 rules_unavailable 挡）。"""

    def test_x2_origin_spec_can_be_registered(self) -> None:
        result = register_spec(good_spec(name="from-x2", origin=ORIGIN_X2STRATEGY),
                               specs_dir=self.dir)
        self.assertTrue(result.report.passed)
        self.assertIn("x2strategy", result.report.rules_run)

    def test_operator_notes_are_archived_and_do_not_block(self) -> None:
        """算子提示是咨询性的：**不阻断**，但要**存档**以便人工复核。"""
        result = register_spec(good_spec(name="x2-notes", origin=ORIGIN_X2STRATEGY),
                               specs_dir=self.dir,
                               operator_notes=["second_moment: 注意数值稳定性"])
        payload = json.loads(result.path.read_text(encoding="utf-8"))
        self.assertTrue(any("second_moment" in n for n in payload["operator_notes"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
