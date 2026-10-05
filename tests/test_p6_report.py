r"""P6.2 报告生成验收（LOCAL_DEPLOYMENT_PLAN.md §P6.2 / F.10）。

`write_run_outputs` 把一次回测的全部产出落进 `runs/<run_id>/`：

    · 真相 Parquet（equity/positions/trades/weights）+ 同名 CSV
    · metrics.json（含年化口径声明）/ spec.json / params.json / run_meta.json
    · equity.png / report.md / tearsheet.html（自包含内嵌 PNG）

本文件钉住「**全文件落盘**」与「**回读一致**」。

运行：
    $env:PYTHONIOENCODING='utf-8'
    uv run --project D:\project\quant python -m unittest tests.test_p6_report -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from quantlab.eval.report import write_run_outputs
from quantlab.engines.execution import TRADE_COLUMNS


class TestWriteRunOutputs(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run-demo"
        self.index = pd.bdate_range("2024-01-02", periods=4, name="ts")
        self.equity = pd.Series([1.0, 1.02, 1.01, 1.03], index=self.index, name="equity")
        self.positions = pd.DataFrame({1: [0.0, 9800.0, 9800.0, 9800.0]},
                                      index=self.index)
        self.weights = pd.DataFrame({1: [1.0, 1.0, 1.0, 1.0]}, index=self.index)
        self.trades = pd.DataFrame([{
            "ts": pd.Timestamp("2024-01-03"), "symbol_id": 1, "side": "buy",
            "units": 9800.0, "price": 100.0, "gross": 980000.0, "cost": 0.0,
            "target_weight": 1.0, "actual_weight": 1.0, "reason": None,
        }], columns=TRADE_COLUMNS)
        self.metrics = {
            "initial_equity": 1_000_000.0, "final_equity": 1_030_000.0,
            "total_return": 0.03, "annualized_return": 0.21,
            "annualized_volatility": 0.10, "sharpe_ratio": 1.4,
            "max_drawdown": -0.01, "risk_free_rate": 0.0,
            "annualization_periods": 261.0,
            "annualization_note": "年化口径：由数据自身会话密度推导 periods_per_year=261.0000…",
        }
        self.spec_dict = {"name": "demo", "universe": [1]}
        self.params = {"cost_label": "0bps", "one_way_bps": 0.0, "initial_cash": 1_000_000.0}
        self.run_meta = {"run_id": "run-demo", "spec_id": "sid", "engine": "portfolio"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_all_files_land_on_disk(self) -> None:
        out = write_run_outputs(
            self.run_dir, equity=self.equity, positions=self.positions,
            trades=self.trades, weights=self.weights, metrics=self.metrics,
            spec_dict=self.spec_dict, params=self.params, run_meta=self.run_meta)
        self.assertTrue(out.is_dir())
        for name in ("equity.parquet", "equity.csv", "positions.parquet", "positions.csv",
                     "trades.parquet", "trades.csv", "weights.parquet", "weights.csv",
                     "metrics.json", "spec.json", "params.json", "run_meta.json",
                     "equity.png", "report.md", "tearsheet.html"):
            self.assertTrue((out / name).is_file(), f"缺产出 {name}")

    def test_equity_parquet_round_trips(self) -> None:
        out = write_run_outputs(
            self.run_dir, equity=self.equity, positions=self.positions,
            trades=self.trades, weights=self.weights, metrics=self.metrics,
            spec_dict=self.spec_dict, params=self.params, run_meta=self.run_meta)
        frame = pd.read_parquet(out / "equity.parquet")
        self.assertEqual(list(frame.columns), ["ts", "equity"])
        self.assertEqual(len(frame), len(self.equity))
        self.assertAlmostEqual(float(frame["equity"].iloc[-1]), 1.03)

    def test_metrics_json_round_trips_and_declares_note(self) -> None:
        out = write_run_outputs(
            self.run_dir, equity=self.equity, positions=self.positions,
            trades=self.trades, weights=self.weights, metrics=self.metrics,
            spec_dict=self.spec_dict, params=self.params, run_meta=self.run_meta)
        metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(metrics["total_return"], 0.03)
        self.assertIn("annualization_note", metrics)

    def test_tearsheet_is_self_contained(self) -> None:
        out = write_run_outputs(
            self.run_dir, equity=self.equity, positions=self.positions,
            trades=self.trades, weights=self.weights, metrics=self.metrics,
            spec_dict=self.spec_dict, params=self.params, run_meta=self.run_meta)
        html = (out / "tearsheet.html").read_text(encoding="utf-8")
        self.assertIn("data:image/png;base64,", html, "tearsheet 未内嵌 PNG，不自包含")

    def test_report_md_discloses_risk_free_zero(self) -> None:
        out = write_run_outputs(
            self.run_dir, equity=self.equity, positions=self.positions,
            trades=self.trades, weights=self.weights, metrics=self.metrics,
            spec_dict=self.spec_dict, params=self.params, run_meta=self.run_meta)
        md = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("无风险利率", md)
        self.assertIn("0", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
