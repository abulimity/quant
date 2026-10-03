"""论文 → `StrategySpec`（LOCAL_DEPLOYMENT_PLAN.md §P5.2）。

**经桥调用 x2 环境**：core 侧不装 x2strategy/litellm，一切经
`bridge.run_in_env("x2", ...)` 走 subprocess + job.json + 结果 JSON。

分工：

    x2 侧（`envs/x2/entry.py`）  —— 跑 paper2spec，产出 **x2strategy 的原始规格**
    core 侧（本文件）             —— 把原始规格**映射到我们的契约** `StrategySpec`

**为什么要映射而不是直接用**：x2strategy 的规格面向「生成 backtrader 代码」，
有 27 个字段（`price_data` / `universe_assets` / `indicators` / `logic_pipeline` …），
与我们的契约（`universe` 是**内部 symbol_id**、`entry`/`exit` 是 **`Expr` 树**）不同构。
直接塞进来会让 P3.2 闸门失去意义 —— 闸门要检查的是**我们**的结构。

**映射是保守的**：能确定映射的才映射；拿不准的**不猜**，记进 `unmapped`。
宁可让规格被闸门拒掉，也不要伪造一份「看起来合规」的规格。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from quantlab.contract.types import (
    ORIGIN_X2STRATEGY,
    DataRequirement,
    Expr,
    F8_SCENARIOS,
    SizingSpec,
    StrategySpec,
)
from quantlab.engines.bridge import run_in_env
from quantlab.x2.llm import LlmConfig, load_llm_config

X2_ENTRY = "envs/x2/entry.py"

# x2strategy 的算子名 → 我们的算子名（保守映射，只收**确定等价**的）
OP_ALIASES: dict[str, str] = {
    "sma": "sma", "moving_average": "sma", "simple_moving_average": "sma",
    "ema": "ema", "exponential_moving_average": "ema",
    "std": "std", "stdev": "std", "rolling_std": "std",
    "momentum": "momentum", "roc": "momentum",
    "shift": "shift", "lag": "lag", "delay": "lag",
}

# 需要「价格字段 + 窗口」两个参数的算子：我们会自动补 shift(1)（§P3.2 G4）
_WINDOW_OPS = frozenset({"sma", "ema", "std", "momentum"})


@dataclass
class Paper2SpecResult:
    spec: StrategySpec | None            # 映射成功的契约规格（可能仍需人工复核）
    raw_spec: dict                       # x2strategy 的原始产出（可追溯）
    source: str                          # 论文来源描述
    model: str                           # 本次使用的模型
    unmapped: list[str] = field(default_factory=list)
    needs_human_review: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "spec": json.loads(self.spec.to_json()) if self.spec else None,
            "raw_spec": self.raw_spec,
            "source": self.source,
            "model": self.model,
            "unmapped": self.unmapped,
            "needs_human_review": self.needs_human_review,
            "notes": self.notes,
        }


def extract_raw_spec(
    paper_path: str | Path,
    *,
    model: str | None = None,
    llm: LlmConfig | None = None,
    title: str = "",
    mode: str = "multilayer",
    workdir: str | Path | None = None,
) -> dict:
    """经桥跑一次 paper2spec，返回 x2 侧的原始结果（不做契约映射）。

    `model` 未显式给出时，会先用 `llm.require()` 取配置里的模型 ——
    未配置就**明确失败**，不静默用 paper2spec 的内置默认模型。
    """
    paper = Path(paper_path)
    if not paper.is_file():
        raise FileNotFoundError(f"论文不存在: {paper}")

    resolved_model = model
    if resolved_model is None:
        resolved_model = (llm or load_llm_config()).require().model_id()

    return run_in_env(
        "x2", X2_ENTRY,
        {"job_id": f"paper2spec-{paper.stem}", "engine": "x2",
         "params": {"op": "paper2spec", "model": resolved_model,
                    "title": title, "mode": mode}},
        inputs={"paper": paper},
        workdir=Path(workdir) if workdir else None,
    )


# --------------------------------------------------------------------------- #
# 映射：x2strategy 原始规格 → 我们的契约
# --------------------------------------------------------------------------- #
def _field(name: str) -> Expr:
    return Expr("field", (name,))


def _as_expr(node) -> tuple[Expr | None, list[str]]:
    """把 x2strategy 的算子描述映射成 `Expr`。返回 (节点, 未映射项)。

    x2strategy 的 `logic_pipeline` 条目形如：

        {"step_id": "S1", "output": "score", "expression": "momentum(close, 63)"}

    注意它把算子写在 **`expression` 字符串**里（不是 `name` 字段）——
    早先只认 `name`/`operator`/`op`，于是这类条目会被**静默映射成 None**
    （所幸有测试断言 `entry is not None` 兜住）。故这里补上 `expression` 分支。
    """
    unmapped: list[str] = []
    if isinstance(node, str):
        return _parse_call(node, unmapped)
    if isinstance(node, dict):
        name = str(node.get("name") or node.get("operator") or node.get("op") or "").strip()
        if name:
            return _build(name, node, unmapped)
        expression = str(node.get("expression") or "").strip()
        if expression:
            return _parse_call(expression, unmapped)
        unmapped.append(f"无法识别的算子描述（无 name/operator/op/expression）: {node!r}")
        return None, unmapped
    unmapped.append(f"无法识别的算子类型 {type(node).__name__}")
    return None, unmapped


def _build(op_name: str, node: dict, unmapped: list[str]) -> tuple[Expr | None, list[str]]:
    mapped = OP_ALIASES.get(op_name.lower())
    if mapped is None:
        unmapped.append(f"未映射的算子 {op_name!r}")
        return None, unmapped

    raw = node.get("params") or node.get("parameters") or node.get("args") or []
    symbol = str(node.get("field") or node.get("symbol") or node.get("column") or "close")

    if isinstance(raw, dict):
        period = raw.get("period") or raw.get("window") or raw.get("n")
        window = int(period) if isinstance(period, (int, float)) else None
    else:
        window = None
        for item in raw:
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                window = int(item)
                break
            if isinstance(item, str) and item.strip().isdigit():
                window = int(item.strip())
                break
            if isinstance(item, str):
                symbol = item

    if mapped in _WINDOW_OPS:
        if window is None:
            unmapped.append(f"算子 {op_name!r} 缺少窗口参数: {node!r}")
            return None, unmapped
        # 我们的口径要求价格字段先 shift ≥ 1（§P3.2 G4），故原语里就带上
        return Expr(mapped, (Expr("shift", (_field(symbol), 1)), window)), unmapped
    if mapped in ("shift", "lag"):
        return Expr(mapped, (_field(symbol), window or 1)), unmapped
    return Expr(mapped, (_field(symbol),)), unmapped


def _parse_call(text: str, unmapped: list[str]) -> tuple[Expr | None, list[str]]:
    """解析形如 `sma(close, 20)` / `close` 的字符串。"""
    text = text.strip()
    if "(" not in text:
        return _field(text or "close"), unmapped
    op = text[: text.index("(")].strip()
    inner = text[text.index("(") + 1: text.rindex(")")]
    mapped = OP_ALIASES.get(op.lower())
    if mapped is None:
        unmapped.append(f"未映射的算子 {op!r}（来自 {text!r}）")
        return None, unmapped
    parts = [p.strip() for p in inner.split(",") if p.strip()]
    if not parts:
        unmapped.append(f"算子 {op!r} 没有参数")
        return None, unmapped
    node = {"name": mapped, "field": parts[0], "params": parts[1:]}
    return _build(mapped, node, unmapped)


def _unwrap_strategy(raw: dict) -> tuple[dict, list[str]]:
    """拆开 paper2spec 的**信封**。

    实测（P5.6 真调云端）真实产出形如：

        {"num_detected": 1, "paper_title": "...", "strategies": [ {…27 字段…} ]}

    而不是单个策略字典。早先只按「单策略字典」处理，于是真实产出会被当成
    「没有 logic_pipeline」→ **静默映射成空 entry**（所幸有测试断言兜住）。
    这里把信封拆开：取第 1 个策略，多于 1 个时**如实记录**（不静默丢掉）。
    """
    strategies = raw.get("strategies")
    if not isinstance(strategies, list) or not strategies:
        return raw, []
    first = strategies[0]
    if not isinstance(first, dict):
        return raw, [f"strategies[0] 不是映射类型: {type(first).__name__}"]
    notes = ([] if len(strategies) == 1 else [
        f"paper2spec 产出 {len(strategies)} 个策略，本平台一次只处理一个 —— "
        f"取第 1 个（paper_title={raw.get('paper_title')!r}）。其余需人工挑选。"])
    return first, notes


def map_to_contract(
    raw: dict,
    *,
    universe: tuple[int, ...] = (),
    source_paper: str = "",
    model: str = "",
) -> Paper2SpecResult:
    """把 x2strategy 的原始规格映射到我们的契约。

    ⚠️ **`universe` 必须由调用方给出**：x2strategy 的 `universe_assets` 是
    **代码/名称**（如 "SPY"），而我们的 `universe` 是**内部 symbol_id**。
    两者之间需要一张映射表（属人工配置），**不做猜测** —— 猜错会静默交易错标的。
    """
    notes: list[str] = []
    raw, unwrap_notes = _unwrap_strategy(raw)      # 拆信封（真实 paper2spec 产出）
    notes.extend(unwrap_notes)
    if not universe:
        notes.append(
            "未提供 universe（内部 symbol_id）—— x2strategy 给的是代码/名称，"
            "需人工配置映射表。此处留空，闸门会因 universe 为空而要求补齐。")

    logic = raw.get("logic_pipeline") or raw.get("logic") or []
    entry, unmapped = (None, [])
    if isinstance(logic, list) and logic:
        entry, unmapped = _as_expr(logic[0])
    elif logic:
        entry, unmapped = _as_expr(logic)
    else:
        notes.append("原始规格里没有 logic_pipeline —— 无法推导入场条件")

    if raw.get("costs") or raw.get("execution_plan"):
        notes.append("原始规格含成本/执行描述，但**不自动采纳** —— "
                     "成本情景须按 F.8 显式选定（否则闸门 G7 会拒）")

    raw_lookback = raw.get("lookback_period")
    lookback = _parse_lookback(raw_lookback) or _infer_lookback(entry)
    if raw_lookback not in (None, "") and _parse_lookback(raw_lookback) == 0:
        notes.append(
            f"lookback_period={raw_lookback!r} 无法解析成数值 —— 改用表达式推断值 "
            f"{lookback}（随后由闸门 G5 校验是否够用）。")
    top_n = _top_n(raw)

    spec = StrategySpec(
        name=str(raw.get("strategy_name") or raw.get("name") or "from-paper").strip()
        or "from-paper",
        universe=tuple(universe),
        entry=entry,
        exit=None,
        sizing=SizingSpec(top_n=top_n),
        costs=F8_SCENARIOS[10],          # **显式**取 F.8 情景，而非裸默认（闸门 G7）
        source_paper=source_paper,
        origin=ORIGIN_X2STRATEGY,
        lookback=lookback,
        data_requirements=(DataRequirement("bars_daily", ("ts", "close", "available_utc")),),
        params={"mapped_from": model} if model else {},
    )
    return Paper2SpecResult(
        spec=spec, raw_spec=raw,
        source=str(raw.get("source", "")), model=model,
        unmapped=unmapped,
        needs_human_review=bool(raw.get("needs_human_review")) or bool(unmapped),
        notes=notes,
    )


def _top_n(raw: dict) -> int:
    sizing = raw.get("position_sizing") or raw.get("sizing") or {}
    if isinstance(sizing, dict):
        for key in ("top_n", "num_assets", "n", "count"):
            value = sizing.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return 3


def _parse_lookback(value) -> int:
    """x2 的 `lookback_period` 可能是**数字**，也可能是字符串（实测出现过
    `"20 and 60 trading days"`）。早先直接 `int(value)` → **崩溃**。
    这里稳健取值：数字直接用；字符串取其中**最大**的整数（窗口应取最长的那个）；
    解析不出则返回 0，由调用方回退到「按表达式推断」。
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else 0
    numbers = [int(n) for n in re.findall(r"\d+", str(value))]
    return max(numbers, default=0)


def _infer_lookback(entry: Expr | None) -> int:
    """从表达式推断所需回看长度（最大窗口 ×2，留出均线预热余量）。"""
    if entry is None:
        return 1
    from quantlab.contract.lint import _max_window
    return max(1, _max_window(entry) * 2)
