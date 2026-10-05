"""评估层（LOCAL_DEPLOYMENT_PLAN.md §P6.2）。

    metrics.py —— 收益派生指标 + 年化口径（日历推导，不写死 365/252）
    report.py  —— 报告生成（Parquet/CSV/PNG/Markdown/tearsheet.html）

**年化口径（P6.2 核心验收）**：全平台**唯一**入口 `derive_periods_per_year` ——
由数据自身的会话密度推导每年期数，CAGR / Sharpe / 波动率**不得**各自硬编码 252/365。

对外入口（惰性导出，避免 import 时拖入 matplotlib / pandas 之外的依赖）：
    from quantlab.eval.metrics import performance_metrics, derive_periods_per_year
    from quantlab.eval.report import write_run_outputs
"""

from __future__ import annotations

_EXPORTS = {
    "derive_periods_per_year": "quantlab.eval.metrics",
    "max_drawdown": "quantlab.eval.metrics",
    "total_return": "quantlab.eval.metrics",
    "annualized_return": "quantlab.eval.metrics",
    "annualized_volatility": "quantlab.eval.metrics",
    "sharpe_ratio": "quantlab.eval.metrics",
    "turnover": "quantlab.eval.metrics",
    "rebalance_count": "quantlab.eval.metrics",
    "performance_metrics": "quantlab.eval.metrics",
    "write_run_outputs": "quantlab.eval.report",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(__all__)
