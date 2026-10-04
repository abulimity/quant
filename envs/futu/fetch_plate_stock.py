"""请求 futu OpenD 的 `get_plate_stock`，把板块成分列表落成 bronze 不可变快照。

用法（在仓库根目录执行）：
    uv run --project envs/futu python envs/futu/fetch_plate_stock.py [--plate-code HK.Fund] [--host 127.0.0.1] [--port 11111]

默认目标：`data/bronze/futu/plate_stock/<snapshot_id>/`
    plate_stock.parquet     # futu 返回的原始 DataFrame（列名保持原样）
    manifest.json           # 本次请求的上下文与元数据（可追溯）

约定：
    · host/port 默认取自 FutuOpenD.xml（127.0.0.1:11111），可用环境变量 FUTU_HOST / FUTU_PORT 覆盖。
    · 每次运行写**新快照目录**，旧快照不覆盖（快照不可变）。
    · 原子写：先写临时目录，再 os.replace 到目标目录。
    · ret != RET_OK 或空表时，打印错误并退出非零，不静默成功。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from futu import RET_OK, OpenQuoteContext, SortField

PROJECT_ROOT = Path(os.environ.get("QUANT_ROOT", str(Path(__file__).resolve().parents[2])))
DATA_ROOT = PROJECT_ROOT / "data" / "bronze" / "futu" / "plate_stock"


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _snapshot_id() -> str:
    return _utcnow_str()


def write_snapshot(df, snapshot_dir: Path, manifest: dict) -> None:
    """原子写：先写临时目录，再替换到目标目录（目标已存在则报错，不覆盖）。"""
    if snapshot_dir.exists():
        raise FileExistsError(
            f"快照目录已存在，拒绝覆盖：{snapshot_dir}（快照不可变，请换 snapshot_id 重跑）"
        )
    staging = snapshot_dir.with_name(snapshot_dir.name + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        df.to_parquet(staging / "plate_stock.parquet", index=False)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(staging, snapshot_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="拉取 futu 板块成分列表并落成 bronze 快照")
    parser.add_argument("--plate-code", default="HK.Fund")
    parser.add_argument("--host", default=os.environ.get("FUTU_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FUTU_PORT", "11111")))
    parser.add_argument("--out", default=str(DATA_ROOT))
    args = parser.parse_args(argv)

    quote_ctx = OpenQuoteContext(host=args.host, port=args.port)
    try:
        ret, df = quote_ctx.get_plate_stock(
            args.plate_code, sort_field=SortField.CODE, ascend=True
        )
        if ret != RET_OK:
            print(
                f"get_plate_stock 失败：ret={ret}，error={quote_ctx.get_last_error()}",
                file=sys.stderr,
            )
            return 1
    finally:
        quote_ctx.close()

    if df is None or len(df) == 0:
        print(f"get_plate_stock 返回空表（plate_code={args.plate_code}），拒绝落盘。", file=sys.stderr)
        return 1

    import futu as _futu

    snapshot_dir = Path(args.out) / _snapshot_id()
    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    manifest = {
        "source": "futu",
        "dataset": "plate_stock",
        "plate_code": args.plate_code,
        "sort_field": "CODE",
        "ascend": True,
        "host": args.host,
        "port": args.port,
        "futu_api_version": getattr(_futu, "__version__", "unknown"),
        "fetched_at_utc": fetched_at,
        "rows": int(len(df)),
        "columns": list(df.columns),
    }

    write_snapshot(df, snapshot_dir, manifest)
    print(json.dumps({"snapshot_dir": str(snapshot_dir), "rows": manifest["rows"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
