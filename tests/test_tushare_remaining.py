"""剩余 tushare 数据集（fund_adj / index_* / hk_symbols / macro_series）的离线契约测试。

不联网：只覆盖 `adapters/tushare.py` 里新增 normalize 纯函数、`realdata.py` 的
`check_real_invariants`（对 index/hk/macro bundle 不得 KeyError）与 `_derive_snapshot_id` /
`content_hashes` 对新表的兼容。端到端跑批仍走 `docs/deploy/EVIDENCE.md` 记录的小样本。
"""

from __future__ import annotations

import unittest
from datetime import date, datetime

import pandas as pd

from quantlab.fixtures.synth import FixtureBundle
from quantlab.ingest.adapters.tushare import TushareSource
from quantlab.ingest.realdata import _derive_snapshot_id, check_real_invariants


def _fund_adj(symbol_id=1, ts=date(2023, 1, 3), adj=1.0):
    return pd.DataFrame({"symbol_id": [symbol_id], "ts": [ts], "adj_factor": [adj]})


def _index_daily(ts_code="000300.SH", ts=date(2023, 1, 3), close=4000.0):
    return pd.DataFrame({
        "ts_code": [ts_code], "ts": [ts],
        "open": [close], "high": [close * 1.01], "low": [close * 0.99], "close": [close],
        "pre_close": [close * 0.999], "change": [close * 0.001], "pct_chg": [0.1],
        "volume": [1e6], "amount": [1e9],
    })


def _macro(series_id="CN_CPI_YOY", ts=date(2023, 1, 31), value=2.0):
    return pd.DataFrame({
        "series_id": [series_id], "ts": [ts], "value": [value], "unit": ["percent"],
        "available_utc": [datetime.combine(date(2023, 2, 15), datetime.min.time())],
    })


class TestMacroTs(unittest.TestCase):
    def test_month_end(self):
        self.assertEqual(TushareSource._macro_ts("202401", "month"), date(2024, 1, 31))

    def test_quarter_end(self):
        self.assertEqual(TushareSource._macro_ts("2024Q1", "quarter"), date(2024, 3, 31))
        self.assertEqual(TushareSource._macro_ts("2024Q4", "quarter"), date(2024, 12, 31))

    def test_day(self):
        self.assertEqual(TushareSource._macro_ts("20240115", "day"), date(2024, 1, 15))

    def test_none_values(self):
        self.assertIsNone(TushareSource._macro_ts("", "month"))
        self.assertIsNone(TushareSource._macro_ts("nan", "month"))


class TestNormalizeFundAdj(unittest.TestCase):
    def test_maps_symbol_and_parses_adj(self):
        raw = pd.DataFrame({
            "ts_code": ["510300.SH"], "trade_date": ["20230103"], "adj_factor": ["1.234"],
        })
        src = TushareSource(dataset="fund_adj", symbol_map={"510300.SH": 7})
        out = src.normalize(raw)
        self.assertEqual(out.iloc[0]["symbol_id"], 7)
        self.assertEqual(out.iloc[0]["ts"], date(2023, 1, 3))
        self.assertEqual(out.iloc[0]["adj_factor"], 1.234)


class TestNormalizeIndexSymbols(unittest.TestCase):
    def test_normalizes_catalog(self):
        raw = pd.DataFrame({
            "ts_code": ["000300.SH"], "name": ["沪深300"], "market": ["SSE"],
            "publisher": ["中证"], "category": ["规模指数"], "base_date": ["20041231"],
            "base_point": ["1000"], "list_date": ["20050408"],
        })
        out = TushareSource(dataset="index_symbols").normalize(raw)
        self.assertEqual(out.iloc[0]["ts_code"], "000300.SH")
        self.assertEqual(out.iloc[0]["base_point"], 1000.0)


class TestNormalizeIndexDaily(unittest.TestCase):
    def test_normalizes_ohlc_and_units(self):
        raw = pd.DataFrame({
            "ts_code": ["000300.SH"], "trade_date": ["20230103"],
            "open": ["3990"], "high": ["4010"], "low": ["3980"], "close": ["4000"],
            "pre_close": ["3985"], "change": ["15"], "pct_chg": ["0.376"],
            "vol": ["1000000"], "amount": ["1000000"],
        })
        out = TushareSource(dataset="index_daily").normalize(raw)
        self.assertEqual(out.iloc[0]["ts_code"], "000300.SH")
        self.assertEqual(out.iloc[0]["volume"], 1000000.0)   # 手（未换算）
        self.assertEqual(out.iloc[0]["amount"], 1000000.0)    # 千元（未换算）
        self.assertEqual(out.iloc[0]["close"], 4000.0)


class TestNormalizeHkSymbols(unittest.TestCase):
    def test_normalizes_metadata(self):
        raw = pd.DataFrame({
            "ts_code": ["00700.HK"], "name": ["腾讯"], "market": ["HK"],
            "list_status": ["L"], "list_date": ["20040616"], "trade_unit": ["100"],
            "isin": ["KYG875721634"], "curr_type": ["HKD"],
        })
        out = TushareSource(dataset="hk_symbols").normalize(raw)
        self.assertEqual(out.iloc[0]["ts_code"], "00700.HK")
        self.assertEqual(out.iloc[0]["curr_type"], "HKD")
        self.assertEqual(out.iloc[0]["trade_unit"], 100.0)


class TestNormalizeMacro(unittest.TestCase):
    def test_produces_contract_and_available_utc(self):
        raw = pd.DataFrame({
            "series_id": ["CN_CPI_YOY"], "ts": [date(2023, 1, 31)],
            "value": [2.1], "unit": ["percent"],
        })
        out = TushareSource(dataset="macro_series").normalize(raw)
        self.assertEqual(list(out.columns),
                         ["series_id", "ts", "value", "unit", "available_utc"])
        self.assertGreaterEqual(
            pd.to_datetime(out.iloc[0]["available_utc"]), pd.to_datetime(out.iloc[0]["ts"]))


class TestRealInvariantsNewTables(unittest.TestCase):
    def test_index_only_bundle_does_not_keyerror(self):
        bundle = FixtureBundle(snapshot_id="tushare_index-test", tables={
            "index_symbols": pd.DataFrame({"ts_code": ["000300.SH"]}),
            "index_daily": _index_daily().assign(snapshot_id="x"),
        })
        self.assertEqual(check_real_invariants(bundle), [])

    def test_hk_only_bundle_does_not_keyerror(self):
        bundle = FixtureBundle(snapshot_id="tushare_hk-test", tables={
            "hk_symbols": pd.DataFrame({"ts_code": ["00700.HK"]}),
        })
        self.assertEqual(check_real_invariants(bundle), [])

    def test_macro_only_bundle_does_not_keyerror(self):
        bundle = FixtureBundle(snapshot_id="tushare_macro-test", tables={
            "macro_series": _macro(),
        })
        self.assertEqual(check_real_invariants(bundle), [])

    def test_fund_adj_non_positive_factor(self):
        bundle = FixtureBundle(snapshot_id="x", tables={"fund_adj": _fund_adj(adj=0.0)})
        self.assertTrue(any("非正复权因子" in m for m in check_real_invariants(bundle)))

    def test_fund_adj_duplicate_key(self):
        adj = pd.concat([_fund_adj(), _fund_adj()], ignore_index=True)
        bundle = FixtureBundle(snapshot_id="x", tables={"fund_adj": adj})
        self.assertTrue(any("fund_adj 重复" in m for m in check_real_invariants(bundle)))

    def test_index_daily_bad_ohlc(self):
        daily = _index_daily()
        daily["low"] = 99999.0
        bundle = FixtureBundle(snapshot_id="x", tables={"index_daily": daily})
        self.assertTrue(any("low" in m for m in check_real_invariants(bundle)))

    def test_index_daily_duplicate_key(self):
        daily = pd.concat([_index_daily(), _index_daily()], ignore_index=True)
        bundle = FixtureBundle(snapshot_id="x", tables={"index_daily": daily})
        self.assertTrue(any("index_daily 重复" in m for m in check_real_invariants(bundle)))

    def test_macro_future_availability(self):
        macro = _macro()
        macro["available_utc"] = datetime.combine(date(2023, 1, 30), datetime.min.time())
        bundle = FixtureBundle(snapshot_id="x", tables={"macro_series": macro})
        self.assertTrue(any("macro_series 存在 available_utc < ts" in m
                            for m in check_real_invariants(bundle)))

    def test_index_symbols_duplicate(self):
        syms = pd.DataFrame({"ts_code": ["000300.SH", "000300.SH"]})
        bundle = FixtureBundle(snapshot_id="x", tables={"index_symbols": syms})
        self.assertTrue(any("index_symbols 存在重复 ts_code" in m
                            for m in check_real_invariants(bundle)))


class TestSnapshotIdNewTables(unittest.TestCase):
    def test_derive_and_content_hashes_no_keyerror(self):
        tables = {
            "index_symbols": pd.DataFrame({"ts_code": ["000300.SH"]}),
            "index_daily": _index_daily(),
        }
        sid = _derive_snapshot_id("tushare_index", tables)
        self.assertTrue(sid.startswith("tushare_index-"))
        # 打标后（含 snapshot_id）应能通过 content_hashes / combined_content_hash
        tagged = {
            "index_symbols": tables["index_symbols"],
            "index_daily": tables["index_daily"].assign(source="tushare_index", snapshot_id=sid),
        }
        bundle = FixtureBundle(snapshot_id=sid, tables=tagged)
        self.assertEqual(set(bundle.content_hashes()), {"index_symbols", "index_daily"})
        self.assertIsInstance(bundle.combined_content_hash(), str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
