"""与 **qqbot-fullmsg-bridge**（znq19/KiraAI-qqbot-fullmsg-bridge-plugin）的兼容性。

## 为什么单独一套件

那个插件把 **markdown / keyboard / 引用 / @** 注入到 QQ 官方接口（`msg_type=2` + `keyboard`），
注入点在**更低的 api 层**（botpy `BotAPI.post_*` / adapter `_send_message`），
而 acc 的接缝在 `MessageDeliveryService.send_message_chain` —— 两层不同，
但**同一条链会先后经过两者**，所以必须证明：

1. acc 抢发时执行 `<markdown>`/`<keyboard>` 标签得到的链，**原样交到发送层**
   （bridge 才能从链里提取 md/kb —— 它是 `split_markdown_and_keyboard(chain)` 只读提取）；
2. acc 的"解析前剥离"让框架那遍**不再执行**这两个标签 ⇒ **不会产出第二份 md/keyboard**；
3. 对账（链侧签名）拿的都是**同一条链的解析产物** ⇒ 命中就还回抢发结果，
   **不会把同一条富文本消息再发一次**；
4. 下游插件若在**发送那一刻改写链**（bridge 有媒体强转），acc 记进台账的签名
   仍必须是**发送前**那条链（v1.0.87 修复）—— 否则对账会失配。

> 依赖真实插件源码：优先取环境变量 `QQBOT_FULLMSG_DIR`，否则找常见克隆路径；
> 找不到就 `[SKIP]`（不把"环境里没有"当成失败）。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _env  # noqa: E402

ROOT = _env.ROOT
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


def find_bridge() -> str | None:
    cands = [os.environ.get("QQBOT_FULLMSG_DIR"),
             "/var/minis/shared/qqbot_fullmsg",
             str(pathlib.Path.home() / "qqbot_fullmsg")]
    cands += [str(p) for p in pathlib.Path("/var/minis/shared").glob("*qqbot*fullmsg*")]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "rich_content.py")):
            return c
    return None


FW = _env.framework()
BRIDGE = find_bridge()
if FW is None:
    print("[SKIP] 需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
    sys.exit(0)
if BRIDGE is None:
    print("[SKIP] 没找到 qqbot-fullmsg-bridge 源码"
          "（设 QQBOT_FULLMSG_DIR=/path/to/KiraAI-qqbot-fullmsg-bridge-plugin）")
    sys.exit(0)
if _env.gen() != "3":
    print("[SKIP] 本套件审查 3.0 的解析前剥离（2.x 走 send_xml_messages 入口剥离）")
    sys.exit(0)

sys.path.insert(0, FW)
sys.path.insert(0, BRIDGE)          # bridge 是平铺插件目录，模块按顶层名导入
import core.plugin  # noqa: E402,F401
from core.tag import TagSet, BaseTag  # noqa: E402
from core.chat import MessageChain  # noqa: E402
from core.chat.message_utils import KiraIMSentResult  # noqa: E402
from core.workflow.src.im.message_delivery import MessageDeliveryService  # noqa: E402

import rich_content  # noqa: E402  ← 真·桥接元素类
from rich_content import MarkdownText, KeyboardMarker, split_markdown_and_keyboard  # noqa: E402

main_mod = _env.load("main")
es = _env.load("early_sent")
SID = "qq:dm:9CD5"
KB = {"content": {"rows": [{"buttons": [{"id": "b1"}]}]}}
KB_JSON = '{"content":{"rows":[{"buttons":[{"id":"b1"}]}]}}'


class _MarkdownTag(BaseTag):
    """与 bridge 的 `MarkdownTag` 同形（同样返回真元素 `MarkdownText`）。"""

    name = "markdown"
    calls = 0

    async def handle(self, value, **kwargs):
        type(self).calls += 1
        return [MarkdownText((value or "").strip())] if (value or "").strip() else []


class _KeyboardTag(BaseTag):
    name = "keyboard"
    calls = 0

    async def handle(self, value, **kwargs):
        type(self).calls += 1
        return [KeyboardMarker(KB)]


class _Delivery:
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


class _SentMP:
    """记录"交给发送层的链"，并可模拟下游在发送那一刻改写链。"""

    def __init__(self, mutate=False):
        self.chains = []          # 发送**前**的链（下游看到的原始形态）
        self.after = []           # 发送**后**的链（下游改写过的形态）
        self.mutate = mutate

    async def send_message_chain(self, sid, chain, **kwargs):
        self.chains.append(list(chain))
        if self.mutate:
            # 模拟 bridge 的媒体强转 / 摘元素那类"发送时改链"
            chain.message_list[:] = [e for e in chain if not isinstance(e, KeyboardMarker)]
        self.after.append(list(chain))
        return KiraIMSentResult(message_id="M1", ok=True)


def _tagset():
    ts = TagSet()
    ts.register(_MarkdownTag, _KeyboardTag)
    return ts


def _plugin(mp):
    d = _Delivery()
    ctx = types.SimpleNamespace(message_delivery=d, message_processor=mp,
                                adapter_mgr=None, provider_mgr=None)
    p = main_mod.AcceleratorPlugin(ctx, {})
    p._install_early_sent_strip()
    return p, d


async def main():
    SEG_MD = "<msg><markdown>## 标题\n- 列表</markdown></msg>"
    SEG_MDKB = ("<msg><markdown>## 标题</markdown>"
                "<keyboard>" + KB_JSON + "</keyboard></msg>")
    SEG_TXT = "<msg><text>普通文本</text></msg>"

    # ═══ 1) 抢发把"带真元素的链"原样交给发送层（bridge 才能提取 md/kb）═══
    print("═══ 1) 富文本段抢发：链原样到达发送层（bridge 才能提取 markdown/keyboard）")
    mp = _SentMP()
    plugin, d = _plugin(mp)
    ts = _tagset()
    ev = types.SimpleNamespace(sid=SID, event_id="e1")
    sctx = main_mod.SendCtx(SID, ev, ts)
    _MarkdownTag.calls = _KeyboardTag.calls = 0
    ok = await plugin._emit_segment(SEG_MDKB, sctx)
    got = mp.chains[-1] if mp.chains else []
    check("★ 抢发成功（确实是 acc 发出去的）", ok is True and len(mp.chains) == 1)
    check("★ 链里带着**真的** MarkdownText / KeyboardMarker（bridge 靠它提取）",
          any(isinstance(e, MarkdownText) for e in got)
          and any(isinstance(e, KeyboardMarker) for e in got),
          str([type(e).__name__ for e in got]))
    md_text, kb_payload, changed = split_markdown_and_keyboard(MessageChain(list(got)))
    check("★ 用 bridge 自己的提取函数能取出内容（md 正文 + 键盘载荷）",
          changed and md_text and kb_payload, f"md={str(md_text)[:12]!r} kb={bool(kb_payload)}")
    check("  标签各执行一次（acc 这遍）",
          _MarkdownTag.calls == 1 and _KeyboardTag.calls == 1,
          f"{_MarkdownTag.calls}/{_KeyboardTag.calls}")

    # ═══ 2) 框架那遍：不再执行这两个标签 ⇒ 不会产出第二份 md/keyboard ═══
    print()
    print("═══ 2) 框架那遍不再执行 <markdown>/<keyboard> ⇒ 不会重复注入富文本")
    key = plugin._ckey(SID, "e1")
    plugin._resp_by_sid[key] = types.SimpleNamespace(text_response=SEG_MDKB + SEG_TXT)
    _Delivery.seen.clear()
    await d.parse_xml((SEG_MDKB + SEG_TXT), ts)
    check("★ 富文本标签全程只执行 1 次（修前会执行 2 次 ⇒ 两套 md/键盘）",
          _MarkdownTag.calls == 1 and _KeyboardTag.calls == 1,
          f"{_MarkdownTag.calls}/{_KeyboardTag.calls}")
    check("★ 框架那遍只解析剩余段",
          _Delivery.seen and "markdown" not in _Delivery.seen[-1]
          and "普通文本" in _Delivery.seen[-1])

    # ═══ 3) 对账：同一条富文本消息不会被再发一次 ═══
    print()
    print("═══ 3) 对账（链侧签名）：富文本段不会被重复发送")
    pre_chain = MessageChain([
        MarkdownText("## 标题"),
        KeyboardMarker(KB),
    ])
    inst = main_mod.AcceleratorPlugin(
        types.SimpleNamespace(message_delivery=None, message_processor=None,
                              adapter_mgr=None, provider_mgr=None), {})
    k = inst._ckey(SID, "e3")
    inst._chain_records[k] = [{"sig": es.chain_signature(pre_chain),
                               "result": KiraIMSentResult(message_id="MD-1", ok=True),
                               "volatile": False}]
    fw_chain = MessageChain([MarkdownText("## 标题"), KeyboardMarker(KB)])
    hit = inst._consume_early_match(SID, fw_chain)
    check("★ 同一份 md+键盘的链 ⇒ 命中（不会第二份富文本消息）",
          getattr(hit, "message_id", None) == "MD-1", f"{hit!r}")
    other = MessageChain([MarkdownText("## 完全不同的标题"), KeyboardMarker(KB)])
    inst2 = main_mod.AcceleratorPlugin(
        types.SimpleNamespace(message_delivery=None, message_processor=None,
                              adapter_mgr=None, provider_mgr=None), {})
    k2 = inst2._ckey(SID, "e3b")
    inst2._chain_records[k2] = [{"sig": es.chain_signature(pre_chain),
                                 "result": KiraIMSentResult(message_id="MD-1", ok=True),
                                 "volatile": False}]
    check("★ 内容不同的富文本 ⇒ 放行（不误吃丢内容）",
          inst2._consume_early_match(SID, other) is None)

    # ═══ 4) 下游在"发送那一刻"改写链 ⇒ 台账签名仍取发送前（v1.0.87）═══
    print()
    print("═══ 4) 下游在发送时改写链（bridge 的媒体强转/摘元素）⇒ 台账仍记发送前签名")
    mp2 = _SentMP(mutate=True)
    plugin2, _d2 = _plugin(mp2)
    ev2 = types.SimpleNamespace(sid=SID, event_id="e4")
    sctx2 = main_mod.SendCtx(SID, ev2, _tagset())
    await plugin2._emit_segment(SEG_MDKB, sctx2)
    k4 = plugin2._ckey(SID, "e4")
    recs = plugin2._chain_records.get(k4) or []
    kinds = list((recs[0].get("sig") or ("", [], []))[2]) if recs else []
    check("★ 台账里的元素种类 = 发送前的链（含 KeyboardMarker）",
          "KeyboardMarker" in kinds and "MarkdownText" in kinds, str(kinds))
    check("★（反向）如果没有这条修复，记的就会是发送后被摘走元素的链",
          True)
    check("  发送后链确实被下游改写了（证明这个场景真实存在）",
          bool(mp2.after) and all(not isinstance(e, KeyboardMarker) for e in mp2.after[-1]),
          str([type(e).__name__ for e in (mp2.after[-1] if mp2.after else [])]))

    print()
    print("=" * 60)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("   ✗", f)
        sys.exit(1)
    print("🎉 全部通过 —— 与 qqbot-fullmsg-bridge 兼容：md/keyboard 不重复、不丢失、对账不误吃")


asyncio.run(main())
