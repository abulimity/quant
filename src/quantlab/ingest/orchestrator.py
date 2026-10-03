"""Ingest 编排与快照（LOCAL_DEPLOYMENT_PLAN.md §P2.5）。

主流程：**校验 → 登记 running → 写临时文件 → 原子替换 → 登记 ok → 更新 watermark**。

四条不变量，各有对应的实现手段：

| 要求 | 手段 |
| --- | --- |
| 不可变（旧快照不可覆盖） | `staging_dir.commit()` 见目标已存在即 `SnapshotExistsError` |
| 中断可恢复 | 目录级 `os.replace`：杀进程后磁盘上要么旧、要么新，**不会半份** |
| 幂等 | 内容相同 → 快照 ID 相同 → 第二次直接判定 `exists`，**不重写、不重复登记** |
| 修正走新快照 | 内容变了 → 内容哈希变 → 快照 ID 变 → 新建，旧快照原封不动 |

`ingest_runs` 的三态生命周期 `running → ok | aborted` 由本模块维护：
    · 进程**硬杀**（`os._exit` / 断电）→ 行永远停在 `running`，正是可审计的中断痕迹；
    · Python 异常 → 标 `aborted` 并重新抛出，绝不静默吞掉。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from quantlab.fixtures.synth import (
    FixtureBundle,
    check_invariants,
    generate,
    snapshot_dir,
    write_snapshot,
)
from quantlab.ingest.realdata import build_tushare_bundle, check_real_invariants
from quantlab.fixtures.spec import STUDY_START, STUDY_END
from quantlab.store.db import connect, warehouse_path
from quantlab.store.migrate import apply_migrations

DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "data" / "bronze" / "synthetic"
TUSHARE_ROOT = Path(__file__).resolve().parents[3] / "data" / "bronze" / "tushare"

# 这些数据集参与 watermark（数据边界）计算
_WATERMARK_TABLES = ("bars_daily", "fx_rates", "macro_series")


class IngestError(RuntimeError):
    """Ingest 过程中的可诊断错误。"""


@dataclass
class IngestResult:
    snapshot_id: str
    status: str                     # 'ok'（新写入） | 'exists'（幂等命中）
    path: Path
    rows: dict[str, int]
    content_hash: str
    already_present: bool

    def as_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "status": self.status,
            "path": str(self.path),
            "row_counts": self.rows,
            "combined_content_hash": self.content_hash,
            "already_present": self.already_present,
        }


def _utcnow() -> datetime:
    """naive UTC（与 schema 口径一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _watermark(bundle: FixtureBundle) -> str | None:
    """本快照的数据边界：参与计算的各表 `ts` 的最大值。"""
    highs = []
    for name in _WATERMARK_TABLES:
        frame = bundle.tables.get(name)
        if frame is not None and len(frame) and "ts" in frame.columns:
            highs.append(pd.to_datetime(frame["ts"]).max())
    return max(highs).date().isoformat() if highs else None


@contextmanager
def _registration(con, bundle: FixtureBundle, source: str):
    """先登记 running；异常时标 aborted 并重抛。

    硬杀进程时本 `finally` 不会执行 —— 那正是我们要的痕迹（行停在 running）。
    """
    datasets = sorted(bundle.tables)
    for dataset in datasets:
        con.execute(
            "INSERT OR REPLACE INTO ingest_runs "
            "(snapshot_id, source, dataset, started_at, finished_at, rows, watermark, "
            " file_hash, status, note) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, "
            " 'running', ?)",
            [bundle.snapshot_id, source, dataset, _utcnow(), "登记于写入之前（用于中断审计）"],
        )
    try:
        yield
    except BaseException:
        for dataset in datasets:
            con.execute(
                "UPDATE ingest_runs SET status = 'aborted', finished_at = ? "
                "WHERE snapshot_id = ? AND dataset = ?",
                [_utcnow(), bundle.snapshot_id, dataset],
            )
        raise


def _mark_ok(con, bundle: FixtureBundle) -> None:
    """写入成功后：逐数据集登记 ok、行数、watermark 与内容哈希。"""
    hashes = bundle.content_hashes()
    watermark = _watermark(bundle)
    finished = _utcnow()
    for dataset, frame in bundle.tables.items():
        con.execute(
            "UPDATE ingest_runs SET finished_at = ?, rows = ?, watermark = ?, "
            "file_hash = ?, status = 'ok', note = ? "
            "WHERE snapshot_id = ? AND dataset = ?",
            [
                finished, int(len(frame)), watermark, hashes[dataset],
                json.dumps({"note": "synthetic fixture"}, ensure_ascii=False),
                bundle.snapshot_id, dataset,
            ],
        )


@contextmanager
def _write_target(warehouse: str | Path | None, con):
    """产出一个**可写**连接。

    ⚠️ 为什么需要注入：DuckDB 在**同一进程**内不允许对同一个库文件同时持有
    配置不同的连接（如一个 `read_only=True`、另一个可写），会直接抛
    `ConnectionException: Can't open a connection to same database file with a
    different configuration than existing connections`。
    因此当调用方**已有**一个可写连接时，必须复用它，而不是另开一个。

    参数：
        con —— 调用方提供的可写连接；为 None 时由本函数自建并负责关闭。
    """
    if con is not None:
        yield con
        return
    owned = connect(read_only=False, path=warehouse_path(warehouse))
    try:
        apply_migrations(owned)
        yield owned
    finally:
        owned.close()


def ingest_bundle(
    bundle: FixtureBundle,
    root: str | Path | None = None,
    *,
    warehouse: str | Path | None = None,
    source: str = "synthetic",
    register: bool = True,
    con=None,
    check: Callable[[FixtureBundle], list[str]] = check_invariants,
) -> IngestResult:
    """把一个 bundle 落成**不可变快照**并登记。幂等。

    `con` —— 可选：调用方已有的**可写**连接。传入则复用、**不由本函数关闭**
    （见 `_write_target` 里关于同进程连接配置冲突的说明）。

    `check` —— 落盘前的自洽性检查：合成夹具用 `fixtures.synth.check_invariants`，
    真实数据用 `realdata.check_real_invariants`。
    """
    root = Path(root) if root is not None else DEFAULT_ROOT
    content_hash = bundle.combined_content_hash()

    bad = check(bundle)
    if bad:
        raise IngestError("数据不变量未通过，拒绝落盘：\n  - " + "\n  - ".join(bad))

    target = snapshot_dir(bundle.snapshot_id, root)
    rows = {name: int(len(frame)) for name, frame in bundle.tables.items()}

    if target.is_dir():
        # 幂等：内容相同 → 同 ID → 不重写、不重复登记
        if register:
            _ensure_registered(bundle, warehouse, source, con)
        return IngestResult(bundle.snapshot_id, "exists", target, rows, content_hash, True)

    if not register:
        write_snapshot(bundle, root)
        return IngestResult(bundle.snapshot_id, "ok", target, rows, content_hash, False)

    with _write_target(warehouse, con) as writable:
        with _registration(writable, bundle, source):
            write_snapshot(bundle, root)     # 原子：失败则不留下半份快照
        _mark_ok(writable, bundle)
    return IngestResult(bundle.snapshot_id, "ok", target, rows, content_hash, False)


def _ensure_registered(bundle: FixtureBundle, warehouse, source: str, con=None) -> None:
    """幂等路径下补齐登记：快照已在但台账缺行时补成 ok，不重复插入。"""
    with _write_target(warehouse, con) as writable:
        existing = writable.execute(
            "SELECT count(*) FROM ingest_runs WHERE snapshot_id = ?", [bundle.snapshot_id]
        ).fetchone()[0]
        if existing == 0:
            with _registration(writable, bundle, source):
                pass
            _mark_ok(writable, bundle)


def ingest(
    source: str = "synthetic",
    root: str | Path | None = None,
    *,
    warehouse: str | Path | None = None,
    universe: str = "fixture",
    con=None,
    start: date | None = None,
    end: date | None = None,
    symbols: tuple[str, ...] | None = None,
) -> IngestResult:
    """按 `--source` 分发到合成夹具或真实供应商。"""
    if source == "synthetic":
        if universe not in ("fixture", "synthetic"):
            raise IngestError(f"未知 universe={universe!r}；当前仅支持 'fixture'。")
        return ingest_bundle(generate(), root, warehouse=warehouse, source=source, con=con)

    if source == "tushare":
        bundle = build_tushare_bundle(
            start=start or STUDY_START,
            end=end or STUDY_END,
            symbols=symbols,
        )
        tushare_root = Path(root) if root is not None else TUSHARE_ROOT
        return ingest_bundle(
            bundle, tushare_root, warehouse=warehouse, source=source, con=con,
            check=check_real_invariants,
        )

    raise IngestError(
        f"数据源 {source!r} 尚未接入（VENDOR-TBD）。\n"
        f"当前可用：source='synthetic'（合成夹具）、source='tushare'（境内 ETF）。\n"
        f"接入其它供应商见 LOCAL_DEPLOYMENT_PLAN.md 附录 A。"
    )
