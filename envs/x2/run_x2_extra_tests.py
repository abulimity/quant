"""测 x2strategy 的其余两项能力（不 import 平台代码）：

    A. spec2code.validator.validate_code  —— backtrader 代码校验
       （AST 语法 + 结构 + 对照已安装 backtrader 的指标注册表）
    B. paper2spec.operator_pitfall        —— 算子误用检测（语义检索）

B 需要真实规格：默认读 runs/x2/<slug>/spec.json（由 run_paper2spec.py 产出）。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("QUANT_ROOT", str(pathlib.Path(__file__).resolve().parents[2])))
SLUG = "多资产-ETF-轮动策略-固收-视角下动态组合管理的构建与实践"
SPEC = ROOT / "runs" / "x2" / SLUG / "spec.json"

# embedding 模型已本地缓存 → 强制离线，避免联网卡住
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


VALID_STRATEGY = '''\
import backtrader as bt


class SmaCross(bt.Strategy):
    params = dict(fast=20, slow=60)

    def __init__(self):
        self.fast = bt.indicators.SMA(self.data.close, period=self.p.fast)
        self.slow = bt.indicators.SMA(self.data.close, period=self.p.slow)
        self.cross = bt.indicators.CrossOver(self.fast, self.slow)

    def next(self):
        if not self.position and self.cross > 0:
            self.buy()
        elif self.position and self.cross < 0:
            self.close()


if __name__ == "__main__":
    cerebro = bt.Cerebro()
    cerebro.addstrategy(SmaCross)
    cerebro.run()
'''

BAD_INDICATOR = '''\
import backtrader as bt


class Bad(bt.Strategy):
    def __init__(self):
        self.x = bt.indicators.DefinitelyNotARealIndicator(self.data.close)


if __name__ == "__main__":
    bt.Cerebro().run()
'''

SYNTAX_ERROR = "def broken(:\n    pass\n"

NO_STRUCTURE = "x = 1 + 1\n"


def section_a() -> None:
    from spec2code import validator
    print("=" * 72)
    print(f"A) spec2code.validate_code —— 已注册 backtrader 指标: {len(validator._VALID_INDICATORS)}")
    print("=" * 72)
    cases = [
        ("合法策略（SMA+CrossOver）", VALID_STRATEGY),
        ("不存在的指标 bt.indicators.DefinitelyNotARealIndicator", BAD_INDICATOR),
        ("语法错误", SYNTAX_ERROR),
        ("无 bt 导入/无 Strategy/无 main 保护", NO_STRUCTURE),
    ]
    for label, code in cases:
        r = validator.validate_code(code)
        print(f"\n[{label}]")
        print(f"  valid={r.valid}  errors={len(r.errors)}  warnings={len(r.warnings)}")
        for e in r.errors:
            print(f"    ERR : {e[:160]}")
        for w in r.warnings:
            print(f"    WARN: {w[:160]}")


def section_b() -> None:
    from paper2spec import operator_pitfall as op
    print("\n" + "=" * 72)
    print("B) paper2spec.operator_pitfall —— 算子误用检测")
    print("=" * 72)
    entries = op.load_operator_pitfall_entries()
    print(f"语料条目: {len(entries)} -> {[e['operator_id'] for e in entries]}")
    if not SPEC.is_file():
        print(f"[SKIP] 缺规格: {SPEC}（先跑 run_paper2spec.py）")
        return
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    strategies = spec.get("strategies") or []
    if not strategies:
        print("[SKIP] spec.json 里没有 strategies")
        return
    s = strategies[0]
    queries = op.operator_pitfall_queries_from_spec(s)
    print(f"规格: {s.get('strategy_name')!r}")
    print(f"切出查询: {len(queries)} 条（indicators/logic_pipeline/execution 各字段）")

    import time
    t0 = time.time()
    matches = op.retrieve_operator_pitfalls(s, threshold=0.65, top_k=3)
    print(f"检索耗时 {time.time()-t0:.1f}s  命中(score>=0.65): {len(matches)}")
    for m in matches:
        print(f"  - {m['operator_id']}  score={m['score']:.3f}  from={m['matched_from']}")

    rendered = op.render_operator_pitfall_matches(matches)
    out_dir = SPEC.parent
    (out_dir / "operator_pitfall.json").write_text(
        json.dumps({"n_entries": len(entries), "n_queries": len(queries),
                    "threshold": 0.65, "top_k": 3, "n_matches": len(matches),
                    "matches": matches}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "operator_pitfall.md").write_text(rendered or "(无命中)", encoding="utf-8")
    print(f"已写: {out_dir / 'operator_pitfall.json'}")


def main() -> int:
    section_a()
    section_b()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
