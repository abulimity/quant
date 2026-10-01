r"""P5.5 自研发射器 `spec2weights` 验收（LOCAL_DEPLOYMENT_PLAN.md §P5.5）。

**关键补口**：x2strategy 只产 backtrader 代码；`spec2weights` 让**同一份规格**
也能喂给 bt / vectorbt —— 规格成为三引擎的**单一真相**。

验收（对齐人工决策 1 = A「缩小验证形态」）：

    V3a 同一份 spec2weights 产出，喂给 backtrader 与 bt，**各自与参考口径**在容差内一致
        （backtrader ↔ ref@open；bt ↔ ref@close）
    V3b 与 backtrader 路径行为一致 —— 用 P4.2 的 `BacktraderRunner` 承担
        （⚠️ 手册说「与 x2strategy 生成的代码对拍」，但 §P5.4 已定
         `spec2code` **无生成器**、只做 validator；故此处以我们的 backtrader 适配器为准）
    V3c 未来扰动测试：改未来数据，**此前权重逐格不变**
    V4  结构非法的规格**拒绝发射**（fail-closed）

⚠️ **为什么不是「bt vs backtrader 直接对拍」**：两者成交时点不同（bt 在**再平衡日收盘**、
backtrader 在 **T+1 开盘**），是 P4.5 已分类的 **D1 引擎设计差异**。直接对拍会看到
~7e-3 的差 —— 那是**成交时点**，不是规格分歧。故用「各自 vs 对应口径的参考」表达
「同一规格 → 各引擎一致」。

⚠️ **多标的换仓**：按决策 1 单独标注为**限 reference/bt** —— backtrader 会 fail-closed
（见 `test_engine_parity.py::TestMultiAssetRebalanceGap`），本文件只钉住「参考/bt 能消费」。
停牌顺延缺口见 `test_p5_bt_halt.py`。

### 已知 backtrader 局限（P5.5 定位，**如实记录，未修**）

用完整周频事件面板驱动 backtrader 时，另发现**两类**超出 P4.5 场景的拒单，都属
**引擎 broker 模型差异**而**不是** spec2weights 的问题：

| # | 触发条件 | 机理 | 处置 |
| --- | --- | --- | --- |
| L1 | 权重面板**重复断言同一目标**（连续多行 `1.0`，满仓） | `next_open()` 每行都重算 `desired=权益×权重/开盘价`；价格下跌 → `desired>持仓` → 下买单补仓，而现金≈0 → **Margin**。参考内核遇此情形按 F.5「缩减订单」（买入≈0） | 压缩为**变化点**面板（`event_panel`）——语义等价，且正是决策 1 的「建仓/清仓」形态 |
| L2 | **跳空低开日的建仓**（`0→1`） | backtrader 的**下单前保证金校验**用**上一收盘价**（券商当前标记价）而非成交开盘价；低开时 `size×昨日收盘 > 现金` → **Margin 误拒**（实测：`pos=0`、`现金=净值`，并非真缺钱） | 未修：属 broker 模型差异。**故对拍只取「一次建仓+一次清仓」的受支持形态**（决策 1 = A）；L2 的完整触发面记入 `parity_report.md` |

> 这两条正是决策 1 选择「缩小验证形态」的实证理由 —— 不是偷懒，是 backtrader 的
> broker 模型对「重复目标」与「跳空建仓」这两类本平台常见形态并不忠实。

运行：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -m unittest tests.test_p5_spec2weights -v
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from quantlab.contract.emit import MarketData, emit_weights, spec2weights
from quantlab.contract.types import (
    F8_SCENARIOS,
    ContractViolation,
    Expr,
    SizingSpec,
    StrategySpec,
)
from quantlab.engines.base import (
    DataBundle,
    EngineError,
    get_runner,
    load_bundle_from_fixture,
    register_builtin,
)
from quantlab.engines.execution import run_reference

TOLERANCE = 1e-4
INITIAL_CASH = 1_000_000.0
COSTS = F8_SCENARIOS[0]


def max_rel_dev(a: pd.Series, b: pd.Series) -> float:
    common = a.dropna().index.intersection(b.dropna().index)
    if not len(common):
        raise AssertionError("两条曲线没有公共日期")
    return float(((a[common] / b[common] - 1.0).abs()).max())


def _sma(n: int) -> Expr:
    return Expr("sma", (Expr("shift", (Expr("field", ("close",)), 1)), n))


def event_panel(weights: pd.DataFrame) -> pd.DataFrame:
    """把「每周重复同一目标」压缩成**变化点**，即 decision 1 的「建仓 / 清仓」形态。

    为什么必须压缩才能喂 backtrader：`WeightsStrategy.next_open()` 在**每一行**都重算
    `desired = 权益 × 权重 / 开盘价`。若连续多行都是 `1.0`（满仓），价格下跌时
    `desired > current` → 它会下买单补仓，而账户已满仓、现金≈0 → **Margin 拒单**
    → runner fail-closed。参考内核遇同样情形会「缩减订单」（买入≈0），故不报错。

    压缩是**语义安全**的：对参考/bt，缺行 = 维持持仓，等价于重复同一目标。
    关键是**同一份面板喂给所有引擎**，比较才成立（决策 1 = A「缩小验证形态」）。
    """
    changed = weights.fillna(0.0).ne(weights.fillna(0.0).shift()).any(axis=1)
    return weights[changed.fillna(True)]


def build_entry_clear_pair(weights: pd.DataFrame) -> pd.DataFrame:
    """从周频面板取出**一次建仓 + 其后一次清仓**（决策 1 的 backtrader 支持形态）。

    ⚠️ 为什么不做「整段周频事件面板」：见模块 docstring 的「已知 backtrader 局限」。
    """
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


def single_entry_exit_spec(symbol: int = 1) -> StrategySpec:
    """单标的、金叉建仓 / 死叉清仓 —— backtrader **支持**的形态（决策 1 的验证形态）。"""
    return StrategySpec(
        name="single-entry-exit", universe=(symbol,),
        entry=Expr("gt", (_sma(20), _sma(60))),
        exit=Expr("lt", (_sma(20), _sma(60))),
        sizing=SizingSpec(top_n=1), costs=COSTS, lookback=60)


def multi_asset_rebalance_spec(universe) -> StrategySpec:
    """3 标的 SMA 择时 + 周频等权调仓 —— **限 reference/bt**（backtrader 会 fail-closed）。"""
    return StrategySpec(
        name="multi-asset-rebalance", universe=tuple(universe),
        entry=Expr("gt", (_sma(20), _sma(60))), exit=None,
        sizing=SizingSpec(top_n=3), costs=COSTS, lookback=60)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        register_builtin()
        cls.full = load_bundle_from_fixture()

    def bundle(self, symbols, *, halt_free: bool) -> DataBundle:
        data = self.full.subset(list(symbols))
        if halt_free:
            keep = data.traded[data.symbols].astype(bool).all(axis=1)
            data = DataBundle(opens=data.opens[keep], closes=data.closes[keep],
                              traded=data.traded[keep], snapshot_id=data.snapshot_id,
                              base_currency=data.base_currency)
        return data

    @staticmethod
    def weights(spec: StrategySpec, data: DataBundle) -> pd.DataFrame:
        return spec2weights(spec, MarketData(prices=data.closes, traded=data.traded))


# --------------------------------------------------------------------------- #
# 契约：单一实现 + fail-closed
# --------------------------------------------------------------------------- #
class TestSpec2WeightsContract(_Base):
    def test_matches_emit_weights_on_a_valid_spec(self) -> None:
        """**单一实现**：规范入口与底层发射器不得分歧（防「两份实现悄悄漂移」）。"""
        data = self.bundle([1], halt_free=False)
        spec = single_entry_exit_spec(1)
        a = spec2weights(spec, MarketData(prices=data.closes, traded=data.traded))
        b = emit_weights(spec, MarketData(prices=data.closes, traded=data.traded))
        pd.testing.assert_frame_equal(a, b)

    def test_structurally_invalid_spec_is_refused(self) -> None:
        """fail-closed：结构非法的规格**拒绝发射**，而不是产出一份看着像样的权重。"""
        data = self.bundle([1], halt_free=False)
        bad = StrategySpec(name="", universe=(1,), entry=_sma(20), costs=COSTS, lookback=60)
        with self.assertRaises(ContractViolation):
            spec2weights(bad, MarketData(prices=data.closes, traded=data.traded))

    def test_output_satisfies_target_weights_invariants(self) -> None:
        data = self.bundle([1], halt_free=False)
        w = self.weights(single_entry_exit_spec(1), data)
        self.assertTrue((w.to_numpy() >= -1e-12).all(), "出现负权重")
        self.assertTrue((w.sum(axis=1) <= 1.0 + 1e-12).all(), "行和 > 1（杠杆）")
        self.assertTrue(set(w.index) <= set(data.closes.index), "调仓日不在交易日上")


# --------------------------------------------------------------------------- #
# V3a/V3b：同一份权重喂给两引擎 → 各自与参考口径一致（backtrader 支持的形态）
# --------------------------------------------------------------------------- #
class TestSpec2WeightsIsSingleSourceOfTruth(_Base):
    def _pair(self) -> tuple[pd.DataFrame, DataBundle]:
        data = self.bundle([1], halt_free=True)
        return build_entry_clear_pair(self.weights(single_entry_exit_spec(1), data)), data

    def test_backtrader_matches_reference_on_the_same_weights(self) -> None:
        """**同一份面板**喂 backtrader 与参考（开盘口径）→ 容差内一致。"""
        w, data = self._pair()
        ref = run_reference(w, data, COSTS, initial_cash=INITIAL_CASH, fill_at="open")[0]
        got = get_runner("backtrader").run(w, data, COSTS).equity
        dev = max_rel_dev(got, ref)
        self.assertLessEqual(dev, TOLERANCE, f"backtrader 偏差 {dev:.3e}")

    def test_bt_matches_reference_on_the_same_weights(self) -> None:
        data = self.bundle([1], halt_free=True)
        w = event_panel(self.weights(single_entry_exit_spec(1), data))
        ref = run_reference(w, data, COSTS, initial_cash=INITIAL_CASH, fill_at="close")[0]
        got = get_runner("bt").run(w, data, COSTS).equity
        dev = max_rel_dev(got, ref)
        self.assertLessEqual(dev, 1e-9, f"bt 偏差 {dev:.3e}（应接近机器精度）")

    def test_the_pair_really_is_an_entry_then_a_clear(self) -> None:
        """确认取到的是「建仓 → 清仓」两行（否则上面两条是空跑）。"""
        w, _ = self._pair()
        self.assertEqual(list(w.sum(axis=1).to_numpy()), [1.0, 0.0])


# --------------------------------------------------------------------------- #
# 多标的换仓：如实标注「限 reference/bt」
# --------------------------------------------------------------------------- #
class TestMultiAssetRebalanceIsLimitedToReferenceAndBt(_Base):
    def test_reference_and_bt_consume_the_weights(self) -> None:
        data = self.bundle([1, 2, 3], halt_free=True)
        w = self.weights(multi_asset_rebalance_spec(data.symbols), data)
        for engine in ("reference", "bt"):
            with self.subTest(engine=engine):
                eq = get_runner(engine).run(w, data, COSTS).equity.dropna()
                self.assertGreater(len(eq), 0)
                self.assertTrue(np.isfinite(eq.to_numpy()).all())

    def test_backtrader_fails_loudly_rather_than_lying(self) -> None:
        """**如实标注**：多标的换仓超出 backtrader 能力，必须**明确失败**。"""
        data = self.bundle([1, 2, 3], halt_free=True)
        w = self.weights(multi_asset_rebalance_spec(data.symbols), data)
        with self.assertRaises(EngineError) as ctx:
            get_runner("backtrader").run(w, data, COSTS)
        self.assertIn("未成交", str(ctx.exception))


# --------------------------------------------------------------------------- #
# V3c：未来扰动
# --------------------------------------------------------------------------- #
class TestFuturePerturbation(_Base):
    def test_perturbing_the_future_does_not_move_past_weights(self) -> None:
        """改**未来**价格，此前产出的权重必须**逐格不变**（因果性的构造性保证）。"""
        data = self.bundle([1, 2, 3], halt_free=True)
        spec = multi_asset_rebalance_spec(data.symbols)
        base = self.weights(spec, data)

        cut = data.closes.index[len(data.closes) // 2]
        perturbed = data.closes.copy()
        perturbed.loc[perturbed.index > cut] *= 3.0        # 只动未来
        data2 = DataBundle(opens=data.opens, closes=perturbed, traded=data.traded,
                           snapshot_id=data.snapshot_id, base_currency=data.base_currency)
        after = self.weights(spec, data2)

        pd.testing.assert_frame_equal(base.loc[base.index <= cut],
                                      after.loc[after.index <= cut])
        self.assertGreater(len(base.index[base.index <= cut]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
