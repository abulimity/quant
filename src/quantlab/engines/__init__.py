"""引擎适配层（LOCAL_DEPLOYMENT_PLAN.md §P4）。

    bridge.py           —— 跨环境桥（core 侧）：job.json + Parquet + subprocess
    base.py             —— BacktestRunner 协议、DataBundle / BacktestResult、注册表
    execution.py        —— **统一撮合语义真值**（T 日收盘决策 → T+1 开盘成交）
    reference.py        —— 语义真值 runner（oracle，非业务引擎）
    backtrader_runner.py—— 高保真执行
    bt_runner.py        —— 组合层

**隔离纪律**：`envs/vbt` / `envs/x2` 里的代码**不得** import 本包；
一切交换走 `bridge.run_in_env(...)`。

对外入口（**惰性导出** —— 避免一 import 就把 backtrader / bt 拉进 `sys.modules`）：
    from quantlab.engines.base import DataBundle, get_runner, load_bundle_from_fixture
    from quantlab.engines.bridge import run_in_env
"""

from __future__ import annotations

_EXPORTS = {
    "BacktestResult": "quantlab.engines.base",
    "BacktestRunner": "quantlab.engines.base",
    "DataBundle": "quantlab.engines.base",
    "EngineError": "quantlab.engines.base",
    "UnknownEngineError": "quantlab.engines.base",
    "available_engines": "quantlab.engines.base",
    "get_runner": "quantlab.engines.base",
    "load_bundle_from_fixture": "quantlab.engines.base",
    "register": "quantlab.engines.base",
    "register_builtin": "quantlab.engines.base",
    "run_in_env": "quantlab.engines.bridge",
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
