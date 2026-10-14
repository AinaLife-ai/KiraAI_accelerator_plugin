"""★★ 兼容性：剥离兜底与 QQ增强（AFTER_XML_PARSE 改写）、markdown / keyboard / at / reply 共存。

## 为什么要这一套

acc 的"解析前剥离"作用在**文本层**（`parse_xml` 之前），而 QQ增强 这类插件的改写发生在
**链层**（`AFTER_XML_PARSE` 钩子里拆分/重排 `<msg>` 链）。两者层次不同，但最终都决定
"哪些内容已经被发出去、框架不该再发"。

本套件锁住四件事：

1. **markdown / keyboard / at / reply 混合段** ⇒ 剥离照常（不能被这些标签干扰）；
2. **重排/穿插**（v1.0.91 的兜底场景）⇒ 全剥掉，不留下会被框架重发的段；
3. ★ **不丢内容**：别的插件**新增/改包**的块（xml_tag_fixer 把裸文本包成 `<msg>`、
   QQ增强 拆行产生的新块）**绝不能被误删**；
4. ★ **文本层被拆分**（把一段拆成两个 `<msg>`）时，兜底剥不到 ⇒ **必须能报出"只剥了 N 段"**，
   让上层打 warning（修复前的日志只会说"剥离 3 段"，把线索掩盖）。
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("es_compat", str(ROOT / "early_sent.py"))
es = importlib.util.module_from_spec(spec)
sys.modules["es_compat"] = es
spec.loader.exec_module(es)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


# ── 带 markdown / keyboard / at / reply 的真实形态段 ──────────────────
M1 = ('<msg><reply>qqo-c806cda6ae</reply><at>E8DF94D4</at>'
      '<markdown>## 标题\n- 项一</markdown><keyboard>{"rows":[]}</keyboard>'
      '<text>姐姐捏 袜子也归姐姐研究ww</text><sticker>11</sticker></msg>')
M2 = ('<msg><reply>qqo-f820d524a1</reply><at>2025B5BE</at>'
      '<text>C7姐三分钟是上限呀w</text><sticker>27</sticker></msg>')
M3 = '<msg><reply>qqo-85d7d243c5</reply><text>那香香不肯了w</text><sticker>12</sticker></msg>'
SEGS = [M1, M2, M3]


def strip(text, segs=SEGS):
    return es.strip_early_sent_smart(text, len(segs), segs)


def leftovers(out, segs=SEGS):
    n = es._norm_seg(out)
    return [i + 1 for i, s in enumerate(segs) if es._norm_seg(s) and es._norm_seg(s) in n]


print("## 1) markdown / keyboard / at / reply 混合段 —— 正常连续")
out = strip(M1 + "\n" + M2 + "\n" + M3)
check("三段全剥掉（含 markdown/keyboard 段）", leftovers(out) == [], f"残留={leftovers(out)}")

print("\n## 2) 同上但**重排**（v1.0.91 兜底场景）")
out = strip(M2 + "\n" + M3 + "\n" + M1)
check("★ 重排后仍全剥掉（修复前会残留 ⇒ 框架重发）", leftovers(out) == [], f"残留={leftovers(out)}")

print("\n## 3) ★ 不丢内容：别的插件新增/改包的块必须原样保留")
# xml_tag_fixer 行为：把散落裸文本包成 <msg>；QQ增强 拆行也可能产生新块
NEW_BARE = "（这是模型写的散落裸文本，被 xml_tag_fixer 包成了块）"
out = strip(f"<msg>{NEW_BARE}</msg>\n" + M1 + "\n" + M2)
check("新增块保留、已发段剥掉",
      NEW_BARE in out and leftovers(out, SEGS[:2]) == [],
      f"新块在={NEW_BARE in out} 残留={leftovers(out, SEGS[:2])}")
KA = '{"rows":[{"buttons":[{"id":"b1"}]}]}'
M1B = M1.replace('{"rows":[]}', KA)
out = strip(M1B + "\n" + M2 + "\n" + M3, [M1B, M2, M3])
check("keyboard 载荷变化后仍能配对剥掉（按规范化内容）", leftovers(out, [M1B, M2, M3]) == [])

print("\n## 4) ★ 文本层被拆分 ⇒ 兜底剥不到时必须**报出真实段数**（上层据此告警）")
split_text = ('<msg><text>姐姐捏 袜子也归姐姐研究ww</text></msg>\n'
              '<msg><sticker>11</sticker></msg>\n' + M2 + "\n" + M3)
out = strip(split_text)
removed = es.count_segments_removed(out, SEGS)
check("★ 拆分段剥不到 ⇒ 实际剥掉数 < 台账数（会触发 main.py 的 warning）",
      removed < len(SEGS), f"实际剥掉={removed} / 台账={len(SEGS)}")
check("★ 且不误删拆分段的内容（不丢内容优先）",
      "姐姐捏" in out or "11" in out, f"剩余={len(out.strip())}字")

print("\n## 5) 空占位 / 纯媒体段不受影响")
out = strip(M1 + "\n<msg/>\n" + M2 + "\n<msg/>\n" + M3)
check("夹在中间的空占位一并清理、三段仍全剥掉", leftovers(out) == [], f"残留={leftovers(out)}")

print(f"\n===== QQ增强/富标签兼容回归：{len(PASS)}/{len(PASS) + len(FAIL)} 通过 =====")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("STRIP COMPAT TEST PASSED")
