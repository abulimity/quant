# 部署证据台账

> 规则（LOCAL_DEPLOYMENT_PLAN.md §0.1-4）：每步执行后把「命令 + 实际输出 + 判定」追加到本文件。
> **无证据的步骤视为未完成。**

---

## P0 · 环境地基

- 执行日期：2026-09-29
- 执行环境：Windows 11 (10.0.26200)，PowerShell，**非管理员**
- 执行者：AI agent

### P0.1 系统与工具核查

命令：

```powershell
[System.Environment]::OSVersion.Version
git --version
uv --version
Get-PSDrive D | Select-Object Used,Free
Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled
```

实际输出：

| 项 | 观测值 | 判定 |
| --- | --- | --- |
| OS 版本 | `10.0.26200.0` | ✅ Windows 11 |
| git | `2.54.0.windows.1` | ✅ |
| uv | `0.11.26 (396ef7ce4 2026-06-30 x86_64-pc-windows-msvc)` | ✅（原设计文档记录为 0.11.21，已更新） |
| D 盘 Free | `296,591,585,280` B ≈ **275.6 GiB** | ✅ ≥ 50 GB |
| LongPathsEnabled | `0` | ❌ **未通过** |

逐条判定：

- [x] **V0** git、uv 均可执行并打印版本
- [ ] **V0** `LongPathsEnabled = 1` —— **失败**
- [x] **V0** D 盘剩余 ≥ 50 GB

失败处置：本会话**非管理员**（`IsInRole(Administrator)` = `False`），无法写 `HKLM`。
按手册 P0.1 失败处理，记为 **`LongPathsEnabled=SKIPPED(no admin)`**，需人工执行（见文末待人工项）。

### P0.2 目录与 IO 优化

命令：

```powershell
New-Item -ItemType Directory -Force -Path data\bronze, data\silver, data\gold, docs\deploy, src\quantlab, tests, runs, config
if (-not (Test-Path docs\deploy\EVIDENCE.md)) { New-Item -ItemType File -Path docs\deploy\EVIDENCE.md }
Add-MpPreference -ExclusionPath 'D:\project\quant\data'
```

实际输出：一级目录 `config, data, docs, runs, src, tests` 全部创建；`Test-Path docs\deploy\EVIDENCE.md` = `True`；`IsInRole(Administrator)` = `False`。

逐条判定：

- [x] **V0** 上述目录全部存在
- [x] **V0** `docs\deploy\EVIDENCE.md` 已创建
- [ ] **V0** Defender 排除项含 `D:\project\quant\data` —— **未执行**（无管理员权限）
- [x] **V0** 未关闭 Defender 的**任何防护功能**

失败处置：记为 **`ExclusionPath=SKIPPED(no admin)`**（手册允许，不阻塞）。需人工执行。

### P0.3 供应商与凭据占位

产出：

- `config/sources.yaml` —— 空模板，`provider`/`credentials_env` 全为空串，**无任何明文密钥**
- `.gitignore` —— 覆盖 `.venv/`、`envs/*/.venv/`、`data/`、`runs/`、`.env`、`*.key`、`*.pem`

逐条判定：

- [x] **V0** `sources.yaml` 中不存在任何真实 key，仅占位符
- [x] **V0** `.gitignore` 覆盖 `.venv/`、`data/`、`runs/`、`.env`
- [ ] **V1** `git check-ignore -v .env` —— **延期至 P1.1**（尚未 `git init`；手册 P0.3 已标注"初始化 git 后执行"）

### 人工裁决记录

| 项 | 裁决 | 日期 | 依据 |
| --- | --- | --- | --- |
| `LongPathsEnabled` | **`GateP0.long_paths=WAIVED`（显式豁免）** | 2026-09-29 | 人工确认。当前目录布局 `data/bronze/<source>/<dataset>/<snapshot_id>/` 层级可控，暂不触及 260 字符上限；若后续出现超长路径报错，再以管理员开启并重启 |
| `ExclusionPath` | `SKIPPED(no admin)` | 2026-09-29 | 仅影响 IO 速度，不影响正确性；可在任意时间以管理员补做 |

> **豁免不等于通过。** 这是人工在"规程要求"与"当前可用性"之间做的**显式取舍**，已按要求留证，不得被后续 agent 解读为"该检查已通过"。

### 🚦 Gate P0 结论：**通过（含 1 项显式豁免）**

| 检查 | 结果 | 备注 |
| --- | --- | --- |
| 工具 | ✅ | git / uv 可用 |
| 长路径 | ⚠️ **WAIVED** | 人工豁免，非通过 |
| 空间 | ✅ | 275.6 GiB |
| 目录 | ✅ | 骨架齐备 |
| 凭据 | ✅ | 无明文密钥；`.gitignore` 就位 |
| 证据 | ✅ | 本文件 |

**→ 准予进入 P1。**（`git check-ignore -v .env` 一项按手册 P0.3 标注，于 P1.1 `git init` 后补验。）

### 待人工执行项（可延后，非阻塞）

```powershell
# 需管理员 PowerShell。仅当出现超长路径报错或需要提速时执行。
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1
Add-MpPreference -ExclusionPath 'D:\project\quant\data'
```

---

## P1 · 仓库骨架与环境隔离

- 执行日期：2026-09-29
- 执行环境：Windows 11 (10.0.26200)，PowerShell，**非管理员**
- 执行者：AI agent
- 依据：`LOCAL_DEPLOYMENT_PLAN.md` §P1（P1.1–P1.5 + Gate P1）

### P1.1 初始化仓库

命令：

```powershell
Set-Location 'D:\project\quant'
git init
git add -A
git commit -q -m "chore: initial docs and skeleton"
git log --oneline
git status --short
git check-ignore -v .env
git check-ignore -v data/
git check-ignore -v runs/
```

实际输出：

```text
Initialized empty Git repository in D:/project/quant/.git/
[commit exit: 0]
40de546 chore: initial docs and skeleton
# git status --short：（空）
.gitignore:20:.env    .env
.gitignore:13:data/   data/
.gitignore:14:runs/   runs/
```

说明：`git add -A` 时出现 `LF will be replaced by CRLF` 警告（仅换行符归一化，无内容影响）；`data/`、`runs/` **未**进入暂存区，忽略规则生效。

逐条判定：

- [x] **V1** `git status` 干净（无未跟踪/未提交文件）
- [x] **V0** `git check-ignore -v data/` 命中（`.gitignore:13`）
- [x] **V0** `git check-ignore -v .env` 命中（`.gitignore:20`）— **P0.3 遗留项补验通过**
- [x] **V0** `git check-ignore -v runs/` 命中（`.gitignore:14`）

**P1.1 完成定义达成**：仓库建立（首提交 `40de546`），忽略规则生效。

---

### ⚠️ P1.2 前置发现：外来环境变量污染（已处置，需人工知悉）

首次执行 `uv add` 时报错：

```text
error: failed to remove directory `\\?\C:\Users\abulimity\AppData\Roaming\Agents Anywhere\connector\.venv\Scripts`: 另一个程序正在使用此文件 (os error 32)
```

排查本机环境变量，发现由**第三方工具「Agents Anywhere」**注入的两个变量：

| 变量 | 值 | 危害 |
| --- | --- | --- |
| `UV_PROJECT_ENVIRONMENT` | `C:\Users\abulimity\AppData\Roaming\Agents Anywhere\connector\.venv` | 会把**任何** uv 项目的虚拟环境都指到该外部路径 |
| `VIRTUAL_ENV` | 同上 | 同上，并干扰 `uv run` 的目标环境判定 |

`UV_PROJECT_ENVIRONMENT` 使**所有环境共用同一个 .venv**，与 §3.1「每环境独立环境」及 Gate P1「每环境独立 uv.lock / 探针」**根本冲突**，且违反 P1.2 验证项「`sys.executable` 指向 `D:\project\quant\.venv`」。

**处置**：本项目所有 uv 命令前，先清除这两个变量（不写回、不持久化）：

```powershell
Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
```

> 判定：这是**移除外部工具对本项目的越界覆盖**，使环境回到手册规定的基线，**不属于** §0.1 禁止的「放宽依赖约束 / 绕过 TLS / --no-deps」。已留证。
>
> 其余 uv 相关变量保留：`UV_DEFAULT_INDEX` / `UV_INDEX_URL` = `https://mirrors.aliyun.com/pypi/simple`（合法 PyPI 镜像，非禁用 TLS），`UV_CACHE_DIR` 指向该外部工具（仅缓存位置，不影响隔离）。
>
> **⚠️ 需人工知悉**：若在别的终端窗口手工执行本项目 uv 命令，务必先清除上述两个变量，否则环境会建到错误位置。

### P1.2 建立 core 环境

命令：

```powershell
Set-Location 'D:\project\quant'
# 先清除外来变量（见上）
uv init --bare --python 3.12
uv python pin 3.12
uv add numpy pandas pyarrow matplotlib jupyterlab ipykernel exchange-calendars tzdata duckdb bt
uv sync --locked
uv add backtrader          # 单独试装，不与其他包同批
```

实际输出（要点）：

```text
Initialized project `quant`              # uv init --bare
Pinned `.python-version` to `3.12`
Using CPython 3.12.13
Creating virtual environment at: .venv
Resolved 129 packages in 3.47s
Installed 125 packages
  + bt==1.2.3   + duckdb==1.5.5   + exchange-calendars==4.13.2
  + matplotlib==3.11.2   + numpy==2.5.3   + pandas==3.0.6
  + pyarrow==25.0.1   + jupyterlab==4.6.3   + ipykernel==7.3.0
  + tzdata==2026.4   + ffn==1.2.2   + yfinance==1.7.0   ...
uv sync --locked   -> Resolved 129 packages / Checked 125 packages, exit=0
uv add backtrader  -> + backtrader==1.9.78.123       # 无冲突，装入 core
```

验证命令与实际输出：

```text
uv run python --version                         -> Python 3.12.13
uv run python -c "…print(sys.executable)"       -> D:\project\quant\.venv\Scripts\python.exe
uv run python -c "import numpy,pandas,pyarrow,duckdb,bt,backtrader,exchange_calendars;print('core imports OK')"
   -> core imports OK   （backtrader 打印 SyntaxWarning: invalid escape sequence '\*'，见下）
uv run python -c "import duckdb;print(duckdb.__version__)"   -> 1.5.5
uv run python -c "import exchange_calendars as xc;print([…('XSHG','XHKG','XNYS')])"
   -> ['XSHG', 'XHKG', 'XNYS']
日历可用范围：
   XSHG first=2006-09-29  last=2026-12-31
   XHKG first=2006-09-29  last=2027-09-29
   XNYS first=2006-09-29  last=2027-09-29
```

逐条判定：

- [x] **V1** `uv run python --version` = `3.12.13`（起点 3.12，**解析通过，未下调**）；`sys.executable` 指向 `D:\project\quant\.venv\Scripts\python.exe` ✅
- [x] **V1** `core imports OK`（含 numpy/pandas/pyarrow/duckdb/bt/backtrader/exchange_calendars）
- [x] **V1** `duckdb 1.5.5` —— 与既有报告观察值 **1.5.5 完全一致** ✅
- [x] **V3** 三个日历 XSHG/XHKG/XNYS 均实例化成功
- [x] **V3** 日历历史范围：三市场均自 **2006-09-29** 起，覆盖最近十年（2016–2026）研究区间，**范围足够** ✅

说明（非失败，供后续知悉）：

- `backtrader 1.9.78.123`（2023-04 发布，与 §1.2 D2 描述一致）在 Python 3.12 下导入时打印 `SyntaxWarning: invalid escape sequence '\*'`（其 `cerebro.py` docstring 中的 `\*`）。**仅告警、不影响导入与运行**；未做任何掩盖。
- `uv` 的 `Failed to hardlink files; falling back to full copy` 提示：缓存目录（在 C 盘）与项目目录（D 盘）跨文件系统，属正常回退，**不影响正确性**。

**P1.2 完成定义达成**：core 环境可导入全部核心包，三个日历可用且范围足够。

**环境边界决策（P1.2 部分）**：backtrader 与 core **可共存**（`uv add backtrader` 无冲突）→ 按 P1.3 决策规则**保留在 core，不创建 `envs/btrader`**。

---

### ⚠️ P1.3 事故 1：`uv init` 自动并入 workspace（**已复现 §3.1 所警告的坑，已修复**）

首次执行：

```powershell
uv init --bare --python 3.12 envs/vbt
uv add --project envs/vbt vectorbt
```

实际输出（节选）：

```text
Adding `vbt` as member of workspace `D:\project\quant`      # ← 危险信号
Initialized project `vbt` at `D:\project\quant\envs\vbt`
```

检查证实**正是手册 §3.1 / Gate P1 严禁的状态**：

```text
根 pyproject.toml:
  [tool.uv.workspace]
  members = [
      "envs/vbt",
  ]
uv.lock 数量：D:\project\quant\uv.lock   ← 只有 1 份（被合并成单一 uv.lock）
envs\vbt\ 内容：只有 pyproject.toml     ← 无自己的 uv.lock / .venv
```

判定：**违反 §3.1「不使用 uv workspace」与 Gate P1「每环境独立 uv.lock」**。根因是 `uv init` 在父项目内创建子项目时**默认把子项目登记进 workspace**。

修复（幂等、可重复）：

```powershell
# 1) 从根 pyproject.toml 删除 [tool.uv.workspace] 段
# 2) 删除被污染的 envs/vbt，改用 --no-workspace 重建为独立项目
Remove-Item -Recurse -Force envs\vbt
uv init --bare --no-workspace --python 3.12 envs/vbt
uv add --project envs/vbt vectorbt
# 3) 重新锁根项目，剔除被并入的 vectorbt
uv lock
uv sync --locked
```

结果：

```text
Initialized project `vbt` at `D:\project\quant\envs\vbt`    # 不再出现 "member of workspace"
根 pyproject.toml 中 workspace 段：已不存在（Select-String 无命中）
uv lock（根）      -> Resolved 130 packages；Removed vectorbt/vbt/numba/plotly 等 16 个包
uv sync --locked（根）-> Uninstalled 16 packages（vectorbt 及 vbt 已从根环境剔除）
uv.lock 现况：根 1 份 + envs/vbt 1 份（见下方 Gate P1 复核）
```

> **教训（供后续环境复用）**：`uv init` 建子环境**必须加 `--no-workspace`**，否则会产生 `[tool.uv.workspace]` 与单一 `uv.lock`。

### P1.3 · envs/vbt（vectorbt）建立过程

```powershell
uv python pin --project envs/vbt 3.12
uv sync --project envs/vbt
```

实际输出（要点）：

```text
Pinned `envs\vbt\.python-version` to `3.12`
Using CPython 3.12.13
Creating virtual environment at: envs\vbt\.venv
Resolved 62 packages / Installed 59 packages
  + vectorbt==1.1.0   + pandas==3.0.6   + numpy==2.5.3
  + numba==0.67.0   + llvmlite==0.49.0   + plotly==7.1.0   ...
```

说明：`--no-workspace` 建的子项目未自动生成 `.python-version`，导致首次解析选中了 **CPython 3.14.5**；因手册起点为 3.12 且 3.12 可解析（`Requires-Python: <3.15,>=3.11`），已用 `uv python pin` 显式固定为 **3.12.13**，并重建 `.venv`。

### 🛑 P1.3 事故 2（**阻塞项，已按 §0.1-6 停下等人工确认**）：vectorbt 与 plotly 运行时不兼容

验证命令：

```powershell
uv run --project envs/vbt python -c "import vectorbt as vbt;print(vbt.__version__)"
```

实际输出（节选）：

```text
File "...\vectorbt\_settings.py", line 96, in register_template
    pio.templates["vbt_" + theme] = go.layout.Template(...)
...
Bad property path:
scattermapbox
^^^^^^^^^^^^^
Did you mean "scattermap"?
```

**根因**：

| 事实 | 证据 |
| --- | --- |
| vectorbt 1.1.0 仅声明 `plotly>=4.12.0`（**无上界**） | `.venv\Lib\site-packages\vectorbt-1.1.0.dist-info\METADATA:26` |
| uv 因此解析出最新 plotly **7.1.0** | vbt `.venv` 解析结果 |
| plotly 7.x 已将 `scattermapbox` 更名为 `scattermap`，vectorbt 1.1.0 的内置模板仍用旧名 | 上面的报错 |
| 索引上 vectorbt 最新版即 **1.1.0**（无 1.1.1；归档文献所记 1.1.1 在索引中不存在） | 索引 `simple/vectorbt/` 版本列表止于 1.1.0 |
| vectorbt `Requires-Python: <3.15,>=3.11` → **Python 版本不是原因** | METADATA:19 |

**为何停下**：这是**手册未覆盖**的情形。P1.3 决策表把「为迁就 A 而降低 B 的版本」列为**禁止**，其给出的出路是「拆环境」——但 plotly 是 vectorbt 的**传递依赖、已在该隔离环境内部**，**无法再拆分**；也没有 vectorbt 的替代实现可换。故按 §0.1-6「不确定就停：先记录现象与候选方案，停下等人工确认」处置。

**候选方案（待人工确认，尚未执行）**：

| # | 方案 | 影响面 | 备注 |
| --- | --- | --- | --- |
| A | 仅在 `envs/vbt` 内加约束 `plotly<7`（如 `uv add --project envs/vbt "plotly<7"`） | **仅隔离环境的传递依赖**，core 不受影响 | 给 vectorbt 自身依赖补上界，是其可用性所需 |
| B | 记录为阻塞，暂停 P1，等人工给出方案 | 全流程暂停 | 最保守 |
| C | 换 vectorbt 的替代实现 | 需改架构 | 与「四引擎」既定决策冲突，且手册未提供替代 |

**人工裁决（2026-09-29）**：采用**方案 A** —— 仅在 `envs/vbt` 内固定 `plotly<7`。

执行与结果：

```powershell
uv add --project envs/vbt "plotly<7"
uv run --project envs/vbt python -c "import vectorbt as vbt;print('vectorbt', vbt.__version__)"
uv run --project envs/vbt python -c "import vectorbt as vbt,pandas as pd,numpy as np;c=np.cumsum(np.random.RandomState(0).randn(200));pf=vbt.Portfolio.from_holding(pd.Series(c),init_cash=1e5);print('total_return=',pf.total_return())"
```

实际输出：

```text
uv add "plotly<7"  -> - plotly==7.1.0  /  + plotly==6.9.0
import vectorbt    -> vectorbt 1.1.0
numba JIT 组合测试 -> total_return= 7.039499882557456   （exit=0）
```

判定：

- [x] **V1** `import vectorbt as vbt` 成功，`vbt.__version__` = `1.1.0`
- [x] **V2** `Portfolio.from_holding(...)` 算出结果（`total_return=7.0395`），**证明 numba JIT 链可用**
- [x] 约束仅落在 `envs/vbt`（`plotly<7` 写入该环境 `pyproject.toml`），**core 与其它环境不受影响**
- [x] **未**使用 `--no-deps` / `pip --force` / 关闭 TLS；**未**为通过验证而放宽任何断言

> 说明：此项为**人工显式裁决后的隔离环境内传递依赖上界**，与 P1.3「禁止为迁就 A 降低 B 版本」所指的**跨组件主版本妥协**不同（后者会牵动 core；本项仅在 vbt 内部闭合其自身依赖）。已按要求留证。

### P1.3 · envs/x2（x2strategy + litellm）建立过程

**先读仓库说明（遵守风险 #2：不猜包名硬装）。** 用 `git ls-remote` 确认可达后，浅克隆到临时目录（`%TEMP%\x2strategy_inspect`，**不进项目**）并阅读其 `pyproject.toml` / `README.md`：

| 事实 | 值 | 来源 |
| --- | --- | --- |
| 发行名 | `x2strategy` | `pyproject.toml` `name` |
| 版本 / 许可 | `0.4.0` / Apache-2.0 | `pyproject.toml` |
| `requires-python` | **`>=3.11`** | `pyproject.toml` |
| **可导入模块** | **`paper2spec`、`spec2code`**（**没有** `x2strategy` 模块） | `[tool.hatch.build.targets.wheel] packages = ["paper2spec","spec2code"]` |
| 依赖 | backtrader / litellm / matplotlib / numpy / pandas / PyMuPDF / pymupdf4llm / python-dotenv / yfinance | `pyproject.toml` |
| 官方安装方式 | **作为 Agent Skill**：`git clone … ~/.claude/skills/x2strategy` + `uv sync --all-extras`；亦支持 `pip install -e ".[codegen,agent,docx,dev]"` | `README.md` §Getting Started |
| 可选 extras | `agent` / `codegen` / `docx` / `dev` | `pyproject.toml` |

> 结论：仓库**同时是合法的可安装包**（hatchling 构建），故手册 P1.3 的 git 依赖方式可行；但**导入名不是 `x2strategy`**，验证须用 `paper2spec`/`spec2code`（与手册「模块名以实际包为准」一致）。仓库 `requires-python` 允许 3.12，故本环境用 **3.12**（起点即通过，无需下调）。

命令：

```powershell
uv init --bare --no-workspace --python 3.12 envs/x2
uv python pin --project envs/x2 3.12
uv add --project envs/x2 litellm
uv add --project envs/x2 "x2strategy @ git+https://github.com/ALAGENT-HKU/x2strategy"
```

实际输出（要点）：

```text
Initialized project `x2` at `D:\project\quant\envs\x2`      # 无 "member of workspace"
Pinned `envs\x2\.python-version` to `3.12`
uv add litellm     -> + litellm==1.102.0（共装 56 包）
uv add x2strategy  -> Building/Built x2strategy @ git+…@e9cd907da492cde5cc5c4e70c78c71e78b288d4f
                      + x2strategy==0.4.0 (from git+…@e9cd907…)
                      + backtrader==1.9.78.123  + pymupdf==1.28.2  + pymupdf4llm==1.28.2
                      + pandas==3.0.6  + numpy==2.5.3  + matplotlib==3.11.2  + yfinance==1.7.0 …
```

验证命令与实际输出：

```text
uv run --project envs/x2 python -c "import paper2spec, spec2code; print('x2 modules OK:', paper2spec.__name__, spec2code.__name__)"
   -> x2 modules OK: paper2spec spec2code          （exit=0）
uv run --project envs/x2 python -c "from importlib.metadata import version; …"
   -> litellm 1.102.0 / x2strategy 0.4.0 / backtrader 1.9.78.123 / pandas 3.0.6 / numpy 2.5.3
uv run --project envs/x2 python -c "import backtrader;print('backtrader', backtrader.__version__)"
   -> backtrader 1.9.78.123
uv run --project envs/x2 python -c "import x2strategy"     # 负向确认
   -> ModuleNotFoundError: No module named 'x2strategy'      （exit=1，符合预期）
```

逐条判定：

- [x] **V1** x2 环境可导入其核心模块（`paper2spec`、`spec2code`）与 `litellm`、`backtrader`
- [x] **V0** 记录实际导入名与官方安装方式（见上表），**未猜测包名硬装**
- [x] 备注（供 P5 知悉，非失败）：
  - `litellm` **不提供 `litellm.__version__`**（1.102.0 无该属性）→ 手册 P5.1 的 `litellm.__version__` 检查届时须改用 `importlib.metadata.version("litellm")`。
  - `import litellm` 时尝试拉取远端 model cost map（`raw.githubusercontent.com`）**读取超时**，自动回退本地备份。**不影响导入**，但说明该 CDN 在本机不可达（P5 需注意）。
- [x] 备注：x2strategy 的 `agent`/`codegen`/`docx`/`dev` extras **未安装**（手册 P1.3 未要求）；后续 P5.2 若需 PDF/RAG 能力再按需补。

### P1.3 · 环境边界表（**Gate P1 要求留证**）

| 环境 | 目录 | 最终 Python | 核心包（实测版本） | 版本决策理由 |
| --- | --- | --- | --- | --- |
| **core** | 根 `D:\project\quant` | **3.12.13** | pandas 3.0.6 / numpy 2.5.3 / duckdb 1.5.5 / pyarrow 25.0.1 / bt 1.2.3 / **backtrader 1.9.78.123** / exchange-calendars 4.13.2 / matplotlib 3.11.2 / jupyterlab 4.6.3 | 起点 3.12 **解析通过**，无需下调 |
| **vbt** | `envs\vbt` | **3.12.13** | vectorbt 1.1.0 / **plotly 6.9.0（人工裁决加 `plotly<7` 上界）** / numba 0.67.0 / pandas 3.0.6 / numpy 2.5.3 | 起点 3.12 解析通过；vectorbt `Requires-Python: <3.15,>=3.11`；plotly 上界见「事故 2」 |
| **x2** | `envs\x2` | **3.12.13** | x2strategy 0.4.0（`paper2spec`/`spec2code`）/ litellm 1.102.0 / backtrader 1.9.78.123 / pymupdf 1.28.2 | 仓库 `requires-python >=3.11`，**3.12 满足**，无需下调 |
| **btrader** | **不创建** | — | backtrader 保留在 core | backtrader 与 core **无冲突**，按决策表不拆 |

**边界决策依据（逐条对应 P1.3 决策表）**：

- backtrader 与 core 可共存 → **保留在 core，不创建 `envs/btrader`**。
- vectorbt**独立环境**（`envs/vbt`），**未与任何其他组件同环境**（强制项）。
- x2strategy 与 litellm 同处 `envs/x2`，解析无冲突；**未为迁就它改动 core 的 pandas 主版本**（core 的 pandas 3.0.6 由 core 自身解析决定，与 x2 无关）。

**「无环境靠放宽组件约束才装上」核对（V3）**：逐环境核对 `uv.lock` 中的版本即该组件当前可用版本——core / vbt / x2 均为解析器自然选出的版本；**唯一的人工干预是 `plotly<7`（收窄上界，非放宽）**，且经人工裁决留证。**未**出现 `--no-deps`、手工 force install、关闭 TLS。

### P1.3 · 结构化验证

```powershell
Get-ChildItem -Recurse -Filter uv.lock -Force | Where-Object { $_.FullName -notmatch '\\\.venv\\' }
Get-ChildItem -Recurse -File -Filter pyproject.toml | Select-String 'tool.uv.workspace'
```

实际输出：

```text
D:\project\quant\uv.lock
D:\project\quant\envs\vbt\uv.lock
D:\project\quant\envs\x2\uv.lock
count = 3
[tool.uv.workspace] 命中：0（无）
envs\vbt: .python-version, pyproject.toml, uv.lock
envs\x2 : .python-version, pyproject.toml, uv.lock
```

逐条判定：

- [x] **V0** **每个环境有各自独立的 `uv.lock`**（3 份；回退情形未触发，故非 4 份）
- [x] **V0** 根 `pyproject.toml`（及任何 pyproject）中**不存在** `[tool.uv.workspace]` 段
- [x] **V1** `import vectorbt` → `1.1.0`
- [x] **V2** vectorbt numba JIT 组合测试 → `total_return=7.0395`
- [x] **V1** x2 环境导入 `paper2spec`/`spec2code` 成功
- [x] **V0** 每环境 Python 版本与理由已记录（见上表）
- [x] **V3** 无环境靠放宽组件约束才装上（见上）

**P1.3 完成定义达成**：环境边界表确定并留证；每个环境可导入其核心包。

---

### P1.4 环境探针

**一处必要决策（留证）**：手册 P1.4 的探针结构把 `duckdb_read_write` 列为**每个环境**的检查项，且 V3 要求其为 `true`；但 `duckdb` 原本只在 core。为使三个环境的探针**口径一致且真实全绿**，向 `envs/vbt`、`envs/x2` 各补装 `duckdb`（1.5.5，纯 Python 包、体积小、无副作用）。**未**用「跳过检查 / 伪造 true」的方式蒙混。

```powershell
uv add --project envs/vbt duckdb      # + duckdb==1.5.5
uv add --project envs/x2  duckdb      # + duckdb==1.5.5
```

产出文件（每环境独立、互不 import）：

| 环境 | 探针路径 |
| --- | --- |
| core | `src/quantlab/probe.py` |
| vbt | `envs/vbt/probe.py` |
| x2 | `envs/x2/probe.py` |

探针实现要点：`import_ok` 逐个导入核心包并取版本；`duckdb_read_write` 在**内存库**建临时表 → 写入 `(1,'x'),(2,'y')` → 读回比对 → `DROP` 并确认无残留；`spawn_guard_ok` 用 `multiprocessing` 的 **spawn** 上下文**真实起一个子进程**执行最小任务（`21→42`）并回收结果——若入口缺 `if __name__ == "__main__":` 保护，子进程会重跑本模块而暴露问题。

命令与实际输出：

```text
Set-Location 'D:\project\quant'
uv run python src/quantlab/probe.py                       -> core exit=0
uv run --project envs/vbt python envs/vbt/probe.py        -> vbt  exit=0
uv run --project envs/x2  python envs/x2/probe.py         -> x2   exit=0
```

```jsonc
// core
{ "env": "core", "python": "3.12.13",
  "executable": "D:\\project\\quant\\.venv\\Scripts\\python.exe",
  "packages": {"numpy":"2.5.3","pandas":"3.0.6","pyarrow":"25.0.1","duckdb":"1.5.5",
               "bt":"1.2.3","backtrader":"1.9.78.123","exchange-calendars":"4.13.2","matplotlib":"3.11.2"},
  "checks": {"import_ok": true, "duckdb_read_write": true, "spawn_guard_ok": true},
  "details": {"import_errors": [], "duckdb": "写读一致=True, 清理后残留表数=0", "spawn": "子进程返回 [42]"} }
// vbt
{ "env": "vbt", "python": "3.12.13",
  "executable": "D:\\project\\quant\\envs\\vbt\\.venv\\Scripts\\python.exe",
  "packages": {"vectorbt":"1.1.0","numba":"0.67.0","plotly":"6.9.0","pandas":"3.0.6","numpy":"2.5.3","duckdb":"1.5.5"},
  "checks": {"import_ok": true, "duckdb_read_write": true, "spawn_guard_ok": true},
  "details": {"import_errors": [], "duckdb": "写读一致=True, 清理后残留表数=0", "spawn": "子进程返回 [42]"} }
// x2
{ "env": "x2", "python": "3.12.13",
  "executable": "D:\\project\\quant\\envs\\x2\\.venv\\Scripts\\python.exe",
  "packages": {"x2strategy":"0.4.0","litellm":"1.102.0","backtrader":"1.9.78.123","pandas":"3.0.6","numpy":"2.5.3","duckdb":"1.5.5"},
  "checks": {"import_ok": true, "duckdb_read_write": true, "spawn_guard_ok": true},
  "details": {"import_errors": [], "duckdb": "写读一致=True, 清理后残留表数=0", "spawn": "子进程返回 [42]"} }
```

逐条判定：

- [x] **V1** 三个探针均可执行且**退出码 0**
- [x] **V3** `spawn_guard_ok` 为 `true`：**真实起 spawn 子进程**执行 `21→42` 并回收结果
- [x] **V3** `duckdb_read_write` 为 `true`：写入读回一致（`写读一致=True`），且清理后**残留表数=0**
- [x] 探针各自指向**本环境的** `.venv` 解释器（三份 `executable` 互不相同，佐证隔离生效）

备注（非失败）：backtrader 导入时的 `SyntaxWarning: invalid escape sequence '\*'` 出现在**标准错误**，**不影响** stdout 的 JSON 与退出码。

> 说明：手册 P4.4 另要求「未加 spawn 保护时能复现失败」的负向对照；那属 **P4.4**，本阶段（P1.4）只要求 `spawn_guard_ok=true`，故未在此展开。

**P1.4 完成定义达成**：一条命令即可判定任一环境是否健康。

---

### P1.5 跨环境桥

产出：

| 文件 | 作用 |
| --- | --- |
| `src/quantlab/engines/bridge.py` | core 侧桥：`run_in_env(env, entry, job, inputs, workdir) -> dict`、`env_lock_hash()`、`BridgeError` |
| `envs/vbt/entry.py` | vbt 环境入口桩（读 job.json → 执行 → 写 result.json） |
| `envs/x2/entry.py` | x2 环境入口桩（同上） |

实现要点：

- 严格执行 §3.1 文件交换：写 `job.json`（含 `job_id / env / engine / entry / params / inputs / outputs / env_lock_sha256`）→ `uv run --project envs/<env> python <entry> <job.json>` → 读回 `result.json`；结果落在 `runs/<job_id>/`。
- **失败可传播**：非零退出抛 `BridgeError`（携带 `returncode` 与 `stderr`），**不静默吞掉**；未产出 `result.json` 同样报错。
- **子进程环境清洗**：`run_in_env` 显式剔除 `UV_PROJECT_ENVIRONMENT` / `VIRTUAL_ENV` / `UV_RUN_RECURSION_DEPTH`，否则外部变量会把目标环境指错位置、或令嵌套 `uv run` 误判（见 P1.2 前置发现）。
- 两个 `entry.py` 均含 `if __name__ == "__main__":` 保护（Windows spawn 必需）。

命令与实际输出（自检同时覆盖 V2 与 V3）：

```powershell
Set-Location 'D:\project\quant'
uv run python src/quantlab/engines/bridge.py
```

```text
V2 vbt add -> {"job_id": "selftest-vbt-add", "env": "vbt", "op": "add", "a": 2, "b": 40, "sum": 42}
V2 x2  add -> {"job_id": "selftest-x2-add", "env": "x2", "op": "add", "a": 20, "b": 22, "sum": 42}
V3 vbt raise -> 已传播：returncode=1, 'boom-from-vbt' in stderr=True
V3 lock hash -> job.json=7d297fbc5545… actual=7d297fbc5545…

SELFTEST OK
exit=0
```

逐条判定：

- [x] **V2** 端到端：core 调用 `envs/vbt/entry.py` 算加法（`2+40`）并正确读回 `sum=42`；`envs/x2` 同样可被调用（`20+22=42`），证明桥与环境无关
- [x] **V3** 目标环境**故意抛异常**时，core 拿到**非零退出码（1）**与错误信息（`boom-from-vbt` 出现在 stderr），**未被静默吞掉**
- [x] **V3** `job.json` 记录的 `env_lock_sha256` 与实际 `uv.lock` 哈希**一致**（`7d297fbc5545…`）
- [x] 嵌套 `uv run --project`（core 的 uv run 内再调 vbt 的 uv run）**工作正常**（环境清洗生效）

**P1.5 完成定义达成**：跨环境调用与失败传播均可验证。

---
