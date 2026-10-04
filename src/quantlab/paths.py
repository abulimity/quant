r"""路径单一事实来源（canonical 根 vs 检出根）。

区分两种根：

- ``CHECKOUT_ROOT`` —— 代码检出根（orca 里是工作树），仅用于 git 溯源（git_sha / 是否 dirty）。
- ``PROJECT_ROOT``  —— canonical 项目根（数据 / 运行产物 / 环境 / 仓库都从这里取）。
                      默认 = CHECKOUT_ROOT（主检出、干净 clone、CI 无需任何配置）；
                      在 orca 工作树里用环境变量 ``QUANT_ROOT`` 指向主检出 ``D:\project\quant``。

本文件只依赖标准库（os / pathlib），不 import 任何 quantlab 模块，故无循环依赖风险。
"""

from __future__ import annotations

import os
from pathlib import Path

# src/quantlab/paths.py -> src/quantlab -> src -> 项目根（注意比 engines/base.py 浅一层）
CHECKOUT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(os.environ.get("QUANT_ROOT", str(CHECKOUT_ROOT)))

DATA_ROOT = PROJECT_ROOT / "data"
RUNS_DIR = PROJECT_ROOT / "runs"
DUCKDB_PATH = DATA_ROOT / "warehouse.duckdb"
ENVS_DIR = PROJECT_ROOT / "envs"
CONFIG_DIR = PROJECT_ROOT / "config"
