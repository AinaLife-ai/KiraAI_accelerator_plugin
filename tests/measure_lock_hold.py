"""量：框架在【会话锁内】占多久（决定同会话新消息要等多久）。

背景（2026-09-23 拆解 Alife 时发现）：
  core/message_manager.py:765  `async with session_lock:`
  core/message_manager.py:766      `await self.send_xml_messages(...)`
  而 send_xml_messages 的循环里（:930）**每段之后无条件 sleep**：
      await asyncio.sleep(random.uniform(min_message_delay, max_message_delay))
  ⇒ 一段 N 段的回复，锁被占 ≈ N × 平均间隔。

  Alife 不这么干：它的逐句推送不在会话级锁里，而且新消息会直接
  Cancel 掉上一次生成（ChatBot.cs:99）。

本脚本对比"没有抢先发送"与"有抢先发送"时，框架这段循环各自要跑多久。
"""
import asyncio
import os
import random
import re
import sys
import time
from pathlib import Path

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)

from core.chat import MessageChain      # noqa: E402
es = _env.load("early_sent")


class FakeMP:
    """复刻 send_xml_messages 的骨架：解析 → 逐段发 → 每段后 sleep。"""

    def __init__(self, lo, hi):
        self.min_message_delay, self.max_message_delay = lo, hi
        self.sent = []

    async def _parse_xml_msg(self, xml, tag_set):
        segs = re.findall(r"<msg[^>]*>.*?</msg>|<msg[^>]*/>", xml, re.S)
        out = []
        for s in segs:
            c = MessageChain()
            if s.rstrip() not in ("<msg/>", "<msg />"):
                c.text(s)
            out.append(c)
        return out

    async def send_message_chain(self, sid, chain):
        self.sent.append(time.monotonic())
        return {"ok": True}


async def lock_hold(text, count_early, lo=0.8, hi=1.5):
    mp = FakeMP(lo, hi)
    xml = es.strip_early_sent(text, count_early) if count_early else text
    t0 = time.monotonic()
    actions = await mp._parse_xml_msg(xml, None)
    for a in actions:
        if isinstance(a, MessageChain) and not a.is_empty():
            await mp.send_message_chain("s", a)
        await asyncio.sleep(random.uniform(lo, hi))     # ← 框架原文，无条件
    return time.monotonic() - t0, len(actions), xml[:26]


async def main():
    reply = "".join("<msg>这是第%d段回复内容</msg>" % i for i in range(1, 6))
    print("一段 5 段的回复，框架在【会话锁内】占多久：\n")

    for lo, hi, label in ((0.8, 1.5, "默认 0.8~1.5s"), (2.0, 5.0, "用户配的 2~5s")):
        d0, n0, _ = await lock_hold(reply, 0, lo, hi)
        d1, n1, x = await lock_hold(reply, 5, lo, hi)
        print(f"  {label}：")
        print(f"    没有抢先发送        {d0:5.2f}s  （框架发 {n0} 段 ⇒ 睡 {n0} 次）")
        print(f"    有抢先发送（全发完）  {d1:5.2f}s  （框架只发 {n1} 段占位 {x}）")
        print(f"    ⇒ 锁少占 {d0 - d1:.2f}s（{(1 - d1 / d0) * 100:.0f}%）\n")


asyncio.run(main())
