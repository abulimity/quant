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
from pathlib import Path

import pandas as pd

from quantlab.fixtures.synth import FixtureBundle
from quantlab.ingest.adapters import _util
from quantlab.ingest.adapters.futu import FutuSource
from quantlab.ingest.adapters.tushare import TushareSource, assign_symbol_ids
from quantlab.ingest.base import ContractError, FetchSpec, validate_normalized
from quantlab.paths import DATA_ROOT
from quantlab.store.canonical import content_hash
from quantlab.store.db import WarehouseNotFoundError, connect, warehouse_path

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


# --------------------------------------------------------------------------- #
# futu：原始 bronze → 契约（normalize-only，抓取在 envs/futu）
# --------------------------------------------------------------------------- #
# symbol_id 命名空间约定（schema.sql:36 的 symbol_id 是全局主键 + bars_daily FK）：
#   cn_etf 已用 1..N；港股若也从 1 起会与之主键冲突。故每个 exchange 预留一个块：
#   XHKG 用 base=1_000_000_000（symbol_id = base + rank）。后续 us_etf（XNYS）再分一块。
_XHKG_SYMBOL_ID_BASE = 1_000_000_000

_FUTU_RAW_BRONZE_ROOT = DATA_ROOT / "bronze" / "futu" / "history_kline"


def _assign_hk_symbol_ids(hk_symbols: pd.DataFrame) -> dict[str, int]:
    """港股永久 ID：XHKG 预留块基数 + 按 ts_code 排序的名次（确定性、不撞 cn_etf）。"""
    return {
        code: _XHKG_SYMBOL_ID_BASE + i + 1
        for i, code in enumerate(sorted(hk_symbols["ts_code"].astype(str)))
    }


def _hk_symbols_to_contract(
    hk_symbols: pd.DataFrame, symbol_map: dict[str, int]
) -> pd.DataFrame:
    """tushare hk_basic（已 normalize 的 hk_symbols）→ 契约 symbols 表（XHKG）。"""
    _util.require_columns(hk_symbols, ("ts_code", "curr_type"), "futu")
    df = hk_symbols.copy()
    out = pd.DataFrame({
        "symbol_id": [symbol_map[str(c)] for c in df["ts_code"]],
        "ticker": df["ts_code"].astype(str),
        "exchange": "XHKG",
        "calendar": "XHKG",
        "currency": df["curr_type"].astype(str),
        "isin": df["isin"].astype(str) if "isin" in df.columns else None,
        "lot_size": (pd.to_numeric(df["trade_unit"], errors="coerce").astype("float64")
                     if "trade_unit" in df.columns else None),
        "listed_on": df["list_date"].tolist() if "list_date" in df.columns else None,
        "delisted_on": df["delist_date"].tolist() if "delist_date" in df.columns else None,
    })
    validate_normalized(out, "symbols")
    return out


def _locate_latest_hk_symbols(warehouse: str | Path | None = None) -> pd.DataFrame | None:
    """从台账找最近一次成功 ingest 的 hk_symbols 快照并读回；无则 None。"""
    try:
        con = connect(read_only=True, path=warehouse_path(warehouse))
    except WarehouseNotFoundError:
        return None
    try:
        row = con.execute(
            "SELECT snapshot_id FROM ingest_runs "
            "WHERE dataset = 'hk_symbols' AND status = 'ok' "
            "ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    path = DATA_ROOT / "bronze" / "tushare" / row[0] / "hk_symbols.parquet"
    if not path.is_file():
        return None
    return pd.read_parquet(path)


def _locate_latest_futu_bronze(
    raw_bronze_root: str | Path | None = None,
) -> Path | None:
    """最新原始 bronze 目录（目录名 = UTC 时间戳，字典序 = 时间序）。"""
    root = Path(raw_bronze_root) if raw_bronze_root is not None else _FUTU_RAW_BRONZE_ROOT
    if not root.is_dir():
        return None
    candidates = [
        d for d in root.iterdir()
        if d.is_dir() and (d / "kline.parquet").is_file()
    ]
    return max(candidates, key=lambda p: p.name) if candidates else None


def build_futu_bundle(
    *,
    start: date,
    end: date,
    symbols: tuple[str, ...] | None = None,
    hk_symbols: pd.DataFrame | None = None,
    raw_bronze_dir: str | Path | None = None,
    warehouse: str | Path | None = None,
) -> FixtureBundle:
    """把 futu 原始 bronze normalize 成契约 bars_daily + HK symbols，打包成可落快照的 bundle。

    `hk_symbols` / `raw_bronze_dir` 缺省时自动定位（前者走台账、后者按时间戳取最新原始
    bronze）；定位不到即 fail-closed（不静默产出空快照）。测试可注入内联合成帧离线跑。
    """
    if hk_symbols is None:
        hk_symbols = _locate_latest_hk_symbols(warehouse)
    if hk_symbols is None or len(hk_symbols) == 0:
        raise ContractError(
            "futu: 找不到 tushare_hk 的 hk_symbols（先跑 `quantlab ingest --source tushare_hk`）")

    symbol_map = _assign_hk_symbol_ids(hk_symbols)
    symbols_frame = _hk_symbols_to_contract(hk_symbols, symbol_map)

    raw_dir = (
        Path(raw_bronze_dir) if raw_bronze_dir is not None else _locate_latest_futu_bronze()
    )
    if raw_dir is None or not (raw_dir / "kline.parquet").is_file():
        raise ContractError(
            "futu: 找不到原始 bronze（先跑 envs/futu/fetch_history_kline.py 抓取）")

    futu = FutuSource(symbol_map=symbol_map, currency="HKD", raw_bronze_dir=raw_dir)
    raw_kline = futu.fetch()
    if symbols:
        requested = set(symbols)
        missing = sorted(requested - set(raw_kline["code"].astype(str)))
        if missing:
            raise ContractError(f"futu: 指定标的在原始 bronze 中无数据: {missing}")
        raw_kline = raw_kline[raw_kline["code"].astype(str).isin(requested)]
    bars = futu.normalize(raw_kline)

    # 裁剪到研究窗口（契约窗口纪律，与 tushare 一致）
    if len(bars):
        bars = bars[(bars["ts"] >= start) & (bars["ts"] <= end)].reset_index(drop=True)

    return _assemble_bundle(
        "futu",
        {"symbols": symbols_frame, "bars_daily": bars},
        {"bars_daily": ("source", "downloaded_at", "snapshot_id")},
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "availability_note": FutuSource.availability_note,
            "symbol_id_namespace": {"exchange": "XHKG", "base": _XHKG_SYMBOL_ID_BASE},
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
