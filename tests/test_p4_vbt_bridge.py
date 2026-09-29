"""P4.4 vectorbt Runner（粗筛，经桥隔离调用）验收。

覆盖：
    V1  `uv run --project envs/vbt python envs/vbt/probe.py` 通过
    V2  经桥完成一次小规模参数扫描，结果回传为 Parquet
    V3  未加 spawn 保护时能**复现失败**，加上后通过（证明该保护必要，避免未来误删）
    V3  扫描结果标注「仅粗筛，未建模撮合细节」，不得当作成交模型正确的证据

**隔离纪律**：vectorbt 只在 `envs/vbt` 里跑，core **不 import** 它；
一切经 `bridge.run_in_env("vbt", ...)` 走 subprocess + job.json + Parquet。

运行：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -m unittest tests.test_p4_vbt_bridge -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from quantlab.engines.base import PROJECT_ROOT, load_bundle_from_fixture
from quantlab.engines.bridge import BridgeError, run_in_env

VBT_ENTRY = "envs/vbt/entry.py"
VBT_PROBE = "envs/vbt/probe.py"
SPAWN_DEMO = "envs/vbt/_spawn_guard_demo.py"

# 扫描样本长度：够短以保证测试快，够长以产生均线交叉
SCAN_SESSIONS = 260


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV")}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_in_vbt(script: str, *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
    """在 **envs/vbt** 环境里跑一个脚本（探针 / spawn 演示）。"""
    return subprocess.run(
        ["uv", "run", "--project", "envs/vbt", "python", script, *args],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=_clean_env(), timeout=timeout)


class TestVectorbtProbe(unittest.TestCase):
    """V1：隔离环境探针通过。"""

    def test_vbt_probe_passes(self) -> None:
        proc = _run_in_vbt(VBT_PROBE)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("import_ok", proc.stdout)


class TestVectorbtScanViaBridge(unittest.TestCase):
    """V2：经桥完成一次小规模参数扫描，结果回传为 Parquet。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_bundle_from_fixture()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _write_prices(self) -> Path:
        prices = self.bundle.closes[1].dropna().iloc[:SCAN_SESSIONS]
        path = self.root / "prices.parquet"
        pd.DataFrame({"close": prices.to_numpy()}, index=prices.index).to_parquet(path)
        return path

    def _scan(self) -> tuple[dict, Path]:
        workdir = self.root / "job"
        result = run_in_env(
            "vbt", VBT_ENTRY,
            {"job_id": "p44-scan", "engine": "vectorbt",
             "params": {"op": "scan", "fast": [5, 10], "slow": [20, 40]}},
            inputs={"prices": self._write_prices()},
            workdir=workdir)
        return result, workdir / "result.parquet"

    def test_scan_runs_through_the_bridge(self) -> None:
        result, _ = self._scan()
        self.assertEqual(result["op"], "scan")
        self.assertGreater(result["n_combos"], 0, "扫描没有产生任何参数组合")
        self.assertEqual(result["n_sessions"], SCAN_SESSIONS)

    def test_scan_results_come_back_as_parquet(self) -> None:
        result, parquet = self._scan()
        self.assertTrue(parquet.is_file(), "未产出 result.parquet")
        frame = pd.read_parquet(parquet)
        self.assertEqual(len(frame), result["n_combos"])
        for column in ("fast", "slow", "total_return", "n_trades", "max_drawdown"):
            self.assertIn(column, frame.columns)

    def test_scan_is_labelled_as_coarse_screen_only(self) -> None:
        """V3：必须标注「仅粗筛，未建模撮合细节」，防止被当成成交模型正确的证据。"""
        result, _ = self._scan()
        self.assertEqual(result["match_quality"], "coarse_screen_only")
        self.assertIn("未建模", result["note"])
        self.assertIn("不得作为成交模型正确的证据", result["note"])

    def test_job_metadata_records_the_env_lock(self) -> None:
        _, parquet = self._scan()
        job = json.loads((parquet.parent / "job.json").read_text(encoding="utf-8"))
        self.assertTrue(job["env_lock_sha256"])
        self.assertEqual(job["env"], "vbt")


class TestBridgeFailurePropagation(unittest.TestCase):
    """失败必须传播，不得静默吞掉（P1.5 起的一贯要求）。"""

    def test_scan_without_prices_input_fails_visibly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BridgeError):
                run_in_env("vbt", VBT_ENTRY,
                           {"job_id": "p44-noinput", "engine": "vectorbt",
                            "params": {"op": "scan"}},
                           workdir=Path(tmp) / "job")


class TestSpawnGuard(unittest.TestCase):
    """§P4.4 V3：spawn 保护**必要性的可执行证据**。

    把「为什么必须有 `if __name__ == "__main__":`」变成**会打的断言**，
    而不是注释里的叮嘱 —— 后人误删保护时会立刻红。
    """

    def test_guarded_entry_runs_fine(self) -> None:
        proc = _run_in_vbt(SPAWN_DEMO, "--guarded")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("RESULT 42", proc.stdout)

    def test_unguarded_entry_reproduces_the_failure(self) -> None:
        """未加保护时**可复现地失败** —— 证明该保护确实必需。"""
        proc = _run_in_vbt(SPAWN_DEMO, "--unguarded")
        self.assertNotEqual(proc.returncode, 0,
                            "未加 __main__ 保护竟然也成功了 —— 该演示不再有效，"
                            "不能用来证明保护的必要性")
        self.assertNotIn("UNGUARDED-SHOULD-NOT-PRINT", proc.stdout)
        combined = (proc.stdout or "") + (proc.stderr or "")
        self.assertTrue(
            any(token in combined for token in
                ("spawn", "RuntimeError", "_check_not_importing_main", "Traceback")),
            f"失败原因不像 spawn 递归导入：{combined[-1500:]}")


class TestIsolationIsPreserved(unittest.TestCase):
    """§P4.4：vectorbt 只在隔离环境里；core 侧不得 import 它。"""

    def test_core_does_not_import_vectorbt(self) -> None:
        self.assertNotIn("vectorbt", sys.modules,
                         "core 进程竟然加载了 vectorbt —— 环境隔离被破坏")

    def test_core_does_not_import_numba_either(self) -> None:
        """numba 是 vectorbt 的传递依赖；出现在 core 里通常意味着隔离被破坏。"""
        self.assertNotIn("numba", sys.modules)

    def test_vectorbt_is_not_a_registered_core_runner(self) -> None:
        from quantlab.engines.base import available_engines, register_builtin

        register_builtin()
        self.assertNotIn("vectorbt", available_engines(),
                         "vectorbt 只能经桥调用，不应注册为 core 侧 runner")


if __name__ == "__main__":
    unittest.main(verbosity=2)
