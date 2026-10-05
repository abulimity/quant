"""Tushare 适配器契约测试（LOCAL_DEPLOYMENT_PLAN.md §P2.4 / 附录 A）。

仿 `tests/test_p2_4_adapters.py`，**不联网**：所有样本都是内联合成帧。
覆盖：
    V0  `import quantlab.ingest.adapters.tushare` 不因缺 `tushare` SDK 而失败（延迟导入）
    V3  `normalize()` 产出符合 P2.1 契约（symbols / bars_daily / corporate_actions）
    F.6 复权/单位/可用时间口径的显式断言
    另：空表、缺列、未配置代码、未识别后缀、缺 token —— 一律显式报错

运行：
    python -m unittest tests.test_tushare -v
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
import unittest
import unittest.mock as mock
from datetime import date

import pandas as pd

from quantlab.ingest.adapters.tushare import TushareSource
from quantlab.ingest.base import CONTRACT, ContractError, FetchSpec, Source, validate_normalized

ADAPTER_MODULE = "quantlab.ingest.adapters.tushare"
VENDOR_SDK = "tushare"


def _spec(dataset: str = "bars_daily", symbols=("510300.SH",)) -> FetchSpec:
    return FetchSpec(dataset=dataset, start=date(2024, 1, 1), end=date(2024, 1, 5),
                     symbols=symbols)


# ---- 固定样本（合成，非真实数据） ----
TSH_BARS_RAW = pd.DataFrame({
    "ts_code": ["510300.SH", "510300.SH"],
    "trade_date": ["20240102", "20240103"],
    "open": [3.500, 3.520],
    "high": [3.560, 3.545],
    "low": [3.480, 3.500],
    "close": [3.540, 3.510],
    "vol": [1_200_000.0, 980_000.0],
    "amount": [42_480.0, 34_398.0],   # 千元（未换算）
})

# 同一 (ts_code, ex_date) 系统重复 3 行「实施」+ 1 行「预案」→ 过滤+去重后应只剩 1 行
TSH_DIV_RAW = pd.DataFrame({
    "ts_code": ["510050.SH"] * 4,
    "ann_date": ["20231110"] * 4,
    "ex_date": ["20231115"] * 4,
    "pay_date": ["20231121"] * 4,
    "div_proc": ["实施", "实施", "实施", "预案"],
    "div_cash": [0.05, 0.05, 0.05, 0.05],
})

# 3 只：SH / SZ / REITs（REITs 应被排除，且无需 symbol_id）
TSH_BASIC_RAW = pd.DataFrame({
    "ts_code": ["510300.SH", "159919.SZ", "508000.SH"],
    "name": ["沪深300ETF", "创业板ETF", "REITs基金"],
    "fund_type": ["股票型", "股票型", "REITs"],
    "list_date": ["20120528", "20121225", "20210621"],
    "delist_date": [None, None, None],
})

_BAR_MAP = {"510300.SH": 1}
_DIV_MAP = {"510050.SH": 2}
_BASIC_MAP = {"510300.SH": 1, "159919.SZ": 2}


class TestImportWithoutSdk(unittest.TestCase):
    """V0：骨架可导入，且延迟导入真实生效。"""

    def test_module_imports(self) -> None:
        importlib.import_module(ADAPTER_MODULE)

    def test_sdk_not_imported_at_module_level(self) -> None:
        code = textwrap.dedent(f"""
            import sys
            __import__({ADAPTER_MODULE!r})
            print("LEAKED", [s for s in {[VENDOR_SDK]!r} if s in sys.modules])
        """)
        env = {"PYTHONIOENCODING": "utf-8", "PATH": os.environ["PATH"]}
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env)
        self.assertIn("LEAKED []", proc.stdout,
                      f"导入适配器时把 SDK 拉进来了: {proc.stdout!r} {proc.stderr!r}")

    def test_source_protocol_satisfied(self) -> None:
        src = TushareSource(dataset="bars_daily")
        self.assertIsInstance(src, Source)
        self.assertEqual(src.name, "tushare")

    def test_registered_in_package_exports(self) -> None:
        from quantlab.ingest.adapters import TushareSource as _Exported  # noqa: F401
        self.assertIs(_Exported, TushareSource)


class TestNormalizeBars(unittest.TestCase):
    """bars_daily 契约。"""

    def test_normalize_satisfies_contract(self) -> None:
        out = TushareSource(dataset="bars_daily", symbol_map=_BAR_MAP).normalize(TSH_BARS_RAW)
        validate_normalized(out, "bars_daily")
        self.assertEqual(set(out["symbol_id"]), {1})
        self.assertEqual(str(out["ts"].iloc[0]), "2024-01-02")
        self.assertTrue(pd.api.types.is_float_dtype(out["close"]))

    def test_volume_kept_in_lots(self) -> None:
        """F.6 单位声明：vol(手) 原样落入 volume，不静默换算。"""
        out = TushareSource(dataset="bars_daily", symbol_map=_BAR_MAP).normalize(TSH_BARS_RAW)
        self.assertEqual(float(out["volume"].iloc[0]), 1_200_000.0)

    def test_amount_kept_in_thousand_yuan(self) -> None:
        """F.6 单位声明：amount(千元) 原样落入 amount，不静默换算为元。"""
        out = TushareSource(dataset="bars_daily", symbol_map=_BAR_MAP).normalize(TSH_BARS_RAW)
        self.assertEqual(float(out["amount"].iloc[0]), 42_480.0)

    def test_availability_is_conservative_lag(self) -> None:
        out = TushareSource(dataset="bars_daily", symbol_map=_BAR_MAP).normalize(TSH_BARS_RAW)
        self.assertEqual(str(out["available_utc"].iloc[0]), "2024-01-03 00:00:00")
        self.assertIn("保守滞后", TushareSource.availability_note)

    def test_close_utc_is_0700(self) -> None:
        out = TushareSource(dataset="bars_daily", symbol_map=_BAR_MAP).normalize(TSH_BARS_RAW)
        self.assertEqual(pd.Timestamp(out["close_utc"].iloc[0]).strftime("%H:%M"), "07:00")


class TestNormalizeCorporateActions(unittest.TestCase):
    """corporate_actions 契约：过滤 + 去重 + 可用时间口径。"""

    def test_filter_and_dedup(self) -> None:
        out = TushareSource(dataset="corporate_actions", symbol_map=_DIV_MAP).normalize(TSH_DIV_RAW)
        validate_normalized(out, "corporate_actions")
        self.assertEqual(len(out), 1, "应过滤掉「预案」并去重到唯一 ex_date")
        self.assertEqual(float(out["cash"].iloc[0]), 0.05)
        self.assertEqual(out["kind"].iloc[0], "dividend")

    def test_available_utc_is_announcement_date(self) -> None:
        out = TushareSource(dataset="corporate_actions", symbol_map=_DIV_MAP).normalize(TSH_DIV_RAW)
        self.assertEqual(pd.Timestamp(out["available_utc"].iloc[0]).date(), date(2023, 11, 10))
        # 公告日早于除权日 → 无未来函数
        self.assertLessEqual(pd.Timestamp(out["available_utc"].iloc[0]).date(),
                             pd.Timestamp(out["ex_date"].iloc[0]).date())

    def test_empty_is_valid(self) -> None:
        """多数 ETF 从未分红：空表是合法结果，不得当作错误。"""
        out = TushareSource(dataset="corporate_actions", symbol_map=_DIV_MAP).normalize(pd.DataFrame())
        validate_normalized(out, "corporate_actions")
        self.assertEqual(len(out), 0)
        self.assertEqual(list(out.columns), list(CONTRACT["corporate_actions"]))


class TestNormalizeSymbols(unittest.TestCase):
    """symbols 契约：排除 REITs、后缀映射交易所。"""

    def test_reits_excluded_and_exchange_mapped(self) -> None:
        out = TushareSource(dataset="symbols", symbol_map=_BASIC_MAP).normalize(TSH_BASIC_RAW)
        validate_normalized(out, "symbols")
        self.assertEqual(len(out), 2, "REITs 应被排除")
        by_ticker = dict(zip(out["ticker"], out["exchange"]))
        self.assertEqual(by_ticker["510300.SH"], "XSHG")
        # exchange-calendars 无 XSHE；沪深共用 A 股日历 → SZ 也映射 XSHG
        self.assertEqual(by_ticker["159919.SZ"], "XSHG")

    def test_listed_on_and_lot_size(self) -> None:
        out = TushareSource(dataset="symbols", symbol_map=_BASIC_MAP).normalize(TSH_BASIC_RAW)
        row = out[out["ticker"] == "510300.SH"].iloc[0]
        self.assertEqual(row["listed_on"], date(2012, 5, 28))
        self.assertEqual(row["lot_size"], 100)
        self.assertEqual(row["currency"], "CNY")

    def test_name_and_invest_type_kept(self) -> None:
        """name 原样落入；fund_type → invest_type 粗分类映射。"""
        out = TushareSource(dataset="symbols", symbol_map=_BASIC_MAP).normalize(TSH_BASIC_RAW)
        by_ticker = dict(zip(out["ticker"], out[["name", "invest_type"]].to_numpy()))
        self.assertEqual(by_ticker["510300.SH"][0], "沪深300ETF")
        self.assertEqual(by_ticker["510300.SH"][1], "股票")

    def test_unlisted_fund_type_maps_to_other(self) -> None:
        """未列出的 fund_type（如 混合型）fail-safe 归「其他」，不猜测。"""
        raw = TSH_BASIC_RAW.copy()
        raw.loc[raw["ts_code"] == "159919.SZ", "fund_type"] = "混合型"
        out = TushareSource(dataset="symbols", symbol_map=_BASIC_MAP).normalize(raw)
        row = out[out["ticker"] == "159919.SZ"].iloc[0]
        self.assertEqual(row["invest_type"], "其他")


class TestFailClosed(unittest.TestCase):
    """空表 / 缺列 / 未配置代码 / 未知后缀 / 缺 token —— 一律显式报错。"""

    def test_empty_bars_raise(self) -> None:
        with self.assertRaises(ContractError):
            TushareSource(dataset="bars_daily", symbol_map=_BAR_MAP).normalize(TSH_BARS_RAW.iloc[0:0])

    def test_missing_column_raises(self) -> None:
        with self.assertRaises(ContractError) as ctx:
            TushareSource(dataset="bars_daily", symbol_map=_BAR_MAP).normalize(
                TSH_BARS_RAW.drop(columns=["close"]))
        self.assertIn("close", str(ctx.exception))

    def test_unmapped_symbol_raises(self) -> None:
        with self.assertRaises(ContractError) as ctx:
            TushareSource(dataset="bars_daily", symbol_map={}).normalize(TSH_BARS_RAW)
        self.assertIn("symbol_map", str(ctx.exception))

    def test_unknown_suffix_raises(self) -> None:
        raw = TSH_BASIC_RAW.copy()
        raw.loc[raw["ts_code"] == "159919.SZ", "ts_code"] = "920000.BJ"
        raw.loc[raw["ts_code"] == "508000.SH", "fund_type"] = "股票型"
        with self.assertRaises(ContractError) as ctx:
            TushareSource(dataset="symbols", symbol_map=_BASIC_MAP).normalize(raw)
        self.assertIn("后缀", str(ctx.exception))

    def test_unknown_dataset_raises(self) -> None:
        with self.assertRaises(ContractError):
            TushareSource(dataset="not_a_real_dataset")

    def test_fetch_missing_token_raises(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TUSHARE_TOKEN", None)
            with self.assertRaises(ContractError) as ctx:
                TushareSource(dataset="bars_daily", symbol_map=_BAR_MAP).fetch(_spec())
        self.assertIn("TUSHARE_TOKEN", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
