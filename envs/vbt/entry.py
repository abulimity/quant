"""目标环境入口（vbt）—— LOCAL_DEPLOYMENT_PLAN.md §P4.4。

契约（手册 P1.5）：
    argv[1] = job.json 路径
    job.json = {job_id, env, engine, entry, params, inputs, outputs, env_lock_sha256}
    结果写入 job["outputs"]["result_json"]；可选 result.parquet

本文件在隔离环境内**独立存在**，**不 import core 的任何代码**（环境之间只经
job.json + Parquet 交换）。

支持的操作（`params.op`）：

    add     —— 桥的最小烟测（P1.5 遗留）
    raise   —— 故意失败，验证「目标环境失败可传播、不被静默吞掉」
    scan    —— **P4.4**：对输入价格序列做一次小规模 SMA 参数扫描

`scan` 的**产出标注（§P4.4 V3 强制）**：
    `match_quality = "coarse_screen_only"` 且带 `note` —— vectorbt 默认**不建模**
    撮合细节（成交价口径、滑点、手数、现金约束），故其结果**不得**当作成交模型
    正确的证据，只能用于粗筛。该字段是机器可读的，避免被下游误用。

**为什么必须有 `if __name__ == "__main__":` 保护**（§P4.4 强制）：
Windows 用 `spawn` 起子进程，子进程会**重新导入本模块**；若入口代码写在模块顶层，
导入即触发 `main()`，于是无限自我复刻。这条保护不是风格偏好，是**必需**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 粗筛产物标注：机器可读，防止下游把它当成「成交模型正确的证据」
MATCH_QUALITY_COARSE = "coarse_screen_only"
COARSE_NOTE = (
    "vectorbt 粗筛结果：仅按收盘价做向量化回测，**未建模**撮合细节"
    "（成交价口径、滑点、最小手数、现金约束）。不得作为成交模型正确的证据。"
)


def _op_add(job: dict, params: dict) -> dict:
    return {
        "job_id": job.get("job_id"), "env": job.get("env"), "op": "add",
        "a": params.get("a"), "b": params.get("b"),
        "sum": params.get("a", 0) + params.get("b", 0),
    }


def _op_scan(job: dict, params: dict):
    """SMA 快速/慢速参数网格扫描。返回 (result_dict, 结果 DataFrame)。"""
    import pandas as pd
    import vectorbt as vbt

    inputs = job.get("inputs", {})
    if "prices" not in inputs:
        raise ValueError("scan 需要 inputs['prices'] 指向一个 Parquet（index=ts, 单列 close）")

    # 用 **DuckDB** 读 Parquet，而不是 pandas 的 read_parquet：
    # `envs/vbt` 里没有 pyarrow / fastparquet（也**不应该**为读一个 Parquet
    # 去动那个隔离环境、并让 vectorbt 的求解变复杂），而 duckdb 本来就在。
    # 这也正合本平台「**Parquet 是真相，DuckDB 是查询层**」的定位。
    import duckdb

    prices = duckdb.sql(
        f"SELECT close FROM read_parquet('{inputs['prices'].replace(chr(92), '/')}')"
        " ORDER BY ts").df()["close"]
    fast_grid = list(params.get("fast", [5, 10, 20]))
    slow_grid = list(params.get("slow", [30, 60]))

    rows = []
    for fast in fast_grid:
        for slow in slow_grid:
            if fast >= slow:
                continue
            fast_ma = prices.rolling(fast, min_periods=fast).mean()
            slow_ma = prices.rolling(slow, min_periods=slow).mean()
            above = fast_ma > slow_ma
            entries = (above & ~above.shift(1).fillna(False)).fillna(False).astype(bool)
            exits = (~above & above.shift(1).fillna(False)).fillna(False).astype(bool)
            if not bool(entries.any()) and not bool(exits.any()):
                continue                     # 无交易则跳过，避免空组合噪声
            portfolio = vbt.Portfolio.from_signals(
                prices, entries, exits, init_cash=1_000_000.0, freq="1D",
                fees=0.0, slippage=0.0)
            rows.append({
                "fast": int(fast), "slow": int(slow),
                "total_return": float(portfolio.total_return()),
                "n_trades": int(len(portfolio.trades.records_readable)),
                "max_drawdown": float(portfolio.max_drawdown()),
            })

    frame = pd.DataFrame(rows)
    result = {
        "job_id": job.get("job_id"), "env": job.get("env"), "op": "scan",
        "n_combos": int(len(frame)),
        "n_sessions": int(len(prices)),
        "vectorbt_version": getattr(vbt, "__version__", "unknown"),
        # ↓ 强制标注，见文件头说明
        "match_quality": MATCH_QUALITY_COARSE,
        "note": COARSE_NOTE,
    }
    return result, frame


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: entry.py <job.json>", file=sys.stderr)
        return 2

    job_path = Path(argv[1])
    job = json.loads(job_path.read_text(encoding="utf-8"))
    params = job.get("params", {})
    op = params.get("op")

    if op == "raise":
        # 故意失败：用于验证「目标环境失败可传播、不被静默吞掉」
        raise RuntimeError(params.get("message", "intentional failure"))

    frame = None
    if op == "add":
        result = _op_add(job, params)
    elif op == "scan":
        result, frame = _op_scan(job, params)
    else:
        result = {"job_id": job.get("job_id"), "env": job.get("env"), "op": op,
                  "note": "stub: 未实现该算子"}

    out = Path(job["outputs"]["result_json"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if frame is not None:
        # 同样用 DuckDB 写 Parquet（本环境没有 pyarrow/fastparquet），
        # 与上面的读取路径保持同一种机制，避免"能读不能写"的半截支持。
        import duckdb

        target = job["outputs"]["result_parquet"].replace("\\", "/")
        duckdb.from_df(frame).write_parquet(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
