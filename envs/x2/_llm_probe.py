"""最小 LLM 可用性探针 —— 只验「配置好的云端模型此刻能否真的回包」。

不 import 平台任何代码；只读 config/llm.toml（本文件在 x2 环境内独立存在）。
用法：envs/x2/.venv/Scripts/python.exe envs/x2/_llm_probe.py
退出码：0 = 真调成功；1 = 配置不全；2 = 调用失败。
"""
from __future__ import annotations

import os
import pathlib
import sys
import time
import tomllib
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[2]
CFG = ROOT / "config" / "llm.toml"


def main() -> int:
    if not CFG.is_file():
        print(f"[FAIL] 配置文件不存在: {CFG}")
        return 1
    provider = tomllib.loads(CFG.read_text(encoding="utf-8")).get("provider", {})
    name = str(provider.get("name", "")).strip()
    model = str(provider.get("model", "")).strip()
    key_env = str(provider.get("api_key_env", "")).strip()
    base_env = str(provider.get("api_base_env", "")).strip()
    base_lit = str(provider.get("api_base", "")).strip()

    if not (name and model and key_env):
        print(f"[FAIL] 配置不全: name={name!r} model={model!r} api_key_env={key_env!r}")
        return 1

    key = os.environ.get(key_env)
    if not key:
        print(f"[FAIL] 环境变量 {key_env} 未设置（配置指向的凭据缺失）")
        return 1

    base = (os.environ.get(base_env) if base_env else "") or base_lit
    model_id = f"{name}/{model}"

    print(f"model_id  : {model_id}")
    print(f"api_base  : {base or '(官方默认)'}")
    print(f"key_env   : {key_env}  (已设置, 不打印值)")

    # litellm 的 anthropic provider 默认读 ANTHROPIC_API_KEY；本平台用 ANTHROPIC_AUTH_TOKEN，
    # 故显式桥接。**不打印 key 值。**
    os.environ.setdefault("ANTHROPIC_API_KEY", key)
    import litellm  # noqa: E402

    t0 = time.time()
    try:
        resp = litellm.completion(
            model=model_id,
            messages=[{"role": "user", "content": "Reply with exactly one word: PONG"}],
            max_tokens=int(os.environ.get("PROBE_MAX_TOKENS", "2048")),
            api_base=base or None,
            timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] 调用失败（{time.time()-t0:.1f}s）: {type(e).__name__}: {str(e)[:600]}")
        traceback.print_exc(file=sys.stdout)
        return 2

    text = (resp.choices[0].message.content or "").strip()
    print(f"[OK] 回包（{time.time()-t0:.1f}s）: {text!r}")
    print(f"     model={getattr(resp,'model',None)}  usage={getattr(resp,'usage',None)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
