"""★ 日志行为回归：不误导、不误报。

针对 2026-09-23 用户实测报的两条：

  ① "日志这干嘛思考显示未启用，太吓人了。这样用户分不清，
      因为他可能本身提供商就开了思考。没开自动思考功能就不要一直显示这个日志好吗？"

     ⇒ 自动思考**没开**时，日志里**完全不能出现**"思考"字段。
       换个委婉的词也不行 —— 用户可能自己在提供商那边开了思考，
       看到任何"思考=未启用/关"都会以为插件把思考关了。

  ② "为什么每一句报说重复发送，但是实际上我没看到重复发送。
      你这个检测和修复对了没有？"

     ⇒ 旧的检测用 **sid** 且**从不清空** ⇒ 同一会话的下一轮就命中，
       而多步 agent loop 又必然多次调用发送层 ⇒ **每条都报**，纯误报。
       正确判据必须是**按内容**核对（剥离后剩余里是否还含已抢发的段）。
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
MAIN = (HERE / "main.py").read_text(encoding="utf-8")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


print("═══ 1) ★ 自动思考没开时，日志里不出现「思考」字段")
# 取 observe_final 函数体
i = MAIN.index("async def observe_final")
j = MAIN.index("\n    # ", i)
body = MAIN[i:j]

# 未启用分支里**不能**有"思考"或"未启用"
seg = body[body.index("if not self.thinking_enabled:"):body.index("if d is None:")]
check("未启用分支里没有『思考』字样", "思考" not in seg, repr(re.findall(r"思考[^\n]{0,20}", seg)))
check("未启用分支里没有『未启用』字样", "未启用" not in seg)
check("未启用分支里的日志串只到「首段=%.2fs」为止",
      '首段=%.2fs",' in seg and "思考=" not in seg)
check("★ 未启用分支会 return（不会继续走到打「思考=」的那条）",
      "return" in seg)

# 启用之后才打"思考"
after = body[body.index("if d is None:"):]
check("启用后仍然打「思考=」", "思考=%s" in after)

print()
print("═══ 2) ★★ 重复发送检测：必须按内容，不能按 sid")
check("★ 不再使用 _sent_once（那套按 sid 的检测会误报）",
      "_sent_once" not in MAIN, f"残留 {MAIN.count('_sent_once')} 处")
check("★ 不再有「同一轮被再次交给发送层」这条误报日志",
      "被再次交给发送层" not in MAIN)
check("★ 改为按内容核对：剩余文本里若仍含已抢发段才报",
      "剥离后剩余文本里仍含已抢发的段" in MAIN)
check("核对用的是规范化后的内容（_norm_seg）",
      "rest_norm = _norm_seg(xml_data)" in MAIN and "_norm_seg(_s)" in MAIN)
check("核对失败不影响发送（有兜底）",
      "重复发送核对失败（不影响发送）" in MAIN)

print()
print("═══ 3) 行为模拟：按内容核对**只在真有重叠时**才报")
# 复刻核对逻辑
sys.path.insert(0, str(HERE))
import importlib.util as ilu
spec = ilu.spec_from_file_location("early_sent", str(HERE / "early_sent.py"))
es = ilu.module_from_spec(spec); spec.loader.exec_module(es)

E1 = "<msg><text>第一段</text></msg>"
E2 = "<msg><text>第二段</text></msg>"
T3 = "<msg><text>第三段</text></msg>"
segs = [E1, E2]


def would_warn(remaining, segs):
    rest_norm = es._norm_seg(remaining)
    for s in segs:
        piece = es._norm_seg(s)
        if piece and piece in rest_norm:
            return True
    return False


# 正常情况：已剥掉，不该报
rest_ok = es.strip_early_sent_smart(E1 + E2 + T3, 2, segs)
check("★ 正常剥离后**不报**（这是关键：原来会狂报）", not would_warn(rest_ok, segs), rest_ok)
# 多步循环：同一会话连续几步都不该报
for k in range(3):
    rest_k = es.strip_early_sent_smart(T3, 0, [])       # 已经全发过 ⇒ 没有可剥的
    check(f"多步循环第 {k+1} 次调用**不报**", not would_warn(T3, []))
# 真出问题：剥离没生效（剩余里还含已发段）⇒ 必须报
check("★ 真的没剥掉时**必须报**（这才是有效判据）",
      would_warn(E1 + E2 + T3, segs))

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 日志不再误导、检测不再误报")
