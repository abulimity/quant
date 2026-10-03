# 对 x2strategy 的本地补丁记录

**日期**：2026-10-02 ｜ **被测对象**：x2strategy 0.4.0（`envs/x2/.venv`） ｜ **授权**：人工明确同意

> ⚠️ **本补丁改变了被测对象本身。** 打补丁之后产出的 `spec.json` **不是 x2strategy 0.4.0 开箱行为**，
> 而是"x2strategy 0.4.0 + 本补丁"的结果。引用结论时必须连带声明这一点。

## 1. 为什么打这个补丁

本机配置的云端通道（`config/llm.toml` → DeepSeek 的 Anthropic 兼容中转）**只提供推理模型**
（`/models` 实测：`deepseek-flash`、`deepseek-v4-pro`，均默认 `effort=high`）。

x2strategy 在 `extractor.py` 中把 `max_tokens` **写死为 8192**，且**无任何环境变量可调**。
推理模型对 x2strategy 的 Layer-2 提示词要吐约 8k–13k token 的思维链，8192 被推理耗尽、
**正文块未产出** → `message.content = None` → `_parse_json_response(None)` 抛
`AttributeError`（未被其自带 retry 覆盖，retry 只 catch `ValueError/JSONDecodeError`）。

实测反证：把预算放大到 32000/393216 后，同一提示词正常产出合法 JSON。
另测：`effort=low`（经 `extra_body`）**无效**，推理仍约 8000 token。

## 2. 改动清单（共 4 处，全部为 `max_tokens` 上限；签名与返回不变）

| # | 文件 | 原行 | 改前 | 改后 |
| --- | --- | --- | --- | --- |
| 1 | `paper2spec/extractor.py:409` | `_extract_single_call` | `max_tokens=8192` | `max_tokens=393216` |
| 2 | `paper2spec/extractor.py:423` | `_call_llm_json`（Layer 2–4 用） | `max_tokens=8192` | `max_tokens=393216` |
| 3 | `paper2spec/llm.py:44` | `chat()` 默认值 | `max_tokens: int = 16384` | `max_tokens: int = 393216` |
| 4 | `paper2spec/llm.py:68` | `achat()` 默认值 | `max_tokens: int = 16384` | `max_tokens: int = 393216` |

每处均带 `# PATCHED(x2strategy@0.4.0)` 注释。`393216` = 该中转模型公布的 `max_output_tokens`（上限，非预占）。

## 3. 配套配置改动（不属于对 x2strategy 的修改）

`config/llm.toml`：`model = "claude-sonnet-4-5"` → `model = "deepseek-v4-pro"`。
原因：`claude-sonnet-4-5` 在本中转**不存在**，请求会被**静默回落**到 `deepseek-flash`；改为中转实际提供的模型名。

## 4. 文件哈希（改动前 → 改动后）

| 文件 | 改动前 sha256 | 改动后 sha256 |
| --- | --- | --- |
| `paper2spec/extractor.py` | `b4a0470d7287ec5083988131700994af07f5a130f1b7a56b6dfc8b38eee6d2a0` | `860b5e0eec479a9d557742c3b48c708eed0d94fa736bfda56fbf8fb0ee7f50ab` |
| `paper2spec/llm.py` | `f936f6ecea7b1bb955df0f41495bebf45da0c3806c1ac9977c80f705369b27eb` | `e8c690d1cfd4d6a2cc6987ddbf5a716154134f4017ca4a27bd2e4b4a4606a1ff` |

## 5. 补丁的脆弱性（重建环境会丢）

改的是 `.venv/Lib/site-packages/` 里的副本。**`uv sync` / 重装 `x2strategy` 会还原为原版 8192**，
届时 Layer 2 会再次崩溃。若需恢复原版，用上表"改动前"哈希核对，或重装：
`uv sync --project envs/x2 --reinstall-package x2strategy`。

## 6. 复现命令

```powershell
# 通道自检（不打印 key）
uv run --project envs/x2 python envs/x2/_llm_probe.py
# 跑 Parse + Extract（首次不加 --reuse-content）
uv run --project envs/x2 python envs/x2/run_paper2spec.py "papers/多资产 ETF 轮动策略：固收+视角下动态组合管理的构建与实践.pdf"
```
