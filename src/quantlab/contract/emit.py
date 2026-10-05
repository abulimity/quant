"""发射器（LOCAL_DEPLOYMENT_PLAN.md §P3.3）。

    emit_signals(spec, data) -> Signals        信号面板（-1 / 0 / 1 / NaN）
    emit_weights(spec, data) -> TargetWeights  目标权重（行和 ≤ 1）

**因果性（本阶段最硬的约束）**：
    整个求值链**只能向后看**。具体地，
      · `sma/ema/momentum/std` 一律用 pandas 的 `rolling/ewm`（因果）；
      · 价格字段必须先 `shift ≥ 1`（由 `lint.G4` 在入闸时保证）；
      · 权重的排名只用**决策日及之前**的数据。
    于是「改未来数据不动过去信号」是**构造性**的，而不是靠事后测试碰运气 ——
    测试只是把这个性质**钉住**（§P3.3 V3）。

引擎无关：本模块**不 import** backtrader / bt / vectorbt / x2strategy。
面向 backtrader 的代码生成委托 x2strategy（P5 接通；此前由 P4 的 `WeightsStrategy` 兜底）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quantlab.contract.types import (
    ContractViolation,
    Expr,
    StrategySpec,
    validate_signals,
    validate_spec,
    validate_target_weights,
)


# --------------------------------------------------------------------------- #
# 输入数据
# --------------------------------------------------------------------------- #
@dataclass
class MarketData:
    """发射器的输入。

    prices  —— index=交易日(DatetimeIndex)、columns=symbol_id、值为**复权收盘价**。
               用复权价是为了不让拆分/分红污染信号（与 P2.6 的 `close_adj` 对齐）。
    traded  —— 可选交易掩码（同形状，bool）。停牌/缺失处为 False：
               不可成交的标的**不得**产生信号。
    """

    prices: pd.DataFrame
    traded: pd.DataFrame | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.prices, pd.DataFrame):
            raise ContractViolation("MarketData.prices 必须是 DataFrame")
        if not isinstance(self.prices.index, pd.DatetimeIndex):
            raise ContractViolation("MarketData.prices 的 index 必须是 DatetimeIndex")
        if not self.prices.index.is_monotonic_increasing:
            raise ContractViolation("MarketData.prices 的 index 必须按时间升序")
        if self.traded is not None:
            if self.traded.shape != self.prices.shape:
                raise ContractViolation("MarketData.traded 与 prices 形状必须一致")
            self.traded = self.traded.reindex_like(self.prices).astype(bool)

    @property
    def symbols(self) -> list[int]:
        return [int(c) for c in self.prices.columns]

    def tradable(self) -> pd.DataFrame:
        """交易掩码；未提供时视为全部可交易。"""
        if self.traded is None:
            return pd.DataFrame(True, index=self.prices.index, columns=self.prices.columns)
        return self.traded


# --------------------------------------------------------------------------- #
# 表达式求值（严格因果）
# --------------------------------------------------------------------------- #
_SUPPORTED_OPS = (
    "field/const/shift/lag/sma/rolling_mean/std/ema/momentum/"
    "gt/lt/ge/le/eq/cross_above/cross_below/and_/or_/not_/"
    "rank/cross_sectional_rank/condition"
)


def evaluate(expr: Expr, prices: pd.DataFrame) -> pd.DataFrame:
    """在 `prices` 上求值，返回同形状的 DataFrame。**严格因果**（只向后看）。"""
    op, args = expr.op, expr.args

    if op == "field":
        name = args[0]
        if name not in ("close", "price"):
            raise ContractViolation(
                f"field({name!r}) 暂不支持；当前仅 'close'（复权收盘）。"
                f"接入更多字段时请同步扩展 lint.G2 与 MarketData。")
        return prices

    if op == "const":
        return pd.DataFrame(float(args[0]), index=prices.index, columns=prices.columns)

    if op in ("shift", "lag"):
        child = evaluate(args[0], prices)
        n = int(args[1]) if len(args) > 1 else 1
        if n < 1:
            # 与 lint.G4 呼应：shift(0) 等于没 shift，会静默引入未来函数
            raise ContractViolation(
                f"{op} 的位移必须是 ≥1 的整数（得到 {n}）—— shift(0) 不解除未来函数。")
        return child.shift(n)

    if op in ("sma", "rolling_mean"):
        return evaluate(args[0], prices).rolling(_window(args, op), min_periods=_window(args, op)).mean()

    if op == "std":
        n = _window(args, op)
        return evaluate(args[0], prices).rolling(n, min_periods=n).std()

    if op == "ema":
        n = _window(args, op)
        return evaluate(args[0], prices).ewm(span=n, min_periods=n, adjust=False).mean()

    if op == "momentum":
        n = _window(args, op)
        child = evaluate(args[0], prices)
        return child / child.shift(n) - 1.0

    if op in ("gt", "lt", "ge", "le", "eq"):
        left, right = _binary(args, prices, op)
        if op == "gt":
            return left > right
        if op == "lt":
            return left < right
        if op == "ge":
            return left >= right
        if op == "le":
            return left <= right
        return np.isclose(left, right)

    if op in ("cross_above", "cross_below"):
        left, right = _binary(args, prices, op)
        prev_left, prev_right = left.shift(1), right.shift(1)
        if op == "cross_above":
            crossed = (left > right) & (prev_left <= prev_right)
        else:
            crossed = (left < right) & (prev_left >= prev_right)
        # 首行 prev 为 NaN → 比较为 False，故不会凭空产生"穿越"
        return crossed.fillna(False)

    if op in ("and_", "or_"):
        left, right = _binary(args, prices, op)
        left_b = left.fillna(False).astype(bool)
        right_b = right.fillna(False).astype(bool)
        return (left_b & right_b) if op == "and_" else (left_b | right_b)

    if op == "not_":
        return ~evaluate(args[0], prices).fillna(False).astype(bool)

    if op in ("rank", "cross_sectional_rank"):
        if not args:
            raise ContractViolation(f"{op} 需要 (序列, [ascending]) 参数")
        child = _as_frame(args[0], prices)
        # ascending 直接从 args 读布尔值，绝不走 _as_frame（否则 float(True)→1.0）
        ascending = args[1] if len(args) > 1 else False
        if not isinstance(ascending, bool):
            raise ContractViolation(f"{op} 的 ascending 必须是布尔值，得到 {ascending!r}")
        return child.rank(axis=1, method="average", ascending=ascending, na_option="keep")

    if op == "condition":
        if len(args) != 3:
            raise ContractViolation(f"condition 需要 (pred, a, b) 三个参数，得到 {len(args)}")
        pred = _as_frame(args[0], prices).fillna(False).astype(bool)
        a = _as_frame(args[1], prices)
        b = _as_frame(args[2], prices)
        return pd.DataFrame(np.where(pred.to_numpy(), a.to_numpy(), b.to_numpy()),
                            index=prices.index, columns=prices.columns)

    raise ContractViolation(f"未知算子 {op!r}。已支持：{_SUPPORTED_OPS}")


def _window(args: tuple, op: str) -> int:
    if len(args) < 2:
        raise ContractViolation(f"{op} 需要 (序列, 窗口) 两个参数")
    n = args[1]
    if isinstance(n, bool) or not isinstance(n, (int, float)) or n < 1 or float(n) != int(n):
        raise ContractViolation(f"{op} 的窗口必须是 ≥1 的整数，得到 {n!r}")
    return int(n)


def _binary(args: tuple, prices: pd.DataFrame, op: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    if len(args) != 2:
        raise ContractViolation(f"{op} 需要两个参数，得到 {len(args)}")
    return _as_frame(args[0], prices), _as_frame(args[1], prices)


def _as_frame(value, prices: pd.DataFrame) -> pd.DataFrame:
    if isinstance(value, Expr):
        return evaluate(value, prices)
    return pd.DataFrame(float(value), index=prices.index, columns=prices.columns)


# --------------------------------------------------------------------------- #
# 发射
# --------------------------------------------------------------------------- #
def emit_signals(spec: StrategySpec, data: MarketData) -> pd.DataFrame:
    """产出信号面板：entry → +1，exit → −1，其余 0；回看期不足处为 NaN。

    NaN 表示「还看不出信号」（预热期），与 0（「看过了，没有信号」）**语义不同**，
    不可互相替代。
    """
    prices = _universe_view(spec, data)
    warmed = _warmup_mask(spec, prices)

    entry = _bool_frame(spec.entry, prices)
    exit_ = _bool_frame(spec.exit, prices)

    signals = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    signals = signals.mask(entry & warmed, 1.0)
    signals = signals.mask(exit_ & warmed, -1.0)
    signals = signals.mask(~warmed, np.nan)

    # 不可成交处不得有信号（停牌/缺失不该产生「要交易」的意图）
    signals = signals.where(data.tradable()[prices.columns], np.nan)

    validate_signals(signals, name=f"Signals({spec.name})")
    return signals


def emit_weights(
    spec: StrategySpec,
    data: MarketData,
    *,
    signals: pd.DataFrame | None = None,
    momentum_window: int = 63,
) -> pd.DataFrame:
    """产出目标权重：每个调仓日取前 `top_n` 等权，行和 ≤ 1（余下为现金）。

    默认参数按附录 F.8：`top_n=3`、等权、动量窗口 63 个**本地**有效交易时段、
    分数相同时按**内部 ID 稳定排序**、无合格标的则**持有现金**。

    入选条件（F.8「条件」行）：63 期数据齐全，且最新行情**不是异常陈旧数据**。
    """
    prices = _universe_view(spec, data)
    signals = emit_signals(spec, data) if signals is None else signals
    tradable = data.tradable()[prices.columns]

    rebalance_dates = _rebalance_dates(prices.index, spec.sizing.rebalance)

    if spec.ranking is not None:
        # 排名分数由 spec 驱动（横截面算子族 #14）。分数是 close-time 计算（调仓在收盘
        # 决策、T+1 开盘成交），故 `momentum` 等窗口因子无需 shift —— 与下方动量兜底
        # 同一口径。`_max_window` 沿用 lint 的窗口口径做预热。
        from quantlab.contract.lint import _max_window
        score = evaluate(spec.ranking, prices)
        warmup = max(_max_window(spec.ranking), int(spec.lookback), 5)
    else:
        # 向后兼容兜底：硬编码 63 日动量（F.8），ranking=None 时仍走此路径。
        score = prices / prices.shift(momentum_window) - 1.0
        warmup = max(momentum_window, 5)
    warmed = score.notna()
    fresh = _fresh_mask(prices, warmup, tradable)

    # **信号是事件，持仓是状态**（F.4.5：挂单在新信号出现时取消并重算）。
    # 若直接要求「调仓日当天恰好发出 +1」，周中出现的金叉会被整条丢掉 ——
    # 对任何事件型策略，结果都是**永远空仓**：回测看起来正常，收益恒为 0，
    # 没有任何报错。故必须先归约为状态。
    state = position_state(signals)

    weights = pd.DataFrame(0.0, index=rebalance_dates, columns=prices.columns)
    budget = 1.0 - spec.sizing.cash_floor

    for date in rebalance_dates:
        eligible = [s for s in prices.columns
                    if bool(tradable.loc[date, s])
                    and bool(warmed.loc[date, s])
                    and bool(fresh.loc[date, s])
                    and state.loc[date, s] == 1.0]
        if not eligible:
            continue                       # 无合格标的 → 持有现金（该行全 0）

        scores = score.loc[date, eligible]
        if bool(np.isfinite(scores.to_numpy(dtype="float64")).all()):
            # 分数相同时按**内部 ID 稳定排序**（F.8 明文要求）
            ordered = sorted(eligible, key=lambda s: (-float(scores[s]), int(s)))
        else:
            ordered = sorted(eligible, key=int)
        chosen = ordered[: spec.sizing.top_n]
        weights.loc[date, chosen] = budget / len(chosen)

    validate_target_weights(weights, name=f"TargetWeights({spec.name})")
    return weights


def spec2weights(
    spec: StrategySpec,
    data: MarketData,
    *,
    signals: pd.DataFrame | None = None,
    momentum_window: int = 63,
) -> pd.DataFrame:
    """**契约 → 目标权重**的规范发射器（LOCAL_DEPLOYMENT_PLAN.md §P5.5）。

    为什么需要它（§P5.5「关键补口」）：x2strategy 只产 backtrader 代码。
    若不自研这个发射器，`bt` 与 `vectorbt` 就**无法复用同一份规格** ——
    规格只能在 backtrader 那条路上跑，跨引擎一致性就无从谈起。
    有了它，**同一份规格**才能同时喂给 backtrader / bt / vectorbt：
    规格成为三引擎的**单一真相**。

    与 `emit_weights` 的关系：`emit_weights` 是底层发射器（P3.3 已验），
    `spec2weights` 是 P5 的**契约闸门入口** —— 多一步 `validate_spec`，
    结构非法的规格**拒绝发射**（fail-closed），而不是产出一份看着像样的权重。

    返回：`TargetWeights`（index=调仓日、columns=symbol_id、行和 ≤ 1）。
    **引擎无关**：本函数不 import 任何引擎。
    """
    validate_spec(spec)                    # fail-closed：结构非法不发射
    return emit_weights(spec, data, signals=signals, momentum_window=momentum_window)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def position_state(signals: pd.DataFrame) -> pd.DataFrame:
    """把**事件型**信号（+1 / −1 / 0 / NaN）归约成**状态型**持仓（1 / 0）。

        +1  → 进入持仓
        −1  → 离场
         0  → **维持前值**（「看过了，没有新信号」不等于「平仓」）
        NaN → 尚无状态（预热期 / 不可成交）→ 维持前值（初始为未持仓）

    这一步是必需的：信号是**事件**，持仓是**状态**。缺了它，「周中金叉 + 周一调仓」
    会整条丢掉，导致策略**静默空仓**。
    """
    values = signals.to_numpy(dtype="float64")
    state = np.zeros(values.shape, dtype="float64")
    current = np.zeros(values.shape[1], dtype="float64")
    for i in range(values.shape[0]):
        row = values[i]
        current = np.where(row == 1.0, 1.0, np.where(row == -1.0, 0.0, current))
        state[i] = current
    return pd.DataFrame(state, index=signals.index, columns=signals.columns)


def _universe_view(spec: StrategySpec, data: MarketData) -> pd.DataFrame:
    """按 spec.universe 过滤；universe 为空则用全部列。"""
    if not spec.universe:
        return data.prices
    missing = [s for s in spec.universe if s not in data.symbols]
    if missing:
        raise ContractViolation(f"universe 含数据中不存在的 symbol_id: {missing}")
    return data.prices[list(spec.universe)]


def _bool_frame(expr: Expr | None, prices: pd.DataFrame) -> pd.DataFrame:
    if expr is None:
        return pd.DataFrame(False, index=prices.index, columns=prices.columns)
    return evaluate(expr, prices).fillna(False).astype(bool)


def _warmup_mask(spec: StrategySpec, prices: pd.DataFrame) -> pd.DataFrame:
    """回看期未满的位置为 False —— 那些位置不该有信号。"""
    n = max(int(spec.lookback), 1)
    warmed = np.arange(len(prices)) >= n
    return pd.DataFrame(np.broadcast_to(warmed[:, None], prices.shape),
                        index=prices.index, columns=prices.columns)


def _fresh_mask(prices: pd.DataFrame, allowance: int, tradable: pd.DataFrame) -> pd.DataFrame:
    """每个位置距「最近一次有效报价」的会话数 ≤ allowance 才算新鲜。"""
    fresh = pd.DataFrame(False, index=prices.index, columns=prices.columns)
    rows = np.arange(len(prices))
    for symbol in prices.columns:
        valid = (prices[symbol].notna() & tradable[symbol]).to_numpy()
        last = np.maximum.accumulate(np.where(valid, rows, -1))
        gap = np.where(last < 0, 10 ** 9, rows - last)
        fresh[symbol] = gap <= allowance
    return fresh


def _rebalance_dates(index: pd.DatetimeIndex, rule: str) -> pd.DatetimeIndex:
    """按 `rule`（如 'W-MON'）取每个周期的**首个交易日**作为调仓日。

    用「周期内第一个交易日」而非「恰好是周一」，是为了让节假日自然顺延 ——
    F.4.5：节假日或停牌时等待下一个有效交易事件。
    """
    try:
        keys = index.to_period(rule)
    except (ValueError, KeyError) as exc:
        raise ContractViolation(
            f"无法解析调仓规则 {rule!r}（应为 pandas 周期别名，如 'W-MON'）: {exc}") from exc
    frame = pd.DataFrame({"ts": index}, index=index)
    return pd.DatetimeIndex(sorted(frame.groupby(keys)["ts"].min().tolist()))
