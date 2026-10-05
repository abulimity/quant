"""AKShare 适配器骨架（境内 ETF 行情）—— `[VENDOR-TBD]`。

**本模块顶层不 import `akshare`**：SDK 只在 `fetch()` 内按需导入，
故「没装 SDK」不会导致 `import quantlab.ingest.adapters.akshare` 失败（V0）。

`fetch()` 未实现 → 抛 `NotImplementedError(VENDOR-TBD)`（V3），**不返回空表**。
`normalize()` 已实现，可用固定样本做契约测试。

**数据可用时间口径（F.3/F.6，必须披露）**：
AKShare 的 ETF 日线只给**日期**、没有「何时发布」。故采用**保守滞后**：
`available_utc = 交易日 + AVAILABILITY_LAG_DAYS`。这**不是**严格的 point-in-time 数据。
若原始数据自带可用时间列，则**优先使用它**（见 `_AVAIL_COL`）。
"""

from __future__ import annotations

import pandas as pd

from quantlab.ingest.adapters import _util
from quantlab.ingest.base import ContractError, FetchSpec, tbd, validate_normalized

NAME = "akshare"
DATASET = "bars_daily"

# AKShare 中文列名 → 契约列名
_COLUMN_MAP = {
    "日期": "ts", "开盘": "open", "最高": "high",
    "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount",
}
_REQUIRED_RAW = tuple(_COLUMN_MAP)

# 若供应商数据自带可用时间列，优先用它，不再走保守滞后
_AVAIL_COL = "可用时间"

AVAILABILITY_LAG_DAYS = 1
AVAILABILITY_NOTE = (
    "AKShare 日线仅有日期、无发布时间；按 F.6 采用保守滞后一日，"
    "available_utc = 交易日 + 1 天 00:00。属显式假设，不得称为严格 point-in-time。"
)

# 本地收盘时刻：A 股 15:00 CST(UTC+8) → 07:00 UTC
_CLOSE_UTC_OFFSET_HOURS = 7


class AkshareSource:
    """境内 ETF 日线。`symbol_map` 用供应商代码 → 内部 symbol_id。"""

    name = NAME
    dataset = DATASET
    availability_note = AVAILABILITY_NOTE

    def __init__(self, symbol_map: dict[str, int] | None = None, currency: str = "CNY") -> None:
        self.symbol_map = dict(symbol_map or {})
        self.currency = currency

    def fetch(self, spec: FetchSpec) -> pd.DataFrame:
        """**未实现**：需先配置供应商（`config/sources.toml`，见附录 A）。"""
        # 真实实现大致是：
        #     ak = import_module("akshare")     # 延迟导入，故顶层不会 ImportError
        #     raw = ak.fund_etf_hist_em(symbol=..., period="daily", ...)
        # 这里刻意不写死 —— 给出「看起来能用」的假实现比不写更危险。
        raise tbd(NAME, f"dataset={spec.dataset} 需先配置 config/sources.toml")

    def normalize(self, raw: pd.DataFrame) -> pd.DataFrame:
        """把 AKShare 原始表映射到 `bars_daily` 契约。"""
        _util.require_columns(raw, _REQUIRED_RAW, NAME)
        if len(raw) == 0:
            raise ContractError(f"{NAME}: normalize() 收到空表 —— 空表必须显式报错")

        code_col = "代码" if "代码" in raw.columns else "symbol"
        out = pd.DataFrame({
            "symbol_id": _util.map_symbols(raw[code_col], self.symbol_map, NAME),
            "ts": pd.to_datetime(raw["日期"]).dt.date,
            "open": pd.to_numeric(raw["开盘"]).astype("float64"),
            "high": pd.to_numeric(raw["最高"]).astype("float64"),
            "low": pd.to_numeric(raw["最低"]).astype("float64"),
            "close": pd.to_numeric(raw["收盘"]).astype("float64"),
            "volume": pd.to_numeric(raw["成交量"]).astype("float64"),
            "amount": pd.to_numeric(raw["成交额"]).astype("float64"),  # 原单位，未换算
        })
        out["currency"] = self.currency
        out["close_utc"] = _util.to_naive_utc(
            pd.to_datetime(out["ts"]) + pd.Timedelta(hours=_CLOSE_UTC_OFFSET_HOURS))
        out["available_utc"] = (
            _util.to_naive_utc(raw[_AVAIL_COL]) if _AVAIL_COL in raw.columns
            else _util.conservative_availability(out["ts"], AVAILABILITY_LAG_DAYS)
        )
        validate_normalized(out, self.dataset)
        return out
