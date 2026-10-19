"""★★ 内容级去重：与已发段同内容（仅差 quote/@ 元数据）的残留块必须丢弃。

## 线上现象（引用回复重复）

模型输出两个 `<msg>`：
1. `<msg><text>正文</text><sticker>7</sticker></msg>` —— acc 抢先发；
2. `<msg><reply>…</reply><at>…</at><text>正文</text><sticker>7</sticker></msg>`

文本剥离按内容匹配，第 2 块因为多了 reply/at，**不是**第 1 块的超集 ⇒ 剥不掉
⇒ 框架再发一次 ⇒ 群里看到「正文 @昵称 正文」。

## 本测试锁住的行为

* 同内容（仅差 reply/at）⇒ 整块删除，**不重发**；
* 正文不同 ⇒ 保留（不丢内容）；
* 表情不同 ⇒ 保留（可能是新内容）；
* 空文本 / 空台账 ⇒ 原样返回；
* 异常输入不抛异常。
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("es_dedupe", str(ROOT / "early_sent.py"))
es = importlib.util.module_from_spec(spec)
spec.loader.exec_module(es)

FAIL = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


B1 = ('<msg><text>脏一点都不放过ww</text><text>哥真严  香香乖</text>'
      '<sticker>7</sticker></msg>')
B2 = ('<msg message_id="qqo-54d97b0b0e"><reply>qqo-b2973cedfd</reply>'
      '<at>9CD54739CC9BAA46B93243088802DC72</at>'
      '<text>脏一点都不放过ww</text><text>哥真严  香香乖</text>'
      '<sticker>7</sticker></msg>')

print("## 1) 同文双块（线上案例）")
# 生产顺序：先按台账剥离（B1 已发），再对残留做内容去重
stripped = es.strip_early_sent_smart(B1 + "\n" + B2, 1, [B1])
out, dropped = es.drop_duplicate_blocks(stripped, [B1])
check("残留块被整块删除", not out.strip() and len(dropped) == 1, repr(out)[:40])

print("\n## 2) 不丢内容：正文不同")
b3 = '<msg><reply>x</reply><text>完全不同的正文</text><sticker>7</sticker></msg>'
out, dropped = es.drop_duplicate_blocks(b3, [B1])
check("不同正文原样保留", bool(out.strip()) and not dropped)

print("\n## 3) 不丢内容：表情不同")
b4 = ('<msg><reply>x</reply><at>y</at><text>脏一点都不放过ww</text>'
      '<text>哥真严  香香乖</text><sticker>8</sticker></msg>')
out, dropped = es.drop_duplicate_blocks(b4, [B1])
check("不同表情原样保留", bool(out.strip()) and not dropped)

print("\n## 4) 空输入 / 空台账")
out, dropped = es.drop_duplicate_blocks("", [B1])
check("空文本原样返回", out == "" and not dropped)
out, dropped = es.drop_duplicate_blocks(B2, [])
check("空台账不删任何内容", out == B2 and not dropped)

print("\n## 5) 异常输入不抛")
try:
    out, dropped = es.drop_duplicate_blocks(None, None)
    check("None 输入安全", out is None and dropped == [])
except Exception as exc:  # noqa: BLE001
    check("None 输入安全", False, repr(exc))

print("\n" + ("ALL PASSED" if not FAIL else f"FAILED: {FAIL}"))
raise SystemExit(0 if not FAIL else 1)
