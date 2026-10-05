# 交接手册（HANDOFF）

> **一次性进度快照**，用于新会话接续执行。
> 与 `CLAUDE.md` 的分工：`CLAUDE.md` 是**长期约定**（每个会话都适用），本文件是**当前进度**（完成后即失效）。
> 更新时间：2026-10-04

---

## 1. 一句话状态

**P5 进行中：P5.1–P5.5 已完成且验证；「规格为真相 A」口径已确认落定，能力域内 E2E 两段均已落地——E2E-A（手写规格契约链，6/6 通过）与 E2E-B（受控 DSL + fail-closed parser，三 V3 全过、真跑权重 exact parity）；横截面算子族 #14 已落地（`rank`/`cross_sectional_rank`/`condition` 一等算子 + `StrategySpec.ranking`，33 测试全绿，见 §7.12）。剩余：全链路六段（vectorbt→backtrader→bt→报告）未做（见 §7.10）。**
**数据层（旁路）：tushare 境内 ETF 已全量回填（2861 只 × 2015–2026-09-30，272.4 万根 bar）并完成对账修复；剩余数据集（复权因子 `fund_adj`、基准指数 `index`、港股名单 `hk_basic`、宏观最小集）已接入并全部回填，时间范围扩展到最新交易日 2026-09-30（最新快照 `tushare-f4f7c81a15b5779c`）；数据并入主项目 `D:\project\quant\data`。**
（更新时间：2026-10-04；P5 全过程见 §7.7，口径重估见 §7.6，tushare 回填见 §7.8，剩余数据集见 §7.9，能力域内 E2E 落地见 §7.10）
（历史：P0 ✅ 含 1 项豁免、P1 ✅ 无豁免、P2 ✅ 无豁免、P3 ✅ 无豁免、P4 ✅ 无豁免）

> ✅ **已解除**：`bt` 多标的换仓的 **3.6% 偏差** = **停牌顺延缺口**（bt 无「交易日掩码」，
> 无法逐标的顺延停牌）。裁掉停牌会话后同一对引擎吻合到 **8.9e-16** → 撮合本身没算错。
> 回归用例 `tests/test_p5_bt_halt.py`；另修两处**静默**问题（bt 整数股取整、参考内核收盘
> 诊断口径自相矛盾）。见 §7.4 与 `parity_report.md` §4.5.2。

> ✅ **已解决（2026-10-04）**：以下「⛔ 当前卡点（P5.6）」是 E2E-B 落地前的**历史记录**——口径已确认「规格为真相 A」，能力域内 E2E 两段均已落地（见 §7.10）。保留此块仅作「为什么选 A」的判据：
> - 真实 `paper2spec` 产出是**带下标的伪代码 / 矩阵 / 因子**（实测两次），
>   `map_to_contract` 只认理想化的 `name(params)` → **映射不出 entry**；
> - x2 **旗舰样例 UPSA** 是**组合优化**（`output_type: matrix`、`long_short`），
>   与本平台契约（时序标量信号、只做多、权重）**能力域不相交**；
> - x2strategy 本质是 **agent skill**：①②（解析/规格）是**包内函数**（可经桥拿），
>   ③ backtrader 代码由 **agent 现场写**（**包里无生成器**），④ 回测/诊断由 agent 跑。
> - 链路**停在闸门且失败可见**（新增 **G9** 拦住了曾经的「静默全现金」）。
> → 口径**已由证据指向「规格为真相」**（§7.7 的 H1–H6 证明「代码」会静默漂移、`validate_code` 查不出），待你确认落定。详见 §7.6「重估」。
> - **本会话新增（2026-10-02，§7.7）**：真实研报已跑通 Parse+Extract（`spec.json` 已产出）；
>   agent 生成的 `strategy.py` 过 `validate_code`，但**静态审阅发现 H1–H6 语义偏差**（会改变结果）；
>   x2strategy 的 `max_tokens=8192` **写死**、推理模型下必崩，**已打补丁**（在 `.venv` 内，重建即失）。

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
| 数据供应商 | **cn_etf = tushare 全量回填**（2861 只）；**macro/index = tushare 回填**；**hk = 名单（tushare_hk）+ 行情 bronze（futu，未 normalize）**；us/fx 仍留空；其余用合成夹具 |
| 网络 | 未验证数据接口可用性；**`raw.githubusercontent.com` 在本机不可达**（litellm 成本表拉取超时） |
| **环境边界**（P1 定） | core（根）/ `envs/vbt` / `envs/x2`，**各自独立 `uv.lock`**；**backtrader 归 core**，未建 `envs/btrader` |
| **打包**（P2 定） | 根项目改为 **`uv_build` 可编辑安装**（`[build-system]` + `module-root="src"`），故 `import quantlab` 可用；CLI 入口 `quantlab = quantlab.cli:main`。`uv.lock` 仅 1 行变化，**依赖零漂移** |
| **测试栈**（P2 定） | **`unittest`**（全库统一，CLAUDE.md 二选一）。命令：`python -m unittest discover -t . -s tests` |
| **夹具快照** | `data/bronze/synthetic/synth-v1-613c5986a898/`（gitignored，可确定性重建）；DuckDB 台账 `data/warehouse.duckdb`（**派生**，可重建） |
| ⚠️ **数据路径解析** | 数据根/runs/envs 统一走 `quantlab.paths`（`DATA_ROOT`/`RUNS_DIR`/`ENVS_DIR`/`DUCKDB_PATH`）；`PROJECT_ROOT` 默认 = 检出根，用环境变量 `QUANT_ROOT` 覆盖为 canonical 根 `D:\project\quant`（**PR#2 `811f0c2`**，见 CLAUDE.md「使用 orca 时的操作规范」）。CLI 仍可用 `--out`/`--warehouse` 显式覆盖 |
| ⚠️ **DuckDB 并发**（P2 实测） | ① 写者对**其他进程**独占：写者持有时连**只读**也打不开 → 「单写多读」仅在**无活跃写者**时成立；② **同进程**内对同一库文件**不得**持有配置不同的连接（RO/RW）→ 用连接注入 |
| ⚠️ **跑测试前** | 设 `PYTHONIOENCODING=utf-8`（否则中文断言信息在子进程里会 UnicodeDecodeError） |
| 三环境 Python | 均 **3.12.13**（起点即通过，无需下调） |
| ⚠️ 外来环境变量 | 手工跑 uv 前**必须**清除 `UV_PROJECT_ENVIRONMENT` 与 `VIRTUAL_ENV`（外部工具「Agents Anywhere」注入，会把环境建到错误位置）；`src/quantlab/engines/bridge.py` 已自动清洗 |
| x2strategy 导入名 | 发行名 `x2strategy`，但**可导入模块是 `paper2spec` / `spec2code`**（**没有** `x2strategy` 模块） |
| ⚠️ **x2 云端中转**（本会话实测） | 中转 `/models` **只提供** `deepseek-flash` / `deepseek-v4-pro`（**均为推理模型**，默认 `effort=high`）；`config/llm.toml` 写 `claude-sonnet-4-5` 会被**静默回落**到 `deepseek-flash`。litellm **原生**认 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL`（无需桥接） |
| ⚠️ **x2strategy `max_tokens` 写死** | `extractor.py:409/423`=8192、`llm.py:44/68` 默认 16384，**无环境变量可调**。推理模型下 Layer-2 思维链吃光 8192 → `content=None` → `AttributeError`。**已打补丁**（→393216，共 4 处），补丁在 `.venv` 内**重建即失**，依据 `envs/x2/X2STRATEGY_PATCH.md` |
| x2 其余能力（本会话实测） | `spec2code` **只有 `validate_code`（无生成器）**；`operator_pitfall` 语料**仅 4 条**、全属组合优化域，对时序 ETF 轮动命中牵强；embedding `BAAI/bge-small-en-v1.5` **已本地缓存**（`HF_HUB_OFFLINE=1` 可离线） |

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

## 5.1 已完成（P2 · 数据层）

| 步骤 | 结果 |
| --- | --- |
| P2.1 | `store/schema.sql` + `migrate.py`（幂等、漂移检测）+ `db.py` + `snapshot_guard.py`；9 表；5/5 事实表含 `available_utc`+`snapshot_id`；`v_bars_latest` 单快照视图 |
| P2.2 | `fixtures/spec.py` + `synth.py`：**9 标的 / 7 表 / 18941 根 bar**；内嵌分红·拆分·停牌·退市·晚上市·汇率·**下载失败**；解析净值与重算 `max\|Δ\|≈6e-15`；跨进程哈希一致 |
| P2.3 | `store/warehouse.py` 两条 Parquet→DuckDB 路径；只读拒写、第二个写进程被拒、`read_parquet` 视图可用 |
| P2.4 | `Source` 协议 + `akshare`/`yfinance`/`macro_fred` 三个骨架；**延迟导入**实测（导入后 SDK 不在 `sys.modules`）；未实现入口抛 `VENDOR-TBD` |
| P2.5 | `ingest/orchestrator.py` + `cli.py`：**原子替换、幂等、中断可恢复**；`ingest_runs` 三态 `running→ok\|aborted` |
| P2.6 | `quality/clean.py` + `checks.py`：8 类缺陷逐条可捕获；**三态互异**；复权/汇率手算吻合；gold 回测视图 |
| **自动化** | **143 tests OK**（`python -m unittest discover -t . -s tests`） |
| 🚦 | **Gate P2 通过（无豁免项）** |

> 全部证据见 `docs/deploy/EVIDENCE.md` §P2。**无证据的步骤视为未完成。**

---

## 5.2 已完成（P3 · 契约层）

| 步骤 | 结果 |
| --- | --- |
| P3.1 | `contract/types.py`：`Expr` 树（**显式结构**，非字符串）→ `StrategySpec` JSON 往返逐字段一致；三个校验器逐条拒绝非法值 |
| P3.2 | `contract/lint.py`：8 条规则 `G1`–`G8` **fail-closed**；含未来函数规格被拒；未上市标的被拒；x2 来源在规则未接通时**默认阻断** |
| P3.3 | `contract/emit.py`：`emit_signals` 与手算逐格一致；**未来扰动 ×3 后此前信号与权重逐格不变**；`emit_weights` 行和/调仓日/持现金/稳定排序全部正确 |
| **自动化** | **213 tests OK**（143 P2 + 70 P3） |
| 🚦 | **Gate P3 通过（无豁免项）** |

> ⚠️ P3 最有价值的发现：**信号是事件、持仓是状态**。原实现要求调仓日当天恰好 `+1`，
> 于是周中金叉被整条丢掉 → 事件型策略**永远空仓**（收益恒 0、不报错）。
> 已加 `position_state()` 归约并单列用例钉住。详见 `EVIDENCE.md` §P3。

---

## 5.3 已完成（P4 · 引擎适配层）

| 步骤 | 结果 |
| --- | --- |
| P4.1 | `engines/base.py`：`BacktestRunner` 协议 + 注册表；**未知引擎报错不回退**；`run_meta` 缺复现性字段**构造即报错**；另加 `reference`（语义真值 oracle） |
| P4.2 | `engines/backtrader_runner.py`：`cheat_on_open` + `next_open()` 按成交价折算份额；5 项验收全过 |
| P4.3 | `engines/bt_runner.py`：权重前移一根，bt 在 T+1 **收盘**成交（差异已分类） |
| P4.4 | `envs/vbt/entry.py` 的 `op=scan` + `_spawn_guard_demo.py`（`--unguarded` **可复现失败**）；core 内不加载 vectorbt/numba |
| P4.5 | **对拍 A/B/C 全过**：reference ↔ backtrader 在三个场景均 **~1e-16**；bt 的偏差**已证明**属成交时点 |
| **自动化** | **268 tests OK**（213 + 55 P4） |
| 🚦 | **Gate P4 通过（无豁免项）** |

> ⚠️ P4 最有价值的产出：**5 处「静默失效」缺陷**（净值变 NaN、成本被绕过、
> 成本双计、主时间轴错位、权重列类型不一致导致静默空仓）——
> 全都不报错、不崩溃。详见 `EVIDENCE.md` §P4 与 **`docs/deploy/parity_report.md`**。

---

## 6. 下一步：剩余 P5.6 全链路 + P6（组合与报告）

> **P5 主体已交付**：P5.1–P5.5 与能力域内 E2E-A/E2E-B 均已落地（见 §7.10）。
> 剩余两项：
> ① **全链路六段**（P5.6 的 V2/V4）——论文 → spec → lint → vectorbt 粗筛 → backtrader 精验 → bt 组合 → 报告，全程无人工改文件，尚未串起来；
> ② **P6 组合与报告**——年化口径按所用日历推导并全局统一（不写死 365/252）。

> **P4 已交付可直接复用**：`engines.base.load_bundle_from_fixture()`（夹具→回测输入）、
> `engines.base.get_runner()`（按名取引擎）、`engines.execution`（撮合语义真值）、
> `docs/deploy/parity_report.md`（三引擎口径差异台账）。

---

## 7. 待办 / 未决

### 7.1 已关闭（历史，勿再当待办）

| # | 事项 | 结论 |
| --- | --- | --- |
| 3 | x2strategy 的 LLM 通道 | ✅ **已定**：云端 API，`api_key_env=ANTHROPIC_AUTH_TOKEN`、`api_base_env=ANTHROPIC_BASE_URL`（均为**环境变量名**，key 值不入库） |
| 8 | P2 对手册 DDL 的 3 处偏离 | ✅ **已人工确认**（`ingest_runs` 主键、两表增 `available_utc`） |
| 10 / 13 | P2 / P4 改动提交 | ✅ 已提交（`1df2d4a` / `efdbe63`） |
| 11 | 成交口径冲突（F.4「收盘」vs §P4.5「T+1 开盘」） | ✅ **已确认维持 T+1 开盘** |

### 7.2 待办（**已按你的指令暂时搁置，仅记录**）

| # | 事项 | 影响 | 何时处理 |
| --- | --- | --- | --- |
| 1 | 长路径 + Defender 排除（需**管理员**） | 非阻塞 | 可延后 |
| 2 | **数据供应商配置** `config/sources.yaml` | 填了才能把「合成夹具验证」换成真实数据 | 等你 |
| 4 | P7 定时任务是否启用 | 默认**不启用**，启用属范围变更 | 等你确认 |
| 12 | §P4.5 场景 C 原文含**汇率**，三个 runner 均不建模（`fx_cost_bps≠0` 直接报错） | 场景 C 尚不完整 | 建议 P6 补 |
| 9 | 质量阈值 `JUMP_SIGMA=8` / `JUMP_FLOOR=0.15` / `FX_STALE_DAYS=10` | 首次设定，**未用真实数据校准** | 接入供应商后重标定 |
| 5 | `envs/vbt` 的 `plotly<7` 上界（现 6.9.0） | 已人工裁决 | 待 vectorbt 上游适配 plotly 7 |
| 6 | `litellm` 无 `__version__` | — | P5 起改 `importlib.metadata.version("litellm")` |
| 7 | 手工跑 uv 前须清 `UV_PROJECT_ENVIRONMENT`/`VIRTUAL_ENV` | 会把环境建到错误位置 | 例行注意（`bridge.py` 已自动清洗） |
| 14 | **横截面算子族**（`rank`/`cross_sectional_rank`/`condition`） | ✅ **已落地**（2026-10-05）：三算子进 `Expr` 一等求值 + `StrategySpec.ranking` 取代 `emit_weights` 硬编码动量，33 测试全绿 | 见 §7.12 |
| 15 | **x2strategy `max_tokens` 补丁在 `.venv` 内** | 重建环境（`uv sync`/重装）即丢 → Extract Layer-2 会再次崩溃 | 重建后按 `envs/x2/X2STRATEGY_PATCH.md` 复现 |
| 16 | **`strategy.py` 静态审阅出 H1–H6 语义偏差**（HRP 退化等权、风险袖套不缩放、横截面 rank 未用、跳空延迟全局生效…） | 若要真跑此代码，须先修，否则结果无解释力 | 待定，见 §7.7 与 `envs/x2/X2_PAPER2CODE_REVIEW.md` |
| 17 | `config/llm.toml` 模型名与中转实际提供的不符 | 已改 `claude-sonnet-4-5` → `deepseek-v4-pro` | ✅ 本会话已修 |

---

### 7.3 P5 待决事项与决策记录（2026-09-29）

#### 三项决策（**已由人工裁定**）

| 决策 | 选择 | 含义 |
| --- | --- | --- |
| **1. P5.5 验收口径** | **A：缩小验证形态** | 用 backtrader **支持**的形态（单标的 / 建仓 / 清仓）验 `spec2weights`；**多标的换仓单独标注「限 reference/bt」**，不阻塞 P5.5 |
| **2. bt 的 3.6% 未解释偏差** | **B：现在定位** | ✅ **已完成**（2026-09-29）：= **停牌顺延缺口**，bt 表达能力所限，撮合本身正确。见 §7.4 |
| **3. P5.6 端到端范围** | **C：与决策 1 保持一致** | 端到端里 backtrader 那一段按决策 1 的口径处理 |

#### 决策 1 的直接后果（P5.5 怎么做）

- `spec2weights` 本身（P5.5 的真正产物）**照常完整实现**；
- 跨引擎对拍只覆盖 backtrader 支持的形态；
- **多标的换仓**在 P5.5 的对拍里**如实标注为受限**，写在产出里而不是悄悄跳过。

---

## 7.4 bt 多标的偏差定位结论（2026-09-29）

**= 停牌顺延缺口**，非撮合错误。摘要：

| 样本 | bt vs ref@open | bt vs ref@close | halt_sessions |
| --- | --- | --- | --- |
| 全样本 [1,2,3] | 4.61e-02 | 3.59e-02 | 182 |
| 裁掉停牌会话 | 1.15e-02（纯成交时点） | **8.88e-16（机器精度）** | 0 |

- **机理**：bt 的 `WeighTarget` 无「交易日掩码」概念，再平衡日落在停牌会话时按 `ffill`
  旧价就地成交；统一口径要求顺延到下一个可交易会话。bt 无法逐标的顺延（一个日期一个向量）。
- **处置**：**不**让别的引擎迁就它（§P4.5）；改为**显式可见** —— `BtRunner` 输出
  `stats["halt_sessions"]` 与 `run_meta["known_deviation"]` 含 `halt_deferral_unsupported`。
  真正的下单层修复留 P6。
- **顺带修掉两处静默问题**：B1 bt 整数股取整（→ `integer_positions=False`）；
  B2 参考内核收盘诊断模式定份额用开盘估值（→ 估值跟随 `fill_at`）。
- **回归用例**：`tests/test_p5_bt_halt.py`（V1 缺口真实、V2 无停牌即吻合到机器精度、
  V3 显式标注）。完整证据见 `docs/deploy/EVIDENCE.md` §P5 与 `parity_report.md` §4.5.2。

---

## 7.5 P5.5 `spec2weights` 完成结论（2026-09-29）

- **产出**：`contract/emit.py::spec2weights(spec, data) -> TargetWeights` —— 三引擎的
  规范入口（多一步 `validate_spec`，fail-closed）。底层仍是 `emit_weights`（**单一实现**，
  用例钉住二者不得分歧）。
- **验收**（对齐决策 1 = A「缩小验证形态」）：
  - 同一份面板喂 **backtrader ↔ ref@open** 与 **bt ↔ ref@close**，均在容差内；
  - 未来扰动：改未来价格，此前权重逐格不变；
  - 单标的**建仓/清仓**：backtrader/bt 各自与对应口径的参考一致。
- **多标的换仓**：如实标注**限 reference/bt**（backtrader fail-closed，用例钉住）。
- **新发现**：backtrader 另有两类 broker 模型拒单 **L1（重复目标→补仓 Margin）**、
  **L2（跳空低开建仓→保证金按昨收校验而误拒）**，**记录未修**，见 `parity_report.md` §4.5.1
  —— 这正是决策 1 缩小验证形态的实证依据。
- **用例**：`tests/test_p5_spec2weights.py`（9 用例）。全套 **332 tests OK**。

---

## 7.6 P5.6 端到端：调研结论与卡点（2026-10-02）

### 已完成

| 步 | 结果 |
| --- | --- |
| LLM 通道 | ✅ 经桥 → `envs/x2` **真实调云端成功**（`anthropic/claude-sonnet-4-5`） |
| 合成样例论文 | `papers/sample-momentum.md`（横截面）、`papers/sample-ma-cross.md`（时序） |
| **信封缺口**（修） | 真实产出是信封 `{num_detected, paper_title, strategies:[…]}`；`map_to_contract` 按单策略字典处理 → **静默空 entry**。已加 `_unwrap_strategy` |
| **G9 闸门**（修） | ⚠️ 闸门**曾放行 `entry=None`** → 权重恒 0 → **静默全现金**。已加 `G9.entry_present`（fail-closed） |
| **lookback 崩溃**（修） | `lookback_period` 可能是字符串（`"20 and 60 trading days"`）→ `int()` 崩溃。已加 `_parse_lookback` |

### x2strategy 的输出（**定论**，读源码 + 官方样例得出）

**它是 agent skill，不是库**（`SKILL.md` v0.6.1：「You are the executor」）。产出四站：

| 站 | 工件 | 谁产出 | 形态 |
| --- | --- | --- | --- |
| ① 解析 | `content.json/md` | **包内函数** | `PaperContent` |
| ② 抽取 | `spec.json/md` | **包内函数** `extract_spec()` + LLM | **`StrategySpec` 信封**（可经桥拿） |
| ③ 生成 | `strategy.py` | **agent 现场写**；包只 `validate_code` | **backtrader 代码**（可交易）**或 pandas**（研究型） |
| ④ 回测/诊断 | `results/*` | **agent 跑** | metrics + 图 |

- **③ 包里没有生成器**（`spec2code/` 只有 `config/models/validator`）；官方 UPSA 生成物实测
  **500 行 pandas/numpy、`grep -c backtrader` = 0**（因其不可交易）。
- **输出内容由**：输入文档 + `prompts.py` 的 LAYER0–4 + `mode`(multilayer/single) +
  `instruction_context` + **LLM**（非确定）决定；**代码形态**另由「是否可交易」决定。
- **能力域判据**（spec 自带）：`strategy_type` / `indicators[].scope`（time_series vs
  cross_sectional）/ `output_type`（scalar·boolean·series vs **ranking·vector·matrix**）/
  `position_sizing.long_short`（本平台 F.1 **只做多**）。→ 闸门可**自动判定能力域内外**。

### ⛔ 卡点：口径未定（**待人工裁定** —— 2026-10-04 已据 §7.7 重估，见下方「重估」）

| 方案 | 含义 | 代价 |
| --- | --- | --- |
| **A. 规格为真相**（推荐，符合手册 §P5.5） | spec 是单一真相；`spec2weights` 投影成权重，四引擎各司其职（**vectorbt 粗筛 → backtrader 精验 → bt 组合 → reference 对账**）；x2 生成的 backtrader 代码作**第二实现**用于对拍（§P5.5 V3b） | 需要「真实产出 → 规格」的**解析层**；能力域外的论文必须 **fail-closed 标注** |
| **B. 代码为真相** | x2 的 backtrader 代码当入口 | bt/vectorbt/reference 对 x2 策略**全部失效** → 退化单引擎，跨引擎一致性保证丢失 |

> **为什么需要 A**：x2 只产 backtrader 代码；**若不自研 `spec2weights`，vectorbt 与 bt
> 无法复用同一规格**（手册 §P5.5「关键补口」原文）。A 正是让另外三个引擎有角色的机制。

### 重估（2026-10-04，据 §7.7 的 H1–H6 证据）

§7.7 用真实研报跑通 x2strategy 后，静态审阅 `strategy.py` 发现 **H1–H6 语义偏差**，
其中 H1/H2/H3/H5 **会静默改变结果**，而 `validate_code` 却报 `valid=True, 0 err, 0 warn`。
这条证据**改变了 A/B 的权重**：

| 方案 | 重估后结论 |
| --- | --- |
| **B. 代码为真相** | ❌ **被证据否决**。「代码」路径唯一的自动闸门 `validate_code` 只查语法/结构/指标名，**结构性查不出语义漂移**；H2（HRP 退化等权）、H1（风险袖套不缩放）、H5（跳空冻结存量）证明代码是 spec 的**有损投影**。以它当真相 = 在不可验证、静默漂移的工件上建平台。 |
| **A. 规格为真相** | ✅ **唯一可行**。spec 可被我们的 `map_to_contract` + `spec2weights` **确定性、fail-closed、可测**地投影，跨引擎一致性得以保留。 |

> **但这不等于 P5.6 打通。** §7.7 的「跑通」是 **x2strategy 冒烟**（明确**未碰平台契约层**），
> 且用的是**横截面组合构建**研报（HRP / 横截面 rank / 风险袖套）——落在 **#14 能力域外**。
> 真正未证明的是**能力域内**时序样例 `sample-ma-cross.md` 走 `map_to_contract → spec2weights →
> 回测` 这一整条契约链。重估把卡点从「A or B 二选一」**收敛**为两件具体事：
> ① 能力域内 E2E（时序样例）；② 横截面算子族 #14（真实研报属此域，另立任务）。

### 下一步（✅ 已确认 A 并执行，见 §7.10）

- **确认 A**：✅ 已确认（证据指向，B 已否决）。
- **能力域内 E2E**：✅ 已落地（E2E-A 契约链 + E2E-B 受控 DSL/parser，见 §7.10）。
- **解析层子问题**：✅ 已定——用户拍板「受控 DSL + fail-closed parser」（§P5.6a），
  不再走 x2strategy 的 `paper2spec` 提取，改为自研 prompt→DSL。
- **真实研报（横截面）**：仍归 **#14**，不作为 P5.6 验收对象；§7.7 的 H1–H6 作为
  「代码不可当真相」的反面证据留存，不进入主线。

---

## 7.7 本会话：用真实研报跑通 paper→code 链（2026-10-02）

**目标**：拿一篇真实研报端到端测 x2strategy，**只测 x2strategy 本身**（不碰平台契约层）。

**输入**：`papers/多资产 ETF 轮动策略：固收+视角下动态组合管理的构建与实践.pdf`（**已 gitignore**）。

### 结果

| 阶段 | 状态 | 关键数字 |
| --- | --- | --- |
| Parse（`parse_document`，Mode A） | ✅ **真实产出** | 69.8s；`content.json` 107KB |
| Extract（`extract_spec`，multilayer） | ✅ **真实产出**（**须先打补丁**） | 490.2s；1 策略 / **42 指标 / 18 步 / 1 执行计划 / 11 风控** |
| `spec2code.validate_code` | ✅ | 4/4 用例正确；注册表 **317 指标**；**结构性缺失只报 warning、不报 error** |
| `paper2spec.operator_pitfall` | ✅（域窄） | 语料 4 条；切 61 查询；命中 2 条但**语义牵强** |
| 生成 backtrader 代码 | ⚠️ **agent 生成**（非包产物） | `strategy.py` 537 行；`validate_code` = `valid=True, 0 err / 0 warn` |

**产物**（`runs/x2/<slug>/`，**均 gitignored**）：`content.*` / `spec.*` / `operator_pitfall.*` /
`strategy.py` / `strategy_validation.json` / `run_meta.json`。

### 关键结论

1. **包只覆盖到「规格」**：③ 生成代码、④ 回测诊断都是 **agent 侧**流程 —— 包里**无生成器、无 runner**。
   故「能否直接跑 backtrader 回测」= **不能**：需 agent 写代码 + 自行提供数据。
2. **`max_tokens` 写死 + 推理模型中转 = Extract 必崩**（详见 §3）。已打补丁并留证。
3. **静态审阅**：agent 生成的 `strategy.py` 与 `spec.json` 存在 **H1–H6 语义偏差**（会改变结果）
   与 M1–M7 简化。`validate_code` **只查语法/结构/指标名，查不出语义**。
   详见 `envs/x2/X2_PAPER2CODE_REVIEW.md`。
4. **提交**：分支 `feat/x2-paper2code`（`5489565`）已 **fast-forward 并入 `master`**
   （现 `master` = `bc2ddb2`，后者含 P5.6 遗留提交）。**未推送**；分支本身**未删除**。

### 新增脚本（已入库）

`envs/x2/run_paper2spec.py`（Parse+Extract 独立驱动）、`_llm_probe.py`（通道探针）、
`_diag_extract_l2.py`（根因诊断）、`run_x2_extra_tests.py`（`validate_code` + `operator_pitfall`）、
`X2STRATEGY_PATCH.md`（补丁记录）、`X2_PAPER2CODE_REVIEW.md`（静态审阅）。

---

## 7.8 本会话：tushare 境内 ETF 全量回填 + 对账修复（2026-10-04）

**目标**：按计划 §一–§六，接入 tushare 为真实数据源，全量回填境内 ETF 并做日历对账。

### 结果

| 阶段 | 状态 | 关键数字 |
| --- | --- | --- |
| 小样本验证（先行） | ✅ | 2 ETF × 2023 跑通 fetch→normalize→快照→DuckDB；242 根/只，XSHG 日历零缺失 |
| 全量回填（第一轮） | ✅ | `tushare-02d1572bb0f220c7`；2861 标的 / 1,989,262 bar / 1,448 分红；69.9 min |
| 对账 | ⚠️ 发现 1 缺陷 | 结构红旗全清，但 8 只窗口内 ETF 被静默跳过（瞬时空表） |
| 修复 + 重跑 | ✅ | `tushare-704bee7c042ed05c`；1,992,886 bar（+3,624）；结构红旗仍全清 |

### 关键结论

1. **结构红旗全清**：0 非正价 / 0 区间违例 / 0 未来函数 / 0 主键重复 / 0 孤儿。
2. **真实缺陷 1 处已修**：`fund_daily` 偶发返回空 DataFrame（非异常），`_call_with_retry`
   只重试异常不重试空表 → 8 只被静默当无数据。新增 `_fetch_fund_daily()`（空表重试
   2 次 / 0.5s），离线测试 `TestFetchFundDailyEmptyRetry` 固化。
3. **对账「缺口」几乎全是口径**：674 只窗口后上市 + 138 只 list_date 缺失 + 82 只退市基金
   + 45 只 list_date 偏晚（负缺口）——bars 正确，属 tushare 元数据缺陷。
4. **留 P2.6**：`fund_basic.list_date` 的 45 偏晚 + 138 缺失，silver 层用
   `bars_daily.min(ts)/max(ts)` 交叉校正 `listed_on/delisted_on`。

### 新增/改动（已入库）

`ingest/adapters/tushare.py`（`_fetch_fund_daily` 空表重试）、`tests/test_realdata.py`
（`TestFetchFundDailyEmptyRetry`）、`docs/deploy/EVIDENCE.md`（回填+对账+修复证据）。

### 待续（数据层）

- ~~复权因子 `fund_adj` 未进契约快照（F.6 总收益待补）。~~ → ✅ 已并入 tushare 快照（§7.9）。
- ~~宏观 / index 基准 / hk_basic。~~ → ✅ 已回填（§7.9）。
- US / FX 仍走 yfinance / futu，属后续回填范围。

### 数据位置迁移（2026-10-04）

tushare 数据原先落在 worktree 的 `data/`（根因：数据根按 `__file__` 相对解析；该根因已由 PR#2 `quantlab.paths`/`QUANT_ROOT` 修复，见 §3），
已并入主项目唯一数据目录 `D:\project\quant\data`：
- Parquet 真相：`D:\project\quant\data\bronze\tushare\{tushare-02d1572bb0f220c7, tushare-704bee7c042ed05c}`（10 文件，sha256 校验 0 失配）。
- 台账：6 行 tushare `ingest_runs` 已并入 `D:\project\quant\data\warehouse.duckdb`（共 13 行），`v_bars_latest` → `tushare-704bee7c042ed05c`。
- worktree 侧 `data/bronze/tushare/` 与 `data/warehouse.duckdb` 已删。

---

## 7.9 本会话：剩余 tushare 数据集回填（2026-10-04）

**目标**：补齐境内 ETF 之外的剩余 tushare 数据——复权因子 `fund_adj`、基准指数 `index`、
港股名单 `hk_basic`、宏观最小集（cn_cpi/cn_ppi/cn_gdp/shibor）。

### 结果

| 源 | snapshot_id | row_counts |
| --- | --- | --- |
| `tushare_index` | `tushare_index-ffa42af4c69a78bd` | index_symbols 8000 + index_daily 23092（10 基准 × 2015–2024） |
| `tushare_hk` | `tushare_hk-f47ed6896ac27499` | hk_symbols 2792 |
| `tushare_macro` | `tushare_macro-5263a25dcde59090` | macro_series 2755（CPI/PPI/GDP/shibor × 2015–2024） |
| `tushare`（fund_adj 并入） | `tushare-18a84ba609fece5d` | `{symbols: 2861, bars_daily: 1993234, corporate_actions: 1448, fund_adj: 2156140}` |

### 关键结论

1. **`fund_adj` 并入 `source=tushare`**（同标的池、同快照），保证复权因子与 bars 同源一致；
   因内容哈希含 fund_adj，产出**新 snapshot_id**（旧快照保留、被新结果取代）。
2. **`trade_cal` 交叉核对**：tushare SSE 与 exchange-calendars XSHG 2015–2024 **完全一致**
   （2431=2431）→ 沿用 core 现有日历。
3. **宏观口径**：`available_utc = 观测期末 + 发布滞后`（CPI/PPI 15d、GDP 30d、shibor 1d）。
4. **回填驱动** `scripts/backfill_tushare.py`：winreg 读 token（Bash 子进程看不到 setx 的
   user env），进程内注入，落点固定主项目 `D:\project\quant\data`。

### 7.9.1 时间范围扩展至最新交易日（2026-09-30）

用户要求把本次数据时间范围扩展到最新交易日。`exchange_calendars.XSHG` 给出 2026-10-04
之前最近一个交易日 = **2026-09-30**（国庆休市前）。`scripts/backfill_tushare.py` 的 `END`
改为动态求「今天（含）前最后一个交易日」，`START` 仍 2015-01-01；重跑三个带日期的源。

| 源 | 新 snapshot_id | row_counts（旧 → 新） |
| --- | --- | --- |
| `tushare` | `tushare-f4f7c81a15b5779c` | `{symbols: 2861, bars_daily: 1993234 → 2724037, corporate_actions: 1448 → 2174, fund_adj: 2156140 → 2845102}` |
| `tushare_index` | `tushare_index-86318b52ea0fbe04` | index_symbols 8000 + index_daily 23092 → 27332 |
| `tushare_macro` | `tushare_macro-a424ac296fabfad8` | macro_series 2755 → 3235 |
| `tushare_hk` | （不变，跳过） | hk_symbols 2792 |

- `symbols` 保持 2861：`fund_basic(market=E)` 是当前全名单（无日期窗口），symbol_id 不因扩展移位。
- 三个源旧快照原封保留，新快照为独立不可变快照；`check_real_invariants` 通过（进程 exit 0）。

---

## 7.10 本会话：P5.6 能力域内 E2E 落地 —— E2E-A 契约链 + E2E-B 受控 DSL/parser（2026-10-04）

**目标**：把 §7.6 的「下一步」落地——口径 A「规格为真相」确认后，补上唯一未证环节：
能力域内时序样例 `sample-ma-cross.md` 的契约链。分两段：E2E-A（确定性契约链，手写规格）与
E2E-B（论文 → spec 的 LLM 提取层，用户拍板「受控 DSL + fail-closed parser」）。

### E2E-A · 确定性契约链（手写规格）

- `tests/test_p5_ma_cross_e2e.py` **6/6 通过**：手写规格过 P3.2 闸门 → `emit_signals` 逐日
  对拍手算金叉/死叉（`cross_above/below` 事件语义）→ 权重时间线逐格对拍持仓状态 →
  reference/backtrader 对拍（1e-4）。
- 关键口径：`momentum_window = lookback = 60`（默认 63 会把 60–62 日的金叉多卡 3 日）；
  「权重」=「持仓状态 ∧ 可成交 ∧ 动量预热」，交易所休市日（167 天）不表达目标（F.4.5）。

### E2E-B · 受控 DSL + fail-closed parser（§P5.6a，用户拍板）

**设计**：prompt 只负责逼出结构化，parser 只负责吃掉结构化，谁都不猜自然语言。核心交付
`src/quantlab/x2/dsl.py`（纯函数 parser，白名单与 `emit.evaluate._SUPPORTED_OPS` 严格同步）。

- **三 V3 全过**（`tests/test_p5_dsl.py` 20/20）：
  - **V3 #1** 合法 DSL → 合规 `StrategySpec`；畸形 DSL（未知算子/缺 op/非对象/窗口非正整数/
    arity 错/缺 entry…）→ 明确抛 `DslParseError`，无静默兜底。
  - **V3 #2** `sample-ma-cross.md` 真跑 prompt→DSL→parser→闸门→`spec2weights`，权重与
    E2E-A 手写规格**逐格一致（exact parity）**。
  - **V3 #3** 语义偏离可见：`gt`/`lt` 结构合法被收下（parser 不猜语义）但权重最大差 >0.5；
    `needs_human_review` 标记保留。
- **真跑**（真实 LLM，`anthropic/deepseek-v4-pro`）：一次即产出语义正确的 DSL（`cross_above`/
  `cross_below`，未偏离成 `gt`/`lt`）。
- **配套改动**：`paper2spec.py`（`OP_ALIASES` 补 cross、二元算子 fail-closed 拒绝并引导走 DSL、
  解析 `exit`、新增 `extract_dsl()`）、`envs/x2/entry.py`（新增 `paper2dsl` 算子，凭据走环境变量
  不落盘）、`src/quantlab/x2/__init__.py`（导出 5 符号）。回归 `test_p5_paper2spec.py` 17/17 无回归。

### 提交（本会话）

| commit | 内容 |
| --- | --- |
| `013f7d8` | `chore(futu)`：`.gitignore` 忽略 `envs/futu/futu_opend/`（250MB OpenD 二进制，不入库） |
| `481fbed` | `feat(x2)`：P5.6 端到端验收落地（E2E-A + E2E-B，8 文件 +1093/−2） |

### 剩余（未做，非阻塞）

- **全链路六段**（P5.6 的 V2/V4）：论文 → spec → lint → vectorbt 粗筛 → backtrader 精验 →
  bt 组合 → 报告，全程无人工改文件 —— **尚未串起来**（本次只到 paper→spec→weights 对拍）。
- **横截面算子族 #14**（`rank`/`cross_sectional_rank`/`condition`）——真实研报属此域，单独立项。

---

## 7.11 PR #2 / PR #3 提交总结（2026-10-04）

> 两次远程 PR 已并入 `main`：PR #2 `034ef24`（解释项目概念）、PR #3 `c0129a5`（获取港股数据），本地经 `pull: Fast-forward` 并入，本会话工作未受影响。

### PR #2 · 引入 canonical 项目根（`811f0c2`）

**一句话**：数据 / runs / envs 与「代码检出根」解耦，解决 orca worktree 里 agent 把产出物写进工作树（gitignored、清理即丢）的问题。

- 新增 `src/quantlab/paths.py` 单一事实来源：`CHECKOUT_ROOT`（git 溯源，= 检出/工作树根）vs `PROJECT_ROOT`（canonical，环境变量 `QUANT_ROOT` 可覆盖，默认 = CHECKOUT_ROOT）；派生 `DATA_ROOT`/`RUNS_DIR`/`DUCKDB_PATH`/`ENVS_DIR`/`CONFIG_DIR`。
- 6 个核心模块改用该入口：`engines/base.py`、`engines/bridge.py`、`fixtures/synth.py`、`ingest/orchestrator.py`、`store/db.py`、`x2/llm.py`；5 个 envs 脚本（futu `fetch_plate_stock.py`、x2 `_diag_extract_l2.py`/`_llm_probe.py`/`run_paper2spec.py`/`run_x2_extra_tests.py`）。
- CLAUDE.md 新增「使用 orca 时的操作规范（强制）」：产出物落点、`QUANT_ROOT` 指主检出、跑代码用主检出环境不重建 `.venv`、脚本归属、DuckDB 单写串行。
- **影响**：§3 的「数据路径解析」旧机制（`__file__` 相对 + `--out`/`--warehouse`）已被 `quantlab.paths` + `QUANT_ROOT` 取代（§3 已同步更新）。

### PR #3 · futu OpenD 港股 K 线 bronze 拉取（`4644489`→`a7cab72` + `345b0ba`）

**一句话**：接入 futu OpenD，拉取港股日线历史 K 线（不复权原始价 + 复权因子），落 bronze 原始快照；配合 `--resume` 分批续抓 + runbook。

| commit | 内容 |
| --- | --- |
| `4644489` | 新增 `envs/futu/fetch_history_kline.py`：`request_history_kline(autype=NONE)` 不复权 + `get_rehab` 复权因子，落 bronze 快照（`kline.parquet`/`rehab.parquet`/`manifest.json`） |
| `a59603f` | 修 `get_last_error` 崩溃 + 限流 `--delay 1.2`；记录全量拉取部分成功证据 |
| `96b539b` | 加 `--resume` 续抓，分 5 批拉全 472 只（每 7 天一批） |
| `a7cab72` | `--resume latest` 自动取最新快照 + 硬故障退出码 2 |
| `345b0ba` | `LOCAL_DEPLOYMENT_PLAN.md` 附录 A.1 记录 runbook（退出码 0/1/2 语义 + 定时续抓命令） |

- **仅 bronze 原始快照**，尚未 normalize 到 `bars_daily` 契约（`symbol_map`/currency 未配，P2.4 适配器留后续）。
- **两道 futu 限额**：① 历史 K 线 / 复权因子各「每 30 秒 60 次」→ `--delay 1.2s`；② 正股 K 线「每 7 天 100 只」→ 分批。
- 首份快照 `20261004_115547_925918` 已存在，续抓命令见附录 A.1。

---

## 7.12 横截面算子族 #14 落地（2026-10-05）

**一句话**：把横截面排名从 `emit_weights` 里**硬编码**的 63 日动量，升级为一等 `Expr` 算子
（`rank` / `cross_sectional_rank` / `condition`）+ spec 驱动的 `StrategySpec.ranking`，使
「63 日动量轮动，买前 3 等权」可表达为一个合规 `StrategySpec`，过全闸门并产出正确权重。

### 三个新算子（`contract.emit.evaluate` 一等求值）

| 算子 | 签名 | 语义 |
| --- | --- | --- |
| `rank` / `cross_sectional_rank` | `(child, ascending=False)` | 横截面排名；`ascending=False` ⇒ rank 1 = 最大值（pandas `rank(axis=1, method='average', na_option='keep')`）。两名为同一原语的正式名；`ascending` 必须严格布尔（防 `bool("false")` 坑） |
| `condition` | `(pred, a, b)` | 三元 `np.where`（pred 先 bool 化，NaN 视为 False）；arity=3 |

### `StrategySpec.ranking: Expr | None = None`（数值分数，**越高越优**）

- `emit_weights`：`ranking` 非 None 时 `score = evaluate(ranking, prices)`、降序排序取 `top_n`
  （分数相同时按 symbol_id 稳定排序）；`ranking is None` 回退到**原硬编码动量**（向后兼容，
  既有测试保持绿）。
- **升/降序口径（关键）**：`ranking` 是「越高越优」，`emit_weights` 用 `-scores[s]` 降序。
  故对「越大越好」的因子（如动量），`cross_sectional_rank` 应取 **`ascending=True`**（rank 值
  随因子递增）；`ascending=False` 会反相选股。默认 `False` 是算子的 pandas 原义（rank 1=最大），
  用于「越小越好」的因子。
- 因果性：`ranking` 的 field 叶子**同样**受 G4（shift ≥ 1）约束；G5（lookback 覆盖最大窗口）与
  G6（窗口正整数）统一扫 `ranking`。

### lint / DSL / paper2spec 同步

- `lint.py`：G4/G5/G6 的遍历从 `(entry, exit)` 扩到 `(entry, exit, ranking)`。
- `dsl.py`：新增 `DSL_CROSS_SECTIONAL_OPS` / `DSL_TERNARY_OPS` 并入白名单；`parse_dsl_node`
  支持三算子；`DSL_SCHEMA` 增三形态 + 「ranking 越高越优，取 ascending 方向」规则。
- `paper2spec.py`：`OP_ALIASES` 受控收三算子；`_build` 对 `_MULTI_CHILD_OPS`
  （原 `_BINARY_OPS` + rank/condition）fail-closed 拒绝并引导走受控 DSL（不猜多子结构）。
- 白名单同步测试（`_SUPPORTED_OPS` ↔ `DSL_ALL_OPS`）保持 1:1。

### 验证

- 新增 `tests/test_p14_cross_sectional.py`（33 测试，本地合成面板）：evaluate 语义 / spec 往返 /
  lint 三规则 / 发射 parity（手写未 shift 动量 == 兜底）/ top-3 选择 / 未来扰动 / DSL parse / 白名单同步。
- 全量 `unittest discover` 绿（含 `test_p3_contract`、`test_p5_spec2weights`、`test_p5_dsl`、
  `test_p5_paper2spec`、`test_p5_ma_cross_e2e`）。证据见 EVIDENCE.md §P14。

---

## 8. 执行纪律（重申，违反会导致返工）

1. **不跳步**：Gate 未通过不得进入下一 Phase。
2. **不放水**：不得为通过验证而注释断言、放宽容差、伪造证据；无法验证时**停下记录并等人工确认**。
3. **留证**：每步命令与输出追加到 `docs/deploy/EVIDENCE.md`，无证据视为未完成。
4. **幂等**：所有步骤可重复执行。
5. **不确定就停**：手册未覆盖的选择，先记录现象与候选方案，**不要自行发明架构**。
