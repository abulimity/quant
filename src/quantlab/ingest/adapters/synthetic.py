"""合成夹具数据源（`--source synthetic`）—— LOCAL_DEPLOYMENT_PLAN.md §P2.5。

把 P2.2 的夹具包装成标准 `Source`，使编排器（P2.5）能用**同一条代码路径**
处理「合成」与将来的「真实供应商」。供应商留空期间，这是唯一可跑通的源。

与其它骨架不同，本源**已实现**（不联网，也不需要 SDK）。
"""

from __future__ import annotations

import pandas as pd

from quantlab.fixtures.synth import FixtureBundle, generate
from quantlab.ingest.base import ContractError, FetchSpec

NAME = "synthetic"

# 夹具覆盖的全部数据集（与 fixtures/synth.py 的 CONTENT_KEYS 一致）
DATASETS: tuple[str, ...] = (
    "symbols", "bars_daily", "corporate_actions", "fx_rates",
    "trading_calendar", "macro_series", "fundamentals",
)


class SyntheticSource:
    """确定性合成夹具。`fetch()` 返回内存中的夹具表，不做任何 IO。"""

    name = NAME

    def __init__(self, bundle: FixtureBundle | None = None) -> None:
        self._bundle = bundle

    @property
    def bundle(self) -> FixtureBundle:
        if self._bundle is None:
            self._bundle = generate()
        return self._bundle

    def fetch(self, spec: FetchSpec) -> pd.DataFrame:
        if spec.dataset not in DATASETS:
            raise ContractError(
                f"{NAME}: 夹具不包含数据集 {spec.dataset!r}；可用: {list(DATASETS)}"
            )
        return self.bundle.tables[spec.dataset]

    def normalize(self, raw: pd.DataFrame) -> pd.DataFrame:
        """夹具已是契约形状，故这里是**校验**而非改写。

        编排器仍会对产出跑 `validate_normalized`：万一夹具改了字段而契约没跟上，
        这里就要炸出来，而不是把不一致带进快照。
        """
        return raw
