"""与「实例级接管」插件的链式共存（multi-decision / qqbot-bridge 形状）。

背景（真实插件形状）：
  · multi-decision（B3.1.8.x）会把 `ctx.message_delivery.send_message_chain`
    换成**实例方法**，并包住 provider_mgr.get_model_client；
  · qqbot-fullmsg-bridge 会把 `chat_stream` 包在**它拿到的客户端**上做旁听。

Python 的属性查找里实例属性优先于类方法 ⇒ 我们打在**类**上的传输层补丁会被
遮住（实测 `_consume_early_match` 调用 0 次），而 __slots__ 代理又让
`client.chat_stream = ...` 直接抛 AttributeError（对方只能静默放弃）。

本测试钉死：
  A. 发送层：对方先装 / 后装 / 中途安装，三种顺序都必须让我们的对账进入调用链；
  B. 代理：可写属性、可包 chat_stream（幂等 + 可还原）、copy/deepcopy 不炸；
  C. 模型客户端：实例级补丁外还能拿到流式代理，且不会套娃；
  D. 代理缓存：模型配置变了必须换新代理（不能把请求发回旧配置）。
"""
import asyncio
import copy
import functools
import os
import sys
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
if _env.gen() != "3":
    _env.skip("实例级接管 / 链式发送是 3.x 形态（2.x 走 MessageProcessor.send_xml_messages）")

main_mod = _env.load("main")
se = _env.load("stream_engine")
ACC = next(o for n, o in vars(main_mod).items()
           if isinstance(o, type) and hasattr(o, "_install_client_cache"))

PASS, FAIL = [], []

def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))

class Delivery:
    """最小可用的发送服务（有 __dict__，可被实例补丁）。"""
    min_message_delay = 0.0
    max_message_delay = 0.0

    def __init__(self):
        self.calls = 0

    async def send_message_chain(self, session, chain, **kwargs):
        self.calls += 1
        return ("sent", self.calls)

    def get_session_lock(self, sid):
        return asyncio.Lock()

class MP:
    min_message_delay = 0.0
    max_message_delay = 0.0

def make_ctx(delivery, provider=None):
    return types.SimpleNamespace(
        message_delivery=delivery, message_processor=MP(),
        adapter_mgr=None, config={}, event_bus=None, persona_mgr=None,
        sticker_mgr=None, tool_mgr=None, session_mgr=None, prompt_mgr=None,
        provider_mgr=provider, db=None, plugin_mgr=None,
        get_plugin_data_dir=lambda: "/tmp/itest/data/plugin",
    )

def foreign_wrap(original):
    """模拟别的插件：实例属性包一层（和 multi-decision 的形状一致）。

    真实插件普遍用 functools.wraps —— 它会把原函数的 __dict__（包括我们的 MARK）
    一起复制过来。这正是 `contains_mark` 要认的情况：链上已经有我们，就不再套。
    """
    @functools.wraps(original)
    async def send_message_chain(*args, **kwargs):
        return await original(*args, **kwargs)
    return send_message_chain

def spy(plugin):
    hits = []
    orig = plugin._consume_early_match
    plugin._consume_early_match = lambda *a, **k: (hits.append(1), None)[1]
    return hits, orig

async def run_delivery_case(order):
    d = Delivery()
    ctx = make_ctx(d)
    p = ACC(ctx=ctx, cfg={"section_stream": {"early_send": True}})
    foreign = foreign_wrap(d.send_message_chain)
    if order == "foreign_first":
        d.send_message_chain = foreign
        p._install_early_sent_strip()
    elif order == "foreign_later":
        p._install_early_sent_strip()
        d.send_message_chain = foreign
        p._ensure_patch_chains()          # 模拟 ON_LLM_REQUEST / ON_LOADED 自愈
    else:
        p._install_early_sent_strip()
        d.send_message_chain = foreign
    hits, orig = spy(p)
    try:
        out = await d.send_message_chain("A:gm:1", [])
    finally:
        p._consume_early_match = orig
        p.patches.uninstall_all()
    return len(hits), d.calls, out

async def delivery_section():
    print("\n[A] 发送层：实例级补丁的三种安装顺序")
    n, calls, _ = await run_delivery_case("foreign_first")
    check("对方先装：对账仍在链上", n == 1, f"hits={n}")
    check("对方先装：底层只发 1 次", calls == 1, f"calls={calls}")
    n, calls, _ = await run_delivery_case("foreign_later")
    check("对方后装 + 自愈：对账仍在链上", n == 1, f"hits={n}")
    n, calls, _ = await run_delivery_case("foreign_after_heal")
    check("对方后装但未自愈：类补丁被遮住（记录真实行为）", n == 0, f"hits={n}")

class FakeClient:
    type = "llm"

    def __init__(self, provider_config=None, model_id="m"):
        self.model = types.SimpleNamespace(
            provider_id="p", model_id=model_id, provider_name="P",
            provider_config=provider_config or {}, model_config={})
        self.chunks = ["a", "b"]

    async def chat(self, request, **kwargs):
        return "".join(self.chunks)

    def chat_stream(self, request, **kwargs):
        async def gen():
            for c in self.chunks:
                yield c
        return gen()

async def proxy_section():
    print("\n[B] 流式代理：可写 / 可包 chat_stream / 可复制")
    proxy = se.LLMClientProxy(FakeClient(), lambda req: None)
    try:
        proxy.cache_flag = {"x": 1}
        wrote = True
    except Exception:  # noqa: BLE001
        wrote = False
    check("插件能往客户端写属性", wrote)
    check("写进去能读回来", getattr(proxy, "cache_flag", None) == {"x": 1})

    raw = FakeClient()
    p = se.LLMClientProxy(raw, lambda req: None)
    orig_stream = raw.chat_stream
    seen = []

    async def wrapped(request, **kwargs):
        async for chunk in orig_stream(request, **kwargs):
            seen.append(chunk)
            yield chunk

    MARK = "_test_bridge_mark"
    setattr(wrapped, MARK, True)
    p.chat_stream = wrapped            # 旧实现这里抛 AttributeError
    check("在代理上包 chat_stream 不抛异常", True)
    check("installed() 能看见 MARK（不会重复套娃）",
          bool(getattr(getattr(p, "chat_stream", None), MARK, False)))
    got = [c async for c in p.chat_stream(None)]
    check("包装真的被执行且产出完整", got == ["a", "b"] and seen == ["a", "b"])
    p.chat_stream = orig_stream
    check("还原后 MARK 消失 / 底层恢复",
          not getattr(getattr(p, "chat_stream", None), MARK, False)
          and raw.chat_stream == orig_stream)

    try:
        copy.copy(p)
        copy.deepcopy(p)
        ok = True
    except Exception as e:  # noqa: BLE001
        ok = False
        print("      copy error:", type(e).__name__, e)
    check("copy / deepcopy 不爆栈", ok)
    try:
        import weakref
        w = weakref.ref(p)
        weak_ok = w() is p
    except Exception:  # noqa: BLE001
        weak_ok = False
    check("支持 weakref（插件常用弱引用缓存）", weak_ok)

class StubManager:
    def __init__(self):
        self.calls = 0

    def get_model_client(self, provider_id, model_id, model_type=None):
        self.calls += 1
        return FakeClient(model_id=model_id)

async def provider_section():
    print("\n[C] 模型客户端：实例级补丁外再链一层")
    mgr = StubManager()
    ctx = make_ctx(Delivery(), provider=mgr)
    p = ACC(ctx=ctx, cfg={})
    foreign = mgr.get_model_client

    def foreign_get(*args, **kwargs):
        return foreign(*args, **kwargs)

    mgr.get_model_client = foreign_get        # 对方先装（实例属性）
    p._chain_provider_instance_patch()
    got = mgr.get_model_client("p", "m")
    check("返回流式代理", isinstance(got, se.LLMClientProxy), type(got).__name__)
    check("代理类型可冒充", isinstance(got, FakeClient))
    check("不套娃（_accel_proxy 只一层）",
          getattr(getattr(got, "_wrapped", None), "_accel_proxy", False) is False)
    p.patches.uninstall_all()

async def cache_section():
    print("\n[D] 代理缓存：模型配置变了必须换新代理")
    mgr = StubManager()
    ctx = make_ctx(Delivery(), provider=mgr)
    p = ACC(ctx=ctx, cfg={})
    c1 = FakeClient(provider_config={"base_url": "https://old"})
    a = p._stream_wrap_client(c1)
    c2 = FakeClient(provider_config={"base_url": "https://old"})
    b = p._stream_wrap_client(c2)
    check("配置相同 ⇒ 复用同一代理", a is b)
    c3 = FakeClient(provider_config={"base_url": "https://new"})
    c = p._stream_wrap_client(c3)
    check("配置变了 ⇒ 换新代理（不会发回旧配置）", c is not a)
    check("新代理包的是新客户端",
          getattr(c, "_wrapped", None) is c3)
    p.patches.uninstall_all()

async def delivery_e2e_section():
    """真实 `MessageDeliveryService` + 抢发 + 外来投递屏障：核对**不重复投递**。

    这是问题 A 的"后果级"验证：只证明"对账函数被调到"还不够，
    要在真实发送链上数**实际投递次数**。
    """
    print("\n[E] 真实发送链：抢发 + 外来实例屏障，不重复投递")
    try:
        from core.workflow.src.im.message_delivery import MessageDeliveryService
        from core.chat.message_utils import KiraIMSentResult, MessageChain
        from core.chat.message_elements import Text
        from core.tag.base import BaseTag
        from core.tag.tag_registry import TagSet
        from core.provider import LLMResponse
    except Exception as exc:  # noqa: BLE001
        print(f"  -    跳过：当前框架没有 3.x 发送服务（{type(exc).__name__}）")
        return

    class TextTag(BaseTag):
        name = "text"

        async def handle(self, value, **kwargs):
            return [Text(value)]

    class FakeAdapter:
        def __init__(self):
            self.sent = []
            self.info = types.SimpleNamespace(platform="test")

        def get_capability(self, cap):
            return self

        async def send_group_message(self, pid, chain):
            self.sent.append(chain)
            return KiraIMSentResult(message_id="M%d" % len(self.sent))

        async def send_direct_message(self, pid, chain):
            return await self.send_group_message(pid, chain)

    class FakeAdapters:
        def __init__(self, adapter):
            self.adapter = adapter

        def get_adapter(self, name):
            return self.adapter

    class FakeConfig:
        def get_config(self, key, default=None):
            return default

    class Delivery(MessageDeliveryService):
        def __init__(self, adapter):
            self.kira_config = FakeConfig()
            self.adapter_mgr = FakeAdapters(adapter)
            self.persona_mgr = None
            self.message_history = None
            self.session_locks = {}

    class Ev:
        def __init__(self):
            self.sid = "bot:gm:1"
            self.event_id = "ev-1"
            self.is_stopped = False
            self.bot = None

    async def run(order):
        adapter = FakeAdapter()
        delivery = Delivery(adapter)

        async def ctx_send(session, chain, **kw):
            return await delivery.send_message_chain(session, chain, **kw)

        ctx = make_ctx(delivery)
        ctx.send_message_chain = ctx_send
        p = ACC(ctx=ctx, cfg={"section_stream": {"early_send": True}})
        guard = None
        if order == "foreign_first":
            # 模拟多样决策：实例方法（包住当时的方法）
            delivery.send_message_chain = foreign_wrap(delivery.send_message_chain)
            p._install_early_sent_strip()
        else:
            p._install_early_sent_strip()
            delivery.send_message_chain = foreign_wrap(delivery.send_message_chain)
            p._ensure_patch_chains()
        try:
            event = Ev()
            sctx = main_mod.SendCtx(event.sid, event, TagSet())
            sctx.tag_set.register(TextTag)
            req = types.SimpleNamespace()
            req.__dict__["_accel_ctx"] = sctx
            resp = LLMResponse("<msg><text>第一段</text></msg><msg><text>第二段</text></msg>")
            p._make_engine(req).on_complete(resp)

            await p._emit_segment("<msg><text>第一段</text></msg>", sctx)
            xml = resp.text_response
            actions = await delivery.parse_xml(xml, sctx.tag_set)
            for action in actions:
                if isinstance(action, MessageChain) and not action.is_empty():
                    await delivery.send_message_chain(event.sid, action)
            normal = list(adapter.sent)
            # 最坏情况：解析剥离失败，框架把已抢发的那一段原样再发一次
            await delivery.send_message_chain(event.sid, MessageChain([Text("第一段")]))
            return ("".join(getattr(e, "text", "") for e in c.message_list)
                    for c in normal), len(adapter.sent)
        finally:
            p.patches.uninstall_all()

    for order in ("accelerator_first", "foreign_first"):
        texts, total = await run(order)
        texts = list(texts)
        check(f"{order}: 两段各投递一次", total == 2, f"total={total} {texts}")
        check(f"{order}: 第一段没有被重发", texts.count("第一段") == 1, str(texts))

async def main():
    print("#" * 70)
    print("# 插件链式共存 / 客户端形状 守卫")
    print("#" * 70)
    await delivery_section()
    await proxy_section()
    await provider_section()
    await cache_section()
    await delivery_e2e_section()
    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
        sys.exit(1)
    print("🎉 全部通过 —— 实例级接管/流式旁听/代理缓存都有守卫")

asyncio.run(main())
