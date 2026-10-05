"""报告生成（LOCAL_DEPLOYMENT_PLAN.md §P6.2 / F.10）。

`write_run_outputs` 把一次回测的全部产出落进 `runs/<run_id>/`：

    equity.parquet / positions.parquet / trades.parquet / weights.parquet   （真相，Parquet）
    同名 .csv                                                                （人类可读副本）
    metrics.json      —— 指标 + 年化口径声明
    spec.json         —— 规格（规格为真相）
    params.json       —— 成本 / 初始资金 / 动量窗口等运行参数
    run_meta.json     —— 复现三件套 + 审计标记（needs_human_review 等）
    equity.png        —— 净值曲线（归一为 1.0）+ 基准线
    report.md         —— 结论摘要（累计收益 / CAGR / 最大回撤 / 波动率 / Sharpe /
                        换手 / 成本 / 无风险利率 0 披露 / 年化口径）
    tearsheet.html    —— 自包含 HTML（内嵌 PNG + 指标表）

落盘纪律：Parquet 走 `atomic_write_parquet`（临时文件 + 原子替换），文本走
`atomic_write_text` —— 报告产物也是「真相」，不得原地半截覆盖。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pandas as pd

from quantlab.store.atomic import atomic_write_parquet, atomic_write_text

# 报告里要展示的关键指标（按此顺序 + 标签渲染）
_METRIC_ROWS = (
    ("initial_equity", "初始权益"),
    ("final_equity", "期末权益"),
    ("total_return", "累计收益"),
    ("annualized_return", "年化收益 (CAGR)"),
    ("annualized_volatility", "年化波动率"),
    ("sharpe_ratio", "Sharpe"),
    ("max_drawdown", "最大回撤（带符号，≤0）"),
    ("turnover", "换手 Σ|Δw|"),
    ("n_rebalances", "换手次数"),
)


def _write_json(obj, path: Path) -> None:
    atomic_write_text(
        path, json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")


def _reset_ts(frame, value_name: str | None = None) -> pd.DataFrame:
    """把 index 为时间轴的 Series/DataFrame 转成含 `ts` 列的宽表（供落盘）。

    首列恒命名 `ts`（不论原 index 名是 ts / index / date），保证 Parquet/CSV 回读口径统一。
    """
    if isinstance(frame, pd.Series):
        df = frame.to_frame(name=value_name or frame.name or "value")
    else:
        df = pd.DataFrame(frame)
    df = df.reset_index()
    df = df.rename(columns={df.columns[0]: "ts"})
    return df


def _write_table(df: pd.DataFrame, run_dir: Path, stem: str) -> None:
    atomic_write_parquet(df, run_dir / f"{stem}.parquet")
    df.to_csv(run_dir / f"{stem}.csv", index=False, encoding="utf-8")


def _fmt(value) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:,.4f}"
    return str(value)


def _equity_png(run_dir: Path, equity: pd.Series) -> bytes:
    """净值曲线 PNG（归一为 1.0 + 基准线），返回 PNG 字节供 tearsheet 内嵌。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    values = pd.Series(equity, dtype="float64").dropna()
    normalized = values / float(values.iloc[0])

    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot(normalized.index, normalized.to_numpy(), label="组合净值（归一）", linewidth=1.4)
    ax.axhline(1.0, color="0.6", linestyle="--", linewidth=1.0, label="基准 1.0")
    ax.set_title("Equity curve (normalized to 1.0)")
    ax.set_xlabel("ts")
    ax.set_ylabel("net asset value")
    ax.legend(loc="best")
    fig.autofmt_xdate()
    fig.tight_layout()

    png_path = run_dir / "equity.png"
    fig.savefig(png_path, dpi=110)
    plt.close(fig)
    return png_path.read_bytes()


def _report_md(metrics: dict, spec_dict: dict, params: dict) -> str:
    lines = ["# 回测报告", ""]
    name = spec_dict.get("name", "(未命名)")
    lines.append(f"- 规格：`{name}`")
    lines.append(f"- 成本情景：`{params.get('cost_label', 'custom')}` "
                 f"（单边 {params.get('one_way_bps', '?')} bps）")
    lines.append("")
    lines.append("## 收益指标")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    for key, label in _METRIC_ROWS:
        lines.append(f"| {label} | {_fmt(metrics.get(key))} |")
    lines.append("")
    lines.append("## 口径声明")
    lines.append("")
    note = metrics.get("annualization_note", "")
    if note:
        lines.append(f"- {note}")
    lines.append("- 无风险利率：默认 0（Sharpe 计算中已披露）。")
    lines.append("- 最大回撤为**带符号**值（≤0），报告展示绝对值请取 `abs(max_drawdown)`。")
    lines.append("")
    return "\n".join(lines) + "\n"


def _tearsheet_html(metrics: dict, spec_dict: dict, params: dict, png: bytes) -> str:
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    rows = "\n".join(
        f"<tr><td>{label}</td><td>{_fmt(metrics.get(key))}</td></tr>"
        for key, label in _METRIC_ROWS if key in metrics)
    title = spec_dict.get("name", "回测")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title} tearsheet</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 24px; color: #1a1a1a; }}
img {{ max-width: 100%; border: 1px solid #ddd; }}
table {{ border-collapse: collapse; margin-top: 12px; }}
td, th {{ border: 1px solid #ccc; padding: 6px 12px; text-align: left; }}
th {{ background: #f4f4f4; }}
</style></head><body>
<h1>{title} — tearsheet</h1>
<img src="{data_uri}" alt="equity curve">
<table><tr><th>指标</th><th>值</th></tr>{rows}</table>
<p style="color:#666">{metrics.get("annualization_note", "")}</p>
</body></html>
"""


def write_run_outputs(
    run_dir,
    *,
    equity: pd.Series,
    positions: pd.DataFrame,
    trades: pd.DataFrame,
    weights: pd.DataFrame,
    metrics: dict,
    spec_dict: dict,
    params: dict,
    run_meta: dict,
) -> Path:
    """把一次回测的全部产出写进 `run_dir`，返回该目录（Path）。"""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    # 真相（Parquet）+ CSV 副本
    _write_table(_reset_ts(equity, "equity"), run_dir, "equity")
    _write_table(_reset_ts(positions), run_dir, "positions")
    _write_table(pd.DataFrame(trades), run_dir, "trades")
    _write_table(_reset_ts(weights), run_dir, "weights")

    # JSON
    _write_json(metrics, run_dir / "metrics.json")
    _write_json(spec_dict, run_dir / "spec.json")
    _write_json(params, run_dir / "params.json")
    _write_json(run_meta, run_dir / "run_meta.json")

    # 图与报告
    png = _equity_png(run_dir, equity)
    atomic_write_text(run_dir / "report.md", _report_md(metrics, spec_dict, params))
    atomic_write_text(run_dir / "tearsheet.html",
                      _tearsheet_html(metrics, spec_dict, params, png))

    return run_dir
