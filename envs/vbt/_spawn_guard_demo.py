"""spawn 保护演示（§P4.4 V3）—— **只用于测试，不是生产入口**。

Windows 上 multiprocessing 用 `spawn`：子进程会**重新导入**本模块。
若在模块**顶层**直接启动进程池（而不是放在 `if __name__ == "__main__":` 下），
导入本身就会启动新的子进程 —— 于是无限自我复刻。

两种模式（由 `tests/test_p4_vbt_bridge.py` 调用）：

    --guarded    —— 正确写法：顶层有 `__main__` 保护 → 正常输出 `RESULT 42`
    --unguarded  —— 错误写法：顶层直接启动 → **确定性、快速地失败**

### 为什么用「继承的深度计数」而不是指望 Python 自己拦

直觉上 `multiprocessing` 的 `_check_not_importing_main()` 会拦住这种写法；
但**实测不成立**：它会**挂住**（子进程反复导入、既不报错也不退出），
在测试里表现为超时而不是失败 —— 那样就无法作为「可复现失败」的证据。

故这里显式记一个继承自父进程的深度计数（spawn 会继承环境变量）：
子进程在顶层再次进入本段时，看到计数已存在，立即报错并以非零码退出。

### ⚠️ 这个演示自己踩过的两个坑（都留在这里当反面教材）

**坑 1：资源泄漏。** 第一版让失败路径上的 `Pool` 就那么挂着。父进程退出后
**子进程池成了孤儿**，继续占 CPU —— 实测一次全量回归里积了 **14 个残留 python 进程**，
把 `test_p4_vbt_bridge` 从 **43 秒**拖成 **10 分钟以上**。
用一个会泄漏资源的演示去「证明」工程纪律，本身就是坏的。

**坑 2：退出码不传播。** 子进程以 97 退出，**不会**让父进程也非零退出，
于是"可复现失败"变成了"悄悄成功"。改为：子进程写标记文件 → 父进程读到后自己以 97 退出。

### 关于耗时（如实说明，别照抄旧说法）

本演示**不是**"毫秒级失败"：最坏情况下要等到 `_TRIP_TIMEOUT_S`（5 秒）硬超时才返回。
之所以要有这个硬超时，是因为 `pool.map` 会等一个**已退出**的 worker 而挂死，
而放任 `Pool` 自己管 worker 又会**无限重启**它（新 worker 再撞守卫再退出）。
故改为「轮询标记文件 + `terminate()` 收尾」，**永远不无限等**。
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import tempfile
import time
from pathlib import Path

_MODE_UNGUARDED = "--unguarded" in sys.argv and "--guarded" not in sys.argv
_DEPTH_ENV = "SPAWN_GUARD_DEMO_DEPTH"
_TRIP_MARKER = "quantlab_spawn_guard_demo.tripped"


def _work(payload: int) -> int:
    return payload * 2


def _run() -> int:
    """真实任务：用 spawn 起一个子进程算个数。**正常路径按 `with` 正常收尾**。"""
    context = multiprocessing.get_context("spawn")
    with context.Pool(1) as pool:
        values = pool.map(_work, [21])
    print("RESULT", values[0])
    return 0


_TRIP_TIMEOUT_S = 5.0


def _run_pool_and_collect() -> bool:
    """起池 → 等子进程撞上守卫 → **无论成败都收干净**。返回「守卫是否被触发」。

    两个踩过的坑，都在这里避开：

    1. **不能用 `pool.map`**：它会等一个**已经退出**的 worker 的结果，直接挂死。
    2. **不能放任 Pool 自己管理 worker**：worker 异常退出后池会**重启**它，
       新 worker 再撞守卫再退出 → 无限循环。
       故一律 `terminate()` 收尾，并给等待设**硬超时**，绝不无限等。
    """
    marker = Path(tempfile.gettempdir()) / _TRIP_MARKER
    marker.unlink(missing_ok=True)
    pool = multiprocessing.get_context("spawn").Pool(1)
    try:
        # 子进程在 Pool 创建时即已启动并导入本模块 → 撞上守卫 → 写标记后退出。
        # 轮询等它，但**有硬上限**：绝不把"等不到"变成"永远等"。
        deadline = time.monotonic() + _TRIP_TIMEOUT_S
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        try:
            pool.terminate()          # 不用 close/join：不给池重启 worker 的机会
        finally:
            pool.join()
    tripped = marker.exists()
    marker.unlink(missing_ok=True)
    return tripped


# --------------------------------------------------------------------------- #
# ⚠️ **反面教材**：顶层直接执行（模拟「忘了写 __main__ 保护」）。
#    spawn 的子进程会继承环境变量并重新导入本模块，于是再次走到这里 ——
#    靠这个继承的深度计数把它当场拦下，并**收拾掉刚起的池**。
# --------------------------------------------------------------------------- #
if _MODE_UNGUARDED:
    if os.environ.get(_DEPTH_ENV):
        # 我们正处在「子进程重新导入 __main__」的那一次 —— 保护缺失的后果。
        # 留一个标记文件，让**父进程**知道守卫被触发了
        # （子进程自己的退出码传播不到父进程，这一点是踩过的坑）。
        try:
            (Path(tempfile.gettempdir()) / _TRIP_MARKER).write_text(
                "tripped", encoding="utf-8")
        except OSError:
            pass
        raise SystemExit(97)

    os.environ[_DEPTH_ENV] = "1"          # 子进程会继承它
    tripped = _run_pool_and_collect()
    if tripped:
        print(
            "SPAWN-GUARD-FAILURE: 子进程在导入 __main__ 时再次执行了顶层代码。\n"
            "原因：入口代码未放在 `if __name__ == \"__main__\":` 之下，"
            "spawn 子进程反复自我复刻。\n"
            "修复：把启动逻辑放进 `if __name__ == \"__main__\":` 保护内。",
            file=sys.stderr)
        raise SystemExit(97)
    print("UNGUARDED-SHOULD-NOT-PRINT")


if __name__ == "__main__":
    if _MODE_UNGUARDED:
        raise SystemExit(0)              # 顶层已处理（并已按上面路径失败）
    raise SystemExit(_run())
