"""P5 归因回归：bt 多标的换仓的 3.6% 偏差 = **停牌顺延缺口**（撮合本身没算错）。

背景：P4 对拍留下一个「bt 在多标的换仓上偏 3.6%、原因未定位」的缺口。
本用例把 P5 的定位结论**钉住**，防止将来回归：

    V1 全样本：bt vs reference(收盘诊断口径) 仍显著超容差 —— 缺口真实存在
    V2 无停牌样本：同两个引擎吻合到**机器精度** —— 偏差**全部**来自停牌顺延
    V3 bt 把 `halt_sessions` 写进 stats / run_meta —— **显式标注**，不静默

机理（`parity_report.md` §4.5）：bt 的 `WeighTarget` 没有「交易日掩码」概念，
递进去的是 `ffill` 价；再平衡日若落在停牌会话上（联合索引里某市场休市而另一开市），
bt 会**就地按停牌前旧价成交**，而统一口径要求**顺延到下一个可交易会话**。
bt 的向量化模型无法表达逐标的顺延，故如实记录。

⚠️ **V2 是本用例的核心价值**：若它失败，说明 bt 还有**停牌之外**的撮合缺陷
（而不仅仅是表达能力不足），必须重新排查 —— 这正是当初要「先定位再提交」的原因。

运行：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -m unittest tests.test_p5_bt_halt -v
"""

from __future__ import annotations

import unittest

import pandas as pd

from quantlab.contract.emit import MarketData, emit_weights
from quantlab.contract.types import F8_SCENARIOS, Expr, StrategySpec
from quantlab.engines.base import (
    DataBundle,
    get_runner,
    load_bundle_from_fixture,
    register_builtin,
)
from quantlab.engines.execution import run_reference

TOLERANCE = 1e-4
INITIAL_CASH = 1_000_000.0
MACHINE_TOLERANCE = 1e-12


def max_rel_dev(a: pd.Series, b: pd.Series) -> float:
    common = a.dropna().index.intersection(b.dropna().index)
    if not len(common):
        raise AssertionError("两条曲线没有公共日期")
    return float(((a[common] / b[common] - 1.0).abs()).max())


def rebalance_weights(data: DataBundle) -> pd.DataFrame:
    """3 标的 SMA20/60 择时 + 周频等权调仓（真实换仓形态）。"""
    spec = StrategySpec(
        name="multi-asset-rebalance", universe=tuple(data.symbols),
        entry=Expr("gt", (Expr("sma", (Expr("shift", (Expr("field", ("close",)), 1)), 20)),
                          Expr("sma", (Expr("shift", (Expr("field", ("close",)), 1)), 60)))),
        exit=None, costs=F8_SCENARIOS[0], lookback=60)
    return emit_weights(spec, MarketData(prices=data.closes, traded=data.traded))


class TestBtHaltAttribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        register_builtin()
        cls.full = load_bundle_from_fixture()

    def bundle(self, *, with_halts: bool) -> DataBundle:
        data = self.full.subset([1, 2, 3])
        if with_halts:
            return data
        keep = data.traded[data.symbols].astype(bool).all(axis=1)   # 三只都交易的会话
        return DataBundle(opens=data.opens[keep], closes=data.closes[keep],
                          traded=data.traded[keep], snapshot_id=data.snapshot_id,
                          base_currency=data.base_currency)

    def run_pair(self, data: DataBundle):
        weights = rebalance_weights(data)
        bt = get_runner("bt").run(weights, data, F8_SCENARIOS[0])
        ref_close = run_reference(weights, data, F8_SCENARIOS[0],
                                  initial_cash=INITIAL_CASH, fill_at="close")[0]
        return bt, ref_close

    # ------------------------------------------------------------------ #
    def test_gap_is_real_on_the_full_sample(self) -> None:
        """V1：全样本上缺口**真实存在**（否则本用例失去前提）。"""
        bt, ref_close = self.run_pair(self.bundle(with_halts=True))
        dev = max_rel_dev(bt.equity, ref_close)
        self.assertGreater(dev, TOLERANCE,
                           f"全样本偏差仅 {dev:.3e}，缺口消失了 —— 前提已变，需重新核实")

    def test_gap_vanishes_without_halts(self) -> None:
        """V2（核心）：去掉停牌会话后，两引擎吻合到**机器精度**。

        这条把「3.6% 全部归因于停牌顺延」变成可复跑的断言。失败即表示
        bt 仍有停牌之外的撮合缺陷 —— 不得据此收工。
        """
        bt, ref_close = self.run_pair(self.bundle(with_halts=False))
        dev = max_rel_dev(bt.equity, ref_close)
        self.assertLessEqual(
            dev, MACHINE_TOLERANCE,
            f"无停牌样本上仍偏 {dev:.3e} —— 说明还有**停牌之外**的缺陷，必须复查")

    def test_bt_flags_halt_sessions_instead_of_staying_silent(self) -> None:
        """V3：受停牌影响的运行必须**显式标注**（stats + run_meta），不得静默。"""
        data = self.bundle(with_halts=True)
        res = get_runner("bt").run(rebalance_weights(data), data, F8_SCENARIOS[0])
        self.assertGreater(res.stats["halt_sessions"], 0,
                           "全样本竟无停牌会话 —— 夹具或掩码有问题")
        self.assertIn("halt_deferral", res.run_meta["known_deviation"])
        self.assertEqual(res.run_meta["halt_sessions"], res.stats["halt_sessions"])

    def test_halt_free_run_carries_no_halt_flag(self) -> None:
        """对照：无停牌样本**不带**停牌标注 —— 证明标注是有信息量的，不是恒真。"""
        data = self.bundle(with_halts=False)
        res = get_runner("bt").run(rebalance_weights(data), data, F8_SCENARIOS[0])
        self.assertEqual(res.stats["halt_sessions"], 0)
        self.assertNotIn("halt_deferral", res.run_meta["known_deviation"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
