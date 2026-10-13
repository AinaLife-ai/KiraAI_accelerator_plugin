"""硬审查：其他插件的**自定义标签**在「抢发 + 解析前剥离」下是否安全（3.0）。

## 背景

生态里的插件都会往**同一个 per-request `tag_set`** 里注册自己的标签
（框架的内置标签、贴纸插件、以及任意第三方插件，都是 `@on.llm_request` 里
`tag_set.register(...)`）。acc 抢发时用的就是这个 tag_set，
所以自定义标签**同样会被抢发**；而 v1.0.86 又把"已抢发段"从框架那遍的 XML 里剥掉了
—— 这一刀必须确保对"我们不认识的标签/元素"同样安全。

## 本套件钉死的不变量

1. **只执行一次**：自定义标签段被抢发后，框架那遍**不会再执行它的 `handle()`**
   （副作用/生成不会跑两遍）——这正是 2.x 的行为（框架压根看不到那些段）；
2. **没成功交付就绝不剥离**（宁可重复，绝不丢内容）：
   · handler 在 acc 那遍抛异常；
   · handler 产出空列表（没有可发元素）；
   · **root 级标签**（`parent=None`，交回框架执行）；
   · 一段里有多条链，**中途发送失败**；
   以上情形 acc **不记账** ⇒ 剥离时找不到锚点 ⇒ 框架照常处理整段。
3. **不认识的元素类型**（自定义元素、没有 text 也没有可解析身份）：
   链级对账靠「元素种类完全一致 + 本轮顺序」配对（v1.0.85）也能出账 ⇒ 不重复。
4. 剥离 + 本轮反查是**毫秒级**（大响应实测），不堵塞事件循环。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _env  # noqa: E402

ROOT = _env.ROOT
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


FW = _env.framework()
if FW is None:
    print("[SKIP] 需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI 后重跑）")
    sys.exit(0)

sys.path.insert(0, FW)

# ★ 世代判断必须在**框架导入之前**：`MessageDeliveryService` 是 3.0 才有的模块，
#   2.x 上导入会直接 ImportError（那样就成了"失败"而不是"跳过"）。
if _env.gen() != "3":
    print("[SKIP] 本套件审查 3.0 的解析前剥离（2.x 走 send_xml_messages 入口剥离）")
    sys.exit(0)

import core.plugin  # noqa: E402,F401  —— 解开 core.tag ↔ core.plugin 循环导入
from core.tag import TagSet, BaseTag  # noqa: E402
from core.chat.message_utils import KiraIMSentResult  # noqa: E402
from core.chat.message_elements import Text  # noqa: E402
from core.workflow.src.im.message_delivery import MessageDeliveryService  # noqa: E402

main_mod = _env.load("main")
es = _env.load("early_sent")

SID = "qq:dm:9CD5"

# ── 模拟"别的插件的自定义标签" ────────────────────────────────────

class _CardElement:
    """acc **完全不认识**的元素类型（没有 text / 不在身份字段表里）。"""

    def __init__(self, card_id):
        self.card_id = card_id


class _CardTag(BaseTag):
    name = "card"
    calls = 0

    async def handle(self, value, **kwargs):
        type(self).calls += 1
        return [_CardElement(value)]


class _EmptyTag(BaseTag):
    name = "nothing"
    calls = 0

    async def handle(self, value, **kwargs):
        type(self).calls += 1
        return []                      # 产不出元素 ⇒ acc 那遍不会交付任何东西


class _BoomTag(BaseTag):
    name = "boom"

    async def handle(self, value, **kwargs):
        raise RuntimeError("custom tag boom")


class _RootNoticeTag(BaseTag):
    name = "notice"
    parent = None                      # root 级：必须交回框架执行

    async def handle(self, value, **kwargs):
        return []


class _TextTag(BaseTag):
    """真框架的 TextTag 等价物（本套件自持，避免依赖内置插件目录）。"""

    name = "text"

    async def handle(self, value, **kwargs):
        return [Text(value)] if value else []


class _Delivery:
    """假投递服务：parse_xml / add_message_ids 接到**真框架实现**上。"""

    min_message_delay = 0.0
    max_message_delay = 0.0
    seen: list = []

    @staticmethod
    async def parse_xml(xml_data, tag_set):
        _Delivery.seen.append(xml_data)
        return await MessageDeliveryService.parse_xml(xml_data, tag_set)

    @staticmethod
    def add_message_ids(xml_data, message_results):
        return MessageDeliveryService.add_message_ids(xml_data, message_results)

    async def send_message_chain(self, session, chain, **kwargs):
        return KiraIMSentResult(message_id="SENT", ok=True)


class _MP:
    """假的消息处理器（`_emit_segment` 用它投递）。"""

    def __init__(self, fail_on_call=None):
        self.sent = []
        self.fail_on_call = fail_on_call

    async def send_message_chain(self, sid, chain, **kwargs):
        self.sent.append(chain)
        if self.fail_on_call == len(self.sent):
            raise RuntimeError("send failed")
        return KiraIMSentResult(message_id="M%d" % len(self.sent), ok=True)


def _mk_plugin(mp=None):
    d = _Delivery()
    ctx = types.SimpleNamespace(message_delivery=d, message_processor=mp or _MP(),
                                adapter_mgr=None, provider_mgr=None)
    plugin = main_mod.AcceleratorPlugin(ctx, {})
    plugin._install_early_sent_strip()
    return plugin, d


def _tagset():
    ts = TagSet()
    ts.register(_TextTag, _CardTag, _EmptyTag, _BoomTag, _RootNoticeTag)
    return ts


async def main():
    # ═══ 1) 自定义标签：抢发后框架**不再执行**它的 handle() ═══
    print("═══ 1) 自定义标签只执行一次（副作用/生成不会跑两遍）")
    plugin, d = _mk_plugin()
    if "early_sent_parse_strip" not in {h.name for h in plugin.patches._handles}:
        print("[SKIP] 解析前剥离未安装（本世代不适用）")
        sys.exit(0)
    ts = _tagset()
    SEG_CARD = "<msg><card>c1</card></msg>"
    SEG_TXT = "<msg><text>看卡</text></msg>"
    FULL = SEG_CARD + SEG_TXT

    _CardTag.calls = 0
    _Delivery.seen.clear()
    await plugin._parse_seg_actions(SEG_CARD, ts, None)          # acc 抢发那遍
    check("acc 那遍真的执行了自定义标签", _CardTag.calls == 1, f"calls={_CardTag.calls}")

    key = plugin._ckey(SID, "evt-1")
    plugin._sent_ledger[key] = [SEG_CARD]
    plugin._early_results[key] = [KiraIMSentResult(message_id="PRE-C1", ok=True)]
    plugin._resp_by_sid[key] = types.SimpleNamespace(text_response=FULL)

    await d.parse_xml(FULL, ts)                                   # 框架那遍
    check("★ 框架那遍**不再执行**它（⇒ 副作用/计费只发生一次）",
          _CardTag.calls == 1, f"calls={_CardTag.calls}")
    check("★ 框架那遍只拿到剩余段（已抢发的自定义标签段被剥掉）",
          _Delivery.seen and SEG_CARD not in _Delivery.seen[-1] and SEG_TXT in _Delivery.seen[-1])

    # ═══ 2) 没成功交付 ⇒ 绝不剥离（宁可重复，绝不丢内容）═══
    print()
    print("═══ 2) 未交付的段绝不剥离")

    # 2a. handler 产出空列表
    plugin2, d2 = _mk_plugin()
    ev2 = types.SimpleNamespace(sid=SID, event_id="e2")
    sctx2 = main_mod.SendCtx(SID, ev2, _tagset())
    _EmptyTag.calls = 0
    seg_empty = "<msg><nothing>x</nothing></msg>"
    ok2 = await plugin2._emit_segment(seg_empty, sctx2)
    check("★ 产不出元素的段：不算交付（不记账）",
          ok2 is False and not (plugin2._sent_ledger.get(plugin2._ckey(SID, "e2")) or []),
          f"delivered={ok2}")

    # 2b. 自定义标签在 acc 那遍抛异常
    plugin3, d3 = _mk_plugin()
    ev3 = types.SimpleNamespace(sid=SID, event_id="e3")
    sctx3 = main_mod.SendCtx(SID, ev3, _tagset())
    ok3 = await plugin3._emit_segment("<msg><boom>1</boom></msg>", sctx3)
    check("★ handler 抛异常：整段交回框架（不记账）",
          ok3 is False and not (plugin3._sent_ledger.get(plugin3._ckey(SID, "e3")) or []))

    # 2c. root 级自定义标签
    plugin4, d4 = _mk_plugin()
    ev4 = types.SimpleNamespace(sid=SID, event_id="e4")
    sctx4 = main_mod.SendCtx(SID, ev4, _tagset())
    ok4 = await plugin4._emit_segment("<msg><notice>重要通知</notice></msg>", sctx4)
    check("★ root 级标签：交回框架执行（不记账、不剥离）",
          ok4 is False and not (plugin4._sent_ledger.get(plugin4._ckey(SID, "e4")) or []))

    # 2d. 一段多条链、中途发送失败
    plugin5, d5 = _mk_plugin(mp=_MP(fail_on_call=2))
    ev5 = types.SimpleNamespace(sid=SID, event_id="e5")
    sctx5 = main_mod.SendCtx(SID, ev5, _tagset())
    k5 = plugin5._ckey(SID, "e5")
    ok5 = await plugin5._emit_segment("<msg><card>a</card></msg><msg><card>b</card></msg>", sctx5)
    check("★ 中途发送失败：整段**不记账**（⇒ 不会被剥离 ⇒ 不丢内容）",
          not (plugin5._sent_ledger.get(k5) or []),
          f"ledger={plugin5._sent_ledger.get(k5)} delivered={ok5}")

    # ═══ 3) 不认识的元素类型：靠"元素种类 + 本轮顺序"也能出账 ═══
    print()
    print("═══ 3) 自定义元素（acc 不认识、无 text 无身份）也能去重")
    plugin6 = main_mod.AcceleratorPlugin(
        types.SimpleNamespace(message_delivery=None, message_processor=None,
                              adapter_mgr=None, provider_mgr=None), {})
    k6 = plugin6._ckey(SID, "e6")
    plugin6._chain_records[k6] = [{
        "sig": ("", [], ["_CardElement"]), "result": KiraIMSentResult(message_id="CARD-1", ok=True),
        "volatile": False}]
    hit = plugin6._consume_early_match(SID, [_CardElement("c9")])
    check("★ 同种类自定义元素 ⇒ 命中（不重复发送）",
          getattr(hit, "message_id", None) == "CARD-1", f"{hit!r}")
    plugin7 = main_mod.AcceleratorPlugin(
        types.SimpleNamespace(message_delivery=None, message_processor=None,
                              adapter_mgr=None, provider_mgr=None), {})
    k7 = plugin7._ckey(SID, "e7")
    plugin7._chain_records[k7] = [{
        "sig": ("", [], ["_CardElement"]), "result": KiraIMSentResult(message_id="CARD-1", ok=True),
        "volatile": False}]
    hit2 = plugin7._consume_early_match(SID, [_EmptyTag])
    check("★ 种类不同的自定义元素 ⇒ 放行（不误吃丢内容）", hit2 is None, f"{hit2!r}")

    # ═══ 4) 剥离 + 本轮反查的性能（不堵塞事件循环）═══
    print()
    print("═══ 4) 性能：剥离与本轮反查的耗时")
    plugin8, _d8 = _mk_plugin()
    segs = ["<msg><text>第%d条%s</text></msg>" % (i, "x" * 200) for i in range(40)]
    big = "".join(segs) + "<msg><text>剩余</text></msg>"
    k8 = plugin8._ckey(SID, "e8")
    plugin8._resp_by_sid[k8] = types.SimpleNamespace(text_response=big)
    plugin8._sent_ledger[k8] = segs
    t0 = time.perf_counter()
    for _ in range(20):
        plugin8._turn_for_text(big)
        from _env import gen  # noqa: F401
        es.strip_early_sent_smart(big, len(segs), segs)
    dt = (time.perf_counter() - t0) / 20 * 1000
    print(f"  文本 {len(big)} 字符 / {len(segs)} 段已抢发 ⇒ 单次 {dt:.2f} ms")
    check("★ 单次剥离+反查 < 20ms（不阻塞事件循环）", dt < 20, f"{dt:.2f} ms")

    # ═══ 5) 全序列复刻（execute.py 的真实顺序）═══
    print()
    print("═══ 5) 全序列复刻：解析（经剥离）→ 逐链发送 → 贴 ID")
    import re as _re
    plugin9, d9 = _mk_plugin()
    ts9 = _tagset()
    _CardTag.calls = 0
    _Delivery.seen.clear()
    await plugin9._parse_seg_actions(SEG_CARD, ts9, None)        # acc 抢发
    k9 = plugin9._ckey(SID, "evt-9")
    plugin9._sent_ledger[k9] = [SEG_CARD]
    plugin9._early_results[k9] = [KiraIMSentResult(message_id="PRE-C1", ok=True)]
    plugin9._resp_by_sid[k9] = types.SimpleNamespace(text_response=FULL)

    actions = await d9.parse_xml(FULL, ts9)                      # 框架那遍（经接管点）
    sent = []
    for a in actions:
        if hasattr(a, "__iter__") and not isinstance(a, str):
            sent.append(await d9.send_message_chain(SID, a))
    raw = d9.add_message_ids(FULL, sent)
    ids = _re.findall(r'<msg[^>]*message_id="([^"]*)"', raw)
    check("★ 自定义标签全程只执行 1 次", _CardTag.calls == 1, f"calls={_CardTag.calls}")
    check("★ 框架只发了「没抢发过」的那一段（条数 = 剩余段数）",
          len(sent) == 1, f"sent={len(sent)}")
    check("★ message_id 顺序 = [抢发的那条, 框架新发的]（模型历史不错位）",
          ids and ids[0] == "PRE-C1" and len(ids) == 2, str(ids))

    print()
    print("=" * 60)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("   ✗", f)
        sys.exit(1)
    print("🎉 全部通过 —— 自定义标签同样安全（只执行一次、未交付绝不剥离、未知元素也能去重）")


asyncio.run(main())
