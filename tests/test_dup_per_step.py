"""★★★ 多步 agent loop：**每一步各自抢发**时，绝不能重复发送。

## 这个套件是第五次修复（2026-09-27）引入的，它替换掉了原来的错误契约

原测试（旧版 `test_dup_send_multistep.py`）假设：

    第二步的文本**与第一步相同**（"框架重放 / 模型复读"）
    ⇒ 于是断言"台账必须活到轮结束、且只消费一次"

**这个前提是错的**，它让一个真 bug 溜过了 4 个版本。事实是：

  ① 流式引擎 patch 在 `ProviderManager.get_model_client` 上
     ⇒ **每一次 LLM 请求都会新建引擎**，`emit` = `_emit_segment`
     ⇒ 多步 loop 的**每一步都会抢发它自己那一段**；
  ② 框架 `core/message_manager.py:766` 里，`event` 是
     `async for step in agent_executor.run(...)` **循环外**创建的**同一个对象**
     ⇒ 同轮各步的 `event_id` **完全相同**；
  ③ 于是"已消费（轮次 id 相同）⇒ n=0 ⇒ 不剥离"，会命中**第二步**
     ⇒ 第二步自己刚抢发的 C,D 被框架**再发一次** ⇒ 用户看到 `C,D | C,D`。

## 本套件锁住的正确契约

  1. **每一次 `send_xml_messages` 对应一步**，要剥的就是"上一次发送之后、
     这一步抢发的那些段" ⇒ 台账**按步消费**（每次调用后清）。
  2. **不存在**"同轮第二步就跳过剥离"的任何判断。
  3. 轮次 id **只**用来判断"台账是否属于本轮"（丢弃跨轮残留，防误剪丢内容）。
  4. 第二步没有抢发时，它的内容**必须原样交给框架**（那才是对的，不能误切）。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")

WS = tempfile.mkdtemp(prefix="dup_step_")
os.makedirs(os.path.join(WS, "data"), exist_ok=True)
os.chdir(WS)
sys.path.insert(0, FW)

from core.message_manager import MessageProcessor                # noqa: E402

main_mod = _env.load("main")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class Ev:
    """框架侧：**一轮里所有步共用同一个 event 对象**（关键事实）。"""
    sid = "test:gm:1"
    is_stopped = False
    event_id = "ev-turn-1"


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
        from core.chat import MessageChain
        out = []
        for s in re.findall(r"<msg[^>]*>.*?</msg>|<msg[^>]*/>", xml, re.S):
            c = MessageChain()
            if s.rstrip() not in ("<msg/>", "<msg />"):
                c.text(s)
            out.append(c)
        return out

    async def send_message_chain(self, sid, chain):
        txt = "".join(str(e) for e in chain.message_list)
        self.sent.append(txt)
        return Result("E%d" % len(self.sent))


class Ctx:
    def __init__(self, mp):
        self.message_processor = mp


# ── 搭台：把**框架的** send_xml_messages 换成桩，再让插件去包它 ──────────
framework_saw: list[str] = []


async def stub_send_xml_messages(self, event, xml_data, tag_set):
    """占位"框架自己的发送实现"：记下它到底拿到了什么文本。"""
    framework_saw.append(xml_data)
    return []


MessageProcessor.send_xml_messages = stub_send_xml_messages


_LIVE: list = []


def K(plugin, sid, ev_id):
    """取状态键：新实现是 (sid,轮次) 复合键；旧实现只有 sid。

    ★ 为什么要兼容：反向验证要把**同一份测试**拿去跑旧实现，
      直接调 `plugin._ckey` 会 AttributeError 崩掉、跑不到第 6 幕
      ⇒ 反向验证就没意义了。
    """
    ck = getattr(plugin, "_ckey", None)
    return ck(sid, ev_id) if ck else sid


def RESET(plugin, sid, ev_id):
    """兼容两种签名：新实现 `_reset_turn_ledger(sid, 轮次)`；旧实现只有 `(sid)`。"""
    try:
        plugin._reset_turn_ledger(sid, ev_id)
    except TypeError:
        plugin._reset_turn_ledger(sid)


def build_plugin(ev):
    # ★ 真实卸载上一个实例的补丁再装 —— `install()` 有 R1 幂等：
    #   目标已打过标记时**不会重复包装**，否则本次插件根本没装上，
    #   调用会落到上一个实例的包装上（本测试第一次就踩到了这个）。
    while _LIVE:
        try:
            _LIVE.pop().patches.uninstall_all()
        except Exception:  # noqa: BLE001
            pass
    plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    plugin.early_send = True
    plugin._resp_by_sid = {}
    plugin._early_results = {}
    plugin._last_seg_ts = {}
    plugin._sent_ledger = {}
    plugin._ledger_ts = {}
    plugin._ledger_consumed = {}
    plugin._stats = {"early_sent": 0, "first_seg_s": None}
    mp = FakeMP()
    plugin.ctx = Ctx(mp)
    plugin._install_early_sent_strip()
    _LIVE.append(plugin)
    return plugin, mp


def step_send(plugin, ev, text):
    """模拟框架对**某一步**的调用。"""
    inst = object.__new__(MessageProcessor)
    return asyncio.run(
        MessageProcessor.send_xml_messages(inst, ev, text, object()))


A = "<msg><text>第一段</text></msg>"
B = "<msg><text>第二段</text></msg>"
C = "<msg><text>第三段</text></msg>"
D = "<msg><text>第四段</text></msg>"

print("═══ 1) 两步各自抢发：第二步**不得**被重复发送（本次 bug 的现场）")
ev = Ev()
plugin, mp = build_plugin(ev)
ctx = main_mod.SendCtx("test:gm:1", ev, object())

# 步1：抢发 A、B（真实 _emit_segment ⇒ 真实台账写入）
asyncio.run(plugin._emit_segment(A, ctx))
asyncio.run(plugin._emit_segment(B, ctx))
step_send(plugin, ev, A + B)
framework_saw.clear()

# 步2：**同轮同 event**，抢发 C、D（这是旧代码漏掉的那一步）
asyncio.run(plugin._emit_segment(C, ctx))
asyncio.run(plugin._emit_segment(D, ctx))
step_send(plugin, ev, C + D)

# ★ 判据必须看**框架实际发出去的**（framework_saw）——
#   不能用 mp.sent：那是我们抢发的记录，当然含有 C、D（第一次就写错了这条）。
fw = "".join(framework_saw)
leaked = [s for s in ("第一段", "第二段", "第三段", "第四段") if s in fw]
check("★★★ 框架没有再发任何已抢发的内容（不重复）",
      not leaked, f"框架重复发出={leaked} 框架原文={framework_saw}")
check("★ 两步共抢发 4 段，各只出现一次",
      len(mp.sent) == 4 and len(set(mp.sent)) == 4, f"抢发={mp.sent}")
check("★ 框架侧收到空占位（<msg/>），即这一步的内容已全部发出",
      all(x.strip() in ("", "<msg/>") for x in framework_saw), f"框架收到={framework_saw}")

print()
print("═══ 2) 每一步都用了**本步**的台账（不是上一步的）")
ev2 = Ev()
plugin2, mp2 = build_plugin(ev2)
ctx2 = main_mod.SendCtx("test:gm:1", ev2, object())
asyncio.run(plugin2._emit_segment(A, ctx2))
step_send(plugin2, ev2, A)
ledger_after_step1 = list(plugin2._sent_ledger.get(
    K(plugin2, "test:gm:1", ev2.event_id), []))
check("★ 一次发送之后台账被消费掉（按步清）",
      ledger_after_step1 == [], f"残留={ledger_after_step1}")

asyncio.run(plugin2._emit_segment(C, ctx2))
check("★ 同轮各步共用同一个键，账上只有本步的段",
      plugin2._sent_ledger.get(K(plugin2, "test:gm:1", ev2.event_id)) == [C],
      f"台账={plugin2._sent_ledger.get(K(plugin2, 'test:gm:1', ev2.event_id))}")

print()
print("═══ 3) 第二步**没有抢发**时：它的内容必须原样交给框架（不能误切）")
ev3 = Ev()
plugin3, mp3 = build_plugin(ev3)
ctx3 = main_mod.SendCtx("test:gm:1", ev3, object())
asyncio.run(plugin3._emit_segment(A, ctx3))
step_send(plugin3, ev3, A)
framework_saw.clear()
step_send(plugin3, ev3, C)          # 这一步没抢发
check("★★ 没抢发的内容原样交给框架（不误剪 = 不丢内容）",
      framework_saw and framework_saw[0].strip() == C.strip(),
      f"框架收到={framework_saw}")

print()
print("═══ 4) 跨轮残留：轮次一变，旧台账不得参与本轮剥离（防误剪丢内容）")
ev4 = Ev()
plugin4, mp4 = build_plugin(ev4)
ctx4 = main_mod.SendCtx("test:gm:1", ev4, object())
# 模拟"上一轮抢发了但没走到发送"（事件被 stop）⇒ 台账残留
# ★ 新契约：台账按 (sid,轮次) 复合键存 ⇒ 上一轮的残留落在**旧键**上，
#   本轮取自己的键，天然取不到 ⇒ 不可能误剪。
plugin4._sent_ledger[K(plugin4, "test:gm:1", "ev-turn-OLD")] = [A]
framework_saw.clear()
step_send(plugin4, ev4, C)          # 本轮文本是 C，与残留的 A 无关
check("★★ 旧轮残留不被拿去剥离（否则 C 会被误剪成空 = 丢内容）",
      framework_saw and framework_saw[0].strip() == C.strip(),
      f"框架收到={framework_saw}")

print()
print("═══ 5) 同轮（轮次键一致）的台账仍然生效（不是一律丢弃）")
ev5 = Ev()
plugin5, mp5 = build_plugin(ev5)
ctx5 = main_mod.SendCtx("test:gm:1", ev5, object())
asyncio.run(plugin5._emit_segment(A, ctx5))     # 轮次键 = ev5.event_id
framework_saw.clear()
step_send(plugin5, ev5, A)
check("★★ 本轮台账参与剥离 ⇒ 不重复",
      not framework_saw or framework_saw[0].strip() in ("", "<msg/>"),
      f"框架收到={framework_saw}")

print()
print("═══ 6) 静态核查：错误设计不得复活（窗口按**函数体**取，不再截 900 字符）")
MAIN = (Path(_env.ROOT) / "main.py").read_text(encoding="utf-8")

_i = MAIN.find("def _install_early_sent_strip")
_j = MAIN.find("def page(", _i)
BODY = MAIN[_i:_j] if _j > _i else MAIN[_i:]
check("★★★ 不再有「按轮次 id 跳过剥离」的判定（本次 bug 的根因）",
      not re.search(r"_cons_ev\s*==\s*_ev", BODY),
      "仍存在" if re.search(r"_cons_ev\s*==\s*_ev", BODY) else "已删除")
check("★★★ 不再有 _ledger_consumed 参与剥离决策",
      "_ledger_consumed" not in BODY,
      "仍引用" if "_ledger_consumed" in BODY else "已移除")
check("★★ 台账在 finally 里按步消费（每次调用后清，按复合键）",
      re.search(r"finally:[\s\S]{0,1500}?_sent_ledger\.pop\(_k, None\)", BODY) is not None)
check("★★ 台账键是 (sid,轮次) 复合键（用 _ckey，不再有独立的轮次键）",
      "_ckey" in BODY)
check("★★★ 轮次已并入键 ⇒ 不再需要比较轮次键（旧实现已被替换）",
      re.search(r"_raw_ev", BODY) is None
      and re.search(r'sid \+ "\\\\x00ev"', BODY) is None)
# 死代码检测：函数体的**最后一行**不该是裸的 pop（return 之后到不了）
_tail = [l for l in BODY.rstrip().splitlines() if l.strip()]
check("★ 函数体末尾没有 return 之后的不可达语句",
      "return mark_side_effects" in _tail[-1] or not _tail[-1].strip().startswith("plugin."),
      f"末行={_tail[-1].strip()[:60]!r}")

print()
print("═══ 6) ★★★ 重叠轮次 —— 线上「重复发送」的真根因")
# 线上日志实证（用户 run7.bat）：
#   15:35:33 [llm] Running agent using ...   ← 第 2 轮的 llm_request（触发 _reset_turn_ledger）
#   15:35:37 [accel] 一轮结束 sid=... steps=1 ← 第 1 轮这时才结束
# 即：第 1 轮"**已经抢发、但还没走到发送层**"时，第 2 轮就开始了。
# 而旧实现把台账 / 响应**只按 sid 存**：
#   · 第 2 轮开始时 `_reset_turn_ledger(sid)` 把第 1 轮的台账**清掉**
#   · `_resp_by_sid[sid]` 又被第 2 轮的响应**覆盖**
# ⇒ 第 1 轮走到发送层时 n=0、也拿不到自己的响应 ⇒ **剥离失败**
# ⇒ 框架把第 1 轮抢发过的内容**再发一次** = 用户看到的重复。
# 现在按 (sid, 轮次) 复合键 ⇒ 两轮各用各的键，互不干扰。
class EvA:
    sid = "test:gm:1"; is_stopped = False; event_id = "turn-A"
class EvB:
    sid = "test:gm:1"; is_stopped = False; event_id = "turn-B"
evA, evB = EvA(), EvB()
plugin6, mp6 = build_plugin(evA)
ctxA = main_mod.SendCtx("test:gm:1", evA, object())
ctxB = main_mod.SendCtx("test:gm:1", evB, object())

asyncio.run(plugin6._emit_segment(A, ctxA))          # 第 1 轮抢发 A
RESET(plugin6, "test:gm:1", evB.event_id)  # 第 2 轮开始（会做的那次清理）
asyncio.run(plugin6._emit_segment(C, ctxB))          # 第 2 轮抢发 C

framework_saw.clear()
step_send(plugin6, evA, A)                           # 第 1 轮这时才发送
check("★★★ 第 2 轮开始后，第 1 轮**仍然**能剥离（不被清账 ⇒ 不重复）",
      bool(framework_saw) and all(x.strip() in ("", "<msg/>") for x in framework_saw),
      f"框架收到={framework_saw}  ← 收到 A 就是重复发送")
framework_saw.clear()
step_send(plugin6, evB, C)                           # 第 2 轮自己发送
check("★★★ 第 2 轮同样能剥离自己的内容",
      bool(framework_saw) and all(x.strip() in ("", "<msg/>") for x in framework_saw),
      f"框架收到={framework_saw}")
# ★ 纯行为判据（不碰内部键名 ⇒ 换实现也不会假红/假绿）：
#   两轮都发送完之后，不该再有**任何**没被消费的台账残留。
check("★★ 两轮都发送完后没有未消费的台账残留",
      all(len(v) == 0 for v in plugin6._sent_ledger.values()),
      f"台账={plugin6._sent_ledger}")

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    shutil.rmtree(WS, ignore_errors=True)
    sys.exit(1)
print("🎉 全部通过 —— 多步 loop 每一步各自抢发，也不会重复发送")
shutil.rmtree(WS, ignore_errors=True)
