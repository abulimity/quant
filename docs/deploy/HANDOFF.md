# 交接手册（HANDOFF）

> **一次性进度快照**，用于新会话接续执行。
> 与 `CLAUDE.md` 的分工：`CLAUDE.md` 是**长期约定**（每个会话都适用），本文件是**当前进度**（完成后即失效）。
> 更新时间：2026-09-29

---

## 1. 一句话状态

**P1（仓库骨架与环境隔离）已完成并通过 Gate P1（无豁免项）；下一步执行 P2（数据层）。**
（更新时间：2026-09-29，P1 完成时）

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
| 网络 | 未验证数据接口可用性；**`raw.githubusercontent.com` 在本机不可达**（litellm 成本表拉取超时） |
| **环境边界**（P1 定） | core（根）/ `envs/vbt` / `envs/x2`，**各自独立 `uv.lock`**；**backtrader 归 core**，未建 `envs/btrader` |
| 三环境 Python | 均 **3.12.13**（起点即通过，无需下调） |
| ⚠️ 外来环境变量 | 手工跑 uv 前**必须**清除 `UV_PROJECT_ENVIRONMENT` 与 `VIRTUAL_ENV`（外部工具「Agents Anywhere」注入，会把环境建到错误位置）；`src/quantlab/engines/bridge.py` 已自动清洗 |
| x2strategy 导入名 | 发行名 `x2strategy`，但**可导入模块是 `paper2spec` / `spec2code`**（**没有** `x2strategy` 模块） |

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

## 5. 已完成（P0 / P1）

**P0（环境地基）**

- ✅ P0.1 系统与工具核查 —— 除长路径外全部通过
- ✅ P0.2 目录骨架（`config / data / docs / runs / src / tests` + `data\{bronze,silver,gold}`）、`EVIDENCE.md` 建立
- ✅ P0.3 `config/sources.yaml`（空模板，无明文密钥）、`.gitignore`
- ⚠️ 长路径 **人工豁免**；Defender 排除 `SKIPPED(no admin)`
- 🚦 **Gate P0 通过（含 1 项显式豁免）**

**P1（仓库骨架与环境隔离）**

- ✅ P1.1 `git init` + 首提交（`40de546`）；`git check-ignore -v .env` / `data/` / `runs/` 三条均命中（P0.3 遗留项已补验）
- ✅ P1.2 core 环境（Python 3.12.13；pyarrow/duckdb/bt/exchange-calendars 等；**backtrader 单独试装无冲突 → 留在 core**）；三个日历可用且覆盖十年
- ✅ P1.3 `envs/vbt`（vectorbt 1.1.0，**固定 `plotly<7`**）、`envs/x2`（x2strategy 0.4.0 + litellm 1.102.0）；**环境边界表已留证**
- ✅ P1.4 三份 `probe.py`（core/vbt/x2）全绿：`import_ok` / `duckdb_read_write` / `spawn_guard_ok`
- ✅ P1.5 `src/quantlab/engines/bridge.py` + 两个 `entry.py` 桩：跨环境调用成功、**失败可传播**、锁哈希一致
- 🚦 **Gate P1 通过（无豁免项）**；干净目录 `uv sync --locked` 三环境 + 三探针全通过

全部证据见 `docs/deploy/EVIDENCE.md`。

---

## 6. 下一步：P2（照 `LOCAL_DEPLOYMENT_PLAN.md` 执行，勿凭记忆）

| 步骤 | 要点 |
| --- | --- |
| P2.1 | 数据契约 DDL（`symbols/bars_daily/corporate_actions/fx_rates/trading_calendar/macro_series/fundamentals/ingest_runs`）；幂等 + 主键约束生效 |
| P2.2 | **合成夹具（本阶段核心，先于任何适配器）**：固定种子，内嵌分红/拆分/停牌/退市/晚上市/汇率/已知 buy&hold 答案 |
| P2.3 | DuckDB 只读约定：研究侧 `read_only=True`；**只读连接执行写必须报错**（负向测试） |
| P2.4 | `Source` 协议 + 空骨架适配器；未实现入口抛明确的 `NotImplementedError("VENDOR-TBD")` |
| P2.5 | Ingest 编排 + 快照：临时文件 → 原子替换；中断可恢复、幂等、旧快照不可覆盖 |
| P2.6 | Silver / Gold 清洗与质量校验；**每种注入缺陷都要被捕获**；休市/停牌/失败三态可区分 |
| 🚦 | **Gate P2**：契约 / 夹具 / 存储 / 适配器 / 快照 / 质量 全部满足 |

> P2 的逐条验证项与失败处理**以手册 §P2 为准**，本表仅为导航。P2 专属风险与通用故障处置见手册 §P2 各步「失败处理」与**附录 C**。

---

## 7. 待办 / 未决

| # | 事项 | 状态 |
| --- | --- | --- |
| 1 | 长路径与 Defender 排除（管理员） | 可延后，非阻塞 |
| 2 | 数据供应商配置 | **等人工填写** `config/sources.yaml` |
| 3 | x2strategy 的 LLM 通道（云端 API / 本地 Ollama） | P5 前决定 |
| 4 | P7 定时任务是否启用 | 默认不启用 |
| 5 | `envs/vbt` 的 `plotly<7` 上界（现 6.9.0） | **已人工裁决**；待 vectorbt 上游适配 plotly 7 后可解除 |
| 6 | `litellm.__version__` 不存在 | P5 改用 `importlib.metadata.version("litellm")` |
| 7 | 手工跑 uv 前须清 `UV_PROJECT_ENVIRONMENT`/`VIRTUAL_ENV` | 例行注意（`bridge.py` 已自动清洗） |

---

## 8. 执行纪律（重申，违反会导致返工）

1. **不跳步**：Gate 未通过不得进入下一 Phase。
2. **不放水**：不得为通过验证而注释断言、放宽容差、伪造证据；无法验证时**停下记录并等人工确认**。
3. **留证**：每步命令与输出追加到 `docs/deploy/EVIDENCE.md`，无证据视为未完成。
4. **幂等**：所有步骤可重复执行。
5. **不确定就停**：手册未覆盖的选择，先记录现象与候选方案，**不要自行发明架构**。
