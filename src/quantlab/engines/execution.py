"""统一的执行语义与撮合内核（LOCAL_DEPLOYMENT_PLAN.md §P4.5）。

**这是 P4 的语义真值**：三引擎对同一契约的理解是否一致，最终都要拿这里的口径去比。

统一口径（**不得**使用任何引擎的默认行为）：

    1. 信号在 **T 日收盘**生成（权重面板的 index 即 T）。
    2. 在 **T+1 开盘价**成交；若 T+1 停牌/缺失，顺延到**下一个可交易会话**（F.4.5）。
    3. 成交价按 **F.6 公式**计提成本：`(1 + r_net) = (1 - cost) × (1 + r_gross)`。
       买入可得份额 = `现金 × (1 - cost) / 价格`（**减法**还原，不是除法）。
    4. **现金不得为负**：买入受当时可用现金约束，不足则**缩减订单**
       （F.5：资金不足时缩减订单并记录目标与实际权重偏差）。
    5. **未成交的卖单不提前释放资金**（F.5）：卖不掉就没有现金，不能拿去支撑买单。
    6. 停牌/缺失处**不得成交**；估值可沿用最近价格，但成交只能发生在真实可交易的事件上。
    7. 权重面板为空时表示**维持既有持仓**（不是清仓）—— 清仓必须显式给出全 0 权重。

`reference` runner 直接复用本模块 —— 它是 oracle，不是第四个业务引擎。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quantlab.contract.types import CostModel
from quantlab.engines.base import DataBundle, EngineError

TRADE_COLUMNS = ["ts", "symbol_id", "side", "units", "price", "gross", "cost",
                 "target_weight", "actual_weight", "reason"]


def cost_fraction(costs: CostModel) -> float:
    """单边综合成本（小数）。换汇成本独立配置，不计入这里。"""
    costs.validate()
    return costs.one_way_bps / 10_000.0


def buy_units(cash: float, price: float, cost: float) -> float:
    """买入可得份额。

    由 F.6 的 `(1 + r_net) = (1 - cost)(1 + r_gross)` 推得：扣掉成本后
    可用于买入的现金是 `cash × (1 - cost)`，故份额 = `现金 × (1 - cost) / 价格`。
    """
    if price <= 0:
        return 0.0
    return max(0.0, cash * (1.0 - cost) / price)


def sell_proceeds(units: float, price: float, cost: float) -> float:
    """卖出所得现金（扣除成本）。"""
    return max(0.0, units * price * (1.0 - cost))


def target_units(target_weights: pd.Series, equity: float, prices: pd.Series) -> pd.Series:
    """目标权重 → 目标份额（按给定价格）。价格缺失/非正处为 0。"""
    units = pd.Series(0.0, index=target_weights.index, dtype="float64")
    for symbol, weight in target_weights.items():
        price = prices.get(symbol, np.nan)
        if weight and np.isfinite(price) and float(price) > 0:
            units[symbol] = (equity * float(weight)) / float(price)
    return units


class MatchEngine:
    """按统一口径逐日推进的撮合内核。

    每根会话的顺序固定为：**执行挂单 → 收盘估值 → 生成次日的挂单**。
    这个顺序不能变：先估值会用到尚未成交的仓位，先挂单会让当日信号当日成交（未来函数）。
    """

    def __init__(self, data: DataBundle, costs: CostModel, *,
                 initial_cash: float = 1_000_000.0, fill_at: str = "open"):
        if fill_at not in ("open", "close"):
            raise EngineError(f"fill_at 只能是 'open' 或 'close'，得到 {fill_at!r}")
        self.data = data
        self.costs = costs
        self.cost = cost_fraction(costs)
        self.initial_cash = float(initial_cash)
        # `fill_at='close'` 用来**模拟 bt 的成交时点**（再平衡日收盘撮合），
        # 以便把「引擎设计不同」与「缺陷」在数值上区分开（§P4.5 要求差异已分类）。
        self.fill_at = fill_at

        self.cash = float(initial_cash)
        self.units = pd.Series(0.0, index=data.symbols, dtype="float64")
        # **估值价**：可沿用最近一次有效收盘（F.4：估值可在限定期限内沿用最近价格）。
        # ffill 只向后看，是因果的，不会引入未来函数。
        #
        # 为什么必须有它：三市场联合索引里含**只有单市场开市**的日子（例如
        # 2015-01-02 是纽交所交易日、而沪深港因元旦休市）。此时该市场的收盘价是 NaN，
        # 若直接用原始收盘价估值，净值会**静默变成 NaN** —— 且不会报任何错。
        # 注意：**成交**仍必须使用真实的当次开盘价（见 execute），绝不能用估值价。
        self.valuation = data.closes.ffill()
        # **成交时点的估值**（开盘价，可沿用最近有效值）：用来在成交那一刻
        # 按权重折算买卖份额。
        self.open_valuation = data.opens.ffill()
        self.pending: dict[int, float] = {}      # symbol -> 目标份额（T 日生成，T+1 执行）
        # 目标**权重**单独留存：`trades.target_weight` 必须是权重，不能拿份额顶替
        # （份额是 9xxx 量级、权重是 0~1，混用会让偏差分析彻底失真）。
        self.pending_weight: dict[int, float] = {}
        self.equity = pd.Series(np.nan, index=data.closes.index, dtype="float64")
        self.position_log: dict[pd.Timestamp, pd.Series] = {}
        self.trades: list[dict] = []

    # ------------------------------------------------------------------ #
    def _positions_value(self, session, *, at_open: bool = False) -> float:
        """按估值价计算持仓市值。持仓非空却无估值价 → **报错**，不静默当 0。"""
        row = (self.open_valuation if at_open else self.valuation).loc[session]
        held = self.units > 0
        if bool(held.any()) and bool(row[held].isna().any()):
            missing = row[held][row[held].isna()].index.tolist()
            raise AssertionError(
                f"{pd.Timestamp(session).date()} 持有 {missing} 却无任何可用估值价 —— "
                f"净值会变成 NaN。这属于数据或撮合缺陷，不得静默吞掉。")
        return float(row[held].fillna(0.0).mul(self.units[held]).sum())

    def schedule(self, session_index: int, weights: pd.DataFrame) -> None:
        """在 `session_index` 这个决策日**收盘**，按目标权重下次日执行的单。

        权重面板里没有该决策日 → **维持既有持仓**（不清仓）。
        要清仓，必须在该日显式给出一行全 0 权重。
        """
        decision_date = self.data.closes.index[session_index]
        if decision_date not in weights.index:
            return                                   # 维持现状
        # 挂单只记**目标权重**；份额在成交那一刻按当时的权益与开盘价折算。
        # 为什么不在这里算份额：决策用 T 日收盘，成交在 T+1 开盘，价格不同。
        # 若按 T 日确定份额，会出现三个后果 —— 现金用不尽、成本约束被绕过
        # （开盘低于收盘时根本触发不了 max_units）、`cash_limited` 永不记录。
        raw = weights.loc[decision_date]
        target = raw.reindex(self.units.index).fillna(0.0)
        # **fail-closed**：若目标里确有非零权重，但对齐全落空（典型原因：列名是
        # 字符串 "1" 而 universe 是整数 1），则所有订单都会静默变成 0 —— 策略
        # 看起来"什么都没买"，既不报错也无从察觉。这里直接报错。
        if not np.isclose(float(np.nansum(np.abs(raw.to_numpy(dtype="float64")))),
                          float(np.abs(target).sum())):
            raise EngineError(
                f"{decision_date.date()} 的目标权重与 universe 对不齐："
                f"权重列={list(raw.index)[:6]}（dtype={raw.index.dtype}），"
                f"universe={list(self.units.index)[:6]}（dtype={self.units.index.dtype}）。\n"
                f"处置：统一列类型（两者都应为内部 symbol_id 整数）。"
                f"**不得**静默当 0 —— 那会让整条策略悄悄空仓。")
        self.pending_weight = {int(s): float(w) for s, w in target.items()}
        self.pending = dict(self.pending_weight)          # 待执行集合（按日清理）

    def _fill_price(self, session, symbol) -> float | None:
        source = self.data.opens if self.fill_at == "open" else self.data.closes
        price = float(source.loc[session, symbol])
        if not np.isfinite(price) or price <= 0:
            return None
        if self.fill_at == "close" and not bool(self.data.traded.loc[session, symbol]):
            return None           # 收盘撮合同样要求当日可交易
        return price

    def execute(self, session) -> None:
        """在 `session` 开盘执行挂单：**先卖后买**，买入受可用现金约束。

        份额在**此刻**按「权益 × 目标权重 ÷ 成交价」折算 —— 这样「目标权重」才
        真的意味着「占权益的比例」，成本也才会真正咬住预算。
        """
        if not self.pending_weight:
            return

        tradable = {s for s in self.pending_weight
                    if bool(self.data.traded.loc[session, s])
                    and self._fill_price(session, s) is not None}

        # 目标份额按**成交时点权益**与成交价折算（F.6：权重 = 占权益的比例）
        equity = self.cash + self._positions_value(session, at_open=True)
        targets = {}
        for symbol in tradable:
            price = self._fill_price(session, symbol)
            targets[symbol] = equity * float(self.pending_weight.get(symbol, 0.0)) / price

        # 1) **先卖**（含**减仓**与清仓）：F.5 —— 未成交的卖单不提前释放资金
        #
        # ⚠️ 这里必须处理 `delta < 0` 的**一般情形**，不能只处理"目标权重为 0"。
        # 早先的实现只在 weight==0 时卖出，于是「由 1.0 减到 0.25」会被**静默忽略** ——
        # 持仓一直不动，没有任何报错。这是典型的静默失效，必须按目标份额减到位。
        for symbol in tradable:
            delta = targets[symbol] - float(self.units[symbol])
            if delta < -1e-12:
                price = self._fill_price(session, symbol)
                self._sell(session, symbol, min(-delta, float(self.units[symbol])), price,
                           float(self.pending_weight.get(symbol, 0.0)))

        # 2) 卖出结算后**重估权益**，再据此决定买入份额（现金约束以此刻为准）
        equity_after = self.cash + self._positions_value(session, at_open=True)
        for symbol in tradable:
            weight = float(self.pending_weight.get(symbol, 0.0))
            price = self._fill_price(session, symbol)
            desired = equity_after * weight / price
            delta = desired - float(self.units[symbol])
            if delta > 0:
                self._buy(session, symbol, delta, price, weight)

        # 停牌/缺价的标的保留挂单，次日重试；现金不足导致的偏差**不重试**
        blocked = {s for s in self.pending_weight if s not in tradable}
        self.pending = {s: self.pending_weight[s] for s in blocked}
        self.pending_weight = {s: self.pending_weight[s] for s in blocked}

    def _sell(self, session, symbol, units, price, target) -> None:
        if units <= 0:
            return
        gross = units * price
        cash = sell_proceeds(units, price, self.cost)
        self.units[symbol] -= units
        self.cash += cash
        self._log(session, symbol, "sell", units, price, gross, gross - cash, target)

    def _buy(self, session, symbol, units, price, target) -> None:
        if units <= 0:
            return
        max_units = buy_units(self.cash, price, self.cost)
        actual = min(units, max_units)
        if actual <= 0:
            return
        gross = actual * price
        self.units[symbol] += actual
        self.cash -= gross
        if self.cash < -1e-9:            # 防御：负现金说明撮合内核有缺陷
            raise AssertionError(f"出现负现金: {self.cash}")
        self._log(session, symbol, "buy", actual, price, gross,
                  gross * self.cost, target,
                  reason=None if actual >= units - 1e-9 else "cash_limited")

    def _log(self, session, symbol, side, units, price, gross, cost, target_weight,
             reason=None) -> None:
        """记录一笔成交。

        `actual_weight` 用**成交价**衡量该标的在总敞口中的占比 —— 与 `target_weight`
        同为「权重」量纲，二者之差才是可解读的偏差。若改用当日收盘估值，会出现
        「同一笔交易在开盘成交、却按收盘价算权重」的量纲错配（曾算出 >1 的权重）。
        """
        marked = self.units.copy()
        total = self.cash + float(marked.mul(price).sum())
        actual_weight = (float(self.units[symbol]) * price / total) if total > 0 else 0.0
        self.trades.append({
            "ts": pd.Timestamp(session), "symbol_id": int(symbol), "side": side,
            "units": float(units), "price": float(price), "gross": float(gross),
            "cost": float(cost), "target_weight": float(target_weight),
            "actual_weight": float(actual_weight), "reason": reason,
        })

    def mark(self, session) -> None:
        """按 `session` 收盘价估值（可沿用最近有效价），并记录持仓。"""
        self.equity.loc[session] = self.cash + self._positions_value(session)
        self.position_log[pd.Timestamp(session)] = self.units.copy()

    # ------------------------------------------------------------------ #
    def frame(self) -> tuple[pd.Series, pd.DataFrame, pd.DataFrame]:
        positions = pd.DataFrame.from_dict(self.position_log, orient="index")
        positions = positions.reindex(self.data.closes.index).fillna(0.0)
        positions.index.name = "ts"
        return self.equity, positions, pd.DataFrame(self.trades, columns=TRADE_COLUMNS)


def run_reference(weights: pd.DataFrame, data: DataBundle, costs: CostModel, *,
                  initial_cash: float = 1_000_000.0, fill_at: str = "open"):
    """按统一口径完整跑一遍，返回 (equity, positions, trades, audit)。

    `audit` 给出逐日现金与「净值 − (持仓市值 + 现金)」的残差，供 V3 断言直接使用。

    `fill_at='close'` 用于**诊断**：它刻意复刻 bt 的成交时点（再平衡日收盘），
    以便把「引擎设计不同」与「缺陷」在数值上分开 —— 若差异因此消失，
    就证明差异来自成交时点而非某处算错。
    """
    engine = MatchEngine(data, costs, initial_cash=initial_cash, fill_at=fill_at)
    cash_log: list[float] = []
    residual_log: list[float] = []

    for i, session in enumerate(data.closes.index):
        engine.execute(session)             # 执行**上一日**收盘挂下的单
        engine.mark(session)                # 按今日收盘估值
        cash_log.append(engine.cash)
        residual_log.append(engine.equity.loc[session]
                            - (engine.cash + engine._positions_value(session)))
        engine.schedule(i, weights)         # 今日收盘生成明日执行的挂单

    equity, positions, trades = engine.frame()
    audit = pd.DataFrame({"cash": cash_log, "net_value_residual": residual_log},
                         index=data.closes.index)
    audit.index.name = "ts"
    return equity, positions, trades, audit
