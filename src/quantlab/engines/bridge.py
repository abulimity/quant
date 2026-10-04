"""跨环境桥（core 侧）。

强制规则（见 CLAUDE.md / 手册 §3.1）：环境之间**不得互相 import**，一切交换通过文件 + 子进程：

    调用方(core) → 写 job.json + 输入 Parquet → subprocess 调目标环境的 entry.py
    目标环境     → 读 job.json → 执行 → 写 result.json（+ 可选 result.parquet） → 退出
    调用方(core) → 读 result.* → 继续

失败必须**可传播**：目标环境非零退出时抛 `BridgeError`，携带 returncode 与 stderr，绝不静默吞掉。

用法（自检，同时验证 V2 与 V3）：
    uv run python src/quantlab/engines/bridge.py
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from quantlab.paths import ENVS_DIR, PROJECT_ROOT, RUNS_DIR


class BridgeError(RuntimeError):
    """目标环境失败时抛出，携带返回码与输出，**不静默吞掉**。"""

    def __init__(self, env: str, returncode: int, stderr: str, stdout: str = "") -> None:
        self.env = env
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        super().__init__(
            f"[bridge:{env}] 退出码={returncode}\n--- stderr ---\n{stderr}\n--- stdout ---\n{stdout}"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def env_lock_path(env: str) -> Path:
    """core 用根 `uv.lock`，其余环境用 `envs/<env>/uv.lock`。"""
    if env == "core":
        return PROJECT_ROOT / "uv.lock"
    return ENVS_DIR / env / "uv.lock"


def env_lock_hash(env: str) -> str:
    path = env_lock_path(env)
    if not path.is_file():
        raise BridgeError(env, -1, f"uv.lock 不存在: {path}")
    return sha256_file(path)


def _subprocess_env() -> dict[str, str]:
    """构造干净的子进程环境。

    必须清除外部工具注入的 `UV_PROJECT_ENVIRONMENT` / `VIRTUAL_ENV`（否则会把目标
    环境指错位置、破坏隔离），以及 `UV_RUN_RECURSION_DEPTH`（避免 uv run 嵌套误判）。
    """
    env = dict(os.environ)
    for key in ("UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "UV_RUN_RECURSION_DEPTH"):
        env.pop(key, None)
    return env


def run_in_env(
    env: str,
    entry: str,
    job: dict,
    inputs: dict[str, Path] | None = None,
    workdir: Path | None = None,
) -> dict:
    """写 job.json 与输入 Parquet → `uv run --project envs/<env>` 跑 entry → 读回 result.json。

    参数：
        env     : 目标环境名（如 "vbt" / "x2"；"core" 指根项目）
        entry   : 目标环境的入口脚本（相对项目根或绝对路径）
        job     : 含 job_id / engine / params 的字典
        inputs  : {名称: Parquet 路径}，必须存在
        workdir : 工作目录；默认 `runs/<job_id>/`

    返回：解析后的 result.json（dict）
    异常：BridgeError（入口缺失 / 输入缺失 / 目标环境非零退出 / 未产出 result.json）
    """
    inputs = inputs or {}
    job_id = job.get("job_id") or uuid.uuid4().hex
    if workdir is None:
        workdir = RUNS_DIR / job_id
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    entry_path = Path(entry)
    if not entry_path.is_absolute():
        entry_path = PROJECT_ROOT / entry_path
    if not entry_path.is_file():
        raise BridgeError(env, -1, f"entry 不存在: {entry_path}")

    for name, path in inputs.items():
        if not Path(path).is_file():
            raise BridgeError(env, -1, f"输入不存在: {name} -> {path}")

    outputs = {
        "result_json": str(workdir / "result.json"),
        "result_parquet": str(workdir / "result.parquet"),
    }
    lock_hash = env_lock_hash(env)
    payload = {
        "job_id": job_id,
        "env": env,
        "engine": job.get("engine", env),
        "entry": str(entry_path),
        "params": job.get("params", {}),
        "inputs": {k: str(Path(v).resolve()) for k, v in inputs.items()},
        "outputs": outputs,
        "env_lock_sha256": lock_hash,
    }
    job_path = workdir / "job.json"
    job_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    project = PROJECT_ROOT if env == "core" else ENVS_DIR / env
    cmd = ["uv", "run", "--project", str(project), "python", str(entry_path), str(job_path)]
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise BridgeError(env, proc.returncode, proc.stderr or "", proc.stdout or "")

    result_path = Path(outputs["result_json"])
    if not result_path.is_file():
        raise BridgeError(
            env,
            proc.returncode,
            f"目标环境未产出 result.json: {result_path}\n{proc.stderr or ''}",
            proc.stdout or "",
        )
    return json.loads(result_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 自检：一条命令同时验证 V2（端到端成功）与 V3（失败可传播、锁哈希一致）
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    failures: list[str] = []

    # V2：core 调用 envs/vbt/entry.py 算加法并回传
    jid = "selftest-vbt-add"
    workdir = PROJECT_ROOT / "runs" / jid
    got = run_in_env(
        "vbt",
        "envs/vbt/entry.py",
        {"job_id": jid, "engine": "vbt", "params": {"op": "add", "a": 2, "b": 40}},
        workdir=workdir,
    )
    print("V2 vbt add ->", json.dumps(got, ensure_ascii=False))
    if got.get("sum") != 42:
        failures.append(f"V2: 期望 sum=42，实得 {got.get('sum')!r}")

    # V2b：x2 环境同样可被调用（证明桥与环境无关）
    got_x2 = run_in_env(
        "x2",
        "envs/x2/entry.py",
        {"job_id": "selftest-x2-add", "engine": "x2", "params": {"op": "add", "a": 20, "b": 22}},
        workdir=PROJECT_ROOT / "runs" / "selftest-x2-add",
    )
    print("V2 x2  add ->", json.dumps(got_x2, ensure_ascii=False))
    if got_x2.get("sum") != 42:
        failures.append(f"V2b: 期望 sum=42，实得 {got_x2.get('sum')!r}")

    # V3：目标环境故意抛异常 → core 必须拿到非零退出码与错误信息
    try:
        run_in_env(
            "vbt",
            "envs/vbt/entry.py",
            {"job_id": "selftest-vbt-raise", "engine": "vbt",
             "params": {"op": "raise", "message": "boom-from-vbt"}},
            workdir=PROJECT_ROOT / "runs" / "selftest-vbt-raise",
        )
        failures.append("V3: 目标环境抛异常却未传播（被静默吞掉）")
    except BridgeError as exc:
        propagated = exc.returncode != 0 and "boom-from-vbt" in exc.stderr
        print(f"V3 vbt raise -> 已传播：returncode={exc.returncode}, "
              f"'boom-from-vbt' in stderr={('boom-from-vbt' in exc.stderr)}")
        if not propagated:
            failures.append(f"V3: 失败信息不完整 returncode={exc.returncode} stderr={exc.stderr!r}")

    # V3b：job.json 中记录的环境锁哈希 == 实际 uv.lock 哈希
    payload = json.loads((workdir / "job.json").read_text(encoding="utf-8"))
    actual = env_lock_hash("vbt")
    print(f"V3 lock hash -> job.json={payload['env_lock_sha256'][:12]}… actual={actual[:12]}…")
    if payload["env_lock_sha256"] != actual:
        failures.append("V3b: job.json 锁哈希与实际 uv.lock 不一致")

    if failures:
        print("\nSELFTEST FAILED:", file=sys.stderr)
        for item in failures:
            print("  -", item, file=sys.stderr)
        return 1
    print("\nSELFTEST OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
