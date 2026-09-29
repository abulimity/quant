"""合成夹具（LOCAL_DEPLOYMENT_PLAN.md §P2.2）。

供应商留空期间，**合成夹具是全部验证的载体**：确定性、固定种子、自带已知答案。

对外入口（**惰性导出**，见下）：
    from quantlab.fixtures.synth import generate, write_snapshot, read_snapshot
    from quantlab.fixtures import spec

为什么惰性：若在此**急切**导入 `synth`，则 `python -m quantlab.fixtures.synth`
会触发 runpy 的 "found in sys.modules ... prior to execution" RuntimeWarning，
并被算作噪声证据。PEP 562 的模块级 `__getattr__` 既保持了 `from quantlab.fixtures
import generate` 的写法可用，又不引入急切导入。
"""

from __future__ import annotations

_EXPORTS = {
    "FixtureBundle": "quantlab.fixtures.synth",
    "generate": "quantlab.fixtures.synth",
    "write_snapshot": "quantlab.fixtures.synth",
    "read_snapshot": "quantlab.fixtures.synth",
    "check_invariants": "quantlab.fixtures.synth",
    "CONTENT_KEYS": "quantlab.fixtures.synth",
}

__all__ = ["FixtureBundle", "generate", "write_snapshot", "read_snapshot",
           "check_invariants", "CONTENT_KEYS"]


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(__all__)
