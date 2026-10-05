"""futu 适配器（core 侧 normalize）—— LOCAL_DEPLOYMENT_PLAN.md 附录 A。

futu 抓取**不在 core**：futu-api SDK 只在 `envs/futu`，OpenD 需常驻，且有两道限额
（60 请求/30s、100 只/7 天），故抓取保留 `envs/futu/fetch_history_kline.py` 手动跑。
本适配器只做「原始 bronze → `bars_daily` 契约」的 normalize，**不 import futu SDK、不联网**。

原始 bronze `kline.parquet` 列（`request_history_kline(autype=NONE)` 原样）：
    code, name, time_key, open, close, high, low, volume, turnover,
    k_type, last_close, pe_ratio, turnover_rate

口径（F.6 披露）：
    · futu 代码 `HK.00700` → tushare ticker `00700.HK` → 内部 symbol_id（symbol_map）。
    · currency = HKD（futu 抓取 universe 为 HK.Fund，港币计价；多币种柜台留后续）。
    · close_utc = ts + 8h（XHKG 16:00 HKT = 08:00 UTC）。
    · available_utc = ts + 1 天 00:00（保守滞后，futu 日线无发布时刻）。
    · volume 单位 = 股（futu 原样；与 tushare A 股的「手」不同，勿混）。
    · amount = turnover（成交额，HKD 原样，未换算）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quantlab.ingest.adapters import _util
from quantlab.ingest.base import ContractError, FetchSpec, validate_normalized

NAME = "futu"

AVAILABILITY_NOTE = (
    "futu 日线无发布时刻；available_utc = 交易日 + 1 天 00:00（保守滞后）。"
    "close_utc = ts + 8h（XHKG 16:00 HKT=08:00 UTC）。volume 单位 = 股（未换算）；"
    "amount = turnover（成交额 HKD，未换算）。"
    "代码映射 HK.XXXXX → XXXXX.HK（tushare ts_code）。"
)

_CLOSE_UTC_OFFSET_HOURS = 8
_AVAILABILITY_LAG_DAYS = 1
_RAW_REQUIRED = ("code", "time_key", "open", "high", "low", "close", "volume", "turnover")


def futu_code_to_ticker(code: str) -> str:
    """futu 代码 → tushare ticker（当前仅支持 HK：`HK.00700` → `00700.HK`）。"""
    market, sep, num = str(code).partition(".")
    if not sep or not num:
        raise ContractError(f"{NAME}: 无法解析 code {code!r}（期望 <市场>.<代码>）")
    if market == "HK":
        return f"{num}.HK"
    raise ContractError(f"{NAME}: 暂不支持市场 {market!r}（当前仅 HK）: {code}")


class FutuSource:
    """港股行情源：读原始 bronze（不联网）→ normalize `bars_daily`。"""

    name = NAME
    availability_note = AVAILABILITY_NOTE

    def __init__(
        self,
        symbol_map: dict[str, int] | None = None,
        *,
        currency: str = "HKD",
        raw_bronze_dir: str | Path | None = None,
    ) -> None:
        self.symbol_map = dict(symbol_map or {})
        self.currency = currency
        self.raw_bronze_dir = raw_bronze_dir

    def fetch(self, spec: FetchSpec | None = None) -> pd.DataFrame:
        """读原始 bronze `kline.parquet`（本适配器不联网）。"""
        if self.raw_bronze_dir is None:
            raise ContractError(
                f"{NAME}: 未指定原始 bronze 目录（raw_bronze_dir）；"
                f"抓取需由 envs/futu 完成，本适配器只读其产物。")
        path = Path(self.raw_bronze_dir) / "kline.parquet"
        if not path.is_file():
            raise ContractError(f"{NAME}: 原始 bronze 缺 kline.parquet: {path}")
        return pd.read_parquet(path)

    def normalize(self, raw: pd.DataFrame) -> pd.DataFrame:
        """futu 原始 kline → `bars_daily` 契约。"""
        _util.require_columns(raw, _RAW_REQUIRED, NAME)
        if len(raw) == 0:
            raise ContractError(f"{NAME}: normalize() 收到空表 —— 空表必须显式报错")

        tickers = [futu_code_to_ticker(c) for c in raw["code"]]
        ts = pd.to_datetime(raw["time_key"]).dt.date

        out = pd.DataFrame({
            "symbol_id": _util.map_symbols(tickers, self.symbol_map, NAME),
            "ts": ts,
            "open": pd.to_numeric(raw["open"]).astype("float64"),
            "high": pd.to_numeric(raw["high"]).astype("float64"),
            "low": pd.to_numeric(raw["low"]).astype("float64"),
            "close": pd.to_numeric(raw["close"]).astype("float64"),
            "volume": pd.to_numeric(raw["volume"]).astype("float64"),    # 股，未换算
            "amount": pd.to_numeric(raw["turnover"]).astype("float64"),  # 成交额 HKD，未换算
        })
        out["currency"] = self.currency
        out["close_utc"] = _util.to_naive_utc(
            pd.to_datetime(out["ts"]) + pd.Timedelta(hours=_CLOSE_UTC_OFFSET_HOURS))
        out["available_utc"] = _util.conservative_availability(
            out["ts"], _AVAILABILITY_LAG_DAYS)
        validate_normalized(out, "bars_daily")
        return out
