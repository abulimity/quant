"""Tushare 适配器（境内 ETF 名单 / 未复权日线 / 分红）—— LOCAL_DEPLOYMENT_PLAN.md §P2.4。

**本模块顶层不 import `tushare`**：SDK 只在 `fetch()` 内按需导入，故「没装 SDK」
不会导致 `import quantlab.ingest.adapters.tushare` 失败（V0）。

支持的数据集（`dataset` 构造参数 / `FetchSpec.dataset`）：
    · `symbols`            ← `fund_basic(market=E)`（境内 ETF 名单，含退市，排除 REITs）
    · `bars_daily`         ← `fund_daily`（**未复权**日线）
    · `corporate_actions`  ← `fund_div`（基金分红；拆分罕见，由复权因子兜底）
    · `fund_adj`           ← `fund_adj`（复权因子，**非契约** raw 表，F.6 交叉核对）
    · `index_symbols`      ← `index_basic`（指数名单，**非契约** raw 表）
    · `index_daily`        ← `index_daily`（基准指数日线，**非契约** raw 表）
    · `hk_symbols`         ← `hk_basic`（港股证券名单，**非契约** raw 表；行情走 futu）
    · `macro_series`       ← `cn_cpi`/`cn_ppi`/`cn_gdp`/`shibor`（契约 `macro_series`）

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
from datetime import date, datetime, timedelta
from importlib import import_module

import pandas as pd

from quantlab.ingest.adapters import _util
from quantlab.ingest.base import CONTRACT, ContractError, FetchSpec, validate_normalized

NAME = "tushare"

DATASETS: tuple[str, ...] = (
    "symbols", "bars_daily", "corporate_actions", "fund_adj",
    "index_symbols", "index_daily", "hk_symbols", "macro_series",
)

# 非契约的「原始 bronze 表」：normalize() **不做** CONTRACT 校验（schema 无对应契约列）。
# fund_adj 是 F.6 复权因子交叉核对；index_*/hk_* 是名单/基准序列，供下游按需取用。
_RAW_DATASETS: frozenset[str] = frozenset(
    {"fund_adj", "index_symbols", "index_daily", "hk_symbols"})

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

# fund_daily 空表重试：tushare 偶发对「窗口内应有数据」的标的返回**空表**（不抛异常），
# 与「真无数据」（退市/窗口外上市）从单次结果无法区分。为避免静默丢标的，对空表做
# 少量重试再接受，把「瞬时空表」与「真缺口」的边界后移，不掩盖真缺口（对账仍以
# exchange-calendars 应取交易日为准，见 LOCAL_DEPLOYMENT_PLAN.md §六）。
_FUND_DAILY_EMPTY_ATTEMPTS = 2
_FUND_DAILY_EMPTY_DELAY = 0.5

# 宏观最小集（LOCAL_DEPLOYMENT_PLAN.md §一：cn_cpi/cn_ppi/cn_gdp/shibor）。
# 每个元素：(series_id, unit, tushare接口, 取值列, 时间列, 时间格式, 发布滞后天数)。
# 发布滞后用于 available_utc = 观测期末 + 滞后天（F.2 保守口径，披露如下）：
#   · CPI/PPI 月频：统计局次月中旬发布 → 滞后 15 天
#   · GDP 季频：次季首月末发布 → 滞后 30 天
#   · shibor 日频：当日收盘可得 → 滞后 1 天
_MACRO_SERIES: tuple[tuple[str, str, str, str, str, str, int], ...] = (
    ("CN_CPI_YOY",   "percent", "cn_cpi",  "nt_yoy",  "month",   "month",   15),
    ("CN_PPI_YOY",   "percent", "cn_ppi",  "ppi_yoy", "month",   "month",   15),
    ("CN_GDP_YOY",   "percent", "cn_gdp",  "gdp_yoy", "quarter", "quarter", 30),
    ("CN_SHIBOR_3M", "percent", "shibor",  "3m",      "date",    "day",      1),
)

# shibor 单次最多返回 2000 行（日频 ~8 年）；研究窗口 10 年需向前翻页补齐。
_SHIBOR_PAGE_CAP = 2000


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
        if dataset == "fund_adj":
            return self._fetch_per_symbol(pro, spec, "fund_adj", allow_empty=False)
        if dataset == "index_symbols":
            return self._fetch_index_basic(pro)
        if dataset == "index_daily":
            return self._fetch_per_symbol(pro, spec, "index_daily", allow_empty=False)
        if dataset == "hk_symbols":
            return self._fetch_hk_basic(pro)
        if dataset == "macro_series":
            return self._fetch_macro(pro, spec)
        raise AssertionError("unreachable")

    def _fetch_symbols(self, pro) -> pd.DataFrame:
        # 默认 market=E 已含 L(上市中)+D(退市)（实测 2251+678）；无需再按 status 分次
        raw = pro.fund_basic(market="E")
        self._throttle()
        if raw is None or len(raw) == 0:
            raise ContractError(f"{NAME}: fund_basic(market=E) 返回空")
        return raw

    def _fetch_index_basic(self, pro) -> pd.DataFrame:
        raw = pro.index_basic()
        self._throttle()
        if raw is None or len(raw) == 0:
            raise ContractError(f"{NAME}: index_basic() 返回空")
        return raw

    def _fetch_hk_basic(self, pro) -> pd.DataFrame:
        raw = pro.hk_basic()
        self._throttle()
        if raw is None or len(raw) == 0:
            raise ContractError(f"{NAME}: hk_basic() 返回空")
        return raw

    def _call_with_retry(self, fn, code: str, *, attempts: int = 4, base_delay: float = 1.0):
        """单次 tushare 调用做**有限**重试（网络/瞬时限流），最终失败**重抛**。

        只对「这次调用」重试，不掩盖数据缺口：重试耗尽仍失败 → 抛错让整批回填
        可见地终止（而不是静默丢某只标的的 bars，制造日历缺口）。
        """
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < attempts - 1:
                    time.sleep(base_delay * (2 ** attempt))
        raise ContractError(
            f"{NAME}: 标的 {code} 拉取失败（重试 {attempts} 次仍失败）: "
            f"{type(last_exc).__name__}: {last_exc}") from last_exc

    def _fetch_fund_daily(self, fn, code: str, start: str, end: str):
        """拉取单只 `fund_daily`：异常重试交给 `_call_with_retry`，空表另做少量重试。

        实测 tushare 偶发对「窗口内应有数据」的标的返回**空 DataFrame**（不抛异常），
        与「真无数据」（退市/窗口外上市）从单次结果无法区分。为避免静默丢标的，空表
        最多重试 `_FUND_DAILY_EMPTY_ATTEMPTS` 次，仍空则接受为真无数据（不掩盖真缺口，
        对账以 exchange-calendars 应取交易日为准）。
        """
        def call():
            return fn(ts_code=code, start_date=start, end_date=end)

        raw = None
        for attempt in range(_FUND_DAILY_EMPTY_ATTEMPTS):
            raw = self._call_with_retry(call, code=code)
            if raw is not None and len(raw):
                return raw
            if attempt < _FUND_DAILY_EMPTY_ATTEMPTS - 1:
                time.sleep(_FUND_DAILY_EMPTY_DELAY)
        return raw

    def _fetch_per_symbol(self, pro, spec: FetchSpec, api: str, *, allow_empty: bool):
        if not spec.symbols:
            raise ContractError(f"{NAME}: {api} 需要指定标的（spec.symbols）")
        start = spec.start.strftime("%Y%m%d")
        end = spec.end.strftime("%Y%m%d")
        fn = getattr(pro, api)

        parts: list[pd.DataFrame] = []
        total = len(spec.symbols)
        for i, code in enumerate(spec.symbols, 1):
            if api in ("fund_daily", "fund_adj", "index_daily"):
                # 逐标的日线：fund_daily/fund_adj/index_daily 均按 (ts_code, 日期窗口) 取，
                # 且都可能偶发瞬时空表 → 统一走空表重试
                raw = self._fetch_fund_daily(fn, code, start, end)
            else:  # fund_div：一次取全量，再裁剪到 [start, end]（与 bars 同窗口）
                raw = self._call_with_retry(lambda: fn(ts_code=code), code=code)
                if raw is not None and len(raw) and "ex_date" in raw.columns:
                    raw = raw[(raw["ex_date"] >= start) & (raw["ex_date"] <= end)]
            if raw is not None and len(raw):
                parts.append(raw)
            self._throttle()
            if total >= 100 and (i % 100 == 0 or i == total):
                print(f"  [tushare:{api}] {i}/{total}", flush=True)

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
        """按 `dataset` 映射到契约；契约数据集产出通过 `validate_normalized`。"""
        dataset = self.dataset
        if dataset is None:
            raise ContractError(f"{NAME}: normalize() 需先指定 dataset（构造参数）")
        if dataset == "symbols":
            out = self._normalize_symbols(raw)
        elif dataset == "bars_daily":
            out = self._normalize_bars(raw)
        elif dataset == "corporate_actions":
            out = self._normalize_corporate_actions(raw)
        elif dataset == "fund_adj":
            out = self._normalize_fund_adj(raw)
        elif dataset == "index_symbols":
            out = self._normalize_index_symbols(raw)
        elif dataset == "index_daily":
            out = self._normalize_index_daily(raw)
        elif dataset == "hk_symbols":
            out = self._normalize_hk_symbols(raw)
        elif dataset == "macro_series":
            out = self._normalize_macro(raw)
        else:
            raise ContractError(f"{NAME}: 未知数据集 {dataset!r}")
        if dataset not in _RAW_DATASETS:
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

    # ------------------------------------------------------------------ #
    # 复权因子 / 指数 / 港股（非契约 raw 表）
    # ------------------------------------------------------------------ #
    def _normalize_fund_adj(self, raw: pd.DataFrame) -> pd.DataFrame:
        _util.require_columns(raw, ("ts_code", "trade_date", "adj_factor"), NAME)
        if len(raw) == 0:
            return pd.DataFrame(columns=["symbol_id", "ts", "adj_factor"])
        return pd.DataFrame({
            "symbol_id": _util.map_symbols(raw["ts_code"], self.symbol_map, NAME),
            "ts": pd.to_datetime(raw["trade_date"]).dt.date,
            "adj_factor": pd.to_numeric(raw["adj_factor"]).astype("float64"),
        })

    def _normalize_index_symbols(self, raw: pd.DataFrame) -> pd.DataFrame:
        _util.require_columns(raw, ("ts_code", "name", "market", "publisher",
                                    "category", "base_date", "base_point", "list_date"), NAME)
        if len(raw) == 0:
            return pd.DataFrame(columns=[
                "ts_code", "name", "market", "publisher", "category",
                "base_date", "base_point", "list_date"])
        return pd.DataFrame({
            "ts_code": raw["ts_code"].astype(str),
            "name": raw["name"].astype(str),
            "market": raw["market"].astype(str),
            "publisher": raw["publisher"].astype(str),
            "category": raw["category"].astype(str),
            "base_date": _to_date(raw["base_date"]),
            "base_point": pd.to_numeric(raw["base_point"]).astype("float64"),
            "list_date": _to_date(raw["list_date"]),
        })

    def _normalize_index_daily(self, raw: pd.DataFrame) -> pd.DataFrame:
        _util.require_columns(raw, ("ts_code", "trade_date", "open", "high", "low",
                                    "close", "pre_close", "vol", "amount"), NAME)
        if len(raw) == 0:
            return pd.DataFrame(columns=[
                "ts_code", "ts", "open", "high", "low", "close", "pre_close",
                "change", "pct_chg", "volume", "amount"])
        return pd.DataFrame({
            "ts_code": raw["ts_code"].astype(str),
            "ts": pd.to_datetime(raw["trade_date"]).dt.date,
            "open": pd.to_numeric(raw["open"]).astype("float64"),
            "high": pd.to_numeric(raw["high"]).astype("float64"),
            "low": pd.to_numeric(raw["low"]).astype("float64"),
            "close": pd.to_numeric(raw["close"]).astype("float64"),
            "pre_close": pd.to_numeric(raw["pre_close"]).astype("float64"),
            "change": pd.to_numeric(raw["change"]).astype("float64"),
            "pct_chg": pd.to_numeric(raw["pct_chg"]).astype("float64"),
            "volume": pd.to_numeric(raw["vol"]).astype("float64"),     # 手（未换算）
            "amount": pd.to_numeric(raw["amount"]).astype("float64"),  # 千元（未换算）
        })

    def _normalize_hk_symbols(self, raw: pd.DataFrame) -> pd.DataFrame:
        _util.require_columns(raw, ("ts_code", "name", "market", "list_status",
                                    "list_date", "trade_unit", "isin", "curr_type"), NAME)
        if len(raw) == 0:
            return pd.DataFrame(columns=[
                "ts_code", "name", "market", "list_status", "list_date",
                "delist_date", "trade_unit", "isin", "curr_type"])
        return pd.DataFrame({
            "ts_code": raw["ts_code"].astype(str),
            "name": raw["name"].astype(str),
            "market": raw["market"].astype(str),
            "list_status": raw["list_status"].astype(str),
            "list_date": _to_date(raw["list_date"]),
            "delist_date": _to_date(raw["delist_date"]) if "delist_date" in raw.columns else None,
            "trade_unit": pd.to_numeric(raw["trade_unit"], errors="coerce").astype("float64"),
            "isin": raw["isin"].astype(str),
            "curr_type": raw["curr_type"].astype(str),
        })

    # ------------------------------------------------------------------ #
    # 宏观（契约 macro_series）
    # ------------------------------------------------------------------ #
    def _fetch_macro(self, pro, spec: FetchSpec) -> pd.DataFrame:
        """按 `_MACRO_SERIES` 逐接口拉取，规整为 (series_id, ts, value, unit)。

        cn_cpi/cn_ppi/cn_gdp 单次返回全量历史（行数少），裁剪到 [start, end]；
        shibor 日频受 2000 行上限约束，需向前翻页补齐。
        """
        start, end = spec.start, spec.end
        rows: list[dict] = []
        for series_id, unit, api, value_col, ts_col, ts_kind, _lag in _MACRO_SERIES:
            fn = getattr(pro, api)
            if api == "shibor":
                raw = self._fetch_shibor_paged(fn, start, end)
            else:
                raw = self._call_with_retry(lambda: fn(), code=series_id)
                self._throttle()
            if raw is None or len(raw) == 0:
                continue
            for _, r in raw.iterrows():
                ts = self._macro_ts(r[ts_col], ts_kind)
                if ts is None or ts < start or ts > end:
                    continue
                v = r[value_col]
                if v is None or pd.isna(v):
                    continue
                rows.append({"series_id": series_id, "ts": ts,
                             "value": float(v), "unit": unit})
        if not rows:
            raise ContractError(f"{NAME}: 宏观接口全部返回空（窗口 {start}..{end}）")
        return pd.DataFrame(rows)

    def _fetch_shibor_paged(self, fn, start: date, end: date) -> pd.DataFrame:
        """shibor 单次 2000 行上限（日频 ~8 年）；研究窗口 10 年需向前翻页补齐。"""
        parts: list[pd.DataFrame] = []
        earliest = start.strftime("%Y%m%d")
        cur_end = end.strftime("%Y%m%d")
        while True:
            raw = self._call_with_retry(
                lambda: fn(start_date=earliest, end_date=cur_end), code="shibor")
            self._throttle()
            if raw is None or len(raw) == 0:
                break
            parts.append(raw)
            mn = str(raw["date"].min())
            if mn <= earliest:
                break
            # 继续向前翻页：把「已取到的最早日」的前一天作为新的上界
            cur_end = (pd.to_datetime(mn) - pd.Timedelta(days=1)).strftime("%Y%m%d")
        if not parts:
            return pd.DataFrame()
        out = pd.concat(parts, ignore_index=True).drop_duplicates(subset=["date"])
        return out.sort_values("date").reset_index(drop=True)

    @staticmethod
    def _macro_ts(raw, kind: str) -> date | None:
        """把宏观的时间列转成观测日（月频→月末，季频→季末，日频→当日）。"""
        s = str(raw).strip()
        if not s or s.lower() in ("nan", "none", "nat"):
            return None
        if kind == "month":
            d = pd.to_datetime(s, format="%Y%m")
            return (d + pd.offsets.MonthEnd(1)).date()
        if kind == "quarter":
            y, q = s.split("Q")
            month = int(q) * 3
            d = pd.Timestamp(year=int(y), month=month, day=1)
            return (d + pd.offsets.MonthEnd(1)).date()
        if kind == "day":
            return pd.to_datetime(s).date()
        raise ContractError(f"{NAME}: 未知宏观时间格式 {kind!r}")

    def _normalize_macro(self, raw: pd.DataFrame) -> pd.DataFrame:
        cols = list(CONTRACT["macro_series"])
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=cols)
        _util.require_columns(raw, ("series_id", "ts", "value", "unit"), NAME)
        lag = {s[0]: s[6] for s in _MACRO_SERIES}
        out = pd.DataFrame({
            "series_id": raw["series_id"].astype(str),
            "ts": pd.to_datetime(raw["ts"]).dt.date,
            "value": pd.to_numeric(raw["value"]).astype("float64"),
            "unit": raw["unit"].astype(str),
        })
        out["available_utc"] = [
            datetime.combine(d + timedelta(days=lag.get(sid, AVAILABILITY_LAG_DAYS)),
                             datetime.min.time())
            for sid, d in zip(out["series_id"], out["ts"])
        ]
        return out
