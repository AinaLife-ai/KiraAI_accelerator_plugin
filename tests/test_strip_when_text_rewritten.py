"""★★ 回归：别的插件改写了 text_response 之后，剥离仍然正确（不重复、不丢内容）。

用户点名要查的插件：`KiraAI_xml_tag_fixer_plugin`（znq19）。
它的 `on_llm_response`（Priority.HIGH）会改写 `resp.text_response`：

    fixed = self.fix_xml(original)
    if fixed != original:
        resp.text_response = fixed

而 `fix_xml` 会改变 `<msg>` 的**数量**：
  · 补全缺失的 `<msg>`（模型忘了写标签时，0 个 → N 个）
  · 空行分段（`split_blank_line_messages`，默认关）：**1 条 → N 条**
  · 跨消息标记对合并（`_merge_marker_spanning_blocks`）：**多条 → 1 条**
  · 裸特殊字符转义（`_escape_code_fences`）：内容里的 `&`/`<` 变成实体

我们原来的剥离是 `strip_early_sent(文本, n)`，`n` 是按**原始**文本数的段数。
文本被改写后 `n` 就对不上：**少切 ⇒ 重复发送；多切 ⇒ 丢内容**。

本测试把每种改写都构造出来，断言剥离结果正确。
"""
from __future__ import annotations

import importlib.util as ilu
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


spec = ilu.spec_from_file_location("early_sent", str(HERE / "early_sent.py"))
es = ilu.module_from_spec(spec)
spec.loader.exec_module(es)

# 真实形状：模型写了 4 段，前 2 段已抢发
E1 = "<msg><text>第一段</text></msg>"
E2 = "<msg><text>第二段</text></msg>"
T3 = "<msg><text>第三段</text></msg>"
T4 = "<msg><text>第四段</text></msg>"
SENT = [E1, E2]
ORIG = E1 + E2 + T3 + T4

print("═══ 0) 基线：文本没被改写 ⇒ 与旧行为一致")
got = es.strip_early_sent_smart(ORIG, 2, SENT)
check("剥掉前 2 段，剩第 3、4 段", got == T3 + T4, got)

print()
print("═══ 1) ★ xml_tag_fixer 的「空行分段」：1 条被拆成多条")
# 场景：抢发出去的第 2 段是一条含空行的长消息，被插件拆成 3 条
E2_MULTI = "<msg><text>甲\n\n乙\n\n丙</text></msg>"
SENT2 = [E1, E2_MULTI]
ORIG2 = E1 + E2_MULTI + T3
# 插件改写后（第 2 段变成 3 条）
FIXED2 = E1 + "<msg><text>甲</text></msg><msg><text>乙</text></msg><msg><text>丙</text></msg>" + T3
got2 = es.strip_early_sent_smart(FIXED2, 2, SENT2)
check("★ 1 条被拆成 3 条后，3 条都被剥掉（不重复）",
      "甲" not in got2 and "乙" not in got2 and "丙" not in got2, got2)
check("★ 未发的第 3 段仍保留（不丢内容）", "第三段" in got2, got2)

print()
print("═══ 2) 对照：仍用旧的「按序号」剥离会怎样")
old2 = es.strip_early_sent(FIXED2, 2)      # 只切 2 块
check("★ 旧写法只切 2 块 ⇒ 「丙」被漏下 ⇒ 会被重复发送",
      "丙" in old2, f"剩余={old2[:70]}")

print()
print("═══ 3) ★ xml_tag_fixer 的「跨消息合并」：多条被并成 1 条")
SENT3 = [E1, E2]
ORIG3 = E1 + E2 + T3
# 插件把 前两段合并成一条
FIXED3 = "<msg><text>第一段第二段</text></msg>" + T3
got3 = es.strip_early_sent_smart(FIXED3, 2, SENT3)
check("★ 两条被并成一条后，那一条被剥掉（不重复）",
      "第一段" not in got3 and "第二段" not in got3, got3)
check("★ 未发的第 3 段仍保留", "第三段" in got3, got3)

print()
print("═══ 4) ★ 转义实体：内容里的 & 变成 &amp;")
SENT4 = ['<msg><text>A & B</text></msg>']
ORIG4 = SENT4[0] + T3
FIXED4 = '<msg><text>A &amp; B</text></msg>' + T3
got4 = es.strip_early_sent_smart(FIXED4, 1, SENT4)
check("★ 转义后仍能匹配上（不重复）", "A " not in got4.replace("第三段", ""), got4)
check("★ 未发段保留", "第三段" in got4, got4)

print()
print("═══ 5) ★ 补全缺失的 <msg>：模型没写标签（0 个 → N 个）")
# 抢发出去的是"裸文本"段（模型没写 <msg>），插件补上了 <msg><text>
SENT5 = ["第一段"]
ORIG5 = "<msg><text>第一段</text></msg>" + T3
got5 = es.strip_early_sent_smart(ORIG5, 1, SENT5)
check("★ 补全标签后仍能剥掉（不重复）", "第一段" not in got5, got5)
check("★ 未发段保留", "第三段" in got5, got5)

print()
print("═══ 6) ★★★ 匹配不上时：**保守不剥**（审计 P3 更正契约）")
# 旧契约"匹配不上就退回按序号切前 N 块"**确认会同时丢内容 + 重复**：
#   下游插件在已发段**之前**新增块时（xml_tag_fixer 把裸文本包成 <msg> 是常规行为），
#   按序号切会删掉**从未发出的新增块**（丢内容），而已发的第 2 段却留下（重复）。
#   新契约：宁可"少剥一次（最多多一条重复）"，**绝不误删未发内容**。
#   配套：锚点搜索已能处理"前面插入新块"（见第 6b 节），所以这条兜底很少走到。
SENT6 = ["完全不同的内容"]
got6 = es.strip_early_sent_smart(ORIG, 2, SENT6)
check("★ 完全匹配不上 ⇒ 原样交回（不剥、不删任何内容）", got6 == ORIG, got6)

print()
print("═══ 6b) ★★★ 前置插入新块：必须精确剥掉已发段、**保住**新增块（P3 的核心）")
E1 = "<msg><text>第一段</text></msg>"
E2 = "<msg><text>第二段</text></msg>"
T3B = "<msg><text>第三段</text></msg>"
X = "<msg><text>开场白新增</text></msg>"          # 插件新包出来的块（从未抢发）
REWRITTEN = X + E1 + E2 + T3B
got6b = es.strip_early_sent_smart(REWRITTEN, 2, [E1, E2])
check("★★★ 新增块 X 必须**保留**（旧版按序号切会把它删掉 = 丢内容）",
      "开场白新增" in (got6b or ""), got6b)
check("★★★ 已发的 E1/E2 必须剥掉（旧版会留下 E2 = 重复发送）",
      "第一段" not in (got6b or "") and "第二段" not in (got6b or ""), got6b)
check("★ 未发的 T3 保留", "第三段" in (got6b or ""), got6b)
# 反向验证：旧写法（按序号）在同样输入下必须出错 —— 证明这条判据不恒真
old_way = es.strip_early_sent(REWRITTEN, 2)
check("★ 反向：旧写法确实丢 X 且留 E2（证明修复有效）",
      "开场白新增" not in old_way and "第二段" in old_way, old_way)

print()
print("═══ 6c) ★★ 前导无关块「恰好撞上前缀」时，仍要选**匹配最完整**的起点")
# 罕见但可能：改写后的第一个块（如"第一"两字）恰好是已发内容的前缀，
# 若贪心"从头扫"，它会被吃掉一部分 ⇒ 剥掉错误的块。择优起点可避免。
E1B = "<msg><text>第一段</text></msg>"
E2B = "<msg><text>第二段</text></msg>"
T3B2 = "<msg><text>第三段</text></msg>"
SENT6c = ["<msg><text>第一段第二段</text></msg>"]     # 一段被合并：内容=第一段+第二段
LURE = "<msg><text>第一</text></msg>"                # 诱饵：也是"第一段…"的前缀
REW6c = LURE + E1B + E2B + T3B2
got6c = es.strip_early_sent_smart(REW6c, 1, SENT6c)
check("★★ 诱饵块必须保留（不能因为撞上前缀就把从未发过的它吃掉）",
      LURE in (got6c or ""), got6c)
check("★★ 真正的已发内容被整段剥掉（E1+E2 都不得留在文本里）",
      "第一段" not in (got6c or "") and "第二段" not in (got6c or ""), got6c)
check("★ 未发的 T3 保留", "第三段" in (got6c or ""), got6c)

print()
print("═══ 6d) ★★★ 媒体段（无可见文字）必须能剥掉 —— 否则贴纸/图片会重复发送")
# 自查发现的回归（本 PR 的第二处）：锚点搜索若只按"可见文字"匹配，
# 媒体段（贴纸/图片/语音）没有文字 ⇒ 匹配不到 ⇒ 保守不剥 ⇒ **框架再发一遍**。
# 这里把常见形态全部锁住（含"文本里有个*别的*媒体在前"这种歧义形态）。
MEDIA_CASES = [
    ("单个贴纸", ["<msg><sticker id=\"7\"/></msg>"],
     "<msg><sticker id=\"7\"/></msg><msg>尾</msg>",
     "<msg>尾</msg>"),
    ("图片在前", ["<msg><image file=\"a.png\"/></msg>", "<msg>甲</msg>"],
     "<msg><image file=\"a.png\"/></msg><msg>甲</msg><msg>尾</msg>",
     "<msg>尾</msg>"),
    ("语音条", ["<msg><record file=\"a.silk\"/></msg>"],
     "<msg><record file=\"a.silk\"/></msg><msg>尾</msg>",
     "<msg>尾</msg>"),
    ("媒体+文字混合", ["<msg>丙<image file=\"c.png\"/></msg>"],
     "<msg>丙<image file=\"c.png\"/></msg><msg>尾</msg>",
     "<msg>尾</msg>"),
]
for name, segs, text, expect in MEDIA_CASES:
    got = es.strip_early_sent_smart(text, len(segs), segs)
    check(f"★★ 媒体段可剥：{name}", got == expect, f"得到 {got!r} 期望 {expect!r}")

# ★ 歧义形态：文本里**另一个**媒体（从未发过）在前 —— 必须保留它、剥掉我们的
segs_amb = ["<msg><sticker id=\"7\"/></msg>", "<msg>甲</msg>"]
text_amb = "<msg><sticker id=\"9\"/></msg><msg><sticker id=\"7\"/></msg><msg>甲</msg><msg>尾</msg>"
got_amb = es.strip_early_sent_smart(text_amb, len(segs_amb), segs_amb)
check("★★★ 歧义：别的媒体(9)保留、我们的(7)剥掉（不误吃、不重复）",
      "9" in (got_amb or "") and "7" not in (got_amb or "") and "甲" not in (got_amb or ""),
      f"{got_amb!r}")

print()
print("═══ 7) 全部发完 ⇒ 返回 <msg/>（框架不发任何东西）")
got7 = es.strip_early_sent_smart(ORIG, 4, [E1, E2, T3, T4])
check("全发完 ⇒ <msg/>", got7 == "<msg/>", got7)

print()
print("═══ 8) 已发段原文可从响应读回（发送层要用）")


class R:
    pass


r = R()
es.mark_early_sent(r, SENT, ORIG)
check("★ 响应的私有属性里存了已发段原文",
      es.early_sent_segments(r) == SENT, str(es.early_sent_segments(r)))
check("段数仍然可读（向后兼容）", es.early_sent_count(r) == 2)

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 文本被改写后剥离依然正确")
