"""★★★ 走**生产路径**的抢发契约：`_make_engine` 的 emit 回调必须遵守投递契约。

## 为什么必须有这个套件（它抓到了一个静默了整整一版的真 bug）

历史事故（2026-09-28 审计 P0）：

    def _make_engine(...):
        if self.early_send:
            async def _emit(seg: str) -> None:
                await self._emit_segment(seg, ctx)     # ← 忘了 return
            emit = _emit

而引擎是**按返回值**判"这一段到底发出去没有"的：

    ok = await self.emit(seg)
    if ok is False or ok is None:        # ← None 被当成"没投递"
        emitter.push_back_many(batch[bi:]); emitter.mark_failed(); break
    emitter.emitted.append(seg)          # ← 永远到不了

后果（全部**静默**，没有异常、没有日志）：
  · 抢先发送只在**第一段**有效（`mark_failed()` 之后整轮不再抢发）—— 提速核心基本失效
  · 响应私有标记（_accel_early_sent_count / _segments）恒为空
    ⇒ 发送层"标记优先"的主路径**从未被执行过**，一切压在台账这一根线上
  · 工具轮防护（写 `_accel_tool_turn_early`）永不触发

## 为什么既有测试没抓到

所有既有 dup 测试都**自己写 emit 回调**（`return True` / `return await real_emit(...)`），
或直接调 `_emit_segment`，**从不经过 `_make_engine()`**。
⇒ 本套件专门走生产路径：`plugin._make_engine(request)` → 真 StreamEngine → 真 client。

## 锁住的三件事

  1. 生产 `_emit` 返回真正的投递结果（True/False，**绝不能是 None**）
  2. 走生产路径时：N 段全部抢发成功 ⇒ N 段各发一次、响应标记 = N、框架收到空
  3. 反向验证：不 return 的形状必须复现"只剩首段"（判据有区分度）
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
from core.provider.llm_model import LLMStreamChunk               # noqa: E402

main_mod = _env.load("main")
es = _env.load("early_sent")
se = _env.load("stream_engine")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


S1 = "<msg><text>第一段</text></msg>"
S2 = "<msg><text>第二段</text></msg>"
S3 = "<msg><text>第三段</text></msg>"
FULL = S1 + S2 + S3


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


class FakeClient:
    """把 FULL 按 7 字符一块吐出来（保证 <msg> 段会被逐个切出）。"""

    def __init__(self, text):
        self.text = text

    def chat_stream(self, request, **kw):
        text = self.text

        async def gen():
            for i in range(0, len(text), 7):
                yield LLMStreamChunk(delta_text=text[i:i + 7])
        return gen()


class Ev:
    sid = "test:gm:1"
    is_stopped = False
    event_id = "ev-prod"


class Ctx:
    def __init__(self, mp):
        self.message_processor = mp


def _make_plugin():
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
    plugin._frame_delay = lambda: (0.0, 0.0)          # 测试里不等
    return plugin, mp


def _mk_req():
    req = se.LLMRequest()
    req.__dict__["_accel_ctx"] = main_mod.SendCtx("test:gm:1", Ev(), object())
    return req


print("═══ 1) ★★★ 生产路径：N 段全部抢发成功，且每段只发一次")
plugin, mp = _make_plugin()
req = _mk_req()
emit_returns = []
engine = plugin._make_engine(req)
# 用"探针"记录生产回调的返回值（保留真实效果 —— 它内部就是生产 _emit）
_base_emit = engine.emit


async def _probe(seg):
    r = await _base_emit(seg)
    emit_returns.append(r)
    return r


engine.emit = _probe
resp = asyncio.run(engine.run(FakeClient(FULL), req))

print(f"     emit 返回值: {emit_returns}")
check("★★★ 生产 emit 回调返回 True（不是 None —— 那就是忘了 return）",
      bool(emit_returns) and all(r is True for r in emit_returns), f"{emit_returns}")
check("★★★ 三段全部抢发出去（不是只剩首段）",
      len(mp.sent) == 3, f"实际发出 {len(mp.sent)} 段: {mp.sent}")
check("★★ 响应私有标记 = 3（这条通道曾经 100% 失效）",
      es.early_sent_count(resp) == 3, f"{es.early_sent_count(resp)}")
check("★★ 响应里存了已发段原文",
      es.early_sent_segments(resp) == [S1, S2, S3], f"{es.early_sent_segments(resp)}")
check("★ 已发段数 == 实际投递数（不重复的根本保证）",
      es.early_sent_count(resp) == len(mp.sent))

print()
print("═══ 2) ★ 发送层拿标记/台账做剥离 ⇒ 框架收到空（零重复）")
n = es.early_sent_count(resp)
segs = es.early_sent_segments(resp)
stripped = es.strip_early_sent_smart(FULL, n, segs)
check("★★ 剥离后不剩内容（框架不会重发）", stripped == "<msg/>", stripped)

print()
print("═══ 3) ★ 工具轮防护：文本先于 tool_calls 到达 ⇒ 必须写标记")


class FakeClientToolTurn:
    """先吐一段完整文本，再吐 tool_calls 增量（同一响应）。"""

    def chat_stream(self, request, **kw):
        async def gen():
            for i in range(0, len(S1), 7):
                yield LLMStreamChunk(delta_text=S1[i:i + 7])
            yield LLMStreamChunk(tool_calls_delta=[
                {"index": 0, "id": "t1", "function": {"name": "f", "arguments": "{}"}}])
        return gen()


plugin_t, mp_t = _make_plugin()
req_t = _mk_req()
eng_t = plugin_t._make_engine(req_t)
resp_t = asyncio.run(eng_t.run(FakeClientToolTurn(), req_t))
check("★★ 工具轮已发内容被标记（避免重复发送/复读）",
      resp_t.__dict__.get("_accel_tool_turn_early") is True,
      f"{resp_t.__dict__.get('_accel_tool_turn_early')!r}")

print()
print("═══ 4) 反向验证：把「忘了 return」的形状放回去，必须复现「只剩首段」")
plugin2, mp2 = _make_plugin()
req2 = _mk_req()
eng2 = plugin2._make_engine(req2)
_ok_emit = eng2.emit


async def _no_return(seg):
    await _ok_emit(seg)          # ← 就是少了 return（复刻修前形状）
    return None


eng2.emit = _no_return
resp2 = asyncio.run(eng2.run(FakeClient(FULL), req2))
check("★★ 反向：返回 None ⇒ 引擎判「没投递」⇒ 响应标记为 0",
      es.early_sent_count(resp2) == 0, f"{es.early_sent_count(resp2)}")
check("★★ 反向：只剩**首段**被抢发（修前就是这个症状 ⇒ 判据有区分度）",
      len(mp2.sent) == 1, f"实际 {len(mp2.sent)} 段: {mp2.sent}")

print()
print("═══ 5) 静默警示：None 必须留下线索（不再是无日志的灾难）")
_src = (Path(__file__).resolve().parent.parent / "stream_engine.py").read_text(encoding="utf-8")
check("★★ 引擎对 None 返回打 warning（含「忘了 return」线索）",
      "忘了 return" in _src)
_src_main = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
check("★★ 生产 _emit 已带 `-> bool` 与 return",
      "async def _emit(seg: str) -> bool:" in _src_main
      and "return await self._emit_segment(seg, ctx)" in _src_main)

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 生产抢发契约成立 —— 不会再无声退化成「只剩首段」")
