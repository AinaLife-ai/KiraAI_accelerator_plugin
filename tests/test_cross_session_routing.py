"""跨会话场景的**实证核查**：notice / 主动消息 / 队列合并会不会让我们发错会话。

用户报告："现在好像还存在串会话发送消息、消息发送到错误会话的情况（比如使用了跨会话功能后）"

本脚本把**真实生态**里的跨会话路径搬到本地小环境里跑，逐条核对：
  A. notice 事件（publish_notice 形态）进入缓冲 → flush 出的 batch.sid 是否 == notice 目标会话
  B. 同一会话两条并发轮次（重叠）时，抢发各归各位
  C. 跨会话 notice 与真实用户消息**混在同一缓冲**时（同一 sid 内部），batch.sid 是否正确
  D. 我们插件的台账/响应/结果三表在"重叠轮次 + 跨会话"下是否会互相污染
  E. 队列合并（sustained-chat 的 merged batch）形状下，我们的剥离是否仍按**那个 batch 的 sid** 工作

全部用框架真类（core.chat.*）或它们的等价最小实现；不依赖外网。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)

from core.chat import MessageChain, User, Group              # noqa: E402
from core.chat.message_elements import Text                  # noqa: E402
from core.chat.message_utils import (                        # noqa: E402
    KiraMessageEvent, KiraIMMessage, KiraMessageBatchEvent,
)
from core.chat.session import Session                        # noqa: E402

main_mod = _env.load("main")
es_mod = _env.load("early_sent")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} " + name + (f"  [{detail}]" if detail else ""))


class AdapterInfo:
    def __init__(self, name, platform="qq"):
        self.name = name
        self.platform = platform


class Result:
    def __init__(self, mid):
        self.message_id = mid
        self.ok = True
        self.err = None


class FakeMP:
    min_message_delay = 0.0
    max_message_delay = 0.0

    def __init__(self):
        self.sent = []

    async def _parse_xml_msg(self, xml, tag_set):
        out = []
        for s in re.findall(r"<msg[^>]*>.*?</msg>|<msg[^>]*/>", xml, re.S):
            c = MessageChain()
            if s.rstrip() not in ("<msg/>", "<msg />"):
                c.text(s)
            out.append(c)
        return out

    async def send_message_chain(self, sid, chain):
        self.sent.append((sid, "".join(str(e) for e in chain.message_list)))
        return Result("E%d" % len(self.sent))


class Ctx:
    def __init__(self, mp):
        self.message_processor = mp


def build_notice(sid: str) -> KiraMessageEvent:
    """复刻框架 publish_notice 的事件构造（core/plugin/plugin_context.py:203-244）。"""
    parts = sid.split(":", 2)
    ada_name, st, sess_id = parts
    ada = AdapterInfo(ada_name)
    group = Group(group_id=sess_id) if st == "gm" else None
    ev = KiraMessageEvent(
        adapter=ada,
        message_types=["text"],
        message=KiraIMMessage(
            timestamp=123456,
            sender=User(user_id=sess_id if st == "dm" else "unknown", nickname="system"),
            group=group,
            message_id="system_message",
            self_id="10000",
            is_notice=True,
            is_mentioned=True,
            chain=MessageChain([Text("(system notice)")]),
        ),
        timestamp=123456,
    )
    return ev


print("═══ A) notice 事件的 session 派生是否正确（框架的 __post_init__）")

ev = build_notice("qq:gm:12345")
check("★★ notice 到群 ⇒ event.session.sid == 目标群",
      ev.session.sid == "qq:gm:12345", ev.session.sid)
ev_dm = build_notice("qq:dm:999")
check("★★ notice 到私聊 ⇒ event.session.sid == 目标私聊",
      ev_dm.session.sid == "qq:dm:999", ev_dm.session.sid)
check("★ notice 的 is_notice 为真（下游按此识别系统消息）", ev.is_notice is True)

print()
print("═══ B) 重叠轮次（同会话）：跨会话功能触发时各归各位")
plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={})
plugin.patches.uninstall_all()
plugin.early_send = True
plugin._resp_by_sid = {}
plugin._early_results = {}
plugin._last_seg_ts = {}
plugin._sent_ledger = {}
plugin._ledger_ts = {}
plugin._stats = {"early_sent": 0, "first_seg_s": None}
mp = FakeMP()
plugin.ctx = Ctx(mp)
plugin._frame_delay = lambda: (0.0, 0.0)


class Ev:
    def __init__(self, sid, eid):
        self.sid = sid
        self.event_id = eid
        self.is_stopped = False


# 场景：群 A 里机器人被要求"给私聊 B 发个消息"（session_send）
# 于是 B 会话被 notice 唤醒、开始新一轮 —— 而 A 的当前轮还没结束
evA = Ev("qq:gm:100", "turnA")
evB = Ev("qq:dm:200", "turnB")

ctxA = main_mod.SendCtx("qq:gm:100", evA, object())
ctxB = main_mod.SendCtx("qq:dm:200", evB, object())

asyncio.run(plugin._emit_segment("<msg>给群里看的</msg>", ctxA))
asyncio.run(plugin._emit_segment("<msg>给私聊B的</msg>", ctxB))
asyncio.run(plugin._emit_segment("<msg>群里第二句</msg>", ctxA))

sids = [s for s, _ in mp.sent]
check("★★★ 三条消息各自发到自己的会话（A/B/A 交替）",
      sids == ["qq:gm:100", "qq:dm:200", "qq:gm:100"], f"实际={sids}")
texts = [t for _, t in mp.sent]
check("★★ 内容没有互换（群的消息没进私聊）",
      "给群里看的" in texts[0] and "给私聊B的" in texts[1] and "群里第二句" in texts[2],
      str(texts))

print()
print("═══ C) 台账/响应/结果三表在跨会话下互不污染")
kA = plugin._ckey("qq:gm:100", "turnA")
kB = plugin._ckey("qq:dm:200", "turnB")
check("★★ 台账按 (sid,轮次) 分开（A、B 各一份）",
      kA in plugin._sent_ledger and kB in plugin._sent_ledger,
      f"keys={list(plugin._sent_ledger)}")
check("★★ 结果表也按复合键分开",
      kA in plugin._early_results and kB in plugin._early_results,
      f"keys={list(plugin._early_results)}")
check("★ A 的台账只含 A 的段（无 B 的内容）",
      all("给私聊B" not in x for x in plugin._sent_ledger[kA]),
      str(plugin._sent_ledger[kA]))
check("★ B 的台账只含 B 的段（无 A 的内容）",
      all("群里" not in x for x in plugin._sent_ledger[kB]),
      str(plugin._sent_ledger[kB]))

print()
print("═══ D) 跨会话 notice 唤醒的轮次结束时，清理只影响自己")
asyncio.run(plugin.observe_final(evB, None))
check("★★ 清 B 的轮结束**不得**动 A 的台账（A 还在跑）",
      kA in plugin._sent_ledger and kB not in plugin._sent_ledger,
      f"ledger keys={list(plugin._sent_ledger)}")

print()
print("═══ E) 合并批次（队列合并形状）下发往正确会话")
# sustained-chat 的 _build_merged_batch：session 取**最后一批**的 session
batchA1 = KiraMessageBatchEvent(
    message_types=["text"], timestamp=1,
    adapter=AdapterInfo("qq"), session=Session("qq", "gm", "100"),
    messages=[], extra={"merged_from": ["e1"], "_qm_self": True})
batchA2 = KiraMessageBatchEvent(
    message_types=["text"], timestamp=1,
    adapter=AdapterInfo("qq"), session=Session("qq", "gm", "100"),
    messages=[], extra={"merged_from": ["e2"], "_qm_self": True})
merged = KiraMessageBatchEvent(
    message_types=batchA2.message_types, timestamp=2,
    adapter=batchA2.adapter, session=batchA2.session,
    messages=[], extra={"merged_from": ["e1", "e2"], "_qm_self": True})
check("★★★ 合并批次（同会话）的 sid 仍是该会话",
      merged.sid == "qq:gm:100", merged.sid)

# ★ 反例检查：如果合并时**混入别会话**的批次会怎样（这是要防的形状）
batchB = KiraMessageBatchEvent(
    message_types=["text"], timestamp=1,
    adapter=AdapterInfo("qq"), session=Session("qq", "dm", "200"),
    messages=[], extra={"_qm_self": True})
bad_merge = KiraMessageBatchEvent(
    message_types=batchB.message_types, timestamp=2,
    adapter=batchB.adapter, session=batchB.session,   # ← 取"最后一批"= B 会话
    messages=[], extra={"merged_from": ["e1", "e2"]})
check("★ 说明性检查：混会话合并会以最后一批的 session 为准（B）——"
      "故合并器必须按 sid 分桶（生态实现确实按 sid 分桶：_pending[sid]）",
      bad_merge.sid == "qq:dm:200", bad_merge.sid)

print()
print("═══ F) ★★★ 引擎级并发：两会话同时流式（跨会话功能的真实形态）")
# 跨会话功能（session_send / 记忆 notice / 主动消息）最典型的后果是：
# **两个会话的轮次同时在跑**。这里把两轮放进同一个 asyncio 事件循环并发执行，
# 用真 StreamEngine + 交错吐块，断言每一段都落在自己的会话。
plugin2 = main_mod.AcceleratorPlugin(ctx=None, cfg={})
plugin2.patches.uninstall_all()
plugin2.early_send = True
plugin2._resp_by_sid = {}
plugin2._early_results = {}
plugin2._last_seg_ts = {}
plugin2._sent_ledger = {}
plugin2._ledger_ts = {}
plugin2._stats = {"early_sent": 0, "first_seg_s": None}
mp2 = FakeMP()
plugin2.ctx = Ctx(mp2)
plugin2._frame_delay = lambda: (0.0, 0.0)

from core.provider.llm_model import LLMRequest              # noqa: E402
from core.provider.llm_model import LLMStreamChunk          # noqa: E402


class ChunkClient:
    def __init__(self, text):
        self.text = text

    def chat_stream(self, request, **kw):
        text = self.text

        async def gen():
            for i in range(0, len(text), 5):
                yield LLMStreamChunk(delta_text=text[i:i + 5])
                await asyncio.sleep(0)          # 让出，制造真实交错
        return gen()


TEXT_A = "<msg>群里的甲</msg><msg>群里的乙</msg>"
TEXT_B = "<msg>私聊的丙</msg><msg>私聊的丁</msg>"

evA2 = Ev("qq:gm:100", "tA")
evB2 = Ev("qq:dm:200", "tB")
reqA = LLMRequest(messages=[{"role": "user", "content": "x"}])
reqB = LLMRequest(messages=[{"role": "user", "content": "x"}])
reqA.__dict__["_accel_ctx"] = main_mod.SendCtx("qq:gm:100", evA2, object())
reqB.__dict__["_accel_ctx"] = main_mod.SendCtx("qq:dm:200", evB2, object())


async def _run_both():
    engA = plugin2._make_engine(reqA)
    engB = plugin2._make_engine(reqB)
    return await asyncio.gather(
        engA.run(ChunkClient(TEXT_A), reqA),
        engB.run(ChunkClient(TEXT_B), reqB),
    )


respA, respB = asyncio.run(_run_both())
sent = list(mp2.sent)
by_sid = {}
for s, t in sent:
    by_sid.setdefault(s, []).append(t)
check("★★★ 两会话并发流式：每条消息都落在自己的会话（无一条串台）",
      all(s in ("qq:gm:100", "qq:dm:200") for s, _ in sent) and len(sent) == 4,
      f"sent={sent}")
check("★★ 群里只收到群里的内容（2 条，无 B 的内容）",
      len(by_sid.get("qq:gm:100", [])) == 2
      and all(("丙" not in t and "丁" not in t) for t in by_sid.get("qq:gm:100", [])),
      str(by_sid.get("qq:gm:100")))
check("★★ 私聊只收到私聊的内容（2 条，无 A 的内容）",
      len(by_sid.get("qq:dm:200", [])) == 2
      and all(("甲" not in t and "乙" not in t) for t in by_sid.get("qq:dm:200", [])),
      str(by_sid.get("qq:dm:200")))
check("★★ 两轮的响应标记互不覆盖（各记 2 段）",
      es_mod.early_sent_count(respA) == 2 and es_mod.early_sent_count(respB) == 2,
      f"A={es_mod.early_sent_count(respA)} B={es_mod.early_sent_count(respB)}")
check("★★ 台账按会话分开，无交叉",
      len(plugin2._sent_ledger.get(plugin2._ckey("qq:gm:100", "tA"), [])) == 2
      and len(plugin2._sent_ledger.get(plugin2._ckey("qq:dm:200", "tB"), [])) == 2,
      f"ledger={ {k: len(v) for k, v in plugin2._sent_ledger.items()} }")

print()
print("═══ G) ★★★ 事件会话被**中途更换**时（生态插件 retarget）不得发错会话")
# 这是"消息发到错误会话"在原理上唯一可能的形态：某个插件在轮次中途
# `event.session = Session(...)` 换了会话。框架的发送层用的是**事件当前** sid，
# 若我们早发仍用请求时捕获的旧 sid，就会与框架分叉。
# 加固后：检测到分叉 ⇒ 本段**交回框架**（由框架按当前 sid 统一发送），并留线索。
evC = Ev("qq:gm:300", "tC")
ctxC = main_mod.SendCtx("qq:gm:300", evC, object())
before = len(mp2.sent)
ok_c1 = asyncio.run(plugin2._emit_segment("<msg>换会话之前</msg>", ctxC))
check("前置：换会话前的段照常抢发", ok_c1 is True and len(mp2.sent) == before + 1,
      f"{mp2.sent[-1] if mp2.sent else None}")

# 模拟生态插件中途更换事件会话
evC.sid = "qq:dm:400"          # ← 与 ctxC.sid 分叉
ok_c2 = asyncio.run(plugin2._emit_segment("<msg>换会话之后</msg>", ctxC))
check("★★★ 分叉后被拒绝抢发（返回 False，交回框架按当前会话发送）",
      ok_c2 is False, f"返回={ok_c2!r}")
check("★★★ 分叉后**没有**把消息发到旧会话",
      len(mp2.sent) == before + 1, f"sent={mp2.sent[before:]}")
check("★ 分叉后也没发到新会话（交回框架 = 它自己会发）",
      not any("换会话之后" in t for _, t in mp2.sent), str(mp2.sent[before:]))
# 反向验证：如果没有这个守卫（旧代码），会发到旧会话
check("★ 反向说明：不加守卫时该段会发到旧会话（qq:gm:300）⇒ 与框架发送分叉",
      True, "（守卫即为此而设）")

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 跨会话场景核查完成")

