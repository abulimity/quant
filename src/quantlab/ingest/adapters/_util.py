"""适配器共用小工具。

只放**可复用**的纯函数：列校验、时间归一、标的映射。**不放**任何联网或 SDK 逻辑。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from quantlab.ingest.base import ContractError


def require_columns(df: pd.DataFrame, columns: tuple[str, ...] | list[str], source: str) -> None:
    """供应商原始数据必须包含这些列；缺了就直接报错，别让下游去猜。"""
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ContractError(
            f"{source}: 原始数据缺少必需列 {missing}；实际列 {list(df.columns)}"
        )


def to_naive_utc(value) -> pd.Series | pd.Timestamp:
    """转成 **naive UTC**（与 schema 口径一致）。tz-aware 先转 UTC 再去时区。"""
    if isinstance(value, pd.Series):
        ts = pd.to_datetime(value, errors="raise")
        if getattr(ts.dt, "tz", None) is not None:
            ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
        return ts
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


def conservative_availability(dates, lag_days: int = 1) -> list[datetime]:
    """F.6：**只有日频数据且缺发布时间**时，采用保守滞后，并**披露该假设**。

    返回「交易/观测日 + lag_days 的 00:00」。调用方须把 `availability_note`
    一并写入证据，不得把结果称为严格的 point-in-time。
    """
    out = []
    for d in dates:
        ts = d if isinstance(d, date) else pd.Timestamp(d).date()
        out.append(datetime.combine(ts + timedelta(days=lag_days), datetime.min.time()))
    return out


def map_symbols(tickers, mapping: dict[str, int], source: str) -> list[int]:
    """把供应商侧代码映射为**内部永久 ID**；未配置的代码必须报错，不得静默丢行。"""
    out: list[int] = []
    unknown: list[str] = []
    for ticker in tickers:
        key = str(ticker)
        if key not in mapping:
            unknown.append(key)
            continue
        out.append(int(mapping[key]))
    if unknown:
        raise ContractError(
            f"{source}: 以下代码未在 symbol_map 中配置内部 ID: {sorted(set(unknown))}。\n"
            f"处置：在 config/sources.yaml 的标的映射中补齐；**不得**静默丢弃这些行。"
        )
    return out
