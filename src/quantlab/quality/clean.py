"""Silver / Gold 清洗（LOCAL_DEPLOYMENT_PLAN.md §P2.6）。

三件事：**复权因子**、**日历对齐**、**汇率方向统一**，外加一个回测输入视图。

复权口径（写死，别处不得另立）：
    F[t] = ∏_{s: ex_s ≤ t} r_s              拆分累积因子（无拆分则恒为 1）
    前复权（以**最新**股本为基准，最新价不变）: m[t] = F[T] / F[t]
    后复权（以**最初**股本为基准，最初价不变）: m[t] = F[t] / F[0]

总收益指数（F.6 的恒等式，**独立于夹具内部量**重算）：
    TR[0] = 1,  TR[t] = TR[t-1] · (close_raw[t]·r_t + d_t) / close_raw[t-1]
这里的 `close_raw` 是**未复权**收盘，`r_t`/`d_t` 来自 `corporate_actions`。
P2.6 用它与夹具的解析答案对拍（V3）。

汇率方向（F.6）：内部统一为「**1 原币 = N 基准货币**」。
供应商给反方向时**取倒数并在输出里留记录**，绝不静默改口径。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

# 复权口径
ADJUST_BACKWARD = "backward"    # 前复权：以**最新**为基准（最新价不变）
ADJUST_FORWARD = "forward"      # 后复权：以**最初**为基准（最初价不变）


class SessionStatus(str, Enum):
    """单个 (标的, 自然日) 的状态。

    **三态必须互不相同**（§P2.6 V3）：
        CLOSED  —— **休市**：市场没开，本就不该有行
        HALTED  —— **停牌**：市场开着、行在，但不可成交
        MISSING —— **下载失败**：市场开着，但我们**没有**这一天的数据
    """

    TRADED = "traded"       # 正常成交
    HALTED = "halted"       # 停牌（有行、不可成交）
    CLOSED = "closed"       # 休市（无行，且本应无行）
    MISSING = "missing"     # 下载失败（无行，但**本应有行**）

    @classmethod
    def three_states(cls) -> tuple[str, ...]:
        """§P2.6 明确要求可区分的那三种状态。"""
        return (cls.CLOSED.value, cls.HALTED.value, cls.MISSING.value)


class FxDirectionError(ValueError):
    """汇率方向无法在不产生歧义的前提下归一到「1 原币 = N 基准货币」。"""


@dataclass
class FxDirectionRecord:
    """一次汇率方向调整的记录（F.6 要求「取倒数并留记录」）。"""

    base: str
    quote: str
    action: str          # 'kept' | 'inverted'
    source_columns: str
    note: str


# --------------------------------------------------------------------------- #
# 复权因子
# --------------------------------------------------------------------------- #
def split_factors(dates, splits: pd.DataFrame) -> np.ndarray:
    """F[t] = ∏_{ex_date ≤ t} ratio（无拆分则恒为 1）。"""
    index = pd.DatetimeIndex(pd.to_datetime(list(dates)))
    factors = np.ones(len(index), dtype="float64")
    if splits is None or len(splits) == 0:
        return factors
    running = 1.0
    for row in splits.sort_values("ex_date").itertuples():
        ex = pd.Timestamp(row.ex_date)
        running *= float(row.ratio)
        factors[index >= ex] = running
    return factors


def adjust_prices(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    *,
    method: str = ADJUST_BACKWARD,
) -> pd.DataFrame:
    """按拆分调整价格，返回新增 `split_factor`/`adjust_multiplier`/`*_adj` 的副本。

    ⚠️ 只处理**拆分**。分红**不进**价格调整 —— 分红进的是**总收益**（见
    `total_return_index`）。把分红也塞进价格，就是 F.6 明令禁止的「重复计收益」。
    """
    if method not in (ADJUST_BACKWARD, ADJUST_FORWARD):
        raise ValueError(f"未知复权口径: {method!r}；可选 {ADJUST_BACKWARD}/{ADJUST_FORWARD}")

    out = bars.sort_values("ts").reset_index(drop=True).copy()
    splits = actions[actions["kind"] == "split"] if actions is not None and len(actions) else None
    factors = split_factors(out["ts"], splits)

    # 乘法因子的方向**极易写反**，这里用具体数字锚定（拆分 4:1，c_ex-1=400, c_ex=100）：
    #   前复权（最新不变）: m = F[t]/F[-1] → 除权前 1/4、除权后 1  → 400·¼=100 == 100 ✓
    #   后复权（最初不变）: m = F[t]/F[0]  → 除权前 1、除权后 4    → 400·1 =400 == 400 ✓
    # 若错写成 F[-1]/F[t]，除权前会变成 ×4，得到 1600 vs 100 —— 16 倍假跳空。
    if len(factors):
        multiplier = (factors / factors[-1]) if method == ADJUST_BACKWARD \
            else (factors / factors[0])
    else:
        multiplier = factors

    out["split_factor"] = factors
    out["adjust_multiplier"] = multiplier
    for col in ("open", "high", "low", "close"):
        out[f"{col}_adj"] = out[col].to_numpy() * multiplier
    return out


def forward_adjust_close(
    bars: pd.DataFrame,
    actions: pd.DataFrame | None = None,
    fund_adj: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """后复权收盘（silver 层 `close_adj` 的纯函数），D0 = 该标的最早交易日，最初价不变。

    两种口径（`method` 列记录，可溯源）：
        · `fund_adj_forward`：`fund_adj` 存在（真实 tushare）时，用供应商**累计复权因子**
          （含分红）归一：`close_adj[t] = close[t] × (adj_factor[t] / adj_factor[t0])`，
          `t0` = 该标的最早 ts。这是「总收益口径」的后复权（含分红），非纯拆分。
        · `split_forward`：无 `fund_adj` 时退化为 `adjust_prices(method=forward)` 的
          **纯拆分**后复权（分红不进价格 —— 分红进的是 `total_return_index`）。
    """
    if fund_adj is not None and len(fund_adj):
        return _fund_adj_forward(bars, fund_adj)

    parts: list[pd.DataFrame] = []
    for symbol_id, sub in bars.groupby("symbol_id", sort=True):
        own = (actions[actions["symbol_id"] == symbol_id]
               if actions is not None and len(actions) else None)
        adj = adjust_prices(sub, own, method=ADJUST_FORWARD)
        parts.append(pd.DataFrame({
            "symbol_id": [symbol_id] * len(adj),
            "ts": adj["ts"].to_list(),
            "close_adj": adj["close_adj"].to_numpy(dtype="float64"),
            "adj_factor": adj["adjust_multiplier"].to_numpy(dtype="float64"),
        }))
    if not parts:
        return pd.DataFrame(columns=["symbol_id", "ts", "close_adj", "adj_factor", "method"])
    out = pd.concat(parts, ignore_index=True)
    out["method"] = "split_forward"
    return out.sort_values(["symbol_id", "ts"]).reset_index(drop=True)


def _fund_adj_forward(bars: pd.DataFrame, fund_adj: pd.DataFrame) -> pd.DataFrame:
    """用供应商累计复权因子做后复权（含分红，总收益口径），缺因子回退 1（不调整）。"""
    fa = fund_adj[["symbol_id", "ts", "adj_factor"]].copy()
    fa["adj_factor"] = pd.to_numeric(fa["adj_factor"], errors="coerce").astype("float64")
    merged = bars[["symbol_id", "ts", "close"]].merge(
        fa, on=["symbol_id", "ts"], how="left")
    merged["adj_factor"] = merged["adj_factor"].fillna(1.0)

    rows: list[pd.DataFrame] = []
    for symbol_id, sub in merged.sort_values(["symbol_id", "ts"]).groupby(
            "symbol_id", sort=True):
        anchor = float(sub["adj_factor"].iloc[0])
        norm = (np.ones(len(sub), dtype="float64") if anchor == 0.0
                else sub["adj_factor"].to_numpy(dtype="float64") / anchor)
        rows.append(pd.DataFrame({
            "symbol_id": [symbol_id] * len(sub),
            "ts": sub["ts"].to_list(),
            "close_adj": sub["close"].to_numpy(dtype="float64") * norm,
            "adj_factor": norm,
        }))
    if not rows:
        return pd.DataFrame(columns=["symbol_id", "ts", "close_adj", "adj_factor", "method"])
    out = pd.concat(rows, ignore_index=True)
    out["method"] = "fund_adj_forward"
    return out.sort_values(["symbol_id", "ts"]).reset_index(drop=True)


def total_return_index(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    *,
    price_col: str = "close",
    base: float = 1.0,
) -> pd.Series:
    """总收益指数（F.6 口径），由**未复权价格 + 公司行动**独立重算。

    `TR[t] = TR[t-1] · (close_raw[t]·r_t + d_t) / close_raw[t-1]`，`TR[0] = base`。
    停牌日 `close` 沿用、`r=1`/`d=0` → 该日收益恰为 1，**停牌不产生收益**。
    """
    frame = bars.sort_values("ts").reset_index(drop=True)
    ratio = {row.ex_date: float(row.ratio) for row in actions.itertuples() if row.kind == "split"}
    cash = {row.ex_date: float(row.cash) for row in actions.itertuples() if row.kind == "dividend"}

    prices = frame[price_col].to_numpy(dtype="float64")
    tr = np.empty(len(frame), dtype="float64")
    tr[0] = base
    for i in range(1, len(frame)):
        r = ratio.get(frame["ts"].iloc[i], 1.0)
        d = cash.get(frame["ts"].iloc[i], 0.0)
        tr[i] = tr[i - 1] * (prices[i] * r + d) / prices[i - 1]
    return pd.Series(tr, index=frame.index, name="total_return_nav")


# --------------------------------------------------------------------------- #
# 日历对齐与三态
# --------------------------------------------------------------------------- #
def build_session_status(
    bars: pd.DataFrame,
    calendar: pd.DataFrame,
    *,
    symbol_id: int,
    exchange: str,
    start,
    end,
) -> pd.DataFrame:
    """逐**自然日**给出该标的的状态：traded / halted / closed / missing。

    判定顺序（关键在于最后两支的区别）：
        · 有行且 traded=True  → TRADED
        · 有行且 traded=False → HALTED（停牌）
        · 无行且日历 is_open=False → CLOSED（休市）
        · 无行且日历 is_open=True  → MISSING（**下载失败**：市场开着却没数据）
    """
    span = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D")
    cal = calendar[calendar["exchange"] == exchange].set_index("ts")
    subset = bars[bars["symbol_id"] == symbol_id]
    traded = set(subset.loc[subset["traded"].astype(bool), "ts"])
    halted = set(subset.loc[~subset["traded"].astype(bool), "ts"])

    rows = []
    for day in span:
        key = day.date()
        if key in traded:
            status = SessionStatus.TRADED
        elif key in halted:
            status = SessionStatus.HALTED
        elif bool(cal["is_open"].get(key, False)):
            status = SessionStatus.MISSING          # 市场开着，却没数据
        else:
            status = SessionStatus.CLOSED           # 市场没开
        rows.append({"symbol_id": symbol_id, "ts": key, "status": status.value})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# 汇率方向
# --------------------------------------------------------------------------- #
def normalize_fx_direction(
    fx: pd.DataFrame,
    *,
    base_currency: str = "CNY",
) -> tuple[pd.DataFrame, list[FxDirectionRecord]]:
    """把汇率统一成「**1 base = N quote**」，并返回方向调整记录。

    方向对错无法由单行判断，只能靠**内部不一致**：若同时存在 (A,B) 与 (B,A)，
    取样本更多的那一侧为正向，另一侧行取倒数。只存在一对时**原样保留**并标注 `kept`
    —— 我们无权凭空断定供应商给反了。
    """
    out = fx.copy()
    records: list[FxDirectionRecord] = []
    pairs = {(b, q) for b, q in zip(out["base"], out["quote"])}
    seen: set[frozenset[str]] = set()

    for base, quote in sorted(pairs):
        key = frozenset({base, quote})
        if key in seen or len(key) < 2:
            continue
        seen.add(key)
        if (quote, base) in pairs:
            fwd_n = int(((out["base"] == base) & (out["quote"] == quote)).sum())
            rev_n = int(((out["base"] == quote) & (out["quote"] == base)).sum())
            # 方向判据（按优先级）：
            #   1) 本平台口径是「1 原币 = N **基准货币**」，故**报价币为基准货币**的
            #      那个方向才是正向。这一条优先于"样本多少"——它来自我们自己的口径，
            #      而不是启发式猜测。
            #   2) 否则取样本更多的一侧（更可能是主序列）。
            # 只按 sorted() 的字典序取，会随机选中 CNY/USD，与口径直接矛盾。
            if base == base_currency and quote != base_currency:
                prefer_reverse = True             # 当前 base 是基准币 → 应反过来
            elif quote == base_currency and base != base_currency:
                prefer_reverse = False            # 当前 quote 是基准币 → 正确
            else:
                prefer_reverse = rev_n > fwd_n
            if prefer_reverse:
                base, quote = quote, base
                fwd_n, rev_n = rev_n, fwd_n
            mask = (out["base"] == quote) & (out["quote"] == base)
            # 取倒数后，这些行会被**改名**成 (base, quote)，可能与原本就存在的
            # 同一序列**撞车**，导致同一 (base,quote,ts) 出现两行。那等于凭空复制
            # 了一份汇率 —— 必须显式拒绝，而不是让重复悄悄进入下游。
            existing_ts = set(out.loc[(out["base"] == base) & (out["quote"] == quote), "ts"])
            colliding = out.loc[mask & out["ts"].isin(existing_ts)]
            if len(colliding):
                raise FxDirectionError(
                    f"汇率方向归一化会产生重复序列：{base}/{quote} 在原表中已存在，"
                    f"而反向的 {quote}/{base} 取倒数后也归到同一序列，"
                    f"冲突 {len(colliding)} 行（示例 ts={sorted(colliding['ts'])[:3]}）。\n"
                    f"同一对**不应同时以两个方向出现**。处置：确认供应商实际给的是哪一个方向，"
                    f"只保留其一；**不得**让重复静默进入下游。"
                )
            out.loc[mask, "rate"] = 1.0 / out.loc[mask, "rate"].astype("float64")
            out.loc[mask, ["base", "quote"]] = [base, quote]
            records.append(FxDirectionRecord(
                base, quote, "inverted", f"{quote}/{base} → {base}/{quote}",
                f"同一对出现双向报价（正向 {fwd_n} 行 / 反向 {rev_n} 行），已对反向行取倒数",
            ))
        else:
            records.append(FxDirectionRecord(
                base, quote, "kept", f"{base}/{quote}",
                "仅出现单向报价，无依据判定方向错误，原样保留并在报告中披露",
            ))

    records.append(FxDirectionRecord(
        base_currency, base_currency, "kept", f"{base_currency}/{base_currency}",
        "基准货币对自身，恒为 1.0",
    ))
    return out, records


# --------------------------------------------------------------------------- #
# Gold：回测输入视图
# --------------------------------------------------------------------------- #
def gold_backtest_view(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    calendar: pd.DataFrame,
    *,
    symbol_id: int,
    exchange: str,
    snapshot_id: str,
) -> pd.DataFrame:
    """产出 gold 层回测输入视图。

    **只含单一快照**（按 `snapshot_id` 过滤并断言），并显式带上：
        · `close_adj`          —— 拆分复权收盘（估值/信号用）
        · `total_return_nav`   —— 总收益净值（F.6 口径，收益用）
        · `traded`             —— **显式交易掩码**（F.4：只能按真实可交易事件成交）
        · `available_utc`      —— 防未来函数的可用时间
        · `status`             —— 三态（traded / halted / closed / missing）
    """
    subset = bars[(bars["symbol_id"] == symbol_id) & (bars["snapshot_id"] == snapshot_id)]
    if len(subset) == 0:
        raise ValueError(f"gold 视图：symbol {symbol_id} 在快照 {snapshot_id} 中无数据")
    if subset["snapshot_id"].nunique() != 1:
        raise ValueError("gold 视图跨了多个快照 —— 违反快照纪律")

    own_actions = actions[actions["symbol_id"] == symbol_id]
    adjusted = adjust_prices(subset, own_actions)
    nav = total_return_index(subset, own_actions)
    status = build_session_status(
        bars, calendar, symbol_id=symbol_id, exchange=exchange,
        start=subset["ts"].min(), end=subset["ts"].max(),
    )

    view = adjusted[["ts", "close", "close_adj", "available_utc", "traded"]].copy()
    view["symbol_id"] = symbol_id
    view["total_return_nav"] = nav.to_numpy()
    view = view.merge(status[["ts", "status"]], on="ts", how="left")
    return view.sort_values("ts").reset_index(drop=True)
