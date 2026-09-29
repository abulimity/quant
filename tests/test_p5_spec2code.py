"""P5.4 策略代码校验（经桥调 x2 环境的 `spec2code.validate_code`）验收。

⚠️ **与手册 §P5.4 的措辞不符，已留证**：
    手册说「调用 x2strategy 的 spec2code **生成** backtrader 策略类」，
    但实测 `spec2code` 包里**没有生成器** —— 其全部函数只有
    `get_backtest_timeout` / `get_data_cache_dir` / **`validate_code`**。
    生成不发生在包里（那是作者侧的 agent 流程）。
    经人工确认：**只做 validator 集成，不做生成**。

覆盖：
    好代码 → `is_valid=True`
    坏代码 → `is_valid=False` 且**给出可定位的错误**（含行号）
    缺参数 → 明确失败（不静默）

**需要 envs/x2 环境**（走 bridge 子进程），故比纯单测慢（每次 ~10s）。
运行：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -m unittest tests.test_p5_spec2code -v
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path

from quantlab.engines.bridge import BridgeError, run_in_env

X2_ENTRY = "envs/x2/entry.py"

GOOD_CODE = textwrap.dedent("""
    import backtrader as bt


    class SmaCross(bt.Strategy):
        params = (("fast", 20), ("slow", 60))

        def __init__(self):
            self.fast = bt.ind.SMA(period=self.p.fast)
            self.slow = bt.ind.SMA(period=self.p.slow)

        def next(self):
            if not self.position and self.fast[0] > self.slow[0]:
                self.buy()
""").strip()

BAD_CODE = textwrap.dedent("""
    import backtrader as bt

    class SmaCross(bt.Strategy)
        def next(self)
            pass
""").strip()


class _BridgeCase(unittest.TestCase):
    def validate(self, code: str) -> dict:
        workdir = Path(tempfile.mkdtemp()) / f"job-{uuid.uuid4().hex[:8]}"
        return run_in_env(
            "x2", X2_ENTRY,
            {"job_id": f"p54-{uuid.uuid4().hex[:8]}", "engine": "x2",
             "params": {"op": "validate_code", "code": code}},
            workdir=workdir)


class TestValidateCode(_BridgeCase):
    def test_good_code_is_valid(self) -> None:
        result = self.validate(GOOD_CODE)
        self.assertTrue(result["is_valid"], f"好代码被判无效: {result['errors']}")
        self.assertEqual(result["n_errors"], 0)

    def test_bad_code_is_invalid_with_a_locatable_error(self) -> None:
        """坏代码必须**被抓住**，且错误信息能定位（含行号）。"""
        result = self.validate(BAD_CODE)
        self.assertFalse(result["is_valid"], "坏代码竟然通过了校验")
        self.assertGreater(result["n_errors"], 0)
        joined = " ".join(result["errors"])
        self.assertIn("SyntaxError", joined)
        self.assertRegex(joined, r"line \d+", "错误信息没有行号，无从定位")

    def test_validator_identity_is_reported(self) -> None:
        """产出里要写明用的是哪个校验器（可追溯）。"""
        self.assertEqual(self.validate(GOOD_CODE)["validator"],
                         "spec2code.validator.validate_code")

    def test_missing_code_argument_fails_loudly(self) -> None:
        """缺参数必须**明确失败**，而不是返回一个「看起来通过」的结果。"""
        with self.assertRaises(BridgeError):
            run_in_env("x2", X2_ENTRY,
                       {"job_id": f"p54-none-{uuid.uuid4().hex[:8]}", "engine": "x2",
                        "params": {"op": "validate_code"}},
                       workdir=Path(tempfile.mkdtemp()) / "job")


if __name__ == "__main__":
    unittest.main(verbosity=2)
