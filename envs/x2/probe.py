"""环境探针（x2 环境，x2strategy + litellm）。

用法：uv run --project envs/x2 python envs/x2/probe.py
输出：单份 JSON（stdout）。退出码：全绿 → 0；否则 → 1。

注意：
- 本文件在隔离环境内**独立存在**，不 import core 的任何代码。
- 发行名为 `x2strategy`，但可导入模块为 `paper2spec` / `spec2code`（无 `x2strategy` 模块）。
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ENV = "x2"

PACKAGES: list[tuple[str, str]] = [
    ("paper2spec", "x2strategy"),
    ("spec2code", "x2strategy"),
    ("litellm", "litellm"),
    ("backtrader", "backtrader"),
    ("pandas", "pandas"),
    ("numpy", "numpy"),
    ("duckdb", "duckdb"),
]


def _safe_version(dist: str) -> str | None:
    try:
        return version(dist)
    except PackageNotFoundError:
        return None


def check_imports() -> tuple[bool, dict[str, str | None], list[str]]:
    versions: dict[str, str | None] = {}
    errors: list[str] = []
    for imp, dist in PACKAGES:
        try:
            __import__(imp)
        except Exception as exc:
            errors.append(f"import {imp}: {type(exc).__name__}: {exc}")
        versions[dist] = _safe_version(dist)
    return (not errors), versions, errors


def check_duckdb_read_write() -> tuple[bool, str]:
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
    return value * 2


def check_spawn_guard() -> tuple[bool, str]:
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
