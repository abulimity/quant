"""P4.1 协议/注册表 + 参考撮合内核（语义真值）验收。

参考 runner 是**对拍基准**，所以它自己的正确性是前提。此处把它钉死，
后续 backtrader / bt 才有可比对象。

覆盖：
    P4.1  V1 注册表解析；V3 未知引擎报明确错误（不静默回退）；V3 run_meta 三字段齐全
    撮合  V2 零成本 buy&hold 净值与解析解**逐点相等**（误差 0，非「容差内」）
          V3 停牌区间内无成交
          V3 现金不为负；净值 = 持仓市值 + 现金
          V3 买入受现金约束、不足时缩减订单并记录偏差
          V3 成本单调性：成本越高净值越低
          V3 未成交卖单不提前释放资金（先卖后买）

运行：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -m unittest discover -t . -s tests
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from quantlab.contract.types import F8_SCENARIOS
from quantlab.engines.base import (
    BacktestResult,
    DataBundle,
    EngineError,
    UnknownEngineError,
    available_engines,
    base_run_meta,
    get_runner,
    register,
    register_builtin,
    reset_registry,
)
from quantlab.engines.execution import buy_units, run_reference
from quantlab.engines.reference import ReferenceRunner

INITIAL_CASH = 1_000_000.0


# --------------------------------------------------------------------------- #
# 合成输入：不依赖夹具，便于精确手算
# --------------------------------------------------------------------------- #
def make_bundle(*, n: int = 60, symbols=(1, 2), seed: int = 3,
                traded_true: bool = True) -> DataBundle:
    index = pd.DatetimeIndex(pd.bdate_range("2021-01-04", periods=n), name="ts")
    rng = np.random.default_rng(seed)
    closes, opens = {}, {}
    for k, symbol in enumerate(symbols):
        steps = 0.0004 * (k + 1) + 0.010 * rng.standard_normal(n)
        path = 100.0 * np.exp(np.cumsum(steps))
        closes[symbol] = path
        opens[symbol] = path * (1.0 + rng.uniform(-0.003, 0.003, n))
    traded = pd.DataFrame(traded_true, index=index, columns=list(symbols))
    return DataBundle(opens=pd.DataFrame(opens, index=index),
                      closes=pd.DataFrame(closes, index=index),
                      traded=traded, snapshot_id="unit-test")


def buy_and_hold(bundle: DataBundle, symbol: int, decision_index: int = 0) -> pd.DataFrame:
    """在 `decision_index` 决策日下一笔满仓买单，此后不再产生新权重（维持持仓）。"""
    day = bundle.closes.index[decision_index]
    row = pd.DataFrame(0.0, index=[day], columns=bundle.symbols)
    row.loc[day, symbol] = 1.0
    return row


# --------------------------------------------------------------------------- #
# P4.1 注册表与元数据
# --------------------------------------------------------------------------- #
class TestRegistry(unittest.TestCase):
    def setUp(self) -> None:
        reset_registry()

    def tearDown(self) -> None:
        reset_registry()

    def test_register_and_resolve(self) -> None:
        runner = ReferenceRunner()
        register(runner)
        self.assertIs(get_runner("reference"), runner)
        self.assertEqual(available_engines(), ["reference"])

    def test_builtin_registry_resolves_required_engines(self) -> None:
        """V1：注册表可按名解析 backtrader / bt / reference。"""
        names = register_builtin()
        for expected in ("backtrader", "bt", "reference"):
            with self.subTest(engine=expected):
                self.assertIn(expected, names)
                self.assertEqual(get_runner(expected).engine, expected)

    def test_unknown_engine_raises_and_does_not_fall_back(self) -> None:
        """V3：未知引擎名报明确错误，**不静默回退**。"""
        register(ReferenceRunner())
        with self.assertRaises(UnknownEngineError) as ctx:
            get_runner("backtader")            # 拼写错误
        message = str(ctx.exception)
        self.assertIn("未知引擎", message)
        self.assertIn("不会", message)          # 明确说明不会回退

    def test_same_class_re_registration_is_idempotent(self) -> None:
        """同**类**实例可重复注册 —— `register_builtin()` 会被反复调用。"""
        first, second = ReferenceRunner(), ReferenceRunner()
        register(first)
        register(second)
        self.assertIs(get_runner("reference"), second)

    def test_different_class_under_same_name_is_refused(self) -> None:
        """换**不同类**实现才是真正的「静默替换」，必须拦下。"""

        class ImpostorRunner:
            engine = "reference"

            def run(self, *a, **kw):      # pragma: no cover - 不应被调用
                raise AssertionError("不应执行")

        register(ReferenceRunner())
        with self.assertRaises(EngineError) as ctx:
            register(ImpostorRunner())
        self.assertIn("拒绝静默替换", str(ctx.exception))

    def test_register_builtin_is_repeatable(self) -> None:
        """反复调用 `register_builtin()` 不得报错（多个测试类会各自调一次）。"""
        first = register_builtin()
        second = register_builtin()
        self.assertEqual(first, second)

    def test_result_requires_reproducibility_metadata(self) -> None:
        """V3：run_meta 缺任一复现性字段 → 构造即报错。"""
        index = pd.DatetimeIndex(["2021-01-04"], name="ts")
        empty = pd.DataFrame(0.0, index=index, columns=[1])
        for missing in ("env_lock_hash", "git_sha", "data_snapshot_id"):
            meta = {"env_lock_hash": "x", "git_sha": "y", "data_snapshot_id": "z"}
            meta.pop(missing)
            with self.subTest(missing=missing):
                with self.assertRaises(EngineError):
                    BacktestResult(equity=pd.Series([1.0], index=index),
                                   positions=empty, trades=pd.DataFrame(), run_meta=meta)

    def test_runner_run_meta_has_all_reproducibility_fields(self) -> None:
        bundle = make_bundle()
        result = ReferenceRunner().run(buy_and_hold(bundle, 1), bundle, F8_SCENARIOS[0])
        for field in ("env_lock_hash", "git_sha", "data_snapshot_id"):
            self.assertIn(field, result.run_meta)
            self.assertTrue(result.run_meta[field])
        self.assertEqual(result.run_meta["data_snapshot_id"], bundle.snapshot_id)

    def test_data_bundle_requires_a_snapshot_id(self) -> None:
        """快照 ID 是复现性前提，缺失必须报错。"""
        bundle = make_bundle()
        with self.assertRaises(EngineError):
            DataBundle(opens=bundle.opens, closes=bundle.closes, traded=bundle.traded,
                       snapshot_id="")

    def test_data_bundle_rejects_misaligned_shapes(self) -> None:
        bundle = make_bundle()
        with self.assertRaises(EngineError):
            DataBundle(opens=bundle.opens[1:], closes=bundle.closes, traded=bundle.traded,
                       snapshot_id="x")

    def test_base_run_meta_is_string_valued(self) -> None:
        meta = base_run_meta(make_bundle(), "reference")
        self.assertIsInstance(meta["git_sha"], str)


# --------------------------------------------------------------------------- #
# 撮合内核：正确性
# --------------------------------------------------------------------------- #
class TestReferenceBuyAndHold(unittest.TestCase):
    """V2：零成本 buy&hold 与解析解比对。"""

    def setUp(self) -> None:
        self.bundle = make_bundle()
        self.weights = buy_and_hold(self.bundle, 1)
        self.equity, self.positions, self.trades, self.audit = run_reference(
            self.weights, self.bundle, F8_SCENARIOS[0], initial_cash=INITIAL_CASH)

    def test_zero_cost_buy_and_hold_matches_analytic_exactly(self) -> None:
        """零成本下净值应**逐点精确**等于「份额 × 收盘价 + 剩余现金」。

        与解析解比对必须用**同一个基准**（都从成交价与成交份额出发）。
        若一边按「首个有效收盘」、另一边按「成交价」归一，会凭空造出千分之几的
        「偏差」—— 那是比较方法的问题，不是引擎的问题（我们已踩过这个坑）。
        """
        trade = self.trades.iloc[0]
        units, fill_price = float(trade["units"]), float(trade["price"])
        cash_left = INITIAL_CASH - units * fill_price

        prices = self.bundle.closes[1]
        idx = self.equity.index[self.equity.index >= trade["ts"]]
        expected = units * prices[idx] + cash_left
        np.testing.assert_allclose(self.equity[idx].to_numpy(), expected.to_numpy(),
                                   rtol=0, atol=1e-6)

    def test_zero_cost_leaves_no_cash_residue(self) -> None:
        """零成本满仓：现金应当正好用尽。"""
        self.assertAlmostEqual(float(self.audit["cash"].iloc[-1]), 0.0, places=6)

    def test_fill_price_is_the_next_session_open(self) -> None:
        """成交价必须是**决策日之后第一个会话的开盘价**（T+1 开盘口径）。"""
        decision = self.weights.index[0]
        expected_session = self.bundle.closes.index[self.bundle.closes.index > decision][0]
        trade = self.trades.iloc[0]
        self.assertEqual(pd.Timestamp(trade["ts"]), expected_session)
        self.assertAlmostEqual(float(trade["price"]),
                               float(self.bundle.opens.loc[expected_session, 1]), places=12)

    def test_holding_produces_no_further_trades(self) -> None:
        self.assertEqual(len(self.trades), 1, "维持持仓时不应反复交易")

    def test_equity_is_flat_before_the_fill(self) -> None:
        fill = self.trades.iloc[0]["ts"]
        before = self.equity[self.equity.index < fill]
        self.assertEqual(before.nunique(), 1)
        self.assertAlmostEqual(float(before.iloc[0]), INITIAL_CASH, places=6)

    def test_weights_are_logged_as_weights_not_units(self) -> None:
        """`target_weight` / `actual_weight` 必须是**权重**量纲（0~1），不是份额。

        份额是 9xxx 量级、权重是 0~1；混用会让「目标 vs 实际」的偏差分析彻底失真。
        """
        trade = self.trades.iloc[0]
        self.assertAlmostEqual(float(trade["target_weight"]), 1.0, places=9)
        self.assertLessEqual(float(trade["actual_weight"]), 1.0 + 1e-9)


class TestReferenceInvariants(unittest.TestCase):
    """V3：现金不为负、净值恒等式、停牌不成交、成本单调、缩减订单。"""

    def setUp(self) -> None:
        self.bundle = make_bundle()

    def test_cash_never_negative(self) -> None:
        for bps in (0, 10, 30):
            with self.subTest(bps=bps):
                _, _, _, audit = run_reference(buy_and_hold(self.bundle, 1), self.bundle,
                                               F8_SCENARIOS[bps], initial_cash=INITIAL_CASH)
                self.assertGreaterEqual(float(audit["cash"].min()), -1e-9)

    def test_net_value_equals_positions_plus_cash(self) -> None:
        for bps in (0, 10, 30):
            with self.subTest(bps=bps):
                _, _, _, audit = run_reference(buy_and_hold(self.bundle, 1), self.bundle,
                                               F8_SCENARIOS[bps], initial_cash=INITIAL_CASH)
                self.assertLess(float(audit["net_value_residual"].abs().max()), 1e-6)

    def test_cost_monotonicity(self) -> None:
        """V3：成本越高，净收益越低。"""
        finals = [float(run_reference(buy_and_hold(self.bundle, 1), self.bundle,
                                      F8_SCENARIOS[bps], initial_cash=INITIAL_CASH)[0].iloc[-1])
                  for bps in (0, 10, 30)]
        self.assertGreater(finals[0], finals[1], "10bps 应低于 0bps")
        self.assertGreater(finals[1], finals[2], "30bps 应低于 10bps")

    def test_suspension_blocks_trades(self) -> None:
        """V3：停牌区间内**无成交** —— 逐笔检查 trades。"""
        bundle = make_bundle(n=40)
        halt = bundle.closes.index[10:20]
        traded = pd.DataFrame(True, index=bundle.closes.index, columns=bundle.symbols)
        traded.loc[halt, 1] = False
        halted = DataBundle(opens=bundle.opens, closes=bundle.closes, traded=traded,
                            snapshot_id=bundle.snapshot_id)

        # 决策日取停牌**前一日**（halt 从 index[10] 开始，故决策在 index[9]），
        # 这样 T+1 恰好落在停牌首日 → 必须顺延。
        weights = buy_and_hold(halted, 1, decision_index=9)
        _, _, trades, _ = run_reference(weights, halted, F8_SCENARIOS[0],
                                        initial_cash=INITIAL_CASH)
        self.assertTrue(len(trades) > 0, "停牌结束后应当补上成交")
        fill = pd.Timestamp(trades.iloc[0]["ts"])
        self.assertNotIn(fill, halt, "在停牌日成交了")
        self.assertGreater(fill, halt[-1], "成交应顺延到停牌结束之后")

    def test_no_trade_at_all_when_symbol_never_trades(self) -> None:
        bundle = make_bundle(n=30)
        traded = pd.DataFrame(False, index=bundle.closes.index, columns=bundle.symbols)
        frozen = DataBundle(opens=bundle.opens, closes=bundle.closes, traded=traded,
                            snapshot_id=bundle.snapshot_id)
        _, _, trades, audit = run_reference(buy_and_hold(frozen, 1), frozen, F8_SCENARIOS[0],
                                            initial_cash=INITIAL_CASH)
        self.assertEqual(len(trades), 0)
        self.assertTrue(np.allclose(audit["cash"].to_numpy(), INITIAL_CASH))

    def test_insufficient_cash_shrinks_the_order_and_records_the_deviation(self) -> None:
        """F.5：资金不足时缩减订单，并记录目标与实际权重的偏差。"""
        bundle = make_bundle()
        day = bundle.closes.index[0]
        # 列名必须用**整数** symbol_id（与 universe 同类型）
        weights = pd.DataFrame({1: [0.9], 2: [0.9]}, index=[day])   # 行和 1.8 > 1
        _, _, trades, audit = run_reference(weights, bundle, F8_SCENARIOS[0],
                                            initial_cash=INITIAL_CASH)
        self.assertGreaterEqual(float(audit["cash"].min()), -1e-9, "出现了负现金")
        limited = trades[trades["reason"] == "cash_limited"]
        self.assertTrue(len(limited) > 0, "资金不足时未记录 cash_limited")
        self.assertTrue((limited["actual_weight"] <= limited["target_weight"] + 1e-9).all())

    def test_misaligned_weight_columns_fail_loudly(self) -> None:
        """**静默空仓**防线：权重列名与 universe 类型不一致时必须报错。

        典型事故：列名写成字符串 `"1"`（例如直接从 CSV 读入），而 universe 是整数 `1`。
        此时 `reindex` 全落空 → 所有目标变成 0 → 策略**看起来什么都没买**，
        既不报错也无从察觉。宁可直接失败。
        """
        bundle = make_bundle()
        day = bundle.closes.index[0]
        weights = pd.DataFrame({"1": [0.9]}, index=[day])      # 字符串列名
        with self.assertRaises(EngineError) as ctx:
            run_reference(weights, bundle, F8_SCENARIOS[0], initial_cash=INITIAL_CASH)
        self.assertIn("对不齐", str(ctx.exception))

    def test_buy_units_formula_is_subtractive_not_divisive(self) -> None:
        """F.6：份额 = 现金 × (1−成本) / 价格（**减法**还原，不是除法）。"""
        self.assertAlmostEqual(buy_units(1_000_000.0, 100.0, 0.001),
                               1_000_000.0 * 0.999 / 100.0)
        self.assertAlmostEqual(buy_units(100.0, 10.0, 0.0), 10.0)
        self.assertEqual(buy_units(100.0, 0.0, 0.001), 0.0)


# --------------------------------------------------------------------------- #
# 撮合内核：语义细节
# --------------------------------------------------------------------------- #
class TestReferenceSemantics(unittest.TestCase):
    def test_missing_weights_row_holds_position_does_not_liquidate(self) -> None:
        """权重面板里**没有**该决策日 → 维持现状；要清仓必须显式给一行全 0。"""
        bundle = make_bundle()
        entry = buy_and_hold(bundle, 1)
        exit_row = pd.DataFrame(0.0, index=[bundle.closes.index[20]], columns=bundle.symbols)

        held, _, trades_hold, _ = run_reference(entry, bundle, F8_SCENARIOS[0],
                                                initial_cash=INITIAL_CASH)
        closed, _, trades_close, _ = run_reference(pd.concat([entry, exit_row]), bundle,
                                                   F8_SCENARIOS[0], initial_cash=INITIAL_CASH)
        self.assertEqual(len(trades_hold), 1, "未给新权重却发生了交易")
        self.assertEqual(len(trades_close), 2, "显式清仓未生效")
        self.assertNotAlmostEqual(float(held.iloc[-1]), float(closed.iloc[-1]))

    def test_valuation_survives_partial_sessions(self) -> None:
        """三市场联合索引里只有单市场开市的日子：估值沿用最近有效价，净值不得变 NaN。

        这是本阶段发现的**真实缺陷**：直接用原始收盘价估值会让净值**静默变成 NaN**。
        """
        bundle = make_bundle(n=30)
        closes = bundle.closes.copy()
        closes.iloc[5:10, 0] = np.nan          # 模拟该标的在自家历法之外的日子
        partial = DataBundle(opens=bundle.opens, closes=closes, traded=bundle.traded,
                             snapshot_id=bundle.snapshot_id)
        equity, _, _, _ = run_reference(buy_and_hold(partial, 1), partial, F8_SCENARIOS[0],
                                        initial_cash=INITIAL_CASH)
        self.assertFalse(equity.isna().any(), "净值出现 NaN —— 估值未沿用最近有效价")
        self.assertTrue(np.isfinite(equity.to_numpy()).all())

    def test_partial_reduction_keeps_a_nonzero_position(self) -> None:
        """F.5：把仓位**减到目标份额**，而不是清仓。

        反例（曾存在于 backtrader runner）：写 `size=min(-delta, current)`，
        当目标小于当前持仓但非零时会把仓位清成 0 —— 覆盖成空仓。
        这里用参考内核把正确的语义钉死。
        """
        bundle = make_bundle()
        entry = buy_and_hold(bundle, 1)          # 满仓 1.0（此后现金 ≈ 0）
        day1, day2 = bundle.closes.index[20], bundle.closes.index[40]
        cut = pd.DataFrame(0.0, index=[day1], columns=bundle.symbols)
        cut.loc[day1, 1] = 0.25                  # 由 1.0 **减持**到 0.25
        add = pd.DataFrame(0.0, index=[day2], columns=bundle.symbols)
        add.loc[day2, 1] = 0.80                  # 再用腾出的现金**增持**到 0.80
        _, positions, trades, _ = run_reference(pd.concat([entry, cut, add]),
                                                bundle, F8_SCENARIOS[0],
                                                initial_cash=INITIAL_CASH)

        units = positions[1]
        held_full = float(units.loc[units.index <= day1].iloc[-1])
        held_cut = float(units.loc[units.index > day1].iloc[0])
        held_add = float(units.loc[units.index > day2].iloc[0])

        # 减持：这是曾经被**静默忽略**的路径（旧实现只在目标权重为 0 时才卖）
        self.assertLess(held_cut, held_full, "减持未生效 —— 「减到非零目标」被忽略了")
        self.assertGreater(held_cut, 0.0, "减持被错误地做成了清仓")
        self.assertTrue((units.loc[units.index > day1] > 0).all(), "减持后持仓归零")

        # 增持：用减持腾出的现金再买回（满仓时无杠杆可用，故不能直接 1.0→1.25）
        self.assertGreater(held_add, held_cut, "增持未生效")

        sides = trades[trades["ts"] > day1]["side"].tolist()
        self.assertEqual(sides[:2], ["sell", "buy"], "应先卖后买")

    def test_switch_sells_before_buying(self) -> None:
        """换仓必须先卖后买，否则资金不足导致新标的买不进（F.5）。"""
        bundle = make_bundle()
        entry = buy_and_hold(bundle, 1)
        switch_day = bundle.closes.index[20]
        switch = pd.DataFrame(0.0, index=[switch_day], columns=bundle.symbols)
        switch.loc[switch_day, 2] = 1.0
        _, _, trades, audit = run_reference(pd.concat([entry, switch]), bundle,
                                            F8_SCENARIOS[0], initial_cash=INITIAL_CASH)
        self.assertGreaterEqual(float(audit["cash"].min()), -1e-9)
        on_switch = trades[trades["ts"] > switch_day]
        self.assertIn("sell", set(on_switch["side"]), "换仓时没有先卖出")
        self.assertIn("buy", set(on_switch["side"]))
        self.assertEqual(on_switch.iloc[0]["side"], "sell", "应先卖后买")


if __name__ == "__main__":
    unittest.main(verbosity=2)
