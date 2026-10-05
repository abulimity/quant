"""数据源注册表 —— 接线层（LOCAL_DEPLOYMENT_PLAN.md 附录 A「换供应商 = 换 adapter」）。

把 `config/sources.toml` 的**声明**（source id）接到真实的 bundle builder：

    SOURCE_REGISTRY[source_id] -> RegisteredSource{source_id, source_tag, builder, root, check}

关键不变量（CLAUDE.md「变更留证」+ P2.5 幂等）：

    · `source_tag` 是烘焙进 snapshot_id / bars_daily.source / ingest_runs.source 的
      字符串，与既有快照**必须一致**（synthetic / tushare / tushare_index /
      tushare_hk / tushare_macro），否则重跑会换 snapshot_id、v_bars_latest 漂移。
      新增 `futu` 用新标签，天然不冲突。
    · 统一 `BundleBuilder` 签名归一异构 builder 的差异：synthetic 无参、tushare*
      需 start/end/token、futu 无 token。**不改**各 builder 的快照 ID 派生。

本模块只做「映射」，不实现 fetch/normalize 逻辑 —— 那些在 realdata / adapters。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

from quantlab.fixtures.synth import FixtureBundle, check_invariants, generate
from quantlab.ingest.realdata import (
    build_futu_bundle,
    build_tushare_bundle,
    build_tushare_hk_bundle,
    build_tushare_index_bundle,
    build_tushare_macro_bundle,
    check_real_invariants,
)
from quantlab.paths import DATA_ROOT


class BundleBuilder(Protocol):
    def __call__(
        self,
        *,
        start: date,
        end: date,
        symbols: tuple[str, ...] | None,
        token: str | None,
    ) -> FixtureBundle: ...


@dataclass(frozen=True)
class RegisteredSource:
    source_id: str
    source_tag: str                              # 烘焙进快照/台账的字符串（不变）
    builder: BundleBuilder
    root: Path
    check: Callable[[FixtureBundle], list[str]]


# ---- 统一签名包装：归一异构 builder（不改其快照 ID 派生） ----
def _build_synthetic(*, start, end, symbols, token) -> FixtureBundle:
    return generate()


def _build_tushare(*, start, end, symbols, token) -> FixtureBundle:
    return build_tushare_bundle(start=start, end=end, symbols=symbols, token=token)


def _build_tushare_index(*, start, end, symbols, token) -> FixtureBundle:
    return build_tushare_index_bundle(start=start, end=end, token=token)


def _build_tushare_hk(*, start, end, symbols, token) -> FixtureBundle:
    return build_tushare_hk_bundle(token=token)


def _build_tushare_macro(*, start, end, symbols, token) -> FixtureBundle:
    return build_tushare_macro_bundle(start=start, end=end, token=token)


def _build_futu(*, start, end, symbols, token) -> FixtureBundle:
    return build_futu_bundle(start=start, end=end, symbols=symbols)


_SYNTH_ROOT = DATA_ROOT / "bronze" / "synthetic"
_TUSHARE_ROOT = DATA_ROOT / "bronze" / "tushare"
_FUTU_ROOT = DATA_ROOT / "bronze" / "futu"


SOURCE_REGISTRY: dict[str, RegisteredSource] = {
    "synthetic": RegisteredSource(
        "synthetic", "synthetic", _build_synthetic, _SYNTH_ROOT, check_invariants,
    ),
    "tushare": RegisteredSource(
        "tushare", "tushare", _build_tushare, _TUSHARE_ROOT, check_real_invariants,
    ),
    "tushare_index": RegisteredSource(
        "tushare_index", "tushare_index", _build_tushare_index,
        _TUSHARE_ROOT, check_real_invariants,
    ),
    "tushare_hk": RegisteredSource(
        "tushare_hk", "tushare_hk", _build_tushare_hk,
        _TUSHARE_ROOT, check_real_invariants,
    ),
    "tushare_macro": RegisteredSource(
        "tushare_macro", "tushare_macro", _build_tushare_macro,
        _TUSHARE_ROOT, check_real_invariants,
    ),
    "futu": RegisteredSource(
        "futu", "futu", _build_futu, _FUTU_ROOT, check_real_invariants,
    ),
}
