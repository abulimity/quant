"""真实供应商数据的快照构造（LOCAL_DEPLOYMENT_PLAN.md §P2.5 两段式）。

与 `fixtures/synth.py` 的合成夹具对称：合成夹具的「已知答案」（解析 TRI）在这里**不存在**，
故本模块只负责把真实数据 fetch→normalize→打包成可落快照的 `FixtureBundle`。

**复用** `FixtureBundle` / `content_hashes()` / `write_snapshot()` 这类通用容器与 IO，
它们本就与「数据是合成还是真实」无关；真正不可复用是合成夹具的
`check_invariants()`（依赖 `traded`/`analytic_tri`/fx 对拍），故这里另立
`check_real_invariants()`。

快照 ID 语义（对齐 orchestrator「内容相同 → ID 相同」）：
    `snapshot_id = <source>-<sha256(source + 各表契约内容哈希)前 16>`
    · 契约内容哈希**不含** downloaded_at（否则每次回填时间不同 → ID 必不同，无法幂等）；
    · 也不含 snapshot_id（否则自指）。故先在**未打标**的契约帧上求哈希，再回填打标。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

import pandas as pd

from quantlab.fixtures.synth import FixtureBundle
from quantlab.ingest.adapters.tushare import TushareSource, assign_symbol_ids
from quantlab.ingest.base import ContractError, FetchSpec
from quantlab.store.canonical import content_hash

# 求 snapshot_id 用的「未打标」排序键（不含 snapshot_id / downloaded_at）
_PREID_KEYS: dict[str, list[str]] = {
    "symbols": ["symbol_id"],
    "bars_daily": ["symbol_id", "ts"],
    "corporate_actions": ["symbol_id", "ex_date", "kind"],
    "fund_adj": ["symbol_id", "ts"],
    "index_symbols": ["ts_code"],
    "index_daily": ["ts_code", "ts"],
    "hk_symbols": ["ts_code"],
    "macro_series": ["series_id", "ts"],
}

# 基准指数（curated）：覆盖主流宽基/规模/风格指数，供报告基准（F.10）使用。
# fund_basic.benchmark 是自由文本描述（实测 1316 种，如「沪深300指数收益率×100%」），
# 无法可靠反查 ts_code，故用固定清单；构建时 fail-closed 校验清单内代码都在 index_basic 内。
_BENCHMARK_INDICES: tuple[str, ...] = (
    "000001.SH",  # 上证指数
    "000016.SH",  # 上证50
    "000010.SH",  # 上证180
    "000300.SH",  # 沪深300
    "000905.SH",  # 中证500
    "000852.SH",  # 中证1000
    "000688.SH",  # 科创50
    "399001.SZ",  # 深证成指
    "399006.SZ",  # 创业板指
    "399005.SZ",  # 中小100
)


def _utcnow() -> datetime:
    """naive UTC（与 schema 口径一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _derive_snapshot_id(source: str, tables: dict[str, pd.DataFrame]) -> str:
    """由契约内容（未打标）确定性派生快照 ID，不依赖时钟。"""
    parts = [
        f"{name}:{content_hash(df, _PREID_KEYS[name])}"
        for name, df in sorted(tables.items())
    ]
    digest = hashlib.sha256((source + "\n" + "\n".join(parts)).encode("utf-8")).hexdigest()
    return f"{source}-{digest[:16]}"


def build_tushare_bundle(
    *,
    start: date,
    end: date,
    symbols: tuple[str, ...] | None = None,
    token: str | None = None,
    rate_limit_delay: float = 0.2,
) -> FixtureBundle:
    """抓取 tushare 境内 ETF 数据，打包成可落快照的 bundle。

    `symbols=None` 时取全量（fund_basic 全名单排除 REITs）；否则只取给定子集
    （子集必须在名单内，否则 fail-closed）。symbol_id 由 `assign_symbol_ids`
    确定性分配（永久 ID：按 ts_code 排序 1..N）。
    """
    # 1) 名单（全量，排除 REITs）→ 分配永久 ID
    sym_src = TushareSource(dataset="symbols", token=token, rate_limit_delay=rate_limit_delay)
    raw_basic = sym_src.fetch(FetchSpec(dataset="symbols", start=start, end=end))
    symbol_map = assign_symbol_ids(raw_basic)
    sym_src.symbol_map = symbol_map
    symbols_frame = sym_src.normalize(raw_basic)

    # 2) 行情标的：全量 or 子集（子集必须在名单内）
    if symbols is None:
        codes = tuple(sorted(symbol_map))
    else:
        codes = tuple(symbols)
        unknown = sorted(c for c in codes if c not in symbol_map)
        if unknown:
            raise ContractError(f"tushare: 标的未在 fund_basic 名单内: {unknown}")

    # 3) 未复权日线
    bar_src = TushareSource(
        dataset="bars_daily", token=token, symbol_map=symbol_map,
        rate_limit_delay=rate_limit_delay,
    )
    raw_bars = bar_src.fetch(
        FetchSpec(dataset="bars_daily", start=start, end=end, symbols=codes))
    bars = bar_src.normalize(raw_bars)

    # 4) 分红（可为空：多数 ETF 从未分红）
    div_src = TushareSource(
        dataset="corporate_actions", token=token, symbol_map=symbol_map,
        rate_limit_delay=rate_limit_delay,
    )
    raw_div = div_src.fetch(
        FetchSpec(dataset="corporate_actions", start=start, end=end, symbols=codes))
    ca = div_src.normalize(raw_div)

    # 4b) 复权因子（同一标的池；F.6 总收益交叉核对用，非契约 raw 表）
    adj_src = TushareSource(
        dataset="fund_adj", token=token, symbol_map=symbol_map,
        rate_limit_delay=rate_limit_delay,
    )
    raw_adj = adj_src.fetch(
        FetchSpec(dataset="fund_adj", start=start, end=end, symbols=codes))
    fund_adj = adj_src.normalize(raw_adj)

    # 5) 先由「未打标」契约帧求 snapshot_id，再回填 source/downloaded_at/snapshot_id
    snapshot_id = _derive_snapshot_id("tushare", {
        "symbols": symbols_frame, "bars_daily": bars, "corporate_actions": ca,
        "fund_adj": fund_adj,
    })
    downloaded_at = _utcnow()

    bars = bars.assign(source="tushare", downloaded_at=downloaded_at, snapshot_id=snapshot_id)
    ca = ca.assign(source="tushare", snapshot_id=snapshot_id)
    fund_adj = fund_adj.assign(source="tushare", snapshot_id=snapshot_id)

    tables = {"symbols": symbols_frame, "bars_daily": bars,
              "corporate_actions": ca, "fund_adj": fund_adj}
    bundle = FixtureBundle(snapshot_id=snapshot_id, tables=tables)
    bundle.meta = {
        "snapshot_id": snapshot_id,
        "source": "tushare",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "downloaded_at": downloaded_at.isoformat(),
        "universe": {"requested": list(codes), "symbols_rows": int(len(symbols_frame))},
        "availability_note": TushareSource.availability_note,
        "row_counts": {name: int(len(df)) for name, df in tables.items()},
        "content_hashes": bundle.content_hashes(),
        "combined_content_hash": bundle.combined_content_hash(),
    }
    return bundle


def _assemble_bundle(
    source: str,
    tables: dict[str, pd.DataFrame],
    tag_spec: dict[str, tuple[str, ...]],
    extra_meta: dict,
) -> FixtureBundle:
    """通用收尾：由「未打标」契约帧派生 snapshot_id，再按 tag_spec 回填 source 等列。

    与 `build_tushare_bundle` 的收尾一致；`tag_spec` 指定每张表回填哪些审计列
    （子集 of {"source", "downloaded_at", "snapshot_id"}）。名单/目录类表不回填。
    """
    snapshot_id = _derive_snapshot_id(source, tables)
    downloaded_at = _utcnow()
    tagged: dict[str, pd.DataFrame] = {}
    for name, df in tables.items():
        df = df.copy()
        for col in tag_spec.get(name, ()):
            if col == "source":
                df = df.assign(source=source)
            elif col == "downloaded_at":
                df = df.assign(downloaded_at=downloaded_at)
            elif col == "snapshot_id":
                df = df.assign(snapshot_id=snapshot_id)
        tagged[name] = df
    bundle = FixtureBundle(snapshot_id=snapshot_id, tables=tagged)
    bundle.meta = {
        "snapshot_id": snapshot_id,
        "source": source,
        "downloaded_at": downloaded_at.isoformat(),
        **extra_meta,
        "row_counts": {n: int(len(d)) for n, d in tagged.items()},
        "content_hashes": bundle.content_hashes(),
        "combined_content_hash": bundle.combined_content_hash(),
    }
    return bundle


def build_tushare_index_bundle(
    *,
    start: date,
    end: date,
    token: str | None = None,
    rate_limit_delay: float = 0.2,
) -> FixtureBundle:
    """抓取 tushare 指数名单（index_basic 全量）+ 基准指数日线（curated 清单）。"""
    cat_src = TushareSource(dataset="index_symbols", token=token,
                            rate_limit_delay=rate_limit_delay)
    raw_cat = cat_src.fetch(FetchSpec(dataset="index_symbols", start=start, end=end))
    catalog = cat_src.normalize(raw_cat)

    # fail-closed：curated 基准必须在 index_basic 名单内，否则立即报错（不浪费 index_daily 调用）
    known = set(catalog["ts_code"].astype(str))
    missing = [c for c in _BENCHMARK_INDICES if c not in known]
    if missing:
        raise ContractError(f"tushare: 基准指数未在 index_basic 名单内: {missing}")

    daily_src = TushareSource(dataset="index_daily", token=token,
                              rate_limit_delay=rate_limit_delay)
    raw_daily = daily_src.fetch(FetchSpec(
        dataset="index_daily", start=start, end=end, symbols=_BENCHMARK_INDICES))
    daily = daily_src.normalize(raw_daily)

    return _assemble_bundle(
        "tushare_index",
        {"index_symbols": catalog, "index_daily": daily},
        {"index_daily": ("source", "snapshot_id")},
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "benchmarks": list(_BENCHMARK_INDICES),
            "availability_note": TushareSource.availability_note,
        },
    )


def build_tushare_hk_bundle(
    *,
    token: str | None = None,
    rate_limit_delay: float = 0.2,
) -> FixtureBundle:
    """抓取 tushare 港股证券名单（hk_basic 全量；行情由 futu 供，此处仅元数据）。"""
    src = TushareSource(dataset="hk_symbols", token=token,
                        rate_limit_delay=rate_limit_delay)
    raw = src.fetch(FetchSpec(
        dataset="hk_symbols", start=date(1970, 1, 1), end=date(2100, 1, 1)))
    hk = src.normalize(raw)
    return _assemble_bundle(
        "tushare_hk",
        {"hk_symbols": hk},
        {},
        {"availability_note": TushareSource.availability_note},
    )


def build_tushare_macro_bundle(
    *,
    start: date,
    end: date,
    token: str | None = None,
    rate_limit_delay: float = 0.2,
) -> FixtureBundle:
    """抓取 tushare 宏观最小集（cn_cpi/cn_ppi/cn_gdp/shibor）→ 契约 macro_series。"""
    src = TushareSource(dataset="macro_series", token=token,
                        rate_limit_delay=rate_limit_delay)
    raw = src.fetch(FetchSpec(dataset="macro_series", start=start, end=end))
    macro = src.normalize(raw)
    return _assemble_bundle(
        "tushare_macro",
        {"macro_series": macro},
        {"macro_series": ("source", "snapshot_id")},
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "availability_note": (
                "宏观 available_utc = 观测期末 + 发布滞后（CPI/PPI 15d、GDP 30d、"
                "shibor 1d，F.2 保守口径）。"),
        },
    )


def check_real_invariants(bundle: FixtureBundle) -> list[str]:
    """真实数据的**通用**自洽性检查（不依赖夹具特有的 traded/TRI/fx 对拍）。

    返回违反项列表；空列表 = 通过。与 `fixtures/synth.check_invariants` 并列，
    覆盖「坏数据」的结构性红旗，供 ingest 前拒绝落盘。只检查 bundle 里**存在**的表。
    """
    bad: list[str] = []
    tables = bundle.tables
    bars = tables.get("bars_daily")
    ca = tables.get("corporate_actions")
    symbols = tables.get("symbols")
    fund_adj = tables.get("fund_adj")
    index_daily = tables.get("index_daily")
    macro = tables.get("macro_series")

    if bars is not None and len(bars):
        for col in ("open", "high", "low", "close"):
            if (bars[col] <= 0).any():
                bad.append(f"bars_daily 非正价格: {col}")
        lo = bars[["open", "close"]].min(axis=1)
        hi = bars[["open", "close"]].max(axis=1)
        if (bars["low"] > lo).any():
            bad.append("bars_daily 存在 low > min(open, close)")
        if (bars["high"] < hi).any():
            bad.append("bars_daily 存在 high < max(open, close)")
        dup = int(bars.duplicated(subset=["symbol_id", "ts"]).sum())
        if dup:
            bad.append(f"bars_daily 重复 (symbol_id, ts) {dup} 行")
        if (pd.to_datetime(bars["available_utc"]) < pd.to_datetime(bars["ts"])).any():
            bad.append("bars_daily 存在 available_utc < ts（未来函数）")

    if ca is not None and len(ca):
        dup = int(ca.duplicated(subset=["symbol_id", "ex_date", "kind"]).sum())
        if dup:
            bad.append(f"corporate_actions 重复 (symbol_id, ex_date, kind) {dup} 行")
        if (pd.to_datetime(ca["available_utc"]) > pd.to_datetime(ca["ex_date"])).any():
            bad.append("corporate_actions 存在 available_utc > ex_date（先除权后公告）")

    if symbols is not None and len(symbols) and bool(symbols["symbol_id"].duplicated().any()):
        bad.append("symbols 存在重复 symbol_id")

    if fund_adj is not None and len(fund_adj):
        if (fund_adj["adj_factor"] <= 0).any():
            bad.append("fund_adj 存在非正复权因子")
        dup = int(fund_adj.duplicated(subset=["symbol_id", "ts"]).sum())
        if dup:
            bad.append(f"fund_adj 重复 (symbol_id, ts) {dup} 行")

    if index_daily is not None and len(index_daily):
        for col in ("open", "high", "low", "close"):
            if (index_daily[col] <= 0).any():
                bad.append(f"index_daily 非正价格: {col}")
        lo = index_daily[["open", "close"]].min(axis=1)
        hi = index_daily[["open", "close"]].max(axis=1)
        if (index_daily["low"] > lo).any():
            bad.append("index_daily 存在 low > min(open, close)")
        if (index_daily["high"] < hi).any():
            bad.append("index_daily 存在 high < max(open, close)")
        dup = int(index_daily.duplicated(subset=["ts_code", "ts"]).sum())
        if dup:
            bad.append(f"index_daily 重复 (ts_code, ts) {dup} 行")

    if macro is not None and len(macro):
        dup = int(macro.duplicated(subset=["series_id", "ts"]).sum())
        if dup:
            bad.append(f"macro_series 重复 (series_id, ts) {dup} 行")
        if (pd.to_datetime(macro["available_utc"]) < pd.to_datetime(macro["ts"])).any():
            bad.append("macro_series 存在 available_utc < ts（未来函数）")

    for name in ("index_symbols", "hk_symbols"):
        frame = tables.get(name)
        if frame is not None and len(frame) and bool(frame["ts_code"].duplicated().any()):
            bad.append(f"{name} 存在重复 ts_code")

    return bad
