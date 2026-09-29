"""规格注册与闸门串联（LOCAL_DEPLOYMENT_PLAN.md §P5.3）。

**唯一入库路径**：`register_spec()` → 内部**强制** `lint_spec()` → 通过才落盘。

    register_spec(spec) ─┬─ lint_spec(spec) 失败 → 抛 SpecRejected（**不落盘**）
                         ├─ 通过 → 算 spec_id → 落 runs/specs/<spec_id>.json
                         └─ 已存在 → 校验内容一致后返回（**幂等**）

**「绕过 lint 的入库路径不存在」是结构性保证**，靠三点：

1. 本模块**不提供**任何 `skip_lint` / `force` 参数 —— 没有开关可开；
2. 落盘只发生在本文件内部（`_write` 是私有的），外部拿不到「不过闸门也能写」的入口；
3. 写入的内容里**捆绑**了 lint 报告，事后可审计「这份规格当时过了什么规则」。

> 不是「调用方记得先 lint」这种君子协定 —— 那种约定迟早会被绕过。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from quantlab.contract.lint import LintFinding, LintReport, lint_spec
from quantlab.contract.types import StrategySpec, validate_spec
from quantlab.engines.base import PROJECT_ROOT

SPECS_DIR = PROJECT_ROOT / "runs" / "specs"


class SpecRejected(RuntimeError):
    """规格未通过闸门 —— 拒绝入库。"""


@dataclass
class RegisteredSpec:
    spec_id: str
    path: Path
    spec: StrategySpec
    report: LintReport
    already_present: bool

    def as_dict(self) -> dict:
        return {
            "spec_id": self.spec_id,
            "path": str(self.path),
            "already_present": self.already_present,
            "passed": self.report.passed,
            "origin": self.spec.origin,
        }


def spec_id(spec: StrategySpec) -> str:
    """规格的内容哈希前 16 位。

    **同一份规格 → 同一 spec_id**（幂等的基础）；规格变了 → id 变
    （旧记录不动，与 P2.5 的快照纪律同一条思路：**修正走新记录，不原地覆盖**）。
    """
    return hashlib.sha256(spec.to_json().encode("utf-8")).hexdigest()[:16]


def _write(spec: StrategySpec, report: LintReport, target: Path) -> None:
    """**唯一**的落盘点（私有）。内容捆绑 lint 报告，便于事后审计。"""
    payload = {
        "spec_id": spec_id(spec),
        "registered_at_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "spec": json.loads(spec.to_json()),
        "lint": report.run_metadata(),
        "operator_notes": [str(f) for f in report.findings if f.rule.startswith("X2.")],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)          # 原子替换，避免半截文件


def register_spec(
    spec: StrategySpec,
    *,
    specs_dir: str | Path | None = None,
    listing_dates: dict[int, object] | None = None,
    as_of: object | None = None,
    operator_notes: list[str] | None = None,
) -> RegisteredSpec:
    """把规格注册入库。**这是唯一入库路径**，闸门不可绕过。

    参数：
        listing_dates / as_of / operator_notes —— 透传给 `lint_spec`（G8 / x2 提示）

    返回：`RegisteredSpec`（含 spec_id、落盘路径、lint 报告、是否已存在）

    异常：
        ContractViolation —— 规格自身结构非法
        SpecRejected      —— **未过闸门**（不落盘）
    """
    # 1) 结构校验（fail-closed）
    validate_spec(spec)

    # 2) **强制**过闸门 —— 没有开关可关
    report = lint_spec(spec, listing_dates=listing_dates, as_of=as_of,
                       operator_notes=operator_notes)
    if not report.passed:
        detail = "\n".join(f"  - {f}" for f in report.errors)
        raise SpecRejected(
            f"规格 {spec.name!r} 未通过闸门，拒绝入库：\n{detail}\n"
            f"处置：改规格或改规则。**没有** --skip-lint 之类的旁路。")

    # 3) 幂等：同 id 已在盘上 → 校验内容一致后返回，不重复写
    directory = Path(specs_dir) if specs_dir else SPECS_DIR
    target = directory / f"{spec_id(spec)}.json"
    if target.is_file():
        existing = load_spec(target)
        if existing.spec.to_json() != spec.to_json():
            raise SpecRejected(
                f"spec_id 冲突：{target} 已存在但内容不同 —— 哈希碰撞或文件被改动。"
                f"不覆盖，请人工核查。")
        return RegisteredSpec(spec_id(spec), target, spec, existing.report, True)

    # 4) 落盘
    _write(spec, report, target)
    return RegisteredSpec(spec_id(spec), target, spec, report, False)


def load_spec(path_or_id: str | Path, *, specs_dir: str | Path | None = None) -> RegisteredSpec:
    """按路径或 `spec_id` 读回已注册的规格。"""
    candidate = Path(path_or_id)
    if not candidate.is_file():
        directory = Path(specs_dir) if specs_dir else SPECS_DIR
        candidate = directory / f"{path_or_id}.json"
    if not candidate.is_file():
        raise FileNotFoundError(f"未找到已注册的规格: {path_or_id}")

    payload = json.loads(candidate.read_text(encoding="utf-8"))
    spec = StrategySpec.from_dict(payload["spec"])

    # 还原一份「当时」的 lint 报告（审计：这份规格入库时是什么状态）
    meta = payload.get("lint", {})
    findings = [LintFinding("X2.operator_notes", "note", n)
                for n in payload.get("operator_notes", [])]
    findings += [LintFinding("registered.error", "error", e) for e in meta.get("errors", [])]
    findings += [LintFinding("registered.warning", "warning", w)
                 for w in meta.get("warnings", [])]
    report = LintReport(spec_name=meta.get("spec_name", spec.name),
                        origin=meta.get("origin", spec.origin),
                        findings=findings,
                        rules_run=list(meta.get("rules_run", [])))
    return RegisteredSpec(payload.get("spec_id", spec_id(spec)), candidate, spec, report, True)


def list_specs(*, specs_dir: str | Path | None = None) -> list[RegisteredSpec]:
    """列出已注册的规格（按 spec_id 排序，确定性）。"""
    directory = Path(specs_dir) if specs_dir else SPECS_DIR
    if not directory.is_dir():
        return []
    return [load_spec(p, specs_dir=directory) for p in sorted(directory.glob("*.json"))]
