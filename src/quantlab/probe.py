"""环境探针（core 环境）。

目的：把「该环境是否可用」变成一条可重复执行的命令，作为后续所有步骤的前置检查。

用法：
    uv run python src/quantlab/probe.py

输出：单份 JSON（stdout）。
退出码：全部检查通过 → 0；任一项失败 → 1（失败不静默）。
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ENV = "core"

# (导入名, 分发名)：导入名用于验证可导入，分发名用于取版本号
PACKAGES: list[tuple[str, str]] = [
    ("numpy", "numpy"),
    ("pandas", "pandas"),
    ("pyarrow", "pyarrow"),
    ("duckdb", "duckdb"),
    ("bt", "bt"),
    ("backtrader", "backtrader"),
    ("exchange_calendars", "exchange-calendars"),
    ("matplotlib", "matplotlib"),
]


def _safe_version(dist: str) -> str | None:
    try:
        return version(dist)
    except PackageNotFoundError:
        return None


def check_imports() -> tuple[bool, dict[str, str | None], list[str]]:
    """导入全部核心包并取版本；返回 (是否全部成功, 版本表, 错误列表)。"""
    versions: dict[str, str | None] = {}
    errors: list[str] = []
    for imp, dist in PACKAGES:
        try:
            __import__(imp)
        except Exception as exc:  # 探针需报告任何导入失败，不吞异常
            errors.append(f"import {imp}: {type(exc).__name__}: {exc}")
        versions[dist] = _safe_version(dist)
    return (not errors), versions, errors


def check_duckdb_read_write() -> tuple[bool, str]:
    """内存库建临时表 → 写入 → 读回一致 → 清理（DROP）。"""
    try:
        import duckdb
    except Exception as exc:
        return False, f"duckdb 不可用: {type(exc).__name__}: {exc}"
    try:
        con = duckdb.connect(":memory:")
        try:
            con.execute("CREATE TEMP TABLE probe_t(a INTEGER, b VARCHAR)")
            con.execute("INSERT INTO probe_t VALUES (1, 'x'), (2, 'y')")
            rows = con.execute("SELECT a, b FROM probe_t ORDER BY a").fetchall()
            con.execute("DROP TABLE probe_t")
            left = con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'probe_t'"
            ).fetchone()
        finally:
            con.close()
        consistent = rows == [(1, "x"), (2, "y")]
        cleaned = left == (0,)
        return (consistent and cleaned), f"写读一致={consistent}, 清理后残留表数={left[0]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _spawn_worker(value: int) -> int:
    """spawn 子进程中执行的最小任务（须为模块级、可 pickle 的函数）。"""
    return value * 2


def check_spawn_guard() -> tuple[bool, str]:
    """起一个 spawn 子进程执行最小任务并回收结果。

    若入口缺少 `if __name__ == "__main__":` 保护，spawn 子进程会重新执行本模块，
    从而暴露问题——这是 numba / vectorbt 在 Windows 上的常见坑。
    """
    try:
        ctx = mp.get_context("spawn")
    except Exception as exc:
        return False, f"spawn context 不可用: {type(exc).__name__}: {exc}"
    try:
        with ctx.Pool(processes=1) as pool:
            got = pool.map(_spawn_worker, [21])
        return got == [42], f"子进程返回 {got}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def build_report() -> dict:
    import_ok, packages, import_errors = check_imports()
    duck_ok, duck_detail = check_duckdb_read_write()
    spawn_ok, spawn_detail = check_spawn_guard()
    return {
        "env": ENV,
        "python": sys.version.split()[0],
        "executable": str(Path(sys.executable)),
        "packages": packages,
        "checks": {
            "import_ok": import_ok,
            "duckdb_read_write": duck_ok,
            "spawn_guard_ok": spawn_ok,
        },
        "details": {
            "import_errors": import_errors,
            "duckdb": duck_detail,
            "spawn": spawn_detail,
        },
    }


def main() -> int:
    report = build_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not all(report["checks"].values()):
        print("PROBE FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
