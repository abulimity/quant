"""契约层：引擎无关的中间表示，四组件协作的唯一接缝（LOCAL_DEPLOYMENT_PLAN.md §P3）。

    types.py —— `Signals` / `TargetWeights` / `StrategySpec` / `CostModel` + 校验器
    lint.py  —— 规格校验闸门（fail-closed）
    emit.py  —— `emit_signals` / `emit_weights` / `spec2weights`（P5.5 规范入口）

对外入口（惰性导出）：
    from quantlab.contract.types import StrategySpec, CostModel, validate_signals
    from quantlab.contract.lint import lint_spec
    from quantlab.contract.emit import emit_signals, emit_weights, spec2weights
"""

from __future__ import annotations

_EXPORTS = {
    "ContractViolation": "quantlab.contract.types",
    "Expr": "quantlab.contract.types",
    "DataRequirement": "quantlab.contract.types",
    "SizingSpec": "quantlab.contract.types",
    "CostModel": "quantlab.contract.types",
    "StrategySpec": "quantlab.contract.types",
    "validate_signals": "quantlab.contract.types",
    "validate_target_weights": "quantlab.contract.types",
    "validate_spec": "quantlab.contract.types",
    "ORIGIN_HANDWRITTEN": "quantlab.contract.types",
    "ORIGIN_X2STRATEGY": "quantlab.contract.types",
    "lint_spec": "quantlab.contract.lint",
    "LintReport": "quantlab.contract.lint",
    "emit_signals": "quantlab.contract.emit",
    "emit_weights": "quantlab.contract.emit",
    "spec2weights": "quantlab.contract.emit",
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
