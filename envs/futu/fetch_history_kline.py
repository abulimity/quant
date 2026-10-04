"""请求 futu OpenD 的日线历史 K 线（**不复权**）与**复权因子**，落成 bronze 不可变快照。

用法（在仓库根目录执行）：
    uv run --project envs/futu python envs/futu/fetch_history_kline.py [--limit N] [--delay 0.3] [--start 2024-10-04] [--end 2026-10-04]

标的清单来源（三选一，优先级从高到低）：
    --codes HK.00700,HK.00005         显式代码列表
    --plate-snapshot PATH             从已有 plate_stock.parquet 读 `code` 列
    缺省                               内部调 get_plate_stock("HK.Fund")（默认全量）

默认目标：`data/bronze/futu/history_kline/<snapshot_id>/`
    kline.parquet     # request_history_kline(autype=NONE) 原始 DataFrame，列名保持原样
    rehab.parquet     # get_rehab(code) 复权因子原始 DataFrame（补 `code` 列以便溯源）
    manifest.json     # 请求上下文 + 复权口径披露 + 两表 rows/columns + errors

口径（F.6 披露）：
    · autype=NONE（不复权）—— 原始价格不可变；复权因子单独存 rehab.parquet，可重算前/后复权。
    · return_kind=price_return —— 不复权原始价不是总收益，不得冒充 total_return。
    · host/port 默认 127.0.0.1:11111，可用环境变量 FUTU_HOST / FUTU_PORT 覆盖。
    · 快照不可变，原子写；单只代码失败不中断其余，收集进 manifest.errors 并最终非零退出（不静默成功）。
    · futu 两道限流（实测撞过）：
        1) 历史K线 / 复权因子各「每30秒最多60次」（≈2 次/秒）——频率超限报「频率太高」。
        2) 正股历史K线额度「每7天100只」——额度打满后报「额度不足（100/100），7天后释放」。
      --delay 默认 1.2s（每只代码 2 个请求 ≈ 1.7 次/秒，留安全边际，规避第 1 道；第 2 道只能靠分批/升级）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from futu import RET_OK, AuType, KLType, OpenQuoteContext, SortField

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data" / "bronze" / "futu" / "history_kline"

DEFAULT_LOOKBACK_DAYS = 730  # 试点短窗口：最近约 2 年


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _snapshot_id() -> str:
    return _utcnow_str()


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _default_start(end: str) -> str:
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    return (end_dt - timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")


def write_snapshot(frames: dict[str, pd.DataFrame], snapshot_dir: Path, manifest: dict) -> None:
    """原子写：先写临时目录，再替换到目标目录（目标已存在则报错，不覆盖）。

    `frames` 是 {文件名(不含 .parquet): DataFrame}；每个 DataFrame 落成同名 `.parquet`。
    """
    if snapshot_dir.exists():
        raise FileExistsError(
            f"快照目录已存在，拒绝覆盖：{snapshot_dir}（快照不可变，请换 snapshot_id 重跑）"
        )
    staging = snapshot_dir.with_name(snapshot_dir.name + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        for name, df in frames.items():
            df.to_parquet(staging / f"{name}.parquet", index=False)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(staging, snapshot_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _resolve_codes(args, quote_ctx) -> tuple[list[str], str]:
    """确定本次要抓取的代码列表（已排序），并返回来源标签。"""
    if args.codes:
        codes = sorted({c.strip() for c in args.codes.split(",") if c.strip()})
        return codes[: args.limit] if args.limit else codes, "cli"

    if args.plate_snapshot:
        path = Path(args.plate_snapshot)
        if not path.exists():
            print(f"plate_snapshot 不存在：{path}", file=sys.stderr)
            return [], "plate_snapshot"
        df = pd.read_parquet(path)
        if "code" not in df.columns:
            print(f"plate_snapshot 缺少 `code` 列：实际列 {list(df.columns)}", file=sys.stderr)
            return [], "plate_snapshot"
        codes = sorted(df["code"].astype(str).tolist())
        return codes[: args.limit] if args.limit else codes, "plate_snapshot"

    ret, df = quote_ctx.get_plate_stock(args.plate_code, sort_field=SortField.CODE, ascend=True)
    if ret != RET_OK:
        print(f"get_plate_stock 失败：ret={ret}，error={df}", file=sys.stderr)
        return [], "plate_api"
    if df is None or len(df) == 0:
        print(f"get_plate_stock 返回空表（plate_code={args.plate_code}），拒绝继续。", file=sys.stderr)
        return [], "plate_api"
    codes = sorted(df["code"].astype(str).tolist())
    return codes[: args.limit] if args.limit else codes, "plate_api"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="拉取 futu 日线历史 K 线（不复权）+ 复权因子并落成 bronze 快照")
    parser.add_argument("--codes", help="显式代码列表，逗号分隔（优先级最高）")
    parser.add_argument("--plate-snapshot", help="从已有 plate_stock.parquet 读 code 列")
    parser.add_argument("--plate-code", default="HK.Fund")
    parser.add_argument("--limit", type=int, default=None, help="只抓排序后前 N 只（默认全部）")
    parser.add_argument("--delay", type=float, default=1.2, help="每只代码之间的限频间隔秒数（默认 1.2，对应 futu「每30秒最多60次」；0 关闭）")
    parser.add_argument("--start", help="开始日期 YYYY-MM-DD（默认 end 往前 730 天）")
    parser.add_argument("--end", default=_today_iso(), help="结束日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--host", default=os.environ.get("FUTU_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FUTU_PORT", "11111")))
    parser.add_argument("--out", default=str(DATA_ROOT))
    args = parser.parse_args(argv)

    start = args.start or _default_start(args.end)

    quote_ctx = OpenQuoteContext(host=args.host, port=args.port)
    try:
        codes, code_source = _resolve_codes(args, quote_ctx)
    finally:
        pass  # 上下文保持打开，后面还要用；close 在抓取完成后统一处理

    if not codes:
        quote_ctx.close()
        return 1

    kline_frames: list[pd.DataFrame] = []
    rehab_frames: list[pd.DataFrame] = []
    errors: dict[str, list[str]] = {}
    ok_count = 0

    for i, code in enumerate(codes):
        ret, kline, _ = quote_ctx.request_history_kline(
            code,
            start=start,
            end=args.end,
            ktype=KLType.K_DAY,
            autype=AuType.NONE,
            max_count=None,
        )
        if ret != RET_OK:
            errors.setdefault(code, []).append(f"kline: ret={ret} {kline}")
        elif kline is None or len(kline) == 0:
            errors.setdefault(code, []).append("kline: 空表")
        else:
            kline_frames.append(kline)

        ret, rehab = quote_ctx.get_rehab(code)
        if ret != RET_OK:
            errors.setdefault(code, []).append(f"rehab: ret={ret} {rehab}")
        elif rehab is not None and len(rehab) > 0:
            rehab = rehab.copy()
            rehab.insert(0, "code", code)  # get_rehab 返回表无 code 列，补上以溯源
            rehab_frames.append(rehab)
        # ret == RET_OK 但空表：标的无除权记录，合法，跳过即可

        if code not in errors:
            ok_count += 1

        if args.delay and i < len(codes) - 1:
            time.sleep(args.delay)

    quote_ctx.close()

    if not kline_frames:
        print("全部代码拉取失败，拒绝落盘。", file=sys.stderr)
        return 1

    kline_df = pd.concat(kline_frames, ignore_index=True)
    rehab_df = (
        pd.concat(rehab_frames, ignore_index=True)
        if rehab_frames
        else pd.DataFrame(columns=["code"])
    )

    import futu as _futu

    snapshot_dir = Path(args.out) / _snapshot_id()
    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    manifest = {
        "source": "futu",
        "dataset": "history_kline",
        "autype": "NONE",                # 不复权（F.6 披露）
        "rehab_separate": True,          # 复权因子单独存 rehab.parquet
        "return_kind": "price_return",   # 原始价非总收益，不得冒充 total_return
        "start": start,
        "end": args.end,
        "codes": codes,
        "code_source": code_source,
        "plate_code": args.plate_code if code_source == "plate_api" else None,
        "host": args.host,
        "port": args.port,
        "futu_api_version": getattr(_futu, "__version__", "unknown"),
        "fetched_at_utc": fetched_at,
        "kline_rows": int(len(kline_df)),
        "kline_columns": list(kline_df.columns),
        "rehab_rows": int(len(rehab_df)),
        "rehab_columns": list(rehab_df.columns),
        "ok_codes": ok_count,
        "failed_codes": len(errors),
        "errors": errors,
    }

    write_snapshot(
        {"kline": kline_df, "rehab": rehab_df},
        snapshot_dir,
        manifest,
    )
    summary = {
        "snapshot_dir": str(snapshot_dir),
        "kline_rows": manifest["kline_rows"],
        "rehab_rows": manifest["rehab_rows"],
        "total_codes": len(codes),
        "ok_codes": ok_count,
        "failed_codes": len(errors),
        "errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if errors:
        print(f"部分失败：{len(errors)}/{len(codes)} 只代码出错，详见 manifest.errors", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
