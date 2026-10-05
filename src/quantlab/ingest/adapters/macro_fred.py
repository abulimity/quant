"""FRED 宏观适配器骨架（`macro_series`）—— `[VENDOR-TBD]`。

宏观在本平台是**一等数据**（P2.1），与行情同级维护、同样带 `available_utc` 与
`snapshot_id`，**不得**塞进行情表当附属列。

**可用时间口径（F.2，必须显式披露）**：
FRED 系列只给**观测期**（如 2024-01-01），**没有**「该值何时首次发布」的完整记录，
免费历史回填数据尤其如此。故按 F.2 记录**假设的发布时间缓冲**：

    available_utc = 观测期 + RELEASE_LAG_DAYS

该缓冲是**显式假设**，产物中必须标注；不得称为严格的 point-in-time 数据。
真实的发布日历（ALFRED / `release_dates`）留待供应商接入时替换。
"""

from __future__ import annotations

import pandas as pd

from quantlab.ingest.adapters import _util
from quantlab.ingest.base import ContractError, FetchSpec, tbd, validate_normalized

NAME = "macro_fred"
DATASET = "macro_series"

# 观测期结束到发布的**假设**缓冲（自然日）。各系列官方滞后不同，
# 这里取保守值；接入时应按系列分别配置。
RELEASE_LAG_DAYS = 30
AVAILABILITY_NOTE = (
    "FRED 只给观测期、无完整发布时间记录；按 F.2 记录**假设的发布缓冲**"
    f"（观测期 + {RELEASE_LAG_DAYS} 天）。属显式假设，不得称为严格 point-in-time。"
)

_RAW_REQUIRED = ("series_id", "ts", "value")


class FredSource:
    """FRED 宏观序列。`unit_map` 记录各系列的计量单位（报告需用）。"""

    name = NAME
    dataset = DATASET
    availability_note = AVAILABILITY_NOTE

    def __init__(self, unit_map: dict[str, str] | None = None) -> None:
        self.unit_map = dict(unit_map or {})

    def fetch(self, spec: FetchSpec) -> pd.DataFrame:
        """**未实现**：需先配置供应商与 API key（`config/sources.toml`，见附录 A）。"""
        # 真实实现大致是：
        #     fred = import_module("fredapi")    # 或 requests 直连 FRED API
        #     raw = ...（API key 走环境变量，**不得**写入仓库）
        raise tbd(NAME, f"dataset={spec.dataset} 需先配置 config/sources.toml 与 API key")

    def normalize(self, raw: pd.DataFrame) -> pd.DataFrame:
        """把 FRED 原始表映射到 `macro_series` 契约。"""
        _util.require_columns(raw, _RAW_REQUIRED, NAME)
        if len(raw) == 0:
            raise ContractError(f"{NAME}: normalize() 收到空表 —— 空表必须显式报错")

        out = pd.DataFrame({
            "series_id": raw["series_id"].astype(str),
            "ts": pd.to_datetime(raw["ts"]).dt.date,
            "value": pd.to_numeric(raw["value"]).astype("float64"),
            "unit": raw["series_id"].astype(str).map(self.unit_map).fillna("unknown"),
        })
        if "available_utc" in raw.columns:
            # 供应商若已给发布时刻，优先采用，不再套用假设缓冲
            out["available_utc"] = _util.to_naive_utc(raw["available_utc"])
        else:
            out["available_utc"] = _util.conservative_availability(out["ts"], RELEASE_LAG_DAYS)
        validate_normalized(out, self.dataset)
        return out
