"""测试公共夹具。

约定：
    · 时间戳一律用 `naive_utc(...)` 构造 —— 与 schema 口径一致（naive UTC）。
    · 数据一律是**合成值**，不含任何真实或生产数据。
"""

from __future__ import annotations

from datetime import date, datetime

import duckdb


def memory_con() -> duckdb.DuckDBPyConnection:
    """未迁移的空内存库。"""
    return duckdb.connect(":memory:")


def migrated_memory_con() -> duckdb.DuckDBPyConnection:
    """已应用 schema 的内存库（多数 P2 测试的起点）。"""
    from quantlab.store.migrate import apply_migrations

    con = duckdb.connect(":memory:")
    apply_migrations(con)
    return con


def naive_utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    """构造 naive UTC 时间戳（schema 的 available_utc / close_utc 口径）。"""
    return datetime(y, m, d, hh, mm, ss)


def insert_symbol(
    con: duckdb.DuckDBPyConnection,
    symbol_id: int = 1,
    ticker: str = "SYN001",
    exchange: str = "XSHG",
    calendar: str = "XSHG",
    currency: str = "CNY",
    listed_on: date | None = date(2015, 1, 5),
    delisted_on: date | None = None,
) -> None:
    con.execute(
        "INSERT INTO symbols "
        "(symbol_id, ticker, exchange, calendar, currency, isin, lot_size, listed_on, delisted_on) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [symbol_id, ticker, exchange, calendar, currency, None, 100, listed_on, delisted_on],
    )


def insert_bar(
    con: duckdb.DuckDBPyConnection,
    symbol_id: int = 1,
    ts: date = date(2024, 1, 2),
    open_: float = 100.0,
    high: float = 102.0,
    low: float = 99.0,
    close: float = 101.5,
    volume: float = 1000.0,
    currency: str = "CNY",
    close_utc: datetime | None = None,
    available_utc: datetime | None = None,
    source: str = "synth",
    downloaded_at: datetime | None = None,
    snapshot_id: str = "synth-A",
) -> None:
    con.execute(
        "INSERT INTO bars_daily "
        "(symbol_id, ts, open, high, low, close, volume, currency, close_utc, available_utc, "
        " source, downloaded_at, snapshot_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            symbol_id, ts, open_, high, low, close, volume, currency,
            close_utc or naive_utc(ts.year, ts.month, ts.day, 7, 0, 0),
            available_utc or naive_utc(ts.year, ts.month, ts.day, 8, 0, 0),
            source,
            downloaded_at or naive_utc(2024, 1, 3, 0, 0, 0),
            snapshot_id,
        ],
    )


def insert_corporate_action(
    con: duckdb.DuckDBPyConnection,
    symbol_id: int = 1,
    ex_date: date = date(2024, 5, 2),
    kind: str = "dividend",
    ratio: float | None = None,
    cash: float | None = None,
    pay_date: date | None = None,
    available_utc: datetime | None = None,
    snapshot_id: str = "synth-A",
) -> None:
    """插入一条公司行动。默认 available_utc 早于 ex_date（先公告、后除权）。"""
    con.execute(
        "INSERT INTO corporate_actions "
        "(symbol_id, ex_date, kind, ratio, cash, pay_date, available_utc, source, snapshot_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            symbol_id, ex_date, kind, ratio, cash, pay_date,
            available_utc or naive_utc(ex_date.year, ex_date.month, max(1, ex_date.day - 7)),
            "synth", snapshot_id,
        ],
    )


def insert_ingest_run(
    con: duckdb.DuckDBPyConnection,
    snapshot_id: str,
    dataset: str = "bars_daily",
    status: str = "ok",
    finished_at: datetime | None = None,
    rows: int = 0,
) -> None:
    con.execute(
        "INSERT INTO ingest_runs "
        "(snapshot_id, source, dataset, started_at, finished_at, rows, watermark, file_hash, "
        " status, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            snapshot_id, "synthetic", dataset,
            naive_utc(2024, 1, 3, 0, 0, 0),
            finished_at or naive_utc(2024, 1, 3, 0, 0, 0),
            rows, date(2024, 1, 3), None, status, None,
        ],
    )


def scalar(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None):
    """执行并取第一行第一列。"""
    return con.execute(sql, params or []).fetchone()[0]


def columns_of(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    """表（或视图）的列名集合。"""
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ?",
        [table],
    ).fetchall()
    return {r[0] for r in rows}
