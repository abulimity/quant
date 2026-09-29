"""把 Parquet 快照装进/挂到 DuckDB（LOCAL_DEPLOYMENT_PLAN.md §P2.3 与 §P2.5）。

重申定位（§3.2）：**Parquet 是真相，DuckDB 是查询层**。
本模块提供两条路径，用途不同、**不可混用**：

1. `register_snapshot_views(con, ...)` —— **零拷贝**：用 `read_parquet()` 直接查 Parquet。
   查询层的主力；改快照内容立刻可见，不占额外磁盘。
2. `load_snapshot(con, ...)` —— **物化**：把快照 `INSERT` 进 DuckDB 表（带主键校验）。
   用于需要约束校验 / 反复聚合的场景。快照不可覆盖，故这里是**追加**语义
   （同一 snapshot_id 重复装载会被主键拒绝，而不会静默翻倍）。

两种路径都**绝不**跨快照混合：视图路径按 `snapshot_id` 目录隔离，物化路径按主键隔离。
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from quantlab.store.migrate import apply_migrations

# 快照中包含的表（与 fixtures/synth.py 的 CONTENT_KEYS 一致）
SNAPSHOT_TABLES: tuple[str, ...] = (
    "symbols", "bars_daily", "corporate_actions", "fx_rates",
    "trading_calendar", "macro_series", "fundamentals",
)


def _posix(path: Path) -> str:
    """DuckDB 在 Windows 上也接受（且更稳的是）正斜杠路径。"""
    return str(path).replace("\\", "/")


def snapshot_glob(root: str | Path, snapshot_id: str) -> str:
    """单快照的 Parquet 通配符。**限定在单个 snapshot_id 目录内**，故不会跨快照叠加。"""
    return _posix(Path(root) / snapshot_id / "*.parquet")


def all_snapshots_glob(root: str | Path) -> str:
    """所有快照的通配符。**仅供元数据/巡检** —— 它**会**跨快照，
    严禁用它读事实表（正是 P2.1 快照纪律要防的叠加）。"""
    return _posix(Path(root) / "**" / "*.parquet")


def register_snapshot_views(
    con: duckdb.DuckDBPyConnection,
    root: str | Path,
    snapshot_id: str,
    *,
    prefix: str = "raw_",
) -> list[str]:
    """为单快照的每张表注册视图 `<prefix><table>`（零拷贝）。

    视图名带 `raw_` 前缀，与契约表（`bars_daily` 等）区分开：
    视图读 Parquet 原样内容（含 `traded` 派生列），契约表只含契约字段。
    """
    directory = Path(root) / snapshot_id
    if not directory.is_dir():
        raise FileNotFoundError(f"快照不存在: {directory}")
    created = []
    for table in SNAPSHOT_TABLES:
        path = directory / f"{table}.parquet"
        if not path.is_file():
            raise FileNotFoundError(f"快照缺少表文件: {path}")
        con.execute(
            f"CREATE OR REPLACE VIEW {prefix}{table} AS "
            f"SELECT * FROM read_parquet('{_posix(path)}')"
        )
        created.append(f"{prefix}{table}")
    return created


def query_parquet_view(
    con: duckdb.DuckDBPyConnection,
    pattern: str,
    sql: str,
    *,
    union_by_name: bool = True,
    with_filename: bool = True,
) -> list[tuple]:
    """对 `read_parquet(pattern)` 执行一段 SQL（`sql` 里用 `_pq` 指代该结果）。

    存在的意义：**把通配符与 SQL 分开**，避免调用方在字符串里手工拼路径。

    两个默认选项是为「跨表通配」而设，缺一不可：
        · `union_by_name=true` —— 跨表通配会读到**不同 schema** 的 Parquet，
          不按列名对齐就会直接报错；
        · `filename=true`     —— 否则**没有** `filename` 列，无法分辨某行来自哪张表/哪个快照。
    """
    opts = []
    if union_by_name:
        opts.append("union_by_name=true")
    if with_filename:
        opts.append("filename=true")
    opt_sql = (", " + ", ".join(opts)) if opts else ""
    con.execute(
        f"CREATE OR REPLACE TEMP VIEW _pq AS "
        f"SELECT * FROM read_parquet('{pattern}'{opt_sql})"
    )
    try:
        # **必须**先取完结果再 DROP：`con.execute()` 返回的是连接对象，
        # 若把连接交出去再 DROP，DROP 会覆盖结果集，调用方 fetchone() 只会得到 None。
        return con.execute(sql).fetchall()
    finally:
        con.execute("DROP VIEW IF EXISTS _pq")


def load_snapshot(
    con: duckdb.DuckDBPyConnection,
    root: str | Path,
    snapshot_id: str,
    *,
    migrate: bool = True,
) -> dict[str, int]:
    """把快照物化进 DuckDB 契约表（**不含** `traded` 派生列）。

    返回 {表名: 本次装载行数}。同一 `snapshot_id` 重复装载会被主键拒绝 —— 这**是特性**：
    快照不可变，重复写入应当失败，而不是悄悄翻倍。
    """
    if migrate:
        apply_migrations(con)

    counts: dict[str, int] = {}
    for table in SNAPSHOT_TABLES:
        path = Path(root) / snapshot_id / f"{table}.parquet"
        if not path.is_file():
            raise FileNotFoundError(f"快照缺少表文件: {path}")
        cols = ", ".join(_contract_columns(con, table))
        before = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        # 只投影契约列：夹具的 traded 等派生列不属于契约表
        con.execute(
            f"INSERT INTO {table} ({cols}) "
            f"SELECT {cols} FROM read_parquet('{_posix(path)}')"
        )
        after = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        counts[table] = int(after - before)
    return counts


def _contract_columns(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return [r[0] for r in rows]
