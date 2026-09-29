"""合成夹具的**声明式规格**：种子、标的、场景与已知答案口径，全部写在这里。

设计要点（决定 V4「同种子两次跑内容哈希一致」能否成立）：
    · 一切随机性来自 `SEED` + `symbol_id` 派生的 `numpy.random.Generator`；
      没有 `time` / `os.urandom` / 哈希随机化参与，故**同机同版本必然逐位一致**。
    · **总收益指数 `TRI` 是原语**（闭式：日收益 g_t 的累乘），原始价格由**精确恒等式**导出：

          close_raw[t] = g_t * close_raw[t-1] / r_t - d_t

      其中 `r_t` 为当日拆分比例（无则 1），`d_t` 为当日每股现金分红（无则 0）。
      该恒等式正是「1 + 总收益 = 本币价格收益 + 分红再投资」的实现，故
      buy&hold 解析净值 `nav[t] = TRI[t]/TRI[0]` **可手算复现**，也能由夹具的
      原始价格 + 公司行动**独立重算**出来（P2.2 V3 的断言）。

    · 场景常量是**声明**，不是运行时巧合：分红/拆分/停牌/退市/晚上市/失败窗都写死在下面。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date

SEED = 20240101
SYNTH_VERSION = "v1"

# 研究窗口（F.1「最近 10 年」）
STUDY_START = date(2015, 1, 1)
STUDY_END = date(2024, 12, 31)

# 默认基准货币（F.1：多基准并存，默认 CNY）
BASE_CURRENCY = "CNY"


# --------------------------------------------------------------------------- #
# 标的
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SymbolSpec:
    symbol_id: int
    ticker: str
    exchange: str          # XSHG / XHKG / XNYS
    currency: str          # 原币
    listed_on: date
    delisted_on: date | None
    init_price: float      # 首根 bar 的原始收盘价
    mu_daily: float        # 日对数漂移（声明值，非随机）
    sigma_daily: float     # 日对数波动（声明值，非随机）
    lot_size: int
    role: str              # 场景标签，用于可读性与证据

    @property
    def calendar(self) -> str:
        return self.exchange


# 9 只标的覆盖 P2.2 要求的全部场景
SYMBOLS: tuple[SymbolSpec, ...] = (
    SymbolSpec(1, "SYN-CN-A", "XSHG", "CNY", date(2010, 1, 4), None,
               100.0, 0.00020, 0.0120, 100, "常规 + 一次分红"),
    SymbolSpec(2, "SYN-CN-B", "XSHG", "CNY", date(2010, 1, 4), None,
               40.0, 0.00025, 0.0150, 100, "常规 + 一次拆分"),
    SymbolSpec(3, "SYN-CN-C", "XSHG", "CNY", date(2010, 1, 4), None,
               25.0, 0.00015, 0.0110, 100, "常规 + 一段停牌"),
    SymbolSpec(4, "SYN-CN-D", "XSHG", "CNY", date(2010, 1, 4), date(2018, 12, 28),
               60.0, 0.00010, 0.0130, 100, "退市标的"),
    SymbolSpec(5, "SYN-CN-E", "XSHG", "CNY", date(2022, 1, 10), None,
               18.0, 0.00030, 0.0160, 100, "晚上市标的"),
    SymbolSpec(6, "SYN-HK-A", "XHKG", "HKD", date(2010, 1, 4), None,
               30.0, 0.00018, 0.0125, 500, "常规 + 分红 + 一段下载失败"),
    SymbolSpec(7, "SYN-HK-B", "XHKG", "HKD", date(2010, 1, 4), None,
               45.0, 0.00012, 0.0105, 500, "常规"),
    SymbolSpec(8, "SYN-US-A", "XNYS", "USD", date(2010, 1, 4), None,
               50.0, 0.00030, 0.0100, 1, "常规 + 一次分红"),
    SymbolSpec(9, "SYN-US-B", "XNYS", "USD", date(2010, 1, 4), None,
               75.0, 0.00022, 0.0095, 1, "常规"),
)

SYMBOLS_BY_ID: dict[int, SymbolSpec] = {s.symbol_id: s for s in SYMBOLS}

# 基准成交量（乘上确定性噪声后作为 volume）
BASE_VOLUME = 1_000_000.0


# --------------------------------------------------------------------------- #
# 公司行动（已知答案：除权日与金额/比例写死）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DividendSpec:
    symbol_id: int
    ex_date: date
    cash: float                  # 每股现金（原币）
    announce_lag_days: int = 10  # 公告早于除权的自然日数（信息可得时间）


@dataclass(frozen=True)
class SplitSpec:
    symbol_id: int
    ex_date: date
    ratio: float                 # 1 股拆成 ratio 股
    announce_lag_days: int = 20


DIVIDENDS: tuple[DividendSpec, ...] = (
    DividendSpec(1, date(2019, 6, 14), 0.35),
    DividendSpec(6, date(2018, 5, 18), 1.20),
    DividendSpec(8, date(2017, 11, 15), 0.85),
)

SPLITS: tuple[SplitSpec, ...] = (
    SplitSpec(2, date(2020, 9, 11), 4.0),
)


# --------------------------------------------------------------------------- #
# 停牌 / 下载失败（闭区间，按各自日历的**交易日**展开）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WindowSpec:
    symbol_id: int
    start: date
    end: date
    reason: str


SUSPENSIONS: tuple[WindowSpec, ...] = (
    WindowSpec(3, date(2021, 3, 1), date(2021, 3, 19), "regulatory_halt"),
)

# 「下载失败」是我们**没有取到**数据，而不是市场没开 —— 必须与停牌区分
FAILURES: tuple[WindowSpec, ...] = (
    WindowSpec(6, date(2023, 7, 3), date(2023, 7, 7), "vendor_timeout"),
)


# --------------------------------------------------------------------------- #
# 汇率（内部统一：1 base = rate quote）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FxSpec:
    base: str
    quote: str
    init_rate: float             # 初始：1 base = init_rate quote
    annual_drift: float          # 年化对数漂移（声明方向）
    sigma_daily: float
    availability_lag_days: int   # F.6：缺发布时点时采用**保守滞后一日**


FX_SERIES: tuple[FxSpec, ...] = (
    FxSpec("USD", "CNY", 7.1000, 0.010, 0.0030, 1),
    FxSpec("USD", "HKD", 7.8000, -0.002, 0.0010, 1),
)
# 派生对：HKD/CNY = (USD/CNY) / (USD/HKD)，保证三对内部自洽
FX_DERIVED: tuple[tuple[str, str], ...] = (("HKD", "CNY"),)

# 说明：夹具**刻意不内置**「反向报价」。
# 理由：若同一张表里同时存在 USD/CNY 与 CNY/USD，它们会互为倒数、**内部自洽**，
# 于是既无法触发方向检查，归一化时还会把两列折叠成同一个 (base,quote) 序列而**产生重复**。
# 按 §P2.6 V3 的措辞，反向输入应当由**测试构造**（「构造反向输入，断言被正确取倒数并留记录」），
# 而不是混进夹具当作真实数据。故这里只保留方向正确的三对。


# --------------------------------------------------------------------------- #
# 宏观（一等数据，与行情同级）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MacroSpec:
    series_id: str
    unit: str
    init_value: float
    annual_drift: float
    sigma: float
    release_lag_days: int   # 观测期结束后多少自然日才发布（**声明假设**）


MACRO_SERIES: tuple[MacroSpec, ...] = (
    MacroSpec("SYNTH_CPI_YOY", "percent", 2.10, 0.05, 0.15, 15),
    MacroSpec("SYNTH_POLICY_RATE", "percent", 3.00, -0.02, 0.05, 30),
)


# --------------------------------------------------------------------------- #
# 数据可用时间缓冲（**声明假设**，非真实发布时刻）
# --------------------------------------------------------------------------- #
# F.2：「免费历史回填数据没有『当时何时发布』的完整记录，必须显式记录假设的
#       发布时间缓冲；不得称为严格的 point-in-time 数据。」
AVAILABILITY_BUFFER_MINUTES: dict[str, int] = {
    "XSHG": 30,
    "XHKG": 30,
    "XNYS": 15,
}


def spec_fingerprint() -> str:
    """规格指纹：规格变了快照 ID 就变；只依赖规格（不依赖数据），故无循环依赖。"""
    payload = {
        "version": SYNTH_VERSION,
        "seed": SEED,
        "study_start": STUDY_START.isoformat(),
        "study_end": STUDY_END.isoformat(),
        "base_currency": BASE_CURRENCY,
        "symbols": [
            asdict(s) | {"listed_on": s.listed_on.isoformat(),
                         "delisted_on": s.delisted_on.isoformat() if s.delisted_on else None}
            for s in SYMBOLS
        ],
        "dividends": [asdict(d) | {"ex_date": d.ex_date.isoformat()} for d in DIVIDENDS],
        "splits": [asdict(s) | {"ex_date": s.ex_date.isoformat()} for s in SPLITS],
        "suspensions": [asdict(w) | {"start": w.start.isoformat(), "end": w.end.isoformat()}
                        for w in SUSPENSIONS],
        "failures": [asdict(w) | {"start": w.start.isoformat(), "end": w.end.isoformat()}
                     for w in FAILURES],
        "fx": [asdict(f) for f in FX_SERIES],
        # 派生对/反向对**必须**进指纹：它们改变 fx_rates 的内容。漏掉就会出现
        # 「内容变了但快照 ID 没变」—— 幂等检查会误判为「已存在」而跳过重写，
        # 于是磁盘上留着与当前代码不符的陈旧快照。这类漏项是静默的，极难察觉。
        "fx_derived": [list(pair) for pair in FX_DERIVED],
        "macro": [asdict(m) for m in MACRO_SERIES],
        "availability_buffer_minutes": AVAILABILITY_BUFFER_MINUTES,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def snapshot_id() -> str:
    """确定性快照 ID（不含时钟，故可重复生成同名快照）。"""
    return f"synth-{SYNTH_VERSION}-{spec_fingerprint()[:12]}"
