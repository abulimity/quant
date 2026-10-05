"""数据源配置（config/sources.toml）—— 声明层（LOCAL_DEPLOYMENT_PLAN.md 附录 A）。

**凭据纪律（CLAUDE.md）**：`credentials_env` 只存环境变量**名**，绝不存明文密钥；
真正的 key 由环境变量提供（`SourceConfig.token()` 只取不存，见 `x2/llm.py` 同款约定）。

与 `x2/llm.py` 对称：文件缺失 → 返回空配置（不报错），让调用方在真正要 ingest 时
才失败（orchestrator 的分发处），从而不阻塞纯合成夹具路径 —— `synthetic` 不依赖本文件。

`provider` / `calendar` / `currency` / `notes` 是**文档与声明**，不参与快照 ID 派生；
快照 ID 只由「source 标签 + 各表契约内容哈希」决定（见 `realdata._derive_snapshot_id`）。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from quantlab.paths import CONFIG_DIR

DEFAULT_SOURCES_PATH = CONFIG_DIR / "sources.toml"


@dataclass(frozen=True)
class SourceConfig:
    """`config/sources.toml` 里一个 `[sources.<id>]` 节的解析结果。"""

    source_id: str
    provider: str = ""            # 适配器/供应商标识（文档用，不参与快照 ID）
    enabled: bool = False
    datasets: tuple[str, ...] = ()
    credentials_env: str = ""     # 环境变量**名**，不是 key 本身
    calendar: str = ""            # exchange-calendars 名（XSHG/XHKG/XNYS）
    currency: str = ""            # 交易币种（原币，ISO-4217）
    notes: str = ""

    def token(self) -> str | None:
        """按 `credentials_env` 读环境变量；空名 / 未设置返回 None。**只取不存**。"""
        if not self.credentials_env:
            return None
        return os.environ.get(self.credentials_env) or None


def _parse_source(source_id: str, data: dict) -> SourceConfig:
    def _str(key: str, default: str = "") -> str:
        return str(data.get(key, default)).strip()

    raw_datasets = data.get("datasets", ())
    if isinstance(raw_datasets, str):
        raw_datasets = [raw_datasets]
    datasets = tuple(str(x).strip() for x in raw_datasets if str(x).strip())

    return SourceConfig(
        source_id=source_id,
        provider=_str("provider"),
        enabled=bool(data.get("enabled", False)),
        datasets=datasets,
        credentials_env=_str("credentials_env"),
        calendar=_str("calendar"),
        currency=_str("currency"),
        notes=_str("notes"),
    )


def load_sources(path: str | Path | None = None) -> dict[str, SourceConfig]:
    """读取 `config/sources.toml`。文件缺失 → 返回 `{}`（未配置，不报错）。"""
    target = Path(path) if path is not None else DEFAULT_SOURCES_PATH
    if not target.is_file():
        return {}
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    sources = data.get("sources", {})
    return {
        source_id: _parse_source(source_id, section)
        for source_id, section in sources.items()
        if isinstance(section, dict)
    }
