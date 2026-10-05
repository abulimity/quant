# 交接手册（HANDOFF MANUAL）

> **用途**：让任何一位新接手的人（或 AI agent）在**不读历史对话**的前提下，安全地继续这个项目。
> **与 `HANDOFF.md` 的分工**：`HANDOFF.md` 是**一屏进度快照**（知道"到哪了"）；本文件是**完整交接手册**（知道"怎么接、坑在哪"）。
> **与 `CLAUDE.md` 的分工**：`CLAUDE.md` 是**长期约定**（每个会话都适用）；本文件是**当前进度 + 操作入口**（每完成一个 Phase 更新一次）。
> **与 `LOCAL_DEPLOYMENT_PLAN.md` 的分工**：手册是**唯一权威执行文档**；本文件**只做导航，不复制步骤**。
> 最后更新：2026-09-29（**P4 完成时**）

---

## 0. 30 秒上手（TL;DR）

- **项目**：本地量化研究平台（Windows 11）。四引擎 = **x2strategy + bt + backtrader + vectorbt**，DuckDB 存历史数据。
- **位置**：`D:\project\quant`
- **当前进度**：**P0 ✅、P1 ✅、P2 ✅、P3 ✅、P4 ✅**；**下一步 = P5 / P6**。
- **数据供应商留空**，全部验证用 `src/quantlab/fixtures` 的**合成夹具**驱动（P2 已写入）。
- **三个环境已就绪**：core（根）/ `envs/vbt` / `envs/x2`，各自独立 `uv.lock`，探针全绿。
- **一键验收**：`python -m unittest discover -t . -s tests` → **268 tests OK**。
- **三引擎对拍已过**：A/B/C 场景 reference ↔ backtrader 均 ~1e-16；bt 差异已证明属成交时点。
- **接续工作的第一步**：读 `LOCAL_DEPLOYMENT_PLAN.md` 的 **§P5 / §P6**，逐条照做、逐步留证。
- ⚠️ **开工前先看**：附录 F.4.4/F.8 与正文 §P4.5 的**成交口径冲突**（收盘 vs T+1 开盘），
  本次按「以正文为准」取 **T+1 开盘** —— 详见 `HANDOFF.md` §7 第 11 项。

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

### 2.3 当前目录结构（**P4 结束时**）

```text
D:\project\quant\
  pyproject.toml  uv.lock  .python-version      # core 环境（uv_build 可编辑安装）
  CLAUDE.md  LOCAL_DEPLOYMENT_PLAN.md  .gitignore
  config\sources.toml                            # 供应商配置（声明式，接线见附录 A）
  docs\archive\OPEN_SOURCE_COMPARISON.md         # 归档，勿读
  docs\deploy\{EVIDENCE.md, HANDOFF.md, HANDOFF_MANUAL.md}
  envs\vbt\ { pyproject.toml, uv.lock, probe.py, entry.py,
              _spawn_guard_demo.py }             # vectorbt 隔离环境；entry 支持 op=scan
  envs\x2\  { pyproject.toml, uv.lock, probe.py, entry.py }   # x2strategy + litellm
  src\quantlab\
    __init__.py  probe.py  cli.py                # cli: `quantlab ingest` / `quantlab schema`
    store\   { schema.sql, migrate.py, db.py, snapshot_guard.py,
               warehouse.py, atomic.py, canonical.py }          # P2：存储层 + 跨环境桥
    fixtures\{ spec.py, synth.py }                                # ← P2 新增（合成夹具）
    ingest\  { base.py, orchestrator.py, adapters\{akshare,yfinance,macro_fred,synthetic,_util}.py }
    quality\ { clean.py, checks.py }                              # P2：Silver/Gold + 质量校验
    contract\{ types.py, lint.py, emit.py }                       # P3：契约 / 闸门 / 发射器
    engines\ { bridge.py, base.py, execution.py,                  # P4：引擎适配层
               reference.py, backtrader_runner.py, bt_runner.py }
  data\  bronze\synthetic\<snapshot_id>\*.parquet   # 夹具快照（gitignored，可重建）
         silver\ gold\                              # gold 目前是**视图**（尚未落盘）
         warehouse.duckdb                           # DuckDB 台账（派生，可重建）
  runs\                                            # 运行产物（gitignored）
  tests\  { __init__.py, helpers.py,
            test_p2_*.py, test_p3_contract.py,
            test_p4_reference.py, test_engine_parity.py, test_p4_vbt_bridge.py }
```

> P5–P6 还将新增 `src/quantlab/{x2,portfolio,eval,registry}`；届时目录约定见手册 §3.3。

### 2.4 统一成交口径（**P4 定案，改它要先想清楚**）

```text
信号在 T 日收盘生成  →  T+1 开盘价成交  →  T+1 不可交易则顺延到下一个可交易会话
```

**口径来自正文 §P4.5**（附录 F.4.4/F.8 写的是「收盘」，两处冲突 → 手册规定以正文为准）。
实现集中在 **`src/quantlab/engines/execution.py`**（`MatchEngine` / `run_reference`），
它是**语义真值**；`reference` runner 直接复用它；backtrader runner 已对齐到 ~1e-16。
**bt** 因 API 限制实际落在 **T+1 收盘**，该差异已记录并**证明**归因（见 `parity_report.md`）。

> 想改成 F.4 的收盘口径：给 `run_reference(..., fill_at="close")` 即可（诊断模式已内置），
> 但**必须同步重跑对拍**，并更新 `parity_report.md` 与本节。

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
| 19 | **信号是事件，持仓是状态** —— 二者混淆是**静默**的 | 若要求「调仓日当天恰好 +1」，周中金叉被整条丢掉 → 事件型策略**永远空仓**：收益恒 0、不报错、不崩溃 | 先过 `emit.position_state()` 归约（`+1` 建仓 / `−1` 平仓 / `0`·`NaN` **维持前值**），再取调仓日；已单列用例钉住 |
| 20 | 手算基准用 `~above.shift(1).fillna(False)` 会凭空造出**幻影穿越** | 把「前一日状态未知」当成「前一日在下方」，首个可评估日多出一次穿越 | 改用 `fast.shift(1) <= slow.shift(1)`（NaN 参与比较恒为 False） |
| 21 | 「每周调仓」**不等于**「间隔恰好 7 天」 | 周一休市则决策顺延到周二 → 相邻两次可只隔 6 天；按 `gap >= 7` 断言会误报 | 正确判据是「每个自然周至多一次」（`isocalendar` 去重） |
| 22 | 闸门规则要能**递归**遍历 `Expr` 树 | `shift` 若在深层嵌套里，只查顶层会漏判未来函数 | `Expr.walk()` 深度优先；用例专门覆盖「深层嵌套的 shift 仍生效」与「`shift(0)` 不算解除」 |
| 23 | **`envs/vbt` 没有 parquet 引擎** | 桥递过去的输入 Parquet 直接 `ImportError` —— 跨环境交换**跑不通** | 用该环境**已有**的 `duckdb` 读写 Parquet；**不要**为读一个文件去动那个隔离环境的依赖求解 |
| 24 | **减仓到非零目标会被静默忽略** | 只在目标权重 = 0 时才卖 → 「1.0 减到 0.25」持仓**完全不动**、不报错；backtrader 侧则 `min(-delta,current)` → **清成 0** | 按**目标份额**卖（含减仓）；只测 buy&hold 与清仓**永远发现不了**，必须有「部分减仓」用例 |
| 25 | spawn 演示别指望 `multiprocessing` 自己拦 | 实测它**挂住**（既不报错也不退出）→ 用例超时，无法作为证据 | 用**继承的深度计数**（spawn 继承环境变量）让失败确定且毫秒级 |
| 26 | **`bt` 会传递性 import `yfinance`** | 在 core 进程里断言「SDK 不在 sys.modules」会**假阳性** | 凡「延迟导入」类断言，一律放到**独立子进程**里验证 |

---

## 5. 已完成（P0 / P1 / P2）与 Gate 结论

### P0 环境地基 — 🚦 **通过（含 1 项显式豁免）**

- P0.1 工具/系统核查：git `2.54.0.windows.1` ✅、uv `0.11.26` ✅、D 盘剩余 ≈275 GiB ✅
- P0.2 目录骨架 + `EVIDENCE.md` 建立；Defender 排除 `SKIPPED(no admin)`
- P0.3 `config/sources.toml`（声明式模板，**无明文密钥**）、`.gitignore`
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

### P4 引擎适配层 — 🚦 **通过（无豁免项）**

| 步骤 | 结果 |
| --- | --- |
| P4.1 | `engines/base.py`：协议 + 注册表；未知引擎**报错不回退**；`run_meta` 缺字段**构造即报错** |
| P4.2 | backtrader：5 项验收全过（解析答案、停牌无成交、现金非负、缩减订单、成本单调） |
| P4.3 | bt：4 项验收全过（含「未成交卖单不提前释放资金」） |
| P4.4 | vectorbt 经桥调用；**spawn 保护有可执行的反证**（`--unguarded` 复现失败） |
| P4.5 | **对拍 A/B/C 全过**；差异已分类（见 `docs/deploy/parity_report.md`） |
| 自动化 | **268 tests OK** |

> ⚠️ **P4 最有价值的是「静默失效」类缺陷**（5 处，全都不报错、不崩溃）：
> 净值静默变 NaN、成本约束被绕过、成本双计 999.0、backtrader 主时间轴错位、
> 权重列类型不一致导致**静默空仓**。逐条已补专条用例。详见 `EVIDENCE.md` §P4。

> ⚠️ **成交口径冲突（需人工知悉）**：附录 F.4.4/F.8 写「信号后第一个有效交易日**收盘**」，
> 正文 §P4.5 写「T+1 **开盘**」。手册规定「凡与正文冲突处，以正文为准」→ 取 **T+1 开盘**。
> bt 引擎因其 API 限制实际落在 **T+1 收盘**，该差异已按 §P4.5 要求**记录并证明**归因，
> **未**调其他引擎去迁就它。

### P3 契约层 — 🚦 **通过（无豁免项）**

| 步骤 | 结果 |
| --- | --- |
| P3.1 | `contract/types.py`：**显式 `Expr` 树**（非字符串 —— 否则闸门只能靠正则猜，会漏）→ `StrategySpec` JSON 往返逐字段一致；三个校验器逐条拒绝非法值 |
| P3.2 | `contract/lint.py`：8 条规则 `G1`–`G8` **fail-closed**；含未来函数规格被拒；未上市标的被拒；**x2 来源在规则未接通时默认阻断**；`origin` 写入 run 元数据可审计 |
| P3.3 | `contract/emit.py`：与手算逐格一致；**未来扰动 ×3 后此前 251 行信号与权重逐格不变**；`emit_weights` 行和/调仓日/持现金/稳定排序全部正确 |
| 自动化 | **213 tests OK**（143 P2 + 70 P3） |

> ⚠️ **P3 最有价值的发现**：**信号是事件、持仓是状态**。原实现要求调仓日当天恰好 `+1`，
> 于是周中金叉被整条丢掉 → 事件型策略**永远空仓**（收益恒 0、不报错、不崩溃）。
> 已加 `position_state()` 归约并单列用例钉住。详见 `EVIDENCE.md` §P3。

> **闸门新规则（手册未逐条枚举，已留证）**：`G7 cost_model_declared`（成本须**显式**选定，
> 裸默认 = 全 0 = 回测偏乐观）、`G8 symbols_listed`（决策时点必须已上市，
> `listing_dates`/`as_of` 由调用方注入，契约层不依赖数据源）。

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
1df2d4a  feat(p2): 数据层 —— 契约 DDL、合成夹具、存储、适配器骨架、快照编排、质量校验
（P3 改动**尚未提交** —— 待确认）
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
.\.venv\Scripts\python.exe -m unittest discover -t . -s tests        # 期望：Ran 213 tests OK

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

## 8. 下一步：P4 · 引擎适配层

**前置：Gate P3 已通过 ✅。** 逐条步骤**以 `LOCAL_DEPLOYMENT_PLAN.md` §P4 为准**，下表仅导航：

| 步骤 | 要点 |
| --- | --- |
| P4.x | 引擎适配：**backtrader 5 项** / **bt 4 项** / **vectorbt spawn 保护** |
| P4.x | 对拍场景 **A/B/C** 在容差内一致；差异**已分类**（语义差异优先于放宽容差） |
| 🚦 | **Gate P4** |

**P2/P3 已交付、P4 可直接复用的东西**：

- `contract.emit.emit_weights()` —— **金标准权重**，对拍的基准
- `contract.types.validate_target_weights()` —— 对拍前的合法性门（行和 ≤ 1、非负）
- `quantlab.quality.clean.gold_backtest_view()` —— 带 `traded` 掩码与 `available_utc` 的回测输入
- `quantlab.store.db.connect()` / `warehouse.register_snapshot_views()` —— 只读查询 + 单快照视图
- `quantlab.engines.bridge.run_in_env()` —— 跨环境桥（vectorbt / x2 隔离环境）
- 测试基座：`python -m unittest discover -t . -s tests`（**记得先设 `PYTHONIOENCODING=utf-8`**）

**P4 的常见坑**：

- 三引擎净值不一致 → **先查成交时点/信号延迟等语义差异**，不要直接放宽容差（附录 C）。
- vectorbt 在 Windows 是 `spawn`，**必须**有 `if __name__ == "__main__":` 保护。
- backtrader 导入会往 stderr 打 `SyntaxWarning`（无害，勿判为失败）。
- 环境之间**只能**经 `job.json` + Parquet 走 subprocess，**不得互相 import**。

---

## 9. 待办 / 未决 / 需人工确认

| # | 事项 | 状态 / 时机 |
| --- | --- | --- |
| 1 | 长路径开关 + Defender 排除（需**管理员**） | 可延后，非阻塞；命令见 `EVIDENCE.md` 文末 |
| 2 | 数据供应商配置 | 接线已落地；**补 provider 实现即可**（附录 A 模板） |
| 3 | x2strategy 的 LLM 通道（云端 API / 本地 Ollama） | **P5 前**决定（附录 D-5） |
| 4 | P7 定时任务是否启用 | 默认不启用；启用须人工确认 |
| 5 | `envs/vbt` 的 `plotly<7` 上界 | **已人工裁决**；待 vectorbt 上游适配 plotly 7 后解除 |
| 6 | 每组件的许可确认（vectorbt / backtrader / DuckDB） | 附录 D-1~3，执行前逐条确认 |
| 7 | 夹具驱动验收的取舍是否认可 | 附录 D-6 |
| 8 | ~~P2 对手册 DDL 的 3 处偏离~~ | **已人工确认（2026-09-29）**，无需再议 |
| 9 | 质量阈值 `JUMP_SIGMA=8` / `JUMP_FLOOR=0.15` / `FX_STALE_DAYS=10` | 手册未指定具体值，**属实现选择**；接入供应商后应重新标定（已授权） |
| 10 | **P3 全部改动尚未 git 提交** | 待你确认后提交 |
| 11 | 夹具的「可用时间缓冲」（交易所收盘 + 15~30 分钟）是**声明假设** | F.2 要求披露：**不是**严格 point-in-time | 报告须标注；接入真实供应商后替换为真实发布时刻 |
| 12 | P3 闸门新增 `G7 cost_model_declared` 与 `G8 symbols_listed` | 手册 §P3.2 只列了通用规则四项，未逐条枚举 | 已在 `lint.py` 注明；`listing_dates`/`as_of` 由调用方**注入** |
| 13 | `emit_weights` 默认 `momentum_window=63` | 手册未指定该参数 | 按 F.8「最近 63 个本地有效交易时段」取值 |

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
