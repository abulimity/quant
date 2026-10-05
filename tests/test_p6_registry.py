r"""P6.3 run registry 验收（LOCAL_DEPLOYMENT_PLAN.md §P6.3）。

    V3  `register_run` 任一必填字段缺失 → fail-closed（不写半截记录）
    V3  重复登记同一 run 幂等（`ON CONFLICT DO NOTHING`）
    V3  `register_metrics` 只收有限数值；非数值 / NaN / bool 直接拒绝
    V3  `get_run` / `get_metrics` 往返一致

运行：
    $env:PYTHONIOENCODING='utf-8'
    uv run --project D:\project\quant python -m unittest tests.test_p6_registry -v
"""

from __future__ import annotations

import unittest
from datetime import datetime

import numpy as np

from quantlab.registry.runs import (
    RunRegistryError,
    get_metrics,
    get_run,
    register_metrics,
    register_run,
)
from tests.helpers import migrated_memory_con, naive_utc


def _valid_run() -> dict:
    return dict(
        run_id="run-0001",
        spec_id="abcd1234efgh5678",
        engine="portfolio",
        origin="handwritten",
        created_at=naive_utc(2024, 1, 3, 12, 0, 0),
        data_snapshot_id="synth-A",
        env_lock_hash="hash-abc",
        git_sha="sha-123",
        params_json="{}",
        status="ok",
    )


class TestRegisterRunFailClosed(unittest.TestCase):
    def setUp(self) -> None:
        self.con = migrated_memory_con()

    def tearDown(self) -> None:
        self.con.close()

    def test_missing_field_raises(self) -> None:
        for missing in ("run_id", "spec_id", "engine", "origin", "data_snapshot_id",
                        "env_lock_hash", "git_sha", "status"):
            with self.subTest(missing=missing):
                args = _valid_run()
                args[missing] = ""
                with self.assertRaises(RunRegistryError):
                    register_run(self.con, **args)

    def test_invalid_status_raises(self) -> None:
        args = _valid_run()
        args["status"] = "bogus"
        with self.assertRaises(RunRegistryError):
            register_run(self.con, **args)

    def test_blank_run_id_is_not_written(self) -> None:
        args = _valid_run()
        args["run_id"] = "   "
        with self.assertRaises(RunRegistryError):
            register_run(self.con, **args)
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 0)


class TestRegisterRunRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.con = migrated_memory_con()
        register_run(self.con, **_valid_run())

    def tearDown(self) -> None:
        self.con.close()

    def test_get_run_round_trips(self) -> None:
        row = get_run(self.con, "run-0001")
        self.assertIsNotNone(row)
        self.assertEqual(row["spec_id"], "abcd1234efgh5678")
        self.assertEqual(row["status"], "ok")
        # created_at 存 naive UTC：读回无时区
        self.assertIsInstance(row["created_at"], datetime)
        self.assertIsNone(row["created_at"].tzinfo)

    def test_idempotent_re_registration(self) -> None:
        register_run(self.con, **_valid_run())
        register_run(self.con, **_valid_run())
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 1)

    def test_get_missing_run_returns_none(self) -> None:
        self.assertIsNone(get_run(self.con, "nope"))


class TestRegisterMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self.con = migrated_memory_con()
        register_run(self.con, **_valid_run())

    def tearDown(self) -> None:
        self.con.close()

    def test_metrics_round_trip(self) -> None:
        register_metrics(self.con, "run-0001", {
            "total_return": 0.1234,
            "sharpe_ratio": 1.5,
            "n_trades": 7,
        })
        metrics = get_metrics(self.con, "run-0001")
        self.assertAlmostEqual(metrics["total_return"], 0.1234)
        self.assertAlmostEqual(metrics["sharpe_ratio"], 1.5)
        self.assertEqual(metrics["n_trades"], 7.0)

    def test_non_numeric_rejected(self) -> None:
        with self.assertRaises(RunRegistryError):
            register_metrics(self.con, "run-0001", {"note": "口径说明不可入指标表"})

    def test_nan_rejected(self) -> None:
        with self.assertRaises(RunRegistryError):
            register_metrics(self.con, "run-0001", {"total_return": float("nan")})

    def test_bool_rejected(self) -> None:
        with self.assertRaises(RunRegistryError):
            register_metrics(self.con, "run-0001", {"passed": True})

    def test_replace_overwrites_same_run_metric(self) -> None:
        register_metrics(self.con, "run-0001", {"total_return": 0.1})
        register_metrics(self.con, "run-0001", {"total_return": 0.2})
        metrics = get_metrics(self.con, "run-0001")
        self.assertAlmostEqual(metrics["total_return"], 0.2)
        self.assertEqual(len(metrics), 1)

    def test_numpy_scalars_accepted(self) -> None:
        register_metrics(self.con, "run-0001", {
            "a": np.float64(1.25), "b": np.int64(3),
        })
        metrics = get_metrics(self.con, "run-0001")
        self.assertAlmostEqual(metrics["a"], 1.25)
        self.assertEqual(metrics["b"], 3.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
