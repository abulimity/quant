"""close_adj 复权收盘（silver 层，P0c）测试。

覆盖：
    · `forward_adjust_close` 纯函数：拆分后复权（split_forward）与供应商复权因子
      后复权（fund_adj_forward）两种口径、D0 锚定、method 溯源。
    · `materialize_close_adj`：把单快照物化进 `close_adj` 表，行数 = bars_daily 行数，
      `v_close_adj_latest` 可查询（绑定最新成功快照）。
    · `register_snapshot_views`：真实供应商的 raw 表（fund_adj / index_daily）也被
      挂成零拷贝视图。

**本模块不联网**：全部用合成夹具或内联合成帧。

运行：
    python -m unittest tests.test_close_adj -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from quantlab.fixtures.synth import generate, write_snapshot
from quantlab.quality.clean import forward_adjust_close
from quantlab.store.warehouse import (
    materialize_close_adj,
    register_snapshot_views,
)
from tests import helpers


class TestForwardAdjustClose(unittest.TestCase):
    """`forward_adjust_close` 纯函数：两种复权口径。"""

    def test_split_forward_d0_anchored(self) -> None:
        """后复权以**最初**价为基准：除权前不变，除权后按拆分比例放大。"""
        bars = pd.DataFrame({
            "symbol_id": [1, 1, 1],
            "ts": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
            "open": [398.0, 99.0, 108.0],
            "high": [402.0, 102.0, 112.0],
            "low": [396.0, 98.0, 106.0],
            "close": [400.0, 100.0, 110.0],
        })
        actions = pd.DataFrame({
            "symbol_id": [1],
            "ex_date": [date(2024, 1, 3)],
            "kind": ["split"],
            "ratio": [4.0],        # 1 拆 4
            "cash": [None],
        })
        out = forward_adjust_close(bars, actions)
        self.assertEqual(out["method"].iloc[0], "split_forward")
        # D0 = 400（不变）；除权日 100 → 400（×4）；次日 110 → 440（×4）
        self.assertAlmostEqual(float(out["close_adj"].iloc[0]), 400.0, places=9)
        self.assertAlmostEqual(float(out["close_adj"].iloc[1]), 400.0, places=9)
        self.assertAlmostEqual(float(out["close_adj"].iloc[2]), 440.0, places=9)
        self.assertAlmostEqual(float(out["adj_factor"].iloc[0]), 1.0, places=9)

    def test_fund_adj_forward_normalizes_by_first_factor(self) -> None:
        """供应商累计复权因子（含分红）：close_adj = close × (factor/factor[0])。"""
        bars = pd.DataFrame({
            "symbol_id": [1, 1, 1],
            "ts": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
            "close": [100.0, 110.0, 105.0],
        })
        fund_adj = pd.DataFrame({
            "symbol_id": [1, 1, 1],
            "ts": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
            "adj_factor": [1.0, 1.1, 1.05],
        })
        out = forward_adjust_close(bars, None, fund_adj)
        self.assertEqual(out["method"].iloc[0], "fund_adj_forward")
        self.assertAlmostEqual(float(out["close_adj"].iloc[0]), 100.0, places=9)
        self.assertAlmostEqual(float(out["close_adj"].iloc[1]), 121.0, places=9)
        self.assertAlmostEqual(float(out["close_adj"].iloc[2]), 110.25, places=9)

    def test_empty_bars_returns_contract_columns(self) -> None:
        out = forward_adjust_close(pd.DataFrame(columns=["symbol_id", "ts", "close"]))
        self.assertEqual(
            set(out.columns), {"symbol_id", "ts", "close_adj", "adj_factor", "method"})


class TestMaterializeCloseAdj(unittest.TestCase):
    """把单快照物化进 `close_adj`，且 `v_close_adj_latest` 可查询。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "synthetic"
        cls.bundle = generate()
        write_snapshot(cls.bundle, cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _con(self) -> duckdb.DuckDBPyConnection:
        con = helpers.migrated_memory_con()
        self.addCleanup(con.close)
        return con

    def test_materialize_rows_match_bars_and_method_recorded(self) -> None:
        con = self._con()
        from quantlab.store.warehouse import load_snapshot
        load_snapshot(con, self.root, self.bundle.snapshot_id)

        res = materialize_close_adj(con, self.root, self.bundle.snapshot_id)
        n_bars = int(len(self.bundle.tables["bars_daily"]))
        self.assertEqual(res["close_adj"], n_bars)
        # 合成夹具无 fund_adj → 走纯拆分后复权
        self.assertEqual(res["method"], "split_forward")
        self.assertEqual(
            helpers.scalar(con, "SELECT count(*) FROM close_adj"), n_bars)

    def test_v_close_adj_latest_returns_only_latest_ok_snapshot(self) -> None:
        con = self._con()
        from quantlab.store.warehouse import load_snapshot
        load_snapshot(con, self.root, self.bundle.snapshot_id)
        materialize_close_adj(con, self.root, self.bundle.snapshot_id)
        helpers.insert_ingest_run(con, self.bundle.snapshot_id,
                                  finished_at=helpers.naive_utc(2025, 1, 2))

        n_bars = int(len(self.bundle.tables["bars_daily"]))
        self.assertEqual(
            helpers.scalar(con, "SELECT count(*) FROM v_close_adj_latest"), n_bars)
        snaps = {r[0] for r in con.execute(
            "SELECT DISTINCT snapshot_id FROM v_close_adj_latest").fetchall()}
        self.assertEqual(snaps, {self.bundle.snapshot_id})

    def test_missing_bars_returns_note_not_error(self) -> None:
        con = self._con()
        with tempfile.TemporaryDirectory() as tmp:
            res = materialize_close_adj(con, tmp, "no-such-snapshot")
        self.assertEqual(res["close_adj"], 0)
        self.assertIn("bars_daily 缺失", res["note"])


class TestRegisterRawViews(unittest.TestCase):
    """真实供应商的 raw 表（fund_adj / index_daily）也被挂成零拷贝视图。"""

    def test_raw_fund_adj_and_index_daily_views_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "real-snap"
            snap.mkdir()
            pd.DataFrame({"symbol_id": [1], "ts": [date(2024, 1, 2)],
                          "adj_factor": [1.0]}).to_parquet(snap / "fund_adj.parquet")
            pd.DataFrame({"ts_code": ["000001.SH"], "ts": [date(2024, 1, 2)],
                          "close": [3000.0]}).to_parquet(snap / "index_daily.parquet")

            con = duckdb.connect(":memory:")
            try:
                created = register_snapshot_views(con, tmp, "real-snap")
                self.assertIn("raw_fund_adj", created)
                self.assertIn("raw_index_daily", created)
                self.assertEqual(
                    con.execute("SELECT count(*) FROM raw_fund_adj").fetchone()[0], 1)
                self.assertEqual(
                    con.execute("SELECT count(*) FROM raw_index_daily").fetchone()[0], 1)
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
