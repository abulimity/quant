"""目标环境入口（x2）—— LOCAL_DEPLOYMENT_PLAN.md §P5.2 / §P5.4。

契约（手册 P1.5）：
    argv[1] = job.json 路径
    job.json = {job_id, env, engine, entry, params, inputs, outputs, env_lock_sha256}
    结果写入 job["outputs"]["result_json"]

本文件在隔离环境内**独立存在**，**不 import core 的任何代码**。
这里跑的是真正的 `paper2spec` / `spec2code`（core 侧不装它们）。

支持的操作（`params.op`）：

    add        —— 桥的最小烟测（P1.5 遗留）
    raise      —— 故意失败，验证「目标环境失败可传播」
    paper2spec —— **P5.2**：PDF / Markdown → x2strategy 的原始规格
    paper2dsl  —— **P5.6a**：PDF / Markdown → 受控 DSL（我们自研 prompt，非 paper2spec）
    spec2code  —— **P5.4**：规格 → backtrader 策略源码

⚠️ **LLM 模型必须显式指定**（§P5.1 V1）：未指定就**明确失败**，
   绝不静默落到 `paper2spec.llm.DEFAULT_MODEL`（那是某个付费默认模型）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# paper2spec 内置默认是 openai/gpt-4o-mini；我们要么收显式 model，
# 要么读这个环境变量 —— **不**沿用它的默认值。
MODEL_ENV = "PAPER2SPEC_MODEL"


class X2EntryError(RuntimeError):
    """x2 环境内的可诊断错误。"""


def _resolve_model(params: dict) -> str:
    """确定本次使用的模型串（litellm 格式 `provider/model`）。"""
    explicit = str(params.get("model", "")).strip()
    if explicit:
        return explicit
    from_env = os.environ.get(MODEL_ENV, "").strip()
    if from_env:
        return from_env
    raise X2EntryError(
        f"paper2spec 未指定模型：请在 job.params.model 给出，"
        f"或设置环境变量 {MODEL_ENV}。\n"
        f"注意：本平台**不会**沿用 paper2spec 的内置默认模型 —— "
        f"那等于静默使用一个你没选过的付费模型（§P5.1 V1）。")


def _load_paper(inputs: dict) -> tuple[str, str]:
    """读入论文。返回 (文本, 来源描述)。PDF 走 paper2spec 自带的 pdf_utils。"""
    raw = inputs.get("paper")
    if not raw:
        raise X2EntryError("缺少输入：job.inputs['paper']")
    path = Path(raw)
    if not path.is_file():
        raise FileNotFoundError(f"论文文件不存在: {path}")

    if path.suffix.lower() == ".pdf":
        from paper2spec import pdf_utils
        for name in ("extract_text", "pdf_to_text", "read_pdf", "load_pdf", "extract"):
            fn = getattr(pdf_utils, name, None)
            if callable(fn):
                return str(fn(path)), f"pdf:{path.name}"
        raise X2EntryError(
            f"paper2spec.pdf_utils 中找不到文本抽取函数；实际导出: "
            f"{[n for n in dir(pdf_utils) if not n.startswith('_')]}")
    return path.read_text(encoding="utf-8"), f"text:{path.name}"


def _op_paper2spec(job: dict, params: dict) -> dict:
    from dataclasses import asdict, is_dataclass

    from paper2spec import extractor, models

    model = _resolve_model(params)
    text, source = _load_paper(job.get("inputs", {}))

    content = models.PaperContent(title=params.get("title", ""), full_text=text)
    result = extractor.extract_spec(
        content, model=model, mode=params.get("mode", "multilayer"),
        instruction_context=params.get("instruction_context", ""))

    spec = getattr(result, "spec", result)
    payload = asdict(spec) if is_dataclass(spec) else spec
    if not isinstance(payload, dict):
        raise X2EntryError(f"paper2spec 产出的规格不是映射类型: {type(payload).__name__}")
    return {
        "job_id": job.get("job_id"), "env": job.get("env"), "op": "paper2spec",
        "model": model,
        "source": source,
        "source_chars": len(text),
        "raw_spec": payload,
        "needs_human_review": bool(getattr(spec, "needs_human_review", False)),
    }


def _parse_json_content(content: str) -> dict:
    """把 LLM 的文本输出解析成 JSON 对象（fail-closed）。

    LLM 常把 JSON 包在 ```json ... ``` 围栏里。这里只做**最小**清理：去围栏；
    JSON 解析失败即明确报错，**绝不**静默吞掉或猜结构。
    """
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise X2EntryError(
            f"LLM 输出不是合法 JSON（无法吃下）：{exc}\n"
            f"--- 原始输出（前 500 字）---\n{text[:500]}")
    if not isinstance(data, dict):
        raise X2EntryError(
            f"LLM 输出的 JSON 顶层必须是对象，得到 {type(data).__name__}")
    return data


def _op_paper2dsl(job: dict, params: dict) -> dict:
    """**P5.6a**：我们自研的「受控 DSL 提取」—— prompt 逼结构化，不经过 paper2spec。

    system = `DSL_SCHEMA`（由 core 传入，单一真相），user = 论文全文。
    凭据纪律：key 从 `params.api_key_env` 指向的环境变量读，**不写进 job.json / result.json**。
    """
    import litellm

    model = _resolve_model(params)
    text, source = _load_paper(job.get("inputs", {}))

    system = str(params.get("dsl_schema") or "").strip()
    if not system:
        raise X2EntryError("paper2dsl 需要 params.dsl_schema（DSL 契约，由 core 传入）")

    api_key_env = str(params.get("api_key_env") or "").strip()
    api_key = os.environ.get(api_key_env) if api_key_env else None
    api_base = str(params.get("api_base") or "").strip() or None
    timeout = int(params.get("timeout_s", 120))

    kwargs: dict = {"timeout": timeout}
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base

    resp = litellm.completion(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        temperature=0.0,
        **kwargs,
    )
    content = resp.choices[0].message.content
    dsl = _parse_json_content(content)
    return {
        "job_id": job.get("job_id"), "env": job.get("env"), "op": "paper2dsl",
        "model": model,
        "source": source,
        "source_chars": len(text),
        "dsl": dsl,
        "needs_human_review": bool(dsl.get("needs_human_review", False)),
    }


def _op_operator_pitfall(job: dict, params: dict) -> dict:
    """算子误用检测（§P3.2 的「x2strategy 规则」）。

    产出是**咨询性**的（operator notes），供人工/报告参考。**是否阻断由 core 侧决定**，
    不在这里下结论 —— 本入口只负责把机制跑通并把结果原样回传。

    需要 `agent` extra（langchain-community + sentence-transformers + faiss-cpu），
    且首次会拉 embedding 模型（本机需经代理，见 docs/deploy/EVIDENCE.md §P5）。
    """
    from paper2spec import operator_pitfall as op

    spec = params.get("spec")
    if not spec:
        raise X2EntryError("operator_pitfall 需要 params.spec")
    threshold = float(params.get("threshold", 0.65))
    top_k = int(params.get("top_k", 3))

    queries = op.operator_pitfall_queries_from_spec(spec)
    matches = op.retrieve_operator_pitfalls(spec, threshold=threshold, top_k=top_k)
    # `matches` 里可能含不可 JSON 序列化的值（分数是 float32 等），统一转字符串兜底
    safe = [{k: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
                 else str(v)) for k, v in m.items()} for m in matches]
    return {
        "job_id": job.get("job_id"), "env": job.get("env"), "op": "operator_pitfall",
        "threshold": threshold, "top_k": top_k,
        "n_entries": len(op.load_operator_pitfall_entries()),
        "n_queries": len(queries),
        "queries": [[p, t] for p, t in queries],
        "n_matches": len(safe),
        "matches": safe,
        "rendered": op.render_operator_pitfall_matches(safe),
    }


def _op_validate_code(job: dict, params: dict) -> dict:
    """**P5.4**：校验 backtrader 策略源码 —— 用 x2strategy 自带的 `spec2code.validate_code`。

    ⚠️ **实测澄清**（与手册 §P5.4 的措辞不符，已留证）：
    `spec2code` 包里**没有代码生成器** —— 它的全部函数只有
    `get_backtest_timeout` / `get_data_cache_dir` / `validate_code`。
    所谓「spec2code 生成策略类」并不发生在包里（那是作者侧的 agent 流程）。
    故本入口只做**校验**这一件包能做且确实是它职责的事。

    经人工确认：**只做 validator 集成，不做生成**。
    """
    from spec2code import validator

    code = params.get("code")
    if not code:
        raise X2EntryError("validate_code 需要 params.code（策略源码文本）")
    result = validator.validate_code(code)

    # ValidationResult 的字段名随版本可能变，故保守地取已知字段并兜底
    ok = bool(getattr(result, "is_valid", getattr(result, "valid", False)))
    issues = list(getattr(result, "errors", []) or [])
    warnings = list(getattr(result, "warnings", []) or [])
    return {
        "job_id": job.get("job_id"), "env": job.get("env"), "op": "validate_code",
        "is_valid": ok,
        "n_errors": len(issues),
        "errors": [str(e) for e in issues[:20]],
        "n_warnings": len(warnings),
        "warnings": [str(w) for w in warnings[:20]],
        "validator": "spec2code.validator.validate_code",
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: entry.py <job.json>", file=sys.stderr)
        return 2

    job = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    params = job.get("params", {})
    op = params.get("op")

    if op == "raise":
        # 故意失败：用于验证「目标环境失败可传播、不被静默吞掉」
        raise RuntimeError(params.get("message", "intentional failure"))

    if op == "add":
        result = {"job_id": job.get("job_id"), "env": job.get("env"), "op": "add",
                  "a": params.get("a"), "b": params.get("b"),
                  "sum": params.get("a", 0) + params.get("b", 0)}
    elif op == "paper2spec":
        result = _op_paper2spec(job, params)
    elif op == "paper2dsl":
        result = _op_paper2dsl(job, params)
    elif op == "validate_code":
        result = _op_validate_code(job, params)
    elif op == "operator_pitfall":
        result = _op_operator_pitfall(job, params)
    else:
        result = {"job_id": job.get("job_id"), "env": job.get("env"), "op": op,
                  "note": "stub: 未实现该算子"}

    out = Path(job["outputs"]["result_json"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
