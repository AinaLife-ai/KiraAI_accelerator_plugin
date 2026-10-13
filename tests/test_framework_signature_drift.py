"""回归：**框架签名漂移**时接管点必须原样透传参数（线上事故 2026-10-13 20:49）。

## 事故

新版框架给 `MessageDeliveryService.send_message_chain` 加了 `bot=` 关键字
（`MessageProcessor.send_message_chain` 已弃用，转发时会带上它）：

    TypeError: AcceleratorPlugin._install_early_sent_strip.<locals>.
               delivery_factory.<locals>.send_message_chain()
               got an unexpected keyword argument 'bot'

而该接管点被声明为"有不可撤销的副作用 ⇒ **不回退原实现**"（否则会重复发送）
⇒ 不是退回旧行为，而是**整条发送失败**：框架直发 / 弃用入口 / acc 抢发
三个路径全被挡死 —— 实测**投递次数 0**（消息一条都发不出去）。

## 本套件钉住的

  S1 「框架直发」带新关键字（`bot=`）⇒ 必须正常投递，且参数**原样**到达原实现；
  S2 「弃用入口」`MessageProcessor.send_message_chain` 转发 `bot=` ⇒ 同样正常；
  S3 任意**位置/关键字**附加参数（未来的新参数）⇒ 原样透传；
  S4 `parse_xml` / `add_message_ids` 带附加参数 ⇒ 原样透传；
  S5 acc 抢发路径：新的 `ctx.send_message_chain` 存在时**优先用它**
     （框架的弃用入口每段刷一条 DeprecationWarning），且确实投递成功；
  S6 2.x 世代（若当前框架走的是 `send_xml_messages` 分支）：包装同样透传附加参数；
  S7 静态守卫：四处包装的签名都必须带 `**kwargs`（别再写死参数表）。

Run: python3 tests/test_framework_signature_drift.py [<plugin_dir> ...]
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
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
    print("[SKIP] 需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
    sys.exit(0)

sys.path.insert(0, FW)
import core.plugin  # noqa: E402,F401
from core.tag import TagSet, BaseTag  # noqa: E402
from core.chat import MessageChain  # noqa: E402
from core.chat.message_elements import Text  # noqa: E402
from core.chat.message_utils import KiraIMSentResult  # noqa: E402

main_mod = _env.load("main")
SID = "qq:gm:D593FBB0F18264477911ECDCBF336D6F"


class _TextTag(BaseTag):
    name = "text"

    async def handle(self, value, **kwargs):
        return [Text(value)] if value else []


def _tagset():
    ts = TagSet()
    ts.register(_TextTag)
    return ts


class _Delivery:
    """模拟**新版**框架：`send_message_chain` 多了 `bot=`（并用 *args/**kwargs 兜底）。"""

    min_message_delay = 0.0
    max_message_delay = 0.0

    def __init__(self):
        self.got = []
        self.parsed = []
        self.ids = []

    @staticmethod
    async def parse_xml(xml_data, *args, **kwargs):
        _CURRENT[0].parsed.append((xml_data, args, kwargs))
        from core.workflow.src.im.message_delivery import MessageDeliveryService as MDS
        return await MDS.parse_xml(xml_data, args[0] if args else kwargs.get("tag_set"))

    @staticmethod
    def add_message_ids(xml_data, message_results, *args, **kwargs):
        _CURRENT[0].ids.append((xml_data, args, kwargs))
        from core.workflow.src.im.message_delivery import MessageDeliveryService as MDS
        return MDS.add_message_ids(xml_data, message_results)

    async def send_message_chain(self, session, chain, *args, **kwargs):
        self.got.append({"session": session, "chain": chain, "args": args, "kwargs": kwargs})
        return KiraIMSentResult(message_id="SENT-%d" % len(self.got), ok=True)


_CURRENT = [None]


class _MP:
    """模拟**新版**框架的 MessageProcessor：已弃用但会转发 `bot=`。"""

    def __init__(self, delivery):
        self.delivery = delivery
        self.used = 0

    async def send_message_chain(self, session, chain, *, memory_message=None):
        self.used += 1
        return await self.delivery.send_message_chain(
            session, chain, memory_message=memory_message, bot=self)


class _Ctx:
    def __init__(self, delivery, mp, with_ctx_send=True, ctx_send_has_bot=True):
        self.message_delivery = delivery
        self.message_processor = mp
        self.adapter_mgr = None
        self.provider_mgr = None
        if with_ctx_send and not ctx_send_has_bot:
            # rc1 形状：入口在，但**不吃 bot**
            async def _ctx_send_rc1(session, chain):
                return await self.message_delivery.send_message_chain(session, chain)
            self.send_message_chain = _ctx_send_rc1
        elif with_ctx_send:
            # 框架推荐的入口（PluginContext.send_message_chain）：
            # 形状按 **rc2**（`*, bot=None`）——真实 rc1 没这个参数，acc 会按探测结果处理
            async def _ctx_send(session, chain, *, bot=None):
                return await self.message_delivery.send_message_chain(
                    session, chain, bot=bot)
            self.send_message_chain = _ctx_send
        self._with_ctx_send = with_ctx_send


def _plugin(delivery, mp, with_ctx_send=True, ctx_send_has_bot=True):
    p = main_mod.AcceleratorPlugin(_Ctx(delivery, mp, with_ctx_send, ctx_send_has_bot), {})
    p._install_early_sent_strip()
    return p


async def run():
    d = _Delivery()
    _CURRENT[0] = d
    mp = _MP(d)
    plugin = _plugin(d, mp)
    installed = sorted(h.name for h in plugin.patches._handles)
    print(f"  已装接管点：{installed}")

    chain = MessageChain([Text("框架直发")])

    is30 = "early_sent_dedupe_delivery" in installed
    if not is30:
        print("  · S1~S5b 跳过：当前世代走 2.x 分支（接管点在 send_xml_messages，"
              "delivery 层未被包装）")

    # S1 框架直发 + 新关键字
    try:
        await d.send_message_chain(SID, chain, memory_message=None, bot="BOT1")
        ok1 = bool(d.got) and d.got[-1]["kwargs"].get("bot") == "BOT1"
        err1 = ""
    except Exception as exc:
        ok1, err1 = False, f"{type(exc).__name__}: {exc}"
    if is30:
        check("S1 框架直发带 bot= ⇒ 投递成功且参数原样到达（修前 TypeError ⇒ 全灭）", ok1, err1)

    # S2 弃用入口（MessageProcessor 转发 bot=）
    try:
        await mp.send_message_chain(SID, MessageChain([Text("弃用入口")]))
        ok2 = mp.used == 1 and d.got[-1]["kwargs"].get("bot") is mp
        err2 = ""
    except Exception as exc:
        ok2, err2 = False, f"{type(exc).__name__}: {exc}"
    if is30:
        check("S2 弃用入口转发 bot= ⇒ 投递成功", ok2, err2)

    # S3 任意附加参数（位置 + 关键字）
    try:
        await d.send_message_chain(SID, MessageChain([Text("额外参数")]), "POS", flag=7)
        last = d.got[-1]
        ok3 = last["args"] == ("POS",) and last["kwargs"].get("flag") == 7
        err3 = ""
    except Exception as exc:
        ok3, err3 = False, f"{type(exc).__name__}: {exc}"
    if is30:
        check("S3 位置/关键字附加参数原样透传（未来的新参数不会再炸）", ok3, err3)

    # S4 parse_xml / add_message_ids 透传
    ts = _tagset()
    try:
        await d.parse_xml("<msg><text>x</text></msg>", ts, "EXTRA", flag=1)
        ok4a = bool(d.parsed) and "EXTRA" in d.parsed[-1][1] and d.parsed[-1][2].get("flag") == 1
    except Exception as exc:
        ok4a, err3 = False, f"{type(exc).__name__}: {exc}"
    try:
        d.add_message_ids("<msg></msg>", [], "EXTRA", flag=2)
        ok4b = d.ids and d.ids[-1][1] == ("EXTRA",) and d.ids[-1][2].get("flag") == 2
    except Exception as exc:
        ok4b = False
    if is30:
        check("S4 parse_xml / add_message_ids 的附加参数原样透传", ok4a and ok4b)
    else:
        print("  · S4 跳过：2.x 不走 parse_xml/add_message_ids 接管点")

    # S5 抢发路径：优先用 ctx.send_message_chain（不刷弃用告警），且投递成功
    ev = types.SimpleNamespace(sid=SID, event_id="e1")
    sctx = main_mod.SendCtx(SID, ev, _tagset())
    before = len(d.got)
    try:
        ok5 = await plugin._emit_segment("<msg><text>抢发一段</text></msg>", sctx)
        ok5b = len(d.got) == before + 1 and mp.used == 1
    except Exception as exc:
        ok5, ok5b = False, False
    if is30:
        check("S5 acc 抢发：走 ctx.send_message_chain（不刷弃用告警）且投递成功",
              bool(ok5) and ok5b, f"delivered={ok5} 投递增量={len(d.got) - before}")

    # S5b 没有 ctx.send_message_chain 时回落到弃用入口（老框架也能用）
    d2 = _Delivery()
    _CURRENT[0] = d2
    mp2 = _MP(d2)
    plugin2 = _plugin(d2, mp2, with_ctx_send=False)
    ok5c = await plugin2._emit_segment("<msg><text>回落</text></msg>",
                                      main_mod.SendCtx(SID, types.SimpleNamespace(sid=SID, event_id="e2"), _tagset()))
    if is30:
        check("S5b 没有 ctx.send_message_chain ⇒ 回落弃用入口也能发出去", bool(ok5c) and mp2.used == 1)

    # S8/S9 出站身份：rc2 起框架用 `bot=` 记录出站历史（self_id/sender_name）。
    #        · 目标**接受** bot= ⇒ acc 抢发必须把本轮 event 的 bot 带上；
    #        · 目标**不接受** ⇒ 绝不能传（否则 TypeError，且不许用重试兜）。
    if is30:
        marker = object()
        d3 = _Delivery()
        _CURRENT[0] = d3
        mp3 = _MP(d3)
        plugin3 = _plugin(d3, mp3)
        ev8 = types.SimpleNamespace(sid=SID, event_id="e8", bot=marker)
        ok8 = await plugin3._emit_segment("<msg><text>带 bot</text></msg>",
                                         main_mod.SendCtx(SID, ev8, _tagset()))
        got_bot = d3.got[-1]["kwargs"].get("bot") if d3.got else None
        check("S8 目标接受 bot= ⇒ 抢发把本轮 event.bot 带上（出站历史身份一致）",
              bool(ok8) and got_bot is marker, f"got={got_bot!r}")

        class _NoBotDelivery(_Delivery):
            async def send_message_chain(self, session, chain):       # 老签名：不吃 bot
                self.got.append({"session": session, "chain": chain, "args": (), "kwargs": {}})
                return KiraIMSentResult(message_id="SENT", ok=True)

        d4 = _NoBotDelivery()
        _CURRENT[0] = d4
        # rc1 形状：ctx 入口在、但不吃 bot（真实旧框架就是这样）
        plugin4 = _plugin(d4, _MP(d4), with_ctx_send=True, ctx_send_has_bot=False)
        try:
            ok9 = await plugin4._emit_segment("<msg><text>无 bot</text></msg>",
                                             main_mod.SendCtx(SID, ev8, _tagset()))
            err9 = ""
        except Exception as exc:
            ok9, err9 = False, f"{type(exc).__name__}: {exc}"
        check("S9 目标不接受 bot= ⇒ 绝不传（老框架/2.x 也不会被 TypeError 挡死）",
              bool(ok9) and bool(d4.got) and "bot" not in d4.got[-1]["kwargs"], err9)

    # S6 2.x 世代（若当前走 send_xml_messages 分支）：
    #    把框架的方法换成"新版签名"（多收参数）再装接管点 —— 模拟"框架加参数"的真实情形。
    if "early_sent_strip" in installed:
        from core.message_manager import MessageProcessor
        calls = []

        async def _tolerant(self_, event, xml_data, tag_set, *args, **kwargs):
            calls.append((xml_data, args, kwargs))
            return ["R"]

        _orig_attr = MessageProcessor.send_xml_messages
        MessageProcessor.send_xml_messages = _tolerant          # 模拟新版框架
        try:
            _plugin(_Delivery(), _MP(_Delivery()), with_ctx_send=False)   # 重新安装接管点
            wrapped = MessageProcessor.send_xml_messages
            await wrapped(types.SimpleNamespace(plugin=None),
                          types.SimpleNamespace(sid=SID), "<msg><text>a</text></msg>",
                          _tagset(), "EXTRA", flag=3)
            ok6 = (bool(calls) and calls[-1][1] == ("EXTRA",)
                   and calls[-1][2].get("flag") == 3)
        except Exception as exc:
            ok6 = False
            print(f"      （2.x 分支调用异常：{type(exc).__name__}: {exc}）")
        finally:
            MessageProcessor.send_xml_messages = _orig_attr
        check("S6 2.x 分支：框架给 send_xml_messages 加参数 ⇒ 包装透传不炸", ok6)
    else:
        print("  · S6 跳过：当前世代走 3.0 分支（2.x 才有 send_xml_messages）")

    # S7 静态守卫：包装签名必须带 **kwargs
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    want = {"send_message_chain", "parse_xml", "add_message_ids", "send_xml_messages"}
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in want:
            # 只看包装实现（有 **kwargs 的那一处就够）
            has_kw = node.args.kwarg is not None and node.args.kwarg.arg == "kwargs"
            found.setdefault(node.name, False)
            found[node.name] = found[node.name] or has_kw
    missing = [k for k in want if k in found and not found[k]]
    check("S7 三处发送/解析包装的签名都带 **kwargs（别再写死参数表）",
          not missing, f"缺 kwargs 的：{missing}（已见到的：{sorted(found)}）")


asyncio.run(run())

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 框架加参数/改签名也不会再把发送挡死")
