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
