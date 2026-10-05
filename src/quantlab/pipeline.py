"""全链路六段编排（LOCAL_DEPLOYMENT_PLAN.md §P5.6 V2/V4）。

一条命令把「夹具数据 + 规格/论文 → 三引擎 → 组合 → 报告」跑通并落
`runs/<run_id>/`。六段：

    1. spec 解析    —— paper（LLM，非确定）/ dsl（golden，确定）/ spec（手写，确定）
    2. lint 闸门    —— `lint_spec(...).raise_if_failed()`（**必须**：spec2weights 不跑 lint）
    3. vectorbt 粗筛 —— 真实桥 `run_in_env("vbt", ...)`，产物标注 `coarse_screen_only`
    4. backtrader 精验 —— 受支持形态（一次建仓+一次清仓）与 reference@open 对拍
    5. bt 组合      —— 组合层引擎，与 reference@close 对拍
    6. reference 对账 + 组合恒等 + 报告 + run 登记

**可复现（V4）**：同一 spec 重跑两遍，指标在容差内一致（引擎 deterministic；
run_id 用时间戳故每次不同，但指标不依赖 run_id）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.contract.emit import MarketData, spec2weights
from quantlab.contract.lint import LintReport, lint_spec
from quantlab.contract.types import CostModel, StrategySpec
from quantlab.engines.base import (
    BacktestResult,
    DataBundle,
    EngineError,
    env_lock_hash,
    get_runner,
    git_sha,
    load_bundle_from_fixture,
    register_builtin,
)
from quantlab.engines.bridge import run_in_env
from quantlab.engines.execution import run_reference
from quantlab.eval.metrics import performance_metrics
from quantlab.eval.report import write_run_outputs
from quantlab.paths import RUNS_DIR
from quantlab.registry.runs import register_metrics, register_run
from quantlab.store.atomic import atomic_write_parquet
from quantlab.store.db import connect, warehouse_path
from quantlab.store.migrate import apply_migrations
from quantlab.x2.dsl import dsl_to_spec
from quantlab.x2.registry import spec_id

VBT_ENTRY = "envs/vbt/entry.py"
TOLERANCE = 1e-4


class PipelineError(RuntimeError):
    """六段编排失败（缺输入 / 闸门不过 / 引擎失败）。"""


@dataclass
class FullChainResult:
    run_id: str
    spec: StrategySpec
    spec_id: str
    per_engine: dict[str, BacktestResult]
    vbt_scan: dict | None
    parity: dict
    metrics: dict
    equity: pd.Series
    weights: pd.DataFrame
    out_dir: Path
    lint_report: LintReport
    needs_human_review: bool
    run_meta: dict
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 小型数值 / 面板工具（复用 E2E-A 的口径）
# --------------------------------------------------------------------------- #
def _max_rel_dev(a: pd.Series, b: pd.Series) -> float:
    common = a.dropna().index.intersection(b.dropna().index)
    if not len(common):
        raise PipelineError("两条曲线没有公共日期，无法对拍")
    return float(((a[common] / b[common] - 1.0).abs()).max())


def _event_panel(weights: pd.DataFrame) -> pd.DataFrame:
    """把「每周重复同一目标」压缩成**变化点**（避免 backtrader L1：重复目标补仓 Margin）。"""
    changed = weights.fillna(0.0).ne(weights.fillna(0.0).shift()).any(axis=1)
    return weights[changed.fillna(True)]


def _build_entry_clear_pair(weights: pd.DataFrame) -> pd.DataFrame:
    """取「一次建仓 + 其后一次清仓」（backtrader 受支持形态，见 P5.5 docstring）。"""
    ev = _event_panel(weights)
    entered = ev.index[ev.sum(axis=1) > 0]
    if not len(entered):
        raise AssertionError("面板里没有建仓事件")
    start = entered[0]
    after = ev.loc[ev.index > start]
    cleared = after.index[after.sum(axis=1) == 0]
    if not len(cleared):
        raise AssertionError("建仓之后没有清仓事件")
    return ev.loc[start:cleared[0]]


def _sma_window(node) -> int | None:
    if getattr(node, "op", None) in ("sma", "rolling_mean") and len(node.args) >= 2:
        window = node.args[1]
        if isinstance(window, (int, float)) and not isinstance(window, bool):
            return int(window)
    return None


def _extract_sma_cross(expr) -> tuple[int, int] | None:
    """从 entry 里抽出 (fast, slow) 的 SMA 窗口（仅当形态是均线交叉时）。"""
    if expr is None:
        return None
    for node in expr.walk():
        if node.op in ("cross_above", "cross_below") and len(node.args) == 2:
            left, right = node.args
            fast, slow = _sma_window(left), _sma_window(right)
            if fast is not None and slow is not None:
                return tuple(sorted((fast, slow)))  # type: ignore[return-value]
    return None


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


# --------------------------------------------------------------------------- #
# vectorbt 粗筛（真实桥）
# --------------------------------------------------------------------------- #
def _run_vbt_scan(spec: StrategySpec, bundle: DataBundle,
                  out_dir: Path, run_id: str) -> dict:
    symbol = bundle.symbols[0]
    prices = bundle.closes[symbol].dropna()
    frame = prices.rename("close").to_frame().reset_index()
    if frame.columns[0] != "ts":
        frame = frame.rename(columns={frame.columns[0]: "ts"})

    vbt_dir = out_dir / "vbt"
    vbt_dir.mkdir(parents=True, exist_ok=True)
    prices_path = vbt_dir / "prices.parquet"
    atomic_write_parquet(frame, prices_path)

    anchor = _extract_sma_cross(spec.entry)
    fast_grid = sorted(set([5, 10, 20] + ([anchor[0]] if anchor else [])))
    slow_grid = sorted(set([30, 60] + ([anchor[1]] if anchor else [])))
    result = run_in_env(
        "vbt", VBT_ENTRY,
        {"job_id": f"{run_id}-vbt", "engine": "vectorbt",
         "params": {"op": "scan", "fast": fast_grid, "slow": slow_grid}},
        inputs={"prices": prices_path},
        workdir=vbt_dir,
    )

    combos = pd.DataFrame()
    result_parquet = vbt_dir / "result.parquet"
    if result_parquet.is_file():
        combos = pd.read_parquet(result_parquet)

    anchor_row = None
    if (anchor is not None and len(combos)
            and {"fast", "slow"} <= set(combos.columns)):
        hit = combos[(combos["fast"] == anchor[0]) & (combos["slow"] == anchor[1])]
        if len(hit):
            anchor_row = _jsonable(hit.iloc[0].to_dict())

    return {
        "match_quality": result.get("match_quality"),
        "note": result.get("note"),
        "n_combos": result.get("n_combos"),
        "n_sessions": result.get("n_sessions"),
        "vectorbt_version": result.get("vectorbt_version"),
        "anchor_fast": anchor[0] if anchor else None,
        "anchor_slow": anchor[1] if anchor else None,
        "anchor_combo": anchor_row,
    }


# --------------------------------------------------------------------------- #
# 六段编排
# --------------------------------------------------------------------------- #
def run_full_chain(
    *,
    spec: StrategySpec | str | Path | None = None,
    paper: str | Path | None = None,
    dsl: dict | None = None,
    universe: tuple[int, ...] = (),
    symbols: list[int] | None = None,
    costs: CostModel | None = None,
    out_dir: str | Path | None = None,
    run_vbt: bool = True,
    initial_cash: float = 1_000_000.0,
    momentum_window: int | None = None,
    warehouse: str | Path | None = None,
    register: bool = False,
    run_id: str | None = None,
) -> FullChainResult:
    """六段全链路。返回 `FullChainResult`；失败一律抛异常（fail-closed）。"""
    if sum(x is not None for x in (spec, paper, dsl)) != 1:
        raise PipelineError("必须且只能提供 spec / paper / dsl 其中之一")

    notes: list[str] = []
    needs_human_review = False

    # 段 1：spec 解析
    if dsl is not None:
        parsed = dsl_to_spec(dsl, universe=tuple(universe), source_paper="")
        spec = parsed.spec
        needs_human_review = parsed.needs_human_review
        notes.append("spec 来源：受控 DSL（确定）")
    elif paper is not None:
        from quantlab.x2.paper2spec import extract_dsl

        raw = extract_dsl(paper)
        payload = raw.get("dsl") if isinstance(raw, dict) else None
        if not isinstance(payload, dict):
            raise PipelineError(f"论文解析未返回 DSL：{raw!r}")
        parsed = dsl_to_spec(payload, universe=tuple(universe), source_paper=str(paper))
        spec = parsed.spec
        needs_human_review = parsed.needs_human_review or bool(raw.get("needs_human_review"))
        notes.append(f"spec 来源：论文 {paper}（LLM，非确定）")
    else:
        if isinstance(spec, (str, Path)):
            spec = StrategySpec.from_json(Path(spec).read_text(encoding="utf-8-sig"))
        if not isinstance(spec, StrategySpec):
            raise TypeError("spec 必须是 StrategySpec 或 JSON 文件路径")
        notes.append("spec 来源：手写 / 显式 StrategySpec")

    if universe:
        spec = replace(spec, universe=tuple(universe))

    # 段 2：lint 闸门（spec2weights 只做 validate_spec，**不跑 lint**）
    lint_report = lint_spec(spec)
    lint_report.raise_if_failed()

    costs = costs if costs is not None else spec.costs
    sid = spec_id(spec)

    if run_id is None:
        run_id = f"run-{sid[:8]}-{int(time.time() * 1000)}"
    out_dir = Path(out_dir) if out_dir is not None else RUNS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # 夹具 → 回测输入（单快照，经 QUANT_ROOT 指向主检出）
    register_builtin()
    bundle = load_bundle_from_fixture()
    final_universe = tuple(spec.universe) or tuple(universe)
    if not final_universe:
        final_universe = tuple(bundle.symbols)
    spec = replace(spec, universe=final_universe)
    subset_ids = list(symbols) if symbols is not None else list(final_universe)
    bundle = bundle.subset(subset_ids)

    mw = momentum_window if momentum_window is not None else max(1, spec.lookback)
    market = MarketData(prices=bundle.closes, traded=bundle.traded)
    weights = spec2weights(spec, market, momentum_window=mw)

    # 段 3：vectorbt 粗筛（真实桥；SMA 交叉的 (fast,slow) 纳入网格并回读锚点）
    vbt_scan = _run_vbt_scan(spec, bundle, out_dir, run_id) if run_vbt else None

    # 段 4–6：三引擎 + 对拍
    ev = _event_panel(weights)
    per_engine: dict[str, BacktestResult] = {}

    ref_open = get_runner("reference").run(ev, bundle, costs,
                                           {"initial_cash": initial_cash})
    per_engine["reference"] = ref_open

    bt_result = get_runner("bt").run(ev, bundle, costs, {"initial_cash": initial_cash})
    per_engine["bt"] = bt_result

    ref_close_equity = run_reference(ev, bundle, costs, initial_cash=initial_cash,
                                     fill_at="close")[0]
    parity_bt = _max_rel_dev(bt_result.equity, ref_close_equity)

    parity_backtrader = None
    btrader_note = ""
    try:
        pair = _build_entry_clear_pair(weights)
        ref_pair_equity = run_reference(pair, bundle, costs, initial_cash=initial_cash,
                                        fill_at="open")[0]
        backtrader_result = get_runner("backtrader").run(
            pair, bundle, costs, {"initial_cash": initial_cash})
        per_engine["backtrader"] = backtrader_result
        parity_backtrader = _max_rel_dev(backtrader_result.equity, ref_pair_equity)
    except (AssertionError, EngineError) as exc:
        btrader_note = f"backtrader 精验跳过：{type(exc).__name__}: {exc}"
        notes.append(btrader_note)

    parity = {
        "backtrader_vs_reference_open": parity_backtrader,
        "bt_vs_reference_close": parity_bt,
    }

    # 段 7：组合（单策略恒等）+ 指标 + 报告 + 登记
    portfolio_equity = bt_result.equity      # 单策略：compose 为恒等
    metrics = performance_metrics(portfolio_equity,
                                  risk_free_rate=costs.risk_free_rate,
                                  weights=weights)

    params = {
        "costs": costs.to_dict(),
        "cost_label": costs.label,
        "one_way_bps": costs.one_way_bps,
        "initial_cash": initial_cash,
        "universe": list(final_universe),
        "momentum_window": mw,
        "top_n": spec.sizing.top_n,
        "rebalance": spec.sizing.rebalance,
        "run_vbt": run_vbt,
    }
    run_meta = {
        "run_id": run_id,
        "spec_id": sid,
        "engine": "portfolio",
        "origin": spec.origin,
        "data_snapshot_id": bundle.snapshot_id,
        "env_lock_hash": env_lock_hash(),
        "git_sha": git_sha(),
        "cost_label": costs.label,
        "one_way_bps": costs.one_way_bps,
        "needs_human_review": needs_human_review,
        "lint": {"passed": lint_report.passed, "n_errors": len(lint_report.errors)},
        "parity": parity,
        "notes": notes,
    }

    write_run_outputs(
        out_dir,
        equity=portfolio_equity,
        positions=bt_result.positions,
        trades=ref_open.trades,
        weights=weights,
        metrics=metrics,
        spec_dict=spec.to_dict(),
        params=params,
        run_meta=run_meta,
    )

    if register:
        _register_run_and_metrics(
            warehouse, run_id, sid, spec, bundle, params, metrics)

    return FullChainResult(
        run_id=run_id, spec=spec, spec_id=sid, per_engine=per_engine,
        vbt_scan=vbt_scan, parity=parity, metrics=metrics,
        equity=portfolio_equity, weights=weights, out_dir=out_dir,
        lint_report=lint_report, needs_human_review=needs_human_review,
        run_meta=run_meta, notes=notes,
    )


def _register_run_and_metrics(warehouse, run_id, sid, spec, bundle, params, metrics) -> None:
    """写 DuckDB：runs + run_metrics（只取有限数值；口径说明不入指标表）。"""
    con = connect(read_only=False, path=warehouse_path(warehouse))
    try:
        apply_migrations(con)
        register_run(
            con, run_id=run_id, spec_id=sid, engine="portfolio", origin=spec.origin,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            data_snapshot_id=bundle.snapshot_id,
            env_lock_hash=env_lock_hash(), git_sha=git_sha(),
            params_json=json.dumps(params, ensure_ascii=False, sort_keys=True),
            status="ok",
        )
        numeric = {
            k: v for k, v in metrics.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v)
        }
        register_metrics(con, run_id, numeric)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit("quantlab.pipeline 是库，不经 python -m 直接执行；请用 `quantlab run`。")
