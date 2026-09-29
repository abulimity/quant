"""yfinance 适配器骨架（美股 / 港股 / 汇率）—— `[VENDOR-TBD]`。

⚠️ **命名陷阱**：本模块名为 `yfinance`，与供应商包**同名**。因此 `import yfinance`
在本模块内是**语义歧义**的（可能被解析到自己）。所以 SDK 一律用
`import_module("yfinance")` 在 `fetch()` 内部按需导入 —— 既避开歧义，
也正好满足「延迟导入」的要求（V0）。

`normalize()` 支持两种输入（由 `_detect_kind` 判定）：
    · ETF/股票行情：含 `Open/High/Low/Close`  → `bars_daily`
    · 历史汇率：仅含 `Close`                  → `fx_rates`

**复权口径（F.6，必须明确披露）**：
yfinance 的 `Close` 是**未复权**价、`Adj Close` 是**已复权**价。
本骨架优先取 `Adj Close` 并标记 `total_return`；只有 `Close` 时标 `price_return`，
**不得**冒充总收益（F.6 原文：标为价格收益，不能冒充总收益）。
"""

from __future__ import annotations

from importlib import import_module

import pandas as pd

from quantlab.ingest.adapters import _util
from quantlab.ingest.base import ContractError, FetchSpec, tbd, validate_normalized

NAME = "yfinance"

OHLC_REQUIRED = ("Open", "High", "Low", "Close")

# F.6：复权口径必须显式标记，不能靠默认值蒙混
RETURN_KIND_TOTAL = "total_return"     # 来自 Adj Close
RETURN_KIND_PRICE = "price_return"     # 仅 Close

# yfinance 日线不含精确发布时间；按 F.6 对「缺发布时间」的保守处理披露
AVAILABILITY_LAG_DAYS = 1
AVAILABILITY_NOTE = (
    "yfinance 日线无发布时间；按 F.6 采用保守滞后一日披露可用时间。"
    "复权口径：优先 Adj Close 并标记 total_return；仅有 Close 时标记 price_return，"
    "不得冒充总收益。"
)


def _dates_index(raw: pd.DataFrame) -> pd.DatetimeIndex:
    """把「Date 列」或「索引」统一成 `DatetimeIndex`。

    注意：`Series` **没有** `.date`（要用 `.dt.date`），而 `DatetimeIndex` **有**。
    统一转成 `DatetimeIndex` 才能让 `index.date` 在两处都成立 —— 这个差别
    是实际踩到的（`AttributeError: 'Series' object has no attribute 'date'`）。
    """
    source = raw["Date"] if "Date" in raw.columns else raw.index
    return pd.DatetimeIndex(pd.to_datetime(source))


def _detect_kind(raw: pd.DataFrame) -> str:
    """判定输入是行情还是汇率。"""
    if all(c in raw.columns for c in OHLC_REQUIRED):
        return "bars_daily"
    if "Close" in raw.columns:
        return "fx_rates"
    raise ContractError(
        f"{NAME}: 无法识别输入类型；期望含 {list(OHLC_REQUIRED)}（行情）"
        f"或含 'Close'（汇率）。实际列: {list(raw.columns)}"
    )


class YfinanceSource:
    """美股/港股 ETF 行情与历史汇率。"""

    name = NAME
    availability_note = AVAILABILITY_NOTE

    def __init__(self, symbol_map: dict[str, int] | None = None,
                 kind: str | None = None) -> None:
        self.symbol_map = dict(symbol_map or {})
        self.kind = kind          # None = 自动判定

    def fetch(self, spec: FetchSpec) -> pd.DataFrame:
        """**未实现**：需先配置供应商（`config/sources.yaml`，见附录 A）。"""
        # 真实实现大致是：
        #     yf = import_module("yfinance")     # 按名导入，规避与本模块同名
        #     raw = yf.download(spec.symbols, start=..., end=..., auto_adjust=False)
        raise tbd(NAME, f"dataset={spec.dataset} 需先配置 config/sources.yaml")

    def normalize(self, raw: pd.DataFrame, *, currency: str = "USD",
                  base: str = "", quote: str = "") -> pd.DataFrame:
        """映射到契约。`kind` 可显式指定，否则自动判定。"""
        if len(raw) == 0:
            raise ContractError(f"{NAME}: normalize() 收到空表 —— 空表必须显式报错")

        kind = self.kind or _detect_kind(raw)
        if kind == "bars_daily":
            out = self._normalize_bars(raw, currency)
        elif kind == "fx_rates":
            out = self._normalize_fx(raw, base, quote)
        else:
            raise ContractError(f"{NAME}: 未知 kind={kind!r}")
        validate_normalized(out, kind)
        return out

    # ------------------------------------------------------------------ #
    def _normalize_bars(self, raw: pd.DataFrame, currency: str) -> pd.DataFrame:
        _util.require_columns(raw, OHLC_REQUIRED, NAME)
        index = _dates_index(raw)
        ticker_col = "Ticker" if "Ticker" in raw.columns else "symbol"
        if ticker_col not in raw.columns:
            raise ContractError(f"{NAME}: 行情数据缺少代码列（{ticker_col}）")

        if "Adj Close" in raw.columns:
            close = pd.to_numeric(raw["Adj Close"]).astype("float64")
            return_kind = RETURN_KIND_TOTAL
        else:
            close = pd.to_numeric(raw["Close"]).astype("float64")
            return_kind = RETURN_KIND_PRICE

        out = pd.DataFrame({
            "symbol_id": _util.map_symbols(raw[ticker_col], self.symbol_map, NAME),
            "ts": index.date,
            "open": pd.to_numeric(raw["Open"]).astype("float64"),
            "high": pd.to_numeric(raw["High"]).astype("float64"),
            "low": pd.to_numeric(raw["Low"]).astype("float64"),
            "close": close,
            "volume": pd.to_numeric(raw.get("Volume", pd.Series(0.0, index=raw.index)))
            .astype("float64"),
        })
        out["currency"] = currency
        # 免费接口不含精确收盘时刻 → 统一按保守滞后披露（F.6）
        out["close_utc"] = _util.conservative_availability(out["ts"], AVAILABILITY_LAG_DAYS)
        out["available_utc"] = _util.conservative_availability(out["ts"], AVAILABILITY_LAG_DAYS)
        out["return_kind"] = return_kind      # 供 P2.6 校验与报告披露
        return out

    def _normalize_fx(self, raw: pd.DataFrame, base: str, quote: str) -> pd.DataFrame:
        if not base or not quote:
            raise ContractError(
                f"{NAME}: 汇率归一必须显式给出 base/quote —— 报价方向决定是否取倒数（F.6）"
            )
        index = _dates_index(raw)
        price_col = "Adj Close" if "Adj Close" in raw.columns else "Close"
        out = pd.DataFrame({
            "base": base,
            "quote": quote,
            "ts": index.date,
            "rate": pd.to_numeric(raw[price_col]).astype("float64"),
        })
        out["available_utc"] = _util.conservative_availability(out["ts"], AVAILABILITY_LAG_DAYS)
        return out
