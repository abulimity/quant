"""多策略净值组合 + 收益归因（LOCAL_DEPLOYMENT_PLAN.md §P6.1）。

组合语义（**确定性**，逐日推进）：

    · 每个子策略给一条净值曲线（NAV，index=交易日，base 货币）。
    · 在 `target_weights` 的再平衡日，把权重**重置**为目标权重（行和 ≤ 1，余下为现金）。
    · 其余日子，权重随子策略当日收益**漂移**：`w' = w·(1+r_i) / (1 + Σ w·r_i)`。
    · 组合日收益 = Σ w·r_i（现金收益 0），净值累乘。

**验证锚点 = 手工复算**（子 NAV × 权重的加权收益），见 `tests/test_p6_portfolio.py`。

单策略时本函数是**恒等**：`target_weights` 恒为 1.0 的单一列时，
组合净值恰好等于该子策略净值（初始本金为 1 的情形下）。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class ComposeResult:
    equity: pd.Series        # index=ts, 组合净值（base 货币）
    weights_used: pd.DataFrame  # index=ts, columns=子策略名，当期持有权重


def _union_index(equities: dict[str, pd.Series]) -> pd.DatetimeIndex:
    index: pd.DatetimeIndex | None = None
    for series in equities.values():
        index = series.index if index is None else index.union(series.index)
    if index is None:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(index).sort_values()


def compose(
    equities: dict[str, pd.Series],
    target_weights: pd.DataFrame,
    *,
    initial_equity: float = 1.0,
    rebalance: str | None = None,
) -> ComposeResult:
    """把多策略净值按目标权重组合成一条组合净值。

    参数：
        equities       —— {子策略名: 净值 Series}（同一 base 货币）
        target_weights —— index=再平衡日、columns=子策略名，行和 ≤ 1（余下为现金）
        initial_equity —— 组合初始本金（默认 1.0，便于当"归一净值"用）
        rebalance      —— 保留参数：再平衡日程由 `target_weights.index` 表达；
                          该参数仅用于显式声明（如 'W-MON'），不参与计算。

    语义细节：**再平衡日在当日收益之前生效** —— 在日期 t 上，若 t 是再平衡日则先
    重置权重为目标，再按该权重吃 t 的当日收益，最后漂移权重供 t+1 使用。
    """
    if not isinstance(equities, dict) or not equities:
        raise ValueError("equities 必须是非空 dict[str, Series]")
    if not isinstance(target_weights, pd.DataFrame):
        raise TypeError("target_weights 必须是 DataFrame")

    names = sorted(equities)
    tw = target_weights.reindex(columns=names).fillna(0.0)
    row_sum = tw.abs().sum(axis=1)
    over = row_sum > 1.0 + 1e-9
    if bool(over.any()):
        raise ValueError(
            f"target_weights 行和 > 1（杠杆）：最大 {float(row_sum[over].max()):.6f}，"
            f"如 {tw.index[over][0].date()}。F.1 明确不加杠杆。")

    index = _union_index(equities)
    returns = {
        name: equities[name].reindex(index).pct_change().fillna(0.0)
        for name in names
    }

    weights = pd.Series(0.0, index=names, dtype="float64")
    equity_rows: dict[pd.Timestamp, float] = {}
    weight_rows: dict[pd.Timestamp, pd.Series] = {}
    nav = float(initial_equity)

    for t in index:
        if t in tw.index:
            weights = tw.loc[t].astype("float64")
        r_t = pd.Series({name: returns[name].loc[t] for name in names}, dtype="float64")
        portfolio_return = float((weights * r_t).sum())
        nav *= 1.0 + portfolio_return
        equity_rows[pd.Timestamp(t)] = nav
        weight_rows[pd.Timestamp(t)] = weights.copy()
        denom = 1.0 + portfolio_return
        if denom > 0:
            weights = weights * (1.0 + r_t) / denom

    equity = pd.Series(equity_rows, dtype="float64", name="equity")
    equity.index.name = "ts"
    weights_used = pd.DataFrame.from_dict(weight_rows, orient="index")
    weights_used.index.name = "ts"
    return ComposeResult(equity=equity, weights_used=weights_used)


def attribute_returns(
    local_returns: pd.Series,
    fx_returns: pd.Series,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """按 F.6 拆出本币 / 汇率 / 交互三项收益。

        (1 + R) = (1 + R_local)(1 + R_fx)
                = 1 + R_local + R_fx + R_local·R_fx

    返回 `(local, fx, interaction)`，其中 `interaction = local·fx`。
    **三项之和只是对总收益的一阶近似**（忽略了高阶交叉），不得当作精确值直接相加；
    精确复合应使用 `(1+local)(1+fx)−1`。
    """
    local = pd.Series(local_returns, dtype="float64")
    fx = pd.Series(fx_returns, dtype="float64")
    interaction = local * fx
    return local, fx, interaction
