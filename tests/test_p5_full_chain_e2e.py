r"""P5.6 全链路六段 E2E —— V2（真实 vbt 桥）+ V4（可复现）+ run 登记（P6.3）。

一条命令把「夹具数据 + golden DSL → 三引擎 → 组合 → 报告」跑通并落
`runs/<run_id>/`。本文件用 `run_full_chain` 直接验证：

    V2  六段全跑通：lint 闸门 → vectorbt 粗筛（**真实桥**，`coarse_screen_only`）→
        backtrader 精验 → bt 组合 → reference 对账 → 报告落盘。
    V4  同输入重跑两遍，指标在容差内一致（引擎 deterministic）。
    V3  backtrader↔reference@open 硬对拍 ≤ 1e-4；bt↔reference@close 只在无停牌缺口时
        ≤ 1e-4，有缺口时必须是**已记录**的 halt_deferral 偏差（见 §P4.5 / parity_report）。
    V3  run registry 可读（`register=True` → `get_run`/`get_metrics` 往返）。

运行（**必须在主检出环境 + 数据**，见 CLAUDE.md）：
    Remove-Item Env:UV_PROJECT_ENVIRONMENT,Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
    $env:QUANT_ROOT='D:\project\quant'
    $env:PYTHONIOENCODING='utf-8'
    uv run --project D:\project\quant python -m unittest tests.test_p5_full_chain_e2e -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quantlab.contract.types import F8_SCENARIOS
from quantlab.engines.base import register_builtin
from quantlab.pipeline import run_full_chain
from quantlab.registry.runs import get_metrics, get_run
from quantlab.store.db import connect

SYMBOL = 1
COSTS = F8_SCENARIOS[0]
TOLERANCE = 1e-4


def golden_dsl() -> dict:
    """sample-ma-cross.md 的忠实 DSL（与 tests/test_p5_dsl.py 同源，确定）。"""
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


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        register_builtin()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()


class TestFullChainV2(_Base):
    """V2：六段全跑通（真实 vbt 桥，含 numba JIT）。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.result = run_full_chain(
            dsl=golden_dsl(), universe=(SYMBOL,), costs=COSTS,
            out_dir=cls.root / "v2", run_vbt=True, run_id="e2e-v2")

    def test_spec_resolved_and_gate_passed(self) -> None:
        self.assertTrue(self.result.lint_report.passed)
        self.assertEqual(self.result.spec.name, "sample-ma-cross")
        self.assertEqual(self.result.spec.universe, (SYMBOL,))

    def test_vbt_scan_is_real_and_labelled_coarse(self) -> None:
        scan = self.result.vbt_scan
        self.assertIsNotNone(scan, "vectorbt 粗筛未执行")
        self.assertEqual(scan["match_quality"], "coarse_screen_only")
        self.assertGreater(scan["n_combos"], 0)
        self.assertEqual(scan["anchor_fast"], 20)
        self.assertEqual(scan["anchor_slow"], 60)
        self.assertIsNotNone(scan["anchor_combo"], "未回读 20/60 锚点组合的粗筛指标")
        self.assertIsNotNone(scan["vectorbt_version"])

    def test_three_engine_parity_within_tolerance(self) -> None:
        # backtrader 与 reference@open 都正确顺延停牌 → 硬对拍 ≤ 1e-4（E2E-A 已证）。
        self.assertLessEqual(self.result.parity["backtrader_vs_reference_open"], TOLERANCE)

        # bt 在再平衡日**收盘**撮合、且无法表达逐标的停牌顺延。全链路用联合日历索引
        # （load_bundle_from_fixture 的 union），标的 1 有交易所休市日（traded=False），
        # bt 会就地按 ffill 旧价成交 → 与 reference@close 的顺延口径产生**已知偏差**。
        # 该偏差必须被显式记录（halt_sessions / known_deviation），不得静默吞掉。
        bt = self.result.per_engine["bt"]
        halt = int(bt.stats.get("halt_sessions", 0))
        dev = self.result.parity["bt_vs_reference_close"]
        if halt == 0:
            self.assertLessEqual(dev, TOLERANCE, "无停牌缺口时 bt 必须与 reference@close 一致")
        else:
            self.assertGreater(halt, 0)
            self.assertIn("halt_deferral_unsupported",
                          bt.run_meta.get("known_deviation", ""),
                          "bt 的停牌顺延偏差必须被显式记录（非静默）")

    def test_all_outputs_land_on_disk(self) -> None:
        for name in ("equity.parquet", "positions.parquet", "trades.parquet",
                     "weights.parquet", "metrics.json", "spec.json", "params.json",
                     "run_meta.json", "report.md", "tearsheet.html", "equity.png"):
            self.assertTrue((self.root / "v2" / name).is_file(), f"缺产出 {name}")
        self.assertTrue((self.root / "v2" / "vbt" / "result.parquet").is_file(),
                        "缺 vbt 粗筛 combos")

    def test_no_human_review_for_golden_dsl(self) -> None:
        self.assertFalse(self.result.needs_human_review)


class TestFullChainV4Determinism(_Base):
    """V4：同输入重跑两遍，指标在容差内一致。"""

    def test_same_input_same_metrics(self) -> None:
        a = run_full_chain(
            dsl=golden_dsl(), universe=(SYMBOL,), costs=COSTS,
            out_dir=self.root / "v4a", run_vbt=False, run_id="e2e-v4a")
        b = run_full_chain(
            dsl=golden_dsl(), universe=(SYMBOL,), costs=COSTS,
            out_dir=self.root / "v4b", run_vbt=False, run_id="e2e-v4b")

        self.assertNotEqual(a.run_id, b.run_id)
        for key in ("initial_equity", "final_equity", "total_return",
                    "annualized_return", "annualized_volatility", "sharpe_ratio",
                    "max_drawdown", "turnover", "n_rebalances"):
            self.assertAlmostEqual(a.metrics[key], b.metrics[key], places=9,
                                   msg=f"指标 {key} 两次运行不一致")


class TestFullChainRegistry(_Base):
    """V3：run registry 可读（register=True → get_run/get_metrics）。"""

    def test_register_and_read_back(self) -> None:
        warehouse = self.root / "registry.duckdb"
        result = run_full_chain(
            dsl=golden_dsl(), universe=(SYMBOL,), costs=COSTS,
            out_dir=self.root / "reg", run_vbt=False,
            register=True, warehouse=warehouse, run_id="e2e-reg")

        con = connect(read_only=False, path=warehouse)
        try:
            row = get_run(con, "e2e-reg")
            self.assertIsNotNone(row)
            self.assertEqual(row["spec_id"], result.spec_id)
            self.assertEqual(row["status"], "ok")
            metrics = get_metrics(con, "e2e-reg")
            self.assertIn("total_return", metrics)
            self.assertIn("max_drawdown", metrics)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
