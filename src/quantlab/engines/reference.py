"""Reference runner —— 语义**真值**（oracle），不是第四个业务引擎（§P4.5）。

它直接复用 `engines.execution` 的统一口径。因此当对拍偏差超出容差时，
可以按「信号时点 → 成交价 → 成本计提 → 舍入」的顺序**逐层比对**它与其它引擎的输出，
定位到具体是哪一层不一致 —— 而不是笼统地「放宽容差」。

⚠️ 它的输出**不代表任何真实成交**，只代表我们**约定的语义**。
生产结论应由 backtrader（高保真）或 bt（组合层）给出。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from quantlab.contract.types import CostModel
from quantlab.engines.base import BacktestResult, DataBundle, base_run_meta
from quantlab.engines.execution import run_reference


@dataclass
class ReferenceRunner:
    engine: str = "reference"
    initial_cash: float = 1_000_000.0

    def run(self, weights: pd.DataFrame, data: DataBundle, costs: CostModel,
            params: dict | None = None) -> BacktestResult:
        params = params or {}
        initial_cash = float(params.get("initial_cash", self.initial_cash))
        equity, positions, trades, audit = run_reference(
            weights, data, costs, initial_cash=initial_cash)

        total_cost = float(trades["cost"].sum()) if len(trades) else 0.0
        return BacktestResult(
            equity=equity, positions=positions, trades=trades,
            stats={
                "initial_cash": initial_cash,
                "final_equity": float(equity.iloc[-1]),
                "total_cost": total_cost,
                "n_trades": int(len(trades)),
                "min_cash": float(audit["cash"].min()),
            },
            run_meta=base_run_meta(data, self.engine,
                                   initial_cash=initial_cash,
                                   cost_label=costs.label,
                                   one_way_bps=costs.one_way_bps),
        )
