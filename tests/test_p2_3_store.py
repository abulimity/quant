"""P2.3 DuckDB 仓库与只读约定验收（LOCAL_DEPLOYMENT_PLAN.md §P2.3）。

覆盖：
    V1  只读连接可查询夹具数据；`read_parquet` 视图可用
    V3  只读连接执行写操作**必须报错**（负向测试）
    V3  单写多读：第二个写连接被拒绝并有**明确报错**，不得静默损坏
    V3  只读**读**者在**写者持有**期间的行为（本机实测：被拒绝）—— 如实断言并留证

**本机实测前提**（见 EVIDENCE.md §P2.1「DuckDB 并发语义」）：
    Windows 上 DuckDB 文件锁是**独占**的 —— 写者持有期间，其他进程**连只读也打不开**；
    且**同一进程**内两次 `connect()` 返回同一个 DB 实例，故并发测试**必须另起进程**。
    因此「单写多读」的准确表述是：**无活跃写者时**，多读者可并发。

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
from pathlib import Path

import duckdb

from quantlab.fixtures.synth import generate, write_snapshot
from quantlab.store.db import WarehouseNotFoundError, connect
from quantlab.store.migrate import apply_migrations
from quantlab.store.warehouse import (
    all_snapshots_glob,
    load_snapshot,
    query_parquet_view,
    register_snapshot_views,
)

_TMP = tempfile.TemporaryDirectory()
ROOT = Path(_TMP.name) / "synthetic"


def setUpModule() -> None:      # noqa: N802 - unittest 约定
    """生成一次夹具快照，供本模块全部用例复用（约 2 秒）。"""
    global BUNDLE
    BUNDLE = generate()
    write_snapshot(BUNDLE, ROOT)


def tearDownModule() -> None:   # noqa: N802
    _TMP.cleanup()


def _subprocess(code: str) -> subprocess.CompletedProcess:
    env = {"PYTHONIOENCODING": "utf-8", "PATH": os.environ["PATH"]}
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def _built_warehouse(directory: str) -> Path:
    wh = Path(directory) / "warehouse.duckdb"
    con = connect(read_only=False, path=wh)
    try:
        load_snapshot(con, ROOT, BUNDLE.snapshot_id)
    finally:
        con.close()
    return wh


class TestReadOnlyQueries(unittest.TestCase):
    """V1：只读连接可查询夹具数据；Parquet 视图可用。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = tempfile.TemporaryDirectory()
        cls.wh = _built_warehouse(cls.dir.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.dir.cleanup()

    def test_read_only_connection_can_query_fixture_data(self) -> None:
        con = connect(read_only=True, path=self.wh)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM bars_daily").fetchone()[0],
                             int(len(BUNDLE.tables["bars_daily"])))
            close = con.execute(
                "SELECT close FROM bars_daily WHERE symbol_id = 1 ORDER BY ts LIMIT 1"
            ).fetchone()[0]
            self.assertGreater(close, 0)
        finally:
            con.close()

    def test_parquet_glob_view_is_usable(self) -> None:
        """V1：`read_parquet('data/bronze/synthetic/**/*.parquet')` 视图可用。"""
        con = duckdb.connect(":memory:")
        try:
            rows = query_parquet_view(
                con, all_snapshots_glob(ROOT),
                "SELECT count(*) FROM _pq WHERE filename LIKE '%bars_daily%'")
            self.assertEqual(rows[0][0], int(len(BUNDLE.tables["bars_daily"])))
        finally:
            con.close()

    def test_single_snapshot_view_is_isolated_to_one_snapshot(self) -> None:
        con = duckdb.connect(":memory:")
        try:
            register_snapshot_views(con, ROOT, BUNDLE.snapshot_id)
            snaps = {r[0] for r in con.execute(
                "SELECT DISTINCT snapshot_id FROM raw_bars_daily").fetchall()}
            self.assertEqual(snaps, {BUNDLE.snapshot_id}, "单快照视图跨了快照")
        finally:
            con.close()


class TestReadOnlyRejectsWrites(unittest.TestCase):
    """V3：只读连接执行写操作**必须报错**。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = tempfile.TemporaryDirectory()
        cls.wh = _built_warehouse(cls.dir.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.dir.cleanup()

    def _ro(self):
        return connect(read_only=True, path=self.wh)

    def test_create_table_rejected(self) -> None:
        con = self._ro()
        try:
            with self.assertRaises(Exception) as ctx:
                con.execute("CREATE TABLE nope(a INT)")
            self.assertIn("read", str(ctx.exception).lower())
        finally:
            con.close()

    def test_insert_rejected_and_data_unchanged(self) -> None:
        con = self._ro()
        try:
            with self.assertRaises(Exception):
                con.execute(
                    "INSERT INTO symbols VALUES (999,'X','XSHG','XSHG','CNY',NULL,1,NULL,NULL,NULL,NULL)"
                )
            self.assertEqual(
                con.execute("SELECT count(*) FROM symbols WHERE symbol_id = 999").fetchone()[0],
                0, "只读连接竟然写入了数据")
        finally:
            con.close()

    def test_drop_rejected_and_data_unchanged(self) -> None:
        con = self._ro()
        try:
            with self.assertRaises(Exception):
                con.execute("DROP TABLE bars_daily")
            self.assertEqual(con.execute("SELECT count(*) FROM bars_daily").fetchone()[0],
                             int(len(BUNDLE.tables["bars_daily"])))
        finally:
            con.close()

    def test_missing_warehouse_read_only_is_an_error_not_an_empty_db(self) -> None:
        """把「路径写错」当成「空库」是最容易造成静默错误的一类事故。"""
        with self.assertRaises(WarehouseNotFoundError):
            connect(read_only=True, path=Path(self.dir.name) / "does_not_exist.duckdb")


class TestSingleWriterSemantics(unittest.TestCase):
    """V3：单写多读 —— 用**独立进程**验证（同进程共享 DB 实例，测不出并发）。"""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.wh = Path(self.dir.name) / "warehouse.duckdb"
        self.writer = connect(read_only=False, path=self.wh)
        apply_migrations(self.writer)

    def tearDown(self) -> None:
        try:
            if self.writer is not None:
                self.writer.close()
        finally:
            self.dir.cleanup()

    def _close_writer(self) -> None:
        """显式关闭并置空 —— 谨慎起见防重复关闭。"""
        self.writer.close()
        self.writer = None

    def test_second_writer_process_is_rejected(self) -> None:
        proc = _subprocess(f"""
            import duckdb
            try:
                duckdb.connect(r"{self.wh}").execute("CREATE TABLE t(a INT)")
                print("SECOND-WRITER-OK")
            except duckdb.IOException as exc:
                print("SECOND-WRITER-REJECTED", type(exc).__name__)
        """)
        self.assertIn("SECOND-WRITER-REJECTED", proc.stdout,
                      f"第二个写进程未被拒绝: {proc.stdout!r} {proc.stderr!r}")

    def test_second_writer_through_our_connect_gives_actionable_error(self) -> None:
        """经 `db.connect()` 打开时，应翻译为带处置建议的 `WarehouseBusyError`。"""
        proc = _subprocess(f"""
            from pathlib import Path
            from quantlab.store.db import connect, WarehouseBusyError
            try:
                connect(read_only=False, path=Path(r"{self.wh}"))
                print("UNEXPECTED-OK")
            except WarehouseBusyError as exc:
                text = str(exc)
                print("BUSY-OK", "不要靠重试掩盖" in text and "独占" in text)
        """)
        self.assertIn("BUSY-OK True", proc.stdout,
                      f"未给出可操作的明确报错: {proc.stdout!r} {proc.stderr!r}")

    def test_reader_is_also_blocked_while_writer_holds_the_file(self) -> None:
        """**如实断言本机行为**：Windows 上写者持有时，只读**也**打不开。

        这不是缺陷，而是「Parquet 是真相、DuckDB 是查询层」的实现理由：
        查询层应在**无写者**时发布/读取。把该事实固化成测试，可防止后人
        误以为「可以边写边读」而把架构建立在错误前提上。
        """
        proc = _subprocess(f"""
            import duckdb
            try:
                con = duckdb.connect(r"{self.wh}", read_only=True)
                print("READER-OK-rows", con.execute("SELECT count(*) FROM symbols").fetchone()[0])
            except duckdb.IOException:
                print("READER-BLOCKED-BY-WRITER")
        """)
        self.assertIn("READER-BLOCKED-BY-WRITER", proc.stdout,
                      "本机行为与 EVIDENCE.md 记录的『写者独占』不符，需重新核实并更新证据")

    def test_readers_can_run_concurrently_once_writer_closes(self) -> None:
        """单写多读的**准确**表述：无活跃写者时，多读者可并发。

        注意：必须用 `self.writer` 写入后**真的关掉它**再起子进程 ——
        同进程内即便另开一个连接，文件锁仍由**本进程**持有（见 EVIDENCE.md
        「同进程两次 connect 返回同一个 DB 实例」），子进程照样会被拒绝。
        """
        self.writer.execute(
            "INSERT INTO symbols VALUES (1,'X','XSHG','XSHG','CNY',NULL,1,NULL,NULL,NULL,NULL)")
        self._close_writer()

        for proc in [_subprocess(f"""
            import duckdb
            con = duckdb.connect(r"{self.wh}", read_only=True)
            print("ROWS", con.execute("SELECT count(*) FROM symbols").fetchone()[0])
        """) for _ in range(2)]:
            self.assertIn("ROWS 1", proc.stdout, proc.stderr)


class TestSnapshotMaterialisation(unittest.TestCase):
    """物化装载的幂等与不可重复语义。"""

    def test_reloading_same_snapshot_is_rejected_by_primary_key(self) -> None:
        """快照不可变 → 重复装载应被主键拒绝，而不是静默翻倍。"""
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(read_only=False, path=Path(tmp) / "w.duckdb")
            try:
                first = load_snapshot(con, ROOT, BUNDLE.snapshot_id)
                self.assertEqual(first["bars_daily"], int(len(BUNDLE.tables["bars_daily"])))
                with self.assertRaises(duckdb.ConstraintException):
                    load_snapshot(con, ROOT, BUNDLE.snapshot_id)
                self.assertEqual(con.execute("SELECT count(*) FROM bars_daily").fetchone()[0],
                                 int(len(BUNDLE.tables["bars_daily"])), "重复装载改动了数据")
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
