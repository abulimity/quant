"""bt runner（组合层）—— LOCAL_DEPLOYMENT_PLAN.md §P4.3。

**§P4.3 明令禁止**沿用 bt 教程里「本行价格生成权重、本行价格调仓」的写法 ——
那等于用当根收盘同时决策与成交，是最典型的未来函数。

bt 的执行模型是：`WeighTarget` 在**再平衡日收盘**按目标权重撮合。
因此要表达 §P4.5 的「T 日收盘决策 → **T+1 开盘**成交」，bt **原生做不到**：
它只能取到 T+1 的**收盘**。按 §P4.5 的要求，此时**记录差异，而不是让别的引擎迁就它**：

    实现方式：把权重面板整体**前移一根**（决策日 T 的目标在 T+1 生效），
    于是 bt 在 T+1 的**收盘**成交。成交时点与参考口径相差「开盘 vs 收盘」。

该差异写入 `run_meta["fill_convention"]` 与 `known_deviation`，
并在 `docs/deploy/parity_report.md` 中归类为**引擎设计不同**（非缺陷）。
"""

from __future__ import annotations

from dataclasses import dataclass

import bt as btlib
import pandas as pd

from quantlab.contract.types import CostModel
from quantlab.engines.base import BacktestResult, DataBundle, EngineError, base_run_meta
from quantlab.engines.execution import cost_fraction

TRADE_COLUMNS = ["ts", "symbol_id", "side", "units", "price", "gross", "cost",
                 "target_weight", "actual_weight", "reason"]


def next_session(index: pd.DatetimeIndex, decisions: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """把每个决策日映射到它之后的第一个会话（找不到则保留原日）。"""
    mapping = {}
    for day in decisions:
        later = index[index > day]
        mapping[day] = later[0] if len(later) else day
    return pd.DatetimeIndex([mapping[d] for d in decisions], name=index.name)


@dataclass
class BtRunner:
    engine: str = "bt"
    initial_cash: float = 1_000_000.0

    def run(self, weights: pd.DataFrame, data: DataBundle, costs: CostModel,
            params: dict | None = None) -> BacktestResult:
        params = params or {}
        initial_cash = float(params.get("initial_cash", self.initial_cash))
        if costs.fx_cost_bps:
            raise EngineError("bt runner 不处理换汇成本；场景 C 走专用路径")
        if not len(weights):
            raise EngineError("weights 为空 —— bt 需要至少一个再平衡日")

        index = data.closes.index
        shifted = weights.copy()
        shifted.index = next_session(index, weights.index)

        prices = data.closes.ffill()
        commission = cost_fraction(costs)

        name = "weights"
        strategy = btlib.Strategy(name, [
            btlib.algos.RunDaily(),
            btlib.algos.SelectAll(),
            btlib.algos.WeighTarget(shifted),
            btlib.algos.Rebalance(),
        ])
        # ⚠️ 两个 bt 用法陷阱（都踩过）：
        #   1. 签名是 `Backtest(strategy, data)` —— 顺序反了会报
        #      "'Strategy' object has no attribute 'columns'"（它把 Strategy 当价格表）。
        #   2. `Backtest.run()` **返回 None**（只跑并存状态）；要拿 `Result`
        #      必须用 `bt.run(*backtests)`。
        backtest = btlib.Backtest(
            strategy, prices, initial_capital=initial_cash,
            commissions=lambda q, p: abs(q) * p * commission)
        result = btlib.run(backtest)

        # bt 的取值路径（试出来才敢写）：`res` 是 dict-like，按 **backtest 名**取；
        # `res.backtests[name].strategy.values` 才是**账户净值 Series**（不是 DataFrame）。
        bt_obj = result.backtests[name]
        raw = pd.Series(bt_obj.strategy.values)
        raw.index = pd.DatetimeIndex(pd.to_datetime(raw.index))
        equity = raw.reindex(prices.index).dropna().rename("equity")
        equity.index.name = "ts"

        # bt 不直接暴露逐日份额；用「权重 × 当日市值 ÷ 收盘价」反推，仅供内部参考
        weights_matrix = result.get_weights(name)
        weights_matrix.index = pd.DatetimeIndex(pd.to_datetime(weights_matrix.index))
        positions = (weights_matrix.mul(equity, axis=0).div(prices, axis=0)).fillna(0.0)
        positions.index.name = "ts"

        return BacktestResult(
            equity=equity, positions=positions,
            trades=pd.DataFrame(columns=TRADE_COLUMNS),
            stats={
                "initial_cash": initial_cash,
                "final_equity": float(equity.iloc[-1]),
                "n_trades": 0,
            },
            run_meta=base_run_meta(
                data, self.engine, initial_cash=initial_cash, cost_label=costs.label,
                one_way_bps=costs.one_way_bps,
                fill_convention=(
                    "bt 在再平衡日**收盘**撮合；已把权重前移一根以近似 T+1，"
                    "故实际成交时点为 T+1 收盘（非参考口径的 T+1 开盘）"),
                known_deviation="fill_at_close_not_open",
                commission_model="size*price*c（bt 原生）"),
        )
