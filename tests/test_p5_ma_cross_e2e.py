r"""P5.6 能力域内 E2E（sample-ma-cross.md，时序金叉/死叉）—— E2E-A。

LOCAL_DEPLOYMENT_PLAN.md §P5.6 的「能力域内 E2E」= 证明**契约链**能表达并执行一个
时序标量信号策略（`papers/sample-ma-cross.md` 的 20/60 均线金叉建仓、死叉清仓）。

**为什么叫 E2E-A、为什么手写 spec**：见 `docs/deploy/HANDOFF.md` §7.6「重估」。
「规格为真相」下，契约链是 `spec → spec2weights → 引擎`；而「论文 → spec」的
LLM 解析层（`map_to_contract` 对 exit / cross_above 的映射缺口）是**另一个子决策**
（E2E-B，待定）。本文件先证明**确定性的那一段**，零外部依赖、可重复。

与 P5.5 的关系：P5.5 用 `gt(fast,slow)` / `lt(fast,slow)`（**连续态**）验「建仓/清仓」；
本文件用 `cross_above` / `cross_below`（**事件**，论文 §2 的原文「当根快线>慢线、且
上一根快线≤慢线」），并验证**整条权重时间线**与手算金叉/死叉逐格一致。

口径注意（§7.6 第 5 步）：
    `emit_weights` 的入场资格用 `momentum_window`（默认 63）做预热，而本策略的
    预热是 `lookback=60`（论文 §5：慢线窗口 60）。两者不一致会让金叉在 60–62 日
    出现时被 63 日预热门多卡 3 个交易日。故此处统一 `momentum_window=lookback=60`。

口径注意（可成交掩码）：标的 1 无「监管停牌」，但合成日历里**交易所休市日**
（春节/国庆等，共 167 天）close 为 NaN、traded 为 False。`emit_weights` 在休市
调仓日不表达目标（权重 0，F.4.5）；`position_state` 则把休市视为「维持前值」。
故「权重时间线」≠「裸持仓状态」，须按 `状态 ∧ 可成交 ∧ 动量预热` 对拍（见
`TestWeightsTimelineMatchesPaper`）。

运行：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -m unittest tests.test_p5_ma_cross_e2e -v
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from quantlab.contract.emit import MarketData, emit_signals, spec2weights
from quantlab.contract.lint import lint_spec
from quantlab.contract.types import (
    F8_SCENARIOS,
    Expr,
    SizingSpec,
    StrategySpec,
)
from quantlab.engines.base import (
    DataBundle,
    get_runner,
    load_bundle_from_fixture,
    register_builtin,
)

TOLERANCE = 1e-4
INITIAL_CASH = 1_000_000.0
COSTS = F8_SCENARIOS[0]
SYMBOL = 1                      # SYN-CN-A：单标的、无停牌、无下载失败（夹具 spec.py）


# --------------------------------------------------------------------------- #
# 规格与手算（独立于被测实现）
# --------------------------------------------------------------------------- #
def sma(field: str, n: int) -> Expr:
    """`sma(shift(field, 1), n)` —— 已解除未来函数（§P3.2 G4）。"""
    return Expr("sma", (Expr("shift", (Expr("field", (field,)), 1)), n))


def ma_cross_spec(symbol: int = SYMBOL) -> StrategySpec:
    """sample-ma-cross.md 的忠实手写规格：金叉建仓 / 死叉清仓、周频、单标的。"""
    return StrategySpec(
        name="sample-ma-cross", universe=(symbol,),
        entry=Expr("cross_above", (sma("close", 20), sma("close", 60))),
        exit=Expr("cross_below", (sma("close", 20), sma("close", 60))),
        sizing=SizingSpec(top_n=1, rebalance="W-MON"),
        costs=COSTS, lookback=60,
    )


def hand_crosses(closes: pd.Series) -> tuple[pd.Series, pd.Series]:
    """**独立**用 pandas 复算金叉/死叉（论文 §2 定义 + shift(1) 因果口径）。

    不复用 `evaluate`，而是直接按论文原文写一遍：金叉 = 当根快线>慢线 且 上一根快线≤慢线。
    这是「被测实现」之外的独立真值，用来钉住 `emit.py::evaluate` 的 `cross_above/below`。
    """
    fast = closes.shift(1).rolling(20).mean()
    slow = closes.shift(1).rolling(60).mean()
    golden = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    death = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    return golden.fillna(False), death.fillna(False)


def _state_from_crosses(golden: pd.Series, death: pd.Series) -> pd.Series:
    """事件 → 状态（金叉后持 1，死叉后持 0）。独立重写，不复用 `position_state`。"""
    g, d = golden.to_numpy(), death.to_numpy()
    state = np.zeros(len(g), dtype="float64")
    cur = 0.0
    for i in range(len(g)):
        if g[i]:
            cur = 1.0
        elif d[i]:
            cur = 0.0
        state[i] = cur
    return pd.Series(state, index=golden.index)


def event_panel(weights: pd.DataFrame) -> pd.DataFrame:
    """压缩成**变化点**（避免 backtrader L1：连续重复目标 → 补仓 Margin，见 P5.5）。"""
    changed = weights.fillna(0.0).ne(weights.fillna(0.0).shift()).any(axis=1)
    return weights[changed.fillna(True)]


def build_entry_clear_pair(weights: pd.DataFrame) -> pd.DataFrame:
    """取「一次建仓 + 其后一次清仓」（backtrader 支持形态，见 P5.5 docstring）。"""
    ev = event_panel(weights)
    entered = ev.index[ev.sum(axis=1) > 0]
    if not len(entered):
        raise AssertionError("面板里没有建仓事件")
    start = entered[0]
    after = ev.loc[ev.index > start]
    cleared = after.index[after.sum(axis=1) == 0]
    if not len(cleared):
        raise AssertionError("建仓之后没有清仓事件")
    return ev.loc[start:cleared[0]]


def max_rel_dev(a: pd.Series, b: pd.Series) -> float:
    common = a.dropna().index.intersection(b.dropna().index)
    if not len(common):
        raise AssertionError("两条曲线没有公共日期")
    return float(((a[common] / b[common] - 1.0).abs()).max())


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        register_builtin()
        cls.full = load_bundle_from_fixture()

    def data(self) -> DataBundle:
        return self.full.subset([SYMBOL])

    @staticmethod
    def weights(spec: StrategySpec, data: DataBundle) -> pd.DataFrame:
        return spec2weights(spec, MarketData(prices=data.closes, traded=data.traded),
                            momentum_window=spec.lookback)


# --------------------------------------------------------------------------- #
# 1. 闸门：手写规格是契约合法的
# --------------------------------------------------------------------------- #
class TestSpecIsContractLegal(_Base):
    def test_spec_passes_the_gate(self) -> None:
        """手写规格须过 P3.2 闸门（G4 无未来函数、G5 回看够、G9 有 entry 等）。"""
        report = lint_spec(ma_cross_spec())
        self.assertTrue(report.passed, f"闸门未通过: {report.errors}")


# --------------------------------------------------------------------------- #
# 2. 信号事件：金叉 +1 / 死叉 −1 恰好落在手算穿越日
# --------------------------------------------------------------------------- #
class TestSignalEventsMatchPaper(_Base):
    def test_emit_signals_fires_exactly_on_golden_and_death_crosses(self) -> None:
        data = self.data()
        spec = ma_cross_spec()
        signals = emit_signals(spec, MarketData(prices=data.closes, traded=data.traded))

        golden, death = hand_crosses(data.closes[SYMBOL])
        warmed = np.arange(len(data.closes)) >= spec.lookback

        expected = pd.Series(0.0, index=data.closes.index)
        expected = expected.mask(golden & warmed, 1.0)
        expected = expected.mask(death & warmed, -1.0)
        expected = expected.mask(~warmed, np.nan)
        expected = expected.where(data.traded[SYMBOL], np.nan)   # 停牌处不得有信号

        pd.testing.assert_series_equal(signals[SYMBOL], expected, check_names=False)


# --------------------------------------------------------------------------- #
# 3. 权重时间线：逐格等于手算持仓状态投影到周频调仓日
# --------------------------------------------------------------------------- #
class TestWeightsTimelineMatchesPaper(_Base):
    def test_weights_track_hold_state(self) -> None:
        data = self.data()
        spec = ma_cross_spec()
        w = self.weights(spec, data)

        golden, death = hand_crosses(data.closes[SYMBOL])
        state = _state_from_crosses(golden, death)
        closes = data.closes[SYMBOL]
        tradable = data.traded[SYMBOL].astype(bool)
        # 目标权重 = 持仓状态 ∧ 当日可成交 ∧ 过了动量预热（逐格精确对拍）。
        # · tradable：合成日历里 XSHG 休市日（如春节/国庆）为 NaN/False ——
        #   标的 1 无「监管停牌」，但**有交易所休市**（167 天），休市日不得表达目标（F.4.5）。
        # · warmed：emit_weights 的动量窗口要求 closes.shift(lookback) 非空，
        #   只在长假后 ~60 个交易日附近的少数调仓日有影响（纯预热见下一条测试）。
        warmed = closes.notna() & closes.shift(spec.lookback).notna()
        expected = (state.astype(bool) & tradable & warmed).astype(float).reindex(w.index)

        pd.testing.assert_series_equal(w[SYMBOL], expected, check_names=False)

    def test_warmup_has_no_positions(self) -> None:
        data = self.data()
        spec = ma_cross_spec()
        w = self.weights(spec, data)

        golden, _ = hand_crosses(data.closes[SYMBOL])
        first_cross = golden[golden].index[0]
        before = w.index[w.index < first_cross]
        self.assertGreater(len(before), 0, "首次金叉前应存在调仓日")
        self.assertTrue((w.loc[before, SYMBOL] == 0.0).all(),
                        "预热期内（首次金叉前）不得持仓")


# --------------------------------------------------------------------------- #
# 4. 引擎：整条交叉循环可被 reference 消费，backtrader 在受支持形态上对拍一致
# --------------------------------------------------------------------------- #
class TestCrossOverEngineParity(_Base):
    def test_reference_consumes_full_cross_cycle(self) -> None:
        """完整金叉/死叉循环喂 reference（oracle）→ 无错、有成交、净值有限。"""
        data = self.data()
        w = event_panel(self.weights(ma_cross_spec(), data))

        golden, death = hand_crosses(data.closes[SYMBOL])
        self.assertGreater(int(golden.sum()), 0, "夹具应至少出现一次金叉")
        self.assertGreater(int(death.sum()), 0, "夹具应至少出现一次死叉")

        result = get_runner("reference").run(w, data, COSTS)
        self.assertTrue(np.isfinite(result.equity.to_numpy()).all())
        self.assertGreater(result.stats["n_trades"], 0)

    def test_backtrader_matches_reference_on_cross_entry_clear(self) -> None:
        """同一份交叉权重（压缩为一次建仓+清仓）→ backtrader 与 reference 容差内一致。"""
        data = self.data()
        pair = build_entry_clear_pair(self.weights(ma_cross_spec(), data))

        ref = get_runner("reference").run(pair, data, COSTS).equity
        got = get_runner("backtrader").run(pair, data, COSTS).equity
        dev = max_rel_dev(got, ref)
        self.assertLessEqual(dev, TOLERANCE, f"backtrader 偏差 {dev:.3e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
