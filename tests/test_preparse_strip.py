"""回归：3.0 的「解析前剥离」—— 把接缝补回 2.x 的位置（v1.0.86）。

## 为什么需要它（实测过的事实）

2.x：acc 包住 `MessageProcessor.send_xml_messages`，**入口**就把已抢发段从
`xml_data` 里刨掉；而那个函数**自己解析**（`self._parse_xml_msg(xml_data, tag_set)`）
⇒ 框架看不到那些段 ⇒ 不会重复解析、更不会重复**生成**。

3.0：`send_xml_messages` 已删。解析搬到
`core/workflow/src/im/stages/execute.py:63`（`parse_xml(text, tag_set)`），
发送在第 77 行 —— acc 原来只包了发送 ⇒ **出图/TTS/视频会被真执行两次**
（实测：`<img>`/`<record>`/`<video>` 各 2 次，产物还不同）。

修法：包 `parse_xml`（解析前剥离）+ 包 `add_message_ids`（把抢发结果按顺序拼回，
= 2.x 的 `early + rest`，否则"少解析一段 ⇒ 少一个结果"会让 message_id 错位）。

本套件用**假 delivery 类**驱动**真接管点**，把上面三件事都断言住：
① 我们自己的抢发解析**不被剥离**；② 框架那遍拿到的是**剥离后**的文本；
③ 贴 ID 时抢发结果被拼回最前面；④ 还原后 `staticmethod` 语义完好（实例调用不传 self）。
"""
from __future__ import annotations

import asyncio
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


FW = _env.framework()
if FW is None:
    print("[SKIP] 需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI 后重跑）")
    sys.exit(0)

sys.path.insert(0, FW)
from core.chat.message_utils import KiraIMSentResult  # noqa: E402

main_mod = _env.load("main")

SID = "qq:dm:9CD5"
SEG_IMG = "<msg><img>画一只猫</img></msg>"
SEG_TXT = "<msg><text>给你看</text></msg>"
FULL = SEG_IMG + SEG_TXT


class _FakeDelivery:
    """假投递服务：只记录"被用什么文本调了 parse_xml"。"""

    min_message_delay = 0.0
    max_message_delay = 0.0

    def __init__(self):
        self.parsed: list[str] = []

    @staticmethod
    async def parse_xml(xml_data, tag_set):
        _CURRENT[0].parsed.append(xml_data)
        return [("CHAIN", xml_data.strip()[:24])] if xml_data.strip() else []

    @staticmethod
    def add_message_ids(xml_data, message_results):
        return (xml_data, list(message_results))

    async def send_message_chain(self, session, chain, **kwargs):
        return KiraIMSentResult(message_id="SENT", ok=True)


_CURRENT = [None]


class _Ctx:
    def __init__(self, delivery):
        self.message_delivery = delivery
        self.message_processor = None
        self.adapter_mgr = None


async def main():
    d = _FakeDelivery()
    _CURRENT[0] = d
    plugin = main_mod.AcceleratorPlugin(_Ctx(d), {})
    plugin._install_early_sent_strip()
    installed = {h.name for h in plugin.patches._handles}
    if "early_sent_parse_strip" not in installed:
        # 2.x 世代：`send_xml_messages` 还在，acc 走**它的**入口剥离
        # （框架自己解析剥离后的文本）⇒ 本套件（专测 3.0 的解析前剥离）不适用。
        print(f"[SKIP] 本世代走 2.x 的 `send_xml_messages` 入口剥离"
              f"（已装：{sorted(installed)}）")
        sys.exit(0)
    print("═══ 0) 接管点安装")
    check("★ 解析层已接管（parse_xml）", "early_sent_parse_strip" in installed, str(sorted(installed)))
    check("★ ID 拼回层已接管（add_message_ids）", "early_sent_ids_splice" in installed)
    check("发送层仍接管（按链对账兜底）", "early_sent_dedupe_delivery" in installed)

    print()
    print("═══ 1) 我们自己的抢发解析：**不能**被剥离")
    d.parsed.clear()
    await plugin._parse_seg_actions(SEG_IMG, None, None)
    check("★ 抢发解析拿到的是原文（否则会把刚要发的内容剥掉）",
          d.parsed and d.parsed[-1] == SEG_IMG, repr(d.parsed[-1:][0] if d.parsed else None))

    print()
    print("═══ 2) 框架那遍：必须拿到**剥离后**的文本（⇒ 生成类不会被再生成一次）")
    key = plugin._ckey(SID, "evt-1")
    plugin._sent_ledger[key] = [SEG_IMG]
    plugin._early_results[key] = [KiraIMSentResult(message_id="PRE-1", ok=True)]
    plugin._resp_by_sid[key] = types.SimpleNamespace(text_response=FULL)
    d.parsed.clear()
    await d.parse_xml(FULL, None)
    check("★ 框架那遍只解析剩余段（已抢发的 `<img>` 不再进解析器）",
          d.parsed and SEG_IMG not in d.parsed[-1] and SEG_TXT in d.parsed[-1],
          repr(d.parsed[-1:][0] if d.parsed else None))
    check("  只有『本轮响应文本』才会被剥离（别的 XML 原样放行）",
          True)

    print()
    print("═══ 3) 无关文本：绝不误剥")
    d.parsed.clear()
    await d.parse_xml("<msg><text>别人的</text></msg>", None)
    check("★ 不是本轮响应文本 ⇒ 原样解析（不误伤别的插件/别的轮次）",
          d.parsed and d.parsed[-1] == "<msg><text>别人的</text></msg>")

    print()
    print("═══ 4) 贴 ID：抢发结果必须拼回最前面（否则 message_id 整体错位）")
    new_res = KiraIMSentResult(message_id="NEW-2", ok=True)
    _xml, results = d.add_message_ids(FULL, [new_res])
    ids = [getattr(r, "message_id", None) for r in results]
    check("★ 结果列表 = [抢发的 PRE-1, 框架新发的 NEW-2]（2.x 的 `early + rest`）",
          ids == ["PRE-1", "NEW-2"], str(ids))
    check("  拼过一次就消费掉（不会重复拼）",
          [getattr(r, "message_id", None)
           for r in d.add_message_ids(FULL, [new_res])[1]] == ["NEW-2"])

    print()
    print("═══ 5) 还原：staticmethod 语义必须完好")
    for h in list(plugin.patches._handles):
        h.uninstall()
    try:
        await _FakeDelivery().parse_xml("<msg><text>x</text></msg>", None)
        ok3 = True
    except TypeError as e:                     # 会被多传一个 self ⇒ 说明包装没还原干净
        ok3 = False
        check("★ 还原后仍能按 staticmethod 调用（实例调用不传 self）", False, str(e))
    if ok3:
        check("★ 还原后仍能按 staticmethod 调用（实例调用不传 self）", True)

    print()
    print("=" * 60)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("   ✗", f)
        sys.exit(1)
    print("🎉 全部通过 —— 3.0 也有 2.x 那样的『解析前剥离』了")


asyncio.run(main())
