"""Runner 协议与注册表（LOCAL_DEPLOYMENT_PLAN.md §P4.1）。

三个引擎（backtrader / bt / vectorbt）都接到**同一份契约**上：

    weights: TargetWeights  →  run(...)  →  BacktestResult

统一的关键是**成交语义必须显式**，不得沿用任何引擎的默认行为：

    信号在 T 日收盘生成  →  T+1 **开盘价**成交

（口径来源：正文 §P4.5。附录 F.4.4/F.8 写的是「信号后第一个有效交易日的**收盘**」，
两处冲突；手册规定「凡与正文冲突处，以正文为准」，故取 §P4.5 的 T+1 开盘。
该冲突已在 `docs/deploy/EVIDENCE.md` §P4 显式记录。）

`run_meta` 必须含 `env_lock_hash` / `git_sha` / `data_snapshot_id` —— 缺任一项，
这次回测就**不可复现**，结论无意义（§P4.1 V3）。
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

from quantlab.contract.types import CostModel

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class EngineError(RuntimeError):
    """引擎运行失败。"""


class UnknownEngineError(EngineError):
    """请求了未注册的引擎 —— **报错，不静默回退**（§P4.1 V3）。"""


# --------------------------------------------------------------------------- #
# 输入 / 输出
# --------------------------------------------------------------------------- #
@dataclass
class DataBundle:
    """一次回测的全部输入。**只含单一快照**（`snapshot_id` 是必填项）。

    opens / closes —— index=交易日(DatetimeIndex)、columns=symbol_id、值为复权价
    traded         —— 同形状 bool 掩码（停牌/缺失为 False）
    available_utc  —— 同形状，数据可用时间（防未来函数；None 表示未提供）
    """

    opens: pd.DataFrame
    closes: pd.DataFrame
    traded: pd.DataFrame
    snapshot_id: str
    available_utc: pd.DataFrame | None = None
    base_currency: str = "CNY"

    def __post_init__(self) -> None:
        for name in ("opens", "closes", "traded"):
            frame = getattr(self, name)
            if not isinstance(frame, pd.DataFrame):
                raise EngineError(f"DataBundle.{name} 必须是 DataFrame")
            if not isinstance(frame.index, pd.DatetimeIndex):
                raise EngineError(f"DataBundle.{name} 的 index 必须是 DatetimeIndex")
            if not frame.index.is_monotonic_increasing:
                raise EngineError(f"DataBundle.{name} 的 index 必须按时间升序")
        if self.opens.shape != self.closes.shape or self.opens.shape != self.traded.shape:
            raise EngineError("DataBundle 的 opens/closes/traded 形状必须一致")
        if not self.snapshot_id:
            raise EngineError("DataBundle.snapshot_id 不能为空（复现性前提）")
        self.traded = self.traded.astype(bool)

    @property
    def symbols(self) -> list[int]:
        return [int(c) for c in self.closes.columns]

    def fill_session(self, decision_date) -> pd.Timestamp | None:
        """决策日之后**第一个可执行成交的会话**（T+1 语义）。

        返回 None 表示样本内已无后续会话（决策日就是最后一根）。
        """
        later = self.closes.index[self.closes.index > pd.Timestamp(decision_date)]
        return later[0] if len(later) else None

    def subset(self, symbols) -> "DataBundle":
        cols = list(symbols)
        return DataBundle(
            opens=self.opens[cols], closes=self.closes[cols], traded=self.traded[cols],
            snapshot_id=self.snapshot_id,
            available_utc=None if self.available_utc is None else self.available_utc[cols],
            base_currency=self.base_currency,
        )


@dataclass
class BacktestResult:
    equity: pd.Series                 # index=ts, 账户净值（基准货币）
    positions: pd.DataFrame           # index=ts, columns=symbol_id, 持仓**份额/单位数**
    trades: pd.DataFrame              # 逐笔成交（列定义见 execution.py）
    stats: dict = field(default_factory=dict)
    run_meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing = {"env_lock_hash", "git_sha", "data_snapshot_id"} - set(self.run_meta or {})
        if missing:
            raise EngineError(
                f"run_meta 缺少复现性字段 {sorted(missing)} —— 这样的回测结论不可复现，"
                f"视为无效（§P4.1 V3）。")


@runtime_checkable
class BacktestRunner(Protocol):
    engine: str

    def run(self, weights: pd.DataFrame, data: DataBundle, costs: CostModel,
            params: dict) -> BacktestResult:      # pragma: no cover - 协议声明
        ...


# --------------------------------------------------------------------------- #
# 复现性元数据
# --------------------------------------------------------------------------- #
def env_lock_hash(root: Path | None = None) -> str:
    """`uv.lock` 的内容哈希（core 环境）。锁变了，回测结论就必须重跑。"""
    path = (root or PROJECT_ROOT) / "uv.lock"
    if not path.is_file():
        return "NO_LOCK"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_sha(root: Path | None = None) -> str:
    """当前提交 SHA；不在 git 仓库时返回 `NO_GIT`（不抛异常，但会在元数据里显形）。"""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root or PROJECT_ROOT),
                             capture_output=True, text=True, encoding="utf-8", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "NO_GIT"
    sha = out.stdout.strip()
    if out.returncode != 0 or not sha:
        return "NO_GIT"
    return sha + ("-dirty" if _is_dirty(root) else "")


def _is_dirty(root: Path | None) -> bool:
    """工作树有未提交改动时标注 `-dirty` —— 否则「同一 SHA 两次结果不同」无从解释。"""
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(root or PROJECT_ROOT),
                             capture_output=True, text=True, encoding="utf-8", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out.stdout.strip())


def base_run_meta(data: DataBundle, engine: str, **extra) -> dict:
    return {
        "engine": engine,
        "env_lock_hash": env_lock_hash(),
        "git_sha": git_sha(),
        "data_snapshot_id": data.snapshot_id,
        **extra,
    }


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
_RUNNERS: dict[str, BacktestRunner] = {}


def register(runner: BacktestRunner) -> BacktestRunner:
    """注册一个 runner。

    **意图**：防止「同一个引擎名被换成另一套实现」这种静默替换 —— 那会让
    「换了引擎但结果没变」无法察觉。

    **实现**：同名的**同类**实例允许重复注册（幂等）；换**不同类**才报错。
    这样 `register_builtin()` 可以被反复调用（测试常常这么做），
    而真正的实现替换仍会被拦下。
    """
    name = getattr(runner, "engine", None)
    if not name:
        raise EngineError(f"runner {runner!r} 缺少 engine 名称")
    existing = _RUNNERS.get(name)
    if existing is not None and type(existing) is not type(runner):
        raise EngineError(
            f"引擎 {name!r} 已注册为 {type(existing).__name__}，"
            f"拒绝静默替换为 {type(runner).__name__}")
    _RUNNERS[name] = runner
    return runner


def get_runner(name: str) -> BacktestRunner:
    """按名解析 runner。**未知引擎报明确错误，不静默回退到默认引擎**（§P4.1 V3）。"""
    if name not in _RUNNERS:
        raise UnknownEngineError(
            f"未知引擎 {name!r}；已注册: {sorted(_RUNNERS)}。\n"
            f"处置：检查引擎名拼写，或确认该 runner 已注册。"
            f"**不会**回退到其它引擎 —— 静默回退会让「换了引擎但结果没变」无法察觉。")
    return _RUNNERS[name]


def available_engines() -> list[str]:
    return sorted(_RUNNERS)


def reset_registry() -> None:
    """仅供测试：清空注册表。"""
    _RUNNERS.clear()


def register_builtin() -> list[str]:
    """注册内置 runner（backtrader / bt / reference）。返回已注册的名字。

    `reference` 是**语义真值**（oracle），不是第四个业务引擎：它按 §P4.5 的统一口径
    直接模拟，用来在偏差超容差时逐层定位（信号时点 → 成交价 → 成本计提 → 舍入）。
    """
    from quantlab.engines import bt_runner, reference

    register(reference.ReferenceRunner())
    register(bt_runner.BtRunner())
    from quantlab.engines import backtrader_runner
    register(backtrader_runner.BacktraderRunner())
    return available_engines()


def load_bundle_from_fixture(root: str | Path | None = None,
                             snapshot_id: str | None = None) -> DataBundle:
    """从 P2 的夹具快照构造 DataBundle（对拍与单引擎验收的公共输入）。

    取**复权收盘**做估值与信号、**复权开盘**做成交价（T+1 开盘）；
    `traded` 直接来自夹具的显式交易掩码。
    """
    from quantlab.fixtures import spec as S
    from quantlab.fixtures.synth import read_snapshot, snapshot_dir
    from quantlab.quality.clean import adjust_prices

    sid = snapshot_id or S.snapshot_id()
    directory = snapshot_dir(sid, root or (PROJECT_ROOT / "data" / "bronze" / "synthetic"))
    bundle = read_snapshot(sid, directory.parent)
    bars, actions = bundle.tables["bars_daily"], bundle.tables["corporate_actions"]

    adjusted = pd.concat(
        [adjust_prices(bars[bars["symbol_id"] == symbol],
                       actions[actions["symbol_id"] == symbol])
         for symbol in sorted(bars["symbol_id"].unique())],
        ignore_index=True)

    # 夹具的 ts 是 datetime.date；pivot 后必须显式转成 DatetimeIndex，
    # 否则后续所有基于索引的时点比较（T+1、reindex）都会走偏。
    def _pivot(values: str) -> pd.DataFrame:
        frame = adjusted.pivot(index="ts", columns="symbol_id", values=values)
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index), name="ts")
        return frame.sort_index()

    opens = _pivot("open_adj")
    closes = _pivot("close_adj")
    traded = _pivot("traded").fillna(False).astype(bool)
    keep = traded.any(axis=1)          # 至少有一只可交易的会话才纳入
    return DataBundle(opens=opens[keep], closes=closes[keep], traded=traded[keep],
                      snapshot_id=sid, base_currency=S.BASE_CURRENCY)
