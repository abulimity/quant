"""适配器骨架（`[VENDOR-TBD]`）—— LOCAL_DEPLOYMENT_PLAN.md §P2.4。

**本包不联网、不导入任何供应商 SDK**。「能导入」与「能用」是两件事：
骨架只保证前者，后者等 `config/sources.toml` 配好后再实现（附录 A）。
"""

from __future__ import annotations

_EXPORTS = {
    "AkshareSource": "quantlab.ingest.adapters.akshare",
    "YfinanceSource": "quantlab.ingest.adapters.yfinance",
    "FredSource": "quantlab.ingest.adapters.macro_fred",
    "TushareSource": "quantlab.ingest.adapters.tushare",
    "FutuSource": "quantlab.ingest.adapters.futu",
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
