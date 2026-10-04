"""x2strategy 集成（LOCAL_DEPLOYMENT_PLAN.md §P5）。

    llm.py         —— LLM 通道配置（凭据走环境变量）
    paper2spec.py  —— 论文 → StrategySpec（经桥调用 x2 环境）（P5.2）
    registry.py    —— 规格注册 + 闸门串联（P5.3）

**隔离纪律**：x2strategy / litellm 只装在 `envs/x2`；core 侧**不 import** 它们，
一切经 `engines.bridge.run_in_env("x2", ...)` 调用。

对外入口（惰性导出）：
    from quantlab.x2.llm import load_llm_config, LlmNotConfigured
"""

from __future__ import annotations

_EXPORTS = {
    "LlmConfig": "quantlab.x2.llm",
    "LlmNotConfigured": "quantlab.x2.llm",
    "load_llm_config": "quantlab.x2.llm",
    "DslParseError": "quantlab.x2.dsl",
    "dsl_to_spec": "quantlab.x2.dsl",
    "parse_dsl_node": "quantlab.x2.dsl",
    "DSL_SCHEMA": "quantlab.x2.dsl",
    "extract_dsl": "quantlab.x2.paper2spec",
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
