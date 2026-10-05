"""规格校验闸门（LOCAL_DEPLOYMENT_PLAN.md §P3.2）。

**目的**：把「未来函数 / 算子误用」检测变成**入库强制步骤**，而不是事后的自觉。

两类规则，**独立启用、不得混为一谈**（§P3.2）：

    1. **通用规则**（P3 起即生效）—— 本文件实现：
       G1 dataset_known          引用的数据集必须在契约里
       G2 fields_exist           声明的字段必须存在
       G3 availability_declared  读行情就必须声明 available_utc（否则无从防未来函数）
       G4 no_lookahead           **结构性**判定：价格字段必须经 shift/lag ≥ 1
       G5 lookback_sufficient    lookback ≥ 表达式里用到的最大窗口
       G6 windows_positive_int   窗口参数必须是正整数
       G7 cost_model_declared    必须**显式**选一个成本情景（不得沿用裸默认）
       G8 symbols_listed         universe 里的标的在决策时点必须已上市（需注入 listing_dates）
       G9 entry_present          必须有入场条件（否则静默全现金）
    2. **x2strategy 规则**（P5 接通后生效）—— 调用其 operator pitfall 检测。

**闸门口径（§P3.2，避免 P3 自锁）**：

    · 对 `origin == x2strategy` 的规格一律 **fail-closed** —— x2 规则未接通时
      **不得入库**，而不是「没规则就放行」。
    · P3/P4 用于对拍与契约测试的**手写规格**走通用规则即可，须显式标记
      `origin=handwritten`，从而不阻塞 P3、P4。
    · **不得**为让手工规格过关而放宽通用规则；`origin` 写入 run 元数据，可审计。

**G4 为什么能被自动化**（本文件最关键的一条）：F.4 规定信号在**周一 09:00 北京时间**
产生，此时内地与香港**当日尚未收盘**。所以「读到当日 close」在结构上就是未来函数。
故判据不是猜意图，而是：**每个价格字段叶子节点都必须有一个 `shift`/`lag` 祖先且 n ≥ 1**。
把这条写进 AST 遍历即可，不依赖人对表达式的理解。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from quantlab.contract.types import (
    ORIGIN_X2STRATEGY,
    ContractViolation,
    Expr,
    StrategySpec,
)

# ---- 规则中引用的常量 ----
PRICE_FIELDS: frozenset[str] = frozenset({"open", "high", "low", "close", "volume"})
SHIFT_OPS: frozenset[str] = frozenset({"shift", "lag"})
# 三类「回看长度」算子（G5 统一按有效窗口覆盖；G6 逐类校验参数）：
#   WINDOW_OPS —— 整数窗口（window ≥ 1 的整数）
#   SPAN_OPS   —— span（≥ 1 的数值，可为浮点）
#   ALPHA_OPS  —— alpha（(0,1) 内的浮点，IIR 滤波）
WINDOW_OPS: frozenset[str] = frozenset({
    "sma", "ema", "rolling_mean", "momentum", "std",
    "linreg_slope", "linreg_r2", "zscore",
})
SPAN_OPS: frozenset[str] = frozenset({"ewm_std"})
ALPHA_OPS: frozenset[str] = frozenset({"llt"})
LOOKBACK_OPS: frozenset[str] = WINDOW_OPS | SPAN_OPS | ALPHA_OPS

# 读行情就必须声明可用时间，否则「防未来函数」无从谈起
AVAILABILITY_FIELDS: dict[str, str] = {
    "bars_daily": "available_utc",
    "fx_rates": "available_utc",
    "macro_series": "available_utc",
    "fundamentals": "available_utc",
}

# x2strategy 的算子 pitfall 检测已接通（P5.2）。置 False 仅用于回归测试。
X2_RULES_AVAILABLE = True

# x2 规则的**严重度**：默认 "note"（咨询性）。
#
# 为什么不是 "error"（**重要取舍，经人工确认**）：
#   1. 它命中的是「你用了这些**数值算子**，注意其数值稳定性陷阱」，
#      与「未来函数」是**两回事** —— 后者由 G4 结构性拦截，更可靠；
#   2. 内置语料仅 4 条，且靠**措辞相似度**召回 —— 既覆盖不全，又可能误拦
#      （措辞像但实际无关）。设成 error 会**永久阻断**几乎所有 x2 规格；
#   3. 故它作为「算子注意点」**随规格展示/存档**，供人工判断，不阻断入库。
# 若将来语料扩充、召回质量经评估可靠，可改为 "error"。
X2_RULE_SEVERITY = "note"


@dataclass
class LintFinding:
    rule: str
    severity: str          # 'error' | 'warning' | 'info'
    message: str
    location: str = ""

    def __str__(self) -> str:
        where = f" @{self.location}" if self.location else ""
        return f"[{self.severity.upper()}] {self.rule}{where}: {self.message}"


@dataclass
class LintReport:
    spec_name: str
    origin: str
    findings: list[LintFinding] = field(default_factory=list)
    rules_run: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def passed(self) -> bool:
        """**fail-closed**：只要有 error 就不通过；warnings 不阻塞但须留痕。"""
        return not self.errors

    def raise_if_failed(self) -> None:
        if not self.passed:
            detail = "\n".join(f"  - {f}" for f in self.errors)
            raise ContractViolation(
                f"规格 {self.spec_name!r} 未通过闸门（origin={self.origin}）：\n{detail}\n"
                f"处置：修规格或修规则。**不得**加 --skip-lint 旁路。"
            )

    def run_metadata(self) -> dict:
        """写入 run 元数据用（§P3.2：`origin` 必须可审计）。"""
        return {
            "spec_name": self.spec_name,
            "origin": self.origin,
            "rules_run": list(self.rules_run),
            "passed": self.passed,
            "errors": [str(f) for f in self.errors],
            "warnings": [str(f) for f in self.findings if f.severity == "warning"],
        }


# --------------------------------------------------------------------------- #
# 通用规则
# --------------------------------------------------------------------------- #
def _rule_dataset_known(spec: StrategySpec, report: LintReport) -> None:
    from quantlab.ingest.base import CONTRACT

    for req in spec.data_requirements:
        if req.dataset not in CONTRACT:
            report.findings.append(LintFinding(
                "G1.dataset_known", "error",
                f"引用了未知数据集 {req.dataset!r}；已知: {sorted(CONTRACT)}",
                location=f"data_requirements[{req.dataset}]"))


def _rule_fields_exist(spec: StrategySpec, report: LintReport) -> None:
    from quantlab.ingest.base import CONTRACT

    for req in spec.data_requirements:
        known = CONTRACT.get(req.dataset)
        if known is None:
            continue                       # G1 已报
        unknown = [f for f in req.fields if f not in known]
        if unknown:
            report.findings.append(LintFinding(
                "G2.fields_exist", "error",
                f"{req.dataset} 不存在字段 {unknown}；可用: {list(known)}",
                location=f"data_requirements[{req.dataset}]"))


def _rule_availability_declared(spec: StrategySpec, report: LintReport) -> None:
    for req in spec.data_requirements:
        needed = AVAILABILITY_FIELDS.get(req.dataset)
        if needed and needed not in req.fields:
            report.findings.append(LintFinding(
                "G3.availability_declared", "error",
                f"{req.dataset} 的数据需求未声明 {needed}。"
                f"没有可用时间就无法判断「信号产生时该数据是否已经可得」，"
                f"未来函数将无从防止。",
                location=f"data_requirements[{req.dataset}]"))


def _rule_no_lookahead(spec: StrategySpec, report: LintReport) -> None:
    """**结构性**未来函数检测：价格字段必须经 shift/lag ≥ 1。

    依据 F.4：信号在周一 09:00（北京时间）产生时，内地与香港**当日尚未收盘**，
    因此「读到当日 close」在结构上就是未来函数。
    """
    for label, expr in (("entry", spec.entry), ("exit", spec.exit), ("ranking", spec.ranking)):
        if expr is None:
            continue
        report.findings.extend(_scan_lookahead(expr, label))


def _scan_lookahead(expr: Expr, path: str, shifted: bool = False) -> list[LintFinding]:
    findings: list[LintFinding] = []

    if expr.op == "field":
        name = expr.args[0] if expr.args else ""
        if name in PRICE_FIELDS and not shifted:
            findings.append(LintFinding(
                "G4.no_lookahead", "error",
                f"字段 {name!r} 未经 shift/lag ≥ 1 就被使用 —— 这是未来函数。\n"
                f"    依据 F.4：信号在周一 09:00（北京时间）产生时当日尚未收盘，"
                f"当日 close 在该时刻**不可得**。\n"
                f"    处置：用 Expr('shift', (Expr('field', ('{name}',)), 1)) 包一层。",
                location=path))
        return findings

    if expr.op in SHIFT_OPS:
        n = expr.args[1] if len(expr.args) > 1 else 1
        now_shifted = shifted or (isinstance(n, (int, float)) and n >= 1)
        child = expr.args[0] if expr.args else None
        if isinstance(child, Expr):
            findings += _scan_lookahead(child, f"{path}.shift({n})", now_shifted)
        return findings

    for index, arg in enumerate(expr.args):
        if isinstance(arg, Expr):
            findings += _scan_lookahead(arg, f"{path}.{expr.op}[{index}]", shifted)
    return findings


def _effective_window(node: Expr) -> int:
    """算子的「有效回看长度」（G5 口径）。

    整数窗口算子直接用 window；`ewm_std` 用 span；`llt` 是无限冲激响应，
    用 2/α 近似其有效回看（α=0.10 → 20 期）。非法参数返回 0（由 G6 负责报错）。
    """
    value = node.args[1] if len(node.args) >= 2 else 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if node.op in ALPHA_OPS:
        return int(round(2.0 / float(value))) if 0.0 < float(value) < 1.0 else 0
    return int(value)


def _max_window(expr: Expr | None) -> int:
    """表达式里用到的最大有效窗口（窗口/span/alpha 三类算子统一换算）。"""
    if expr is None:
        return 0
    best = 0
    for node in expr.walk():
        if node.op in LOOKBACK_OPS:
            best = max(best, _effective_window(node))
    return best


def _rule_lookback_sufficient(spec: StrategySpec, report: LintReport) -> None:
    needed = max(_max_window(spec.entry), _max_window(spec.exit), _max_window(spec.ranking))
    if spec.lookback < needed:
        report.findings.append(LintFinding(
            "G5.lookback_sufficient", "error",
            f"lookback={spec.lookback} 小于表达式所需的最大窗口 {needed}；"
            f"回看期不足会静默产出前若干根的垃圾信号。",
            location="lookback"))


def _rule_windows_positive_int(spec: StrategySpec, report: LintReport) -> None:
    for label, expr in (("entry", spec.entry), ("exit", spec.exit), ("ranking", spec.ranking)):
        if expr is None:
            continue
        for node in expr.walk():
            if node.op not in LOOKBACK_OPS or len(node.args) < 2:
                continue
            value = node.args[1]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                report.findings.append(LintFinding(
                    "G6.windows_positive_int", "error",
                    f"{node.op} 的窗口参数必须是数值，得到 {value!r}", location=label))
            elif node.op in ALPHA_OPS:
                if not (0.0 < float(value) < 1.0):
                    report.findings.append(LintFinding(
                        "G6.windows_positive_int", "error",
                        f"{node.op} 的 alpha 必须是 (0,1) 内的数值，得到 {value!r}",
                        location=label))
            elif node.op in SPAN_OPS:
                if float(value) < 1.0:
                    report.findings.append(LintFinding(
                        "G6.windows_positive_int", "error",
                        f"{node.op} 的 span 必须是 ≥1 的数值，得到 {value!r}",
                        location=label))
            elif value < 1 or float(value) != int(value):
                report.findings.append(LintFinding(
                    "G6.windows_positive_int", "error",
                    f"{node.op} 的窗口参数必须是 ≥1 的整数，得到 {value!r}", location=label))


def _rule_cost_model_declared(spec: StrategySpec, report: LintReport) -> None:
    """必须**显式**选定成本情景。

    裸默认 `CostModel()` 是「四项成本全 0 + label=custom」，等价于**默认零成本** ——
    回测会系统性偏乐观。故闸门拒绝它，强制作者写 `CostModel.scenario(0/10/30)`；
    显式选 0 bps 是**合法**的（label 会是 "0bps"），区别在于「想过」还是「忘了」。
    """
    from quantlab.contract.types import CostModel

    try:
        spec.costs.validate()
    except ContractViolation as exc:
        report.findings.append(LintFinding("G7.cost_model_declared", "error", str(exc),
                                           location="costs"))
        return

    if spec.costs == CostModel():
        report.findings.append(LintFinding(
            "G7.cost_model_declared", "error",
            "成本模型未显式指定（仍是裸默认：四项成本全 0）。"
            "默认零成本会让回测系统性偏乐观。\n"
            "    处置：用 CostModel.scenario(0/10/30) 显式选定（F.8 的三个情景）。",
            location="costs"))
        return

    report.findings.append(LintFinding(
        "G7.cost_model_declared", "info",
        f"成本情景 label={spec.costs.label!r} 单边综合 "
        f"{spec.costs.one_way_bps} bps，换汇 {spec.costs.fx_cost_bps} bps，"
        f"无风险利率 {spec.costs.risk_free_rate}（须在报告中披露）",
        location="costs"))


def _rule_symbols_listed(spec: StrategySpec, report: LintReport, *,
                         listing_dates: dict[int, object] | None,
                         as_of: object | None) -> None:
    """G8：universe 里的标的必须在**决策时点**已经上市。

    依据 F.7：ETF **上市前没有持仓和收益**；「上市时间与有效历史长度**分别检查**，
    不能只用首条下载数据推断真实上市日」。

    `listing_dates` 由调用方注入（内部 symbol_id → 上市日），使契约层**不依赖**
    任何具体数据源。未注入时本规则**跳过**（并如实标注，不得当作通过）。
    """
    if listing_dates is None or as_of is None:
        report.findings.append(LintFinding(
            "G8.symbols_listed", "info",
            "未提供 listing_dates/as_of → 本规则**跳过**（非通过）", location="universe"))
        return

    reference = pd.Timestamp(as_of)
    for symbol in spec.universe:
        listed = listing_dates.get(symbol)
        if listed is None:
            report.findings.append(LintFinding(
                "G8.symbols_listed", "error",
                f"标的 {symbol} 无上市日记录，无法确认其在 {reference.date()} 是否可交易。",
                location=f"universe[{symbol}]"))
        elif pd.Timestamp(listed) > reference:
            report.findings.append(LintFinding(
                "G8.symbols_listed", "error",
                f"标的 {symbol} 上市日 {pd.Timestamp(listed).date()} "
                f"晚于决策时点 {reference.date()} —— 上市前没有行情与收益，不得入选。",
                location=f"universe[{symbol}]"))


def _rule_entry_present(spec: StrategySpec, report: LintReport) -> None:
    """G9：必须有**入场条件**。

    没有 `entry` 时，`emit_weights` 的资格筛选（`state == 1`）**恒为空** →
    目标权重**恒为 0** → 策略**永远空仓**：回测跑得出来、不崩溃、不报错，
    收益恒为 0。这正是 P3 已经中过一次的「**静默全现金**」类缺陷，
    故在闸门处 fail-closed。

    ⚠️ 实测触发路径（P5.6 端到端）：`paper2spec` 的真实产出若映射不出入口条件
    （`entry=None`），闸门**竟然放行** —— 见 `EVIDENCE.md` §P5.6。

    注：若将来引入「不需要入场条件、直接给权重面板」的 sizing 方法，
    应在此**按 `spec.sizing.method` 分支放宽** —— 而不是把本规则删掉。
    """
    if spec.entry is None:
        report.findings.append(LintFinding(
            "G9.entry_present", "error",
            "规格没有 entry（入场条件）→ 发射器产出的权重恒为 0，"
            "策略会**静默全现金**（回测跑得出、收益恒 0、**不报错**）。\n"
            "    处置：补一个入场表达式；若确实要「不交易」，请显式说明而不是留空。",
            location="entry"))


GENERIC_RULES = (
    _rule_dataset_known,
    _rule_fields_exist,
    _rule_availability_declared,
    _rule_no_lookahead,
    _rule_lookback_sufficient,
    _rule_windows_positive_int,
    _rule_cost_model_declared,
    _rule_entry_present,
)


# --------------------------------------------------------------------------- #
# 闸门
# --------------------------------------------------------------------------- #
def lint_spec(
    spec: StrategySpec,
    *,
    x2_rules_available: bool | None = None,
    listing_dates: dict[int, object] | None = None,
    as_of: object | None = None,
    operator_notes: list[str] | None = None,
) -> LintReport:
    """跑闸门，返回报告。**不抛异常**（调用方决定是否 `raise_if_failed`）。

    参数：
        x2_rules_available —— 覆盖 `X2_RULES_AVAILABLE`（测试用；生产走模块常量）
        listing_dates      —— 内部 symbol_id → 上市日；由调用方注入，
                              使契约层不依赖具体数据源（未注入则 G8 跳过）
        as_of              —— 决策时点，配合 `listing_dates` 做上市日检查
    """
    available = X2_RULES_AVAILABLE if x2_rules_available is None else x2_rules_available
    report = LintReport(spec_name=spec.name, origin=spec.origin)
    report.rules_run.append("generic")

    for rule in GENERIC_RULES:
        rule(spec, report)
    _rule_symbols_listed(spec, report, listing_dates=listing_dates, as_of=as_of)

    # x2strategy 来源：x2 规则未接通时**一律阻断**（fail-closed），
    # 而不是「没有规则就放行」。
    if spec.origin == ORIGIN_X2STRATEGY:
        report.rules_run.append("x2strategy")
        if not available:
            report.findings.append(LintFinding(
                "X2.rules_unavailable", "error",
                "规格来源为 x2strategy，但 x2strategy 的算子 pitfall 检测**尚未接通**。\n"
                "    fail-closed：此类规格在 x2 规则可用之前**不得入库**，"
                "而不是默认放行。\n"
                "    处置：接通后重跑；P3/P4 的对拍请改用手写规格 "
                "并标记 origin='handwritten'。",
                location="origin"))
        elif operator_notes:
            report.findings.append(LintFinding(
                f"X2.operator_notes[{len(operator_notes)}]",
                X2_RULE_SEVERITY,
                "x2strategy 算子误用检索命中了以下**数值算子**注意点。\n"
                "    这是**咨询性**提示，**不阻断入库** —— 它讲的是数值稳定性陷阱，\n"
                "    与「未来函数」是两回事（后者由 G4 结构性拦截）。请人工确认是否相关。\n"
                "    " + "\n    ".join(str(n)[:200] for n in operator_notes[:5]),
                location="operator_pitfall"))

    return report
