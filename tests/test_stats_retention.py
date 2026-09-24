"""计数保留期（stats_keep_days）的功能测试。

为什么要有它：前端审计只能"搜字符串"证明代码写了这个分支，
**证明不了它真的会清零**。实测时正是靠这个测试抓到过一个真 bug：
清零之后紧接着又被文件里的旧值覆盖回去（看起来代码全对，功能却不动）。
所以这里用真插件类 + 伪造的 kpi_stats.json 跑真实路径。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

FRAMEWORK = _env.framework()
if FRAMEWORK is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
os.environ["KIRA_FW"] = FRAMEWORK
sys.path.insert(0, FRAMEWORK)
os.makedirs("/tmp/keep_test/data", exist_ok=True)
os.chdir("/tmp/keep_test")

M = _env.load("main")


class FakeCtx:
    def __init__(self, d):
        self._d = Path(d)

    def get_plugin_data_dir(self):
        return self._d


class QuietLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def exception(self, *a, **k): pass


M.logger = QuietLogger()

PASS = []


def check(name, ok, detail=""):
    PASS.append(ok)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


def make(keep_days):
    p = M.AcceleratorPlugin.__new__(M.AcceleratorPlugin)
    p.patches = M.PatchRegistry()
    p.stats_keep_days = keep_days
    p._stats = {
        "turns": 0, "steps": 0, "tool_signals": 0, "strip_calls": 0,
        "parallel_batches": 0, "content_normalized": 0, "streamed_calls": 0,
        "early_sent": 0, "first_seg_s": None, "thinking_on": 0,
        "thinking_off": 0, "thinking_skipped_budget": 0,
    }
    return p


def seed(td, since_days, **nums):
    d = {k: 0 for k in ("turns", "steps", "tool_signals", "strip_calls",
                        "parallel_batches", "content_normalized", "streamed_calls",
                        "early_sent", "thinking_on", "thinking_off",
                        "thinking_skipped_budget")}
    d["first_seg_s"] = None
    d.update(nums)
    if since_days is not None:
        d["_since"] = time.time() - since_days * 86400.0
    Path(td, "kpi_stats.json").write_text(json.dumps(d), encoding="utf-8")
    return d


def scenario(label, keep, since_days, expect_cleared):
    with tempfile.TemporaryDirectory() as td:
        seed(td, since_days, turns=12345, steps=9999, early_sent=777)
        p = make(keep)
        p.ctx = FakeCtx(td)
        p._load_stats()
        got = {k: p._stats[k] for k in ("turns", "steps", "early_sent")}
        cleared = all(v == 0 for v in got.values())
        check(label, cleared == expect_cleared, f"{got}")
        if expect_cleared:
            after = json.loads(Path(td, "kpi_stats.json").read_text(encoding="utf-8"))
            check("   起算点已重置为现在（滚动窗口）",
                  isinstance(after.get("_since"), (int, float))
                  and time.time() - after["_since"] < 60)


print("\n═══ 计数保留天数（stats_keep_days）═══")
scenario("默认 30 天，已过 45 天 ⇒ 清零", 30, 45, True)
scenario("默认 30 天，才过 10 天 ⇒ 保留", 30, 10, False)
scenario("设为 0（永久）⇒ 过 3650 天也不清", 0, 3650, False)
scenario("边界：刚过 30 天 ⇒ 清零", 30, 30.01, True)
scenario("首次启动（无 _since）⇒ 不清零", 30, None, False)
scenario("自定义 7 天，过了 8 天 ⇒ 清零", 7, 8, True)

print("\n═══ schema 默认值 ═══")
import json as _j
_s = _j.loads((Path(__file__).resolve().parent.parent / "schema.json").read_text(encoding="utf-8"))
_f = _s["section_observe"]["fields"].get("stats_keep_days", {})
check("stats_keep_days 默认 30", _f.get("default") == 30, str(_f.get("default")))
check("有中文说明", "zh" in (_f.get("locales") or {}))

print()
if all(PASS):
    print(f"🎉 全部通过 —— 计数保留期行为符合预期（{len(PASS)} 项）")
    sys.exit(0)
print(f"❌ {PASS.count(False)} 项未通过")
sys.exit(1)
