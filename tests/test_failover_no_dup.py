"""★★★ 模型故障转移（failover）会不会让同一段抢发**两次**？

## 背景（读框架源码发现的发送向量）

`core/agent/agent_executor.py` 的模型组循环：

    for model_idx, model in enumerate(model_group):
        try:
            llm_resp = await model.chat(request)      # ← 同一个 request 对象！
            break
        except (APIStatusError, APITimeoutError, APIConnectionError, ProviderAPIError):
            if model_idx < len(model_group) - 1:
                continue                              # ← 换下一个模型**重试**

而我们的引擎 patch 在 `ProviderManager.get_model_client` 上 ——
**每个模型都是被包装过的 proxy** ⇒ `model.chat(request)` 每一次都会
新建一个 `StreamEngine` 并跑一遍。

⇒ 若第 1 个模型**流到一半才失败**（已经抢发出了几段，不可撤销），
   第 2 个模型拿到**同一个 request**、重新生成、我们的引擎**再抢发一遍**
   ⇒ 用户会看到开头那几段**重复**。

## 与"多步 loop"的区别（不能用"试过一次就不再抢发"来修）

多步 loop 里 **step 2 也用同一个 request** —— 那是**设计如此**：
每一步的文本都是新生成的、必须抢发。
区分两者的**唯一可靠信号**是：
  · 多步：上一次调用**成功返回**过 → 正常抢发
  · 重试：上一次调用**抛异常** → 可能已部分发送 → 本次**停止抢发**

## 本测试

  1. 真 `_make_engine` + 真引擎：模型 A 吐出 2 段后抛错
  2. 模型 B（同一个 request）把**同样的文本**再吐一遍
  3. 断言：第 2 次尝试**不得**再抢发（否则同一段出现两次）
  4. 反向验证：多步场景（上一调用成功）**必须继续抢发**（别把功能一起关掉）
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

from core.chat import MessageChain                              # noqa: E402
from core.provider.llm_model import LLMRequest, LLMStreamChunk  # noqa: E402

main_mod = _env.load("main")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} " + name + (f"  [{detail}]" if detail else ""))


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
        self.sent.append("".join(str(e) for e in chain.message_list))
        return Result("E%d" % len(self.sent))


class Ctx:
    def __init__(self, mp):
        self.message_processor = mp


class Ev:
    def __init__(self, eid="ev-failover"):
        self.sid = "qq:gm:999"
        self.event_id = eid
        self.is_stopped = False


TEXT = "<msg>第一段</msg><msg>第二段</msg><msg>第三段</msg>"


class FlakyClient:
    """吐完前 2 段后抛错 —— 模拟"流到一半网络断了"。"""

    def __init__(self, fail_after=None):
        self.fail_after = fail_after

    def chat_stream(self, request, **kw):
        text = TEXT
        fail_after = self.fail_after

        async def gen():
            for i in range(0, len(text), 5):
                piece = text[i:i + 5]
                # 以"完整段数"计：到 2 段后抛
                if fail_after is not None and text[:i].count("</msg>") >= fail_after:
                    raise ConnectionError("stream broke")
                yield LLMStreamChunk(delta_text=piece)
        return gen()


def build():
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
    return plugin, mp


print("═══ 1) 故障转移：第 1 个模型半途失败 ⇒ 第 2 个模型不得把已发段再发一遍")
plugin, mp = build()
ev = Ev()
req = LLMRequest(messages=[{"role": "user", "content": "x"}])
req.__dict__["_accel_ctx"] = main_mod.SendCtx("qq:gm:999", ev, object())

# ── 第 1 次尝试（模型 A）：吐 2 段后失败 ──
engA = plugin._make_engine(req)
failed = None
try:
    asyncio.run(engA.run(FlakyClient(fail_after=2), req))
except Exception as e:  # noqa: BLE001
    failed = type(e).__name__
sent_after_first = list(mp.sent)
check("前置：第 1 次尝试确实抛错（流到一半断了）", failed == "ConnectionError", f"{failed}")
check("前置：第 1 次已抢发出去 ≥1 段（不可撤销）",
      len(sent_after_first) >= 1, f"已发={sent_after_first}")

# ── 第 2 次尝试（模型 B）：同一个 request，完整重发 ──
engB = plugin._make_engine(req)
respB = asyncio.run(engB.run(FlakyClient(fail_after=None), req))
sent_after_retry = list(mp.sent)

dups = [s for s in sent_after_retry if sent_after_retry.count(s) > 1]
check("★★★ 重试后**没有段被发两次**（故障转移不得造成重复）",
      not dups, f"重复={dups} 全部={sent_after_retry}")

# ★ 内容完整性：抑制的是"**我们**的抢发"，不是内容 ——
#   重试的完整文本仍在响应里，发送层会按台账剥掉已发段、框架补发剩余 ⇒ 不丢。
es_mod = _env.load("early_sent")
full_text = respB.text_response
check("★★ 重试响应里带着**完整**文本（框架据此补齐剩余段）",
      full_text.count("</msg>") == 3, f"段数={full_text.count('</msg>')}")
_ledger_key = plugin._ckey("qq:gm:999", ev.event_id)
ledger = plugin._sent_ledger.get(_ledger_key, [])
check("★★ 台账记录着「已发过哪两段」（供发送层剥离）", len(ledger) == 2, str(ledger))
rest = es_mod.strip_early_sent_smart(full_text, len(ledger), ledger)
check("★★ 剥离后**只剩第 3 段**交给框架 ⇒ 不重不漏",
      rest.count("</msg>") == 1 and "第三段" in rest, f"剩余={rest!r}")

print()
print("═══ 2) 反向验证：多步 loop（上一次成功）**必须**继续抢发")
plugin2, mp2 = build()
ev2 = Ev("ev-multistep")
req2 = LLMRequest(messages=[{"role": "user", "content": "x"}])
req2.__dict__["_accel_ctx"] = main_mod.SendCtx("qq:gm:999", ev2, object())

STEP1 = "<msg>第一步的甲</msg><msg>第一步的乙</msg>"
STEP2 = "<msg>第二步的丙</msg>"


class StepClient:
    def __init__(self, text):
        self.text = text

    def chat_stream(self, request, **kw):
        text = self.text

        async def gen():
            for i in range(0, len(text), 5):
                yield LLMStreamChunk(delta_text=text[i:i + 5])
        return gen()


r1 = asyncio.run(plugin2._make_engine(req2).run(StepClient(STEP1), req2))
check("★ 第 1 步抢发成功（2 段）", len(mp2.sent) == 2, str(mp2.sent))
r2 = asyncio.run(plugin2._make_engine(req2).run(StepClient(STEP2), req2))
check("★★★ 第 2 步**仍然抢发**（多步功能不能被误关）",
      any("第二步的丙" in s for s in mp2.sent), str(mp2.sent))
check("★★ 两步的段各发一次、无重复",
      len(mp2.sent) == 3 and len(set(mp2.sent)) == 3, str(mp2.sent))

print()
print("═" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 故障转移不重复、多步抢发不误关")
