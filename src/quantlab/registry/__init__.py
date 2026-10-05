"""运行登记层（LOCAL_DEPLOYMENT_PLAN.md §P6.3）。

    runs.py —— run registry：DuckDB runs / run_metrics 两表 + 登记（缺字段即失败）

把「一次回测」及其**复现三件套**（`data_snapshot_id` / `env_lock_hash` / `git_sha`）
固化进 DuckDB，研究侧只读可查。登记是 **fail-closed** 的：任一必填字段缺失即抛错，
不静默写入半截记录。

对外入口（惰性导出）：
    from quantlab.registry.runs import register_run, register_metrics, get_run, get_metrics
"""

from __future__ import annotations

_EXPORTS = {
    "register_run": "quantlab.registry.runs",
    "register_metrics": "quantlab.registry.runs",
    "get_run": "quantlab.registry.runs",
    "get_metrics": "quantlab.registry.runs",
    "RunRegistryError": "quantlab.registry.runs",
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
