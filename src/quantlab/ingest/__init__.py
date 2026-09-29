"""接入层：Source 协议、适配器骨架、编排与快照（LOCAL_DEPLOYMENT_PLAN.md §P2.4、§P2.5）。

对外入口（惰性导出，避免子模块被急切导入）：
    from quantlab.ingest.base import Source, FetchSpec, validate_normalized
    from quantlab.ingest.orchestrator import ingest
"""

from __future__ import annotations

_EXPORTS = {
    "Source": "quantlab.ingest.base",
    "FetchSpec": "quantlab.ingest.base",
    "ContractError": "quantlab.ingest.base",
    "validate_normalized": "quantlab.ingest.base",
    "VENDOR_TBD": "quantlab.ingest.base",
    "ingest": "quantlab.ingest.orchestrator",
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
