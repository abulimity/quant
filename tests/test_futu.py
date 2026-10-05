"""futu 适配器 normalize + build_futu_bundle 单测 —— 离线、不 import futu SDK。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from quantlab.ingest.adapters.futu import FutuSource, futu_code_to_ticker
from quantlab.ingest.base import ContractError, Source
from quantlab.ingest.realdata import build_futu_bundle


def _kline() -> pd.DataFrame:
    return pd.DataFrame({
        "code": ["HK.00700", "HK.00700"],
        "name": ["腾讯控股", "腾讯控股"],
        "time_key": ["2024-01-02", "2024-01-03"],
        "open": [300.0, 305.0],
        "close": [305.0, 310.0],
        "high": [310.0, 312.0],
        "low": [298.0, 302.0],
        "volume": [100000.0, 120000.0],
        "turnover": [0.0, 0.0],
        "k_type": ["K_DAY", "K_DAY"],
        "last_close": [0.0, 0.0],
        "pe_ratio": [0.0, 0.0],
        "turnover_rate": [0.0, 0.0],
    })


def _hk_symbols() -> pd.DataFrame:
    return pd.DataFrame({
        "ts_code": ["00700.HK", "00005.HK"],
        "name": ["腾讯控股", "汇丰控股"],
        "market": ["HK", "HK"],
        "list_status": ["L", "L"],
        "list_date": [date(2004, 6, 16), date(1990, 1, 1)],
        "delist_date": [None, None],
        "trade_unit": [100.0, 400.0],
        "isin": ["KYG875721634", "GB0005405286"],
        "curr_type": ["HKD", "HKD"],
    })


class TestFutuCodeToTicker(unittest.TestCase):
    def test_hk(self):
        self.assertEqual(futu_code_to_ticker("HK.00700"), "00700.HK")

    def test_rejects_other_market(self):
        with self.assertRaises(ContractError):
            futu_code_to_ticker("US.AAPL")

    def test_rejects_malformed(self):
        with self.assertRaises(ContractError):
            futu_code_to_ticker("00700")


class TestFutuNormalize(unittest.TestCase):
    def setUp(self):
        self.symbol_map = {"00700.HK": 1_000_000_002}

    def test_normalize_bars(self):
        src = FutuSource(symbol_map=self.symbol_map)
        out = src.normalize(_kline())
        self.assertEqual(list(out["symbol_id"]), [1_000_000_002, 1_000_000_002])
        self.assertEqual(list(out["ts"]), [date(2024, 1, 2), date(2024, 1, 3)])
        self.assertEqual(set(out["currency"]), {"HKD"})
        # close_utc = ts + 8h（XHKG 16:00 HKT = 08:00 UTC）
        self.assertEqual(str(out["close_utc"].iloc[0]), "2024-01-02 08:00:00")
        # available_utc = ts + 1 天 00:00
        self.assertEqual(str(out["available_utc"].iloc[0]), "2024-01-03 00:00:00")

    def test_unmapped_code_fails_closed(self):
        src = FutuSource(symbol_map={"99999.HK": 1})
        with self.assertRaises(ContractError):
            src.normalize(_kline())

    def test_is_source_protocol(self):
        self.assertIsInstance(FutuSource(), Source)

    def test_fetch_requires_raw_bronze_dir(self):
        with self.assertRaises(ContractError):
            FutuSource().fetch()


class TestBuildFutuBundle(unittest.TestCase):
    def test_offline_bundle(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        raw_dir = Path(tmp.name)
        _kline().to_parquet(raw_dir / "kline.parquet")

        bundle = build_futu_bundle(
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            hk_symbols=_hk_symbols(),
            raw_bronze_dir=raw_dir,
        )
        self.assertTrue(bundle.snapshot_id.startswith("futu-"))
        self.assertIn("symbols", bundle.tables)
        self.assertIn("bars_daily", bundle.tables)
        # 00700.HK 在排序后第 2 位（"00005.HK" < "00700.HK"）→ base(1e9) + 2
        self.assertEqual(int(bundle.tables["bars_daily"]["symbol_id"].iloc[0]), 1_000_000_002)
        # symbols 表是全量目录（2 行）
        self.assertEqual(len(bundle.tables["symbols"]), 2)


if __name__ == "__main__":
    unittest.main()
