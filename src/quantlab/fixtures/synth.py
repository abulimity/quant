"""合成夹具生成器（LOCAL_DEPLOYMENT_PLAN.md §P2.2）。

**总收益指数是原语，原始价格是导出量。** 这一取向是为了让「已知答案」可被**手算复现**：

    日总收益 g_t（对数收益 = mu + sigma·z，z 由固定种子派生）
    拆分比例 r_t（除权日为声明比例，否则 1）
    每股现金 d_t（除权日为声明金额，否则 0）

        TRI[0] = 1,  TRI[t] = TRI[t-1] · g_t               ← 总收益指数（闭式，即答案）
        close_raw[t] = g_t · close_raw[t-1] / r_t - d_t     ← 原始价格（精确恒等式）

于是 `buy&hold 解析净值 = TRI[t]`，而 `(close_raw[t]·r_t + d_t)/close_raw[t-1]` 逐日累乘
必然**逐位**复现它 —— 这就是 P2.2 V3「buy&hold 解析净值与夹具数据算出的净值在 1e-9 内一致」
的构造性证明，不需要任何近似。

三态（休市/停牌/下载失败）在**行是否存在**与**是否可成交**上区分：
    · 休市      → 该日**无行**（不在会话网格内）
    · 停牌      → **有行**，`traded=False`、`volume=0`、价格沿用上一有效收盘（估值用，不可成交）
    · 下载失败  → 该日**无行**（我们没取到数据），但会话网格仍认为市场开着 —— 故可被**检出为缺口**

用法：
    python -m quantlab.fixtures.synth --out data/bronze/synthetic
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from quantlab.fixtures import spec as S
from quantlab.paths import DATA_ROOT
from quantlab.store.atomic import atomic_write_parquet, atomic_write_text, rmtree, staging_dir
from quantlab.store.canonical import content_hash

# 每张表的规范化排序键（用于内容哈希；须与 store/schema.sql 的主键一致）
CONTENT_KEYS: dict[str, list[str]] = {
    "symbols": ["symbol_id"],
    "bars_daily": ["symbol_id", "ts", "snapshot_id"],
    "corporate_actions": ["symbol_id", "ex_date", "kind", "snapshot_id"],
    "fx_rates": ["base", "quote", "ts", "snapshot_id"],
    "trading_calendar": ["exchange", "ts"],
    "macro_series": ["series_id", "ts", "snapshot_id"],
    "fundamentals": ["symbol_id", "period_end", "as_of_date", "item", "snapshot_id"],
    # 真实 tushare 的非契约 raw 表（真实快照也会经过 content_hashes / write_snapshot）
    "fund_adj": ["symbol_id", "ts", "snapshot_id"],
    "index_symbols": ["ts_code"],
    "index_daily": ["ts_code", "ts", "snapshot_id"],
    "hk_symbols": ["ts_code"],
}

_CALENDAR_CACHE: dict[str, object] = {}


# --------------------------------------------------------------------------- #
# 确定性随机
# --------------------------------------------------------------------------- #
def _generator(label: str, symbol_id: int | None = None) -> np.random.Generator:
    """由 (SEED, label, symbol_id) 派生独立随机流。

    用 blake2b 而非内建 `hash()`：后者受 `PYTHONHASHSEED` 影响、**每次进程不同**，
    会直接破坏 V4「同种子两次跑哈希一致」。
    """
    key = f"{S.SEED}|{S.SYNTH_VERSION}|{label}|{'' if symbol_id is None else symbol_id}"
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return np.random.default_rng(int.from_bytes(digest, "big"))


def _calendar(name: str):
    if name not in _CALENDAR_CACHE:
        _CALENDAR_CACHE[name] = xcals.get_calendar(name)
    return _CALENDAR_CACHE[name]


def _naive_utc(ts) -> datetime:
    """转成 naive UTC datetime（与 schema 口径一致）。"""
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.to_pydatetime()


def _session_closes(sessions: pd.DatetimeIndex, cal) -> list[pd.Timestamp]:
    """各会话的收盘时刻（tz-aware）。兼容批量与逐个两种 API 形态。"""
    try:
        return list(pd.DatetimeIndex(cal.session_close(sessions)))
    except (TypeError, ValueError):
        return [cal.session_close(s) for s in sessions]


# --------------------------------------------------------------------------- #
# 结果容器
# --------------------------------------------------------------------------- #
@dataclass
class FixtureBundle:
    snapshot_id: str
    tables: dict[str, pd.DataFrame]
    analytic_tri: dict[int, pd.Series] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    # ---- 已知答案 ----
    def analytic_nav(self, symbol_id: int, *, traded_only: bool = True) -> pd.DataFrame:
        """buy&hold 解析净值（= 总收益指数，起点归一为 1）。返回列：`ts`(date), `nav`(float)。

        ⚠️ 两种「不可得」语义**不同**，不可混为一谈：

        · **停牌**（`traded=False`）：市场开着、行**在**，只是不能成交。
          `TRI` 在停牌日不变（g=1），故 `traded_only=True`（只看可成交行）
          仍能**逐位复现** `TRI` —— 停牌不产生收益，这正是要验证的。
        · **下载失败**：我们**没有取到**数据，行**不存在**，但市场当时是开着的，
          `TRI` 在那些日子**照常累积**。因此带失败窗的标的（如 symbol 6）
          **无法**仅由观测到的 bars 复现 —— 那不是缺陷，而是「数据缺失真实存在」
          的证据，P2.6 应据此报出缺口。用 `unobserved_sessions()` 取该缺口。

        故：V3 的「解析净值 vs 从夹具重算」断言应作用于**无失败窗**的标的；
        带失败窗的标的另立断言（**必须不可复现**）。
        """
        if symbol_id not in self.analytic_tri:
            raise KeyError(f"未知 symbol_id={symbol_id}")
        tri = self.analytic_tri[symbol_id]
        frame = pd.DataFrame({"ts": list(tri.index), "nav": tri.to_numpy(dtype="float64")})
        if traded_only:
            bars = self.tables["bars_daily"]
            tradable = set(
                bars.loc[bars["symbol_id"] == symbol_id].loc[lambda d: d["traded"], "ts"]
            )
            frame = frame[frame["ts"].isin(tradable)].reset_index(drop=True)
        return frame

    def unobserved_sessions(self, symbol_id: int) -> list[date]:
        """该标的在日历上**开盘但我们没有数据**的交易日（即下载失败缺口）。

        这是 P2.6「日历预期缺口」检测的输入：休市**不算**缺口（行本就不该有），
        失败**才算**（市场开着却没数据）。
        """
        sym = S.SYMBOLS_BY_ID[symbol_id]
        cal = _calendar(sym.calendar)
        start = max(sym.listed_on, S.STUDY_START)
        end = min(sym.delisted_on or S.STUDY_END, S.STUDY_END)
        if end < start:
            return []
        expected = {pd.Timestamp(s).date() for s in cal.sessions_in_range(
            pd.Timestamp(start), pd.Timestamp(end))}
        bars = self.tables["bars_daily"]
        observed = set(bars.loc[bars["symbol_id"] == symbol_id, "ts"])
        return sorted(expected - observed)

    # ---- 审计 ----
    def content_hashes(self) -> dict[str, str]:
        return {name: content_hash(df, CONTENT_KEYS[name]) for name, df in self.tables.items()}

    def combined_content_hash(self) -> str:
        """单一口径的内容指纹：表名 + 各表内容哈希，字典序拼接后 sha256。"""
        parts = [f"{name}:{h}" for name, h in sorted(self.content_hashes().items())]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #
def _sessions(sym: S.SymbolSpec) -> pd.DatetimeIndex:
    """该标的在研究窗口内的交易日（按当地日历）。"""
    cal = _calendar(sym.calendar)
    start = max(sym.listed_on, S.STUDY_START)
    end = min(sym.delisted_on or S.STUDY_END, S.STUDY_END)
    if end < start:
        return pd.DatetimeIndex([])
    return cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))


def _build_symbol_frames(sym: S.SymbolSpec, snapshot_id: str):
    """生成单只标的的 bars_daily、解析 TRI、掩码与网格。"""
    cal = _calendar(sym.calendar)
    sessions = _sessions(sym)
    n = len(sessions)
    if n == 0:
        return pd.DataFrame(), pd.Series(dtype="float64"), None

    rng = _generator("returns", sym.symbol_id)
    log_ret = sym.mu_daily + sym.sigma_daily * rng.standard_normal(n)

    def _mask(windows: tuple[S.WindowSpec, ...]) -> np.ndarray:
        mask = np.zeros(n, dtype=bool)
        for w in windows:
            if w.symbol_id != sym.symbol_id:
                continue
            mask |= (sessions >= pd.Timestamp(w.start)) & (sessions <= pd.Timestamp(w.end))
        return mask

    suspended = _mask(S.SUSPENSIONS)
    failed = _mask(S.FAILURES)
    log_ret[suspended] = 0.0            # 停牌：价格沿用，日收益为 0

    # 公司行动（除权日 → 比例 / 现金）。
    # **fail-closed**：除权日必须落在该标的的会话网格内，否则该事件会被静默丢弃，
    # 于是「已知答案」就成了假证据 —— 干脆直接报错，绝不静默。
    r = np.ones(n)
    d = np.zeros(n)
    for sp in S.SPLITS:
        if sp.symbol_id != sym.symbol_id:
            continue
        hits = np.flatnonzero(sessions == pd.Timestamp(sp.ex_date))
        if len(hits) != 1:
            raise AssertionError(
                f"symbol {sym.symbol_id}: 拆分除权日 {sp.ex_date} 不是唯一交易日 "
                f"(命中 {len(hits)})，规格与日历不一致")
        r[hits[0]] = sp.ratio
    for dv in S.DIVIDENDS:
        if dv.symbol_id != sym.symbol_id:
            continue
        hits = np.flatnonzero(sessions == pd.Timestamp(dv.ex_date))
        if len(hits) != 1:
            raise AssertionError(
                f"symbol {sym.symbol_id}: 分红除权日 {dv.ex_date} 不是唯一交易日 "
                f"(命中 {len(hits)})，规格与日历不一致")
        d[hits[0]] = dv.cash

    g = np.exp(log_ret)
    close = np.empty(n, dtype="float64")
    close[0] = sym.init_price
    for i in range(1, n):
        close[i] = g[i] * close[i - 1] / r[i] - d[i]
    if np.any(close <= 0):
        raise AssertionError(f"symbol {sym.symbol_id}: 生成出非正价格，规格有误")

    # TRI[0] = 1（不是 g[0]）：close[0] = init_price 是**基线**，第 0 日的收益
    # 并未体现为价格变动，故从第 1 日起才开始累积。写错一位会得到一条
    # 缓慢发散的假曲线（Δ 随天数增长），V3 的等比断言正是为抓它而设。
    tri = np.empty(n, dtype="float64")
    tri[0] = 1.0
    tri[1:] = np.cumprod(g[1:])
    tri_series = pd.Series(tri, index=[pd.Timestamp(s).date() for s in sessions], dtype="float64")

    # OHLC：围绕 close 构造。**close 不取整** —— 取整会破坏恒等式的逐位可复现性。
    o_rng = _generator("ohlc", sym.symbol_id)
    open_off = o_rng.uniform(-0.008, 0.008, n)
    up = o_rng.uniform(0.0, 0.010, n)
    dn = o_rng.uniform(0.0, 0.010, n)
    open_ = close * (1.0 + open_off)
    high = np.maximum(open_, close) * (1.0 + up)
    low = np.minimum(open_, close) * (1.0 - dn)

    v_rng = _generator("volume", sym.symbol_id)
    volume = np.round(S.BASE_VOLUME * np.exp(v_rng.uniform(-0.3, 0.3, n)))

    # 停牌：不可成交 —— 价格沿用上一有效收盘，量为 0
    if suspended.any():
        carried = np.empty(n, dtype="float64")
        last = close[0]
        for i in range(n):
            carried[i] = last if suspended[i] else close[i]
            if not suspended[i]:
                last = close[i]
        open_[suspended] = carried[suspended]
        high[suspended] = carried[suspended]
        low[suspended] = carried[suspended]
        close[suspended] = carried[suspended]
        volume[suspended] = 0.0

    close_ts = _session_closes(sessions, cal)
    buffer = timedelta(minutes=S.AVAILABILITY_BUFFER_MINUTES[sym.calendar])

    rows = []
    for i, session in enumerate(sessions):
        if failed[i]:
            continue                      # 下载失败：**不产出行**（可被检出为缺口）
        rows.append(
            {
                "symbol_id": sym.symbol_id,
                "ts": pd.Timestamp(session).date(),
                "open": float(round(float(open_[i]), 6)),
                "high": float(max(round(float(high[i]), 6), float(close[i]))),
                "low": float(min(round(float(low[i]), 6), float(close[i]))),
                "close": float(close[i]),
                "volume": float(volume[i]),
                "currency": sym.currency,
                "close_utc": _naive_utc(close_ts[i]),
                "available_utc": _naive_utc(close_ts[i] + buffer),
                "source": "synthetic",
                "downloaded_at": datetime(2024, 12, 31, 0, 0, 0),
                "snapshot_id": snapshot_id,
                "traded": bool(not suspended[i]),
            }
        )
    return pd.DataFrame(rows), tri_series, (suspended, failed, sessions)


def _build_symbols_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol_id": s.symbol_id,
                "ticker": s.ticker,
                "exchange": s.exchange,
                "calendar": s.calendar,
                "currency": s.currency,
                "isin": f"SYN{s.symbol_id:04d}0000000",   # 合成 ISIN，明显非真实
                "lot_size": s.lot_size,
                "listed_on": s.listed_on,
                "delisted_on": s.delisted_on,
            }
            for s in S.SYMBOLS
        ]
    )


def _build_corporate_actions(snapshot_id: str) -> pd.DataFrame:
    rows = []
    for sp in S.SPLITS:
        rows.append({
            "symbol_id": sp.symbol_id, "ex_date": sp.ex_date, "kind": "split",
            "ratio": sp.ratio, "cash": None, "pay_date": None,
            "available_utc": datetime.combine(
                sp.ex_date - timedelta(days=sp.announce_lag_days), datetime.min.time()),
            "source": "synthetic", "snapshot_id": snapshot_id,
        })
    for dv in S.DIVIDENDS:
        rows.append({
            "symbol_id": dv.symbol_id, "ex_date": dv.ex_date, "kind": "dividend",
            "ratio": None, "cash": dv.cash, "pay_date": dv.ex_date + timedelta(days=21),
            "available_utc": datetime.combine(
                dv.ex_date - timedelta(days=dv.announce_lag_days), datetime.min.time()),
            "source": "synthetic", "snapshot_id": snapshot_id,
        })
    return pd.DataFrame(rows).sort_values(["symbol_id", "ex_date", "kind"]).reset_index(drop=True)


def _availability_from_lag(sessions: pd.DatetimeIndex, lag_days: int) -> list[datetime]:
    """F.6：只有日频汇率且缺发布时间时，采用**保守滞后**（须披露该假设）。"""
    return [
        datetime.combine(pd.Timestamp(s).date() + timedelta(days=lag_days), datetime.min.time())
        for s in sessions
    ]


def _build_fx(snapshot_id: str) -> pd.DataFrame:
    """汇率：内部统一「1 base = rate quote」。用 XNYS 会话作交易日网格。"""
    cal = _calendar("XNYS")
    sessions = cal.sessions_in_range(pd.Timestamp(S.STUDY_START), pd.Timestamp(S.STUDY_END))
    n = len(sessions)

    series: dict[tuple[str, str], np.ndarray] = {}
    for fx in S.FX_SERIES:
        rng = _generator(f"fx:{fx.base}{fx.quote}")
        steps = fx.annual_drift / 252.0 + fx.sigma_daily * rng.standard_normal(n)
        series[(fx.base, fx.quote)] = fx.init_rate * np.exp(np.cumsum(steps))
    for base, quote in S.FX_DERIVED:
        if base == "HKD" and quote == "CNY":
            series[(base, quote)] = series[("USD", "CNY")] / series[("USD", "HKD")]

    rows = []
    for fx in S.FX_SERIES:
        rate = series[(fx.base, fx.quote)]
        av = _availability_from_lag(sessions, fx.availability_lag_days)
        for i, session in enumerate(sessions):
            rows.append({
                "base": fx.base, "quote": fx.quote, "ts": pd.Timestamp(session).date(),
                "rate": float(rate[i]), "available_utc": av[i],
                "source": "synthetic", "snapshot_id": snapshot_id,
            })
    for base, quote in S.FX_DERIVED:
        rate = series[(base, quote)]
        av = _availability_from_lag(sessions, 1)
        for i, session in enumerate(sessions):
            rows.append({
                "base": base, "quote": quote, "ts": pd.Timestamp(session).date(),
                "rate": float(rate[i]), "available_utc": av[i],
                "source": "synthetic", "snapshot_id": snapshot_id,
            })
    # 注：不生成反向报价对（理由见 fixtures/spec.py 的说明）
    return pd.DataFrame(rows).sort_values(
        ["base", "quote", "ts", "snapshot_id"]).reset_index(drop=True)


def _build_trading_calendar() -> pd.DataFrame:
    """各交易所的完整日历掩码（窗口内每个自然日一行，含休市日）。"""
    rows = []
    span = pd.date_range(S.STUDY_START, S.STUDY_END, freq="D")
    for exchange in sorted({s.calendar for s in S.SYMBOLS}):
        cal = _calendar(exchange)
        sessions = cal.sessions_in_range(pd.Timestamp(S.STUDY_START),
                                         pd.Timestamp(S.STUDY_END))
        close_map = {pd.Timestamp(s): cal.session_close(s) for s in sessions}
        for day in span:
            is_open = day in sessions
            rows.append({
                "exchange": exchange,
                "ts": day.date(),
                "is_open": bool(is_open),
                "session_close_utc": _naive_utc(close_map[day]) if is_open else None,
                "source": "synthetic",
            })
    return pd.DataFrame(rows).sort_values(["exchange", "ts"]).reset_index(drop=True)


def _build_macro(snapshot_id: str) -> pd.DataFrame:
    """宏观：月频观测；`available_utc = 观测期末 + 声明发布滞后`（F.2 的显式假设）。"""
    months = pd.date_range(S.STUDY_START, S.STUDY_END, freq="ME")
    rows = []
    for m in S.MACRO_SERIES:
        rng = _generator(f"macro:{m.series_id}")
        steps = (m.annual_drift / 12.0) + m.sigma * rng.standard_normal(len(months))
        level = m.init_value + np.cumsum(steps)
        for i, month_end in enumerate(months):
            obs = pd.Timestamp(month_end).date()
            rows.append({
                "series_id": m.series_id, "source": "synthetic", "ts": obs,
                "value": float(level[i]), "unit": m.unit,
                "available_utc": datetime.combine(obs + timedelta(days=m.release_lag_days),
                                                  datetime.min.time()),
                "snapshot_id": snapshot_id,
            })
    return pd.DataFrame(rows).sort_values(["series_id", "ts"]).reset_index(drop=True)


def _build_fundamentals(snapshot_id: str) -> pd.DataFrame:
    """基本面：年度；`as_of_date = period_end + 45d`，`available_utc = as_of_date 09:00`。

    刻意保证 `as_of_date >= period_end`，满足契约层硬逻辑约束。
    """
    rows = []
    for sym in S.SYMBOLS:
        rng = _generator("fundamentals", sym.symbol_id)
        base = float(sym.init_price * 10_000)
        for year in range(S.STUDY_START.year, S.STUDY_END.year + 1):
            period_end = date(year, 12, 31)
            as_of = period_end + timedelta(days=45)
            rows.append({
                "symbol_id": sym.symbol_id, "period_end": period_end,
                "as_of_date": as_of, "item": "revenue",
                "value": base * float(np.exp(rng.normal(0.05, 0.08))),
                "available_utc": datetime.combine(as_of, datetime.min.time())
                + timedelta(hours=9),
                "source": "synthetic", "snapshot_id": snapshot_id,
            })
    return pd.DataFrame(rows).sort_values(
        ["symbol_id", "period_end", "as_of_date", "item"]).reset_index(drop=True)


def generate() -> FixtureBundle:
    """生成完整夹具（纯函数：同种子必然同结果）。"""
    snapshot_id = S.snapshot_id()

    bars_parts, tri = [], {}
    for sym in S.SYMBOLS:
        bars, tri_s, _mask = _build_symbol_frames(sym, snapshot_id)
        if len(bars):
            bars_parts.append(bars)
            tri[sym.symbol_id] = tri_s

    tables = {
        "symbols": _build_symbols_frame(),
        "bars_daily": pd.concat(bars_parts, ignore_index=True),
        "corporate_actions": _build_corporate_actions(snapshot_id),
        "fx_rates": _build_fx(snapshot_id),
        "trading_calendar": _build_trading_calendar(),
        "macro_series": _build_macro(snapshot_id),
        "fundamentals": _build_fundamentals(snapshot_id),
    }

    bundle = FixtureBundle(snapshot_id=snapshot_id, tables=tables, analytic_tri=tri)
    bundle.meta = {
        "snapshot_id": snapshot_id,
        "synth_version": S.SYNTH_VERSION,
        "seed": S.SEED,
        "spec_fingerprint": S.spec_fingerprint(),
        "study_start": S.STUDY_START.isoformat(),
        "study_end": S.STUDY_END.isoformat(),
        "base_currency": S.BASE_CURRENCY,
        "scenarios": {
            "dividends": [{"symbol_id": d.symbol_id, "ex_date": d.ex_date.isoformat(),
                           "cash": d.cash} for d in S.DIVIDENDS],
            "splits": [{"symbol_id": sp.symbol_id, "ex_date": sp.ex_date.isoformat(),
                        "ratio": sp.ratio} for sp in S.SPLITS],
            "suspensions": [{"symbol_id": w.symbol_id, "start": w.start.isoformat(),
                             "end": w.end.isoformat(), "reason": w.reason}
                            for w in S.SUSPENSIONS],
            "failures": [{"symbol_id": w.symbol_id, "start": w.start.isoformat(),
                          "end": w.end.isoformat(), "reason": w.reason}
                         for w in S.FAILURES],
            "delisted": [s.symbol_id for s in S.SYMBOLS if s.delisted_on],
            "late_listed": [s.symbol_id for s in S.SYMBOLS if s.listed_on > S.STUDY_START],
        },
        "row_counts": {name: int(len(df)) for name, df in tables.items()},
        "content_hashes": bundle.content_hashes(),
        "combined_content_hash": bundle.combined_content_hash(),
    }
    return bundle


# --------------------------------------------------------------------------- #
# 不变量（V3：夹具自身的自洽性）
# --------------------------------------------------------------------------- #
def check_invariants(bundle: FixtureBundle) -> list[str]:
    """返回违反项列表；空列表 = 全部通过。供测试与 P2.6 复用。"""
    bad: list[str] = []
    bars = bundle.tables["bars_daily"]
    fx = bundle.tables["fx_rates"]

    for col in ("open", "high", "low", "close"):
        if (bars[col] <= 0).any():
            bad.append(f"非正价格: {col}")

    lo_bound = bars[["open", "close"]].min(axis=1)
    hi_bound = bars[["open", "close"]].max(axis=1)
    if not (bars["low"] <= lo_bound).all():
        bad.append("low 超过 min(open, close)")
    if not (bars["high"] >= hi_bound).all():
        bad.append("high 低于 max(open, close)")

    dup = int(bars.duplicated(subset=CONTENT_KEYS["bars_daily"]).sum())
    if dup:
        bad.append(f"bars_daily 重复主键 {dup} 行")

    halted = bars[~bars["traded"].astype(bool)]
    if len(halted) and (halted["volume"] != 0).any():
        bad.append("停牌日存在非零成交量")

    if (fx["rate"] <= 0).any():
        bad.append("存在非正汇率")
    piv = fx.pivot_table(index="ts", columns=["base", "quote"], values="rate")
    if ("HKD", "CNY") in piv.columns and ("USD", "CNY") in piv.columns:
        lhs = piv[("HKD", "CNY")].dropna()
        rhs = (piv[("USD", "CNY")] / piv[("USD", "HKD")]).dropna()
        if not np.allclose(lhs.to_numpy(), rhs.to_numpy(), rtol=1e-12, atol=0):
            bad.append("HKD/CNY 与 (USD/CNY)/(USD/HKD) 不自洽")

    for sid, tri in bundle.analytic_tri.items():
        if (tri <= 0).any():
            bad.append(f"symbol {sid} 解析净值非正")

    for sym in S.SYMBOLS:
        sub = bars[bars["symbol_id"] == sym.symbol_id]
        if not len(sub):
            continue
        if pd.Timestamp(sub["ts"].min()) < pd.Timestamp(sym.listed_on):
            bad.append(f"symbol {sym.symbol_id} 出现上市前行情")
        if sym.delisted_on and pd.Timestamp(sub["ts"].max()) > pd.Timestamp(sym.delisted_on):
            bad.append(f"symbol {sym.symbol_id} 出现退市后行情")
    return bad


# --------------------------------------------------------------------------- #
# 快照读写
# --------------------------------------------------------------------------- #
def snapshot_dir(snapshot_id: str, root: str | Path) -> Path:
    return Path(root) / snapshot_id


def write_snapshot(bundle: FixtureBundle, root: str | Path) -> Path:
    """原子写入快照目录：先 `.staging-*`，全部落盘后一次改名。已存在则**拒绝覆盖**。"""
    root = Path(root)
    manifest = {
        **bundle.meta,
        "tables": {name: {"rows": int(len(df)), "content_hash": h}
                   for (name, df), h in
                   zip(bundle.tables.items(), bundle.content_hashes().values())},
    }
    with staging_dir(root) as (staging, commit):
        for name, df in bundle.tables.items():
            atomic_write_parquet(df, staging / f"{name}.parquet")
        atomic_write_text(staging / "meta.json",
                          json.dumps(bundle.meta, ensure_ascii=False, indent=2, default=str))
        atomic_write_text(staging / "manifest.json",
                          json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
        return commit(bundle.snapshot_id)


def read_snapshot(snapshot_id: str, root: str | Path) -> FixtureBundle:
    """读回快照（**不重算**），可用于「读到的内容是否与生成的一致」这类断言。"""
    directory = snapshot_dir(snapshot_id, root)
    if not directory.is_dir():
        raise FileNotFoundError(f"快照不存在: {directory}")
    tables = {p.stem: pd.read_parquet(p) for p in sorted(directory.glob("*.parquet"))}
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    return FixtureBundle(snapshot_id=snapshot_id, tables=tables, meta=meta)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成合成夹具快照（§P2.2）")
    parser.add_argument("--out", default=str(DATA_ROOT / "bronze" / "synthetic"),
                        help="快照根目录（默认 data/bronze/synthetic）")
    parser.add_argument("--force-new", action="store_true",
                        help="快照已存在时先删除再重建（**仅用于开发**；默认拒绝覆盖）")
    args = parser.parse_args(argv)

    root = Path(args.out)
    bundle = generate()
    target = snapshot_dir(bundle.snapshot_id, root)

    if target.exists() and args.force_new:
        rmtree(target)

    bad = check_invariants(bundle)
    if bad:
        for item in bad:
            print(f"不变量失败: {item}", file=sys.stderr)
        return 1

    written = write_snapshot(bundle, root)
    print(json.dumps({
        "snapshot_id": bundle.snapshot_id,
        "path": str(written),
        "row_counts": bundle.meta["row_counts"],
        "combined_content_hash": bundle.meta["combined_content_hash"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
