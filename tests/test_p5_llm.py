"""P5.1 LLM 通道验收（LOCAL_DEPLOYMENT_PLAN.md §P5.1）。

覆盖：
    V0  仓库内**无任何明文 API key**
    V1  未配置 provider 时，调用报**明确**错误并提示如何配置，**不得静默使用付费默认值**
    V1  `config/llm.toml` 可解析；凭据只以**环境变量名**形式出现
    另：cloud/ollama 两种后端的 `model_id()` 组装；`describe()` **不泄漏**凭据值

**这些用例不联网、不调用任何 LLM**；测试里的 key 值全部是明显假的占位。

运行：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -m unittest tests.test_p5_llm -v
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

from quantlab.x2.llm import (
    KIND_CLOUD,
    LlmConfig,
    LlmNotConfigured,
    load_llm_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LLM_TOML = PROJECT_ROOT / "config" / "llm.toml"

# 形如真实 key 的模式（用于扫仓库）
_KEY_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),          # google
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),             # github pat
)

CLOUD_BODY = """
[provider]
kind = "cloud"
name = "anthropic"
model = "claude-sonnet-4-5"
api_key_env = "QUANTLAB_TEST_KEY_ABSENT"
api_base_env = "QUANTLAB_TEST_BASE_ABSENT"
timeout_s = 30

[ollama]
base_url = "http://127.0.0.1:11434"
"""


def _write_config(tmp: str, body: str) -> Path:
    path = Path(tmp) / "llm.toml"
    path.write_text(body, encoding="utf-8")
    return path


class TestNoCredentialsInRepo(unittest.TestCase):
    """V0：仓库内**无任何明文 API key**。"""

    def test_shipped_config_declares_env_var_names_not_values(self) -> None:
        self.assertTrue(LLM_TOML.is_file(), f"缺少 {LLM_TOML}")
        text = LLM_TOML.read_text(encoding="utf-8")
        for pattern in _KEY_PATTERNS:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.search(text), "llm.toml 里出现疑似明文 key")

    def test_shipped_config_is_structurally_valid(self) -> None:
        """随仓库交付的配置必须**可解析且结构完整**。

        注意：**不能**断言「kind 为空」。P5.1 完成后，人工会把 provider 填好
        （这是预期状态），所以这里只校验结构 —— 该断「空」，人工一填就红，
        那是在把「配置好了」误判成故障。
        真正要守的是：**凭据值不得出现在文件里**（由上面两条扫描覆盖）。
        """
        config = load_llm_config(LLM_TOML)
        self.assertIn(config.kind, ("", "cloud", "ollama"),
                      f"kind 取值非法: {config.kind!r}")
        if config.kind == KIND_CLOUD:
            self.assertTrue(config.name, "cloud 必须填 name")
            self.assertTrue(config.model, "cloud 必须填 model")
            self.assertTrue(config.api_key_env, "cloud 必须声明 api_key_env")
            # api_key_env 必须是**变量名**，不能是 key 值本身
            self.assertRegex(config.api_key_env, r"^[A-Z][A-Z0-9_]*$",
                             "api_key_env 应当是环境变量名（大写+下划线），不是 key 值")

    def test_no_plaintext_keys_anywhere_in_tracked_sources(self) -> None:
        """扫源码与配置目录（跳过 .venv/缓存），不得出现明文 key。"""
        offenders: list[str] = []
        for root in ("src", "config", "tests", "envs"):
            for path in (PROJECT_ROOT / root).rglob("*"):
                if not path.is_file() or path.suffix not in (
                        ".py", ".toml", ".yaml", ".yml", ".json", ".md"):
                    continue
                if any(part in {".venv", "__pycache__", "node_modules"} for part in path.parts):
                    continue
                if path.resolve() == Path(__file__).resolve():
                    continue   # 不扫自己（本文件含用于比对的模式串）
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for pattern in _KEY_PATTERNS:
                    if pattern.search(text):
                        offenders.append(f"{path.relative_to(PROJECT_ROOT)} :: {pattern.pattern}")
        self.assertEqual(offenders, [], f"发现疑似明文凭据：{offenders}")


class TestUnconfiguredFailsClosed(unittest.TestCase):
    """V1：未配置时**明确报错**，不得静默使用付费默认值。"""

    def test_blank_kind_raises(self) -> None:
        with self.assertRaises(LlmNotConfigured) as ctx:
            LlmConfig(kind="").require()
        self.assertIn("config/llm.toml", str(ctx.exception))

    def test_missing_config_file_is_unconfigured_not_a_crash(self) -> None:
        """文件不存在 → 返回「未配置」（不阻塞 P5 其余步骤），但**仍不可用**。"""
        with tempfile.TemporaryDirectory() as tmp:
            config = load_llm_config(Path(tmp) / "nope.toml")
        self.assertFalse(config.configured)
        self.assertFalse(config._usable())
        with self.assertRaises(LlmNotConfigured):
            config.require()

    def test_cloud_without_env_var_set_raises(self) -> None:
        """配了 name/model，但**环境变量没设** → 仍然必须失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            config = load_llm_config(_write_config(tmp, CLOUD_BODY))
        self.assertTrue(config.configured, "kind=cloud 应被视为已配置")
        self.assertFalse(config.api_key_present)
        with self.assertRaises(LlmNotConfigured) as ctx:
            config.require()
        self.assertIn("QUANTLAB_TEST_KEY_ABSENT", str(ctx.exception))

    def test_error_message_tells_you_what_to_do(self) -> None:
        """报错必须**可操作**：指出配置文件、环境变量、自检命令，并声明不会静默兜底。"""
        with self.assertRaises(LlmNotConfigured) as ctx:
            LlmConfig(kind="").require()
        message = str(ctx.exception)
        for expected in ("config/llm.toml", "api_key_env", "quantlab.x2.llm", "不会"):
            self.assertIn(expected, message)

    def test_cloud_without_api_key_env_name_raises(self) -> None:
        body = '[provider]\nkind="cloud"\nname="anthropic"\nmodel="x"\n'
        with tempfile.TemporaryDirectory() as tmp:
            config = load_llm_config(_write_config(tmp, body))
        with self.assertRaises(LlmNotConfigured):
            config.require()

    def test_cloud_without_name_or_model_raises(self) -> None:
        body = '[provider]\nkind="cloud"\napi_key_env="K"\n'
        with tempfile.TemporaryDirectory() as tmp:
            config = load_llm_config(_write_config(tmp, body))
        with self.assertRaises(LlmNotConfigured):
            config.require()


class TestConfiguredBehaviour(unittest.TestCase):
    """配上（假的）环境变量后应当通过闸门 —— 证明失败原因确实是「没配」。"""

    def test_cloud_requires_env_and_then_passes(self) -> None:
        env_name = "QUANTLAB_TEST_KEY_PRESENT"
        os.environ[env_name] = "not-a-real-key-just-for-test"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = load_llm_config(
                    _write_config(tmp, CLOUD_BODY.replace("QUANTLAB_TEST_KEY_ABSENT", env_name)))
            self.assertTrue(config.api_key_present)
            self.assertIs(config.require(), config)          # 不抛
            self.assertEqual(config.model_id(), "anthropic/claude-sonnet-4-5")
            self.assertEqual(config.timeout_s, 30)
        finally:
            os.environ.pop(env_name, None)

    def test_ollama_needs_no_key(self) -> None:
        body = ('[provider]\nkind="ollama"\nmodel="llama3"\n'
                '[ollama]\nbase_url="http://127.0.0.1:11434"\n')
        with tempfile.TemporaryDirectory() as tmp:
            config = load_llm_config(_write_config(tmp, body))
        self.assertTrue(config.require())
        self.assertEqual(config.model_id(), "ollama/llama3")

    def test_describe_never_leaks_the_secret_value(self) -> None:
        """`describe()` 只能报**有没有**，绝不能报**是什么**。"""
        env_name = "QUANTLAB_TEST_KEY_SECRET"
        secret = "FAKE-KEY-FOR-LEAK-TEST-DO-NOT-USE-abcdef"
        os.environ[env_name] = secret
        try:
            config = LlmConfig(kind=KIND_CLOUD, name="anthropic", model="claude-sonnet-4-5",
                               api_key_env=env_name)
            status = config.describe()
            blob = repr(status)
            self.assertNotIn(secret, blob, "describe() 泄漏了 key 的值")
            self.assertNotIn(secret[:12], blob)
            self.assertTrue(status["api_key_present"])       # 但要说「有」
        finally:
            os.environ.pop(env_name, None)

    def test_api_base_reported_as_boolean_only(self) -> None:
        env_name = "QUANTLAB_TEST_BASE_URL"
        os.environ[env_name] = "https://example.internal/v1"
        try:
            config = LlmConfig(kind=KIND_CLOUD, name="anthropic", model="m",
                               api_key_env="X", api_base_env=env_name)
            status = config.describe()
            self.assertTrue(status["api_base_resolved"])
            self.assertNotIn("example.internal", repr(status))
        finally:
            os.environ.pop(env_name, None)


class TestConfigParsing(unittest.TestCase):
    def test_unknown_kind_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_llm_config(_write_config(tmp, '[provider]\nkind="magic"\n'))
        self.assertFalse(config.configured)
        with self.assertRaises(LlmNotConfigured):
            config.require()

    def test_whitespace_is_stripped(self) -> None:
        body = ('[provider]\nkind = "  cloud  "\nname="  anthropic "\n'
                'model=" m "\napi_key_env=" K "\n')
        with tempfile.TemporaryDirectory() as tmp:
            config = load_llm_config(_write_config(tmp, body))
        self.assertEqual(config.kind, "cloud")
        self.assertEqual(config.name, "anthropic")
        self.assertEqual(config.api_key_env, "K")


if __name__ == "__main__":
    unittest.main(verbosity=2)
