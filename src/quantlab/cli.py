"""quantlab 命令行入口。

用法：
    quantlab ingest --source synthetic --universe fixture
    quantlab schema --print            # 打印契约 DDL（便于人工核对）

设计：CLI 只做**参数解析 + 调用**，业务逻辑一律在 `quantlab.*` 里，
这样每个能力都能被测试直接调用（不必经由 subprocess）。
退出码：0 成功；1 业务失败；2 参数错误（argparse 约定）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from quantlab.ingest.orchestrator import IngestError, ingest
from quantlab.store.db import connect, warehouse_path
from quantlab.store.migrate import SCHEMA_VERSION, apply_migrations, read_schema_sql


def _parse_date(raw: str | None) -> date | None:
    return date.fromisoformat(raw) if raw else None


def _parse_symbols(raw: str | None) -> tuple[str, ...] | None:
    if not raw:
        return None
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts or None


def _cmd_ingest(args: argparse.Namespace) -> int:
    # 先建好**可写**连接并迁移，再交给 ingest 复用 —— 避免同进程内
    # 「一个只读连接 + 一个可写连接指向同一文件」的配置冲突（见 orchestrator._write_target）。
    con = connect(read_only=False, path=warehouse_path(args.warehouse))
    try:
        apply_migrations(con)
        try:
            result = ingest(
                source=args.source,
                root=args.out,
                warehouse=args.warehouse,
                universe=args.universe,
                con=con,
                start=_parse_date(args.start),
                end=_parse_date(args.end),
                symbols=_parse_symbols(args.symbols),
            )
        except IngestError as exc:
            print(f"ingest 失败：{exc}", file=sys.stderr)
            return 1
    finally:
        con.close()
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_schema(args: argparse.Namespace) -> int:
    if args.print:
        print(read_schema_sql())
        return 0
    print(json.dumps({"schema_version": SCHEMA_VERSION}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quantlab", description="本地量化研究平台")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="抓取并落成不可变快照")
    p_ingest.add_argument("--source", default="synthetic", help="数据源（synthetic / tushare）")
    p_ingest.add_argument("--universe", default="fixture", help="synthetic 的标的集合（fixture）")
    p_ingest.add_argument("--start", default=None,
                          help="起始交易日 YYYY-MM-DD（tushare；缺省=研究窗口起点）")
    p_ingest.add_argument("--end", default=None,
                          help="结束交易日 YYYY-MM-DD（tushare；缺省=研究窗口终点）")
    p_ingest.add_argument("--symbols", default=None,
                          help="逗号分隔的 ts_code（tushare；缺省=全量非 REIT 名单）")
    p_ingest.add_argument("--out", default=None,
                          help="快照根目录（缺省按 source 选 data/bronze/<source>）")
    p_ingest.add_argument("--warehouse", default=None,
                          help="DuckDB 台账路径（默认 data/warehouse.duckdb）")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_schema = sub.add_parser("schema", help="契约 schema 信息")
    p_schema.add_argument("--print", action="store_true", help="打印 schema.sql 全文")
    p_schema.set_defaults(func=_cmd_schema)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
