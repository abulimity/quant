"""清洗与质量校验（LOCAL_DEPLOYMENT_PLAN.md §P2.6）。

分工：
    clean.py  —— Silver / Gold 清洗：复权因子、日历对齐、汇率方向统一、回测输入视图
    checks.py —— 质量规则：唯一键/排序/正价格/OHLC 关系/异常跳变/日历缺口/汇率陈旧度

对外入口（惰性导出）：
    from quantlab.quality.clean import adjust_prices, total_return_index, build_session_status
    from quantlab.quality.checks import run_all_checks
"""

from __future__ import annotations

_EXPORTS = {
    "SessionStatus": "quantlab.quality.clean",
    "build_session_status": "quantlab.quality.clean",
    "split_factors": "quantlab.quality.clean",
    "adjust_prices": "quantlab.quality.clean",
    "total_return_index": "quantlab.quality.clean",
    "normalize_fx_direction": "quantlab.quality.clean",
    "gold_backtest_view": "quantlab.quality.clean",
    "CheckResult": "quantlab.quality.checks",
    "QualityReport": "quantlab.quality.checks",
    "run_all_checks": "quantlab.quality.checks",
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
