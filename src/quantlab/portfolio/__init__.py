"""组合层（LOCAL_DEPLOYMENT_PLAN.md §P6.1）。

    compose.py —— 多策略净值组合（再平衡重置目标、其余漂移）+ 收益归因

单策略时 `compose` 是**恒等**（组合净值 = 该策略净值）；多策略时才真正加权组合。
归因 `attribute_returns` 按 F.6 `1+R = (1+R_local)(1+R_fx)` 拆出本币/汇率/交互项。

对外入口（惰性导出）：
    from quantlab.portfolio.compose import compose, attribute_returns
"""

from __future__ import annotations

_EXPORTS = {
    "compose": "quantlab.portfolio.compose",
    "attribute_returns": "quantlab.portfolio.compose",
    "ComposeResult": "quantlab.portfolio.compose",
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
