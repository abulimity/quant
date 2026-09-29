"""P4.5 跨引擎对拍（本阶段核心 Gate）。

三个场景，由简到繁，**先对齐语义再谈数值**（§P4.5）。

统一成交口径（正文 §P4.5）：**信号 T 日收盘生成 → T+1 开盘价成交**。
附录 F.4.4/F.8 写的是「信号后第一个有效交易日的**收盘**」，两处冲突；
手册规定以正文为准，故取 T+1 开盘。差异已在 `docs/deploy/parity_report.md` 记录。

容差 1e-4。**若不过，先排查成交时点/信号延迟等语义差异，而不是先怀疑数值精度**
（§P4.5 失败处理）。

运行：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -m unittest discover -t . -s tests
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from quantlab.contract.types import F8_SCENARIOS, CostModel
from quantlab.engines.base import (
    EngineError,
    DataBundle,
    get_runner,
    load_bundle_from_fixture,
    register_builtin,
)
from quantlab.engines.execution import run_reference

TOLERANCE = 1e-4
INITIAL_CASH = 1_000_000.0


def max_rel_dev(a: pd.Series, b: pd.Series) -> float:
    """两条净值曲线的最大相对偏差（先对齐到公共日期）。"""
    common = a.dropna().index.intersection(b.dropna().index)
    if not len(common):
        raise AssertionError("两条曲线没有公共日期 —— 无从比对")
    return float(((a[common] / b[common] - 1.0).abs()).max())


def buy_and_hold_weights(bundle: DataBundle, symbol: int) -> pd.DataFrame:
    """在该标的**自己的**首个可交易日决策满仓（避免选到它不交易的联合会话）。"""
    day = bundle.traded.index[bundle.traded[symbol].astype(bool)][0]
    row = pd.DataFrame(0.0, index=[day], columns=bundle.symbols)
    row.loc[day, symbol] = 1.0
    return row


class _ParityBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        register_builtin()
        cls.full = load_bundle_from_fixture()

    def single(self, symbol: int = 1) -> DataBundle:
        return self.full.subset([symbol])

    def run_engine(self, engine: str, weights: pd.DataFrame, data: DataBundle,
                   costs: CostModel):
        return get_runner(engine).run(weights, data, costs)


# --------------------------------------------------------------------------- #
# 场景 A：单市场 / 单标的 / 零成本
# --------------------------------------------------------------------------- #
class TestScenarioA(_ParityBase):
    """V3：三引擎在零成本下净值最大相对偏差 ≤ 1e-4。"""

    def setUp(self) -> None:
        self.data = self.single(1)
        self.weights = buy_and_hold_weights(self.data, 1)
        self.reference = run_reference(self.weights, self.data, F8_SCENARIOS[0],
                                       initial_cash=INITIAL_CASH)[0]

    def test_backtrader_matches_reference_within_tolerance(self) -> None:
        """backtrader 与参考口径同为「T+1 开盘成交」，应在容差内一致。"""
        result = self.run_engine("backtrader", self.weights, self.data, F8_SCENARIOS[0])
        dev = max_rel_dev(result.equity, self.reference)
        self.assertLessEqual(dev, TOLERANCE, f"backtrader 偏差 {dev:.3e} 超出容差")

    def test_backtrader_agrees_to_machine_precision(self) -> None:
        """更强：同为 T+1 开盘口径时，二者应接近**浮点精度**，而不是勉强过容差。"""
        result = self.run_engine("backtrader", self.weights, self.data, F8_SCENARIOS[0])
        dev = max_rel_dev(result.equity, self.reference)
        self.assertLessEqual(dev, 1e-12, f"偏差 {dev:.3e} —— 不应只是勉强达标")

    def test_bt_deviation_is_due_to_fill_timing_not_to_a_defect(self) -> None:
        """**差异分类的证据**（§P4.5 要求显式区分「设计不同」与「缺陷」）。

        bt 在再平衡日**收盘**撮合，我们约定的是 **T+1 开盘**。若把参考内核也切成
        收盘成交，bt 的偏差应立刻落回容差内 —— 那就证明差异来自成交时点，
        **不是**算错了。反之若仍超容差，才需继续逐层排查。
        """
        bt_equity = self.run_engine("bt", self.weights, self.data, F8_SCENARIOS[0]).equity
        close_ref = run_reference(self.weights, self.data, F8_SCENARIOS[0],
                                  initial_cash=INITIAL_CASH, fill_at="close")[0]

        dev_vs_open = max_rel_dev(bt_equity, self.reference)
        dev_vs_close = max_rel_dev(bt_equity, close_ref)

        self.assertGreater(dev_vs_open, TOLERANCE,
                           "bt 与 T+1 开盘口径竟然在容差内 —— 前提变了，需重新核实")
        self.assertLessEqual(dev_vs_close, TOLERANCE,
                             f"换成收盘口径仍超容差（{dev_vs_close:.3e}）—— "
                             f"说明还有**成交时点之外**的缺陷，必须继续排查")

    def test_three_engines_all_produce_a_finite_curve(self) -> None:
        for engine in ("reference", "backtrader", "bt"):
            with self.subTest(engine=engine):
                equity = self.run_engine(engine, self.weights, self.data,
                                         F8_SCENARIOS[0]).equity.dropna()
                self.assertGreater(len(equity), 0)
                self.assertTrue(np.isfinite(equity.to_numpy()).all(), "净值出现非有限值")


# --------------------------------------------------------------------------- #
# 场景 B：加成本（0/10/30 bps）
# --------------------------------------------------------------------------- #
class TestScenarioB(_ParityBase):
    """V3：成本越高净值越低；同成本下仍 ≤ 1e-4。"""

    def setUp(self) -> None:
        self.data = self.single(1)
        self.weights = buy_and_hold_weights(self.data, 1)

    def test_cost_monotonicity_for_every_engine(self) -> None:
        for engine in ("reference", "backtrader", "bt"):
            finals = []
            for bps in (0, 10, 30):
                equity = self.run_engine(engine, self.weights, self.data,
                                         F8_SCENARIOS[bps]).equity.dropna()
                finals.append(float(equity.iloc[-1]))
            with self.subTest(engine=engine):
                self.assertGreater(finals[0], finals[1], f"{engine}: 10bps 未低于 0bps")
                self.assertGreater(finals[1], finals[2], f"{engine}: 30bps 未低于 10bps")

    def test_backtrader_parity_holds_at_each_cost_level(self) -> None:
        for bps in (0, 10, 30):
            with self.subTest(bps=bps):
                ref = run_reference(self.weights, self.data, F8_SCENARIOS[bps],
                                    initial_cash=INITIAL_CASH)[0]
                got = self.run_engine("backtrader", self.weights, self.data,
                                      F8_SCENARIOS[bps]).equity
                self.assertLessEqual(max_rel_dev(got, ref), TOLERANCE)


# --------------------------------------------------------------------------- #
# 场景 C：多市场异步成交 + 现金约束（bt / backtrader）
# --------------------------------------------------------------------------- #
class TestScenarioC(_ParityBase):
    """V3：多市场异步成交与现金约束。

    ⚠️ **范围说明（如实记录，不含糊）**：本节覆盖「多市场异步成交」与「现金约束」，
    但**不覆盖汇率换算** —— 三个 runner 当前都不建模换汇（`fx_cost_bps` 非零时直接
    报错，而不是当成 0 悄悄放过）。跨币种换算属组合层（P6）职责，届时再接入对拍。
    """

    def setUp(self) -> None:
        # 1 为 XSHG、8 为 XNYS —— 两地日历不同，成交日天然异步
        self.data = self.full.subset([1, 8])
        day = self.data.traded.index[self.data.traded[1].astype(bool)][0]
        row = pd.DataFrame(0.0, index=[day], columns=self.data.symbols)
        row.loc[day, 1] = 0.5
        row.loc[day, 8] = 0.5
        self.weights = row

    def test_backtrader_matches_reference_on_multi_market(self) -> None:
        ref = run_reference(self.weights, self.data, F8_SCENARIOS[0],
                            initial_cash=INITIAL_CASH)[0]
        got = self.run_engine("backtrader", self.weights, self.data,
                              F8_SCENARIOS[0]).equity
        dev = max_rel_dev(got, ref)
        self.assertLessEqual(dev, TOLERANCE, f"多市场偏差 {dev:.3e}")

    def test_equity_is_finite_across_asynchronous_calendars(self) -> None:
        """两地日历不同 → 某些会话只有一边开市。净值不得因 NaN 估值而变成 NaN。"""
        for engine in ("reference", "backtrader"):
            with self.subTest(engine=engine):
                equity = self.run_engine(engine, self.weights, self.data,
                                         F8_SCENARIOS[0]).equity.dropna()
                self.assertTrue(np.isfinite(equity.to_numpy()).all(),
                                f"{engine} 在异步日历下净值出现 NaN")

    def test_fx_cost_is_refused_rather_than_silently_ignored(self) -> None:
        """未建模换汇时必须**报错**，而不是把 fx_cost 当 0 悄悄放过。"""
        costly = CostModel.scenario(10, fx_cost_bps=5)
        for engine in ("backtrader", "bt"):
            with self.subTest(engine=engine):
                with self.assertRaises(EngineError) as ctx:
                    self.run_engine(engine, self.weights, self.data, costly)
                self.assertIn("换汇", str(ctx.exception))

    def test_cash_constraint_blocks_over_allocation(self) -> None:
        """现金约束：目标权重之和 > 1 时不得出现负现金（参考内核为语义真值）。"""
        day = self.data.closes.index[0]
        greedy = pd.DataFrame({s: [0.8] for s in self.data.symbols}, index=[day])
        _, _, _, audit = run_reference(greedy, self.data, F8_SCENARIOS[0],
                                       initial_cash=INITIAL_CASH)
        self.assertGreaterEqual(float(audit["cash"].min()), -1e-9)

    def test_vectorbt_does_not_participate_in_scenario_c(self) -> None:
        """§P4.5：vectorbt 不参与场景 C（不建模撮合细节），此处显式记录。"""
        from quantlab.engines.base import available_engines

        self.assertNotIn("vectorbt", available_engines(),
                         "vectorbt 不应注册为 core 侧 BacktestRunner；"
                         "它只能经 bridge 在隔离环境里调用（P4.4）")


# --------------------------------------------------------------------------- #
# 已知缺口：多标的换仓（**不在原 §P4.5 定义的 A/B/C 场景内**）
# --------------------------------------------------------------------------- #
class TestMultiAssetRebalanceGap(_ParityBase):
    """**已知缺口**（由额外冒烟测试发现，非 §P4.5 定义的场景）。

    场景 A/B/C 都是「单标的 buy&hold / 建仓 / 清仓」，**从未覆盖多标的换仓**。
    补一个真实策略（3 标的、SMA 择时、周期调仓）后暴露：

        backtrader 在「同一根 bar 内卖 A 买 B」时会把买单判为 Margin 并**整单拒绝**
        —— 它的下单校验用的是**本根收盘价**与**当时现金**，而同根卖出所得要到
        结算后才入账。参考内核按 F.5 是「**缩减订单**」，两者口径不同。

    实测差异达 **16%**（1,261,800 vs 1,467,537），且此前**完全静默**
    （订单被丢弃、不报错、净值照算）。

    处置（当前）：**fail-closed** —— 出现未成交订单即抛 `EngineError`，
    **不再**给出一个悄悄错掉的结论。多标的换仓暂以 `reference` / `bt` 为准。

    ⚠️ 修复尝试（自行推算可用现金、`set_checksubmit(False)`、限价单）**均未成功**，
    且会**回归已通过的 A/B/C**，故已回退。此处用例把「当前是明确失败、而非静默错误」
    这一事实**钉住** —— 将来谁要修它，先看这条。
    """

    @staticmethod
    def _rebalancing_weights(bundle: DataBundle) -> pd.DataFrame:
        """3 标的 SMA20/60 择时 + 周频等权调仓（真实换仓形态）。"""
        from quantlab.contract.emit import MarketData, emit_weights
        from quantlab.contract.types import Expr, StrategySpec

        spec = StrategySpec(
            name="multi-asset-rebalance", universe=tuple(bundle.symbols),
            entry=Expr("gt", (Expr("sma", (Expr("shift", (Expr("field", ("close",)), 1)), 20)),
                              Expr("sma", (Expr("shift", (Expr("field", ("close",)), 1)), 60)))),
            exit=None, costs=F8_SCENARIOS[0], lookback=60)
        return emit_weights(spec, MarketData(prices=bundle.closes, traded=bundle.traded))

    def setUp(self) -> None:
        self.data = self.full.subset([1, 2, 3])
        self.weights = self._rebalancing_weights(self.data)

    def test_reference_handles_multi_asset_rebalancing(self) -> None:
        """参考内核能正常处理多标的换仓（语义真值）。"""
        ref = self.run_engine("reference", self.weights, self.data, F8_SCENARIOS[0])
        equity = ref.equity.dropna()
        self.assertGreater(len(equity), 0)
        self.assertTrue(np.isfinite(equity.to_numpy()).all())

    def test_weights_really_do_rebalance(self) -> None:
        """确认这份权重确实在不断换仓（而不是又一个 buy&hold）。"""
        changed = (self.weights.fillna(0).diff().abs().sum(axis=1) > 0).sum()
        self.assertGreater(int(changed), 10, "权重没有发生换仓，本用例就失去意义")

    def test_backtrader_fails_loudly_instead_of_silently_lying(self) -> None:
        """**关键**：backtrader 在此场景必须**明确失败**，不得返回悄悄错掉的结果。"""
        with self.assertRaises(EngineError) as ctx:
            self.run_engine("backtrader", self.weights, self.data, F8_SCENARIOS[0])
        message = str(ctx.exception)
        self.assertIn("未成交", message)
        self.assertIn("缩减订单", message)         # 指出 F.5 的正确口径
        self.assertIn("reference", message)        # 指出可用的替代

    def test_error_names_the_affected_orders(self) -> None:
        """报错要能定位到具体日期与标的，否则无从排查。"""
        with self.assertRaises(EngineError) as ctx:
            self.run_engine("backtrader", self.weights, self.data, F8_SCENARIOS[0])
        self.assertIn("Margin", str(ctx.exception))
        self.assertRegex(str(ctx.exception), r"\d{4}-\d{2}-\d{2}")


# --------------------------------------------------------------------------- #
# 复现性
# --------------------------------------------------------------------------- #
class TestReproducibility(_ParityBase):
    """V4：相同输入重跑两次，对拍结论一致。"""

    def test_reruns_are_bitwise_identical(self) -> None:
        data = self.single(1)
        weights = buy_and_hold_weights(data, 1)
        for engine in ("reference", "backtrader", "bt"):
            with self.subTest(engine=engine):
                first = self.run_engine(engine, weights, data, F8_SCENARIOS[10]).equity
                second = self.run_engine(engine, weights, data, F8_SCENARIOS[10]).equity
                np.testing.assert_array_equal(first.dropna().to_numpy(),
                                              second.dropna().to_numpy())

    def test_run_meta_records_the_same_snapshot_and_lock(self) -> None:
        data = self.single(1)
        weights = buy_and_hold_weights(data, 1)
        metas = [self.run_engine(e, weights, data, F8_SCENARIOS[0]).run_meta
                 for e in ("reference", "backtrader", "bt")]
        for meta in metas:
            self.assertEqual(meta["data_snapshot_id"], data.snapshot_id)
            self.assertEqual(meta["env_lock_hash"], metas[0]["env_lock_hash"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
