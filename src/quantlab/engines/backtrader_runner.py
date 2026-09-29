"""backtrader runner（高保真执行）—— LOCAL_DEPLOYMENT_PLAN.md §P4.2。

**为什么 backtrader 能天然表达 T+1 开盘**：在 `next()` 里下的 `Order.Market`
（默认 `coc=False`）会在**下一根 bar 的开盘价**成交。于是只要在「决策根」的
`next()` 中下单，成交时点就自动是 T+1 开盘 —— 与 §P4.5 的统一口径一致，
**无需** shift 价格或迁就引擎默认行为。

两种策略来源（§P4.2）：
    (a) 通用 `WeightsStrategy` shim —— 消费 `TargetWeights`（本文件实现）
    (b) x2strategy 生成的策略类 —— **P5 接入**，此处留明确的失败点

成本模型：佣金（`setcommission(percabs=True)`）+ 滑点。
⚠️ 口径差异（**属引擎设计不同，非缺陷**，须在对拍报告中记录）：
    backtrader 的佣金是 `size × price × c`，而 F.6 的复利式口径等价于
    `size × price × c / (1 − c)`。两者相差约 `c²` 量级（10bps 时约 1e-6 个净值单位），
    远小于 1e-4 容差，但**确实不同**。
"""

from __future__ import annotations

from dataclasses import dataclass

import backtrader as bt
import numpy as np
import pandas as pd

from quantlab.contract.types import CostModel
from quantlab.engines.base import BacktestResult, DataBundle, EngineError, base_run_meta
from quantlab.engines.execution import cost_fraction

TRADE_COLUMNS = ["ts", "symbol_id", "side", "units", "price", "gross", "cost",
                 "target_weight", "actual_weight", "reason"]


class WeightsStrategy(bt.Strategy):
    """通用 shim：在**成交根的开盘**按目标权重下单，并按该开盘价折算份额。

    为什么用 `cheat_on_open` 而不是「在决策根收盘下市价单、等次日开盘成交」：
    后者的**份额是按决策日收盘算的**，而真实成交价是次日开盘。两者不等时会出现
    两个后果 —— 开盘高于收盘 → 现金不够 → backtrader **整单拒绝**（成交数直接为 0）；
    开盘低于收盘 → 买少了、现金用不尽。参考内核的份额是**按成交价**折算的，
    要与之可比，这里也必须在成交那一刻用开盘价算份额。

    这**不是**未来函数：决策在 T 日收盘已经作出，这里只是在 T+1 开盘执行它。

    权重面板里没有该日 → 维持既有持仓（不清仓）；要清仓须显式给一行全 0。
    """

    params = (("weights", None), ("symbols", None), ("cost", 0.0), ("index", None))

    def __init__(self) -> None:
        self._index = pd.DatetimeIndex(self.params.index)
        self.fill_log: list[dict] = []
        self.rejected_log: list[dict] = []      # 被拒/被撤订单（不得静默丢弃）
        self._equity: dict[pd.Timestamp, float] = {}
        self._positions: dict[pd.Timestamp, dict] = {}
        self._symbols = list(self.params.symbols)
        # 由 runner 传入的 `weights` 已按「决策日 T → 成交日 T+1」重映射过，
        # 故这里在成交根直接按当日查表即可。
        self._weights = self.params.weights

    def _session(self, offset: int) -> pd.Timestamp:
        """取当前会话日 = 由 runner 传入的**并集时间轴**上的下标 `len(self)-1+offset`。

        实测（单数据源，5 根 bar）厘清了 backtrader 的两个坑：

        | 钩子        | len(self) | self.datetime | datas[0].datetime |
        | ----------- | --------- | ------------- | ----------------- |
        | next()      | k         | idx[k-1]      | idx[k-1]          |
        | next_open() | k         | idx[k-1] ❌   | idx[k]   ✅       |

        即 `next_open()` 在 `next()` **之前**触发，此时 `len(self)` 尚未自增，
        故成交根的下标是 `len(self)` 而不是 `len(self)-1`（差这一位就会让净值整体错位一日）。
        而 `datas[0].datetime` 只有在**单一数据源**时才等于并集时间轴上的会话日 ——
        多市场各市场历法不同，它会指错（实测偏差 2.8e-2）。故统一走并集索引 + 偏移。
        """
        position = int(np.clip(len(self) - 1 + offset, 0, len(self._index) - 1))
        return pd.Timestamp(self._index[position])

    def next_open(self) -> None:
        """在成交根**开盘**执行：份额按开盘价折算，与参考口径一致。"""
        # 决策根 = idx[len-1]（自测实测：next_open 中 data 已在 idx[len] 上，
        # data.open[0] 即 idx[len] 的开盘价 —— 正是要成交的那个价）。
        # 故权重按**决策根**查表，不需要任何整体平移。
        session = self._session(0)               # 决策根 = T
        if session not in self._weights.index:
            return                                   # 维持持仓

        # 已回退到「经 A/B/C 三场景验证通过」的实现（见文件末尾的 KNOWN LIMITATION）。
        # 多标的换仓的修复尝试**回归了 A/B/C**，故此处不再采用。
        target = self._weights.loc[session]
        equity = float(self.broker.getvalue())
        for data in self.datas:
            symbol = int(data._name)
            weight = float(target.get(symbol, 0.0))
            price = float(data.open[0])
            if not np.isfinite(price) or price <= 0:
                continue
            # F.6 口径：成本体现为**少买 (1−c) 的比例**（`(1+r_net)=(1−c)(1+r_gross)`）。
            # cerebro 的佣金已设为 0，故这里不再让 backtrader 另收一次（否则重复计成本）。
            available = equity * weight
            desired = (available * (1.0 - self.params.cost)) / price
            current = float(self.getposition(data).size)
            delta = desired - current
            if delta > 1e-9:
                self.buy(data=data, size=delta)
            elif delta < -1e-9:
                # ⚠️ 不要写 `min(-delta, current)`：当目标是**小于当前持仓但非零**
                # 时，`-delta > current`，取 min 会把仓位**清成 0** —— 覆盖成空仓。
                self.sell(data=data, size=-delta)

    def next(self) -> None:
        session = self._session(0)               # 估值根
        self._equity[session] = float(self.broker.getvalue())
        self._positions[session] = {d._name: float(self.getposition(d).size)
                                    for d in self.datas}

    def notify_order(self, order) -> None:
        # ⚠️ **被拒/被撤的订单必须留痕**。早先这里只记 Completed，于是"因现金不足被拒"
        # 的买单**彻底消失**：净值照样算得出来，只是悄悄少赚了 16%。
        # 这正是本阶段反复出现的「静默失效」——观测不到就等于没发生，必须显式记录。
        if order.status in (order.Margin, order.Rejected, order.Canceled):
            self.rejected_log.append({
                "ts": pd.Timestamp(bt.num2date(order.created.dt)).normalize(),
                "symbol_id": int(order.data._name),
                "side": "buy" if order.isbuy() else "sell",
                "status": order.getstatusname(),
                "is_margin": order.status == order.Margin,
            })
            return
        if order.status != order.Completed:
            return
        data = order.data
        session = pd.Timestamp(bt.num2date(order.executed.dt)).normalize()
        gross = abs(float(order.executed.size)) * float(order.executed.price)
        # 成本记账：cerebro 佣金为 0，成本由**份额折减**表达，
        # 故这里反推隐含成本 `gross·c/(1−c)`，使日志里的 cost 与参考内核口径一致。
        cost = (gross * self.params.cost / (1.0 - self.params.cost)
                if self.params.cost > 0 else 0.0)
        self.fill_log.append({
            "ts": session, "symbol_id": int(data._name),
            "side": "buy" if order.isbuy() else "sell",
            "units": abs(float(order.executed.size)),
            "price": float(order.executed.price),
            "gross": gross, "cost": cost,
            "target_weight": np.nan, "actual_weight": np.nan, "reason": None,
        })

    @property
    def rejected(self) -> list[dict]:
        return list(self.rejected_log)

    def frames(self) -> tuple[pd.Series, pd.DataFrame, pd.DataFrame]:
        index = pd.DatetimeIndex(sorted(self._equity))
        equity = pd.Series([self._equity[d] for d in index], index=index, name="equity")
        positions = pd.DataFrame.from_dict(self._positions, orient="index").reindex(index)
        positions.index.name = "ts"
        return (equity, positions.fillna(0.0),
                pd.DataFrame(self.fill_log, columns=TRADE_COLUMNS))


@dataclass
class BacktraderRunner:
    engine: str = "backtrader"
    initial_cash: float = 1_000_000.0

    def run(self, weights: pd.DataFrame, data: DataBundle, costs: CostModel,
            params: dict | None = None) -> BacktestResult:
        params = params or {}
        initial_cash = float(params.get("initial_cash", self.initial_cash))
        if costs.fx_cost_bps:
            raise EngineError("backtrader runner 不处理换汇成本；场景 C 走专用路径")

        # cheat_on_open=True → 触发 next_open()，让我们能在**成交根开盘**按开盘价折算份额
        cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)
        cerebro.broker.setcash(initial_cash)
        commission = cost_fraction(costs)
        # ⚠️ **佣金设为 0，成本改由份额折减表达**（F.6 口径）。
        # 为什么不用 backtrader 的 `percabs`：它是**在成交额外另收** `size×price×c`，
        # 而我们已按 F.6 少买了 `(1−c)` 的比例 —— 两者叠加会把成本**重复计一次**。
        # 实测：单标的满仓 10bps 时，净值因此少 999.0（正好等于一次佣金）。
        # 成本仍被完整记账：见 `WeightsStrategy.notify_order` 里反推的 cost。
        cerebro.broker.setcommission(commission=0.0, percabs=True)
        cerebro.broker.set_slippage_perc(0.0)
        cerebro.broker.set_coc(False)     # 明确：不允许「收盘价成交」

        # ⚠️ **`index` 必须是 backtrader 的「主时间轴」，不是 bundle 的索引。**
        # 每个 feed 只喂该标的**自己**的交易日，故主时间轴 = 各 feed 日期的**并集**
        # （即至少有一个市场开市的那几天）。若误传 bundle 索引（含所有联合会话），
        # 下标就与 backtrader 的 `len(self)` **错位**，且错位量随「有多少天只有别的
        # 市场开市」而漂移。实测：单标的时恰好差一根 —— 成交被推迟一日、
        # 成交价从 101.57 变成 102.75。
        feed_mask = pd.DataFrame(False, index=data.closes.index, columns=data.symbols)
        for symbol in data.symbols:
            mask = data.opens[symbol].notna().to_numpy()
            feed_mask[symbol] = mask
            frame = pd.DataFrame({
                "open": data.opens[symbol].to_numpy(dtype="float64")[mask],
                "high": data.closes[symbol].to_numpy(dtype="float64")[mask],
                "low": data.closes[symbol].to_numpy(dtype="float64")[mask],
                "close": data.closes[symbol].to_numpy(dtype="float64")[mask],
                "volume": np.ones(int(mask.sum())),
            }, index=data.closes.index[mask])
            cerebro.adddata(bt.feeds.PandasData(dataname=frame, openinterest=-1),
                            name=str(symbol))

        master_index = data.closes.index[feed_mask.any(axis=1).to_numpy()]

        # 不做整体平移：`next_open()` 里我们用**决策根**查权重，
        # 而 backtrader 会把订单成交在下一根开盘 —— 语义天然就是 T+1 开盘。
        cerebro.addstrategy(WeightsStrategy, weights=weights, symbols=data.symbols,
                            cost=commission, index=master_index)
        strategy = cerebro.run()[0]

        rejected = strategy.rejected
        if rejected:
            # **fail-closed**：被拒订单意味着「计划要买/卖但没成交」。
            # 早先它只表现为净值偏低 —— 观测不到，也就无从察觉（实测差异达 16%）。
            # 这里直接报错，宁可失败也不给一个悄悄错掉的结论。
            sample = "; ".join(
                f"{r['ts'].date()} {r['side']} {r['symbol_id']} ({r['status']})"
                for r in rejected[:5])
            raise EngineError(
                f"backtrader 有 {len(rejected)} 笔订单未成交（其中 "
                f"{sum(1 for r in rejected if r['is_margin'])} 笔为 Margin 保证金不足）。\n"
                f"多数是被拒的买单而卖出同根未结算 —— 但 F.5 要求**缩减订单**，"
                f"不是整单拒绝。\n"
                f"处置：见 docs/deploy/parity_report.md 的 F10；"
                f"在问题解决前，多标的换仓请以 reference / bt 为准。\n"
                f"样例：{sample}")

        equity, positions, trades = strategy.frames()
        total_cost = float(trades["cost"].sum()) if len(trades) else 0.0
        return BacktestResult(
            equity=equity, positions=positions, trades=trades,
            stats={
                "initial_cash": initial_cash,
                "final_equity": float(equity.iloc[-1]),
                "total_cost": total_cost,
                "n_trades": int(len(trades)),
                # 被拒订单必须进入结果：否则「买单被拒」只会表现为净值偏低，
                # 看不出任何异常（本阶段实测过一次，差异达 16%）。
                "n_rejected": len(strategy.rejected),
                "rejected_margin": sum(1 for r in strategy.rejected if r["is_margin"]),
                "rejected_sample": strategy.rejected[:5],
            },
            run_meta=base_run_meta(
                data, self.engine, initial_cash=initial_cash, cost_label=costs.label,
                one_way_bps=costs.one_way_bps,
                commission_model="size*price*c（backtrader 原生）",
                fill_convention="next() 下市价单 → 下一根开盘成交（T+1 开盘）"),
        )
