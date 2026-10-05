"""受控 DSL → `StrategySpec`（LOCAL_DEPLOYMENT_PLAN.md §P5.6a「E2E-B」）。

**分工**（用户拍板的设计，见 §P5.6a）：

    prompt 只负责「逼出结构化」→ 让 LLM 产出**受控 DSL**（JSON，`DSL_SCHEMA`）
    parser 只负责「吃掉结构化」→ 本文件是**纯函数**：合法 DSL → `StrategySpec`，
                                         非法 DSL → **明确报错**（fail-closed）

两者谁都不猜自然语言：LLM 一旦输出不在白名单里的结构，parser 直接拒绝。

**为什么这是「语法」不是「语义」**：parser 能保证的只有**结构正确**（算子名合法、
窗口是正整数、二元算子有两个子节点、field 只引用已知字段……）。至于「论文本来说的是
`cross_above`，LLM 却写成了 `gt`」这类**语义偏离**，`gt` 与 `cross_above` 都是白名单
里的合法算子，parser **无法**也**不应**去猜 —— 那必须靠人工对拍 / `needs_human_review`
标记（见 HANDOFF §7.6「规格为真相」：绝不因 `valid=True` 单独放行）。

**算子白名单**与 `contract.emit.evaluate` 的 `_SUPPORTED_OPS` **一一对应，不得超出**：
一个 DSL 算子若这里收下、而 `evaluate` 不认识，就会在发射期崩溃 —— 所以白名单必须
与求值器同步（测试 `tests/test_p5_dsl.py` 钉住这一点）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from quantlab.contract.types import (
    ORIGIN_X2STRATEGY,
    DataRequirement,
    Expr,
    F8_SCENARIOS,
    SizingSpec,
    StrategySpec,
)

# --------------------------------------------------------------------------- #
# 算子白名单（必须与 contract.emit.evaluate._SUPPORTED_OPS 同步）
# --------------------------------------------------------------------------- #
DSL_FIELD_OPS = frozenset({"field"})
DSL_CONST_OPS = frozenset({"const"})
DSL_WINDOW_OPS = frozenset({
    "sma", "rolling_mean", "ema", "std", "momentum",
    "linreg_slope", "linreg_r2", "llt", "zscore", "ewm_std",
})
DSL_SHIFT_OPS = frozenset({"shift", "lag"})
# 算术（二元 add/sub/mul/div + 一元 neg）与数学（log/exp）—— 逐元素算子，无窗口
DSL_ARITHMETIC_OPS = frozenset({"add", "sub", "mul", "div", "neg"})
DSL_MATH_OPS = frozenset({"log", "exp"})
DSL_BINARY_OPS = frozenset({
    "gt", "lt", "ge", "le", "eq",
    "cross_above", "cross_below",
    "and_", "or_",
    "add", "sub", "mul", "div",
})
DSL_UNARY_OPS = frozenset({"not_", "neg", "log", "exp"})
DSL_CROSS_SECTIONAL_OPS = frozenset({"rank", "cross_sectional_rank"})
DSL_TERNARY_OPS = frozenset({"condition"})
DSL_ALL_OPS = (DSL_FIELD_OPS | DSL_CONST_OPS | DSL_WINDOW_OPS
               | DSL_SHIFT_OPS | DSL_ARITHMETIC_OPS | DSL_MATH_OPS
               | DSL_BINARY_OPS | DSL_UNARY_OPS
               | DSL_CROSS_SECTIONAL_OPS | DSL_TERNARY_OPS)

# 价格字段：与 evaluate 一致，当前仅支持 close（复权收盘）。
DSL_PRICE_FIELDS = frozenset({"close", "price"})


class DslParseError(ValueError):
    """DSL 违反契约 —— 一律 fail-closed，不得吞掉或静默降级。"""


@dataclass
class DslResult:
    """DSL → 契约的解析结果（可审计、可 JSON 落盘）。"""

    spec: StrategySpec | None
    dsl: dict                                  # 原始 DSL（可追溯）
    source: str = ""
    model: str = ""
    unmapped: list[str] = field(default_factory=list)
    needs_human_review: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "spec": json.loads(self.spec.to_json()) if self.spec else None,
            "dsl": self.dsl,
            "source": self.source,
            "model": self.model,
            "unmapped": self.unmapped,
            "needs_human_review": self.needs_human_review,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# 节点解析（递归，fail-closed）
# --------------------------------------------------------------------------- #
def _positive_int(value: Any, *, where: str, what: str) -> int:
    """窗口 / 位移必须是「≥1 的整数」（与 lint.G6 / evaluate._window 同口径）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DslParseError(f"{where}: {what} 必须是数值，得到 {value!r}")
    if value < 1 or float(value) != int(value):
        raise DslParseError(f"{where}: {what} 必须是 ≥1 的整数，得到 {value!r}")
    return int(value)


def _field_name(node: dict, where: str) -> str:
    name = node.get("field")
    if not isinstance(name, str) or not name.strip():
        raise DslParseError(f"{where}: 缺少字段名 'field'（字符串）")
    name = name.strip()
    if name not in DSL_PRICE_FIELDS:
        raise DslParseError(
            f"{where}: 字段 {name!r} 不受支持；当前仅 {sorted(DSL_PRICE_FIELDS)}")
    return name


def _parse_field(node: dict, where: str) -> Expr:
    return Expr("field", (_field_name(node, where),))


def _parse_const(node: dict, where: str) -> Expr:
    value = node.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DslParseError(f"{where}: const 需要数值 'value'，得到 {value!r}")
    return Expr("const", (float(value),))


def _parse_window(node: dict, op: str, where: str) -> Expr:
    name = _field_name(node, where)
    # 窗口算子自动滞后 1 期（§P3.2 G4）：LLM 永不手写因果位移。
    shifted = Expr("shift", (Expr("field", (name,)), 1))
    if op == "llt":
        alpha = node.get("alpha")
        if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) \
                or not (0.0 < float(alpha) < 1.0):
            raise DslParseError(
                f"{where}: {op}.alpha 必须是 (0,1) 内的数值，得到 {alpha!r}")
        return Expr(op, (shifted, float(alpha)))
    if op == "ewm_std":
        span = node.get("span")
        if isinstance(span, bool) or not isinstance(span, (int, float)) \
                or float(span) < 1.0:
            raise DslParseError(
                f"{where}: {op}.span 必须是 ≥1 的数值，得到 {span!r}")
        return Expr(op, (shifted, float(span)))
    window = _positive_int(node.get("window"), where=where, what=f"{op}.window")
    return Expr(op, (shifted, window))


def _parse_shift(node: dict, op: str, where: str) -> Expr:
    name = _field_name(node, where)
    n = _positive_int(node.get("n"), where=where, what=f"{op}.n")
    return Expr(op, (Expr("field", (name,)), n))


def _child_args(node: dict, op: str, where: str, arity: int) -> list[Expr]:
    args = node.get("args")
    if not isinstance(args, (list, tuple)) or len(args) != arity:
        raise DslParseError(
            f"{where}: {op} 需要恰好 {arity} 个子节点（在 'args' 数组里），"
            f"得到 {args!r}")
    return [parse_dsl_node(child, where=f"{where}.{op}[{i}]")
            for i, child in enumerate(args)]


def _parse_binary(node: dict, op: str, where: str) -> Expr:
    return Expr(op, tuple(_child_args(node, op, where, 2)))


def _parse_unary(node: dict, op: str, where: str) -> Expr:
    return Expr(op, tuple(_child_args(node, op, where, 1)))


def _parse_cross_sectional(node: dict, op: str, where: str) -> Expr:
    """横截面排名：`(child, ascending=False)`。ascending 必须严格布尔（防 bool("false") 坑）。"""
    child = tuple(_child_args(node, op, where, 1))[0]
    ascending = node.get("ascending", False)
    if not isinstance(ascending, bool):
        raise DslParseError(f"{where}: {op}.ascending 必须是布尔值，得到 {ascending!r}")
    return Expr(op, (child, ascending))


def _parse_ternary(node: dict, op: str, where: str) -> Expr:
    return Expr(op, tuple(_child_args(node, op, where, 3)))


def parse_dsl_node(node: Any, *, where: str = "node") -> Expr:
    """把**一个** DSL 节点转成 `Expr`。任何结构违规都抛 `DslParseError`（fail-closed）。"""
    if not isinstance(node, dict):
        raise DslParseError(f"{where}: 节点必须是对象，得到 {type(node).__name__}")
    op = node.get("op")
    if not isinstance(op, str) or not op.strip():
        raise DslParseError(f"{where}: 节点缺少字符串字段 'op'")
    op = op.strip()
    if op not in DSL_ALL_OPS:
        raise DslParseError(
            f"{where}: 未知算子 {op!r}；白名单: {sorted(DSL_ALL_OPS)}")

    if op in DSL_FIELD_OPS:
        return _parse_field(node, where)
    if op in DSL_CONST_OPS:
        return _parse_const(node, where)
    if op in DSL_WINDOW_OPS:
        return _parse_window(node, op, where)
    if op in DSL_SHIFT_OPS:
        return _parse_shift(node, op, where)
    if op in DSL_BINARY_OPS:
        return _parse_binary(node, op, where)
    if op in DSL_CROSS_SECTIONAL_OPS:
        return _parse_cross_sectional(node, op, where)
    if op in DSL_TERNARY_OPS:
        return _parse_ternary(node, op, where)
    return _parse_unary(node, op, where)


# --------------------------------------------------------------------------- #
# DSL 信封 → StrategySpec
# --------------------------------------------------------------------------- #
def _parse_sizing(raw: Any, where: str) -> SizingSpec:
    if raw is None:
        return SizingSpec(top_n=3)                     # F.8 默认
    if not isinstance(raw, dict):
        raise DslParseError(f"{where}: sizing 必须是对象，得到 {type(raw).__name__}")
    top_n = raw.get("top_n", 3)
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n <= 0:
        raise DslParseError(f"{where}.top_n 必须是正整数，得到 {top_n!r}")
    rebalance = str(raw.get("rebalance") or "W-MON").strip() or "W-MON"
    return SizingSpec(top_n=top_n, rebalance=rebalance)


def _parse_lookback(value: Any, where: str) -> int:
    """lookback 可能是数字，也可能是字符串（如 "20 and 60 trading days"）——
    字符串取其中**最大**的整数（窗口应取最长）。解析不出返回 0，由调用方回退推断。"""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else 0
    numbers = [int(n) for n in re.findall(r"\d+", str(value))]
    return max(numbers, default=0)


def _infer_lookback(entry: Expr | None, exit_expr: Expr | None, ranking: Expr | None = None) -> int:
    from quantlab.contract.lint import _max_window
    return max(1, max(_max_window(entry), _max_window(exit_expr), _max_window(ranking)) * 2)


def dsl_to_spec(
    dsl: dict,
    *,
    universe: tuple[int, ...] = (),
    source_paper: str = "",
    model: str = "",
) -> DslResult:
    """把整份 DSL 解析成 `StrategySpec`。**不跑闸门**（与 `map_to_contract` 同口径，
    闸门由调用方 `lint_spec` 执行 —— 见 §P5.6a「产出仍走 lint_spec 闸门」）。

    ⚠️ `universe` 必须由调用方给出：DSL 里的 `universe_assets` 是代码/名称，
    与内部 `symbol_id` 之间需要人工映射表，**不做猜测**。
    """
    notes: list[str] = []
    if not isinstance(dsl, dict):
        raise DslParseError(f"DSL 顶层必须是 JSON 对象，得到 {type(dsl).__name__}")

    name = str(dsl.get("name") or "from-dsl").strip() or "from-dsl"

    entry_node = dsl.get("entry")
    if entry_node is None:
        raise DslParseError("DSL 缺少 entry（入场条件）—— 拒绝，避免「静默全现金」")
    entry = parse_dsl_node(entry_node, where="entry")

    exit_node = dsl.get("exit")
    exit_expr = parse_dsl_node(exit_node, where="exit") if exit_node else None

    ranking_node = dsl.get("ranking")
    ranking = parse_dsl_node(ranking_node, where="ranking") if ranking_node else None

    sizing = _parse_sizing(dsl.get("sizing"), where="sizing")

    raw_lookback = dsl.get("lookback")
    lookback = _parse_lookback(raw_lookback, "lookback") or _infer_lookback(entry, exit_expr, ranking)
    if raw_lookback not in (None, "") and _parse_lookback(raw_lookback, "lookback") == 0:
        notes.append(f"lookback={raw_lookback!r} 无法解析成数值 —— 改用表达式推断值 "
                     f"{lookback}（随后由闸门 G5 校验是否够用）")

    if dsl.get("universe_assets"):
        notes.append("DSL 的 universe_assets 是代码/名称，需人工映射到内部 symbol_id；"
                     "本次未提供 universe，闸门/发射将无法定位标的。")
    if not universe:
        notes.append("未提供 universe（内部 symbol_id）—— DSL 给的 universe_assets "
                     "是代码/名称，需人工配置映射表。")

    spec = StrategySpec(
        name=name,
        universe=tuple(universe),
        entry=entry,
        exit=exit_expr,
        ranking=ranking,
        sizing=sizing,
        costs=F8_SCENARIOS[10],            # **显式**取 F.8 情景（闸门 G7），非裸默认
        source_paper=source_paper,
        origin=ORIGIN_X2STRATEGY,          # 仍是「LLM 读论文产出」，继承 fail-closed + 算子注意点口径
        lookback=lookback,
        data_requirements=(DataRequirement("bars_daily", ("ts", "close", "available_utc")),),
    )
    return DslResult(
        spec=spec, dsl=dsl,
        source=source_paper, model=model,
        needs_human_review=bool(dsl.get("needs_human_review", False)),
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Prompt 用 schema（交给 x2 环境的 LLM；这里是**单一真相**，经 params 传入）
# --------------------------------------------------------------------------- #
DSL_SCHEMA = """\
输出必须是**单个 JSON 对象**（不要输出任何解释文字，不要代码块围栏）：

{
  "name": "<策略名，简短>",
  "universe_assets": ["<标的代码/名称>"],   // 论文未指定代码则用空数组 []
  "entry": <条件节点>,                        // 必填
  "exit": <条件节点 或 null>,                  // 无明确出场则 null
  "ranking": <数值节点 或 null>,                // 横截面排序分数（越高越优）；纯轮动必填
  "sizing": {"top_n": <正整数, 默认 3>, "rebalance": "<pandas 周期别名, 默认 W-MON>"},
  "lookback": <正整数, 所需最少回看交易日数>,
  "needs_human_review": <true 若论文表述有歧义/拿不准, 否则 false>
}

「条件节点」只能是以下形态之一（op 字段必填）：
  {"op": "field", "field": "close"}                     // 价格字段（只用 close）
  {"op": "const", "value": <数值>}
  {"op": "sma", "field": "close", "window": <正整数>}   // 均线（自动滞后 1 期，勿手写 shift）
  {"op": "rolling_mean", "field": "close", "window": <正整数>}  // 同 sma
  {"op": "ema", "field": "close", "window": <正整数>}
  {"op": "std", "field": "close", "window": <正整数>}
  {"op": "momentum", "field": "close", "window": <正整数>}  // 过去 window 期收益率
  {"op": "linreg_slope", "field": "close", "window": <正整数>}  // 对 log(close) 等权滚动 OLS 斜率
  {"op": "linreg_r2", "field": "close", "window": <正整数>}    // 对 log(close) 等权滚动 OLS 拟合优度 R²
  {"op": "zscore", "field": "close", "window": <正整数>}       // 滚动 (x−mean)/std
  {"op": "llt", "field": "close", "alpha": <(0,1) 浮点, 如 0.10>}  // 低延迟趋势线（二阶滤波）
  {"op": "ewm_std", "field": "close", "span": <≥1 数值>}      // 收益率的指数加权波动率
  {"op": "shift", "field": "close", "n": <正整数>}      // 显式滞后（一般无需手写）
  {"op": "cross_above", "args": [<节点>, <节点>]}        // 左**上穿**右（事件）
  {"op": "cross_below", "args": [<节点>, <节点>]}        // 左**下穿**右（事件）
  {"op": "gt", "args": [<节点>, <节点>]}                 // 左 > 右（连续态）
  {"op": "lt", "args": [<节点>, <节点>]}                 // 左 < 右
  {"op": "ge", "args": [<节点>, <节点>]}                 // 左 >= 右
  {"op": "le", "args": [<节点>, <节点>]}                 // 左 <= 右
  {"op": "eq", "args": [<节点>, <节点>]}                 // 左 == 右
  {"op": "and_", "args": [<节点>, <节点>]}               // 且
  {"op": "or_", "args": [<节点>, <节点>]}                // 或
  {"op": "not_", "args": [<节点>]}                       // 非
  {"op": "add", "args": [<节点>, <节点>]}                 // 左 + 右（逐元素）
  {"op": "sub", "args": [<节点>, <节点>]}                 // 左 − 右
  {"op": "mul", "args": [<节点>, <节点>]}                 // 左 × 右
  {"op": "div", "args": [<节点>, <节点>]}                 // 左 ÷ 右
  {"op": "neg", "args": [<节点>]}                         // 取反
  {"op": "log", "args": [<节点>]}                         // 自然对数
  {"op": "exp", "args": [<节点>]}                         // 指数
  {"op": "rank", "args": [<节点>], "ascending": <bool, 默认 false>}  // 横截面排名（1=最大值）
  {"op": "cross_sectional_rank", "args": [<节点>], "ascending": <bool, 默认 false>}  // 同 rank
  {"op": "condition", "args": [<条件节点>, <节点>, <节点>]}  // 三元：pred 为真取左，否则取右

关键规则：
  · 只使用上面列出的 op，不要造新算子、不要缩写、不要改名。
  · 窗口类算子（sma/rolling_mean/ema/std/momentum/linreg_slope/linreg_r2/zscore/llt/ewm_std）
    **不要**手写 shift —— parser 会自动补。llt 用 alpha、ewm_std 用 span，其余用 window。
  · 「上穿 / 金叉」用 cross_above；「下穿 / 死叉」用 cross_below —— **不要**用 gt/lt 代替。
  · 表达式的每个叶子都必须是 {"op":"field","field":"close"}。
  · args 数组的元素是「节点对象」，不是字符串、不是函数调用式文本。
  · rank/cross_sectional_rank/condition 的字段叶子仍需 shift ≥ 1（窗口算子会自动补 shift）。
  · ranking 是「越高越优」的分数：对「越大越好」的因子用 rank/cross_sectional_rank 时取
    ascending=true（rank 值随因子递增）；对「越小越好」的因子取 ascending=false（rank 1=最大值）。
  · 纯横截面轮动没有时间性入场条件时，用 entry={"op":"const","value":1.0}（"始终合格"），
    再用 ranking 表达排序分数；不要留空 entry（否则闸门 G9 拒绝，会静默全现金）。
"""
