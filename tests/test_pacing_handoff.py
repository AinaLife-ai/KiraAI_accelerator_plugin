"""端到端核对：抢发之后，用户设置的"消息间隔"还有没有被完整遵守？

（这是回归测试，不是单纯测量 —— 之前这里真的出过问题，见下。）

用户明确指出：min/max_message_delay 是**有意为之的显示节奏**
（LLM 已经生成完了，这段是"让 bot 像人一样一条条说"），不是性能成本。

所以要问的不是"能不能省掉它"，而是：**我的抢先发送有没有把这个节奏搞乱？**

现在有**两套时钟**：
  · 我抢发的那几段        → 我的 _pace 按框架的 min/max 控制
  · 交给框架的剩余段      → 框架自己的循环控制（它的 sleep 在**每段之后**，
                            所以框架发的**第一段是立即发的**）
⇒ 交接处可能出现"零间隔"，即两段时间轴没有接上。

本测试把两段合起来看**全部段间间隔**。

**这里真的出过问题**（2026-09-23，被用户点醒）：修复前
```
5 段发出时刻: [0.0, 0.301, 0.602, 0.603, 0.903]
段间间隔:     [0.301, 0.301, 0.001, 0.301]
                              ↑ 交接处挤成一条
```
修法：交出剩余段之前，先把"距上次发送"补足（且只在剩余段真会发东西时才补）。
"""
import asyncio
import os
import random
import re
import sys
import time
import types
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
main_mod = _env.load("main")

LO, HI = 0.30, 0.30
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class Ev:
    sid = "test:gm:1"
    is_stopped = False
    event_id = "e1"


class FakeMP:
    """复刻 send_xml_messages 的骨架：解析 → 逐段发 → 每段后 sleep。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.sent = []
        self.min_message_delay = LO
        self.max_message_delay = HI

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
        class R:
            message_id = "F%d" % len(self.sent)
            ok = True
            err = None
        return R()


def _ctx30(delivery):
    return types.SimpleNamespace(
        message_delivery=delivery, message_processor=delivery,
        adapter_mgr=None, config={}, event_bus=None, persona_mgr=None,
        sticker_mgr=None, tool_mgr=None, session_mgr=None, prompt_mgr=None,
        provider_mgr=None, db=None, plugin_mgr=None,
    )


async def main_30():
    """★ 3.0 交接节奏：抢发段 → 框架首段之间必须守间隔。

    为什么这条在 3.0 也要测：框架的 sleep 是**每段之后**（`execute.py` 里
    `send_message_chain` 之后再 sleep）⇒ 它发的**第一条不会等待**；而抢发段是
    accel 自己按 min/max 节奏发的 ⇒ 交接处若不补等待就会出现"零间隔、两条挤成一条"。
    2.x 侧当年修过同一个坑；v1.0.83 起 3.0 的链级包装也补上了（本测试就是它的守卫）。
    """
    from core.chat.message_elements import Text
    from core.chat import MessageChain

    LO = HI = 0.3

    class Result:
        message_id = "M"
        ok = True
        err = None

    class FakeDelivery:
        min_message_delay = LO
        max_message_delay = HI

        def __init__(self):
            self.ts = []

        async def send_message_chain(self, session, chain, *, memory_message=None, self_id=None):
            self.ts.append(time.monotonic())
            return Result()

        def get_session_lock(self, sid):
            return asyncio.Lock()

    delivery = FakeDelivery()
    plugin = main_mod.AcceleratorPlugin(ctx=_ctx30(delivery), cfg={})
    plugin.patches.uninstall_all()
    plugin.early_send = True
    plugin._install_early_sent_strip()
    dcls = type(delivery)
    SID = "test:gm:1"

    print("\n[3.0] 1) 模拟「刚抢发过一段」后，框架发第一条链")
    plugin._last_seg_ts[SID] = time.monotonic()      # _emit_segment 收尾留下的状态
    t0 = time.monotonic()
    await dcls.send_message_chain(delivery, SID, MessageChain([Text("框架第1段")]))
    gap1 = delivery.ts[0] - t0
    print(f"     交接间隔 = {gap1:.3f}s（配置 {LO}s）")
    check("★★ 抢发段 → 框架首段的交接处守间隔（关键）",
          gap1 >= LO * 0.9, f"{gap1:.3f}s（≈0 就是又挤成一条了）")

    print("\n[3.0] 2) 框架自己每段后会 sleep ⇒ 后续链不该被**双重**等待")
    await asyncio.sleep(LO)                          # 镜像框架的 sleep
    await dcls.send_message_chain(delivery, SID, MessageChain([Text("框架第2段")]))
    gap2 = delivery.ts[1] - delivery.ts[0]
    print(f"     第二条链间隔 = {gap2:.3f}s（框架已等过一次）")
    check("★ 不双重等待（间隔≈框架自己的节奏，不是 2×配置）",
          gap2 < LO * 1.6, f"{gap2:.3f}s")

    plugin.patches.uninstall_all()


async def main():
    full = "".join("<msg>第%d段</msg>" % i for i in range(1, 6))     # 5 段
    from core.message_manager import MessageProcessor as _MP   # 世代判定用（局部导入避免作用域问题）
    if not hasattr(_MP, "send_xml_messages"):
        await main_30()
        return
    plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    plugin.patches.uninstall_all()
    plugin.early_send = True
    plugin._resp_by_sid = {}
    plugin._early_results = {}
    plugin._last_seg_ts = {}
    plugin._install_early_sent_strip()

    plug = main_mod.PluginsStub if hasattr(main_mod, "PluginsStub") else None

    class Ctx:
        message_processor = None

    mp = FakeMP(plugin)
    plugin.ctx = Ctx()
    plugin.ctx.message_processor = mp
    plugin.early_send = True
    plugin._frame_delay = lambda: (LO, HI)          # 固定间隔，便于判断

    print(f"设定间隔 min=max={LO}s，一段 5 段的回复\n")

    # ① 前 3 段"抢先发出"（模拟流式期间发的）
    ctx = main_mod.SendCtx("test:gm:1", Ev(), None)
    t_start = time.monotonic()
    early_ts = []
    for i in range(1, 4):
        # 用 _pace + 记录，复刻 _emit_segment 的发送部分
        await plugin._pace(ctx)
        mp.sent.append(time.monotonic())
        early_ts.append(time.monotonic())
        plugin._mark_sent(ctx)

    # ② 交给框架发剩余 2 段
    # ★★★ 2026-09-28（审计 P2）：`_early_results` 现在是**复合键**（与台账/响应一致）
    plugin._early_results[plugin._ckey("test:gm:1", Ev.event_id)] = [FakeMP(plugin) for _ in range(3)]
    class R:
        def __init__(self):
            self.__dict__["_accel_early_sent_count"] = 3
    # ★ 按 (sid,轮次) 复合键灌入 —— 与 main.py 的查找键一致
    plugin._resp_by_sid[plugin._ckey("test:gm:1", Ev.event_id)] = R()

    from core.message_manager import MessageProcessor
    FakeMP.__name__ = "MessageProcessorLike"
    results = await MessageProcessor.send_xml_messages(mp, Ev(), full, None)

    ts = sorted(mp.sent)
    gaps = [round(ts[i + 1] - ts[i], 3) for i in range(len(ts) - 1)]
    print(f"  5 段各自发出的时刻（相对起点）: {[round(t - t_start, 3) for t in ts]}")
    print(f"  段间间隔: {gaps}")
    print(f"  第一个间隔: {gaps[0] if gaps else '—'}")
    print()

    check("5 段都发出去了", len(ts) == 5, f"{len(ts)} 段")
    check("抢发段之间守间隔（0.3s）",
          all(0.27 <= g <= 0.6 for g in gaps[:2]), f"{gaps[:2]}")
    # ★ 关键：第 3 段（最后一个抢发段）→ 第 4 段（框架的第一段）
    handoff = gaps[2] if len(gaps) > 2 else None
    check("★★ 抢发段→框架段的交接处也守间隔（关键）",
          handoff is not None and handoff >= 0.27,
          f"交接间隔 = {handoff}（应 ≈{LO}s；若≈0 就是挤成一条）")

    plugin.patches.uninstall_all()

    print("\n" + "=" * 58)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for x in FAIL:
            print("   ✗", x)
        sys.exit(1)
    print("🎉 全部通过 —— 节奏从头到尾都守住了")


asyncio.run(main())
