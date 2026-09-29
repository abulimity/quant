> **【归档文献 · 执行时请勿阅读】**
> 本文件是**选型阶段的证据存档**，仅用于追溯"某个许可证是什么""某个版本号从哪来"。
> **部署与开发时不需要阅读本文件** —— 执行所需的全部结论已内联进 `LOCAL_DEPLOYMENT_PLAN.md`。
> 本文件内容**不再维护**，**不得作为执行依据**；与 `LOCAL_DEPLOYMENT_PLAN.md` 冲突处以该手册为准。

---

# 中港美 ETF 回测：开源组件与项目选型对比

核查日期：2026-09-26。适用范围：Windows 本地、免费数据、最近十年、日频或周频 ETF 轮动与资产配置、人民币研究账户、手动运行，不接实盘。

## 1. 结论与对原方案的修正

建议主环境采用：**Python 3.12 + uv + JupyterLab + pandas/NumPy + PyArrow/Parquet + AKShare + yfinance + exchange-calendars + bt**。先用 bt 自带的 ffn 分析能力和 Matplotlib 输出报告，需要完整 HTML 分析报告时再加 QuantStats。

组合优化不在第一个动量案例中安装。后续按研报公式选择：均值方差、Black-Litterman 优先 PyPortfolioOpt；风险预算和多种风险度量优先比较 Riskfolio-Lib 与 skfolio；滚动验证、参数选择优先 skfolio。

原方案把 vectorbt 直接称为“开源版”不够准确：本次读取的官方许可证为 **Apache 2.0 with Commons Clause**，包含限制出售软件及部分相关服务的附加条件，不属于通常所说的无用途限制的标准开源许可。它仍可作为适合个人研究的源码可见工具，但不再列为本项目必须采用的组件。[许可证](https://github.com/polakowo/vectorbt/blob/master/LICENSE.md)

推荐 bt 也基于功能匹配：ETF 轮动主要是“定期选品 → 生成权重 → 再平衡”，bt 提供组合树与可组合的 Algos；当前不需要为了大规模参数扫描或实时交易引入更复杂的框架。[官方入门](https://pmorissette.github.io/bt/intro.html)

**关键边界：bt 和 vectorbt 都不能省掉本项目的跨市场时点、汇率、公司行动和现金约束适配。** 原方案里的人民币统一账户、分数份额、即时兑换等研究假设保持显式；真实多币种现金与结算是另一层需求。

## 2. 研究方法与证据边界

- 读取了 29 个候选 Python 包的 PyPI 元数据，并检查主要项目的官方 README、文档或源码；额外比较 LEAN、FinRL 及基础存储方案。
- 版本、发布日期、Python 要求和 wheel 来自 PyPI；功能与许可证优先参考项目官方资料。文中的“适合程度”“接入成本”是针对本项目的判断，不是性能实测。
- GitHub 匿名 API 已限流，未获取完整提交、issue 响应和发布历史。因此使用“最近 PyPI 发布”作为维护信号之一，不将其等同于项目健康度，也不按 star 排名。
- 当前分支文档可能领先已发布包；上线选型时应复核锁定版本附带的许可证、API 和依赖。Hikyuu、NautilusTrader 已发现元数据与当前分支表述差异。
- 未安装候选包、未跑性能基准、未实际下载行情。“有 Windows wheel”只表示提供了分发文件，不等于所有传递依赖和功能已在本机通过测试。

## 3. 数据获取：保持两个主入口

各行项目名链接为源码入口；版本证据见第 10 节。客户端的开源许可证不等于上游数据免费、可商用、可再分发，也不等于十年历史完整。

| 项目 | 能解决的问题 | 对本项目的缺口或代价 | 结论 |
| --- | --- | --- | --- |
| [AKShare](https://github.com/akfamily/akshare) | 中文生态广，提供境内 ETF 行情等接口；MIT | 聚合多个上游，不同接口字段、单位和复权口径可能不同；免费接口会变化 | **境内主入口**，逐接口固定契约和快照 |
| [yfinance](https://github.com/ranaroussi/yfinance) | Yahoo 行情、公司行动等；Apache-2.0，适合美股和港股 ETF 候选数据 | 非 Yahoo 官方 SDK；限流、代码变更、历史与复权需核验；官方声明数据 API 面向个人使用 | **美国、香港主入口**，汇率先小样本验证 |
| [efinance](https://github.com/Micro-sheep/efinance) | API 较简洁，官方示例包含 ETF、非 A 股行情；MIT | 较依赖东方财富。与 AKShare 的东方财富接口可能同源，不能当独立交叉验证 | **备用适配器**，不与主库无差别混用 |
| [qstock](https://github.com/tkfy920/qstock) | 数据与研究便捷封装；MIT 元数据 | 与现有数据、分析能力重叠，增加封装层；本次 PyPI 最新发布时间早于其他主要数据候选 | 暂不引入；具体接口有独特价值再评估 |
| [Tushare](https://github.com/waditu/tushare) | 国内金融数据 SDK，结构化接口；BSD 元数据 | Tushare Pro 的积分、权限与数据服务另算，不能从 SDK 开源推断所需接口全部免费；也不直接覆盖全部三市场需求 | 免费优先阶段不设为核心；未来付费国内数据候选 |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | 统一多供应商数据入口，提供 Python/REST 接口；当前包 AGPL-3.0-only | 部分供应商仍需 key 或订阅；主包含许多领域模块，单人 ETF 研究的收益暂不足以抵消依赖规模 | 多种宏观、基本面和数据供应商研究时再引入 |

数据侧最有价值的工作是固定标的主表、保存原始快照和核验分红拆分，不是再安装第三、第四个抓取库。同源接口返回一致也不能证明数据正确。

数据获取不放在回测引擎内部自动执行。即使 bt 的 `bt.get` 示例可直接下载，也应将生产研究输入替换成已经冻结和核验的本地数据。

## 4. 回测框架：按任务选择

### 4.1 广泛对比

| 项目 | 强项 | 本项目需要额外处理的内容 | Windows / 许可证 | 判断 |
| --- | --- | --- | --- | --- |
| [bt](https://github.com/pmorissette/bt) | 组合树、按期调仓、目标权重、组合策略比较；适合 ETF 资产配置 | 跨市场异步成交、可交易掩码、汇率、分红口径与现金约束；教程默认逻辑不能直接当三市场生产回测 | 1.2.3 有 CPython 3.12 x64 wheel；MIT | **首选研究引擎** |
| [vectorbt](https://github.com/polakowo/vectorbt) | 数组广播、参数批量实验、多资产分析与图表 | 多币种账户和时点仍要适配；区分免费可用代码与 PRO 功能；当前依赖要求较新 | Python 通用 wheel，依赖含 Numba；Apache-2.0 + Commons Clause | **可选实验工具**，不作为严格开源默认项 |
| [Backtrader](https://github.com/mementum/backtrader) | 事件驱动、多数据流、订单、手续费模型；教程与案例多 | 原生多币种账户不是直接可用的完整方案；现代依赖与绘图兼容性要检查 | 通用 Python 包；GPL-3.0+；最近 PyPI 发布为 2023-04 | 已有 Backtrader 策略代码时可复用；新项目不列首选 |
| [backtesting.py](https://github.com/kernc/backtesting.py) | 单标的择时、参数优化、交互报告，上手容易 | `MultiBacktest` 是多个独立标的回测的包装器，不代表共享资金池的跨标的轮动 | 通用 Python wheel；AGPL-3.0 | 适合单 ETF 择时验算，不作为组合主引擎 |
| [Zipline-reloaded](https://github.com/stefan-jansen/zipline-reloaded) | 事件驱动、数据 bundle、Pipeline 研究生态 | 三市场 bundle、交易日历、币种与国内规则需要工程适配；维护一个 bundle 系统对当前规模偏重 | 含编译依赖，需核验具体 wheel 组合；Apache-2.0 | 研报已有 Zipline/Pipeline 实现时再用 |
| [LEAN](https://github.com/QuantConnect/Lean) | 多资产、订单/费用/结算模型、CashBook 多币种现金账本，后续可接交易 | 中国内地/香港具体市场和数据并非安装即得；仍需逐一验证映射、数据及市场模型 | C# 引擎，支持 Python；CLI 常用 Docker，本地源码构建另有路径；Apache-2.0 | **精细多币种账户的升级候选** |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | 事件驱动、多 venue、多 instrument、账户、精细订单与高频数据，同一架构用于研究和交易 | 类型和账户建模学习成本较高；三市场数据、公司行动与结算仍需验证 | 1.231.0 有 Python 3.12 x64 wheel，不必因 Rust 核心就安装 Rust；发布元数据 LGPL-3.0-or-later | **精细执行的升级候选**，当前工作量偏大 |
| [RQAlpha](https://github.com/ricequant/rqalpha) | 中文文档、国内股票/期货等研究流程、模块化模拟与分析 | 国内生态更匹配；完整中港美和多币种不是本次核验过的即装即用路径；RQData 服务与开源引擎分开 | Python 包；Apache-2.0 | 未来主要复现 A 股研报时重新评估 |
| [VeighNa / vn.py](https://github.com/vnpy/vnpy) | 国内交易接口、事件系统、策略应用；当前 4.x 也有 alpha 研究模块 | 对纯手工日频研究引入了交易平台复杂度；数据权限与各应用能力分别核验 | 官方列出 Windows 支持；MIT，插件另查 | 实盘/交易接口需求出现后再用，不能简单把它只当 CTA 工具 |
| [AKQuant](https://github.com/akfamily/akquant) | Rust 核心、Python 接口、因子表达式和滚动研究能力，中文生态衔接方便 | 本次未证明其中港美多币种、公司行动与异步成交语义完全满足需求；不能照搬宣传性能 | 0.3.64 有 cp310-abi3 Windows x64 wheel；MIT | 值得小规模对照验证，暂不替换主引擎 |
| [Hikyuu](https://github.com/fasiondog/hikyuu) | 本地量化工具体系、C++/Python 和数据管理 | 当前依赖包含 GUI、存储及多种连接器，超出最小研究环境；跨市场账户需另验 | 有 Windows wheel；当前仓库 LICENSE 为 Apache-2.0，但 PyPI license 字段仍写 MIT | 偏国内本地平台场景再考虑，先核对实际分发包许可证 |

依据：各项目官方 README、PyPI 元数据；backtesting.py 的范围直接核查了 [`MultiBacktest` 源码](https://github.com/kernc/backtesting.py/blob/master/backtesting/lib.py)，LEAN 多币种机制核查了 [`CashBook.cs`](https://github.com/QuantConnect/Lean/blob/master/Common/Securities/CashBook.cs)。这里不把“能处理多个数据流”直接解释为“已支持三个市场的真实账户会计”。

### 4.2 为什么当前首选 bt

官方示例直接展示定期调仓、选品、等权及再平衡的组合方式，并通过 ffn 提供结果分析。目标权重驱动的策略与券商 ETF 轮动/资产配置报告更接近。其 MIT 许可和已发布的 Python 3.12 Windows wheel 也降低了首次部署的复杂度。

但不应复制教程里的“本行价格生成权重、本行价格调仓”就结束。具体实施仍需：

1. 先按 UTC 可用时点生成不可回看的信号，保存原始信号时间。
2. 为标的分别确定下一有效交易事件，估值价格和可成交价格分开。
3. 按事件执行交易，未成交卖单不能预先释放资金，休市标的不能按前值成交。
4. 用原方案的现金、汇率、未来数据扰动和公司行动案例验收。

这些规则应作为小范围适配代码。如果适配逐渐扩展成完整挂单、结算和多币种账本，就切换到 LEAN 或 NautilusTrader 的评估，不继续给 bt 添加一个自研交易平台。

### 4.3 vectorbt 保留在什么位置

当问题变成“比较很多回看窗口、持有数量与调仓组合”时，它的批量研究方式很有价值。参数扫描得到的候选策略需要按相同数据快照、信号时点和成本口径复核；快速算出的净值不能作为成交模型已经正确的证据。

当前版本 1.1.1 的包元数据要求 `numpy>=2.4.6`、`pandas>=3.0.3,<4.0`、`numba>=0.66`。旧教程和老版本兼容性结论不能直接沿用。默认不安装 `vectorbt[full]`，因为那会带入大量当前不需要的交易、指标和数据集成。[版本元数据](https://pypi.org/pypi/vectorbt/1.1.1/json)

## 5. 组合优化：按数学模型选一个

| 项目 | 擅长的问题 | 不负责的事情 / 接入注意 | 推荐场景 |
| --- | --- | --- | --- |
| [PyPortfolioOpt](https://github.com/PyPortfolio/PyPortfolioOpt) | 均值方差、协方差收缩、Black-Litterman、HRP；MIT | 不是交易回测引擎；优化器给权重不代表权重可成交。标准 HRP 也不等于一般的等风险贡献优化 | 常见资产配置研报的轻量首选 |
| [Riskfolio-Lib](https://github.com/dcajasn/Riskfolio-Lib) | 多种风险度量、风险平价/预算、HRP/HERC、复杂约束；BSD-3-Clause | 依赖较多，模型与求解器适配要检查；7.3.0 直接依赖 `vectorbt>=0.28.0` | 研报涉及明确风险预算或复杂风险模型时优先评估 |
| [skfolio](https://github.com/skfolio/skfolio) | scikit-learn 风格的组合估计器、风险预算、分层方法、验证与调参；BSD-3-Clause | 不是成交与账户引擎；应使用适当的时间序列/滚动切分，不能将普通随机 K 折当时间安全 | 需要规范样本外验证与多模型比较时优先 |

三者都接收历史信息并生成目标组合，随后仍由 bt 或事件引擎处理持仓和成交。避免同时安装三套优化库；先把研报目标函数、约束、回看窗口、缺失样本规则写清，再选择实现。

**逆波动率配置不等于一般风险平价。** 只有在特定相关性结构等条件下，简单逆波动率才等价于某种等风险贡献配置。复现风险平价研报时，要按协方差矩阵和原文风险贡献定义验算，不能因为某个教程使用了 risk parity 字样就认为已复现。

Riskfolio-Lib 的 BSD 许可只描述其自身，不会覆盖传递依赖的许可。若目标是整个依赖图都采用标准开源许可，应先看具体模型能否由 PyPortfolioOpt/skfolio 覆盖；确需 Riskfolio 时，再核验选定版本完整依赖，不声称“装一个 BSD 包就全是 BSD”。[Riskfolio 7.3.0 依赖](https://pypi.org/pypi/riskfolio-lib/7.3.0/json)

## 6. 绩效分析与因子诊断

| 项目 | 用途 | 对本项目的建议 |
| --- | --- | --- |
| [ffn](https://github.com/pmorissette/ffn) | 净值/收益统计、图表、策略比较；MIT | **随 bt 使用**，第一版通常足够；不再另建重复的统计框架 |
| [QuantStats](https://github.com/ranaroussi/quantstats) | 组合收益分析、风险指标、HTML 报告；Apache-2.0 | **需要成品 HTML 报告时加入**；只传已核验的收益和基准，不让报告层偷偷另下载基准 |
| [Empyrical-reloaded](https://github.com/stefan-jansen/empyrical-reloaded) | 回报和风险指标函数；Apache-2.0 | 需要与既有研究指标对齐时用，不与其他库各自输出一套不一致口径 |
| [Alphalens-reloaded](https://github.com/stefan-jansen/alphalens-reloaded) | IC、分组收益、因子换手等截面诊断；Apache-2.0 | 大资产池、多因子研究时再加；6～12 只 ETF 的分组和 IC 统计应谨慎解释 |

统计库不决定经济口径。必须统一总收益/价格收益、人民币/本币、基准、无风险收益率、日历和年化系数。原方案采用固定北京时间截点的自然日日度估值，如果保留周末，应显式处理 365 的年化口径；不能直接接受某库默认 252 的全部输出。

同一净值曲线的累计收益和最大回撤应可交叉核验；不同库的 CAGR、Sharpe、缺失值规则可能不同，先解释定义差异，再判断是否计算有错。

## 7. 日历、存储与研究平台

### 7.1 日历与时区

| 项目 | 优势 | 选择 |
| --- | --- | --- |
| [exchange-calendars](https://github.com/gerrymanoim/exchange_calendars) | session、开闭市与交易时间的统一表示；Apache-2.0 | **主选**，配合标准库 `zoneinfo` 和 Windows 的 `tzdata` |
| [pandas-market-calendars](https://github.com/rsheftel/pandas_market_calendars) | pandas 风格 schedule，支持中间休市和不同市场日程；MIT | 可替代前者；不要两个库同时成为日历真相来源 |

日历库不能证明某只 ETF 在某天未停牌，也不会自动提供完整十年行情。应核查每只 ETF 的实际交易所、提前收盘、特殊休市和历史覆盖；供应商日行情的日期标签要映射到真实时间戳。

### 7.2 存储与计算

| 组件 | 适用性 | 选择 |
| --- | --- | --- |
| pandas + NumPy | 绝大多数量化库的输入输出生态；当前日频规模已足够 | **主计算层** |
| PyArrow + Parquet | 压缩列存、可移植、无需服务进程，便于快照 | **主数据层**，保留源数据与标准化结果 |
| [DuckDB](https://duckdb.org/docs/stable/data/parquet/overview) | 在本地直接 SQL 查询 Parquet，适合跨文件筛选与聚合；MIT | 文件和查询复杂后添加，不需要部署服务器 |
| [Polars](https://github.com/pola-rs/polars) | 惰性执行、并行表达式，适合较大清洗与因子计算；MIT | pandas 出现实测性能瓶颈时再用，考虑转回 pandas 的成本 |
| SQLite / 标准库 sqlite3 | 元数据、运行清单、简单事务 | 运行管理变复杂再引入；目前 TOML/JSON 清单足够 |

没有必要为几十只 ETF 日线部署 PostgreSQL、ClickHouse 或 Redis。也不建议先上 DVC、MLflow 管理少量手工实验，先保存代码版本、锁文件、配置和数据哈希即可。

### 7.3 综合研究平台

| 项目 | 定位 | 何时适合 |
| --- | --- | --- |
| [Microsoft Qlib](https://github.com/microsoft/qlib) | AI 量化数据处理、训练、回测和研究流程；MIT 元数据 | 明确开始多因子、监督学习、模型对比后；当前动量轮动无需为此转换到专属数据和训练流程 |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 金融强化学习的训练、测试、交易研究流程 | 研报明确使用强化学习时再评估；本次未单独核验其分发许可证，不据此作许可结论 |

Qlib 或 FinRL 并不会自动提供符合本项目要求的三市场十年完整数据。Qlib 本次读取的 README 对官方数据集可用性还有说明，不能将其示例下载脚本当成免费数据服务保证。

## 8. 已核实的依赖和许可问题

### 8.1 不兼容的 pandas 版本区间

| 包版本 | 公开依赖约束 |
| --- | --- |
| vectorbt 1.1.1 | `pandas>=3.0.3,<4.0` |
| Alphalens-reloaded 0.4.6 | `pandas>=1.5.0,<3.0` |

这两个范围无交集，因此不能将这两个指定版本装进同一环境。结论来自包元数据，不需要等安装失败才发现。若以后同时需要，优先拆成独立研究环境并交换 Parquet/CSV，而不是在主环境反复强制升级/降级。

来源：[vectorbt 1.1.1](https://pypi.org/pypi/vectorbt/1.1.1/json)、[Alphalens-reloaded 0.4.6](https://pypi.org/pypi/alphalens-reloaded/0.4.6/json)。旧版 vectorbt 或未来新版 Alphalens 是否兼容需另验，此处不推断。

### 8.2 许可证与传递依赖

- vectorbt 当前官方 LICENSE：Apache-2.0 + Commons Clause，不能只摘取 Apache 标签。[原文](https://github.com/polakowo/vectorbt/blob/master/LICENSE.md)
- Riskfolio-Lib 7.3.0：直接要求 `vectorbt>=0.28.0`，不能假设其安装环境仅含 BSD/MIT 组件。[元数据](https://pypi.org/pypi/riskfolio-lib/7.3.0/json)
- Hikyuu 2.8.2：PyPI license 写 MIT，但包说明和当前仓库 LICENSE 写 Apache-2.0；锁版时以对应分发文件及组件授权为准。[PyPI](https://pypi.org/pypi/hikyuu/2.8.2/json) / [仓库 LICENSE](https://github.com/fasiondog/hikyuu/blob/master/LICENSE)
- NautilusTrader 1.231.0 发布元数据为 LGPL-3.0-or-later，当前 develop README 的依赖许可检查描述又出现 LGPL-3.0-only；不能把开发分支的一句话当成已发布版本授权变更。[版本元数据](https://pypi.org/pypi/nautilus_trader/1.231.0/json)

这里只区分组件性质，不建议为了本地个人研究建立额外审批流程。未来分发软件或提供服务时，重新核验锁定版本和完整依赖许可即可。

## 9. 安装顺序与验证门槛

主环境从以下最小集合开始，具体流程见更新后的部署指南：

```powershell
uv add numpy pandas pyarrow matplotlib jupyterlab ipykernel akshare yfinance exchange-calendars tzdata bt
uv sync --locked
```

`ffn` 是 bt 的直接依赖；项目代码若直接调用 ffn API，再把它声明为项目直接依赖。需要 HTML 报告时执行 `uv add quantstats`。优化器等选定具体研报后再安装，不将候选清单变成安装清单。

版本表记录的是本次观察到的发布，不是已经联合测试的推荐锁版本。先解决依赖、跑样例，再提交 `uv.lock`，不要直接将所有“最新版”当成已兼容组合。

| 验证 | 通过条件 | 能排除的问题 |
| --- | --- | --- |
| 环境 | Python 3.12 Windows 下依赖解析、导入通过 | ABI、缺失 wheel、依赖冲突 |
| 数据 | 三市场各选标的，核验上市、休市、分红拆分、汇率方向 | 代码映射、假缺失、错误复权 |
| 信号 | 修改未来数据不改变过去信号 | 未来数据泄漏 |
| 成交 | 各市场只能在信号之后的有效事件成交，缺现金时受限 | 同日时间错位、前值假成交、透支 |
| 会计 | 手算例子与净值一致；分红不重复；汇率公式吻合 | 净值曲线正确外观下的账本错误 |
| 报告 | 买入持有和简单等权基准可复算 | 年化、成本、基准口径不一致 |

当原研报以单币种同步收盘价进行理论配置时，可以增加同口径的“研报理论复现”视图；同时保留跨市场延迟执行的研究结果，不能悄悄用简化成交替换原定规则。

## 10. 发布与部署证据快照

以下为本次查询时 PyPI 当前版本的上传日期（UTC），不是最后一次代码提交日期。链接指向该版本元数据；最新版未来可能变化。

| 包 | 观察版本 | 上传日期 | 主要含义 |
| --- | --- | --- | --- |
| [bt](https://pypi.org/pypi/bt/1.2.3/json) | 1.2.3 | 2026-09-12 | 有 cp312 Windows x64 wheel |
| [vectorbt](https://pypi.org/pypi/vectorbt/1.1.1/json) | 1.1.1 | 2026-09-26 | 新依赖区间，不能照搬旧教程 |
| [Backtrader](https://pypi.org/pypi/backtrader/1.9.78.123/json) | 1.9.78.123 | 2023-04-19 | 发布较旧，不据此断言项目停更 |
| [backtesting.py](https://pypi.org/pypi/backtesting/0.6.6/json) | 0.6.6 | 2026-07-22 | 单策略/单标的主模型需区分 |
| [Zipline-reloaded](https://pypi.org/pypi/zipline-reloaded/3.1.1/json) | 3.1.1 | 2025-07-19 | 跟进维护分支，不用原 Quantopian 包代替 |
| [NautilusTrader](https://pypi.org/pypi/nautilus_trader/1.231.0/json) | 1.231.0 | 2026-08-02 | Python >=3.12，有 cp312 Windows wheel |
| [vnpy](https://pypi.org/pypi/vnpy/4.4.0/json) | 4.4.0 | 2026-05-14 | 已到 4.x，不能只沿用老 CTA 印象 |
| [RQAlpha](https://pypi.org/pypi/rqalpha/6.4.0/json) | 6.4.0 | 2026-09-18 | 需区分引擎与数据服务 |
| [AKQuant](https://pypi.org/pypi/akquant/0.3.64/json) | 0.3.64 | 2026-09-22 | 有 Windows ABI3 wheel，待功能验证 |
| [Hikyuu](https://pypi.org/pypi/hikyuu/2.8.2/json) | 2.8.2 | 2026-08-19 | 有 Windows wheel，许可元数据不一致 |
| [AKShare](https://pypi.org/pypi/akshare/1.18.97/json) | 1.18.97 | 2026-09-20 | 当前要求 Python >=3.11 |
| [yfinance](https://pypi.org/pypi/yfinance/1.7.0/json) | 1.7.0 | 2026-08-26 | 历史 API 示例应按安装版本校验 |
| [Tushare](https://pypi.org/pypi/tushare/1.4.29/json) | 1.4.29 | 2026-03-25 | SDK 开源与接口权限分开 |
| [efinance](https://pypi.org/pypi/efinance/0.5.9/json) | 0.5.9 | 2026-07-17 | 备用数据适配器 |
| [qstock](https://pypi.org/pypi/qstock/1.3.8/json) | 1.3.8 | 2025-03-16 | 当前优先级低 |
| [OpenBB](https://pypi.org/pypi/openbb/4.7.2/json) | 4.7.2 | 2026-05-26 | 多供应商聚合，依赖范围较广 |
| [Riskfolio-Lib](https://pypi.org/pypi/riskfolio-lib/7.3.0/json) | 7.3.0 | 2026-05-31 | 有 cp312 Windows wheel；依赖 vectorbt |
| [PyPortfolioOpt](https://pypi.org/pypi/PyPortfolioOpt/1.6.0/json) | 1.6.0 | 2026-02-26 | 常见组合优化候选 |
| [skfolio](https://pypi.org/pypi/skfolio/1.3.4/json) | 1.3.4 | 2026-09-25 | 优化与验证候选 |
| [QuantStats](https://pypi.org/pypi/quantstats/0.0.82/json) | 0.0.82 | 2026-09-26 | HTML 报告候选 |
| [Empyrical-reloaded](https://pypi.org/pypi/empyrical-reloaded/0.5.12/json) | 0.5.12 | 2025-06-01 | 指标工具 |
| [Alphalens-reloaded](https://pypi.org/pypi/alphalens-reloaded/0.4.6/json) | 0.4.6 | 2025-06-02 | 要求 pandas <3.0 |
| [ffn](https://pypi.org/pypi/ffn/1.2.2/json) | 1.2.2 | 2026-09-17 | bt 已直接依赖 |
| [exchange-calendars](https://pypi.org/pypi/exchange-calendars/4.13.2/json) | 4.13.2 | 2026-03-10 | 主日历候选 |
| [pandas-market-calendars](https://pypi.org/pypi/pandas-market-calendars/5.4.0/json) | 5.4.0 | 2026-05-27 | 日历替代方案 |
| [DuckDB](https://pypi.org/pypi/duckdb/1.5.5/json) | 1.5.5 | 2026-07-22 | 按需添加本地 SQL |
| [Polars](https://pypi.org/pypi/polars/1.44.2/json) | 1.44.2 | 2026-09-09 | 性能瓶颈出现后再评估 |
| [PyArrow](https://pypi.org/pypi/pyarrow/25.0.1/json) | 25.0.1 | 2026-08-10 | Parquet 基础组件 |
| [Qlib / pyqlib](https://pypi.org/pypi/pyqlib/0.9.7/json) | 0.9.7 | 2025-08-15 | ML 研究阶段再评估 |

## 11. 推荐采用顺序

**现在采用：** AKShare、yfinance、pandas/NumPy、PyArrow/Parquet、exchange-calendars、bt，以及 uv/JupyterLab/Matplotlib。

**具体需要出现时加入：** QuantStats、efinance、PyPortfolioOpt 或 skfolio/Riskfolio-Lib、DuckDB。

**单独评估环境：** vectorbt 批量实验；Alphalens 因子诊断；LEAN/NautilusTrader 精细执行；Qlib 模型研究。

**当前不作为核心：** Backtrader、backtesting.py、Zipline-reloaded、RQAlpha、VeighNa、AKQuant、Hikyuu、OpenBB、FinRL。它们各有适用任务；当前项目不需要承担全部平台的接入成本。
