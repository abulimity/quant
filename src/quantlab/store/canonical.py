"""内容哈希：对**规范化的数据内容**取哈希，**不是** Parquet 文件字节哈希。

依据（LOCAL_DEPLOYMENT_PLAN.md §P2.2 V4）：
    「断言对象是**规范化后的数据内容**（排序 → 固定 dtype → 逐行哈希），
      **不是 Parquet 文件字节哈希** —— 文件元数据会随写入器版本变化，
      字节级比对会产生**假失败**。」

规范化三步：
    1. **排序**：按主键稳定排序（`mergesort`），使行序不影响结果
    2. **固定 dtype**：按 dtype 类别归一到 {int64, float64, bool, datetime64[ns], 文本}
       —— 使 int32/int64、float32/float64 这类**同值不同型**不产生假失败
    3. **逐行哈希**：每行按列序拼接为规范化文本，喂入 sha256

缺失值统一为 `<NA>`；浮点用 `repr()`（Python 3 的最短往返表示，确定性）。
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, datetime
from pathlib import Path

import pandas as pd

NA_TOKEN = "<NA>"
ROW_SEP = "\n"
COL_SEP = "\x1f"

# 字符串列统一 dtype 名，写进哈希头，使「列类型变了」这件事本身可被检测
TEXT_DTYPE = "text"


def _canonical_cell_value(v) -> str:
    """把单元格转成确定性文本。"""
    if v is None:
        return NA_TOKEN
    try:
        if pd.isna(v) is True:
            return NA_TOKEN
    except (TypeError, ValueError):
        pass
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return NA_TOKEN
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return repr(v)
    if isinstance(v, datetime):
        return v.isoformat(sep="T")       # 含 pd.Timestamp；naive UTC 无后缀
    if isinstance(v, date):
        return v.isoformat()
    if hasattr(v, "item"):                # numpy 标量兜底
        try:
            return _canonical_cell_value(v.item())
        except Exception:                 # noqa: BLE001 - 兜底转字符串
            pass
    return str(v)


def _canonical_dtype(series: pd.Series) -> tuple[pd.Series, str]:
    """把一个 Series 归一到规范 dtype，并返回其规范 dtype 名。"""
    kind = series.dtype.kind
    if kind in ("i", "u"):
        return series.astype("int64"), "int64"
    if kind == "f":
        return series.astype("float64"), "float64"
    if kind == "b":
        return series.astype("bool"), "bool"
    if kind == "M":
        return series.astype("datetime64[ns]"), "datetime64[ns]"
    return series.map(_canonical_cell_value).astype("object"), TEXT_DTYPE


def canonicalize(df: pd.DataFrame, key_cols: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """排序 + 固定 dtype。返回 (规范表, 规范 dtype 名列表)。"""
    missing = [c for c in key_cols if c not in df.columns]
    if missing:
        raise KeyError(f"排序键缺失: {missing}；实际列: {list(df.columns)}")

    out = df.sort_values(key_cols, kind="mergesort").reset_index(drop=True)
    dtypes: list[str] = []
    for col in out.columns:
        out[col], dname = _canonical_dtype(out[col])
        dtypes.append(dname)
    return out, dtypes


def canonical_bytes(df: pd.DataFrame, key_cols: list[str]) -> bytes:
    """规范化的字节流（列名 + 规范 dtype + 逐行）。"""
    canon, dtypes = canonicalize(df, key_cols)
    parts: list[str] = [
        COL_SEP.join(map(str, canon.columns)),
        COL_SEP.join(dtypes),
    ]
    for row in canon.itertuples(index=False, name=None):
        parts.append(COL_SEP.join(str(v) for v in row))
    return (ROW_SEP.join(parts) + ROW_SEP).encode("utf-8")


def content_hash(df: pd.DataFrame, key_cols: list[str]) -> str:
    """规范化内容的 sha256（V4 的断言对象）。"""
    return hashlib.sha256(canonical_bytes(df, key_cols)).hexdigest()


def frames_from_dir(directory: str | Path, suffix: str = ".parquet") -> dict[str, pd.DataFrame]:
    """读目录下所有 Parquet，返回 {文件名(不含后缀): DataFrame}。按名排序保证确定性。"""
    directory = Path(directory)
    return {p.stem: pd.read_parquet(p) for p in sorted(directory.glob(f"*{suffix}"))}
