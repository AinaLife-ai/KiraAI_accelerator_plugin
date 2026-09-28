"""★★★ 跨轮清理安全：轮结束清理**绝不能**清掉仍在跑的另一轮（审计 P1，真重复）。

## 现场（2026-09-28 审计发现并实测复现）

框架会**重叠处理同一个会话** —— 用户自己提供的线上日志就是：

    15:35:33 [llm] Running agent using ...      ← 第 2 轮的 llm_request 先到
    15:35:37 [accel] 一轮结束 sid=... steps=1   ← 第 1 轮这时才结束

而 `_clear_round_state` 原来按 **sid 前缀**清掉该会话**所有轮次**的台账：

    _pre = sid + "\x00"
    for _d in (self._sent_ledger, self._resp_by_sid):
        for _x in [... if _y == sid or _y.startswith(_pre)]:
            _d.pop(_x, None)

于是这个时序必然重复发送：

    第 1 轮抢发 A          ⇒ 台账[(sid, turn-1)] = [A]
    第 2 轮（短回复）先跑完 ⇒ observe_final ⇒ 把**第 1 轮**的台账一起清掉
    第 1 轮这才走到 send_xml_messages ⇒ n=0 ⇒ 不剥离
    ⇒ **框架把 A 再发一次** ✗

## 修法

`_clear_round_state(sid, event_id)` **只清本轮**（复合键），与 `_reset_turn_ledger` 对齐；
"残留到下一轮"改由"每轮开始前清本轮 + >600s 陈旧窗口"两处覆盖。

## 本套件

  A. 真跑：重叠两轮 —— 清第 2 轮不得影响第 1 轮，且第 1 轮仍能正确剥离
  B. 对照 / 反向验证（本轮结束时该清；没抢发时框架本就该发）
  C. 静默场景：不带 event_id 时必须保守
  D. 静态判据：不得再有"按 sid 前缀全清"这种更粗的动作
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
from core.message_manager import MessageProcessor               # noqa: E402

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
    """站在 MessageProcessor 的位置（只实现被用到的两个方法）。"""

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


class Ev:
    """一个"轮次"事件：sid 相同、event_id 不同（框架里 event 每轮新建）。"""

    def __init__(self, event_id):
        self.sid = "test:gm:1"
        self.is_stopped = False
        self.event_id = event_id


class Ctx:
    def __init__(self, mp):
        self.message_processor = mp


A = "<msg><text>第一段</text></msg>"

# ── 搭台：把**框架的** send_xml_messages 换成桩，再让插件去包它 ──
framework_saw: list = []


async def stub_send_xml_messages(self, event, xml_data, tag_set):
    """占位"框架自己的发送实现"：记下它到底拿到了什么文本。"""
    framework_saw.append(xml_data)
    return []


_LIVE = []


def build_plugin():
    while _LIVE:
        try:
            _LIVE.pop().patches.uninstall_all()
        except Exception:  # noqa: BLE001
            pass
    # 每次重建：先把桩复位（清掉上一次的包装标记），再让新插件装补丁
    MessageProcessor.send_xml_messages = stub_send_xml_messages
    try:
        delattr(stub_send_xml_messages, "__kira_accel__")
    except Exception:  # noqa: BLE001
        pass
    plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    plugin.early_send = True
    plugin._resp_by_sid = {}
    plugin._early_results = {}
    plugin._last_seg_ts = {}
    plugin._sent_ledger = {}
    plugin._ledger_ts = {}
    plugin._stats = {"early_sent": 0, "first_seg_s": None, "turns": 0}
    mp = FakeMP()
    plugin.ctx = Ctx(mp)
    plugin._frame_delay = lambda: (0.0, 0.0)
    plugin._install_early_sent_strip()
    _LIVE.append(plugin)
    return plugin, mp


def step_send(plugin, ev, text):
    inst = object.__new__(MessageProcessor)
    return asyncio.run(MessageProcessor.send_xml_messages(inst, ev, text, object()))


def clear_saw():
    framework_saw.clear()


print("═══ 1) ★★★ 重叠两轮：第 2 轮的轮结束清理**不得**动第 1 轮的台账")
plugin, mp = build_plugin()
ev1 = Ev("turn-1")
ev2 = Ev("turn-2")

# 第 1 轮抢发 A（真 _emit_segment）
ctx1 = main_mod.SendCtx("test:gm:1", ev1, object())
ok = asyncio.run(plugin._emit_segment(A, ctx1))
k1 = plugin._ckey("test:gm:1", "turn-1")
check("前置：第 1 轮抢发成功且台账已记", ok is True and plugin._sent_ledger.get(k1) == [A],
      f"台账={plugin._sent_ledger}")

# 第 2 轮（短回复）整轮跑完 ⇒ observe_final 触发清理
asyncio.run(plugin.observe_final(ev2, None))
check("★★★ 第 2 轮的清理**没有清掉**第 1 轮的台账（原按 sid 前缀清 ⇒ 必被清）",
      plugin._sent_ledger.get(k1) == [A], f"台账={plugin._sent_ledger}")

# 第 1 轮这才走到 send_xml_messages
clear_saw()
step_send(plugin, ev1, A)
check("★★★ 第 1 轮仍能剥离 ⇒ 框架收到空占位（不会重复发送）",
      framework_saw == ["<msg/>"], f"框架收到={framework_saw}")

print()
print("═══ 2) 对照：第 1 轮**自己**结束时清理本轮（应当清掉）")
plugin2, mp2 = build_plugin()
ev1b = Ev("turn-1")
ctx1b = main_mod.SendCtx("test:gm:1", ev1b, object())
asyncio.run(plugin2._emit_segment(A, ctx1b))
k1b = plugin2._ckey("test:gm:1", "turn-1")
check("前置：台账在", plugin2._sent_ledger.get(k1b) == [A])
asyncio.run(plugin2.observe_final(ev1b, None))
check("★ 本轮结束 ⇒ 清掉本轮残留", k1b not in plugin2._sent_ledger,
      f"台账={plugin2._sent_ledger}")

print()
print("═══ 3) 反向验证：判据有区分度 —— 没抢发时框架本就该把 A 发出去")
plugin3, mp3 = build_plugin()
ev3 = Ev("turn-1")
clear_saw()
step_send(plugin3, ev3, A)
check("★ 没抢发 ⇒ 框架收到 A（本该如此）", framework_saw == [A], f"{framework_saw}")

print()
print("═══ 4) 静默场景：`_clear_round_state` 不带 event_id 时必须保守")
plugin4, _ = build_plugin()
ev4 = Ev("turn-1")
asyncio.run(plugin4._emit_segment(A, main_mod.SendCtx("test:gm:1", ev4, object())))
k4 = plugin4._ckey("test:gm:1", "turn-1")
plugin4._clear_round_state("test:gm:1")          # 不带轮次（理论不该发生）
check("★★ 不带轮次 ⇒ 不误清带轮次的其它轮（保守，宁可留给陈旧窗口）",
      plugin4._sent_ledger.get(k4) == [A], f"台账={plugin4._sent_ledger}")

print()
print("═══ 5) 静态判据：不得再有「按 sid 前缀全清」")
MAIN = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
_i = MAIN.find("def _clear_round_state")
_j = MAIN.find("def observe_final", _i)
_body = MAIN[_i:_j]
_code = re.sub(r"(?m)^\s*#.*$", "", _body)      # 去注释（注释里会引用旧的写法）
check("★★★ 清理函数里不再有「前缀匹配全清」的循环",
      "startswith(_pre)" not in _code, "已移除" if "startswith(_pre)" not in _code else "仍存在")
check("★★★ 清理函数接受 event_id（只清本轮）",
      "def _clear_round_state(self, sid, event_id=None)" in MAIN)
check("★★★ observe_final 把 event_id 传进去",
      "_clear_round_state(getattr(event, \"sid\", None),\n" in MAIN
      or re.search(r"_clear_round_state\(getattr\(event, \"sid\", None\),\s*getattr\(event, \"event_id\", None\)\)",
                   MAIN) is not None)

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 跨轮清理安全 —— 重叠轮次不会再互相清账（重复发送的这条路径已封死）")
