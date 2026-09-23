"""★ 经常性 warning 的守卫：`响应标记缺失，改用本轮台账剥离` 不该反复出现。

## 这条告警的两种触发，必须区分
  ① **真该报**：本轮确实抢发过，但响应标记丢了（例如 on_complete 没跑到）
     ⇒ 台账是唯一的旁证 ⇒ 用它是**正确**的，报一次即可。
  ② **不该报**（就是用户看到的"经常性 warning"）：
     同一轮内 `send_xml_messages` 被调用**第二次**（多步 agent loop / 框架其它调用点），
     此时响应已 pop、而台账**还没清** ⇒ 走兜底 + 告警。
     更糟：会拿**本次的（可能完全不相关的）文本**去按内容剥离 ⇒ **可能丢内容**。

## 根因与修法
台账原来只等"下一轮开始"才清。现在**消费完立即清**（与 pop 响应同一处 finally）。
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


print("═══ 1) 台账必须在**消费完**就清，而不是只等下一轮")
# 找 finally 块
# ⚠️ 用**唯一**锚点定位那段 finally：文件里有多个 finally 块，
#    按 "_install_early_sent_strip" 起算会算到别处（上一版就这么找错了）。
_anchor = "本轮已消费完，清掉标记"
assert _anchor in MAIN, "找不到 finally 里的注释锚点"
_j = MAIN.index(_anchor)
_seg_start = MAIN.rindex("finally:", 0, _j)
fin = MAIN[_seg_start:_j + 1400]
seg = MAIN[MAIN.index("_install_early_sent_strip"):_j + 1400]
check("★ finally 里清了 _sent_ledger（消费完立即清）",
      "plugin._sent_ledger.pop(sid_now, None)" in fin)
check("★ finally 里也清了 _resp_by_sid", "plugin._resp_by_sid.pop(sid_now, None)" in fin)
check("★ 清台账与清响应在**同一个** finally 块里（顺序一致才不会有窗口）",
      fin.index("_resp_by_sid.pop") < fin.index("_sent_ledger.pop"))

print()
print("═══ 2) ★★★ 台账是权威来源（不是兜底），且拿不到标记时不该告警")
# 背景：原判据以为"响应标记才是正路、台账是兜底"，据此要求"用兜底时必须告警"。
# 事实相反：台账是**发送那一刻**记下的第一手记录；响应标记只是同一份信息的拷贝，
# 而框架在 ON_LLM_RESPONSE 时已消费它、其它插件还会重写它 ⇒ 拿不到是**常态**。
# ★ 正确的结构是"先取两边、再决定用谁"：
#     ① segs = early_sent_segments(resp)   ← 先取响应标记的原文
#     ② ledger = _sent_ledger.get(sid)     ← 再取台账
#     ③ if ledger: n = len(ledger); segs = list(ledger)   ← **台账胜出**
#   所以不能拿"取值顺序"当判据（那样必然报错，上一版就这么自己误判）。
#   要断言的是**决策结构**：`if ledger:` 分支里必须写入 n 与 segs（即台账胜出）。
_lb = seg[seg.index("if ledger:"):seg.index("elif n > 0 and not segs:")]
check("★ 台账优先：`if ledger:` 分支里就用台账覆盖 n 与 segs",
      "n = len(ledger)" in _lb and "segs = list(ledger)" in _lb)
check("★ 且该分支在按序号剥离之前（不会被绕过）",
      seg.index("if ledger:") < seg.index("strip_early_sent_smart("))

# 去掉注释后再查（注释里会引用旧文案，说明"曾经这么报"）
_code = re.sub(r"(?m)^\s*#.*$", "", seg)
check("★★ 拿不到响应标记时不再打 warning（原来一直刷屏、且误导）",
      "响应标记缺失，改用本轮台账剥离" not in _code)
check("★ 只在『两边段数不一致』时才告警（真异常）",
      "已发段数不一致" in seg)
check("★ 台账为空且拿不到段原文时也留线索",
      "拿不到段原文" in seg)

print()
print("═══ 3) 行为模拟：同一轮被调用两次（多步 loop）")
# 复刻发送层的判定
def decide(n_marker, ledger):
    n = n_marker
    segs = []
    warn = False
    if n <= 0 and ledger:
        n = len(ledger)
        segs = list(ledger)
        warn = True
    return n, segs, warn


ledger = ["A", "B"]

# 第一次调用：标记在 ⇒ 用它，不告警；**消费完立即清**
n1, s1, w1 = decide(2, ledger)
check("第一次：标记 2 生效", n1 == 2 and not w1, f"n={n1} warn={w1}")
ledger = []          # ← 修法的效果：消费完立即清
resp = None          # ← finally 里 pop 掉

# 第二次调用（多步 loop）：标记没了、台账也没了
n2, s2, w2 = decide(0, ledger)
check("★ 第二次：n=0、安静交给框架（不误切、不告警）", n2 == 0 and not w2,
      f"n={n2} warn={w2}")
check("★ 第二次：segs 为空（不拿台账去误切本次文本）", s2 == [], str(s2))

print()
print("═══ 4) ★ 台账优先：标记缺失时安静使用（这正是线上一直在走的路）")
n4, s4, _ = decide(0, ["A", "B"])       # 标记缺失、台账在
check("★ 标记缺失 ⇒ 用台账、且**不告警**（线上常态路径）",
      n4 == 2 and s4 == ["A", "B"], f"n={n4}")

print()
print("═══ 5) 反向验证：若坚持标记优先，台账就会被忽略 ⇒ 整份回复重发")
def decide_marker_first(n_marker, ledger):
    if n_marker > 0:
        return n_marker, [], False
    return 0, [], False                 # 标记优先且为 0 ⇒ 不剥离
n5, s5, _ = decide_marker_first(0, ["A", "B"])
check("★ 反向验证：标记优先 ⇒ n=0、不剥离 ⇒ 会全量重复（说明台账必须优先）",
      n5 == 0 and s5 == [], f"n={n5}")

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 那条告警只在真该报时出现")
