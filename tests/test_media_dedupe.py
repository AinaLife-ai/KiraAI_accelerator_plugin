"""★★★ 回归：3.0 下「表情包被发两次」—— 纯媒体链在按链对账里匹配不上。

## 用户报告（线上）

官方 QQ bot + KiraAI 3.0rc1：**表情包发出两次**，文字正常。
把本插件的 `section_stream.early_send`（抢先发送）关掉后重复消失
⇒ 问题出在「抢发去重」这一环，而不是表情控制插件本身。

## 根因（用本仓库真实代码复现过）

3.0 的发送按**链**走，抢发去重落在 `_consume_early_match()`：它原来只比对
**可见文字**——

    text = "".join(getattr(e, "text", "") or "" for e in chain)
    norm = _norm_seg(text)
    if not norm:
        return None                     # ← 媒体链永远走到这里

而 `Sticker`/`Image` 这类元素**没有 text** ⇒ 纯媒体链的 norm 为空 ⇒
不消费台账 ⇒ 框架把已经抢发过的表情包**再发一次**。
文字链能匹配上 ⇒ 文字不重复 ⇒ 与线上现象（只有表情包重复）完全吻合。

触发条件是「表情独立成行」这类 `AFTER_XML_PARSE` 改写：它把一个
`<msg><sticker>21</sticker><text>…</text></msg>` 拆成「贴纸一条 + 文字一条」，
于是框架发来的正是**一条纯媒体链**，精准落进上面的盲区。
（2.x 的剥离路径早修过同一类坑 —— `early_sent._block_has_content` 的注释里写着
「媒体段没有文字 ⇒ 匹配不到 ⇒ 框架把贴纸/图片再发一遍」；3.0 的链级路径没搬过来。）

## 修法

· 两侧都用**身份令牌**：`early_sent.elem_ident`（元素侧）与 `seg_idents`（台账 XML 侧），
  规则同源、**tag 名不参与**比对（同一个 `<file type="image">` 在链路里是 Image 元素）；
· 文字维度改用 `norm_text_only`（把媒体标签**连同内文**去掉），
  这样贴纸 id 不会被当成正文，纯媒体段命中后能正常出账；
· **部分消费**：一条台账段可被两条链分别消费（贴纸链吃媒体、文字链吃文字），
  只有整段被覆盖才出账 —— 否则贴纸链吃掉整段后文字链找不到台账 ⇒ 变成文字重复；
· 保守取向：拿不到身份的生成类（`<img>` 出图 / `<record>` TTS）**不参与**比对 ——
  宁可不剥（最多重复一次），也不误吃（会丢内容）。
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _env  # noqa: E402

ROOT = _env.ROOT
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


def _fake(cls_name, **fields):
    """元素桩：字段名与真实元素一致（身份提取只依赖字段名与类名）。"""
    return type(cls_name, (), fields)()


# ────────────────────────────────────────────────────────────────
# 1) 纯逻辑：身份令牌（不需要框架）
# ────────────────────────────────────────────────────────────────
es = _env.load("early_sent")

print("═══ 1) 台账 XML 段 → 身份令牌 `seg_idents`")
check("贴纸段取内文当身份", es.seg_idents("<msg><sticker>21</sticker><text>hi</text></msg>") == ["21"])
check("file 段取内文（发送已有图片走这条）",
      es.seg_idents('<msg><file type="image">https://x/a.png</file></msg>') == ["https://x/a.png"])
check("属性型媒体取属性",
      es.seg_idents('<msg><image url="https://y/b.png"/></msg>') == ["https://y/b.png"])
check("结构化元素（at/reply/emoji）都能取到",
      es.seg_idents("<msg><at>all</at><reply>m1</reply><emoji>e9</emoji></msg>") == ["all", "m1", "e9"])
check("生成类 <img>（值只是提示词）不参与比对 —— 保守不误吃",
      es.seg_idents('<msg><img path="p">画一只猫</img></msg>') == [])
check("纯文字段没有身份", es.seg_idents("<msg><text>hello</text></msg>") == [])
check("外层 <msg> 包裹不会把内层标签吞掉（实现时踩过）",
      es.seg_idents("<msg><msg><sticker>7</sticker></msg></msg>") == ["7"])

print()
print("═══ 2) 文字维度 `norm_text_only`：媒体 id 不能混进正文")
check("贴纸+文字段：只剩正文",
      es.norm_text_only("<msg><sticker>21</sticker><text>哥ww香香乖乖等</text></msg>") == "哥ww香香乖乖等")
check("纯贴纸段：正文为空（于是可被判为整段已覆盖 ⇒ 出账）",
      es.norm_text_only("<msg><sticker>21</sticker></msg>") == "")
check("原有 `_norm_seg` 保持不变（2.x 剥离路径与既有测试都依赖它）",
      es._norm_seg("<msg><sticker>21</sticker></msg>") == "21")

print()
print("═══ 3) 元素侧身份 `elem_ident`")
check("贴纸取 sticker_id（而不是 base64 的 file）",
      es.elem_ident(_fake("Sticker", file="data:image/png;base64,AAA", sticker_id="21")) == "21")
check("图片取 file", es.elem_ident(_fake("Image", file="https://x/a.png")) == "https://x/a.png")
check("@ 取 pid", es.elem_ident(_fake("At", pid="all")) == "all")
check("回复取 message_id", es.elem_ident(_fake("Reply", message_id="m1")) == "m1")
check("文字元素没有身份", es.elem_ident(_fake("Text", text="hello")) == "")
check("拿不到身份的未知元素返回空（不参与比对）", es.elem_ident(_fake("Weird", foo=1)) == "")

# ────────────────────────────────────────────────────────────────
# 4) 静态守卫：对账判据里必须真的用了身份维度
# ────────────────────────────────────────────────────────────────
print()
print("═══ 4) 静态守卫：`_consume_early_match` 必须把身份维度算进去")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
_i = MAIN.find("def _consume_early_match")
_j = MAIN.find("\n    def ", _i + 10)
_BODY = MAIN[_i:_j if _j > _i else len(MAIN)]
_CODE = re.sub(r"#[^\n]*", "", _BODY)          # 去注释：注释里提到旧写法不算数
check("用 elem_ident 取媒体身份", "elem_ident(e)" in _CODE)
check("用 seg_idents 取台账身份", "seg_idents(seg)" in _CODE)
check("文字维度用 norm_text_only（不再拿 _norm_seg(seg) 当正文）",
      "norm_text_only(" in _CODE and "_norm_seg(seg)" not in _CODE)
check("旧的「只有文字就放行」守卫已被替换",
      "if not norm and not idents" in _CODE)

# ────────────────────────────────────────────────────────────────
# 5) 集成：真元素 + 真方法（没有框架源码时跳过）
# ────────────────────────────────────────────────────────────────
FW = _env.framework()
if FW is None:
    print()
    print("═══ 5) 集成：按链对账的真实行为 —— [SKIP] 需要 KIRA_FW")
else:
    sys.path.insert(0, FW)
    from core.chat.message_elements import Text, Sticker, Image, At, Emoji   # noqa: E402
    from core.chat import MessageChain                                      # noqa: E402

    main_mod = _env.load("main")
    SID = "qq:gm:1"
    SEG_MIX = "<msg><sticker>21</sticker><text>哥ww香香乖乖等</text></msg>"
    SEG_TEXT = "<msg><text>只有文字</text></msg>"
    SEG_IMG = '<msg><file type="image">https://cdn/a.png</file></msg>'

    class _Ctx:
        message_delivery = None
        message_processor = None
        adapter_mgr = None

    def _fresh(segs, results=None):
        inst = main_mod.AcceleratorPlugin(_Ctx(), {})
        key = inst._ckey(SID, "evt-1")
        inst._sent_ledger[key] = list(segs)
        inst._early_results[key] = list(
            results if results is not None else ["R%d" % i for i in range(len(segs))])
        return inst, key

    print()
    print("═══ 5) 集成：按链对账的真实行为")
    # 5.1 本次 bug 的核心：纯贴纸链必须命中
    inst, key = _fresh([SEG_MIX])
    hit = inst._consume_early_match(
        SID, MessageChain([Sticker(sticker="data:image/png;base64,AAA", sticker_id="21")]))
    check("★ 纯贴纸链命中台账并还回抢发结果（修前：None ⇒ 框架重发 ⇒ 表情包两次）",
          hit == "R0", f"实得 {hit!r}")
    check("★ 部分消费：台账保留，兄弟文字链还要用",
          len(inst._sent_ledger.get(key) or []) == 1)
    # 5.2 兄弟文字链也必须命中（否则变成文字重复）
    hit2 = inst._consume_early_match(SID, MessageChain([Text("哥ww香香乖乖等")]))
    check("★ 拆出来的文字链同样命中（不会把重复从表情换到文字）", hit2 == "R0", f"实得 {hit2!r}")
    # 5.3 顺序反过来也一样
    inst3, _ = _fresh([SEG_MIX])
    h1 = inst3._consume_early_match(SID, MessageChain([Text("哥ww香香乖乖等")]))
    h2 = inst3._consume_early_match(
        SID, MessageChain([Sticker(sticker="https://x/s21.png", sticker_id="21")]))
    check("★ 先文字后贴纸：两条都命中", h1 == "R0" and h2 == "R0", f"{h1!r}/{h2!r}")
    # 5.4 单条链整段覆盖 ⇒ 出账（纯文字 / 纯媒体）
    inst4, k4 = _fresh([SEG_TEXT])
    check("纯文字链命中且出账（旧行为不回归）",
          inst4._consume_early_match(SID, MessageChain([Text("只有文字")])) == "R0"
          and not (inst4._sent_ledger.get(k4) or []))
    inst5, k5 = _fresh(["<msg><sticker>7</sticker></msg>"])
    check("纯媒体段命中且出账",
          inst5._consume_early_match(
              SID, MessageChain([Sticker(sticker="https://x/s7.png", sticker_id="7")])) == "R0"
          and not (inst5._sent_ledger.get(k5) or []))
    # 5.5 没抢发过的内容绝不能被吃掉
    inst6, _ = _fresh([SEG_MIX])
    check("不同贴纸 ⇒ 放行（不丢内容）",
          inst6._consume_early_match(
              SID, MessageChain([Sticker(sticker="https://x/s99.png", sticker_id="99")])) is None)
    check("不同文字 ⇒ 放行（不丢内容）",
          inst6._consume_early_match(SID, MessageChain([Text("完全不同的新话")])) is None)
    # 5.6 属性型媒体 / 结构化元素 / 生成类
    inst7, _ = _fresh([SEG_IMG])
    check("file 段（发送已有图片）按身份命中",
          inst7._consume_early_match(SID, MessageChain([Image(image="https://cdn/a.png")])) == "R0")
    inst8, _ = _fresh(['<msg><img path="p">画一只猫</img></msg>'])
    check("★ 生成类出图：身份不可比 ⇒ 放行（宁可重复也不误吃）",
          inst8._consume_early_match(
              SID, MessageChain([Image(image="https://cdn/generated.png")])) is None)
    inst9, _ = _fresh(["<msg><at>all</at><text>集合啦</text></msg>"])
    check("@ 元素链命中",
          inst9._consume_early_match(SID, MessageChain([At("all")])) == "R0")
    inst10, _ = _fresh(["<msg><emoji>e9</emoji><text>笑死</text></msg>"])
    check("emoji 链命中",
          inst10._consume_early_match(SID, MessageChain([Emoji("e9")])) == "R0")
    # 5.7 台账/结果不齐：放行（宁可重发，不还错结果）
    inst11, _ = _fresh([SEG_MIX], results=[])
    check("结果缺失 ⇒ 放行（不还错结果）",
          inst11._consume_early_match(
              SID, MessageChain([Sticker(sticker="https://x/s21.png", sticker_id="21")])) is None)

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 表情包不会再被发两次（且没有把重复换到文字上）")
