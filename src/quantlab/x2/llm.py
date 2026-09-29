"""LLM 通道配置（LOCAL_DEPLOYMENT_PLAN.md §P5.1）。

**凭据纪律（CLAUDE.md）**：仓库里只有「**环境变量名**」，绝无 key 的值。
`config/llm.toml` 写 `api_key_env = "SOME_ENV_VAR"`，真正的 key 由环境变量提供。

**fail-closed（§P5.1 V1）**：未配置 provider 时，`require()` 会**明确报错**
并说明如何配置 —— **绝不**静默退回某个付费默认值。理由：静默用付费 API
既烧钱、又可能把数据送出境，而且出问题时毫无痕迹。

自检（**不打印任何凭据**）：

    python -m quantlab.x2.llm --check
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "llm.toml"

KIND_CLOUD = "cloud"
KIND_OLLAMA = "ollama"


class LlmNotConfigured(RuntimeError):
    """LLM 通道未配置 —— 必须显式失败，不得静默使用付费默认值。"""


@dataclass(frozen=True)
class LlmConfig:
    kind: str                  # "" | "cloud" | "ollama"
    name: str = ""
    model: str = ""
    api_key_env: str = ""
    api_base_env: str = ""
    timeout_s: int = 120
    ollama_base_url: str = "http://127.0.0.1:11434"

    @property
    def configured(self) -> bool:
        return self.kind in (KIND_CLOUD, KIND_OLLAMA)

    # ---- 凭据（只读环境变量，绝不落盘） ----
    @property
    def api_key_present(self) -> bool:
        return bool(self.api_key_env) and bool(os.environ.get(self.api_key_env))

    def api_key(self) -> str:
        """取 key 值。**调用方不得记录或打印它。**"""
        if not self.api_key_env:
            raise LlmNotConfigured(self._how_to_configure("未声明 api_key_env"))
        value = os.environ.get(self.api_key_env)
        if not value:
            raise LlmNotConfigured(
                self._how_to_configure(f"环境变量 {self.api_key_env} 未设置或为空"))
        return value

    def api_base(self) -> str | None:
        if not self.api_base_env:
            return None
        return os.environ.get(self.api_base_env) or None

    def model_id(self) -> str:
        """litellm 用的模型串：`provider/model`（ollama 走 `ollama/<model>`）。"""
        if self.kind == KIND_OLLAMA:
            return f"ollama/{self.model}" if self.model else "ollama"
        return f"{self.name}/{self.model}" if self.name else self.model

    # ---- 报错文案：必须告诉人「下一步做什么」 ----
    def _how_to_configure(self, reason: str) -> str:
        return (
            f"LLM 通道未配置（{reason}）。\n"
            f"处置：编辑 config/llm.toml，在 [provider] 填 kind/name/model 与\n"
            f"      api_key_env（**环境变量名**，不是 key 本身），"
            f"设置该环境变量后重试。\n"
            f"      自检（不会打印 key）：python -m quantlab.x2.llm --check\n"
            f"注意：本平台**不会**静默使用任何付费默认值 —— "
            f"未配置就是明确失败（§P5.1 V1）。\n"
            f"      离线场景可改走本地 Ollama（kind=\"ollama\"）。")

    def require(self) -> "LlmConfig":
        """闸门：未配置就抛错。**调用 LLM 之前必须先过这里。**"""
        if not self.configured:
            raise LlmNotConfigured(self._how_to_configure("kind 为空"))
        if self.kind == KIND_CLOUD:
            if not self.name or not self.model:
                raise LlmNotConfigured(self._how_to_configure("cloud 缺少 name/model"))
            if not self.api_key_env:
                raise LlmNotConfigured(self._how_to_configure("cloud 缺少 api_key_env"))
            self.api_key()          # 顺带校验环境变量确实存在
        elif self.kind == KIND_OLLAMA and not self.ollama_base_url:
            raise LlmNotConfigured(self._how_to_configure("ollama 缺少 base_url"))
        return self

    # ---- 自检输出：只讲「有没有」，不讲「是什么」 ----
    def describe(self) -> dict:
        return {
            "kind": self.kind or "(未配置)",
            "name": self.name or "(空)",
            "model": self.model or "(空)",
            "model_id": self.model_id() if self.configured else "(未配置)",
            "api_key_env": self.api_key_env or "(空)",
            "api_key_present": self.api_key_present,     # 布尔，不是值
            "api_base_env": self.api_base_env or "(空)",
            "api_base_present": bool(self.api_base()) if self.api_base_env else False,
            "ollama_base_url": self.ollama_base_url,
            "configured": self.configured,
            "usable": self._usable(),
        }

    def _usable(self) -> bool:
        try:
            self.require()
        except LlmNotConfigured:
            return False
        return True


def load_llm_config(path: str | Path | None = None) -> LlmConfig:
    """读取 `config/llm.toml`。

    **文件不存在时返回「未配置」而不是报错** —— 让调用方在真正要用 LLM 时
    （`require()`）才失败，从而不阻塞 P5 其余步骤（§P5.1 失败处理）。
    """
    target = Path(path) if path is not None else DEFAULT_CONFIG
    if not target.is_file():
        return LlmConfig(kind="")
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    provider = data.get("provider", {})
    ollama = data.get("ollama", {})
    return LlmConfig(
        kind=str(provider.get("kind", "")).strip(),
        name=str(provider.get("name", "")).strip(),
        model=str(provider.get("model", "")).strip(),
        api_key_env=str(provider.get("api_key_env", "")).strip(),
        api_base_env=str(provider.get("api_base_env", "")).strip(),
        timeout_s=int(provider.get("timeout_s", 120)),
        ollama_base_url=str(ollama.get("base_url", "http://127.0.0.1:11434")).strip(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM 通道自检（不打印任何凭据）")
    parser.add_argument("--check", action="store_true", help="打印配置状态并给出退出码")
    parser.add_argument("--config", default=None, help="覆盖 config 路径")
    args = parser.parse_args(argv)

    config = load_llm_config(args.config)
    status = config.describe()
    for key, value in status.items():
        print(f"  {key:18s}: {value}")
    if not args.check:
        return 0
    if status["usable"]:
        print("LLM 通道：可用")
        return 0
    print("LLM 通道：**未配置或不可用**（调用时会明确报错，不会静默使用付费默认值）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
