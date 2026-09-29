"""P2.4 Source 协议与适配器骨架验收（LOCAL_DEPLOYMENT_PLAN.md §P2.4）。

覆盖：
    V0  每个骨架适配器**均可导入**，不因缺少供应商 SDK 而 ImportError（延迟导入）
    V3  每个骨架的 `normalize()` 用固定输入样本测试：输出符合 P2.1 契约
        （字段齐全、类型正确、含 available_utc）
    V3  未实现入口调用时抛出**明确的** NotImplementedError("VENDOR-TBD")，
        不得返回空表或静默成功
    另：空表 / 缺列 / 未配置代码 —— 一律显式报错

**本模块不联网**：所有样本都是内联的合成帧。

运行：
    python -m unittest discover -t . -s tests -v
"""

from __future__ import annotations

import importlib
import sys
import unittest
from datetime import date

import pandas as pd

from quantlab.ingest.adapters.akshare import AkshareSource
from quantlab.ingest.adapters.macro_fred import FredSource
from quantlab.ingest.adapters.yfinance import (
    RETURN_KIND_PRICE,
    RETURN_KIND_TOTAL,
    YfinanceSource,
)
from quantlab.ingest.base import (
    CONTRACT,
    ContractError,
    FetchSpec,
    Source,
    validate_normalized,
)

ADAPTER_MODULES = [
    "quantlab.ingest.adapters.akshare",
    "quantlab.ingest.adapters.yfinance",
    "quantlab.ingest.adapters.macro_fred",
]

# 供应商 SDK —— 不应因未安装而使骨架导入失败
VENDOR_SDKS = ("akshare", "yfinance", "fredapi")


def _spec(dataset: str = "bars_daily") -> FetchSpec:
    return FetchSpec(dataset=dataset, start=date(2024, 1, 1), end=date(2024, 1, 5),
                     symbols=("SYN",))


# ---- 固定样本（合成，非真实数据） ----
AKSHARE_RAW = pd.DataFrame({
    "代码": ["510300", "510300"],
    "日期": ["2024-01-02", "2024-01-03"],
    "开盘": [3.500, 3.520],
    "最高": [3.560, 3.545],
    "最低": [3.480, 3.500],
    "收盘": [3.540, 3.510],
    "成交量": [1_200_000, 980_000],
})

YF_BARS_ADJ = pd.DataFrame({
    "Date": ["2024-01-02", "2024-01-03"],
    "Ticker": ["SPY", "SPY"],
    "Open": [470.0, 471.5], "High": [475.0, 473.0],
    "Low": [468.0, 469.0], "Close": [474.0, 470.0],
    "Adj Close": [470.1, 466.2], "Volume": [80_000_000, 75_000_000],
})
YF_BARS_PRICE_ONLY = YF_BARS_ADJ.drop(columns=["Adj Close"])

YF_FX = pd.DataFrame({"Date": ["2024-01-02", "2024-01-03"], "Close": [7.1010, 7.0950]})

FRED_RAW = pd.DataFrame({
    "series_id": ["DGS10", "DGS10"],
    "ts": ["2024-01-02", "2024-01-03"],
    "value": [3.95, 3.98],
})


class TestSkeletonsImportWithoutSdk(unittest.TestCase):
    """V0：骨架可导入，且**延迟导入**真实生效。"""

    def test_all_adapter_modules_import(self) -> None:
        for name in ADAPTER_MODULES:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_vendor_sdks_are_not_imported_at_module_level(self) -> None:
        """延迟导入的**可执行**证明：导入骨架后，SDK 仍不在 sys.modules 里。"""
        for name in ADAPTER_MODULES:
            importlib.import_module(name)
        leaked = [sdk for sdk in VENDOR_SDKS if sdk in sys.modules]
        self.assertEqual(leaked, [],
                         f"这些 SDK 在骨架导入时被拉进来了（应延迟到 fetch()）: {leaked}")

    def test_source_protocol_is_satisfied(self) -> None:
        for src in (AkshareSource(), YfinanceSource(), FredSource()):
            with self.subTest(source=src.name):
                self.assertIsInstance(src, Source)
                self.assertTrue(str(src.name))


class TestUnimplementedEntrancesFailClosed(unittest.TestCase):
    """V3：未实现入口必须**明确报错**，不得返回空表或静默成功。"""

    def test_akshare_fetch_raises_vendor_tbd(self) -> None:
        with self.assertRaises(NotImplementedError) as ctx:
            AkshareSource().fetch(_spec())
        self.assertIn("VENDOR-TBD", str(ctx.exception))

    def test_yfinance_fetch_raises_vendor_tbd(self) -> None:
        with self.assertRaises(NotImplementedError) as ctx:
            YfinanceSource().fetch(_spec())
        self.assertIn("VENDOR-TBD", str(ctx.exception))

    def test_fred_fetch_raises_vendor_tbd(self) -> None:
        with self.assertRaises(NotImplementedError) as ctx:
            FredSource().fetch(_spec("macro_series"))
        self.assertIn("VENDOR-TBD", str(ctx.exception))

    def test_error_message_is_actionable(self) -> None:
        """报错必须告诉人**下一步做什么**，而不是只抛一句「未实现」。"""
        with self.assertRaises(NotImplementedError) as ctx:
            AkshareSource().fetch(_spec())
        message = str(ctx.exception)
        self.assertIn("sources.yaml", message)
        self.assertIn("不得返回空表或静默成功", message)


class TestNormalizeContract(unittest.TestCase):
    """V3：`normalize()` 产出符合 P2.1 契约。"""

    def test_akshare_normalize_satisfies_contract(self) -> None:
        out = AkshareSource(symbol_map={"510300": 1}).normalize(AKSHARE_RAW)
        validate_normalized(out, "bars_daily")
        self.assertEqual(set(out["symbol_id"]), {1})
        self.assertEqual(str(out["ts"].iloc[0]), "2024-01-02")
        self.assertTrue(pd.api.types.is_float_dtype(out["close"]))
        self.assertTrue(out["available_utc"].notna().all())

    def test_akshare_availability_is_disclosed_conservative_lag(self) -> None:
        """F.6：无发布时刻时用保守滞后，且必须**可追溯**（note 挂在适配器上）。"""
        out = AkshareSource(symbol_map={"510300": 1}).normalize(AKSHARE_RAW)
        self.assertEqual(str(out["available_utc"].iloc[0]), "2024-01-03 00:00:00")
        self.assertIn("保守滞后", AkshareSource.availability_note)

    def test_yfinance_total_return_uses_adj_close(self) -> None:
        out = YfinanceSource(symbol_map={"SPY": 8}).normalize(YF_BARS_ADJ, currency="USD")
        validate_normalized(out, "bars_daily")
        self.assertEqual(str(out["return_kind"].iloc[0]), RETURN_KIND_TOTAL)
        self.assertAlmostEqual(float(out["close"].iloc[0]), 470.1, places=6)

    def test_yfinance_price_only_is_labelled_price_return(self) -> None:
        """F.6：只有 Close 时必须标为价格收益，**不得**冒充总收益。"""
        out = YfinanceSource(symbol_map={"SPY": 8}).normalize(YF_BARS_PRICE_ONLY)
        self.assertEqual(str(out["return_kind"].iloc[0]), RETURN_KIND_PRICE)

    def test_yfinance_fx_requires_explicit_direction(self) -> None:
        """F.6：报价方向决定是否取倒数，故必须显式给出 base/quote。"""
        with self.assertRaises(ContractError):
            YfinanceSource(kind="fx_rates").normalize(YF_FX)

    def test_yfinance_fx_normalize_satisfies_contract(self) -> None:
        out = YfinanceSource(kind="fx_rates").normalize(YF_FX, base="USD", quote="CNY")
        validate_normalized(out, "fx_rates")
        self.assertEqual((out["base"].iloc[0], out["quote"].iloc[0]), ("USD", "CNY"))

    def test_fred_normalize_satisfies_contract(self) -> None:
        out = FredSource(unit_map={"DGS10": "percent"}).normalize(FRED_RAW)
        validate_normalized(out, "macro_series")
        self.assertEqual(str(out["unit"].iloc[0]), "percent")
        self.assertTrue(out["available_utc"].notna().all())

    def test_fred_uses_vendor_available_utc_when_present(self) -> None:
        """供应商已给发布时刻时**优先采用**，不再套用假设缓冲。"""
        raw = FRED_RAW.assign(available_utc=["2024-01-03 13:00:00", "2024-01-04 13:00:00"])
        out = FredSource().normalize(raw)
        self.assertEqual(str(out["available_utc"].iloc[0]), "2024-01-03 13:00:00")


class TestFailClosedOnBadInput(unittest.TestCase):
    """空表 / 缺列 / 未配置代码 —— 一律显式报错，不得静默。"""

    def test_empty_frame_raises(self) -> None:
        with self.assertRaises(ContractError):
            AkshareSource(symbol_map={"510300": 1}).normalize(AKSHARE_RAW.iloc[0:0])

    def test_missing_required_column_raises(self) -> None:
        with self.assertRaises(ContractError) as ctx:
            AkshareSource(symbol_map={"510300": 1}).normalize(AKSHARE_RAW.drop(columns=["收盘"]))
        self.assertIn("收盘", str(ctx.exception))

    def test_unmapped_symbol_raises_instead_of_dropping_rows(self) -> None:
        """未配置的代码必须报错 —— 静默丢行会让「标的覆盖」检查失去意义。"""
        with self.assertRaises(ContractError) as ctx:
            AkshareSource(symbol_map={}).normalize(AKSHARE_RAW)
        self.assertIn("symbol_map", str(ctx.exception))

    def test_validate_rejects_null_available_utc(self) -> None:
        """契约校验器本身要能抓住 available_utc 为空（而非只看列在不在）。"""
        df = pd.DataFrame({
            "symbol_id": [1], "ts": [date(2024, 1, 2)],
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [0.0],
            "currency": ["CNY"], "close_utc": [pd.Timestamp("2024-01-02 07:00")],
            "available_utc": [None],
        })
        with self.assertRaises(ContractError) as ctx:
            validate_normalized(df, "bars_daily")
        self.assertIn("available_utc", str(ctx.exception))

    def test_validate_rejects_missing_contract_column(self) -> None:
        df = pd.DataFrame({"symbol_id": [1], "ts": [date(2024, 1, 2)]})
        with self.assertRaises(ContractError) as ctx:
            validate_normalized(df, "bars_daily")
        self.assertIn("缺少契约列", str(ctx.exception))

    def test_validate_rejects_unknown_dataset(self) -> None:
        with self.assertRaises(ContractError):
            validate_normalized(pd.DataFrame({"a": [1]}), "no_such_dataset")

    def test_every_dataset_needed_by_adapters_exists_in_contract(self) -> None:
        self.assertTrue({"bars_daily", "fx_rates", "macro_series"} <= set(CONTRACT))


if __name__ == "__main__":
    unittest.main(verbosity=2)
