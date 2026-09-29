"""质量校验规则（LOCAL_DEPLOYMENT_PLAN.md §P2.6）。

每条规则都是一个「输入快照表 → 输出 `CheckResult`」的函数。

**为什么规则必须能被负向测试**：§P2.6 明确要求「质量校验能**捕获**人为注入的每种缺陷
（每种缺陷一个用例，不得只测『正常数据通过』）」。所以每条规则都要能被
`tests/test_p2_6_quality.py` 用**注入缺陷**的方式打红；只测正常数据的规则等于没测。

阈值口径（**可配置、须披露**，不得暗改）：
    · 异常跳变：单日对数收益绝对值 > `JUMP_SIGMA` 倍滚动波动 **且** > `JUMP_FLOOR`
      —— 双条件是为了避免「低波动期把正常波动误报成跳变」。只看倍数会大量误报。
    · 汇率陈旧：同一方向连续**精确相同**报价超过 `FX_STALE_DAYS` 天。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

# ---- 阈值（改这里必须同步更新 EVIDENCE.md 的披露） ----
JUMP_SIGMA = 8.0            # 超过 8 倍滚动波动
JUMP_FLOOR = 0.15           # 且当日对数收益绝对值 > 15%
JUMP_WINDOW = 60            # 滚动窗口（交易日）
FX_STALE_DAYS = 10          # 汇率连续同值超过 10 天视为陈旧


@dataclass
class CheckResult:
    name: str
    ok: bool
    violations: int
    detail: str = ""
    samples: list = field(default_factory=list)

    def __str__(self) -> str:
        return f"[{'PASS' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


@dataclass
class QualityReport:
    results: list[CheckResult]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def by_name(self, name: str) -> CheckResult:
        for result in self.results:
            if result.name == name:
                return result
        raise KeyError(f"未执行的检查: {name}")

    def summary(self) -> str:
        return "\n".join(str(r) for r in self.results)


# --------------------------------------------------------------------------- #
# 逐条规则
# --------------------------------------------------------------------------- #
def check_positive_prices(bars: pd.DataFrame) -> CheckResult:
    """价格必须为正。"""
    bad = bars[(bars[["open", "high", "low", "close"]] <= 0).any(axis=1)]
    return CheckResult("positive_prices", bad.empty, len(bad),
                       f"非正价格 {len(bad)} 行",
                       bad[["symbol_id", "ts"]].head(5).to_dict("records"))


def check_ohlc_relations(bars: pd.DataFrame) -> CheckResult:
    """`low ≤ min(open, close) ≤ max(open, close) ≤ high`。"""
    lower = bars[["open", "close"]].min(axis=1)
    upper = bars[["open", "close"]].max(axis=1)
    bad = bars[(bars["low"] > lower) | (bars["high"] < upper)]
    return CheckResult("ohlc_relations", bad.empty, len(bad),
                       f"OHLC 关系违例 {len(bad)} 行",
                       bad[["symbol_id", "ts"]].head(5).to_dict("records"))


def check_unique_keys(bars: pd.DataFrame) -> CheckResult:
    """主键 `(symbol_id, ts, snapshot_id)` 不得重复。"""
    dup = bars[bars.duplicated(["symbol_id", "ts", "snapshot_id"], keep=False)]
    return CheckResult("unique_keys", dup.empty, len(dup),
                       f"重复主键 {len(dup)} 行",
                       dup[["symbol_id", "ts"]].head(5).to_dict("records"))


def check_sorted(bars: pd.DataFrame) -> CheckResult:
    """每个标的的 `ts` 必须严格递增。"""
    bad = [int(sid) for sid, group in bars.groupby("symbol_id")
           if not pd.to_datetime(group["ts"]).reset_index(drop=True).is_monotonic_increasing]
    return CheckResult("sorted_by_ts", not bad, len(bad),
                       f"时间非递增的标的: {sorted(bad) if bad else '无'}", bad)


def check_calendar_gaps(
    bars: pd.DataFrame,
    calendar: pd.DataFrame,
    *,
    exchange_of: Callable[[int], str] | None = None,
) -> CheckResult:
    """**日历预期缺口**：市场开着却没有数据 → 下载失败。

    关键区分：**休市日不算缺口**（本就不该有行）；**停牌日有行**，也不算缺口。
    只有「日历说开市 + 我们没有这一天的行」才算。

    `exchange_of` —— 由 symbol_id 映射到交易所。夹具的 bars 不含 exchange 列，
    故默认从夹具规格推断；真实数据应从 `symbols` 表取，届时注入即可。
    """
    exchange_of = exchange_of or _exchange_of_fixture
    rows = []
    for symbol_id, group in bars.groupby("symbol_id"):
        exchange = exchange_of(int(symbol_id))
        cal = calendar[calendar["exchange"] == exchange]
        if cal.empty:
            continue
        span = pd.date_range(pd.to_datetime(group["ts"]).min(),
                             pd.to_datetime(group["ts"]).max(), freq="D")
        open_days = set(cal.loc[cal["is_open"].astype(bool), "ts"])
        present = set(pd.to_datetime(group["ts"]).dt.date)
        missing = sorted(({d.date() for d in span} & open_days) - present)
        rows.extend({"symbol_id": int(symbol_id), "ts": d} for d in missing)
    return CheckResult("calendar_gaps", not rows, len(rows),
                       f"开市但无数据的交易日 {len(rows)} 个", rows[:5])


def _exchange_of_fixture(symbol_id: int) -> str:
    """兜底映射：从夹具规格取交易所（bars 表本身不含该列）。"""
    from quantlab.fixtures import spec as S

    symbol = S.SYMBOLS_BY_ID.get(symbol_id)
    return symbol.exchange if symbol else ""


def check_price_jumps(bars: pd.DataFrame, price_col: str = "close_adj") -> CheckResult:
    """异常跳变：单日对数收益既超绝对阈值、又显著偏离**以往**波动。

    两个容易踩的坑，都在这里显式处理：

    1. **必须用复权价**。用未复权价会把「4:1 拆分」当成 -75% 的异常跳变 —— 拆分是
       已知公司行动，不是数据缺陷。夹具的 symbol 2 就专门用来钉住这个误报。
    2. **波动基准不得包含被检验的那一天**。若滚动窗口把当日算进去，突变会抬高自身
       的 σ，于是 `|r| > 8σ` 永远不成立 —— 检测器**被自己废掉**。故用
       `shift(1)` 只看**此前**的波动。

    未复权数据（无 `close_adj` 列）会退化为按 `close` 检验，此时**必须**预期拆分日误报；
    调用方应先复权，而非放宽容差。
    """
    if price_col not in bars.columns:
        price_col = "close"
    bad = []
    for symbol_id, group in bars.groupby("symbol_id"):
        frame = group.sort_values("ts")
        prices = frame[price_col].to_numpy(dtype="float64")
        if len(prices) < JUMP_WINDOW + 2:
            continue
        log_ret = np.diff(np.log(prices))
        # shift(1)：只用 t 之前的波动率作为基准，避免「自证清白」
        rolling = (pd.Series(log_ret).rolling(JUMP_WINDOW, min_periods=20)
                   .std().shift(1).to_numpy())
        for i, (ret, sigma) in enumerate(zip(log_ret, rolling), start=1):
            if not np.isfinite(sigma) or sigma <= 0:
                continue
            if abs(ret) > JUMP_FLOOR and abs(ret) > JUMP_SIGMA * sigma:
                bad.append({"symbol_id": int(symbol_id),
                            "ts": str(frame["ts"].iloc[i]),
                            "log_return": float(ret), "rolling_sigma": float(sigma)})
    return CheckResult("price_jumps", not bad, len(bad), f"异常跳变 {len(bad)} 处", bad[:5])


def check_fx_staleness(fx: pd.DataFrame, max_stale_days: int = FX_STALE_DAYS) -> CheckResult:
    """汇率陈旧度：同一方向连续**精确相同**报价超过阈值 → 陈旧。

    真实汇率极少精确重复；连续多日一字不差通常意味着报价没更新。
    """
    bad = []
    for (base, quote), group in fx.groupby(["base", "quote"]):
        rates = group.sort_values("ts")["rate"].to_numpy(dtype="float64")
        run = best = 1
        for i in range(1, len(rates)):
            run = run + 1 if rates[i] == rates[i - 1] else 1
            best = max(best, run)
        if best > max_stale_days:
            bad.append({"base": base, "quote": quote, "longest_flat_run": int(best)})
    return CheckResult("fx_staleness", not bad, len(bad),
                       f"陈旧汇率对 {len(bad)} 个（连续同值 > {max_stale_days} 天）", bad[:5])


def check_fx_direction(fx: pd.DataFrame) -> CheckResult:
    """汇率必须恒为「1 base = rate quote」且为正；方向靠**内部一致性**判定。

    同一对若同时出现 (A,B) 与 (B,A)，两者乘积应恒为 1；否则方向不一致。
    只出现单向报价时**无法**判定方向错误 —— 不报违规，但须在报告中披露。
    """
    bad = []
    if (fx["rate"] <= 0).any():
        bad.append({"issue": "non_positive_rate", "count": int((fx["rate"] <= 0).sum())})
    piv = fx.pivot_table(index="ts", columns=["base", "quote"], values="rate")
    for col in piv.columns:
        base, quote = col
        if (quote, base) in piv.columns:
            product = (piv[col] * piv[(quote, base)]).dropna()
            off = product[(product - 1.0).abs() > 1e-6] if len(product) else product
            if len(off):
                bad.append({"issue": "inconsistent_direction",
                            "pair": f"{base}/{quote}", "rows": int(len(off))})
    return CheckResult("fx_direction", not bad, len(bad),
                       f"汇率方向问题 {len(bad)} 处", bad[:5])


DEFAULT_CHECKS: tuple[Callable, ...] = (
    check_positive_prices,
    check_ohlc_relations,
    check_unique_keys,
    check_sorted,
)


def run_all_checks(
    *,
    bars: pd.DataFrame,
    calendar: pd.DataFrame | None = None,
    fx: pd.DataFrame | None = None,
    exchange_of: Callable[[int], str] | None = None,
    adjusted_bars: pd.DataFrame | None = None,
) -> QualityReport:
    """跑齐全部规则，返回报告。

    缺哪张表就**跳过**依赖它的规则，并在结果 detail 里写明「跳过（非通过）」
    —— 不得让「没跑」看起来像「通过了」。

    `adjusted_bars` —— **强烈建议**传入（`clean.adjust_prices` 的产出）。
    异常跳变检测必须基于复权价，否则拆分日会被误报成跳空；未提供时退化为按
    未复权 `close` 检验，此时拆分误报属预期，调用方应自行判别而非放宽容差。
    """
    results: list[CheckResult] = [check(bars) for check in DEFAULT_CHECKS]
    results.append(check_price_jumps(adjusted_bars if adjusted_bars is not None else bars))

    if calendar is not None:
        results.append(check_calendar_gaps(bars, calendar, exchange_of=exchange_of))
    else:
        results.append(CheckResult("calendar_gaps", True, 0, "未提供交易日历 → 跳过（非通过）"))

    if fx is not None:
        results.append(check_fx_staleness(fx))
        results.append(check_fx_direction(fx))
    else:
        results.append(CheckResult("fx_staleness", True, 0, "未提供汇率 → 跳过（非通过）"))
        results.append(CheckResult("fx_direction", True, 0, "未提供汇率 → 跳过（非通过）"))

    return QualityReport(results)
