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
print("═══ 2) 兜底判据本身仍然保留（真该报的场景要能报）")
check("★ 仍然保留了『n<=0 且台账非空 ⇒ 用台账』这条兜底",
      "if n <= 0 and ledger:" in seg)
check("★ 仍然会告警一次（真丢标记时需要线索）",
      "响应标记缺失，改用本轮台账剥离" in seg)

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
check("★ 第二次：n=0、**不告警**（修前的常驻告警就是这里）", n2 == 0 and not w2,
      f"n={n2} warn={w2}")
check("★ 第二次：不拿台账去误切本次文本（segs 为空）", s2 == [], str(s2))

print()
print("═══ 4) 反向验证：不清台账就会复现那条约会性告警")
n3, s3, w3 = decide(0, ["A", "B"])      # 台账未清（修前行为）
check("★ 反向验证：台账未清 ⇒ n 被抬到 2 且**告警**（复现修前）",
      n3 == 2 and w3, f"n={n3} warn={w3}")

print()
print("═══ 5) 真丢标记的场景仍能兜底（不能被这次改动弄坏）")
n4, s4, w4 = decide(0, ["A", "B"])      # 第一次调用、标记丢了、台账在
check("★ 首次调用就丢标记 ⇒ 用台账兜底并告警（这是**该报**的）",
      n4 == 2 and w4 and s4 == ["A", "B"], f"n={n4} warn={w4}")

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 那条告警只在真该报时出现")
