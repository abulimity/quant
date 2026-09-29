"""P2.2 合成夹具验收（LOCAL_DEPLOYMENT_PLAN.md §P2.2）。

覆盖：
    V4  同一脚本 + 同一种子跑两次，**数据内容哈希**完全一致（且跨进程一致）
    V3  夹具不变量：正价格、OHLC 关系、无重复键、停牌掩码、汇率方向为正
    V3  buy&hold 解析净值与「从夹具价格 + 公司行动重算」在 1e-9 内一致
    V3  停牌区间内不可成交
    另：七类场景齐备；快照写入/读回；快照不可覆盖；失败缺口不可复现（语义断言）

生成一次约 2 秒，故在 setUpClass 里生成一次共用。

运行：
    python -m unittest discover -t . -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.fixtures import spec as S
from quantlab.fixtures.synth import (
    FixtureBundle,
    check_invariants,
    generate,
    read_snapshot,
    write_snapshot,
)
from quantlab.store.atomic import SnapshotExistsError

# 带下载失败窗的标的；其解析答案**不应**能从观测 bars 复现
GAP_SYMBOL = S.FAILURES[0].symbol_id
GAPLESS_SYMBOLS = [s.symbol_id for s in S.SYMBOLS if s.symbol_id != GAP_SYMBOL]


def recompute_nav_from_bars(bundle: FixtureBundle, symbol_id: int) -> np.ndarray:
    """**独立**用「原始价格 + 公司行动」重算总收益净值（不复用生成器内部量）。

    逐日：nav *= (close_t · r_t + d_t) / close_{t-1}，nav_0 = 1。
    停牌日 close 沿用、r=1/d=0 → 该日收益恰为 1，即停牌不产生收益。
    """
    bars = bundle.tables["bars_daily"]
    bars = bars[bars["symbol_id"] == symbol_id].sort_values("ts").reset_index(drop=True)
    actions = bundle.tables["corporate_actions"]
    actions = actions[actions["symbol_id"] == symbol_id]
    ratio = {r.ex_date: (r.ratio or 1.0) for r in actions.itertuples() if r.kind == "split"}
    cash = {r.ex_date: (r.cash or 0.0) for r in actions.itertuples() if r.kind == "dividend"}

    nav = np.empty(len(bars), dtype="float64")
    acc, prev = 1.0, None
    for i, row in enumerate(bars.itertuples()):
        if prev is not None:
            acc *= (row.close * ratio.get(row.ts, 1.0) + cash.get(row.ts, 0.0)) / prev
        nav[i] = acc
        prev = row.close
    return nav


class TestDeterminism(unittest.TestCase):
    """V4：可重复生成，内容哈希稳定。"""

    def test_same_seed_same_content_hash(self) -> None:
        a, b = generate(), generate()
        self.assertEqual(a.snapshot_id, b.snapshot_id)
        self.assertEqual(a.content_hashes(), b.content_hashes())
        self.assertEqual(a.combined_content_hash(), b.combined_content_hash())

    def test_hash_stable_across_processes(self) -> None:
        """跨进程一致 —— 抓「误用内建 hash()（受 PYTHONHASHSEED 影响）」这类坑。"""
        code = textwrap.dedent("""
            import json
            from quantlab.fixtures.synth import generate
            b = generate()
            print(json.dumps({"id": b.snapshot_id, "combined": b.combined_content_hash()}))
        """)
        outs = []
        for seed in ("0", "12345"):
            env = {"PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": seed,
                   "PATH": os.environ["PATH"]}
            proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outs.append(json.loads(proc.stdout))
        self.assertEqual(outs[0], outs[1], "不同 PYTHONHASHSEED 下内容哈希应完全一致")

    def test_content_hash_is_not_parquet_byte_hash(self) -> None:
        """V4 明确要求：断言对象是**规范化内容**，不是 Parquet 字节。

        做法：打乱行序后重新计算 —— 内容哈希不变（字节流则必然改变）。
        """
        from quantlab.store.canonical import content_hash

        bars = generate().tables["bars_daily"]
        shuffled = bars.sample(frac=1.0, random_state=7).reset_index(drop=True)
        self.assertEqual(content_hash(bars, ["symbol_id", "ts", "snapshot_id"]),
                         content_hash(shuffled, ["symbol_id", "ts", "snapshot_id"]))


class TestInvariants(unittest.TestCase):
    """V3：夹具不变量（自带断言，可被 P2.6 复用）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = generate()

    def test_all_invariants_pass(self) -> None:
        self.assertEqual(check_invariants(self.bundle), [])

    def test_prices_are_positive(self) -> None:
        bars = self.bundle.tables["bars_daily"]
        for col in ("open", "high", "low", "close"):
            self.assertTrue((bars[col] > 0).all(), f"{col} 出现非正价格")

    def test_ohlc_relations(self) -> None:
        bars = self.bundle.tables["bars_daily"]
        self.assertTrue((bars["low"] <= bars[["open", "close"]].min(axis=1)).all())
        self.assertTrue((bars["high"] >= bars[["open", "close"]].max(axis=1)).all())

    def test_no_duplicate_keys(self) -> None:
        bars = self.bundle.tables["bars_daily"]
        self.assertEqual(int(bars.duplicated(["symbol_id", "ts", "snapshot_id"]).sum()), 0)

    def test_fx_rates_positive_and_derived_pair_consistent(self) -> None:
        fx = self.bundle.tables["fx_rates"]
        self.assertTrue((fx["rate"] > 0).all())
        piv = fx.pivot_table(index="ts", columns=["base", "quote"], values="rate")
        lhs = piv[("HKD", "CNY")].dropna()
        rhs = (piv[("USD", "CNY")] / piv[("USD", "HKD")]).dropna()
        np.testing.assert_allclose(lhs.to_numpy(), rhs.to_numpy(), rtol=1e-12)


class TestKnownAnswer(unittest.TestCase):
    """V3：解析净值 == 从夹具数据重算（本阶段「已知答案」的核心）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = generate()

    def test_analytic_nav_matches_recomputation_for_all_gapless_symbols(self) -> None:
        for sid in GAPLESS_SYMBOLS:
            with self.subTest(symbol_id=sid):
                # traded_only=False：与重算同样覆盖**全部**会话（含停牌日）。
                # 停牌日 g=1，两侧都应恰好保持不变 —— 这正是在验证「停牌不产生收益」。
                analytic = self.bundle.analytic_nav(sid, traded_only=False)["nav"].to_numpy()
                recomputed = recompute_nav_from_bars(self.bundle, sid)
                self.assertEqual(len(analytic), len(recomputed))
                np.testing.assert_allclose(analytic, recomputed, rtol=0, atol=1e-9)

    def test_gap_symbol_is_not_reproducible_from_observed_bars(self) -> None:
        """**语义断言**：带下载失败窗的标的必须**不可**由观测 bars 复现。

        失败窗里市场开着、总收益照常走，但我们没有数据 —— 这正是 P2.6 应报出的
        「日历预期缺口」。若这里能复现，说明缺口是假的、失败窗形同虚设。
        """
        analytic = self.bundle.analytic_nav(GAP_SYMBOL, traded_only=False)["nav"].to_numpy()
        recomputed = recompute_nav_from_bars(self.bundle, GAP_SYMBOL)
        n = min(len(analytic), len(recomputed))
        self.assertGreater(float(np.max(np.abs(analytic[:n] - recomputed[:n]))), 1e-6,
                           "带失败窗的标的竟然可复现 —— 缺口未真正生效")

    def test_unobserved_sessions_exposes_the_failure_window(self) -> None:
        gaps = self.bundle.unobserved_sessions(GAP_SYMBOL)
        window = set(pd.date_range(S.FAILURES[0].start, S.FAILURES[0].end, freq="D").date)
        self.assertTrue(gaps, "失败窗未产生任何缺口")
        self.assertTrue(set(gaps) <= window, f"缺口越出失败窗: {gaps}")
        self.assertEqual(self.bundle.unobserved_sessions(GAPLESS_SYMBOLS[0]), [])

    def test_dividend_and_split_are_answerable(self) -> None:
        """分红/拆分必须真的改变了价格路径，否则「已知答案」是空的。"""
        ca = self.bundle.tables["corporate_actions"]
        self.assertEqual(set(ca["kind"]), {"dividend", "split"})
        sp = S.SPLITS[0]
        sub = (self.bundle.tables["bars_daily"]
               .loc[lambda d: d["symbol_id"] == sp.symbol_id]
               .sort_values("ts").reset_index(drop=True))
        idx = sub.index[sub["ts"] == sp.ex_date][0]
        self.assertGreater(sub.loc[idx - 1, "close"] / sub.loc[idx, "close"], 2.0,
                           "拆分日未见预期的大幅价格下移")


class TestSuspensionSemantics(unittest.TestCase):
    """V3：停牌区间内不可成交，且不产生收益。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = generate()
        cls.spec = S.SUSPENSIONS[0]

    def _window(self) -> pd.DataFrame:
        bars = self.bundle.tables["bars_daily"]
        return bars[(bars["symbol_id"] == self.spec.symbol_id)
                    & (bars["ts"] >= self.spec.start) & (bars["ts"] <= self.spec.end)]

    def test_suspension_rows_are_marked_untradeable(self) -> None:
        win = self._window()
        self.assertTrue(len(win) > 0, "停牌窗内应有行（估值用），而非完全缺失")
        self.assertTrue((~win["traded"].astype(bool)).all(), "停牌窗内出现可成交行")
        self.assertTrue((win["volume"] == 0).all(), "停牌窗内出现非零成交量")

    def test_suspension_prices_are_carried(self) -> None:
        self.assertEqual(len(self._window()["close"].unique()), 1, "停牌期间收盘价应沿用同一值")

    def test_suspension_window_is_not_a_data_gap(self) -> None:
        """停牌 ≠ 休市/缺失：这些日子市场开着，故**不该**被算作数据缺口。"""
        gaps = set(self.bundle.unobserved_sessions(self.spec.symbol_id))
        window = set(pd.date_range(self.spec.start, self.spec.end, freq="D").date)
        self.assertEqual(gaps & window, set(), "停牌日被误判为数据缺口（停牌应产出行）")


class TestScenarioCoverage(unittest.TestCase):
    """七类场景齐备（P2.2 表格逐项）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = generate()

    def test_all_required_scenarios_present(self) -> None:
        bars = self.bundle.tables["bars_daily"]
        ca = self.bundle.tables["corporate_actions"]
        meta = self.bundle.meta["scenarios"]

        for key in ("dividends", "splits", "suspensions", "delisted", "late_listed", "failures"):
            self.assertTrue(meta[key], f"缺场景: {key}")
        for table in ("fx_rates", "macro_series", "trading_calendar", "fundamentals"):
            self.assertIn(table, self.bundle.tables, f"缺表: {table}")

        d = S.SYMBOLS_BY_ID[meta["delisted"][0]]
        self.assertEqual(pd.Timestamp(bars.loc[bars["symbol_id"] == d.symbol_id, "ts"].max()),
                         pd.Timestamp(d.delisted_on), "退市标的多出退市后行情")

        late = S.SYMBOLS_BY_ID[meta["late_listed"][0]]
        self.assertEqual(pd.Timestamp(bars.loc[bars["symbol_id"] == late.symbol_id, "ts"].min()),
                         pd.Timestamp(late.listed_on), "晚上市标的的首行日期不符")
        self.assertGreater(late.listed_on, S.STUDY_START, "晚上市标的应晚于研究起点")

        self.assertTrue(ca["available_utc"].notna().all(),
                        "公司行动缺 available_utc（会产生「除权前已知拆分」的未来函数）")

    def test_available_utc_never_precedes_close(self) -> None:
        """时序纪律：数据可用时间不得早于该 bar 的收盘时刻。"""
        bars = self.bundle.tables["bars_daily"]
        self.assertTrue((bars["available_utc"] >= bars["close_utc"]).all())


class TestSnapshotIO(unittest.TestCase):
    """快照写入 / 读回 / 不可覆盖。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = generate()

    def test_write_then_read_roundtrip_preserves_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_snapshot(self.bundle, tmp)
            self.assertTrue(path.is_dir())
            back = read_snapshot(self.bundle.snapshot_id, tmp)
            self.assertEqual(back.content_hashes(), self.bundle.content_hashes())
            for name, df in self.bundle.tables.items():
                pd.testing.assert_frame_equal(back.tables[name].reset_index(drop=True),
                                              df.reset_index(drop=True))

    def test_no_staging_left_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_snapshot(self.bundle, tmp)
            self.assertEqual(list(Path(tmp).glob(".staging-*")), [], "残留暂存目录")

    def test_existing_snapshot_is_not_overwritten(self) -> None:
        """快照不可变：重复写入必须**拒绝**，而非原地覆盖。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_snapshot(self.bundle, tmp)
            marker = path / "manifest.json"
            before = marker.read_bytes()
            with self.assertRaises(SnapshotExistsError):
                write_snapshot(self.bundle, tmp)
            self.assertEqual(marker.read_bytes(), before, "旧快照内容被改动")

    def test_failed_write_leaves_no_partial_snapshot(self) -> None:
        """写入中途失败 → 目标路径不存在，且不残留暂存目录。"""
        from quantlab.fixtures import synth

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 注意：必须在 **synth 的命名空间**上打补丁 —— synth.py 用
            # `from ... import atomic_write_parquet` 按名绑定，改 atomic 模块
            # 上的属性不会影响它已绑定的引用（这类"补丁打错层"会让失败测试假通过）。
            original = synth.atomic_write_parquet
            calls = {"n": 0}

            def boom(df, path):
                calls["n"] += 1
                if calls["n"] == 3:
                    raise OSError("disk full (simulated)")
                return original(df, path)

            synth.atomic_write_parquet = boom
            try:
                with self.assertRaises(OSError):
                    write_snapshot(self.bundle, root)
            finally:
                synth.atomic_write_parquet = original

            self.assertFalse((root / self.bundle.snapshot_id).exists(), "产生了半截快照")
            self.assertEqual(list(root.glob(".staging-*")), [], "残留暂存目录")


if __name__ == "__main__":
    unittest.main(verbosity=2)
