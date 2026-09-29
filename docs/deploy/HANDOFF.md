# 交接手册（HANDOFF）

> **一次性进度快照**，用于新会话接续执行。
> 与 `CLAUDE.md` 的分工：`CLAUDE.md` 是**长期约定**（每个会话都适用），本文件是**当前进度**（完成后即失效）。
> 更新时间：2026-09-29

---

## 1. 一句话状态

**P0（环境地基）已完成并放行；下一步执行 P1（仓库骨架与环境隔离）。**

---

## 2. 必读（按顺序，只读这三份 + 本文件）

| 文件 | 作用 |
| --- | --- |
| `CLAUDE.md` | 项目约定、关键约束、**禁读清单**（会话启动时自动加载，无需手动读） |
| `LOCAL_DEPLOYMENT_PLAN.md` | **唯一权威执行文档**。所有步骤以它为准，含附录 A–F |
| `docs/deploy/HANDOFF.md` | 本文件 —— 当前进度与下一步 |
| `docs/deploy/EVIDENCE.md` | 已完成的执行证据（新步骤继续往这里追加） |

**不要读**：`docs/archive/**`（归档文献）、`data/**`、`runs/**`。理由见 `CLAUDE.md`。

---

## 3. 环境事实（避免重复探测）

| 项 | 值 |
| --- | --- |
| 系统 | Windows 11，版本 `10.0.26200` |
| 工作目录 | `D:\project\quant` |
| git | `2.54.0.windows.1` ✅ |
| uv | `0.11.26` ✅ |
| D 盘剩余 | ≈ 275 GiB ✅ |
| **管理员权限** | **无** —— 需要管理员的操作会失败，须记录为 `SKIPPED(no admin)` 并交人工 |
| 长路径 | `LongPathsEnabled=0`，**已人工豁免**（`GateP0.long_paths=WAIVED`） |
| 数据供应商 | **留空**，用合成夹具驱动全部验证 |
| 网络 | 未验证数据接口可用性 |

---

## 4. 已定的关键决策（不要重新讨论）

- **四引擎**：x2strategy + bt + backtrader + vectorbt，DuckDB 存历史数据
- **环境隔离**：每引擎独立 uv 项目，**禁用 uv workspace**（会合并成单一 `uv.lock`）；环境间只经 `job.json` + Parquet 用 subprocess 交换，**不得互相 import**
- **Python 版本**：**逐环境按依赖解析结果确定，可用性优先**，不强行统一（起点 3.12）
- **存储**：**Parquet 是真相，DuckDB 是查询层**；单写多读，研究侧 `read_only=True`
- **基准货币**：**支持多基准并存**（默认 CNY），同一次研究可分别以 CNY/USD/HKD 出报告，各自独立不覆盖
- **组件许可**：vectorbt = Apache-2.0 + Commons Clause（仅个人研究，隔离环境）；backtrader = GPL-3.0+
- **年化口径**：按所用日历推导并全局统一，**不写死 365 或 252**

---

## 5. 已完成（P0）

- ✅ P0.1 系统与工具核查 —— 除长路径外全部通过
- ✅ P0.2 目录骨架（`config / data / docs / runs / src / tests` + `data\{bronze,silver,gold}`）、`EVIDENCE.md` 建立
- ✅ P0.3 `config/sources.yaml`（空模板，无明文密钥）、`.gitignore`
- ⚠️ 长路径 **人工豁免**；Defender 排除 `SKIPPED(no admin)`
- 🚦 **Gate P0 通过（含 1 项显式豁免）**

全部证据见 `docs/deploy/EVIDENCE.md`。

---

## 6. 下一步：P1（照 `LOCAL_DEPLOYMENT_PLAN.md` 执行，勿凭记忆）

| 步骤 | 要点 |
| --- | --- |
| P1.1 | `git init` + 首次提交；补验 `git check-ignore -v .env`（P0.3 遗留项） |
| P1.2 | 建立 **core** 环境（起点 Python 3.12）；先装 bt，backtrader **单独试装** |
| P1.3 | 建立 `envs/vbt`（vectorbt）与 `envs/x2`（x2strategy + litellm）；**确定环境边界并留证** |
| P1.4 | 每个环境写 `probe.py`，输出 JSON 探针（含 `spawn_guard_ok`） |
| P1.5 | 实现跨环境桥 `src/quantlab/engines/bridge.py`（文件交换，失败须可传播） |
| 🚦 | **Gate P1**：每环境独立 `uv.lock`、无 workspace、探针全绿、版本与理由留证 |

### 已知风险（P1 专属）

1. **环境边界是全流程返工风险最高处**。按手册 P1.3 的决策规则执行：冲突就拆环境，**不得**为迁就某组件而放宽其版本约束。
2. **`x2strategy` 的包名与安装方式未知**（GitHub 仓库，非 PyPI）。**先读仓库说明**，不要猜包名硬装。
3. **vectorbt 依赖较新**（`pandas>=3.0.3`、`numpy>=2.4.6`），会牵制其 Python 下限。
4. **numba 首次调用慢属正常**，勿误判卡死。
5. 依赖求解失败时，**禁止** `--no-deps`、手工 force install、关闭 TLS 校验。

---

## 7. 待办 / 未决

| # | 事项 | 状态 |
| --- | --- | --- |
| 1 | 长路径与 Defender 排除（管理员） | 可延后，非阻塞 |
| 2 | 数据供应商配置 | **等人工填写** `config/sources.yaml` |
| 3 | x2strategy 的 LLM 通道（云端 API / 本地 Ollama） | P5 前决定 |
| 4 | P7 定时任务是否启用 | 默认不启用 |

---

## 8. 执行纪律（重申，违反会导致返工）

1. **不跳步**：Gate 未通过不得进入下一 Phase。
2. **不放水**：不得为通过验证而注释断言、放宽容差、伪造证据；无法验证时**停下记录并等人工确认**。
3. **留证**：每步命令与输出追加到 `docs/deploy/EVIDENCE.md`，无证据视为未完成。
4. **幂等**：所有步骤可重复执行。
5. **不确定就停**：手册未覆盖的选择，先记录现象与候选方案，**不要自行发明架构**。
