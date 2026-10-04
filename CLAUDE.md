# 项目约定（供 AI agent 阅读）

本地量化研究平台，Windows 11。技术栈：x2strategy + bt + backtrader + vectorbt，DuckDB 存历史数据。

## 执行入口

**`LOCAL_DEPLOYMENT_PLAN.md` 是唯一权威的执行文档。** 所有部署、开发、验证动作以它为准。
开始任何工作前只读这一个文件；它的附录 A–F 已包含业务口径、检查表与验收清单。

## 使用 orca 时的操作规范（强制）

orca 为每个任务建一个 git worktree：`C:\Users\abulimity\orca\workspaces\quant\<任务名>\`。
**工作树是「代码改动」的隔离副本，不是工作区。** 数据、运行产物、虚拟环境是工作区状态，
只存在于主检出 `D:\project\quant`。所有工作树、所有任务（数据抓取、回测、研究）一律遵守：

### 1. 产出物落点（核心规则）

| 产出物 | 正确落点 | 工作树内禁止 |
| --- | --- | --- |
| 代码 / 测试 / 文档 | 在工作树改，**commit 回分支** | — |
| 数据（bronze/silver/gold 快照、warehouse.duckdb） | `D:\project\quant\data\` | 新建工作树 `data/` 并写入 |
| 运行产物（runs、specs、报告） | `D:\project\quant\runs\` | 新建工作树 `runs/` 并写入 |
| 虚拟环境 `.venv`（core/vbt/x2/futu） | `D:\project\quant\.venv`、`D:\project\quant\envs\*\.venv` | `uv sync`/`uv add` 重建 |
| 凭据 | 环境变量 | 写入工作树 |

原因：`data/`、`runs/`、`.venv/` 都是 gitignored，**不会随 commit 回到主检出**，工作树清理即静默丢失；
且重复生成快照破坏「Parquet 是真相、快照唯一、DuckDB 单写多读」纪律。

### 2. 数据/runs/envs 路径显式指向主检出

代码里项目根由 `__file__` 推导，在 orca 工作树里指向**工作树自身**。凡写数据 / 读快照 / 写 run /
跑子环境，必须走统一入口 `quantlab.paths`（`DATA_ROOT` / `RUNS_DIR` / `ENVS_DIR` / `DUCKDB_PATH`），
或先 `$env:QUANT_ROOT = 'D:\project\quant'` 让这些入口指向主检出。

### 3. 跑代码用主检出环境，不重建 .venv

- 跑脚本/测试：`Set-Location 'D:\project\quant'` 后操作，或 `uv run --project D:\project\quant ...`。
- 先清外部注入的环境变量（orca/「Agents Anywhere」会注入 `UV_PROJECT_ENVIRONMENT`/`VIRTUAL_ENV`，
  把 .venv 建到错误位置）：
  `Remove-Item Env:UV_PROJECT_ENVIRONMENT,Env:VIRTUAL_ENV -ErrorAction SilentlyContinue`。
- 工作树里**不要** `uv sync` / `uv add`。

### 4. 脚本归属

- 入库适配器 → `src/quantlab/ingest/adapters/<source>.py`（commit）。
- 一次性探查/驱动脚本 → 主检出对应 `envs/<env>/`，或 commit 到工作树，但**数据仍落主检出**；
  不要散落在工作树根目录。
- 数据抓取产出必须走 `quantlab ingest` 编排（原子替换 + 唯一 snapshot_id），不要手写 Parquet。

### 5. 数据写入并发

DuckDB 单写多读：多个 agent 并发写数据要**串行**（一次只有一个 ingest 进程），不要各写各的工作树 `data/`。

## 不要读的文件

| 路径 | 原因 |
| --- | --- |
| `docs/archive/**` | **归档文献**，仅供追溯许可证/版本来源。执行时阅读纯属浪费时间，**不要打开、不要 Grep** |
| `data/**` | Parquet 数据，体积大。要用数据请通过 `src/quantlab/store` 的接口，不要直接读文件 |
| `runs/**` | 历史运行产物，体积大。需要时按 `run_id` 定向查询，不要全量读取 |

## 关键约定（违反会导致返工）

1. **环境隔离**：每个引擎一个独立 uv 项目，**不使用 uv workspace**（workspace 会合并成单一 `uv.lock`，破坏隔离）。环境之间**只能通过 `job.json` + Parquet 经 subprocess 交换，不得互相 import**。Python 版本按各环境依赖解析结果确定，**可用性优先，不强行统一**。
2. **存储**：**Parquet 是真相，DuckDB 是查询层**。DuckDB 单写多读——研究进程一律 `read_only=True`。原始快照不可原地覆盖，写入用临时文件再原子替换。
3. **供应商留空时用合成夹具**：数据供应商尚未配置，`src/quantlab/fixtures` 的合成夹具是全部验证的载体，不得依赖外部数据源才能跑绿。
4. **时序纪律**：`available_utc`（可用时间）与 `ts`（交易日期）必须分离。所有读取必须限定单一快照，禁止裸 `SELECT *` 叠加多快照。
5. **每阶段有 Gate**：Gate 未通过不得进入下一阶段。**不得为了让验证通过而注释断言、放宽容差或伪造证据**；无法验证时停下来记录并等人工确认。
6. **变更留证**：每步执行的命令与输出追加到 `docs/deploy/EVIDENCE.md`，无证据视为未完成。

## 代码风格

- Python 3.12+；路径用 `pathlib.Path`；文件读写显式 `encoding="utf-8"`。
- 配置用 TOML + 标准库 `tomllib`；凭据一律走环境变量，**不得写入仓库**。
- 测试放 `tests/`，用 `unittest` 或 `pytest`（选一个并全库统一）。
- 所有可执行入口含 `if __name__ == "__main__":` 保护（Windows 是 `spawn`，numba/vectorbt 必需）。

## 文档

- 需要改部署步骤 → 改 `LOCAL_DEPLOYMENT_PLAN.md`，**不要新建第 3 份设计文档**。
- `docs/archive/` 下的文件**不再维护**，不要更新它。
