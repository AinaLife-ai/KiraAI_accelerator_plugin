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
ES_SRC = (ROOT / "early_sent.py").read_text(encoding="utf-8")
_ES_CODE = re.sub(r"#[^\n]*", "", ES_SRC)
check("用 elem_ident 取媒体身份（v1.0.85 起由 chain_signature 统一产出）",
      "elem_ident(e)" in _ES_CODE and "chain_signature(chain)" in _CODE)
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
    from core.chat.message_elements import (   # noqa: E402
        Text, Sticker, Image, At, Emoji, File, Record, Video, Json, Forward)
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

    # ────────────────────────────────────────────────────────────
    # 5b) v1.0.84：<file> 本地路径 & 图片 md5 —— 同类重复（线上：文件被发两次）
    #     框架解析 `<file>` 会把 data/ 相对路径拼成**绝对路径**
    #     （kira-ai 的 resolve_local_send_path），台账里却是原始 XML ⇒ 严格相等时
    #     身份一个都对不上 ⇒ 台账不出账 ⇒ 框架再发一次。下面是真实形状的回归。
    # ────────────────────────────────────────────────────────────
    print()
    print("═══ 5b) v1.0.84：<file> 本地路径 / 图片 md5（同类重复）")
    _tmp = pathlib.Path("/tmp") / "acc_media_dedupe_t"
    _abs = str(_tmp / "data" / "files" / "香香写给哥.txt")
    pathlib.Path(_abs).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(_abs).write_text("hi", encoding="utf-8")

    SEG_FILE_REL = '<msg><file type="file">data/files/香香写给哥.txt</file></msg>'
    instF, kF = _fresh([SEG_FILE_REL])
    check("★ 文件：链里是**绝对路径**元素、台账是相对路径 ⇒ 必须命中"
          "（修前：None ⇒ 框架再发一次 ⇒ 文件重复）",
          instF._consume_early_match(SID, MessageChain([File(file=_abs, name="香香写给哥.txt")])) == "R0")
    check("★ 整段覆盖 ⇒ 出账（不会一直挂着这条台账）", not (instF._sent_ledger.get(kF) or []))

    SEG_IMG_REL = '<msg><file type="image">data/files/pic.png</file></msg>'
    _abs_pic = str(_tmp / "data" / "files" / "pic.png")
    pathlib.Path(_abs_pic).write_bytes(b"\x89PNG\r\n\x1a\n")
    instP, _ = _fresh([SEG_IMG_REL])
    check("★ 本地图片：绝对路径元素 vs 相对路径台账 ⇒ 命中",
          instP._consume_early_match(SID, MessageChain([Image(image=_abs_pic)])) == "R0")

    SEG_IMG_URL = '<msg><file type="image">https://cdn/a.png</file></msg>'
    _img_md5 = Image(image="https://cdn/a.png")
    _img_md5.md5 = "9f2cdeadbeef00112233"      # 模拟 message_formatter 调过 hash_image
    instM, _ = _fresh([SEG_IMG_URL])
    check("★ URL 图片但 md5 已被算过 ⇒ 仍要命中（修前：元素身份=md5 ⇒ 对不上 ⇒ 重复）",
          instM._consume_early_match(SID, MessageChain([_img_md5])) == "R0")

    # ────────────────────────────────────────────────────────────
    # 5c) v1.0.85：链签名台账 —— 生成类 / 结构类不再重复
    #     抢发时那条链就在手上，台账直接记**元素侧签名**（与对账同一函数）
    #     ⇒ 解析期改写（路径、md5、生成结果）再也影响不到对账。
    #     生成类（出图/自拍/TTS/视频）的元素身份是**生成结果**、与台账里的
    #     提示词不可比 ⇒ 靠"元素种类一致 + 本轮顺序"配对（volatile 记录）。
    # ────────────────────────────────────────────────────────────
    print()
    print("═══ 5c) v1.0.85：链签名台账（生成类/结构类不再重复）")

    def _with_records(seg, pre_chain, n_records=1, ok=True):
        inst = main_mod.AcceleratorPlugin(_Ctx(), {})
        k = inst._ckey(SID, "evt-1")
        inst._sent_ledger[k] = [seg]
        inst._early_results[k] = ["R0"]
        inst._chain_records[k] = [
            {"sig": es.chain_signature(pre_chain), "result": ("R%d" % i if ok else None),
             "volatile": ("<img" in seg.lower() or "<selfie" in seg.lower()
                          or "<record" in seg.lower() or "<video" in seg.lower())}
            for i in range(n_records)]
        return inst, k

    class _Res:
        def __init__(self, mid, ok=True):
            self.message_id = mid
            self.ok = ok

    # 6 类"元素身份不可比"的段：修前全部不命中（框架再发一次 ⇒ 重复）
    _GEN = [
        ("出图 <img>", "<msg><img>画一只猫</img></msg>",
         lambda url: MessageChain([Image(image=url)])),
        ("自拍 <selfie>", "<msg><selfie>来张自拍</selfie></msg>",
         lambda url: MessageChain([Image(image=url)])),
        ("TTS <record>", "<msg><record>说句话</record></msg>",
         lambda url: MessageChain([Record(record=url)])),
        ("生成 <video>", "<msg><video>一段视频</video></msg>",
         lambda url: MessageChain([Video(file=url)])),
        ("<json>", '<msg><json>{"a":1}</json></msg>',
         lambda url: MessageChain([Json({"a": 1})])),
        ("<forward>", '<msg><forward merge="true">m1,m2</forward></msg>',
         lambda url: MessageChain([Forward(message_id=["m1", "m2"], merge=True)])),
    ]
    for _label, _seg, _mk in _GEN:
        _pre = _mk("https://cdn/early.png" if "json" not in _label and "forward" not in _label
                   else "https://cdn/irrelevant")
        _fw = _mk("https://cdn/later.png" if "json" not in _label and "forward" not in _label
                  else "https://cdn/irrelevant")
        _inst, _k = _with_records(_seg, _pre)
        check(f"★ {_label}：抢发段必须被消费（修前：不命中 ⇒ 框架再发一次）",
              _inst._consume_early_match(SID, _fw) is not None)
        check(f"   {_label}：消费后记录清空", not (_inst._chain_records.get(_k) or []))

    # 种类不同 ⇒ 绝不能配对（防误吃丢内容）
    _instA, _ = _with_records("<msg><img>猫</img></msg>", MessageChain([Image(image="https://cdn/a.png")]))
    check("★ 生成类但元素种类不同（记的是 Image、来的是 Record）⇒ 放行",
          _instA._consume_early_match(SID, MessageChain([Record(record="https://cdn/b.mp3")])) is None)
    _instB, _ = _with_records("<msg><record>话</record></msg>",
                              MessageChain([Record(record="https://cdn/a.mp3")]))
    check("★ 生成类但元素种类不同（记的是 Record、来的是 Image）⇒ 放行",
          _instB._consume_early_match(SID, MessageChain([Image(image="https://cdn/b.png")])) is None)

    # 抢发结果明确失败 ⇒ 销账但不返回（框架这次发的正是那一段，必须放行）
    _instC, _kC = _with_records("<msg><img>猫</img></msg>",
                                MessageChain([Image(image="https://cdn/a.png")]), ok=False)
    check("★ 抢发那遍**没发成功**（ok=False）⇒ 放行框架去发（不丢内容）",
          _instC._consume_early_match(SID, MessageChain([Image(image="https://cdn/b.png")])) is None)
    check("   但记录已被销账（不会一直挂着）", not (_instC._chain_records.get(_kC) or []))

    # 同轮两条生成段：按顺序各消费一条，结果各归各（message_id 不再错位）
    _instD = main_mod.AcceleratorPlugin(_Ctx(), {})
    _kD = _instD._ckey(SID, "evt-1")
    _instD._sent_ledger[_kD] = ["<msg><img>一</img></msg>", "<msg><img>二</img></msg>"]
    _instD._early_results[_kD] = [_Res("M1"), _Res("M2")]
    _instD._chain_records[_kD] = [
        {"sig": es.chain_signature(MessageChain([Image(image="https://cdn/g1.png")])),
         "result": _Res("M1"), "volatile": True},
        {"sig": es.chain_signature(MessageChain([Image(image="https://cdn/g2.png")])),
         "result": _Res("M2"), "volatile": True},
    ]
    _h1 = _instD._consume_early_match(SID, MessageChain([Image(image="https://cdn/h1.png")]))
    _h2 = _instD._consume_early_match(SID, MessageChain([Image(image="https://cdn/h2.png")]))
    check("★ 同轮两条生成段：按顺序各归各（ID 不错位）",
          getattr(_h1, "message_id", None) == "M1" and getattr(_h2, "message_id", None) == "M2",
          f"{getattr(_h1, 'message_id', None)}/{getattr(_h2, 'message_id', None)}")
    check("   两条记录都出账", not (_instD._chain_records.get(_kD) or []))

    # 拆分场景（sticker + text 两条链 = 两条记录）⇒ 各自命中、ID 各归各
    _instE = main_mod.AcceleratorPlugin(_Ctx(), {})
    _kE = _instE._ckey(SID, "evt-1")
    _instE._sent_ledger[_kE] = ["<msg><sticker>27</sticker><text>看看</text></msg>"]
    _instE._early_results[_kE] = [_Res("S1"), _Res("S2")]
    _instE._chain_records[_kE] = [
        {"sig": es.chain_signature(MessageChain([Sticker(sticker="data:image/png;base64,AA", sticker_id="27")])),
         "result": _Res("S1"), "volatile": False},
        {"sig": es.chain_signature(MessageChain([Text("看看")])), "result": _Res("S2"), "volatile": False},
    ]
    _m = _instE._consume_early_match(SID, MessageChain([Sticker(sticker="https://x/s27.png", sticker_id="27")]))
    _t = _instE._consume_early_match(SID, MessageChain([Text("看看")]))
    check("★ 拆链场景：媒体链拿抢发的第 1 条、文字链拿第 2 条（ID 对位）",
          getattr(_m, "message_id", None) == "S1" and getattr(_t, "message_id", None) == "S2",
          f"{getattr(_m, 'message_id', None)}/{getattr(_t, 'message_id', None)}")

    # 没抢发过的内容绝不能被链签名台账吃掉
    _instF2, _ = _with_records("<msg><img>猫</img></msg>", MessageChain([Image(image="https://cdn/a.png")]))
    check("★ 链签名台账没有对应记录 ⇒ 放行（不丢内容）",
          _instF2._consume_early_match(SID, MessageChain([Text("全新的一句话")])) is None)

    # 静态守卫：记账必须真的在 `_emit_segment` 里、且标注生成类
    _EMIT = MAIN[MAIN.find("async def _emit_segment"):MAIN.find("async def _emit_segment") + 40000]
    _EMIT_CODE = re.sub(r"#[^\n]*", "", _EMIT)
    check("★ 抢发时记下链签名（_chain_records）", "_chain_records.setdefault(_k, [])" in _EMIT_CODE)
    check("★ 生成类段标注 volatile（img/selfie/record/video）",
          "volatile" in _EMIT_CODE and "img|selfie|record|video" in _EMIT_CODE)
    check("★ 对账首选链签名、段台账留作兜底",
          "_consume_chain_record(" in _CODE and "self._sent_ledger.keys()" in _CODE)

    # 误吃防线：同名不同目录，绝不能算同一张（那会丢内容）
    _other = _tmp / "elsewhere" / "other" / "a.txt"
    _other.parent.mkdir(parents=True, exist_ok=True)
    _other.write_text("x", encoding="utf-8")
    _zh = _tmp / "elsewhere" / "zhabc.txt"
    _zh.write_text("x", encoding="utf-8")
    instN, _ = _fresh(['<msg><file type="file">data/files/a.txt</file></msg>'])
    check("★ 另一个目录下的同名文件 ⇒ 放行（宁可重复，也不误吃丢内容）",
          instN._consume_early_match(
              SID, MessageChain([File(file=str(_other), name="a.txt")])) is None)
    check("★ 后缀撞车（zhabc.txt ≠ abc.txt）⇒ 放行",
          instN._consume_early_match(
              SID, MessageChain([File(file=str(_zh), name="zhabc.txt")])) is None)
    # 文件与图片混在同一轮：各自的台账各自消费
    instX, kX = _fresh([SEG_FILE_REL, SEG_IMG_URL])
    check("同一轮里文件+图片各消费各的",
          instX._consume_early_match(SID, MessageChain([File(file=_abs, name="香香写给哥.txt")])) == "R0"
          and instX._consume_early_match(SID, MessageChain([Image(image="https://cdn/a.png")])) == "R1"
          and not (instX._sent_ledger.get(kX) or []))

# ────────────────────────────────────────────────────────────────
# 6) v1.0.84 纯逻辑：身份规范化 + 身份匹配（不依赖框架源码）
# ────────────────────────────────────────────────────────────────
print()
print("═══ 6) v1.0.84 纯逻辑：ident_match / _canon_ident")
_im = es.ident_match
check("相等 ⇒ 匹配", _im("https://cdn/a.png", "https://cdn/a.png") is True)
check("data/ 相对路径 vs 它的绝对形式 ⇒ 匹配（这是线上那条文件重复的修法）",
      _im("data/files/香香写给哥.txt",
          "/var/minis/data/files/香香写给哥.txt") is True)
check("顺序无关", _im("/root/data/files/a.png", "data/files/a.png") is True)
check("空值一律不匹配（避免空串互相吃掉）",
      _im("", "") is False and _im("", "data/files/a.txt") is False)
check("同名不同目录 ⇒ 不匹配（绝不丢内容）",
      _im("data/files/a.txt", "/elsewhere/other/a.txt") is False)
check("后缀撞车不算（必须整段路径对得上）",
      _im("data/files/abc.txt", "/x/zhabc.txt") is False)
check("非 data/ 的短边不放宽（URL/绝对路径只认严格相等）",
      _im("https://cdn/a.png", "/tmp/x/a.png") is False
      and _im("/mnt/a.png", "/other/a.png") is False)
check("图片 md5 不再抢 `file` 的身份",
      es.elem_ident(type("E", (), {"file": "https://cdn/a.png", "md5": "deadbeef"})()) == "https://cdn/a.png")
check("只有 md5 时仍可当身份（兜底，不报错）",
      es.elem_ident(type("E", (), {"md5": " deadbeef "})()) == "deadbeef")
check("反斜杠归一到 /",
      es._canon_ident("data\\files\\a.txt") == "data/files/a.txt")
check("data/ 路径做 normpath（`data/./files/x` 也要认）",
      es._canon_ident("data/./files/x.txt") == "data/files/x.txt")
check("台账身份同样规范化",
      es.seg_idents('<msg><file type="file">data\\./files\\\\x.txt</file></msg>')
      == ["data/files/x.txt"])
check("带空白的台账身份被去掉空白",
      es.seg_idents("<msg><sticker> 27 </sticker></msg>") == ["27"])

# 静态守卫：对账循环里必须用 ident_match（不能用严格 `in`）
_i = MAIN.find("def _consume_early_match")
_j = MAIN.find("\n    def ", _i + 10)
_BODY2 = re.sub(r"#[^\n]*", "", MAIN[_i:_j if _j > _i else len(MAIN)])
check("★ 对账身份比较走 ident_match（不再用 `need in rest_ids`）",
      "ident_match(need, c)" in _BODY2 and "if need in rest_ids" not in _BODY2)

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 表情包不会再被发两次（且没有把重复换到文字上）")
