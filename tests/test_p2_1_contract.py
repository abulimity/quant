"""P2.1 数据契约验收（LOCAL_DEPLOYMENT_PLAN.md §P2.1）。

覆盖：
    V1  DDL 幂等；重复执行不报错、不改结构、不重复登记
    V3  主键/外键/NOT NULL/CHECK 约束生效（复合主键含 snapshot_id）
    V3  事实表均含 available_utc + snapshot_id（脚本化断言，非目测）
    V3  macro_series 可写回读；fundamentals 的 as_of_date < period_end 被拒绝
    另：快照叠加哨兵、schema 漂移检测、v_bars_latest 单快照语义、连接层基本语义

运行：
    python -m unittest discover -t . -s tests -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import date
from pathlib import Path

import duckdb

from quantlab.store.db import WarehouseNotFoundError, connect
from quantlab.store.migrate import (
    SCHEMA_VERSION,
    SchemaDriftError,
    apply_migrations,
    read_schema_sql,
    schema_version,
)
from quantlab.store.snapshot_guard import (
    SnapshotLeakError,
    assert_single_snapshot,
    referenced_fact_tables,
)
from tests import helpers

EXPECTED_TABLES = {
    "symbols", "bars_daily", "corporate_actions", "fx_rates",
    "trading_calendar", "macro_series", "fundamentals", "ingest_runs",
    "schema_migrations",
}
FACT_TABLES = ["bars_daily", "corporate_actions", "fx_rates", "macro_series", "fundamentals"]


class TestSchemaShape(unittest.TestCase):
    """V1：schema 建立与幂等。"""

    def setUp(self) -> None:
        self.con = helpers.migrated_memory_con()

    def tearDown(self) -> None:
        self.con.close()

    def test_expected_tables_exist(self) -> None:
        rows = self.con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
        ).fetchall()
        missing = EXPECTED_TABLES - {r[0] for r in rows}
        self.assertEqual(missing, set(), f"缺少契约表: {sorted(missing)}")

    def test_single_snapshot_view_exists(self) -> None:
        rows = self.con.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'main'"
        ).fetchall()
        self.assertIn("v_bars_latest", {r[0] for r in rows})

    def test_ddl_is_idempotent(self) -> None:
        """V1：重复执行不报错、不重复登记、结构不变，但**确实执行了**语句。"""
        before = helpers.scalar(
            self.con,
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='main'",
        )
        report = apply_migrations(self.con)
        after = helpers.scalar(
            self.con,
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='main'",
        )
        self.assertFalse(report["applied_now"], "第二次执行不应重复登记")
        self.assertGreater(report["statements"], 0, "应真的执行了 DDL 语句（而非静默跳过）")
        self.assertEqual(before, after, "表数量不应变化")

    def test_schema_version_recorded_once(self) -> None:
        apply_migrations(self.con)
        self.assertEqual(schema_version(self.con), SCHEMA_VERSION)
        self.assertEqual(helpers.scalar(self.con, "SELECT count(*) FROM schema_migrations"), 1)

    def test_all_fact_tables_carry_available_utc_and_snapshot_id(self) -> None:
        """V3：脚本化断言，**非目测**。"""
        for table in FACT_TABLES:
            cols = helpers.columns_of(self.con, table)
            self.assertIn("available_utc", cols, f"{table} 缺少 available_utc")
            self.assertIn("snapshot_id", cols, f"{table} 缺少 snapshot_id")
            self.assertIn("source", cols, f"{table} 缺少 source（溯源）")

    def test_macro_series_is_first_class_not_attached_to_bars(self) -> None:
        """宏观必须独立成表，不得塞进 bars_daily 当附属列。"""
        self.assertIn("series_id", helpers.columns_of(self.con, "macro_series"))
        bar_cols = " ".join(helpers.columns_of(self.con, "bars_daily")).lower()
        self.assertNotIn("macro", bar_cols)


class TestConstraints(unittest.TestCase):
    """V3：约束真的生效（负向为主）。"""

    def setUp(self) -> None:
        self.con = helpers.migrated_memory_con()
        helpers.insert_symbol(self.con, 1)
        helpers.insert_symbol(self.con, 2, ticker="SYN002", exchange="XNYS",
                              calendar="XNYS", currency="USD")

    def tearDown(self) -> None:
        self.con.close()

    def test_bars_composite_primary_key_rejects_duplicate(self) -> None:
        helpers.insert_bar(self.con, symbol_id=1, snapshot_id="synth-A")
        with self.assertRaises(duckdb.ConstraintException):
            helpers.insert_bar(self.con, symbol_id=1, snapshot_id="synth-A")

    def test_same_ts_different_snapshot_is_allowed(self) -> None:
        """同一 (symbol, ts) 在不同快照中共存 —— 这正是「必须限定单一快照」的由来。"""
        helpers.insert_bar(self.con, symbol_id=1, snapshot_id="synth-A")
        helpers.insert_bar(self.con, symbol_id=1, snapshot_id="synth-B")
        self.assertEqual(
            helpers.scalar(self.con, "SELECT count(*) FROM bars_daily WHERE symbol_id=1"), 2
        )

    def test_bars_foreign_key_rejects_unknown_symbol(self) -> None:
        with self.assertRaises(duckdb.ConstraintException):
            helpers.insert_bar(self.con, symbol_id=999)

    def test_available_utc_is_not_nullable(self) -> None:
        with self.assertRaises(duckdb.ConstraintException):
            self.con.execute(
                "INSERT INTO bars_daily (symbol_id, ts, close, available_utc, source, "
                "downloaded_at, snapshot_id) VALUES (1, DATE '2024-01-02', 1.0, NULL, "
                "'synth', TIMESTAMP '2024-01-03 00:00:00', 'synth-A')"
            )

    def test_fundamentals_as_of_date_before_period_end_rejected(self) -> None:
        """V3：未来信息必须被契约层拒绝。"""
        with self.assertRaises(duckdb.ConstraintException):
            self.con.execute(
                "INSERT INTO fundamentals VALUES "
                "(1, DATE '2024-03-31', DATE '2024-01-01', 'revenue', 1.0, "
                "TIMESTAMP '2024-01-01 00:00:00', 'synth', 'synth-A')"
            )

    def test_fundamentals_available_utc_before_as_of_date_rejected(self) -> None:
        """V3：发布时刻不可能早于可得日期的零点。"""
        with self.assertRaises(duckdb.ConstraintException):
            self.con.execute(
                "INSERT INTO fundamentals VALUES "
                "(1, DATE '2024-03-31', DATE '2024-04-30', 'revenue', 1.0, "
                "TIMESTAMP '2024-04-01 00:00:00', 'synth', 'synth-A')"
            )

    def test_fundamentals_as_of_equal_period_end_accepted(self) -> None:
        self.con.execute(
            "INSERT INTO fundamentals VALUES "
            "(1, DATE '2024-03-31', DATE '2024-03-31', 'revenue', 1.0, "
            "TIMESTAMP '2024-03-31 00:00:00', 'synth', 'synth-A')"
        )
        self.assertEqual(helpers.scalar(self.con, "SELECT count(*) FROM fundamentals"), 1)

    def test_corporate_actions_kind_is_constrained(self) -> None:
        with self.assertRaises(duckdb.ConstraintException):
            helpers.insert_corporate_action(self.con, kind="bonus_issue")

    def test_corporate_actions_payload_must_match_kind(self) -> None:
        with self.assertRaises(duckdb.ConstraintException):  # split 缺 ratio
            helpers.insert_corporate_action(self.con, kind="split")
        with self.assertRaises(duckdb.ConstraintException):  # dividend 缺 cash
            helpers.insert_corporate_action(self.con, kind="dividend")

    def test_corporate_actions_available_utc_is_not_nullable(self) -> None:
        """公司行动同样需要 available_utc —— 缺了它会产生「除权前已知拆分」的未来函数。"""
        with self.assertRaises(duckdb.ConstraintException):
            self.con.execute(
                "INSERT INTO corporate_actions "
                "(symbol_id, ex_date, kind, ratio, cash, pay_date, available_utc, source, "
                "snapshot_id) VALUES (1, DATE '2024-05-02', 'split', 2.0, NULL, NULL, NULL, "
                "'synth', 'synth-A')"
            )

    def test_corporate_actions_allows_dividend_and_split_same_day(self) -> None:
        helpers.insert_corporate_action(self.con, kind="split", ratio=2.0)
        helpers.insert_corporate_action(self.con, kind="dividend", cash=0.5,
                                        pay_date=date(2024, 5, 20))
        self.assertEqual(helpers.scalar(self.con, "SELECT count(*) FROM corporate_actions"), 2)

    def test_fx_rate_must_be_positive(self) -> None:
        with self.assertRaises(duckdb.ConstraintException):
            self.con.execute(
                "INSERT INTO fx_rates VALUES "
                "('USD', 'CNY', DATE '2024-01-02', -7.1, "
                "TIMESTAMP '2024-01-03 00:00:00', 'synth', 'synth-A')"
            )

    def test_fx_primary_key_includes_snapshot(self) -> None:
        for snap in ("synth-A", "synth-B"):
            self.con.execute(
                "INSERT INTO fx_rates VALUES "
                "('USD', 'CNY', DATE '2024-01-02', 7.1, "
                "TIMESTAMP '2024-01-03 00:00:00', 'synth', ?)", [snap],
            )
        with self.assertRaises(duckdb.ConstraintException):
            self.con.execute(
                "INSERT INTO fx_rates VALUES "
                "('USD', 'CNY', DATE '2024-01-02', 7.2, "
                "TIMESTAMP '2024-01-03 00:00:00', 'synth', 'synth-A')"
            )

    def test_trading_calendar_closed_day_has_no_close_time(self) -> None:
        with self.assertRaises(duckdb.ConstraintException):
            self.con.execute(
                "INSERT INTO trading_calendar VALUES "
                "('XSHG', DATE '2024-01-01', FALSE, TIMESTAMP '2024-01-01 07:00:00', 'synth')"
            )
        self.con.execute(
            "INSERT INTO trading_calendar VALUES ('XSHG', DATE '2024-01-01', FALSE, NULL, 'synth')"
        )
        self.assertEqual(helpers.scalar(self.con, "SELECT count(*) FROM trading_calendar"), 1)

    def test_symbols_delisted_before_listed_rejected(self) -> None:
        with self.assertRaises(duckdb.ConstraintException):
            helpers.insert_symbol(self.con, 3, ticker="SYN003",
                                  listed_on=date(2020, 1, 1), delisted_on=date(2019, 1, 1))

    def test_ingest_runs_status_is_constrained(self) -> None:
        with self.assertRaises(duckdb.ConstraintException):
            helpers.insert_ingest_run(self.con, "synth-X", status="finished")

    def test_macro_series_write_and_read_back(self) -> None:
        """V3：宏观可写回读，且带 available_utc + snapshot_id。"""
        self.con.execute(
            "INSERT INTO macro_series (series_id, source, ts, value, unit, available_utc, "
            "snapshot_id) VALUES ('DGS10', 'synth', DATE '2024-01-02', 3.95, 'percent', "
            "TIMESTAMP '2024-01-03 00:00:00', 'synth-A')"
        )
        row = self.con.execute(
            "SELECT series_id, value, unit, snapshot_id FROM macro_series WHERE snapshot_id = ?",
            ["synth-A"],
        ).fetchone()
        self.assertEqual(row, ("DGS10", 3.95, "percent", "synth-A"))


class TestSingleSnapshotDiscipline(unittest.TestCase):
    """P2.1 快照读取约定：不得跨快照叠加。"""

    def setUp(self) -> None:
        self.con = helpers.migrated_memory_con()
        helpers.insert_symbol(self.con, 1)
        for snap, close in (("synth-A", 100.0), ("synth-B", 111.0)):
            for day in (2, 3):
                helpers.insert_bar(self.con, 1, date(2024, 1, day), close=close, snapshot_id=snap)
        helpers.insert_ingest_run(self.con, "synth-A", finished_at=helpers.naive_utc(2024, 1, 3))
        helpers.insert_ingest_run(self.con, "synth-B", finished_at=helpers.naive_utc(2024, 1, 4))
        helpers.insert_ingest_run(self.con, "synth-C", status="running",
                                  finished_at=helpers.naive_utc(2024, 1, 5))

    def tearDown(self) -> None:
        self.con.close()

    def test_bare_select_stacks_snapshots(self) -> None:
        """反例留存：裸 SELECT 确实把两份快照叠加 —— 这正是必须避免的行为。"""
        self.assertEqual(
            helpers.scalar(self.con, "SELECT count(*) FROM bars_daily"), 4,
            "裸读应叠加 A/B 两份快照（2 天 × 2 快照）",
        )

    def test_view_returns_only_latest_ok_snapshot(self) -> None:
        snaps = {r[0] for r in self.con.execute(
            "SELECT DISTINCT snapshot_id FROM v_bars_latest").fetchall()}
        self.assertEqual(snaps, {"synth-B"}, "视图必须只返回最近一次**成功**的快照")
        self.assertEqual(helpers.scalar(self.con, "SELECT count(*) FROM v_bars_latest"), 2)

    def test_view_ignores_non_ok_snapshot(self) -> None:
        """即使 C（running）finished_at 更晚，也不得被选中。"""
        snaps = {r[0] for r in self.con.execute(
            "SELECT snapshot_id FROM v_bars_latest").fetchall()}
        self.assertNotIn("synth-C", snaps)


class TestSnapshotGuard(unittest.TestCase):
    """静态哨兵：保守但可执行的跨快照读取检查。"""

    def test_bare_fact_table_rejected(self) -> None:
        with self.assertRaises(SnapshotLeakError):
            assert_single_snapshot("SELECT * FROM bars_daily")

    def test_join_without_snapshot_rejected(self) -> None:
        with self.assertRaises(SnapshotLeakError):
            assert_single_snapshot(
                "SELECT b.* FROM symbols s JOIN bars_daily b ON b.symbol_id=s.symbol_id"
            )

    def test_qualified_read_allowed(self) -> None:
        assert_single_snapshot("SELECT * FROM bars_daily WHERE snapshot_id = ?")

    def test_view_allowed(self) -> None:
        assert_single_snapshot("SELECT * FROM v_bars_latest")

    def test_non_fact_table_allowed(self) -> None:
        assert_single_snapshot("SELECT * FROM symbols")
        assert_single_snapshot("SELECT * FROM ingest_runs")

    def test_comments_and_strings_do_not_trigger(self) -> None:
        assert_single_snapshot("SELECT 1 -- from bars_daily\n")
        assert_single_snapshot("SELECT 'from fx_rates' AS s")

    def test_referenced_fact_tables_detects_alias_form(self) -> None:
        self.assertEqual(referenced_fact_tables("select * from bars_daily b"), {"bars_daily"})


class TestSchemaDrift(unittest.TestCase):
    """schema.sql 变更后必须走新迁移，不得静默重建。"""

    def test_drift_detected(self) -> None:
        con = helpers.migrated_memory_con()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                altered = Path(tmp) / "schema.sql"
                altered.write_text(read_schema_sql() + "\n-- 人为改动\n", encoding="utf-8")
                with self.assertRaises(SchemaDriftError):
                    apply_migrations(con, path=altered)
        finally:
            con.close()


class TestWarehouseConnection(unittest.TestCase):
    """连接层语义（只读/锁的完整负向测试见 test_p2_3）。"""

    def test_read_only_open_of_missing_warehouse_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(WarehouseNotFoundError):
                connect(read_only=True, path=Path(tmp) / "nope.duckdb")

    def test_write_connection_creates_parent_dir_and_migrates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "warehouse.duckdb"
            con = connect(read_only=False, path=target)
            try:
                apply_migrations(con)
                self.assertTrue(target.is_file())
                self.assertIn("symbol_id", helpers.columns_of(con, "bars_daily"))
                self.assertEqual(schema_version(con), SCHEMA_VERSION)
            finally:
                con.close()

    def test_second_writer_is_rejected_with_clear_error(self) -> None:
        """P2.3 负向：第二个写连接（另起进程）必须被拒绝。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "wh.duckdb"
            writer = connect(read_only=False, path=target)
            try:
                apply_migrations(writer)
                code = textwrap.dedent(f"""
                    import duckdb
                    try:
                        duckdb.connect(r"{target}").execute("CREATE TABLE t(a INT)")
                        print("SECOND-WRITER-OK")
                    except duckdb.IOException:
                        print("SECOND-WRITER-REJECTED")
                """)
                proc = subprocess.run(
                    [sys.executable, "-c", code], capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    env={"PYTHONIOENCODING": "utf-8", "PATH": os.environ["PATH"]},
                )
                self.assertIn("SECOND-WRITER-REJECTED", proc.stdout,
                              f"第二个写连接未被拒绝: {proc.stdout!r} / {proc.stderr!r}")
            finally:
                writer.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
