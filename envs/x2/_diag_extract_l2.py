"""诊断 x2strategy Extract 在 Layer 2 崩溃的真因。

现象：`extractor.extract_spec()` 在 Layer 2（indicators）崩于
      `_parse_json_response(None)` → AttributeError（未捕获）。

本脚本复现同一 Layer-2 提示词，但直接调 litellm 并**打印原始响应**，
以区分两种可能：
    (a) 模型/中转返回了空正文（content=None）
    (b) x2strategy 的解析器对空正文不设防

只读 runs/x2/<slug>/content.json；不写任何文件。
"""
from __future__ import annotations

import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[2]
CFG = ROOT / "config" / "llm.toml"
SLUG = "多资产-ETF-轮动策略-固收-视角下动态组合管理的构建与实践"


def main() -> int:
    provider = tomllib.loads(CFG.read_text(encoding="utf-8"))["provider"]
    model = f"{provider['name']}/{provider['model']}"

    from paper2spec import models
    from paper2spec.prompts import LAYER2_INDICATORS_PROMPT, SYSTEM_PROMPT

    content = models.PaperContent.from_json(
        (ROOT / "runs/x2" / SLUG / "content.json").read_text(encoding="utf-8"))

    prompt = LAYER2_INDICATORS_PROMPT.format(
        strategy_name="(diag)", strategy_type="(diag)", description="(diag)",
        signal_logic=content.signal_logic, methodology=content.methodology,
        strategy_focus="", instruction_context="",
    )
    print(f"model     : {model}")
    print(f"prompt    : {len(prompt)} chars")

    import litellm
    resp = litellm.completion(
        model=model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": prompt}],
        max_tokens=8192,
        timeout=300,
    )
    choice = resp.choices[0]
    msg = choice.message
    print(f"finish_reason : {choice.finish_reason}")
    print(f"content type  : {type(msg.content).__name__}")
    print(f"content repr  : {repr(msg.content)[:300]}")
    print(f"content len   : {len(msg.content) if msg.content else 0}")
    rc = getattr(msg, "reasoning_content", None)
    print(f"reasoning     : {(len(rc) if rc else 0)} chars")
    print(f"usage         : {resp.usage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
