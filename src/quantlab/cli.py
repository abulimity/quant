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
from pathlib import Path

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


def _parse_int_list(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    try:
        return [int(p) for p in parts]
    except ValueError as exc:
        raise SystemExit(f"2\n--universe/--symbols 必须是逗号分隔的整数：{raw!r}（{exc}）")


def _cmd_run(args: argparse.Namespace) -> int:
    from quantlab.contract.types import F8_SCENARIOS
    from quantlab.pipeline import run_full_chain

    costs = None
    if args.costs is not None:
        if args.costs not in F8_SCENARIOS:
            print(f"未知成本情景 {args.costs}，可选 {sorted(F8_SCENARIOS)}", file=sys.stderr)
            return 2
        costs = F8_SCENARIOS[args.costs]

    dsl_payload = None
    if args.dsl:
        dsl_payload = json.loads(Path(args.dsl).read_text(encoding="utf-8-sig"))
    spec_arg = Path(args.spec) if args.spec else None

    universe = tuple(_parse_int_list(args.universe) or [])
    symbols = _parse_int_list(args.symbols)

    try:
        result = run_full_chain(
            spec=spec_arg, paper=args.paper, dsl=dsl_payload,
            universe=universe, symbols=symbols, costs=costs,
            out_dir=args.out, run_vbt=not args.no_vbt,
            initial_cash=args.initial_cash,
            warehouse=args.warehouse, register=args.register, run_id=args.run_id,
        )
    except Exception as exc:  # noqa: BLE001 —— CLI 边界：报错 + 非零退出，不静默
        print(f"run 失败：{exc}", file=sys.stderr)
        return 1

    print(json.dumps({
        "run_id": result.run_id,
        "spec_id": result.spec_id,
        "out_dir": str(result.out_dir),
        "metrics": {k: v for k, v in result.metrics.items() if k != "annualization_note"},
        "parity": result.parity,
        "needs_human_review": result.needs_human_review,
    }, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quantlab", description="本地量化研究平台")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="抓取并落成不可变快照")
    p_ingest.add_argument("--source", default="synthetic",
                          help="数据源（synthetic / tushare / tushare_index / tushare_hk / tushare_macro）")
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

    p_run = sub.add_parser("run", help="全链路六段：规格/论文 → 三引擎 → 组合 → 报告")
    src = p_run.add_mutually_exclusive_group(required=True)
    src.add_argument("--paper", default=None, help="论文路径（LLM 解析，非确定）")
    src.add_argument("--dsl", default=None, help="受控 DSL JSON 文件（确定）")
    src.add_argument("--spec", default=None, help="手写 StrategySpec JSON 文件（确定）")
    p_run.add_argument("--universe", default=None, help="逗号分隔的内部 symbol_id（覆盖 spec.universe）")
    p_run.add_argument("--symbols", default=None, help="逗号分隔的 symbol_id（回测子集，缺省=universe）")
    p_run.add_argument("--costs", type=int, choices=(0, 10, 30), default=None,
                       help="成本情景（单边 bps；缺省用 spec 自带成本）")
    p_run.add_argument("--initial-cash", type=float, default=1_000_000.0, help="初始资金")
    p_run.add_argument("--out", default=None, help="产出目录（缺省 runs/<run_id>/）")
    p_run.add_argument("--run-id", default=None, help="显式 run_id（缺省自动生成）")
    p_run.add_argument("--no-vbt", action="store_true", help="跳过 vectorbt 粗筛")
    p_run.add_argument("--register", action="store_true", help="把 run 登记进 DuckDB")
    p_run.add_argument("--warehouse", default=None, help="DuckDB 台账路径（--register 时用）")
    p_run.set_defaults(func=_cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
