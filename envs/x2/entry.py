"""目标环境入口桩（x2）。

契约（手册 P1.5）：
    argv[1] = job.json 路径
    job.json = {job_id, env, engine, entry, params, inputs, outputs, env_lock_sha256}
    结果写入 job["outputs"]["result_json"]

本文件在隔离环境内**独立存在**，不 import core 的任何代码。
真正的 paper2spec / spec2code 调用在 P5 接入；此处仅实现最小任务以打通桥。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


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

    if op == "add":
        result = {
            "job_id": job.get("job_id"),
            "env": job.get("env"),
            "op": "add",
            "a": params.get("a"),
            "b": params.get("b"),
            "sum": params.get("a", 0) + params.get("b", 0),
        }
    else:
        result = {
            "job_id": job.get("job_id"),
            "env": job.get("env"),
            "op": op,
            "note": "stub: 未实现其它算子（P5 接入真实逻辑）",
        }

    out = Path(job["outputs"]["result_json"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
