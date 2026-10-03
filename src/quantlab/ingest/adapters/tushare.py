"""Tushare 适配器（境内 ETF 名单 / 未复权日线 / 分红）—— LOCAL_DEPLOYMENT_PLAN.md §P2.4。

**本模块顶层不 import `tushare`**：SDK 只在 `fetch()` 内按需导入，故「没装 SDK」
不会导致 `import quantlab.ingest.adapters.tushare` 失败（V0）。

支持的数据集（`dataset` 构造参数 / `FetchSpec.dataset`）：
    · `symbols`            ← `fund_basic(market=E)`（境内 ETF 名单，含退市，排除 REITs）
    · `bars_daily`         ← `fund_daily`（**未复权**日线）
    · `corporate_actions`  ← `fund_div`（基金分红；拆分罕见，由复权因子兜底）

复权口径（F.6，必须披露）：
    · `fund_daily.close` 是**未复权**价；总收益由 `corporate_actions`（分红）经
      `quality/clean.py.total_return_index` 独立重算。
    · `fund_adj.adj_factor`（复权因子）是**独立交叉核对**用的原始数据，**不进契约表**
      （schema 无复权因子列）——它已在探针中确认可调通，F.6 对拍另立脚本处理。

单位声明（保留原单位，不静默换算，符合「Parquet 是真相」）：
    · `fund_daily.vol` 单位 = **手**（1 手 = 100 份），原样落入 `volume`；
      `amount`（千元）不落入契约（契约无成交额列）。
    · `fund_div.div_cash` 单位 = **每股税前现金（原币 CNY）**。

可用时间口径（F.2/F.6，必须披露）：
    · 日线无发布时刻 → `available_utc = 交易日 + 1 天 00:00`（保守滞后，非 point-in-time）。
    · 分红用 `ann_date`（公告日）作为「可得时间」，早于 `ex_date`（除权除息日），
      与 schema 注释「公司行动先公告、后除权」一致；缺 `ann_date` 时回退 `ex_date`。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from importlib import import_module

import pandas as pd

from quantlab.ingest.adapters import _util
from quantlab.ingest.base import CONTRACT, ContractError, FetchSpec, validate_normalized

NAME = "tushare"

DATASETS: tuple[str, ...] = ("symbols", "bars_daily", "corporate_actions")

# fund_basic 中需**排除**的基金类型（用户已确认：排除 REITs，其余全收）
_EXCLUDED_FUND_TYPES = ("REITs",)

# fund_div 中仅保留「已实施」的分红，排除预案/取消等未实际发生的行
_DIV_PROC_IMPLEMENTED = "实施"

# ts_code 后缀 → (exchange, calendar)；未识别后缀必须报错（fail-closed）
# 注意：exchange-calendars **没有** XSHE；沪、深共用同一 A 股交易日历，
# 故 .SZ 也映射到 XSHG（平台规范 exchange ∈ {XSHG, XHKG, XNYS}，见 schema.sql）。
# SH/SZ 的区分由 ticker 后缀（510300.SH / 159919.SZ）承载，不靠 exchange。
_CODE_SUFFIX_EXCHANGE: dict[str, tuple[str, str]] = {
    ".SH": ("XSHG", "XSHG"),
    ".SZ": ("XSHG", "XSHG"),
}

AVAILABILITY_LAG_DAYS = 1
AVAILABILITY_NOTE = (
    "Tushare 日线/名单仅给日期、无发布时刻；日线按 F.6 采用保守滞后一日，"
    "available_utc = 交易日 + 1 天 00:00，不得称为严格 point-in-time。"
    "分红用 ann_date（公告日）作为可得时间。"
    "volume 单位 = 手（1 手 = 100 份，未换算）；close 为未复权价。"
)

# A 股 15:00 CST(UTC+8) → 07:00 UTC
_CLOSE_UTC_OFFSET_HOURS = 7

# 境内 ETF 交易手 = 100 份（fund_basic 不提供 lot_size，按交易规则固定）
_CN_ETF_LOT_SIZE = 100

# 默认限速（秒/次）：5000 分限 500 次/分，取 ~300 次/分留重试余量
_DEFAULT_RATE_LIMIT_DELAY = 0.2


def assign_symbol_ids(raw_basic: pd.DataFrame) -> dict[str, int]:
    """由 fund_basic 名单分配**确定性**内部永久 ID。

    口径与 `_normalize_symbols` 一致：排除 REITs，按 ts_code 排序后依次编号 1..N。
    确定性 → 同一名单两次跑得同一 ID；新上市标的追加在尾部，旧 ID 不变（永久 ID 语义）。
    """
    non_reit = raw_basic[~raw_basic["fund_type"].isin(_EXCLUDED_FUND_TYPES)]
    return {code: i + 1 for i, code in enumerate(sorted(non_reit["ts_code"].astype(str)))}


def _exchange_pair(ts_code: str) -> tuple[str, str]:
    """把 ts_code 后缀解析为 (exchange, calendar)；未识别则报错。"""
    for suffix, pair in _CODE_SUFFIX_EXCHANGE.items():
        if ts_code.endswith(suffix):
            return pair
    raise ContractError(
        f"{NAME}: 无法识别 ts_code 后缀: {ts_code}（期望 .SH / .SZ）")


def _to_date(series: pd.Series) -> list:
    """把 'YYYYMMDD' 字符串列转成 date；空值/缺失转 None。"""
    out: list = []
    for v in series:
        if v is None or pd.isna(v) or str(v).strip() in ("", "nan", "None", "NaT"):
            out.append(None)
        else:
            out.append(pd.to_datetime(str(v).strip()).date())
    return out


def _corporate_available_utc(df: pd.DataFrame) -> list[datetime]:
    """分红「可得时间」= 公告日（ann_date），缺则回退除权日（ex_date）。

    取两者较早者，确保**绝不晚于**除权日（避免「先除权后公告」的未来函数）。
    """
    ex = pd.to_datetime(df["ex_date"])
    if "ann_date" in df.columns:
        ann = pd.to_datetime(df["ann_date"], errors="coerce")
    else:
        ann = pd.Series(pd.NaT, index=df.index)
    out: list[datetime] = []
    for i in range(len(df)):
        a = ann.iloc[i]
        e = ex.iloc[i]
        known = e if pd.isna(a) else min(a, e)
        out.append(datetime.combine(known.date(), datetime.min.time()))
    return out


class TushareSource:
    """境内 ETF 数据源。`symbol_map`：tushare 代码（如 510300.SH）→ 内部 symbol_id。"""

    name = NAME
    availability_note = AVAILABILITY_NOTE

    def __init__(
        self,
        dataset: str | None = None,
        symbol_map: dict[str, int] | None = None,
        *,
        currency: str = "CNY",
        token: str | None = None,
        rate_limit_delay: float = _DEFAULT_RATE_LIMIT_DELAY,
    ) -> None:
        if dataset is not None and dataset not in DATASETS:
            raise ContractError(
                f"{NAME}: 未知数据集 {dataset!r}；可用: {list(DATASETS)}")
        self.dataset = dataset
        self.symbol_map = dict(symbol_map or {})
        self.currency = currency
        self.token = token
        self.rate_limit_delay = rate_limit_delay

    # ------------------------------------------------------------------ #
    # fetch
    # ------------------------------------------------------------------ #
    def _pro(self):
        token = self.token or os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise ContractError(
                f"{NAME}: 未提供 TUSHARE_TOKEN（环境变量 TUSHARE_TOKEN 或构造参数）。"
                f"凭据一律走环境变量，不得写入仓库。")
        ts = import_module("tushare")     # 延迟导入（V0）
        return ts.pro_api(token)

    def _throttle(self) -> None:
        if self.rate_limit_delay:
            time.sleep(self.rate_limit_delay)

    def fetch(self, spec: FetchSpec) -> pd.DataFrame:
        """按 `dataset` 分发到各 tushare 接口，返回**原始** DataFrame。"""
        dataset = self.dataset or spec.dataset
        if dataset not in DATASETS:
            raise ContractError(
                f"{NAME}: 不支持数据集 {dataset!r}；可用: {list(DATASETS)}")

        pro = self._pro()
        if dataset == "symbols":
            return self._fetch_symbols(pro)
        if dataset == "bars_daily":
            return self._fetch_per_symbol(pro, spec, "fund_daily", allow_empty=False)
        if dataset == "corporate_actions":
            return self._fetch_per_symbol(pro, spec, "fund_div", allow_empty=True)
        raise AssertionError("unreachable")

    def _fetch_symbols(self, pro) -> pd.DataFrame:
        # 默认 market=E 已含 L(上市中)+D(退市)（实测 2251+678）；无需再按 status 分次
        raw = pro.fund_basic(market="E")
        self._throttle()
        if raw is None or len(raw) == 0:
            raise ContractError(f"{NAME}: fund_basic(market=E) 返回空")
        return raw

    def _fetch_per_symbol(self, pro, spec: FetchSpec, api: str, *, allow_empty: bool):
        if not spec.symbols:
            raise ContractError(f"{NAME}: {api} 需要指定标的（spec.symbols）")
        start = spec.start.strftime("%Y%m%d")
        end = spec.end.strftime("%Y%m%d")
        fn = getattr(pro, api)

        parts: list[pd.DataFrame] = []
        for code in spec.symbols:
            if api == "fund_daily":
                raw = fn(ts_code=code, start_date=start, end_date=end)
            else:  # fund_div：一次取全量，再按窗口裁剪到 ex_date <= end
                raw = fn(ts_code=code)
                if raw is not None and len(raw) and "ex_date" in raw.columns:
                    raw = raw[raw["ex_date"] <= end]
            if raw is not None and len(raw):
                parts.append(raw)
            self._throttle()

        if not parts:
            if allow_empty:
                # 分红可为空（多数 ETF 从未分红）；返回无列空表，normalize 会产出空契约表
                return pd.DataFrame()
            raise ContractError(f"{NAME}: {api} 对所有标的返回空")
        return pd.concat(parts, ignore_index=True)

    # ------------------------------------------------------------------ #
    # normalize
    # ------------------------------------------------------------------ #
    def normalize(self, raw: pd.DataFrame) -> pd.DataFrame:
        """按 `dataset` 映射到契约；产出通过 `validate_normalized`。"""
        dataset = self.dataset
        if dataset is None:
            raise ContractError(f"{NAME}: normalize() 需先指定 dataset（构造参数）")
        if dataset == "symbols":
            out = self._normalize_symbols(raw)
        elif dataset == "bars_daily":
            out = self._normalize_bars(raw)
        elif dataset == "corporate_actions":
            out = self._normalize_corporate_actions(raw)
        else:
            raise ContractError(f"{NAME}: 未知数据集 {dataset!r}")
        validate_normalized(out, dataset)
        return out

    def _normalize_symbols(self, raw: pd.DataFrame) -> pd.DataFrame:
        _util.require_columns(raw, ("ts_code", "fund_type", "list_date"), NAME)
        if len(raw) == 0:
            raise ContractError(f"{NAME}: symbols 原始表为空")

        df = raw[~raw["fund_type"].isin(_EXCLUDED_FUND_TYPES)].copy()
        if len(df) == 0:
            raise ContractError(f"{NAME}: 排除 {_EXCLUDED_FUND_TYPES} 后名单为空")

        pairs = [_exchange_pair(str(c)) for c in df["ts_code"]]
        out = pd.DataFrame({
            "symbol_id": _util.map_symbols(df["ts_code"], self.symbol_map, NAME),
            "ticker": df["ts_code"].astype(str),
            "exchange": [p[0] for p in pairs],
            "calendar": [p[1] for p in pairs],
            "currency": self.currency,
            "isin": None,
            "lot_size": _CN_ETF_LOT_SIZE,
            "listed_on": _to_date(df["list_date"]),
            "delisted_on": _to_date(df["delist_date"]) if "delist_date" in df.columns else None,
        })
        return out

    def _normalize_bars(self, raw: pd.DataFrame) -> pd.DataFrame:
        _util.require_columns(
            raw, ("ts_code", "trade_date", "open", "high", "low", "close", "vol"), NAME)
        if len(raw) == 0:
            raise ContractError(f"{NAME}: bars 原始表为空")

        out = pd.DataFrame({
            "symbol_id": _util.map_symbols(raw["ts_code"], self.symbol_map, NAME),
            "ts": pd.to_datetime(raw["trade_date"]).dt.date,
            "open": pd.to_numeric(raw["open"]).astype("float64"),
            "high": pd.to_numeric(raw["high"]).astype("float64"),
            "low": pd.to_numeric(raw["low"]).astype("float64"),
            "close": pd.to_numeric(raw["close"]).astype("float64"),
            "volume": pd.to_numeric(raw["vol"]).astype("float64"),   # 手，未换算
        })
        out["currency"] = self.currency
        out["close_utc"] = _util.to_naive_utc(
            pd.to_datetime(out["ts"]) + pd.Timedelta(hours=_CLOSE_UTC_OFFSET_HOURS))
        out["available_utc"] = _util.conservative_availability(out["ts"], AVAILABILITY_LAG_DAYS)
        return out

    def _normalize_corporate_actions(self, raw: pd.DataFrame) -> pd.DataFrame:
        cols = list(CONTRACT["corporate_actions"])
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=cols)      # 无分红是合法结果

        _util.require_columns(raw, ("ts_code", "ex_date", "div_cash"), NAME)
        df = raw.copy()
        if "div_proc" in df.columns:
            df = df[df["div_proc"].astype(str).str.strip() == _DIV_PROC_IMPLEMENTED]
        # 实测 fund_div 对同一 (ts_code, ex_date) 系统性地重复两行 → 去重
        df = df.drop_duplicates(subset=["ts_code", "ex_date"], keep="first")
        if len(df) == 0:
            return pd.DataFrame(columns=cols)

        out = pd.DataFrame({
            "symbol_id": _util.map_symbols(df["ts_code"], self.symbol_map, NAME),
            "ex_date": pd.to_datetime(df["ex_date"]).dt.date,
            "kind": "dividend",
            "ratio": None,
            "cash": pd.to_numeric(df["div_cash"]).astype("float64"),
            "pay_date": _to_date(df["pay_date"]) if "pay_date" in df.columns else None,
            "available_utc": _corporate_available_utc(df),
        })
        return out
