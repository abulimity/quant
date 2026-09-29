"""P2.6 Silver/Gold 清洗与质量校验验收（LOCAL_DEPLOYMENT_PLAN.md §P2.6）。

覆盖：
    V3  质量校验能**捕获**人为注入的**每种**缺陷（每种一个用例，不只测正常数据）
    V3  休市 / 停牌 / 失败三态**取值互不相同**（脚本断言）
    V3  复权：用夹具的已知拆分日核对，复权因子与手算一致
    V3  汇率：内部统一「1 原币 = N 基准货币」；构造反向输入，断言取倒数并留记录
    V1  gold 层可产出回测输入视图

运行：
    python -m unittest discover -t . -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import date

import numpy as np
import pandas as pd

from quantlab.fixtures import spec as S
from quantlab.fixtures.synth import generate
from quantlab.quality.checks import (
    check_calendar_gaps,
    check_fx_direction,
    check_fx_staleness,
    check_ohlc_relations,
    check_positive_prices,
    check_price_jumps,
    check_sorted,
    check_unique_keys,
    run_all_checks,
)
from quantlab.quality.clean import (
    ADJUST_BACKWARD,
    ADJUST_FORWARD,
    SessionStatus,
    adjust_prices,
    build_session_status,
    gold_backtest_view,
    normalize_fx_direction,
    split_factors,
    total_return_index,
)

_BUNDLE = generate()
BARS = _BUNDLE.tables["bars_daily"]
ACTIONS = _BUNDLE.tables["corporate_actions"]
CALENDAR = _BUNDLE.tables["trading_calendar"]
FX = _BUNDLE.tables["fx_rates"]

SPLIT_SYMBOL = S.SPLITS[0].symbol_id
DIV_SYMBOL = S.DIVIDENDS[0].symbol_id
HALT_SYMBOL = S.SUSPENSIONS[0].symbol_id
GAP_SYMBOL = S.FAILURES[0].symbol_id


def _exchange_of(symbol_id: int) -> str:
    return S.SYMBOLS_BY_ID[symbol_id].exchange


def _one(symbol_id: int) -> pd.DataFrame:
    return BARS[BARS["symbol_id"] == symbol_id].sort_values("ts").reset_index(drop=True)


def _adjusted(symbol_id: int) -> pd.DataFrame:
    return adjust_prices(_one(symbol_id),
                         ACTIONS[ACTIONS["symbol_id"] == symbol_id])


# 全样本的复权表（异常跳变检测必须用它，否则拆分日会被误报成跳空）
BARS_ADJ = pd.concat([_adjusted(sid) for sid in sorted(BARS["symbol_id"].unique())],
                     ignore_index=True)

# 夹具**故意**保留的下载失败窗：P2.6 的存在意义之一就是把它抓出来
FAILURE_WINDOW = set(pd.date_range(S.FAILURES[0].start, S.FAILURES[0].end, freq="D").date)


class TestFixturePassesAllChecks(unittest.TestCase):
    """正常数据必须通过。

    例外只有一个：夹具**故意**内嵌了 symbol 6 的下载失败窗 —— 那不是误报，
    而是质量校验应当报出的**真实缺陷**，故按「其余全过、缺口精确等于该失败窗」来断言。
    """

    def test_report_passes_except_the_declared_failure_window(self) -> None:
        report = run_all_checks(bars=BARS, calendar=CALENDAR, fx=FX,
                                exchange_of=_exchange_of, adjusted_bars=BARS_ADJ)
        failures = [r for r in report.results if not r.ok]
        self.assertEqual([r.name for r in failures], ["calendar_gaps"],
                         "除声明的下载失败窗外，不应有任何其它失败:\n" + report.summary())

        gaps = report.by_name("calendar_gaps")
        self.assertTrue(all(s["symbol_id"] == GAP_SYMBOL for s in gaps.samples))
        self.assertEqual({s["ts"] for s in gaps.samples} <= FAILURE_WINDOW, True)

    def test_each_rule_individually_behaves_as_expected(self) -> None:
        self.assertTrue(check_positive_prices(BARS).ok)
        self.assertTrue(check_ohlc_relations(BARS).ok)
        self.assertTrue(check_unique_keys(BARS).ok)
        self.assertTrue(check_sorted(BARS).ok)
        self.assertTrue(check_price_jumps(BARS_ADJ).ok, "复权后不应有跳变")
        self.assertTrue(check_fx_staleness(FX).ok)
        self.assertTrue(check_fx_direction(FX).ok)

        gaps = check_calendar_gaps(BARS, CALENDAR, exchange_of=_exchange_of)
        self.assertEqual(gaps.violations, len(FAILURE_WINDOW),
                         "缺口数应恰等于声明的失败窗天数")
        self.assertEqual({s["ts"] for s in gaps.samples}, FAILURE_WINDOW)

    def test_raw_prices_would_flag_the_split_as_a_jump(self) -> None:
        """**反证**：若用未复权价检测跳变，4:1 拆分会被误报成跳空。

        这条用例把「必须用复权价」固化成可执行事实，防止后人把
        `check_price_jumps` 误接回未复权序列。
        """
        result = check_price_jumps(BARS, price_col="close")
        self.assertFalse(result.ok, "未复权价应把拆分误报为跳变（这正是要避免的）")
        self.assertTrue(any(s["symbol_id"] == SPLIT_SYMBOL for s in result.samples))


class TestInjectedDefectsAreCaught(unittest.TestCase):
    """§P2.6 原文：『每种缺陷一个用例，不得只测正常数据通过』。"""

    def test_non_positive_price_is_caught(self) -> None:
        bars = BARS.copy()
        bars.loc[bars.index[0], "close"] = -1.0
        result = check_positive_prices(bars)
        self.assertFalse(result.ok)
        self.assertEqual(result.violations, 1)

    def test_broken_ohlc_relation_is_caught(self) -> None:
        bars = BARS.copy()
        bars.loc[bars.index[0], "high"] = bars.loc[bars.index[0], "low"] - 1.0
        self.assertFalse(check_ohlc_relations(bars).ok)

    def test_duplicate_key_is_caught(self) -> None:
        bars = pd.concat([BARS, BARS.iloc[[0]]], ignore_index=True)
        result = check_unique_keys(bars)
        self.assertFalse(result.ok)
        self.assertGreaterEqual(result.violations, 2)

    def test_unsorted_dates_are_caught(self) -> None:
        bars = BARS.copy()
        subset = bars[bars["symbol_id"] == 1].sort_values("ts")
        first, second = subset.index[0], subset.index[1]
        bars.loc[first, "ts"], bars.loc[second, "ts"] = (
            subset.loc[second, "ts"], subset.loc[first, "ts"])
        result = check_sorted(bars)
        self.assertFalse(result.ok)
        self.assertIn(1, result.samples)

    def test_calendar_gap_is_caught(self) -> None:
        """删掉一个**开市**交易日 → 必须报成缺口（下载失败）。"""
        victim = _one(GAP_SYMBOL)
        drop_ts = victim["ts"].iloc[100]
        bars = BARS[~((BARS["symbol_id"] == GAP_SYMBOL) & (BARS["ts"] == drop_ts))]
        result = check_calendar_gaps(bars, CALENDAR, exchange_of=_exchange_of)
        self.assertFalse(result.ok)
        self.assertIn({"symbol_id": GAP_SYMBOL, "ts": drop_ts}, result.samples)

    def test_holiday_is_not_reported_as_gap(self) -> None:
        """**关键区分**：休市日无行是**正常**的，不得报成缺口。

        注意必须用**该标的所属交易所**的休市日来比对。用「全交易所休市日的并集」
        会踩坑：2023-07-04 是美国独立日（XNYS 休市），但**香港当天照常开市**，
        于是它理应出现在 XHKG 标的的缺口里 —— 那是正确行为，不是误报。
        """
        result = check_calendar_gaps(BARS, CALENDAR, exchange_of=_exchange_of)
        reported = {r["ts"] for r in result.samples}
        xhkg_holidays = set(CALENDAR.loc[(~CALENDAR["is_open"].astype(bool))
                                         & (CALENDAR["exchange"] == "XHKG"), "ts"])
        self.assertEqual(reported & xhkg_holidays, set(), "XHKG 休市日被误报为数据缺口")
        # 被报出的应当**恰好**是声明的下载失败窗
        self.assertEqual(reported, FAILURE_WINDOW)

    def test_holiday_across_exchanges_is_not_confused(self) -> None:
        """跨市场陷阱：XSHG 在 XNYS 的圣诞节**是交易日**，不得被当成休市。

        只有按**该标的自己的交易所**取日历才判得对；用「全交易所休市日的并集」
        会把正常交易日误判为休市。
        """
        holidays_xshg = set(CALENDAR.loc[(~CALENDAR["is_open"].astype(bool))
                                         & (CALENDAR["exchange"] == "XSHG"), "ts"])
        holidays_all = set(CALENDAR.loc[~CALENDAR["is_open"].astype(bool), "ts"])
        present = set(BARS.loc[BARS["symbol_id"] == HALT_SYMBOL, "ts"])
        self.assertEqual(present & holidays_xshg, set(), "XSHG 标的不应在 XSHG 休市日有行")
        self.assertTrue(present & (holidays_all - holidays_xshg),
                        "XSHG 标的应在其它市场的休市日有行（跨市场日历不可混用）")

    def test_price_jump_is_caught(self) -> None:
        bars = BARS_ADJ.copy()
        subset = bars[bars["symbol_id"] == 1].sort_values("ts")
        idx = subset.index[500]
        bars.loc[idx, "close_adj"] = bars.loc[idx, "close_adj"] * 3.0   # 单日翻三倍
        result = check_price_jumps(bars)
        self.assertFalse(result.ok)
        self.assertTrue(any(s["log_return"] > 1.0 for s in result.samples))

    def test_jump_detection_is_not_defeated_by_its_own_window(self) -> None:
        """**自废检测器**的反证：滚动波动必须排除被检验当天。

        若窗口含当天，突变会抬高自身 σ，`|r| > 8σ` 永不成立 —— 检测器沦为摆设。
        这条用例注入一个**孤立**突跳并断言它仍被抓住。
        """
        bars = BARS_ADJ.copy()
        subset = bars[bars["symbol_id"] == 9].sort_values("ts")
        idx = subset.index[1000]
        bars.loc[idx, "close_adj"] = bars.loc[idx, "close_adj"] * 5.0
        self.assertFalse(check_price_jumps(bars).ok,
                         "孤立突跳未被抓住 → 波动窗口很可能把当天算了进去")

    def test_stale_fx_is_caught(self) -> None:
        fx = FX.copy()
        mask = (fx["base"] == "USD") & (fx["quote"] == "CNY")
        flat_index = fx[mask].sort_values("ts").index[:20]
        fx.loc[flat_index, "rate"] = 7.1                        # 连续 20 天一字不差
        result = check_fx_staleness(fx)
        self.assertFalse(result.ok)
        self.assertGreater(result.samples[0]["longest_flat_run"], 10)

    def test_inconsistent_fx_direction_is_caught(self) -> None:
        """同一对同时给两个方向且乘积 ≠ 1 → 方向不一致。"""
        usd_cny = FX[(FX["base"] == "USD") & (FX["quote"] == "CNY")].head(5)
        bogus = usd_cny.assign(base="CNY", quote="USD", rate=lambda d: 1.0 / d["rate"] * 2)
        combined = pd.concat([FX, bogus], ignore_index=True)
        result = check_fx_direction(combined)
        self.assertFalse(result.ok)
        self.assertTrue(any(s.get("issue") == "inconsistent_direction" for s in result.samples))

    def test_non_positive_fx_is_caught(self) -> None:
        fx = FX.copy()
        fx.loc[fx.index[0], "rate"] = -1.0
        self.assertFalse(check_fx_direction(fx).ok)


class TestThreeStates(unittest.TestCase):
    """休市 / 停牌 / 下载失败 三种状态**取值互不相同**，且判定正确。"""

    def setUp(self) -> None:
        self.status = build_session_status(
            BARS, CALENDAR, symbol_id=HALT_SYMBOL, exchange=_exchange_of(HALT_SYMBOL),
            start=date(2021, 2, 1), end=date(2021, 4, 1),
        )
        self.by_date = dict(zip(self.status["ts"], self.status["status"]))

    def test_three_state_values_are_distinct(self) -> None:
        states = SessionStatus.three_states()
        self.assertEqual(len(set(states)), 3, "三态取值必须互不相同")
        self.assertEqual(states, ("closed", "halted", "missing"))

    def test_all_relevant_states_appear_in_this_window(self) -> None:
        values = set(self.by_date.values())
        self.assertIn("halted", values, "停牌窗内应出现 halted")
        self.assertIn("traded", values, "非停牌日应为 traded")
        self.assertIn("closed", values, "窗口内应出现休市日（春节）")

    def test_suspension_days_are_halted_not_closed(self) -> None:
        """停牌日必须是 halted —— 若判成 closed，就等于把「不能成交」误当「没开市」。"""
        spec = S.SUSPENSIONS[0]
        halted = [d for d, s in self.by_date.items()
                  if s == "halted" and pd.Timestamp(d) >= pd.Timestamp(spec.start)
                  and pd.Timestamp(d) <= pd.Timestamp(spec.end)]
        self.assertTrue(halted, "停牌窗内未识别出 halted")

    def test_missing_is_detected_for_failure_window(self) -> None:
        """下载失败窗内的**开市**日必须是 missing（不是 closed）。"""
        spec = S.FAILURES[0]
        status = build_session_status(
            BARS, CALENDAR, symbol_id=GAP_SYMBOL, exchange=_exchange_of(GAP_SYMBOL),
            start=spec.start, end=spec.end,
        )
        self.assertIn("missing", set(status["status"]), "下载失败窗内应出现 missing 状态")

    def test_closed_days_never_have_rows_in_bars(self) -> None:
        """休市日不应有行情行 —— 必须按**该标的自己的交易所**取日历。"""
        exchange = _exchange_of(HALT_SYMBOL)
        holidays = set(CALENDAR.loc[(~CALENDAR["is_open"].astype(bool))
                                    & (CALENDAR["exchange"] == exchange), "ts"])
        present = set(BARS.loc[BARS["symbol_id"] == HALT_SYMBOL, "ts"])
        self.assertEqual(present & holidays, set(), "休市日不应有行情行")


class TestAdjustment(unittest.TestCase):
    """复权因子与手算一致（与夹具的已知拆分日核对）。"""

    def test_split_factors_match_hand_calculation(self) -> None:
        spec = S.SPLITS[0]
        frame = _one(SPLIT_SYMBOL)
        factors = split_factors(frame["ts"], ACTIONS[ACTIONS["symbol_id"] == SPLIT_SYMBOL])
        before = (frame["ts"] < spec.ex_date).to_numpy()
        after = (frame["ts"] >= spec.ex_date).to_numpy()
        self.assertTrue(np.allclose(factors[before], 1.0), "除权前因子应为 1")
        self.assertTrue(np.allclose(factors[after], spec.ratio),
                        f"除权后因子应为拆分比例 {spec.ratio}")

    def test_split_adjustment_removes_exactly_the_split_jump(self) -> None:
        """拆分日前后的**原始**收益里含一个 ratio 因子；复权后该因子被精确移除。

        不能断言「复权后相邻两日价格相等」—— 相邻日之间本来就有正常日收益。
        正确的不变量是：`raw_ratio / adj_ratio` 恰好等于拆分比例。
        """
        spec = S.SPLITS[0]
        frame = _one(SPLIT_SYMBOL)
        adjusted = adjust_prices(frame, ACTIONS[ACTIONS["symbol_id"] == SPLIT_SYMBOL])
        idx = adjusted.index[adjusted["ts"] == spec.ex_date][0]

        raw_ratio = adjusted.loc[idx - 1, "close"] / adjusted.loc[idx, "close"]
        adj_ratio = adjusted.loc[idx - 1, "close_adj"] / adjusted.loc[idx, "close_adj"]
        self.assertGreater(raw_ratio, 2.0, "原始价格未见预期下移")
        self.assertAlmostEqual(raw_ratio / adj_ratio, spec.ratio, places=9,
                               msg="复权未精确移除拆分因子")

    def test_backward_adjustment_keeps_last_price_unchanged(self) -> None:
        """前复权以**最新**为基准：最新一根的复权价 == 原始价。"""
        frame = _one(SPLIT_SYMBOL)
        adjusted = adjust_prices(frame, ACTIONS[ACTIONS["symbol_id"] == SPLIT_SYMBOL],
                                 method=ADJUST_BACKWARD)
        self.assertAlmostEqual(float(adjusted["close_adj"].iloc[-1]),
                               float(adjusted["close"].iloc[-1]), places=9)

    def test_forward_adjustment_keeps_first_price_unchanged(self) -> None:
        """后复权以**最初**为基准：第一根的复权价 == 原始价。"""
        frame = _one(SPLIT_SYMBOL)
        adjusted = adjust_prices(frame, ACTIONS[ACTIONS["symbol_id"] == SPLIT_SYMBOL],
                                 method=ADJUST_FORWARD)
        self.assertAlmostEqual(float(adjusted["close_adj"].iloc[0]),
                               float(adjusted["close"].iloc[0]), places=9)

    def test_dividend_is_not_folded_into_price(self) -> None:
        """F.6：已用总收益序列时**不再额外**把分红计进价格（否则重复计收益）。"""
        frame = _one(DIV_SYMBOL)
        adjusted = adjust_prices(frame, ACTIONS[ACTIONS["symbol_id"] == DIV_SYMBOL])
        self.assertTrue(np.allclose(adjusted["adjust_multiplier"].to_numpy(), 1.0))
        self.assertTrue(np.allclose(adjusted["close_adj"].to_numpy(),
                                    adjusted["close"].to_numpy()))

    def test_total_return_index_matches_fixture_analytic_answer(self) -> None:
        """V3 核心：清洗层算出的总收益 == 夹具解析答案（无失败窗标的）。"""
        for symbol_id in [s.symbol_id for s in S.SYMBOLS if s.symbol_id != GAP_SYMBOL]:
            with self.subTest(symbol_id=symbol_id):
                frame = _one(symbol_id)
                nav = total_return_index(frame, ACTIONS[ACTIONS["symbol_id"] == symbol_id])
                analytic = _BUNDLE.analytic_nav(symbol_id, traded_only=False)["nav"].to_numpy()
                np.testing.assert_allclose(nav.to_numpy(), analytic, rtol=0, atol=1e-9)


class TestFxDirection(unittest.TestCase):
    """构造反向输入 → 断言取倒数并留记录。"""

    @staticmethod
    def _reversed_input(*, window: int | None = None) -> pd.DataFrame:
        """**构造**反向输入：模拟「供应商只给反方向」的 CNY/USD 序列。

        按 §P2.6 V3 的措辞，反向输入由测试**构造**，不混进夹具。

        `window` —— 只取前 N 行（默认取**全部且不重叠**的一段）。取子集是为了
        让反向序列的 ts 与正向序列**不重合**：同一对若两个方向在同一 ts 上并存，
        归一化后会撞成重复行，那是另一回事（见 `test_ambiguous_direction_...`）。
        """
        usd_cny = (FX[(FX["base"] == "USD") & (FX["quote"] == "CNY")]
                   .sort_values("ts").reset_index(drop=True))
        if window is None:
            window = len(usd_cny) // 2
        chosen = usd_cny.iloc[:window]
        reversed_rows = chosen.assign(base="CNY", quote="USD",
                                      rate=1.0 / chosen["rate"].astype("float64"))
        # 正向序列去掉被反向覆盖的那段 ts，保证同 ts 只出现一个方向
        kept_forward = usd_cny.iloc[window:]
        merged = pd.concat([FX[~((FX["base"] == "USD") & (FX["quote"] == "CNY"))],
                            kept_forward, reversed_rows], ignore_index=True)
        return merged

    def test_reversed_pair_is_inverted_and_recorded(self) -> None:
        _, records = normalize_fx_direction(self._reversed_input())
        inverted = [r for r in records if r.action == "inverted"]
        self.assertTrue(inverted, "未识别出反向报价对")
        self.assertEqual((inverted[0].base, inverted[0].quote), ("USD", "CNY"))

    def test_inversion_actually_flips_the_values(self) -> None:
        raw = self._reversed_input()
        normalized, _ = normalize_fx_direction(raw)
        usd_cny = normalized[(normalized["base"] == "USD") & (normalized["quote"] == "CNY")]
        self.assertTrue((usd_cny["rate"] > 1.0).all(), "USD/CNY 应 > 1")

        original_reverse = raw[(raw["base"] == "CNY") & (raw["quote"] == "USD")]
        merged = original_reverse.merge(usd_cny[["ts", "rate"]], on="ts",
                                        suffixes=("_rev", "_fwd"))
        np.testing.assert_allclose(merged["rate_rev"].to_numpy(),
                                   (1.0 / merged["rate_fwd"]).to_numpy(), rtol=1e-12)

    def test_single_direction_is_kept_and_disclosed(self) -> None:
        """只出现单向报价 → 不得凭空断言方向错误，但要留记录披露。"""
        _, records = normalize_fx_direction(FX)
        kept = [r for r in records if r.action == "kept"]
        self.assertTrue(kept)
        self.assertTrue(any("无依据判定方向错误" in r.note for r in kept))

    def test_fixture_directions_are_self_consistent(self) -> None:
        """夹具里的汇率方向本就正确（1 原币 = N 基准货币），归一后不改动任何值。"""
        normalized, records = normalize_fx_direction(FX)
        self.assertFalse([r for r in records if r.action == "inverted"],
                         "夹具不应内含反向报价对")
        merged = FX.merge(normalized, on=["base", "quote", "ts"], suffixes=("_in", "_out"))
        np.testing.assert_allclose(merged["rate_in"].to_numpy(),
                                   merged["rate_out"].to_numpy(), rtol=0, atol=0)

    @staticmethod
    def _both_directions_same_ts() -> pd.DataFrame:
        """同一 ts 上**同时**存在 USD/CNY 与 CNY/USD —— 真实的歧义输入。

        这正是归一化会撞车的形态：反向行取倒数后会与正向行归到同一 (base,quote)。
        """
        usd_cny = FX[(FX["base"] == "USD") & (FX["quote"] == "CNY")].copy()
        reversed_rows = usd_cny.assign(base="CNY", quote="USD",
                                       rate=1.0 / usd_cny["rate"].astype("float64"))
        return pd.concat([FX, reversed_rows], ignore_index=True)

    def test_ambiguous_direction_is_rejected_not_silently_duplicated(self) -> None:
        """**关键负向**：同一对同时给两个方向时，归一化不得悄悄折叠成重复序列。

        若放任不管，inverted 后的 (USD,CNY) 会与原有 (USD,CNY) 撞车，
        同一 (base,quote,ts) 出现两行 —— 等于凭空复制了一份汇率。
        """
        from quantlab.quality.clean import FxDirectionError

        with self.assertRaises(FxDirectionError):
            normalize_fx_direction(self._both_directions_same_ts())

    def test_normalized_direction_passes_the_check(self) -> None:
        """归一之后再跑方向检查应通过 —— 证明归一确实消除了不一致。"""
        normalized, _ = normalize_fx_direction(FX)
        self.assertTrue(check_fx_direction(normalized).ok)
        self.assertTrue(check_fx_direction(FX).ok, "夹具本身方向自洽，应通过")

    def test_inconsistent_direction_is_flagged(self) -> None:
        """同一 ts 双方向且乘积 ≠ 1 → 方向检查必须判红。"""
        broken = self._both_directions_same_ts()
        mask = broken["base"] == "CNY"
        broken.loc[mask, "rate"] = broken.loc[mask, "rate"] * 1.7   # 破坏互为倒数
        result = check_fx_direction(broken)
        self.assertFalse(result.ok)
        self.assertTrue(any(s.get("issue") == "inconsistent_direction" for s in result.samples))


class TestGoldLayer(unittest.TestCase):
    """gold 层可产出回测输入视图（V1）。"""

    def setUp(self) -> None:
        self.view = gold_backtest_view(
            BARS, ACTIONS, CALENDAR, symbol_id=HALT_SYMBOL,
            exchange=_exchange_of(HALT_SYMBOL), snapshot_id=_BUNDLE.snapshot_id,
        )

    def test_backtest_view_has_required_columns(self) -> None:
        self.assertTrue(
            {"ts", "close", "close_adj", "total_return_nav", "traded", "status",
             "available_utc", "symbol_id"} <= set(self.view.columns))

    def test_backtest_view_is_single_snapshot(self) -> None:
        subset = BARS[(BARS["symbol_id"] == HALT_SYMBOL)
                      & (BARS["snapshot_id"] == _BUNDLE.snapshot_id)]
        self.assertEqual(len(self.view), len(subset))

    def test_view_carries_explicit_trade_mask(self) -> None:
        """F.4：成交只能用**真实可交易**事件，故掩码必须显式带出。"""
        self.assertTrue(self.view["traded"].isin([True, False]).all())
        self.assertFalse(self.view.loc[self.view["status"] == "halted", "traded"].any())

    def test_view_available_utc_is_present_and_ordered(self) -> None:
        self.assertTrue(self.view["available_utc"].notna().all())
        self.assertTrue((pd.to_datetime(self.view["available_utc"]) >=
                         pd.to_datetime(self.view["ts"])).all())

    def test_total_return_nav_starts_at_one(self) -> None:
        self.assertAlmostEqual(float(self.view["total_return_nav"].iloc[0]), 1.0, places=12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
