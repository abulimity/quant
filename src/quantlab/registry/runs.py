"""run registry（LOCAL_DEPLOYMENT_PLAN.md §P6.3）。

`runs` / `run_metrics` 两表 + 登记函数。**fail-closed**：`register_run` 任一必填字段
缺失（None 或空白字符串）即抛 `RunRegistryError`，不写半截记录。`run_metrics` 只收
**有限数值**（非数值 / bool / NaN 直接拒绝），防止把口径说明当指标存进 DOUBLE 列。

**连接注入**：`register_run` / `register_metrics` 只接受已建好的 `con`（由
`quantlab.store.db.connect` + `migrate.apply_migrations` 准备好），不在本模块里
自行开连接 —— 避免同进程 read_only / read_write 配置冲突。

幂等：`runs` 用 `ON CONFLICT (run_id) DO NOTHING`（重复登记同一 run 无副作用）；
`run_metrics` 用 `INSERT OR REPLACE`（同 run 重跑覆盖指标）。`created_at` 存 **naive UTC**。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np

RUN_STATUSES = ("ok", "failed", "partial")


class RunRegistryError(ValueError):
    """run 登记失败（缺字段 / 非法值）—— fail-closed。"""


def _require(name: str, value) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RunRegistryError(f"register_run 缺少字段 {name!r}（不可为空）")


def _naive_utc(created_at) -> datetime:
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    if not isinstance(created_at, datetime):
        raise RunRegistryError("created_at 必须是 datetime")
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
    return created_at


def _params_json(params_json) -> str:
    if isinstance(params_json, str):
        return params_json
    if isinstance(params_json, dict):
        return json.dumps(params_json, ensure_ascii=False, sort_keys=True)
    raise RunRegistryError("params_json 必须是 str 或 dict")


def register_run(
    con,
    *,
    run_id: str,
    spec_id: str,
    engine: str,
    origin: str,
    created_at,
    data_snapshot_id: str,
    env_lock_hash: str,
    git_sha: str,
    params_json,
    status: str,
) -> None:
    """登记一次 run。**任一字段缺失即抛错**（fail-closed），重复 run_id 幂等跳过。"""
    for name, value in (
        ("run_id", run_id), ("spec_id", spec_id), ("engine", engine),
        ("origin", origin), ("data_snapshot_id", data_snapshot_id),
        ("env_lock_hash", env_lock_hash), ("git_sha", git_sha), ("status", status),
    ):
        _require(name, value)
    if status not in RUN_STATUSES:
        raise RunRegistryError(f"status 必须是 {RUN_STATUSES} 之一，得到 {status!r}")

    created = _naive_utc(created_at)
    params = _params_json(params_json)
    con.execute(
        "INSERT INTO runs (run_id, spec_id, engine, origin, created_at, data_snapshot_id, "
        " env_lock_hash, git_sha, params_json, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (run_id) DO NOTHING",
        [run_id, spec_id, engine, origin, created, data_snapshot_id, env_lock_hash,
         git_sha, params, status],
    )


def register_metrics(con, run_id: str, metrics: dict) -> None:
    """登记数值指标（fail-closed：非有限数值直接拒绝，杜绝「口径说明」混入指标）。"""
    _require("run_id", run_id)
    if not isinstance(metrics, dict):
        raise RunRegistryError("metrics 必须是 dict")
    for metric, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
            raise RunRegistryError(
                f"metric {metric!r} 的值必须是数值，得到 {value!r}（口径说明请入报告，不入指标表）")
        fvalue = float(value)
        if not np.isfinite(fvalue):
            raise RunRegistryError(f"metric {metric!r} 的值必须有限，得到 {value!r}")
        con.execute(
            "INSERT OR REPLACE INTO run_metrics (run_id, metric, value) VALUES (?, ?, ?)",
            [run_id, str(metric), fvalue],
        )


_RUN_COLUMNS = (
    "run_id", "spec_id", "engine", "origin", "created_at", "data_snapshot_id",
    "env_lock_hash", "git_sha", "params_json", "status",
)


def get_run(con, run_id: str) -> dict | None:
    """读回一条 run（不存在返回 None）。"""
    row = con.execute(
        "SELECT run_id, spec_id, engine, origin, created_at, data_snapshot_id, "
        "env_lock_hash, git_sha, params_json, status FROM runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_RUN_COLUMNS, row))


def get_metrics(con, run_id: str) -> dict:
    """读回某 run 的全部指标（按 metric 名排序）。"""
    rows = con.execute(
        "SELECT metric, value FROM run_metrics WHERE run_id = ? ORDER BY metric",
        [run_id],
    ).fetchall()
    return {metric: value for metric, value in rows}
