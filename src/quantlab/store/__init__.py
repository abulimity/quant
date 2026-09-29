"""存储层：DuckDB 连接、schema 迁移、快照纪律。

对外入口：
    quantlab.store.db.connect(read_only=True)   —— 连接仓库（研究侧默认只读）
    quantlab.store.migrate.apply_migrations(con) —— 幂等建/迁移 schema
    quantlab.store.snapshot_guard.assert_single_snapshot(sql) —— 防快照叠加哨兵
"""

from quantlab.store.db import WarehouseBusyError, WarehouseNotFoundError, connect

__all__ = ["connect", "WarehouseBusyError", "WarehouseNotFoundError"]
