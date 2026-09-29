"""原子写入（LOCAL_DEPLOYMENT_PLAN.md §P2.5「临时文件 → 原子替换」）。

核心纪律：**原始快照不可原地覆盖**。
    · 单个文件：先写 `.tmp-<pid>-<rand>`，`fsync`，再 `os.replace` 到目标。
    · 整个快照目录：先写进同级的 `.staging-<rand>/`，全部落盘后再**一次** `os.replace` 改名。
      目标已存在则**拒绝**（`SnapshotExistsError`）—— 这是「不可覆盖」的实现。

`os.replace` 在同一卷上是原子的：进程在替换前被杀，磁盘上要么是旧内容、要么是新内容，
**不会**出现半个文件。中断恢复测试（P2.5 V3）依赖的正是这一点。
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pandas as pd

STAGING_PREFIX = ".staging-"
TMP_PREFIX = ".tmp-"


class SnapshotExistsError(FileExistsError):
    """目标快照已存在 —— 快照是不可变的，绝不原地覆盖。"""


def _tmp_path(path: Path) -> Path:
    return path.with_name(f"{TMP_PREFIX}{path.name}-{os.getpid()}-{uuid.uuid4().hex[:8]}")


def _fsync_file(fh) -> None:
    fh.flush()
    os.fsync(fh.fileno())


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    """原子写二进制。异常时清理临时文件，不留下半成品。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            _fsync_file(fh)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> Path:
    """原子写文本（显式 encoding，遵循 CLAUDE.md 代码风格）。"""
    return atomic_write_bytes(path, text.encode(encoding))


def atomic_write_parquet(df: pd.DataFrame, path: str | Path) -> Path:
    """原子写 Parquet。先落临时文件再替换，避免读者看到半截文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        df.to_parquet(tmp, index=False, engine="pyarrow", compression="zstd")
        # 必须用**可写**句柄 fsync：Windows 上对只读句柄调用 FlushFileBuffers
        # 会以 EBADF 失败（Errno 9），而 "rb" 正是只读句柄。
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


@contextmanager
def staging_dir(root: str | Path) -> Iterator[tuple[Path, "callable"]]:
    """在 `root` 下开一个暂存目录；**显式 commit 时**才提交为最终快照。

    用法：
        with staging_dir(root) as (staging, commit):
            ...写文件到 staging...
            final = commit(snapshot_id)   # 一次原子改名；已存在则抛 SnapshotExistsError

    异常退出时暂存目录被清理，目标路径**不受影响**。
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f"{STAGING_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}"
    staging.mkdir()
    committed = False

    def commit(final_name: str) -> Path:
        nonlocal committed
        final = root / final_name
        if final.exists():
            raise SnapshotExistsError(
                f"快照已存在，拒绝覆盖: {final}\n"
                f"纪律：原始快照不可原地覆盖；修正数据请**新建快照**。"
            )
        os.replace(staging, final)   # 同卷内原子改名
        committed = True
        return final

    try:
        yield staging, commit
    finally:
        if not committed and staging.exists():
            rmtree(staging)


def rmtree(path: str | Path) -> None:
    """递归删除目录（不依赖 shutil，避免与只读位/链接的额外语义纠缠）。"""
    path = Path(path)
    if not path.exists():
        return
    for child in path.rglob("*"):
        if child.is_file():
            child.unlink(missing_ok=True)
    for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if child.is_dir():
            child.rmdir()
    path.rmdir()


def prune_staging(root: str | Path) -> int:
    """清理遗留的 `.staging-*`（上一次中断留下的）。返回清理数量。"""
    root = Path(root)
    if not root.is_dir():
        return 0
    n = 0
    for leftover in root.glob(f"{STAGING_PREFIX}*"):
        if leftover.is_dir():
            rmtree(leftover)
            n += 1
    return n
