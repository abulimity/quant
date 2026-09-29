"""契约类型与校验器（LOCAL_DEPLOYMENT_PLAN.md §P3.1）。

四件东西，全都**引擎无关**（不得 import backtrader / bt / vectorbt / x2strategy）：

    Signals        —— 信号面板：index=交易日, columns=symbol, 取值 ∈ {-1, 0, 1, NaN}
    TargetWeights  —— 目标权重：index=调仓日, columns=symbol, 行和 ≤ 1
    StrategySpec   —— 策略规格（可用 JSON 往返）
    CostModel      —— 成本模型（默认情景见附录 F.8）

设计取舍：
    · 策略表达式用**显式的 `Expr` 树**，不用裸字符串。理由：`lint.py` 要能对
      「算子误用 / 未来函数」做**结构性**判定。字符串表达式只能靠正则猜，会漏。
    · `StrategySpec` 带 `origin`：`lint.py` 的闸门口径依赖它
      （x2strategy 来源一律 fail-closed；手写规格走通用规则，不阻塞 P3/P4）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
ORIGIN_HANDWRITTEN = "handwritten"
ORIGIN_X2STRATEGY = "x2strategy"
ORIGINS: tuple[str, ...] = (ORIGIN_HANDWRITTEN, ORIGIN_X2STRATEGY)

# Signals 的合法取值（NaN 表示「无信号 / 不参与」）
SIGNAL_VALUES: frozenset[float] = frozenset({-1.0, 0.0, 1.0})

# 行和比较用的数值容差：这是**浮点求和**的固有误差（1e-9 量级），
# 不是「放宽容忍度掩盖缺陷」—— 真正的越界依然会被抓住。
ROW_SUM_EPS = 1e-9


class ContractViolation(ValueError):
    """契约被违反 —— 一律 fail-closed，不得吞掉。"""


# --------------------------------------------------------------------------- #
# 表达式树
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Expr:
    """策略表达式节点。`args` 里可嵌套 `Expr`、字段名（str）或数值。

    例：20 日均线上穿 60 日均线

        Expr("cross_above", (
            Expr("sma", (Expr("field", ("close",)), 20)),
            Expr("sma", (Expr("field", ("close",)), 60)),
        ))

    用显式树而非字符串，是为了让 `lint.py` 能做**结构性**判定（§P3.2）。
    """

    op: str
    args: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.op, str) or not self.op:
            raise ContractViolation(f"Expr.op 必须是非空字符串，得到 {self.op!r}")

    def walk(self):
        """深度优先遍历自身与所有嵌套 Expr。"""
        yield self
        for arg in self.args:
            if isinstance(arg, Expr):
                yield from arg.walk()

    def to_dict(self) -> dict:
        return {"op": self.op, "args": [_to_jsonable(a) for a in self.args]}

    @classmethod
    def from_dict(cls, data: dict) -> "Expr":
        return cls(op=data["op"], args=tuple(_from_jsonable(a) for a in data.get("args", ())))


def _to_jsonable(value):
    if isinstance(value, Expr):
        return {"__expr__": value.to_dict()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


def _from_jsonable(value):
    if isinstance(value, dict) and "__expr__" in value:
        return Expr.from_dict(value["__expr__"])
    if isinstance(value, list):
        return [_from_jsonable(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# 数据需求 / 成本 / 仓位
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DataRequirement:
    """规格声明它要读哪张表的哪些字段 —— 闸门据此做**可用性**检查。"""

    dataset: str                                  # bars_daily / fx_rates / macro_series ...
    fields: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"dataset": self.dataset, "fields": list(self.fields)}

    @classmethod
    def from_dict(cls, data: dict) -> "DataRequirement":
        return cls(dataset=data["dataset"], fields=tuple(data.get("fields", ())))


@dataclass(frozen=True)
class CostModel:
    """成本模型（单边，单位：**基点 bps**）。

    附录 F.8：成本情景为单边综合成本 **0 / 10 / 30 bps**，换汇成本**独立配置**；
    无风险收益率默认 0 **并在报告中披露**。
    """

    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    stamp_duty_bps: float = 0.0      # 印花税（多为卖出侧；本平台只做多）
    fx_cost_bps: float = 0.0         # 换汇成本，独立于上面三项
    risk_free_rate: float = 0.0      # 默认 0，须披露
    label: str = "custom"

    @property
    def one_way_bps(self) -> float:
        """单边综合成本（不含换汇 —— 换汇独立配置）。"""
        return self.commission_bps + self.slippage_bps + self.stamp_duty_bps

    def validate(self) -> None:
        for name in ("commission_bps", "slippage_bps", "stamp_duty_bps", "fx_cost_bps"):
            value = getattr(self, name)
            if not np.isfinite(value) or value < 0:
                raise ContractViolation(f"CostModel.{name} 必须是非负有限数，得到 {value!r}")

    def to_dict(self) -> dict:
        return {
            "commission_bps": self.commission_bps, "slippage_bps": self.slippage_bps,
            "stamp_duty_bps": self.stamp_duty_bps, "fx_cost_bps": self.fx_cost_bps,
            "risk_free_rate": self.risk_free_rate, "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CostModel":
        keys = ("commission_bps", "slippage_bps", "stamp_duty_bps",
                "fx_cost_bps", "risk_free_rate", "label")
        return cls(**{k: data[k] for k in keys if k in data})

    @classmethod
    def scenario(cls, one_way_bps: float, *, fx_cost_bps: float = 0.0) -> "CostModel":
        """按 F.8 的「单边综合成本」构造情景（0 / 10 / 30 bps）。"""
        model = cls(commission_bps=float(one_way_bps), fx_cost_bps=float(fx_cost_bps),
                    label=f"{int(one_way_bps)}bps")
        model.validate()
        return model


# F.8 的三个默认情景
F8_SCENARIOS: dict[int, CostModel] = {bps: CostModel.scenario(bps) for bps in (0, 10, 30)}


@dataclass(frozen=True)
class SizingSpec:
    """仓位规则。默认按 F.8：取前 3、等权、不足时按实际数量分配、无合格标的持现金。"""

    method: str = "equal_weight"      # equal_weight / target_weights
    top_n: int = 3
    rebalance: str = "W-MON"          # 周频；信号在周一（F.4 / F.8）
    cash_floor: float = 0.0           # 最少保留现金比例

    def validate(self) -> None:
        if self.method not in ("equal_weight", "target_weights"):
            raise ContractViolation(f"未知 sizing.method: {self.method!r}")
        if not isinstance(self.top_n, int) or isinstance(self.top_n, bool) or self.top_n <= 0:
            raise ContractViolation(f"sizing.top_n 必须是正整数，得到 {self.top_n!r}")
        if not 0.0 <= self.cash_floor < 1.0:
            raise ContractViolation(f"sizing.cash_floor 须在 [0, 1)，得到 {self.cash_floor!r}")

    def to_dict(self) -> dict:
        return {"method": self.method, "top_n": self.top_n,
                "rebalance": self.rebalance, "cash_floor": self.cash_floor}

    @classmethod
    def from_dict(cls, data: dict) -> "SizingSpec":
        keys = ("method", "top_n", "rebalance", "cash_floor")
        return cls(**{k: data[k] for k in keys if k in data})


# --------------------------------------------------------------------------- #
# 策略规格
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StrategySpec:
    """引擎无关的策略规格。**可 JSON 往返**（§P3.1 V1）。"""

    name: str
    universe: tuple[int, ...] = ()            # 内部 symbol_id
    timeframe: str = "1d"
    entry: Expr | None = None
    exit: Expr | None = None
    sizing: SizingSpec = field(default_factory=SizingSpec)
    params: dict[str, float] = field(default_factory=dict)
    costs: CostModel = field(default_factory=CostModel)
    source_paper: str = ""
    origin: str = ORIGIN_HANDWRITTEN
    lookback: int = 1                         # 需要的回看交易日数
    data_requirements: tuple[DataRequirement, ...] = ()

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "universe": list(self.universe),
            "timeframe": self.timeframe,
            "entry": self.entry.to_dict() if self.entry else None,
            "exit": self.exit.to_dict() if self.exit else None,
            "sizing": self.sizing.to_dict(),
            "params": dict(self.params),
            "costs": self.costs.to_dict(),
            "source_paper": self.source_paper,
            "origin": self.origin,
            "lookback": self.lookback,
            "data_requirements": [d.to_dict() for d in self.data_requirements],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StrategySpec":
        return cls(
            name=data["name"],
            universe=tuple(data.get("universe", ())),
            timeframe=data.get("timeframe", "1d"),
            entry=Expr.from_dict(data["entry"]) if data.get("entry") else None,
            exit=Expr.from_dict(data["exit"]) if data.get("exit") else None,
            sizing=SizingSpec.from_dict(data.get("sizing", {})),
            params=dict(data.get("params", {})),
            costs=CostModel.from_dict(data.get("costs", {})),
            source_paper=data.get("source_paper", ""),
            origin=data.get("origin", ORIGIN_HANDWRITTEN),
            lookback=int(data.get("lookback", 1)),
            data_requirements=tuple(
                DataRequirement.from_dict(d) for d in data.get("data_requirements", ())),
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "StrategySpec":
        return cls.from_dict(json.loads(text))

    def with_params(self, **overrides) -> "StrategySpec":
        """派生一份改了参数的规格（**原规格不变**）。"""
        return replace(self, params={**self.params, **overrides})


# --------------------------------------------------------------------------- #
# 校验器
# --------------------------------------------------------------------------- #
def _check_index(frame: pd.DataFrame, what: str) -> pd.DatetimeIndex:
    if not isinstance(frame, pd.DataFrame):
        raise ContractViolation(f"{what} 必须是 DataFrame，得到 {type(frame).__name__}")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ContractViolation(f"{what} 的 index 必须是 DatetimeIndex（交易日），"
                                f"得到 {type(frame.index).__name__}")
    if frame.index.has_duplicates:
        raise ContractViolation(f"{what} 的 index 存在重复日期")
    if not frame.index.is_monotonic_increasing:
        raise ContractViolation(f"{what} 的 index 未按时间升序")
    return frame.index


def validate_signals(signals: pd.DataFrame, *, name: str = "Signals") -> None:
    """校验信号面板：取值必须 ∈ {-1, 0, 1, NaN}（§P3.1 V3）。

    非数值列、以及 `0.5` / `2` 这类「看起来无害」的越界值，**一律拒绝**。
    """
    _check_index(signals, name)

    for column in signals.columns:
        if not pd.api.types.is_numeric_dtype(signals[column]):
            raise ContractViolation(f"{name}[{column!r}] 非数值型: {signals[column].dtype}")

    values = signals.to_numpy(dtype="float64", na_value=np.nan).ravel()
    finite = values[np.isfinite(values)]
    bad = sorted({float(v) for v in finite if float(v) not in SIGNAL_VALUES})
    if bad:
        raise ContractViolation(
            f"{name} 含非法取值 {bad}；只允许 -1.0 / 0.0 / 1.0 或 NaN。\n"
            f"处置：信号表达的是**方向**而非**强度**；连续分数请放进 TargetWeights "
            f"或单独的 score 面板。"
        )


def validate_target_weights(
    weights: pd.DataFrame,
    *,
    name: str = "TargetWeights",
    as_of=None,
    allow_short: bool = False,
    max_row_sum: float = 1.0,
) -> None:
    """校验目标权重（§P3.1 V3）：行和 ≤ 1、非负（默认只做多）、不含未来日期。

    参数：
        as_of       —— 参考时点；给定时 index 中**不得**出现晚于它的调仓日
        allow_short —— 默认 False（F.1：只做多、不加杠杆、不做空）
        max_row_sum —— 行和上限，默认 1.0（余下即为现金）
    """
    index = _check_index(weights, name)

    values = weights.to_numpy(dtype="float64", na_value=np.nan)
    finite = values[np.isfinite(values)]
    if not allow_short and np.any(finite < -ROW_SUM_EPS):
        raise ContractViolation(
            f"{name} 含负权重（最小 {float(finite.min())}）。"
            f"默认只做多；确需做空请显式传 allow_short=True。"
        )

    # 约束的是**总敞口**而非净敞口：允许做空时，`+0.8 / -0.8` 的净和是 0，
    # 但占用的是 1.6 倍资金。用 nansum 会把它当成「没用杠杆」而放过 —— 那是错的。
    row_exposure = (np.nansum(np.abs(values), axis=1) if allow_short
                    else np.nansum(values, axis=1))      # NaN 视作未持有（0）
    over = row_exposure > (max_row_sum + ROW_SUM_EPS)
    if np.any(over):
        when = index[over][0]
        kind = "总敞口" if allow_short else "行和"
        raise ContractViolation(
            f"{name} {kind}超过 {max_row_sum}"
            f"（最大 {float(row_exposure[over].max()):.6f}，如 {when.date()}）。\n"
            f"超过 1 意味着杠杆 —— F.1 明确不加杠杆。"
        )

    if as_of is not None:
        as_of = pd.Timestamp(as_of)
        future = index[index > as_of]
        if len(future):
            raise ContractViolation(
                f"{name} 含未来日期（as_of={as_of.date()}，最早越界 {future[0].date()}）。\n"
                f"处置：未来日期通常是「用了未来信息」的症状，不是笔误 —— 请查数据流。"
            )


def validate_spec(spec: StrategySpec) -> None:
    """规格自身的结构校验（闸门之外的基础检查）。"""
    if not spec.name:
        raise ContractViolation("StrategySpec.name 不能为空")
    if spec.origin not in ORIGINS:
        raise ContractViolation(f"未知 origin={spec.origin!r}；允许 {ORIGINS}")
    if spec.timeframe != "1d":
        raise ContractViolation(f"当前仅支持日频（timeframe='1d'），得到 {spec.timeframe!r}")
    if spec.lookback < 0:
        raise ContractViolation(f"lookback 不能为负，得到 {spec.lookback!r}")
    if len(set(spec.universe)) != len(spec.universe):
        raise ContractViolation(f"universe 含重复 symbol_id: {spec.universe}")
    spec.costs.validate()
    spec.sizing.validate()
