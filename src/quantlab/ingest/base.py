"""Source 协议与契约校验（LOCAL_DEPLOYMENT_PLAN.md §P2.4）。

目的：把「换供应商」限定为「换一个 adapter」，并让**契约测试先行**。

    class Source(Protocol):
        name: str
        def fetch(self, spec: FetchSpec) -> pd.DataFrame: ...
        def normalize(self, raw: pd.DataFrame) -> pd.DataFrame: ...

两条纪律：

1. **延迟导入**：适配器模块顶层**不得** import 供应商 SDK。SDK 只在 `fetch()` 内部导入，
   这样「骨架可导入」与「没装 SDK」两件事互不干扰（V0）。
2. **失败要响**：未实现的入口一律抛 `NotImplementedError(VENDOR_TBD)`，
   **不得**返回空表或静默成功（V3）。

`normalize()` 的产出必须满足 `CONTRACT`：字段齐全、关键时序列非空。
`source` / `downloaded_at` / `snapshot_id` 由编排器（P2.5）统一补，不走适配器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd

# 供应商尚未配置时的统一报错锚点（测试与人工排查都以它为准）
VENDOR_TBD = "VENDOR-TBD"


class ContractError(ValueError):
    """适配器 `normalize()` 的产出不符合 P2.1 数据契约。"""


@dataclass(frozen=True)
class FetchSpec:
    """一次抓取的声明（编排器构造，适配器只读）。"""

    dataset: str                      # bars_daily / fx_rates / macro_series / ...
    start: date
    end: date
    symbols: tuple[str, ...] = ()     # 供应商侧的标识（ticker / 代码）
    params: dict = field(default_factory=dict)


@runtime_checkable
class Source(Protocol):
    """数据源协议。实现者需提供 `name`、`fetch()`、`normalize()`。"""

    name: str

    def fetch(self, spec: FetchSpec) -> pd.DataFrame:  # pragma: no cover - 协议声明
        ...

    def normalize(self, raw: pd.DataFrame) -> pd.DataFrame:  # pragma: no cover
        ...


# --------------------------------------------------------------------------- #
# 契约：每个数据集 normalize() **必须**产出的列
# --------------------------------------------------------------------------- #
# **不含** source / downloaded_at / snapshot_id —— 它们是「谁抓的、何时抓的、哪份快照」，
# 属编排器职责，不是供应商数据本身的属性。
CONTRACT: dict[str, tuple[str, ...]] = {
    "bars_daily": (
        "symbol_id", "ts", "open", "high", "low", "close", "volume",
        "currency", "close_utc", "available_utc",
    ),
    "corporate_actions": (
        "symbol_id", "ex_date", "kind", "ratio", "cash", "pay_date", "available_utc",
    ),
    "fx_rates": ("base", "quote", "ts", "rate", "available_utc"),
    "macro_series": ("series_id", "ts", "value", "unit", "available_utc"),
    "fundamentals": (
        "symbol_id", "period_end", "as_of_date", "item", "value", "available_utc",
    ),
    "symbols": (
        "symbol_id", "ticker", "exchange", "calendar", "currency",
        "isin", "lot_size", "listed_on", "delisted_on",
    ),
}

# 不得为空的关键时序列：缺了它们，防未来函数就无从谈起
REQUIRED_NON_NULL: dict[str, tuple[str, ...]] = {
    "bars_daily": ("symbol_id", "ts", "available_utc"),
    "corporate_actions": ("symbol_id", "ex_date", "kind", "available_utc"),
    "fx_rates": ("base", "quote", "ts", "rate", "available_utc"),
    "macro_series": ("series_id", "ts", "available_utc"),
    "fundamentals": ("symbol_id", "period_end", "as_of_date", "item", "available_utc"),
}


def validate_normalized(df: pd.DataFrame, dataset: str) -> None:
    """校验 `normalize()` 的产出是否符合契约；违反则抛 `ContractError`。

    三项检查（全是脚本化的，不靠目测）：
        1. 契约列**齐全**；
        2. 关键时序列**非空**（尤其 `available_utc`）；
        3. `available_utc` 确实是**时间**，不是字符串垃圾。
    """
    if dataset not in CONTRACT:
        raise ContractError(f"未知数据集: {dataset}（已知: {sorted(CONTRACT)}）")

    missing = [c for c in CONTRACT[dataset] if c not in df.columns]
    if missing:
        raise ContractError(f"{dataset}: normalize() 产出缺少契约列 {missing}")

    for col in REQUIRED_NON_NULL.get(dataset, ()):
        if df[col].isna().any():
            raise ContractError(f"{dataset}: 关键列 {col} 存在空值")

    if len(df) and "available_utc" in df.columns:
        try:
            pd.to_datetime(df["available_utc"])
        except (ValueError, TypeError) as exc:
            raise ContractError(f"{dataset}: available_utc 无法解析为时间: {exc}") from exc


def tbd(source_name: str, detail: str = "") -> NotImplementedError:
    """构造统一的「供应商待接入」异常。"""
    extra = f"（{detail}）" if detail else ""
    return NotImplementedError(
        f"{VENDOR_TBD}: 数据源 '{source_name}' 的抓取尚未实现{extra}。\n"
        f"处置：在 config/sources.toml 配置该供应商后，实现 fetch() 并补一组 "
        f"normalize() 契约测试（LOCAL_DEPLOYMENT_PLAN.md 附录 A）。\n"
        f"注意：不得返回空表或静默成功。"
    )
