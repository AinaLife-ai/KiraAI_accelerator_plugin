"""★★★ 回归：解析前剥离必须"逐段兜底"，不能只剥连续同序的那一段。

## 线上现象（用户截图 + 日志）

引用回复里同一条正文出现两遍（`正文@昵称 正文`），且日志里
`[QQBOT-BRIDGE] 媒体上传完成 file=84f07fec` 同一文件**出现两次** —— 即
**同一条消息被投递了两遍**（一次带 @ 的 markdown 报文 + 一次纯文本报文）。

## 根因（本测试锁住的那一类）

`strip_by_content` 的锚点是"一段**连续且同序**的已发内容"。一旦下游把已发段
**重排 / 拆分 / 穿插**，锚点只能消费其中一部分 ⇒ 剩下的段留在文本里
⇒ 框架**再发一次**（= 用户看到的重复）。
而旧日志打的是**台账条数**（`len(segs)`），漏剥时仍显示"剥离 3 段"，把线索掩盖。

## 本测试

1. 正常连续三段 ⇒ 全剥掉（行为不变）
2. **重排**（第 2 段跑到最前）⇒ 修复前残留 [2,3]；修复后**全剥掉**
3. 段间穿插文字 ⇒ 全剥掉
4. **未投递的段（不在台账里）绝不能被删** —— 不丢内容
5. `count_segments_removed` 报的数是**实际**剥掉的段数（日志据此告警）
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("es_under_test", str(ROOT / "early_sent.py"))
es = importlib.util.module_from_spec(spec)
sys.modules["es_under_test"] = es
spec.loader.exec_module(es)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


S1 = ('<msg><reply>r1</reply><at>a1</at>'
      '<text>姐姐捏 袜子也归姐姐研究ww</text><sticker>11</sticker></msg>')
S2 = ('<msg><reply>r2</reply><at>a2</at>'
      '<text>C7姐三分钟是上限呀w</text><sticker>27</sticker></msg>')
S3 = '<msg><reply>r3</reply><text>那香香不肯了w</text><sticker>12</sticker></msg>'
SEGS = [S1, S2, S3]


def strip(text, segs=SEGS):
    return es.strip_early_sent_smart(text, len(segs), segs)


def leftovers(out, segs=SEGS):
    n = es._norm_seg(out)
    return [i + 1 for i, s in enumerate(segs) if es._norm_seg(s) and es._norm_seg(s) in n]


print("## 1) 正常：连续同序三段")
out = strip(S1 + "\n" + S2 + "\n" + S3)
check("三段全剥掉", leftovers(out) == [], f"残留={leftovers(out)}")

print("\n## 2) ★ 重排（第 2 段跑到最前）—— 修复前的漏剥来源")
out = strip(S2 + "\n" + S1 + "\n" + S3)
check("★ 重排后仍然三段全剥掉（修复前残留 [2,3]）", leftovers(out) == [], f"残留={leftovers(out)}")

print("\n## 3) 段间穿插文字 / 空块")
out = strip(S1 + "\n（顺带说一句）\n" + S2 + "\n<msg/>\n" + S3)
check("穿插说明文字仍全剥掉", leftovers(out) == [], f"残留={leftovers(out)}")

print("\n## 4) ★ 不丢内容：未投递的段必须原样保留")
S4 = '<msg><text>这段没发出去过，绝不能删</text></msg>'
out = strip(S1 + "\n" + S2 + "\n" + S4)
check("已发段剥掉、未发段保留", leftovers(out, SEGS[:2]) == [] and "绝不能删" in out,
      f"残留={leftovers(out, SEGS[:2])} 含未发段={'绝不能删' in out}")

print("\n## 5) 实际剥掉段数（日志用）")
out = strip(S1 + "\n" + S2 + "\n" + S3)
check("count_segments_removed == 3", es.count_segments_removed(out, SEGS) == 3,
      str(es.count_segments_removed(out, SEGS)))
partial = S1 + "\n" + "<msg><text>别人的新块</text></msg>"
check("部分台账场景下报数正确（1）",
      es.count_segments_removed(strip(partial, [S1]), [S1]) == 1)

print(f"\n===== 剥离兜底回归：{len(PASS)}/{len(PASS) + len(FAIL)} 通过 =====")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("STRIP LEFTOVER TEST PASSED")
