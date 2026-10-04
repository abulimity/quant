"""DuckDB 仓库连接（LOCAL_DEPLOYMENT_PLAN.md §P2.3）。

读写语义（§3.2）：
    **研究侧一律 `read_only=True`**；只有 ingest 进程持有写连接。

本机实测事实（2026-09-29，Windows 11 / duckdb 1.5.5）——**这不是猜测，是可复现观察**：
    · 第二个**写**连接（另一进程）→ `IOException: Cannot open file ... 另一个程序正在使用此文件`
      → **被正确拒绝**，不会静默损坏。✅ 满足 P2.3「第二个写连接应被拒绝并有明确报错」。
    · **只读连接在写者持有文件时同样被拒绝**（Windows 独占共享锁）。
      → 「单写**多读**」只在**无活跃写者**时成立；DuckDB 文件**不支持**「边写边读」。
    · 同一进程内 `duckdb.connect()` 两次返回**同一个 DB 实例**，故**同进程不是有效的并发测试**。

上述第三点正是本平台「**Parquet 是真相，DuckDB 是查询层**」的实现理由：
ingest 把结果写成 Parquet；查询层在**无写者**时发布/读取。切勿把 DuckDB 当并发事务库。
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

from quantlab.paths import DUCKDB_PATH

DEFAULT_WAREHOUSE = DUCKDB_PATH

WAREHOUSE_ENV = "QUANTLAB_WAREHOUSE"


class WarehouseError(RuntimeError):
    """仓库相关错误的基类。"""


class WarehouseNotFoundError(WarehouseError):
    """只读打开了一个尚不存在的仓库 —— 这是错误，不是「空库」。"""


class WarehouseBusyError(WarehouseError):
    """仓库被另一个**写**进程占用。

    Windows 上 DuckDB 的共享锁是独占的：写者持有文件期间，**其他进程连只读也打不开**。
    这里把它翻译成明确、可操作的报错，而不是把裸 `IOException` 抛给上层。
    """


def warehouse_path(path: str | Path | None = None) -> Path:
    """解析仓库路径：显式参数 > 环境变量 `QUANTLAB_WAREHOUSE` > 默认。"""
    if path is not None:
        return Path(path)
    env = os.environ.get(WAREHOUSE_ENV)
    return Path(env) if env else DEFAULT_WAREHOUSE


def connect(
    read_only: bool = True,
    path: str | Path | None = None,
) -> duckdb.DuckDBPyConnection:
    """连接仓库。**默认只读** —— 研究侧不要传 `read_only=False`。

    参数：
        read_only : 研究侧必须为 True（默认）；仅 ingest 进程可传 False
        path      : 仓库文件；默认 `data/warehouse.duckdb`，可用 `QUANTLAB_WAREHOUSE` 覆盖

    异常：
        WarehouseNotFoundError : 只读打开不存在的仓库（防止把「路径写错」当成「空库」）
        WarehouseBusyError     : 文件被其他进程的写连接占用
    """
    target = warehouse_path(path)

    if read_only:
        if not target.is_file():
            raise WarehouseNotFoundError(
                f"仓库不存在，无法只读打开: {target}\n"
                f"（先跑 ingest 生成仓库；如需新建请用 connect(read_only=False)）"
            )
    else:
        target.parent.mkdir(parents=True, exist_ok=True)

    try:
        return duckdb.connect(str(target), read_only=read_only)
    except duckdb.IOException as exc:
        # 面向终端用户，故用纯文本（不写 Markdown 强调符号）
        raise WarehouseBusyError(
            f"仓库被另一个写进程占用，无法以 read_only={read_only} 打开: {target}\n"
            f"DuckDB 的锁是独占的：写者持有期间，其他进程连只读也打不开。\n"
            f"处置：确认是否仍有 ingest 进程在跑；研究请等 ingest 结束后再读。\n"
            f"注意：不要靠重试掩盖（LOCAL_DEPLOYMENT_PLAN.md 附录 C）。\n"
            f"原始错误：{exc}"
        ) from exc


def table_names(con: duckdb.DuckDBPyConnection) -> list[str]:
    """列出当前库中的表（不含视图），按名排序。测试与诊断用。"""
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' "
        "ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]


def view_names(con: duckdb.DuckDBPyConnection) -> list[str]:
    """列出当前库中的**用户**视图（排除 DuckDB 内置的 duckdb_*/sqlite_*/pragma_*），按名排序。"""
    rows = con.execute(
        "SELECT table_name FROM information_schema.views "
        "WHERE table_schema = 'main' "
        "  AND table_name NOT LIKE 'duckdb\\_%' ESCAPE '\\' "
        "  AND table_name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
        "  AND table_name NOT LIKE 'pragma\\_%' ESCAPE '\\' "
        "ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]
