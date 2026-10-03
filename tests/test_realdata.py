"""真实数据快照构造的离线契约测试（不联网）。

覆盖 `realdata.py` 里**纯函数**的部分：
    · `_derive_snapshot_id`：确定性 + 不依赖时钟/downloaded_at
    · `check_real_invariants`：结构红旗（非正价、区间自洽、主键唯一、防未来函数）
    · `assign_symbol_ids`：排除 REITs、按 ts_code 排序的确定性永久 ID

`build_tushare_bundle` 需联网，端到端验证走 `docs/deploy/EVIDENCE.md` 记录的小样本跑批，
不在此用网络 fixture 制造假绿。
"""

from __future__ import annotations

import unittest
from datetime import date, datetime

import pandas as pd

from quantlab.fixtures.synth import FixtureBundle
from quantlab.ingest.adapters.tushare import assign_symbol_ids
from quantlab.ingest.realdata import _derive_snapshot_id, check_real_invariants


def _bars(symbol_id=1, ts=date(2023, 1, 3), close=3.5, available=None):
    """一张最小但合法的 bars 帧。available_utc 默认 = ts + 1 天。"""
    av = available or datetime.combine(ts + pd.Timedelta(days=1).to_pytimedelta(),
                                       datetime.min.time())
    return pd.DataFrame({
        "symbol_id": [symbol_id],
        "ts": [ts],
        "open": [close],
        "high": [close * 1.01],
        "low": [close * 0.99],
        "close": [close],
        "volume": [1000.0],
        "currency": ["CNY"],
        "close_utc": [datetime.combine(ts, datetime.min.time())],
        "available_utc": [av],
    })


def _symbols():
    return pd.DataFrame({
        "symbol_id": [1, 2],
        "ticker": ["510300.SH", "159919.SZ"],
        "exchange": ["XSHG", "XSHG"],
        "calendar": ["XSHG", "XSHG"],
        "currency": ["CNY", "CNY"],
        "isin": [None, None],
        "lot_size": [100, 100],
        "listed_on": [date(2012, 5, 28), date(2012, 12, 25)],
        "delisted_on": [None, None],
    })


def _bundle(bars=None, ca=None, symbols=None):
    tables = {
        "symbols": symbols if symbols is not None else _symbols(),
        "bars_daily": bars if bars is not None else _bars(),
        "corporate_actions": ca if ca is not None else pd.DataFrame(columns=[
            "symbol_id", "ex_date", "kind", "ratio", "cash", "pay_date", "available_utc"]),
    }
    return FixtureBundle(snapshot_id="tushare-test", tables=tables)


class TestDeriveSnapshotId(unittest.TestCase):
    def test_deterministic(self):
        tables = {"symbols": _symbols(), "bars_daily": _bars()}
        self.assertEqual(_derive_snapshot_id("tushare", tables),
                         _derive_snapshot_id("tushare", tables))

    def test_source_is_a_salt(self):
        tables = {"symbols": _symbols(), "bars_daily": _bars()}
        self.assertNotEqual(_derive_snapshot_id("tushare", tables),
                            _derive_snapshot_id("futu", tables))

    def test_data_change_changes_id(self):
        tables = {"symbols": _symbols(), "bars_daily": _bars()}
        changed = {"symbols": _symbols(), "bars_daily": _bars(close=4.2)}
        self.assertNotEqual(_derive_snapshot_id("tushare", tables),
                            _derive_snapshot_id("tushare", changed))


class TestCheckRealInvariants(unittest.TestCase):
    def test_valid_bundle_passes(self):
        self.assertEqual(check_real_invariants(_bundle()), [])

    def test_empty_bars_passes(self):
        bars = _bars().iloc[0:0]
        self.assertEqual(check_real_invariants(_bundle(bars=bars)), [])

    def test_non_positive_price(self):
        bars = _bars(close=0.0)
        self.assertTrue(any("非正价格" in m for m in check_real_invariants(_bundle(bars=bars))))

    def test_low_above_min_open_close(self):
        bars = _bars()
        bars["low"] = 999.0
        self.assertTrue(any("low" in m for m in check_real_invariants(_bundle(bars=bars))))

    def test_duplicate_bars_primary_key(self):
        bars = pd.concat([_bars(), _bars()], ignore_index=True)
        self.assertTrue(any("重复" in m for m in check_real_invariants(_bundle(bars=bars))))

    def test_future_availability(self):
        bars = _bars()
        bars["available_utc"] = datetime.combine(date(2023, 1, 2), datetime.min.time())
        self.assertTrue(any("未来函数" in m for m in check_real_invariants(_bundle(bars=bars))))

    def test_corporate_action_after_ex_date(self):
        ca = pd.DataFrame({
            "symbol_id": [1], "ex_date": [date(2023, 1, 5)], "kind": ["dividend"],
            "ratio": [None], "cash": [0.05], "pay_date": [date(2023, 1, 10)],
            "available_utc": [datetime(2023, 1, 6)],
        })
        self.assertTrue(any("ex_date" in m for m in check_real_invariants(_bundle(ca=ca))))

    def test_duplicate_symbols(self):
        symbols = pd.concat([_symbols(), _symbols().iloc[0:1]], ignore_index=True)
        self.assertTrue(any("symbols" in m for m in check_real_invariants(_bundle(symbols=symbols))))


class TestAssignSymbolIds(unittest.TestCase):
    def test_excludes_reits_and_sorts(self):
        raw = pd.DataFrame({
            "ts_code": ["159919.SZ", "508000.SH", "510300.SH"],
            "fund_type": ["股票型", "REITs", "股票型"],
        })
        ids = assign_symbol_ids(raw)
        self.assertEqual(ids, {"159919.SZ": 1, "510300.SH": 2})
        self.assertNotIn("508000.SH", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
