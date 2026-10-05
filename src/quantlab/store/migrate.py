"""Schema 迁移执行器（LOCAL_DEPLOYMENT_PLAN.md §P2.1）。

设计：
    · `schema.sql` 是**唯一** DDL 真相；本模块只负责「读出 → 逐句执行 → 登记」。
    · 幂等：DDL 全部使用 `IF NOT EXISTS` / `CREATE OR REPLACE VIEW`，重复执行无副作用（V1）。
    · 漂移检测：把 schema.sql 的**内容哈希**登记到 `schema_migrations`。若文件已变更而库
      仍是旧哈希，则**报错而不是静默重建** —— 强制走新迁移，避免 schema 悄悄分叉。
    · 逐句执行用 DuckDB 自带的 `extract_statements()`（能正确处理注释与字符串里的分号），
      不用 `split(';')` 这种会切坏 DDL 的土办法。

用法：
    from quantlab.store.db import connect
    from quantlab.store.migrate import apply_migrations

    con = connect(read_only=False)
    apply_migrations(con)
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# schema.sql 的版本标签；改动 DDL 时应同步更新，作为漂移检测的人类可读标识。
SCHEMA_VERSION = "0003_close_adj"

MIGRATIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL,
    file_hash  TEXT NOT NULL
)
"""


class SchemaDriftError(RuntimeError):
    """已登记的 schema 与磁盘上的 schema.sql 不一致 —— 需要新迁移，不要静默重建。"""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_schema_sql(path: Path | None = None) -> str:
    """读取并校验 DDL 文件存在。"""
    target = path or SCHEMA_PATH
    if not target.is_file():
        raise FileNotFoundError(f"schema.sql 不存在: {target}")
    return target.read_text(encoding="utf-8")


def apply_migrations(
    con: duckdb.DuckDBPyConnection,
    path: Path | None = None,
    *,
    allow_drift: bool = False,
) -> dict:
    """幂等地建立/校验 schema。返回一份执行报告（供证据留档）。

    参数：
        con         : **可写** DuckDB 连接
        path        : 覆盖 schema.sql 路径（测试用）
        allow_drift : 仅用于测试；生产路径**不要**打开，否则会掩盖 schema 分叉

    异常：
        SchemaDriftError : 已登记哈希 ≠ 当前文件哈希
    """
    sql_text = read_schema_sql(path)
    file_hash = _sha256_text(sql_text)

    con.execute(MIGRATIONS_TABLE_DDL)

    row = con.execute(
        "SELECT version, file_hash FROM schema_migrations WHERE version = ?",
        [SCHEMA_VERSION],
    ).fetchone()

    if row is not None:
        recorded_version, recorded_hash = row
        if recorded_hash != file_hash and not allow_drift:
            raise SchemaDriftError(
                f"schema 漂移：已登记 {recorded_version} 的哈希为 {recorded_hash[:12]}…，"
                f"但 {path or SCHEMA_PATH} 现为 {file_hash[:12]}…\n"
                f"处置：新增一份迁移，**不要**直接改已应用的 schema.sql。"
            )
        applied_now = False
    else:
        applied_now = True

    # 逐句执行（extract_statements 正确处理注释 / 字符串内的分号）
    statements = con.extract_statements(sql_text)
    executed: list[str] = []
    for stmt in statements:
        con.execute(stmt)
        executed.append(" ".join(stmt.query.split())[:80])

    if applied_now:
        con.execute(
            "INSERT INTO schema_migrations VALUES (?, now() AT TIME ZONE 'UTC', ?)",
            [SCHEMA_VERSION, file_hash],
        )

    return {
        "version": SCHEMA_VERSION,
        "file_hash": file_hash,
        "statements": len(executed),
        "applied_now": applied_now,
        "statements_preview": executed,
    }


def schema_version(con: duckdb.DuckDBPyConnection) -> str | None:
    """当前库登记的 schema 版本；未迁移过则返回 None。"""
    has_table = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = 'schema_migrations'"
    ).fetchone()[0]
    if not has_table:
        return None
    row = con.execute(
        "SELECT version FROM schema_migrations ORDER BY applied_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None
