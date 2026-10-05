"""收益派生指标 + 年化口径（LOCAL_DEPLOYMENT_PLAN.md §P6.2）。

**年化口径是全局唯一入口**：`derive_periods_per_year` 从**数据自身的会话密度**推导
每年期数（交易日日频 ≈ 252、自然日日频 ≈ 365），而不是写死 365/252。CAGR / 波动率 /
Sharpe 的 `periods_per_year` 一律由它（或显式覆盖）提供 —— **不得**各自硬编码。

**无风险利率默认 0 并披露**：`sharpe_ratio` 的 `risk_free_rate` 默认 0.0，
`performance_metrics` 返回的 `annualization_note` 里显式声明这两条口径。

**max_drawdown 的符号约定**：返回**带符号**的最大回撤（≤ 0，即 `equity / 峰值 − 1`
的最小值）。带符号是为了能与累计收益**交叉核验**（`cum_return + drawdown` 在同一处
恰好等于零），报告层再按需要取绝对值展示。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def derive_periods_per_year(index: pd.DatetimeIndex) -> float:
    """由会话密度推导**每年期数**（全平台唯一入口，不写死 365/252）。

        periods_per_year = n / span_years
        span_years        = (index.max() − index.min()).days / 365.25

    交易日日频样本 ≈ 252、自然日日频样本 ≈ 365，都由数据自身推出。样本不足
    两个会话或跨度为零时返回 NaN（调用方据此显式记录「无法年化」，而非猜一个数）。
    """
    if not isinstance(index, pd.DatetimeIndex):
        index = pd.DatetimeIndex(pd.to_datetime(index))
    n = len(index)
    if n < 2:
        return float("nan")
    span_days = (index.max() - index.min()).days
    if span_days <= 0:
        return float("nan")
    return float(n / (span_days / 365.25))


def total_return(equity: pd.Series) -> float:
    """累计收益 = 末值 / 初值 − 1。"""
    values = pd.Series(equity, dtype="float64").dropna()
    if not len(values):
        return float("nan")
    return float(values.iloc[-1] / values.iloc[0] - 1.0)


def max_drawdown(equity: pd.Series) -> float:
    """带符号的最大回撤（≤ 0）。详见模块 docstring 的符号约定。"""
    values = pd.Series(equity, dtype="float64").dropna()
    if not len(values):
        return float("nan")
    drawdown = values / values.cummax() - 1.0
    return float(drawdown.min())


def annualized_return(
    equity: pd.Series,
    periods_per_year: float | None = None,
) -> float:
    """年化收益（CAGR）= (末/初)^(1/年数) − 1，年数 = n / periods_per_year。"""
    values = pd.Series(equity, dtype="float64").dropna()
    if not len(values):
        return float("nan")
    ppy = _resolve_ppy(values.index, periods_per_year)
    if ppy is None:
        return float("nan")
    total = float(values.iloc[-1] / values.iloc[0])
    years = len(values) / ppy
    if total <= 0 or years <= 0:
        return float("nan")
    return float(total ** (1.0 / years) - 1.0)


def annualized_volatility(
    returns: pd.Series,
    periods_per_year: float | None = None,
) -> float:
    """年化波动率 = 期收益率标准差 × sqrt(periods_per_year)。"""
    r = pd.Series(returns, dtype="float64").dropna()
    if len(r) < 2:
        return float("nan")
    ppy = _resolve_ppy(r.index, periods_per_year)
    if ppy is None:
        return float("nan")
    return float(r.std(ddof=1) * np.sqrt(ppy))


def sharpe_ratio(
    returns: pd.Series,
    periods_per_year: float | None = None,
    risk_free_rate: float = 0.0,
) -> float:
    """年化 Sharpe：均值超额收益 / 波动率 × sqrt(periods_per_year)。

    无风险利率按**年化**传入（默认 0，须在报告中披露）；期化时除以 periods_per_year。
    """
    r = pd.Series(returns, dtype="float64").dropna()
    if len(r) < 2:
        return float("nan")
    ppy = _resolve_ppy(r.index, periods_per_year)
    if ppy is None:
        return float("nan")
    excess = r - float(risk_free_rate) / ppy
    sd = float(excess.std(ddof=1))
    if not np.isfinite(sd) or sd == 0:
        return float("nan")
    return float(excess.mean() / sd * np.sqrt(ppy))


def turnover(weights: pd.DataFrame) -> float:
    """换手 = Σ |Δw|（对每一行再平衡求 |Δw| 之和，再跨行累加）。

    首行相对「无持仓」的 0→w 换手按 diff 的 NaN 视为 0 —— 初始建仓在语义上
    是「第一次进入」，若要计入请自行在首行前补一行全 0。
    """
    frame = pd.DataFrame(weights, dtype="float64").fillna(0.0)
    return float(frame.diff().abs().sum(axis=1).sum())


def rebalance_count(weights: pd.DataFrame) -> int:
    """换手次数 = 有任一标的权重发生变化的再平衡日数。"""
    frame = pd.DataFrame(weights, dtype="float64").fillna(0.0)
    changed = frame.diff().abs().sum(axis=1) > 1e-12
    return int(changed.fillna(False).sum())


def performance_metrics(
    equity: pd.Series,
    *,
    periods_per_year: float | None = None,
    risk_free_rate: float = 0.0,
    weights: pd.DataFrame | None = None,
) -> dict:
    """聚合收益指标。**必含** `annualization_periods` 与 `annualization_note`。

    参数：
        periods_per_year —— 显式覆盖（测试确定性）；缺省由 `derive_periods_per_year` 推导
        risk_free_rate   —— 年化无风险利率（默认 0，写入 annualization_note 披露）
        weights          —— 目标权重面板；给定时追加 turnover / n_rebalances
    """
    values = pd.Series(equity, dtype="float64").dropna()
    if not len(values):
        return {
            "initial_equity": float("nan"), "final_equity": float("nan"),
            "total_return": float("nan"), "annualized_return": float("nan"),
            "annualized_volatility": float("nan"), "sharpe_ratio": float("nan"),
            "max_drawdown": float("nan"), "risk_free_rate": float(risk_free_rate),
            "annualization_periods": None, "annualization_note": _annualization_note(None),
        }

    ppy = _resolve_ppy(values.index, periods_per_year)
    returns = values.pct_change()
    result = {
        "initial_equity": float(values.iloc[0]),
        "final_equity": float(values.iloc[-1]),
        "total_return": total_return(values),
        "annualized_return": annualized_return(values, ppy),
        "annualized_volatility": annualized_volatility(returns, ppy),
        "sharpe_ratio": sharpe_ratio(returns, ppy, risk_free_rate),
        "max_drawdown": max_drawdown(values),
        "risk_free_rate": float(risk_free_rate),
        "annualization_periods": ppy,
        "annualization_note": _annualization_note(ppy),
    }
    if weights is not None:
        result["turnover"] = turnover(weights)
        result["n_rebalances"] = rebalance_count(weights)
    return result


def _resolve_ppy(
    index: pd.DatetimeIndex,
    periods_per_year: float | None,
) -> float | None:
    """解析年化期数：显式值优先；否则由索引密度推导；仍不可得则 None。"""
    if periods_per_year is not None:
        return float(periods_per_year)
    derived = derive_periods_per_year(index)
    return derived if np.isfinite(derived) else None


def _annualization_note(ppy: float | None) -> str:
    if ppy is None or not np.isfinite(ppy):
        return ("年化口径：样本不足，无法推导每年期数（periods_per_year），"
                "相关年化指标为 NaN，**未**用 252/365 硬编码兜底。")
    return (
        f"年化口径：由数据自身会话密度推导 periods_per_year={ppy:.4f}"
        f"（会话数 ÷ 跨度自然年数，跨度按 365.25 天/年折算），全平台唯一入口，"
        f"未写死 365/252；Sharpe 无风险利率默认 0（已披露）。"
    )
