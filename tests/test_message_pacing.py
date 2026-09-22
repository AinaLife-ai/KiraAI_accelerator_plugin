"""消息间隔：抢先发送必须遵守框架的 min/max_message_delay。

用户反馈（2026-09-23）：最小/最大消息间隔没有按框架设置生效。

框架侧的真实语义（core/message_manager.py:930）：
    在 send_xml_messages 的循环里，**每发一条之后**
        await asyncio.sleep(random.uniform(min_message_delay, max_message_delay))
    min/max 来自 bot_config，默认 0.8 / 1.5（message_manager.py:167-168）。

我们绕过那个循环自己发，所以必须把同样的间隔补上。
本测试**量真实的时间差**，而不是看代码"应该有等"。
"""
import asyncio
import os
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

from core.chat import MessageChain                        # noqa: E402
from core.provider.llm_model import LLMRequest            # noqa: E402

main_mod = _env.load("main")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class FakeEvent:
    def __init__(self, sid):
        self.sid = sid


class FakeMP:
    def __init__(self, lo, hi):
        self.sent = []
        self.min_message_delay = lo
        self.max_message_delay = hi

    async def _parse_xml_msg(self, seg, tag_set):
        c = MessageChain()
        c.text(seg)
        return [c]

    async def send_message_chain(self, sid, chain):
        self.sent.append((sid, time.monotonic()))
        return {"status": "ok"}


class FakeCtx:
    def __init__(self, mp):
        self.message_processor = mp


def make_plugin(mp):
    p = main_mod.AcceleratorPlugin(ctx=FakeCtx(mp), cfg={})
    p.early_send = True
    p.thinking_enabled = False
    p._resp_by_sid = {}
    p._last_seg_ts = {}
    return p


async def main():
    print("1) 间隔 = 0.30s，连发 4 段，量实际间隔")
    mp = FakeMP(0.30, 0.30)
    p = make_plugin(mp)
    req = LLMRequest(messages=[{"role": "user", "content": "hi"}])
    await p.capture_and_optimize(FakeEvent("S1"), req, {})
    ctx = main_mod.ctx_of(req)

    t0 = time.monotonic()
    for i in range(4):
        await p._emit_segment(f"<msg>seg{i}</msg>", ctx)
    ts = [t for _, t in mp.sent]
    gaps = [round(ts[i + 1] - ts[i], 3) for i in range(len(ts) - 1)]
    print(f"     各段相对起点的时刻: {[round(t - t0, 3) for t in ts]}")
    print(f"     段间间隔: {gaps}")

    check("4 段都发出去了", len(ts) == 4, f"{len(ts)} 段")
    check("★ 第一段立即发（不等）", ts[0] - t0 < 0.05, f"{ts[0]-t0:.3f}s")
    # 下界是硬要求（原来的 bug 就是间隔 0）；上界放宽些 ——
    # 机器负载高时某一段可能被拖慢，那是环境不是逻辑。
    bad = [g for g in gaps if not (0.27 <= g <= 0.75)]
    check("★ 后续每段间隔 >= 0.30s（遵守框架配置，原 bug 是 0）",
          not bad, f"异常间隔={bad}")

    print("\n2) 间隔 = 1.0s 时是否跟着变")
    mp2 = FakeMP(1.0, 1.0)
    p2 = make_plugin(mp2)
    req2 = LLMRequest(messages=[{"role": "user", "content": "x"}])
    await p2.capture_and_optimize(FakeEvent("S2"), req2, {})
    for i in range(3):
        await p2._emit_segment(f"<msg>s{i}</msg>", ctx=main_mod.ctx_of(req2))
    ts2 = [t for _, t in mp2.sent]
    gaps2 = [round(ts2[i + 1] - ts2[i], 3) for i in range(len(ts2) - 1)]
    print(f"     段间间隔: {gaps2}")
    check("★ 间隔跟着配置变成 >=1.0s", all(0.90 <= g <= 2.0 for g in gaps2), str(gaps2))

    print("\n3) 间隔区间 (0.1, 0.3)：每段都要落在区间内")
    mp3 = FakeMP(0.1, 0.3)
    p3 = make_plugin(mp3)
    req3 = LLMRequest(messages=[{"role": "user", "content": "x"}])
    await p3.capture_and_optimize(FakeEvent("S3"), req3, {})
    for i in range(5):
        await p3._emit_segment(f"<msg>s{i}</msg>", ctx=main_mod.ctx_of(req3))
    ts3 = [t for _, t in mp3.sent]
    gaps3 = [ts3[i + 1] - ts3[i] for i in range(len(ts3) - 1)]
    print(f"     段间间隔: {[round(g,3) for g in gaps3]}")
    check("★ 都在 [0.1, 0.3] 内（不是固定值也不是 0）",
          all(0.09 <= g <= 0.33 for g in gaps3), str([round(g, 3) for g in gaps3]))
    check("确实有随机性（不是每段一模一样）",
          len({round(g, 1) for g in gaps3}) > 1, str([round(g, 3) for g in gaps3]))

    print("\n4) 会话之间互不干扰：A 发完 B 的第一段不该被拖住")
    mp4 = FakeMP(0.5, 0.5)
    p4 = make_plugin(mp4)
    rA = LLMRequest(messages=[{"role": "user", "content": "x"}])
    rB = LLMRequest(messages=[{"role": "user", "content": "x"}])
    await p4.capture_and_optimize(FakeEvent("A"), rA, {})
    await p4.capture_and_optimize(FakeEvent("B"), rB, {})
    await p4._emit_segment("<msg>a</msg>", main_mod.ctx_of(rA))
    t_b = time.monotonic()
    await p4._emit_segment("<msg>b</msg>", main_mod.ctx_of(rB))
    check("★ B 的第一段立即发（不被 A 的节奏拖住）",
          time.monotonic() - t_b < 0.15, f"{time.monotonic()-t_b:.3f}s")

    print("\n5) 拿不到 message_processor 时不该崩")
    p5 = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    p5._last_seg_ts = {}
    lo, hi = p5._frame_delay()
    check("ctx 为 None ⇒ (0,0) 而不是异常", (lo, hi) == (0.0, 0.0), f"{lo},{hi}")

    print("\n" + "=" * 58)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("   ✗", f)
        sys.exit(1)
    print("🎉 全部通过 —— 消息间隔与框架配置一致")


asyncio.run(main())
