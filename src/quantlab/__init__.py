"""quantlab —— 本地量化研究平台。

模块边界（与 LOCAL_DEPLOYMENT_PLAN.md 各 Phase 对应）：

    store/      存储层：DuckDB 连接、schema DDL、Parquet 快照读写
    fixtures/   合成夹具：供应商留空期间的全部验证载体
    ingest/     接入层：Source 协议、适配器骨架、编排与快照
    engines/    引擎适配层：跨环境桥（core 侧）
    contract/   契约层（P3）
    portfolio/  组合与报告（P6）

约定：本包只装进 core 环境；`envs/vbt` 与 `envs/x2` **不得** import 本包，
一切交换走 `quantlab.engines.bridge` 的 job.json + Parquet + subprocess。
"""

__version__ = "0.1.0"
