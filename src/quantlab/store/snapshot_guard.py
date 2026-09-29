"""防「快照叠加」哨兵（LOCAL_DEPLOYMENT_PLAN.md §P2.1 快照读取约定）。

问题：事实表主键含 `snapshot_id`，同一 `(symbol_id, ts)` 会**同时**存在于多个快照中。
裸 `SELECT * FROM bars_daily` 会把多份快照叠加成脏数据（重复行、陈旧值与新值混排），
而且**不会报错** —— 属于最危险的一类静默错误。

本模块是一个**可执行的静态哨兵**：在运行 SQL 前做保守检查，命中即抛错。

能力与边界（**不要**误以为它是完备的 SQL 解析器）：
    能抓：`SELECT ... FROM bars_daily` 这类**未出现 snapshot_id** 的读取。
    不抓：动态拼接的 SQL、经视图间接放大（视图自身已在 schema.sql 定义好单快照语义）。
    因此它是「CI / 代码评审的辅助门」，不是安全边界。真正的纪律是：
        —— 研究侧一律走 `v_bars_latest` 或显式 `WHERE snapshot_id = :one`。

用法：
    from quantlab.store.snapshot_guard import assert_single_snapshot
    assert_single_snapshot("SELECT * FROM bars_daily")                        # -> SnapshotLeakError
    assert_single_snapshot("SELECT * FROM bars_daily WHERE snapshot_id = ?")  # -> 通过
    assert_single_snapshot("SELECT * FROM v_bars_latest")                     # -> 通过
"""

from __future__ import annotations

import re

# 带 snapshot_id 的事实表：读取时必须限定单一快照
FACT_TABLES: frozenset[str] = frozenset(
    {"bars_daily", "corporate_actions", "fx_rates", "macro_series", "fundamentals"}
)

# 已是单快照语义的视图，允许直接读
SAFE_VIEWS: frozenset[str] = frozenset({"v_bars_latest"})

# 事实表引用：FROM/JOIN 后紧跟的标识符（兼容别名写法）
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


class SnapshotLeakError(ValueError):
    """检测到可能跨越多个快照的读取。"""


def _strip_noise(sql: str) -> str:
    """去掉注释与字符串字面量，避免它们里的表名/关键词干扰判断。"""
    sql = _BLOCK_COMMENT.sub(" ", sql)
    sql = _LINE_COMMENT.sub(" ", sql)
    sql = _STRING_LITERAL.sub("''", sql)
    return sql


def referenced_fact_tables(sql: str) -> set[str]:
    """返回 SQL 中被直接读取的事实表名（已剔除注释与字符串）。"""
    clean = _strip_noise(sql)
    mentioned = {m.group(1).lower() for m in _TABLE_REF.finditer(clean)}
    return mentioned & FACT_TABLES


def assert_single_snapshot(sql: str, *, where: str = "<unknown>") -> None:
    """断言 SQL 不会跨快照读取；违反则抛 `SnapshotLeakError`。

    规则：
        · 引用了任一事实表 → SQL 中**必须**出现 `snapshot_id`（或改用 `v_bars_latest`）。
        · 未引用事实表（如只读 `symbols` / `ingest_runs`）→ 直接放行。

    参数：
        sql   : 待检查的 SQL 文本
        where : 出错信息中标注调用点（便于定位），如 "portfolio/optimizer.py:88"
    """
    tables = referenced_fact_tables(sql)
    if not tables:
        return

    clean = _strip_noise(sql)
    if re.search(r"\bsnapshot_id\b", clean, re.IGNORECASE):
        return

    hint = " 或 ".join(sorted(SAFE_VIEWS))
    raise SnapshotLeakError(
        f"[{where}] 读取事实表 {sorted(tables)} 时未限定快照。\n"
        f"事实表主键含 snapshot_id，跨快照读取会把多份快照叠加成脏数据且**不会报错**。\n"
        f"处置：改用 {hint}，或显式加 `WHERE snapshot_id = :one_snapshot`。\n"
        f"SQL：{' '.join(sql.split())[:200]}"
    )
