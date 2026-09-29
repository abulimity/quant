# 交接手册（HANDOFF MANUAL）

> **用途**：让任何一位新接手的人（或 AI agent）在**不读历史对话**的前提下，安全地继续这个项目。
> **与 `HANDOFF.md` 的分工**：`HANDOFF.md` 是**一屏进度快照**（知道"到哪了"）；本文件是**完整交接手册**（知道"怎么接、坑在哪"）。
> **与 `CLAUDE.md` 的分工**：`CLAUDE.md` 是**长期约定**（每个会话都适用）；本文件是**当前进度 + 操作入口**（每完成一个 Phase 更新一次）。
> **与 `LOCAL_DEPLOYMENT_PLAN.md` 的分工**：手册是**唯一权威执行文档**；本文件**只做导航，不复制步骤**。
> 最后更新：2026-09-29（**P2 完成时**）

---

## 0. 30 秒上手（TL;DR）

- **项目**：本地量化研究平台（Windows 11）。四引擎 = **x2strategy + bt + backtrader + vectorbt**，DuckDB 存历史数据。
- **位置**：`D:\project\quant`
- **当前进度**：**P0 ✅、P1 ✅、P2 ✅**；**下一步 = P3（契约层）**。
- **数据供应商留空**，全部验证用 `src/quantlab/fixtures` 的**合成夹具**驱动（**P2 已写入**）。
- **三个环境已就绪**：core（根）/ `envs/vbt` / `envs/x2`，各自独立 `uv.lock`，探针全绿。
- **一键验收**：`python -m unittest discover -t . -s tests` → **143 tests OK**。
- **接续工作的第一步**：读 `LOCAL_DEPLOYMENT_PLAN.md` 的 **§P3**，逐条照做、逐步留证。

---

## 1. 必读与禁读

### 1.1 必读（按顺序）

| 顺序 | 文件 | 作用 |
| --- | --- | --- |
| 1 | `CLAUDE.md` | 项目约定、关键约束、**禁读清单**（会话启动自动加载） |
| 2 | `LOCAL_DEPLOYMENT_PLAN.md` | **唯一权威执行文档**（步骤 + Gate + 附录 A–F 业务口径） |
| 3 | `docs/deploy/HANDOFF.md` | 一屏进度快照 —— 当前进度与下一步 |
| 4 | `docs/deploy/HANDOFF_MANUAL.md` | 本文件 —— 完整交接手册（环境事实、本机坑、操作入口） |
| 5 | `docs/deploy/EVIDENCE.md` | 已完成步骤的**命令 + 实际输出 + 判定**台账 |

### 1.2 禁读（会浪费时间或压垮上下文）

| 路径 | 原因 |
| --- | --- |
| `docs/archive/**` | 归档文献，仅供追溯许可证/版本来源，**执行时不需要读** |
| `data/**` | Parquet 数据，体积大。用数据走 `src/quantlab/store` 接口 |
| `runs/**` | 历史运行产物，体积大。按 `run_id` 定向查询 |

> **不要新建第 3 份设计文档。** 要改部署步骤 → 改 `LOCAL_DEPLOYMENT_PLAN.md`。

---

## 2. 项目全景

### 2.1 目标与范围

本地复现券商研究报告：**论文/报告 → 规格（StrategySpec）→ 回测 → 报告**。
市场：中国内地 / 香港 / 美国 ETF；日频；手动运行（**无定时任务**，P7 可选且默认不启用）。
**不在范围**：券商连接、自动下单、实盘、真实资金账本。

### 2.2 依赖顺序（为何是这个顺序）

```text
P0 环境地基 → P1 仓库骨架与环境隔离 → P2 数据层 → P3 契约层
   → P4 引擎适配层 → P5 x2strategy 集成 ↘
                    → P6 组合与报告 → P7 自动化(可选) → P8 验收与固化
```

上层依赖下层；**下层不稳，上层的验证结论无意义**。每 Phase 末尾有 Gate，**不过不得进入下一 Phase**。
> 关键：**先做数据层合成夹具（P2.2），再做引擎适配（P4）** —— 否则三引擎对拍无从验证。

### 2.3 当前目录结构（**P2 结束时**）

```text
D:\project\quant\
  pyproject.toml  uv.lock  .python-version      # core 环境（uv_build 可编辑安装）
  CLAUDE.md  LOCAL_DEPLOYMENT_PLAN.md  .gitignore
  config\sources.yaml                            # 供应商配置（空模板，待人工填）
  docs\archive\OPEN_SOURCE_COMPARISON.md         # 归档，勿读
  docs\deploy\{EVIDENCE.md, HANDOFF.md, HANDOFF_MANUAL.md}
  envs\vbt\ { pyproject.toml, uv.lock, probe.py, entry.py }   # vectorbt 隔离环境
  envs\x2\  { pyproject.toml, uv.lock, probe.py, entry.py }   # x2strategy + litellm
  src\quantlab\
    __init__.py  probe.py  cli.py                # cli: `quantlab ingest` / `quantlab schema`
    engines\bridge.py                            # 跨环境桥（core 侧）
    store\   { schema.sql, migrate.py, db.py, snapshot_guard.py,
               warehouse.py, atomic.py, canonical.py }          # ← P2 新增
    fixtures\{ spec.py, synth.py }                                # ← P2 新增（合成夹具）
    ingest\  { base.py, orchestrator.py, adapters\{akshare,yfinance,macro_fred,synthetic,_util}.py }
    quality\ { clean.py, checks.py }                              # ← P2 新增（Silver/Gold + 校验）
  data\  bronze\synthetic\<snapshot_id>\*.parquet   # 夹具快照（gitignored，可重建）
         silver\ gold\                              # P2 已产出 gold **视图**（尚未落盘）
         warehouse.duckdb                           # DuckDB 台账（派生，可重建）
  runs\                                            # 运行产物（gitignored）
  tests\  { __init__.py, helpers.py,
            test_p2_1_contract.py, test_p2_2_fixtures.py, test_p2_3_store.py,
            test_p2_4_adapters.py, test_p2_5_ingest.py, test_p2_6_quality.py }
```

> P3–P6 还将新增 `src/quantlab/{contract,portfolio,eval,registry}`；届时目录约定见手册 §3.3。

---

## 3. 环境布局（P1 定案，**不要重开**）

### 3.1 三环境事实表

| 环境 | 目录 | Python | 核心包（实测版本） | 用途 |
| --- | --- | --- | --- | --- |
| **core** | `D:\project\quant`（根） | **3.12.13** | pandas 3.0.6 / numpy 2.5.3 / pyarrow 25.0.1 / **duckdb 1.5.5** / **bt 1.2.3** / **backtrader 1.9.78.123** / exchange-calendars 4.13.2 / matplotlib 3.11.2 / jupyterlab 4.6.3 / ipykernel 7.3.0 / ffn 1.2.2 / yfinance 1.7.0 | 主环境：数据、研究、组合、报告；backtrader 也在此 |
| **vbt** | `envs\vbt` | **3.12.13** | **vectorbt 1.1.0** / numba 0.67.0 / **plotly 6.9.0（固定 `<7`）** / pandas 3.0.6 / numpy 2.5.3 / duckdb 1.5.5 | vectorbt 粗筛（**必须独立**） |
| **x2** | `envs\x2` | **3.12.13** | **x2strategy 0.4.0**（模块 `paper2spec`/`spec2code`）/ **litellm 1.102.0** / backtrader 1.9.78.123 / pymupdf 1.28.2 / pandas 3.0.6 / numpy 2.5.3 / duckdb 1.5.5 | 论文→规格→代码 |

**没有 `envs/btrader`** —— backtrader 与 core 无冲突，按手册决策规则保留在 core。

### 3.2 为什么这样分（决策规则，违反会返工）

- **禁用 uv workspace**：workspace 会解析出**单一** `uv.lock`，破坏隔离。**每环境一份独立 `uv.lock`**（现为 3 份）。
- **环境之间不得互相 import**，只经 **`job.json` + Parquet 用 subprocess** 交换（§3.3）。
- **Python 版本可用性优先**：逐环境由依赖解析决定，**不强行统一**（本机三者恰好都是 3.12.13）。
- vectorbt **必须独立环境**（许可 Apache-2.0 + Commons Clause，且依赖 pandas≥3.0.3/numpy≥2.4.6）。

### 3.3 跨环境调用怎么走（已实现）

```text
core(bridge.run_in_env) → 写 job.json(+输入 Parquet) → uv run --project envs/<env> python envs/<env>/entry.py job.json
目标环境                 → 读 job.json → 执行 → 写 result.json（+可选 result.parquet）
core                    → 读 result.json 继续；失败抛 BridgeError（非零退出码 + stderr，不静默吞）
```

`job.json` 字段：`job_id / env / engine / entry / params / inputs / outputs / env_lock_sha256`；结果落 `runs/<job_id>/`。

---

## 4. ⚠️ 本机特有的坑（**先看这节，能省几小时**）

| # | 坑 | 现象 | 处置 |
| --- | --- | --- | --- |
| 1 | **外部工具「Agents Anywhere」注入 `UV_PROJECT_ENVIRONMENT` / `VIRTUAL_ENV`** | `uv add` 报 `另一个程序正在使用此文件`；更危险的是**所有环境会被指到同一个外部 `.venv`**，隔离失效 | 手工跑 uv 前先执行 `Remove-Item Env:UV_PROJECT_ENVIRONMENT,Env:VIRTUAL_ENV -ErrorAction SilentlyContinue`。`src/quantlab/engines/bridge.py` 已在子进程里自动清洗 |
| 2 | **`uv init` 会把子项目并入 workspace** | 根 `pyproject.toml` 多出 `[tool.uv.workspace]`，且只剩 1 份 `uv.lock` | 建子环境**必须加 `--no-workspace`**：`uv init --bare --no-workspace --python 3.12 envs/<name>`。**事后检查**：`Select-String pyproject.toml 'tool.uv.workspace'` 应无命中 |
| 3 | **vectorbt × plotly 7 不兼容** | `import vectorbt` 报 `Bad property path: scattermapbox` | 已在 `envs/vbt` 固定 `plotly<7`（现 6.9.0）。上游修好前**不要**解除 |
| 4 | **x2strategy 没有 `x2strategy` 模块** | `import x2strategy` → `ModuleNotFoundError`（符合预期） | 用真实模块名 `import paper2spec, spec2code` |
| 5 | `litellm` 无 `__version__` | `litellm.__version__` → `AttributeError` | 用 `importlib.metadata.version("litellm")` |
| 6 | `raw.githubusercontent.com` 在本机**不可达** | litellm 拉模型成本表超时（自动回退本地备份，不影响导入） | P5 若需在线 LLM，先确认网络/代理，或走本地 Ollama |
| 7 | **无管理员权限** | Defender 排除、长路径开关会失败 | 记为 `SKIPPED(no admin)` / `WAIVED`，交人工；**不要**试图绕过 |
| 8 | backtrader 导入打印 `SyntaxWarning: invalid escape sequence '\*'` | 出现在 **stderr** | **无害**，不影响导入与退出码，无需处理 |
| 9 | numba 首次调用慢 | vectorbt 首次 JIT 编译耗时 | **正常**，勿判为卡死；给足超时 |
| 10 | `uv` 提示 `Failed to hardlink files; falling back to full copy` | 缓存(C盘)与项目(D盘)跨文件系统 | **无害**，仅速度提示 |
| 11 | **DuckDB 写者对其它进程独占** | 写者持有时**连只读也打不开**（`IOException: 另一个程序正在使用此文件`）。故「单写多读」**仅在无活跃写者时成立** | 这正是「Parquet 是真相、DuckDB 是查询层」的理由：ingest 写 Parquet，查询层在**无写者**时读。**不要**靠重试掩盖 |
| 12 | **同进程内不得对同一库文件持有配置不同的连接** | RO 与 RW 混用 → `ConnectionException: Can't open a connection to same database file with a different configuration` | 传 `con=` 复用同一连接（`orchestrator` 已支持连接注入）；测试里读台账也要用**同配置**连接 |
| 13 | 测试含中文断言信息，子进程输出按本地编码 | Windows 上 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xc1` | 跑测试前设 `PYTHONIOENCODING=utf-8`（`EVIDENCE.md` 的命令已固定该设置） |
| 14 | `Series` 没有 `.date`（只有 `.dt.date`），`DatetimeIndex` 才有 | `AttributeError: 'Series' object has no attribute 'date'` | 统一转成 `DatetimeIndex` 再用 `index.date` |
| 15 | 公司行动除权日若**不在**交易日网格上会被**静默丢弃** | 「已知答案」变成假证据 | 生成器已 **fail-closed**：除权日必须唯一命中交易日，否则报错 |
| 16 | 汇率归一在同 ts 双方向时会**折叠成重复行** | 凭空复制一份汇率且不报错 | 已 **fail-closed**：检测到撞车即抛 `FxDirectionError` |
| 17 | `uv fsync` 对**只读**句柄在 Windows 上失败 | `OSError: [Errno 9] Bad file descriptor` | 用 `open(tmp, "rb+")`（可写句柄）再 `fsync` |
| 18 | 拆分复权乘法因子**极易写反** | 前复权错写成 `F[-1]/F[t]` → 除权前价格被放大 → 假跳空 | 代码内以 4:1 的具体数字锚定方向；测试断言 `raw_ratio/adj_ratio == ratio` |

---

## 5. 已完成（P0 / P1 / P2）与 Gate 结论

### P0 环境地基 — 🚦 **通过（含 1 项显式豁免）**

- P0.1 工具/系统核查：git `2.54.0.windows.1` ✅、uv `0.11.26` ✅、D 盘剩余 ≈275 GiB ✅
- P0.2 目录骨架 + `EVIDENCE.md` 建立；Defender 排除 `SKIPPED(no admin)`
- P0.3 `config/sources.yaml`（空模板，**无明文密钥**）、`.gitignore`
- ⚠️ **`LongPathsEnabled=0` → `GateP0.long_paths=WAIVED`（人工豁免）**。**豁免 ≠ 通过**，不得解读为该检查已过

### P1 仓库骨架与环境隔离 — 🚦 **通过（无豁免项）**

| 步骤 | 结果 |
| --- | --- |
| P1.1 | `git init` + 首提交 `40de546`；`git check-ignore -v` 对 `.env`/`data/`/`runs/` 三条均命中（P0.3 遗留项已补验） |
| P1.2 | core 环境建立；`duckdb 1.5.5` 与既有观察值一致；三个日历 XSHG/XHKG/XNYS 可用，历史自 **2006-09-29** 起（覆盖十年）；backtrader **单独试装无冲突** |
| P1.3 | `envs/vbt` / `envs/x2` 建立；**环境边界表已留证**；**3 份独立 `uv.lock`**、**0 处 `[tool.uv.workspace]`** |
| P1.4 | 三份 `probe.py` 全绿（`import_ok` / `duckdb_read_write` / `spawn_guard_ok`），退出码 0 |
| P1.5 | `bridge.py` + 两个 `entry.py` 桩：跨环境调用成功、**失败可传播**（`returncode=1` 且错误信息保留）、锁哈希一致 |
| 复现 | `git archive` 干净树 → `uv sync --locked`（三环境）+ 三探针**全部通过** |

**Git 提交**

```text
40de546  chore: initial docs and skeleton
f7cfdb6  chore(p1): core/vbt/x2 isolated envs, probes, cross-env bridge
cb73878  docs(deploy): record P1 evidence and Gate P1 verdict; update handoff
```

> **全部证据**（每条命令 + 实际输出 + 逐项判定）见 `docs/deploy/EVIDENCE.md`。**无证据的步骤视为未完成。**

### P2 数据层 — 🚦 **通过（无豁免项）**

| 步骤 | 结果 |
| --- | --- |
| P2.0 | 根项目改为 `uv_build` 可编辑安装（否则 `import quantlab` 不可用，`tests/` 与 CLI 都跑不起来）；`uv.lock` **仅 1 行变化**，依赖零漂移 |
| P2.1 | `store/`：`schema.sql`（9 表，幂等）、`migrate.py`（含漂移检测）、`db.py`、`snapshot_guard.py`（防快照叠加哨兵）；5/5 事实表含 `available_utc`+`snapshot_id` |
| P2.2 | `fixtures/spec.py` + `synth.py`：**9 标的 / 7 表 / 18941 根 bar**；内嵌分红·拆分·停牌·退市·晚上市·汇率·**下载失败**；解析净值 vs 重算 `max\|Δ\|≈6e-15`；跨进程哈希一致 |
| P2.3 | `store/warehouse.py`：零拷贝视图 + 物化装载；只读拒写且**数据未变**；第二个写进程被拒并获得可操作报错 |
| P2.4 | `ingest/base.py` + 三个骨架适配器；**延迟导入实测**（导入后 SDK 不在 `sys.modules`）；未实现入口抛 `VENDOR-TBD` |
| P2.5 | `ingest/orchestrator.py` + `cli.py`：原子替换、幂等、**硬杀可恢复**；`ingest_runs` 三态 `running→ok\|aborted` |
| P2.6 | `quality/`：8 类注入缺陷逐条被捕获；**三态互异**；复权/汇率手算吻合；gold 回测输入视图 |
| 自动化 | **143 tests OK**（`python -m unittest discover -t . -s tests`） |

**Git 提交**

```text
40de546  chore: initial docs and skeleton
f7cfdb6  chore(p1): core/vbt/x2 isolated envs, probes, cross-env bridge
cb73878  docs(deploy): record P1 evidence and Gate P1 verdict; update handoff
3ebe386  docs(deploy): add expanded handoff manual (keep HANDOFF.md as progress snapshot)
（P2 改动**尚未提交** —— 本次未收到提交指令）
```

> **P2 对手册 DDL 有 3 处偏离**（`ingest_runs` 主键、`corporate_actions`/`fundamentals` 增
> `available_utc`），理由与可回退说明见 `EVIDENCE.md` §P2.1 / §P2.5，**待人工确认**。

---

## 6. 已定的关键决策（**不要重新讨论**）

| 决策 | 结论 |
| --- | --- |
| 引擎 | 四引擎并存：x2strategy + bt + backtrader + vectorbt；DuckDB 存历史数据 |
| 环境隔离 | 每引擎独立 uv 项目，**禁用 workspace**；环境间只经文件 + subprocess 交换 |
| Python 版本 | **逐环境按解析结果定，可用性优先**，不强行统一（起点 3.12） |
| 存储 | **Parquet 是真相，DuckDB 是查询层**；单写多读，研究侧 `read_only=True`；原始快照不可原地覆盖 |
| 时序纪律 | `available_utc`（可用时间）与 `ts`（交易日期）**必须分离**；所有读取限定单一快照 |
| 基准货币 | **多基准并存（默认 CNY）**，同一研究可分别以 CNY/USD/HKD 出报告，各自独立不覆盖 |
| 汇率口径 | 内部统一为「1 单位原币 = 多少**基准货币**」；反向输入取倒数并留记录 |
| 成交口径 | 显式统一「T 日收盘出信号 → T+1 开盘成交」；**不得依赖任何引擎默认行为** |
| 年化口径 | 按所用日历推导并**全局统一**，不写死 365 或 252 |
| 许可 | vectorbt = Apache-2.0 + Commons Clause（仅个人研究、隔离环境）；backtrader = GPL-3.0+ |
| P7 定时任务 | **默认不启用**；启用属范围变更，须人工确认 |
| 验收载体 | 供应商接入前，**合成夹具**为全部验证载体，不得依赖外部数据源才能跑绿 |

---

## 7. 常用命令速查（PowerShell）

```powershell
Set-Location 'D:\project\quant'
# 先清外来变量（见 §4 坑 1）——强烈建议每次手工操作前执行
Remove-Item Env:UV_PROJECT_ENVIRONMENT,Env:VIRTUAL_ENV -ErrorAction SilentlyContinue

# —— 环境健康（一条命令判定）——
uv run python src/quantlab/probe.py                      # core
uv run --project envs/vbt python envs/vbt/probe.py       # vbt
uv run --project envs/x2  python envs/x2/probe.py        # x2

# —— 跨环境桥自检（成功 + 失败传播 + 锁哈希）——
uv run python src/quantlab/engines/bridge.py             # 期望末行 SELFTEST OK

# —— 全量验收（P2 起；必须先设编码，否则中文断言信息会 UnicodeDecodeError）——
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m unittest discover -t . -s tests        # 期望：Ran 143 tests OK

# —— 数据层（P2）——
.\.venv\Scripts\python.exe -m quantlab.fixtures.synth --out data/bronze/synthetic   # 生成夹具快照
.\.venv\Scripts\python.exe -m quantlab.cli ingest --source synthetic --universe fixture
.\.venv\Scripts\python.exe -m quantlab.cli schema --print          # 人工核对契约 DDL

# —— 重建环境（幂等）——
uv sync --locked
uv sync --locked --project envs/vbt
uv sync --locked --project envs/x2

# —— 干净目录复现性验证 ——
git archive --format=tar -o "$env:TEMP\quant_clean\repo.tar" HEAD
tar -xf "$env:TEMP\quant_clean\repo.tar" -C "$env:TEMP\quant_clean"
#   然后在新目录对三环境各跑一次 uv sync --locked

# —— JupyterLab（详见手册 F.12）——
uv run jupyter lab --ServerApp.ip=127.0.0.1 --ServerApp.port=8888 --ServerApp.open_browser=False
```

> **注意**：`git archive | tar` 经 PowerShell 管道会破坏二进制流，**必须**用 `git archive -o <文件>` 再 `tar -xf`。

---

## 8. 下一步：P3 · 契约层

**前置：Gate P2 已通过 ✅。** 逐条步骤**以 `LOCAL_DEPLOYMENT_PLAN.md` §P3 为准**，下表仅导航：

| 步骤 | 要点 |
| --- | --- |
| P3.1 | 契约类型：`Signals` / `TargetWeights` / `StrategySpec` / `CostModel`；`StrategySpec` 可 JSON 往返 |
| P3.2 | 校验器 **fail-closed**：拒绝行和 > 1、拒绝负权重（只做多默认）、拒绝含未来日期 |
| P3.3 | **未来扰动测试**：修改信号时刻**之后**的数据，不得改变此前已产生的信号与订单 |

**P2 已交付、P3 可直接复用的东西**：

- `quantlab.fixtures.synth.generate()` —— 确定性夹具（含已知答案），P3 的扰动测试用它最方便
- `quantlab.store.snapshot_guard.assert_single_snapshot()` —— 防快照叠加哨兵
- `quantlab.quality.clean.gold_backtest_view()` —— 已带 `traded` 掩码与 `available_utc` 的回测输入
- 测试基座：`python -m unittest discover -t . -s tests`（**记得先设 `PYTHONIOENCODING=utf-8`**）

**P3 的常见坑**：

- 未来函数是最隐蔽的返工源 → 判据是**行为**（改未来不动过去），不是「有没有写 `shift(1)`」。
- 契约校验要在**入口**就 fail-closed，别留给下游去猜。

---

## 9. 待办 / 未决 / 需人工确认

| # | 事项 | 状态 / 时机 |
| --- | --- | --- |
| 1 | 长路径开关 + Defender 排除（需**管理员**） | 可延后，非阻塞；命令见 `EVIDENCE.md` 文末 |
| 2 | 数据供应商配置 | **等人工填写** `config/sources.yaml`（附录 A 模板） |
| 3 | x2strategy 的 LLM 通道（云端 API / 本地 Ollama） | **P5 前**决定（附录 D-5） |
| 4 | P7 定时任务是否启用 | 默认不启用；启用须人工确认 |
| 5 | `envs/vbt` 的 `plotly<7` 上界 | **已人工裁决**；待 vectorbt 上游适配 plotly 7 后解除 |
| 6 | 每组件的许可确认（vectorbt / backtrader / DuckDB） | 附录 D-1~3，执行前逐条确认 |
| 7 | 夹具驱动验收的取舍是否认可 | 附录 D-6 |
| 8 | **P2 对手册 DDL 的 3 处偏离**：① `ingest_runs` 主键 → `(snapshot_id, dataset)`；② `corporate_actions` 增 `available_utc`；③ `fundamentals` 增 `available_utc` | **待确认**（理由见 `EVIDENCE.md` §P2.1/§P2.5）。如不认可请指示回改 |
| 9 | 质量阈值 `JUMP_SIGMA=8` / `JUMP_FLOOR=0.15` / `FX_STALE_DAYS=10` | 首次设定，**未用真实数据校准**；接入供应商后应重新标定 |
| 10 | **P2 全部改动尚未 git 提交** | 待你确认后提交（本次未收到提交指令） |
| 11 | 夹具的"可用时间缓冲"（交易所收盘 + 15~30 分钟）是**声明假设** | F.2 要求披露：**不是**严格 point-in-time | 报告须标注；接入真实供应商后替换为真实发布时刻 |

---

## 10. 执行纪律（重申；违反会导致返工）

1. **不跳步** —— Gate 未通过不得进入下一 Phase。
2. **不放水** —— 不得为通过验证而注释断言、放宽容差、伪造证据；**无法验证时停下记录并等人工确认**。
3. **留证** —— 每步「命令 + 实际输出 + 判定」追加到 `docs/deploy/EVIDENCE.md`；**无证据视为未完成**。
4. **幂等** —— 所有步骤可重复执行，不产生副作用。
5. **不确定就停** —— 手册未覆盖的选择，先记录现象与候选方案，**不要自行发明架构**。
6. **禁止手段** —— `--no-deps`、手工 force install、关闭 TLS 校验、靠重试掩盖锁冲突，一律禁止。

---

## 11. 索引

| 想看什么 | 去哪里 |
| --- | --- |
| 每步的命令与真实输出 | `docs/deploy/EVIDENCE.md` |
| 一屏进度（到哪了） | `docs/deploy/HANDOFF.md` |
| 权威步骤 / Gate / 业务口径 | `LOCAL_DEPLOYMENT_PLAN.md`（正文 + 附录 A–F） |
| 长期约定 / 禁读清单 | `CLAUDE.md` |
| 故障处置速查 | 手册 **附录 C** |
| 待人工确认项 | 手册 **附录 D** |
| 最小验收清单 | 手册 **附录 F.9** |
