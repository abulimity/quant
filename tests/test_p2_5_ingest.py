"""P2.5 Ingest 编排与快照验收（LOCAL_DEPLOYMENT_PLAN.md §P2.5）。

覆盖：
    V2  `quantlab ingest --source synthetic --universe fixture` 全流程跑通并登记
    V3  **中断恢复**：写一半杀进程，已有快照**未被破坏**（原子替换生效）
    V3  重复 ingest **幂等**：不重复行、旧快照保留、快照 ID 稳定
    V3  修正数据时**新建快照**而非覆盖（负向断言：旧文件哈希不变）
    另：不变量不通过时拒绝落盘；`ingest_runs` 三态生命周期

运行：
    python -m unittest discover -t . -s tests -v
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from quantlab import cli
from quantlab.fixtures import spec as S
from quantlab.fixtures.synth import generate, write_snapshot
from quantlab.ingest.orchestrator import IngestError, ingest, ingest_bundle
from quantlab.store.db import connect
from quantlab.store.migrate import apply_migrations


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dir_hashes(directory: Path) -> dict[str, str]:
    return {p.name: _sha256(p) for p in sorted(directory.glob("*.parquet"))}


def _ledger(warehouse: Path) -> tuple[int, list[str]]:
    """返回 (ingest_runs 行数, 去重后的状态列表)。"""
    con = connect(read_only=True, path=warehouse)
    try:
        rows = con.execute("SELECT count(*) FROM ingest_runs").fetchone()[0]
        status = sorted({r[0] for r in con.execute("SELECT status FROM ingest_runs").fetchall()})
        return int(rows), status
    finally:
        con.close()


class _Scratch(unittest.TestCase):
    """每个用例一个临时快照根 + 独立台账。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "synthetic"
        self.warehouse = Path(self.tmp.name) / "warehouse.duckdb"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _snapshot_dirs(self) -> list[Path]:
        """已落盘的快照目录。根目录**不存在**也算「没有快照」（那正是失败路径的正常结果）。"""
        return [d for d in self.root.iterdir() if d.is_dir()] if self.root.is_dir() else []

    def _subprocess(self, code: str) -> subprocess.CompletedProcess:
        env = {"PYTHONIOENCODING": "utf-8", "PATH": os.environ["PATH"]}
        return subprocess.run([sys.executable, "-c", textwrap.dedent(code)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env)


class TestEndToEndIngest(_Scratch):
    """V2：全流程跑通并登记。"""

    def test_ingest_writes_snapshot_and_registers(self) -> None:
        result = ingest("synthetic", self.root, warehouse=self.warehouse)
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.path.is_dir())
        self.assertFalse(result.already_present)

        for name in ("symbols", "bars_daily", "fx_rates", "macro_series",
                     "trading_calendar", "fundamentals", "corporate_actions"):
            self.assertTrue((result.path / f"{name}.parquet").is_file(), f"缺 {name}.parquet")

        rows, status = _ledger(self.warehouse)
        self.assertGreaterEqual(rows, 7, "每个数据集应有一行登记")
        self.assertEqual(status, ["ok"])

        con = connect(read_only=True, path=self.warehouse)
        try:
            watermark = con.execute(
                "SELECT watermark FROM ingest_runs WHERE dataset = 'bars_daily'").fetchone()[0]
            self.assertIsNotNone(watermark)
            self.assertEqual(
                con.execute("SELECT rows FROM ingest_runs WHERE dataset = 'bars_daily'")
                .fetchone()[0], result.rows["bars_daily"])
        finally:
            con.close()

    def test_cli_ingest_runs_end_to_end(self) -> None:
        """V2 原文命令：`quantlab ingest --source synthetic --universe fixture`。"""
        code = cli.main(["ingest", "--source", "synthetic", "--universe", "fixture",
                         "--out", str(self.root), "--warehouse", str(self.warehouse)])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / S.snapshot_id()).is_dir())

    def test_cli_rejects_unknown_source(self) -> None:
        code = cli.main(["ingest", "--source", "akshare", "--out", str(self.root),
                         "--warehouse", str(self.warehouse)])
        self.assertEqual(code, 1, "未接入的数据源必须失败，不得静默成功")

    def test_ingest_marks_running_before_writing(self) -> None:
        """登记**先于**写入 —— 否则硬杀时无从判断「跑到哪了」。"""
        con = connect(read_only=False, path=self.warehouse)
        try:
            apply_migrations(con)
        finally:
            con.close()

        import quantlab.ingest.orchestrator as orch

        original = orch.write_snapshot
        seen: dict[str, list[str]] = {}

        def spy(bundle, root, *a, **kw):
            # 必须与 ingest 持有的连接**同配置**（同为可写）：DuckDB 同进程内
            # 不允许对同一库文件同时持有配置不同的连接。
            c = connect(read_only=False, path=self.warehouse)
            try:
                seen["statuses"] = sorted({r[0] for r in c.execute(
                    "SELECT status FROM ingest_runs").fetchall()})
            finally:
                c.close()
            return original(bundle, root, *a, **kw)

        orch.write_snapshot = spy
        try:
            ingest("synthetic", self.root, warehouse=self.warehouse)
        finally:
            orch.write_snapshot = original

        self.assertEqual(seen["statuses"], ["running"],
                         "写入开始时台账里应只有 running（尚未标 ok）")


class TestIdempotence(_Scratch):
    """V3：重复 ingest 幂等。"""

    def test_second_ingest_is_idempotent(self) -> None:
        first = ingest("synthetic", self.root, warehouse=self.warehouse)
        before_files = _dir_hashes(first.path)
        rows_before, _ = _ledger(self.warehouse)

        second = ingest("synthetic", self.root, warehouse=self.warehouse)

        self.assertTrue(second.already_present)
        self.assertEqual(second.status, "exists")
        self.assertEqual(second.snapshot_id, first.snapshot_id)
        self.assertEqual(_dir_hashes(first.path), before_files, "重复 ingest 改动了快照内容")

        rows_after, _ = _ledger(self.warehouse)
        self.assertEqual(rows_after, rows_before, "重复 ingest 产生了重复登记行")

    def test_only_one_snapshot_directory_exists(self) -> None:
        ingest("synthetic", self.root, warehouse=self.warehouse)
        ingest("synthetic", self.root, warehouse=self.warehouse)
        dirs = [d for d in self.root.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1, f"出现了多个快照目录: {dirs}")


class TestImmutability(_Scratch):
    """V3：旧快照不可覆盖；修正数据须新建快照。"""

    def test_amending_data_creates_a_new_snapshot_not_overwrite(self) -> None:
        """改内容 → 内容哈希变 → 快照 ID 变 → 旧快照**逐字节不变**。"""
        bundle = generate()
        write_snapshot(bundle, self.root)
        old_dir = self.root / bundle.snapshot_id
        old_hashes = _dir_hashes(old_dir)

        amended = generate()
        amended.tables["bars_daily"] = amended.tables["bars_daily"].iloc[:-1].reset_index(drop=True)
        amended.snapshot_id = S.snapshot_id() + "-amended"
        write_snapshot(amended, self.root)

        self.assertTrue(old_dir.is_dir())
        self.assertEqual(_dir_hashes(old_dir), old_hashes, "旧快照被覆盖了")
        new_dir = self.root / amended.snapshot_id
        self.assertTrue(new_dir.is_dir())
        self.assertNotEqual(_sha256(new_dir / "bars_daily.parquet"),
                            old_hashes["bars_daily.parquet"])

    def test_write_snapshot_refuses_to_overwrite_in_place(self) -> None:
        from quantlab.store.atomic import SnapshotExistsError

        bundle = generate()
        write_snapshot(bundle, self.root)
        with self.assertRaises(SnapshotExistsError):
            write_snapshot(bundle, self.root)


class TestInterruptionRecovery(_Scratch):
    """V3：写入中途被杀 → 已有快照未被破坏，无半份快照残留。"""

    def test_kill_during_write_leaves_previous_snapshot_intact(self) -> None:
        """先正常落一份快照；再在**另一次**写入中途硬杀进程，验证前者未被破坏。"""
        good = generate()
        write_snapshot(good, self.root)
        good_dir = self.root / good.snapshot_id
        good_hashes = _dir_hashes(good_dir)

        other_id = S.snapshot_id() + "-interrupted"
        child = f"""
            import os
            from pathlib import Path
            from quantlab.fixtures.synth import generate
            from quantlab.store.atomic import staging_dir, atomic_write_parquet

            bundle = generate()
            root = Path(r"{self.root}")
            with staging_dir(root) as (staging, commit):
                for name, df in bundle.tables.items():
                    atomic_write_parquet(df, staging / f"{{name}}.parquet")
                # 关键：**硬杀**，绕过 finally 清理，模拟断电 / 强杀
                os._exit(9)
            commit(r"{other_id}")
        """
        proc = self._subprocess(child)
        self.assertEqual(proc.returncode, 9, f"子进程未被硬杀: {proc.stderr}")

        self.assertTrue(good_dir.is_dir())
        self.assertEqual(_dir_hashes(good_dir), good_hashes, "中断破坏了已有快照")
        self.assertFalse((self.root / other_id).exists(), "产生了半份快照")
        # 硬杀会留下 .staging-* 残留（预期）：它可清理，且不影响任何已发布快照
        self.assertTrue(list(self.root.glob(".staging-*")),
                        "预期硬杀会留下暂存残留（用于诊断）")

    def test_prune_staging_cleans_leftovers(self) -> None:
        from quantlab.store.atomic import prune_staging

        (self.root / ".staging-999-deadbeef").mkdir(parents=True)
        (self.root / ".staging-999-deadbeef" / "junk.parquet").write_bytes(b"x")
        self.assertEqual(prune_staging(self.root), 1)
        self.assertEqual(list(self.root.glob(".staging-*")), [])

    def test_python_exception_marks_aborted_and_leaves_no_snapshot(self) -> None:
        """Python 异常（非硬杀）→ 台账标 aborted，且不留下快照。"""
        import quantlab.ingest.orchestrator as orch

        original = orch.write_snapshot

        def boom(bundle, root, *a, **kw):
            raise RuntimeError("simulated vendor failure")

        orch.write_snapshot = boom
        try:
            with self.assertRaises(RuntimeError):
                ingest("synthetic", self.root, warehouse=self.warehouse)
        finally:
            orch.write_snapshot = original

        rows, status = _ledger(self.warehouse)
        self.assertGreater(rows, 0)
        self.assertEqual(status, ["aborted"], "异常后状态应标为 aborted")
        self.assertEqual(self._snapshot_dirs(), [], "失败路径不应留下快照")


class TestFailClosed(_Scratch):
    """不变量不通过 / 未知源 → 拒绝落盘并显式报错。"""

    def test_invariant_violation_refuses_to_write(self) -> None:
        bundle = generate()
        bars = bundle.tables["bars_daily"].copy()
        bars.loc[bars.index[0], "close"] = -1.0        # 注入非正价格
        bundle.tables["bars_daily"] = bars

        with self.assertRaises(IngestError) as ctx:
            ingest_bundle(bundle, self.root, warehouse=self.warehouse)
        self.assertIn("不变量", str(ctx.exception))
        self.assertEqual(self._snapshot_dirs(), [], "不变量失败时不应产生快照")

    def test_unknown_universe_rejected(self) -> None:
        with self.assertRaises(IngestError):
            ingest("synthetic", self.root, warehouse=self.warehouse, universe="sp500")


if __name__ == "__main__":
    unittest.main(verbosity=2)
