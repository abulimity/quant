"""从 tushare 回填剩余数据集到主项目 `D:\\project\\quant\\data`（一次性驱动脚本）。

背景：Claude Code 的 Bash 子进程环境**看不到** `setx` 写入的用户级 `TUSHARE_TOKEN`
（父进程先于 setx 启动，继承的是旧环境）。token 真实落在 Windows 注册表
`HKCU\\Environment\\TUSHARE_TOKEN`，故本脚本用 `winreg` 读回 → 进程内注入
`os.environ["TUSHARE_TOKEN"]`，再调用 `ingest()`。**token 不打印、不落盘**。

用法（从 worktree 根）：
    .\\.venv\\Scripts\\python.exe scripts\\backfill_tushare.py [--source tushare|tushare_index|tushare_hk|tushare_macro|all]

缺省跑全部剩余源；数据落点固定为主项目（与 worktree 数据目录解耦）：
    · 快照根  D:\\project\\quant\\data\\bronze\\tushare\\
    · 台账    D:\\project\\quant\\data\\warehouse.duckdb
"""

from __future__ import annotations

import os
import sys
import winreg
from datetime import date

from quantlab.ingest.orchestrator import ingest

MAIN_DATA = r"D:\project\quant\data"
ROOT = MAIN_DATA + r"\bronze\tushare"
WAREHOUSE = MAIN_DATA + r"\warehouse.duckdb"
START = date(2015, 1, 1)
END = date(2024, 12, 31)

SOURCES = ("tushare", "tushare_index", "tushare_hk", "tushare_macro")


def _read_token() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "TUSHARE_TOKEN")
    except FileNotFoundError as exc:
        raise SystemExit("未在 HKCU\\Environment 找到 TUSHARE_TOKEN；先 setx TUSHARE_TOKEN <token>") from exc
    if not value:
        raise SystemExit("TUSHARE_TOKEN 为空")
    return value


def _run(source: str) -> None:
    print(f"\n=== ingest source={source} ===", flush=True)
    kwargs = {}
    if source != "tushare_hk":
        kwargs = {"start": START, "end": END}
    result = ingest(source=source, root=ROOT, warehouse=WAREHOUSE, **kwargs)
    print(f"snapshot_id: {result.snapshot_id}")
    print(f"status: {result.status}   already_present: {result.already_present}")
    print(f"row_counts: {result.rows}")


def main() -> int:
    os.environ["TUSHARE_TOKEN"] = _read_token()

    targets = sys.argv[1:] or list(SOURCES)
    if "all" in targets:
        targets = list(SOURCES)
    unknown = [t for t in targets if t not in SOURCES]
    if unknown:
        raise SystemExit(f"未知 source: {unknown}；可用: {list(SOURCES)}")

    for source in targets:
        _run(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
