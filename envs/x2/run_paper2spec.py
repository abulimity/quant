"""独立驱动 x2strategy 的 **Parse → Extract** 两个阶段（不 import 平台代码）。

严格按 x2strategy 官方 README 的流程与产物布局：

    Parse   : 任意文档 → PaperContent      (paper2spec.parser.parse_document, mode="builtin" = Mode A)
    Extract : PaperContent → StrategySpec[] (paper2spec.extractor.extract_spec, mode="multilayer")

产物写到 `PAPER2SPEC_LIBRARY_PATH/<slug>/`（x2strategy 的默认约定）：
    content.json / content.md   —— Parse 阶段产出
    spec.json    / spec.md      —— Extract 阶段产出（真实 x2strategy 结果）
    run_meta.json               —— 本次用的模型/来源/耗时（便于追溯）

凭据与模型：
  - 模型来自 `config/llm.toml`（`[provider].name/model`），注入 `PAPER2SPEC_MODEL`。
  - 凭据走环境变量（litellm 原生认 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL`），**不读也不打印 key 值**。

用法：
    envs/x2/.venv/Scripts/python.exe envs/x2/run_paper2spec.py "papers/xxx.pdf" [--library runs/x2]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import tomllib

ROOT = pathlib.Path(os.environ.get("QUANT_ROOT", str(pathlib.Path(__file__).resolve().parents[2])))
CFG = ROOT / "config" / "llm.toml"


def resolve_model_from_config() -> str:
    """从 config/llm.toml 取本次模型串（provider/model）。配置缺失即明确失败。"""
    if not CFG.is_file():
        raise SystemExit(f"[FAIL] 配置文件不存在: {CFG}")
    provider = tomllib.loads(CFG.read_text(encoding="utf-8")).get("provider", {})
    name = str(provider.get("name", "")).strip()
    model = str(provider.get("model", "")).strip()
    if not (name and model):
        raise SystemExit(f"[FAIL] config/llm.toml 的 [provider] 缺 name/model: {name!r}/{model!r}")
    key_env = str(provider.get("api_key_env", "")).strip()
    if key_env and not os.environ.get(key_env):
        raise SystemExit(f"[FAIL] 环境变量 {key_env} 未设置（凭据缺失）")
    return f"{name}/{model}"


def slugify(path: pathlib.Path) -> str:
    stem = path.stem
    # 保留 CJK 与字母数字，其余折叠为 '-'（Windows 文件名安全）
    s = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", stem).strip("-")
    return s or "paper"


def main() -> int:
    ap = argparse.ArgumentParser(description="x2strategy: 文档 → StrategySpec（Parse+Extract）")
    ap.add_argument("paper", help="输入文档路径（.pdf/.md/.docx/.txt）")
    ap.add_argument("--library", default="runs/x2", help="产物根目录（默认 runs/x2）")
    ap.add_argument("--parse-mode", default="builtin", choices=["builtin", "agent"],
                    help="Parse 模式：builtin=Mode A（直读），agent=Mode B（FAISS）")
    ap.add_argument("--extract-mode", default="multilayer", choices=["multilayer", "single"],
                    help="Extract 模式：multilayer=4 层聚焦（推荐），single=单次调用")
    ap.add_argument("--model", default=None, help="覆盖模型串；默认取 config/llm.toml")
    ap.add_argument("--instruction-context", default="", help="可选的抽取指令/约束")
    ap.add_argument("--reuse-content", action="store_true",
                    help="复用已落盘的 content.json，跳过 Parse（仅重跑 Extract）")
    args = ap.parse_args()

    paper = pathlib.Path(args.paper)
    if not paper.is_file():
        print(f"[FAIL] 研报不存在: {paper}")
        return 2

    model = args.model or resolve_model_from_config()
    os.environ["PAPER2SPEC_MODEL"] = model          # x2strategy 自己的模型入口
    lib_root = (ROOT / args.library).resolve()
    out_dir = lib_root / slugify(paper)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["PAPER2SPEC_LIBRARY_PATH"] = str(lib_root)

    print("=" * 72)
    print(f"输入    : {paper}")
    print(f"模型    : {model}")
    print(f"Parse   : mode={args.parse_mode}")
    print(f"Extract : mode={args.extract_mode}")
    print(f"产物目录: {out_dir}")
    print("=" * 72)

    from paper2spec import extractor, models, parser, render

    # ── Stage 1: Parse ──
    cached = out_dir / "content.json"
    if args.reuse_content and cached.is_file():
        content = models.PaperContent.from_json(cached.read_text(encoding="utf-8"))
        dt_parse = 0.0
        print(f"[Parse] 复用已落盘产物: {cached}")
    else:
        t0 = time.time()
        content = parser.parse_document(str(paper), mode=args.parse_mode, model=model)
        dt_parse = time.time() - t0
        (out_dir / "content.json").write_text(content.to_json(), encoding="utf-8")
        (out_dir / "content.md").write_text(render.content_to_markdown(content), encoding="utf-8")
        print(f"[Parse] OK  {dt_parse:.1f}s  title={content.title[:60]!r}")
    print(f"        full_text={len(content.full_text)} chars  "
          f"methodology={len(content.methodology)}  data={len(content.data_description)}  "
          f"signal={len(content.signal_logic)}")

    # ── Stage 2: Extract ──
    t1 = time.time()
    result = extractor.extract_spec(
        content, model=model, mode=args.extract_mode,
        instruction_context=args.instruction_context,
    )
    dt_extract = time.time() - t1
    (out_dir / "spec.json").write_text(result.to_json(), encoding="utf-8")
    (out_dir / "spec.md").write_text(render.spec_to_markdown(result), encoding="utf-8")
    print(f"[Extract] OK  {dt_extract:.1f}s  num_detected={result.num_detected}  "
          f"strategies={len(result.strategies)}")

    meta = {
        "input": str(paper), "model": model,
        "parse_mode": args.parse_mode, "extract_mode": args.extract_mode,
        "num_detected": result.num_detected, "n_strategies": len(result.strategies),
        "paper_title": result.paper_title,
        "seconds": {"parse": round(dt_parse, 2), "extract": round(dt_extract, 2)},
        "x2strategy_version": _pkg_version("x2strategy"),
        "outputs": ["content.json", "content.md", "spec.json", "spec.md"],
    }
    (out_dir / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 关键结果摘要（供人直接看）
    print("-" * 72)
    for i, spec in enumerate(result.strategies, 1):
        print(f"策略 {i}: {spec.strategy_name!r}  type={spec.strategy_type}  "
              f"assets={spec.asset_class}  freq={spec.data_frequency}  lookback={spec.lookback_period}")
        print(f"   indicators      : {[getattr(x, 'name', x) for x in spec.indicators]}")
        print(f"   logic_pipeline  : {len(spec.logic_pipeline)} steps, "
              f"execution_plan: {len(spec.execution_plan)} blocks, "
              f"needs_human_review: {len(spec.needs_human_review)}")
    print("-" * 72)
    print(f"完成。真实产物见: {out_dir}")
    return 0


def _pkg_version(name: str) -> str:
    try:
        import importlib.metadata as m
        return m.version(name)
    except Exception:  # noqa: BLE001
        return "?"


if __name__ == "__main__":
    raise SystemExit(main())
