# 项目约定（供 AI agent 阅读）

本地量化研究平台，Windows 11。技术栈：x2strategy + bt + backtrader + vectorbt，DuckDB 存历史数据。

## 执行入口

**`LOCAL_DEPLOYMENT_PLAN.md` 是唯一权威的执行文档。** 所有部署、开发、验证动作以它为准。
开始任何工作前只读这一个文件；它的附录 A–F 已包含业务口径、检查表与验收清单。

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
