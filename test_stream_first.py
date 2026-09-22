"""旁路流式的行为自检 —— 用假客户端模拟 SSE 流，验证：
  1. 段切分正确（只有闭合的 <msg> 才发）
  2. 已发段从 text_response 剥离（框架不重复发）
  3. tool_calls 回合不抢先发
  4. 发送失败立刻停手
  5. 首字延迟确实下降（模拟慢速流，对比"等全部"的耗时）
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── 把 core.provider.llm_model 用最小 stub 注入，让自检不依赖 KiraAI 运行环境 ──
# （真实场景下 core 是导入得到的；这里只为让本文件可以独立运行）
import types
from dataclasses import dataclass, field


@dataclass
class _Chunk:
    delta_text: str = ""
    delta_reasoning: str = ""
    tool_calls_delta: list = field(default_factory=list)
    is_final: bool = False
    finish_reason: str = ""
    usage: dict | None = None


@dataclass
class _Request:
    messages: list = field(default_factory=list)


@dataclass
class _Response:
    def __init__(self, text: str = ""):
        self.text_response = text
        self.reasoning_content = ""
        self.tool_calls: list = []
        self.tool_results: list = []
        self.input_tokens = None
        self.output_tokens = None
        self.cached_tokens = None
        self.time_consumed = None


_stub = types.ModuleType("core.provider.llm_model")
_stub.LLMStreamChunk = _Chunk
_stub.LLMRequest = _Request
_stub.LLMResponse = _Response
for _name in ("core", "core.provider"):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        _m.__path__ = []
        sys.modules[_name] = _m
sys.modules["core.provider.llm_model"] = _stub

from stream_first import StreamFirstRunner, SegmentEmitter  # noqa: E402
LLMStreamChunk = _Chunk
LLMRequest = _Request

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class FakeClient:
    """模拟一个慢速 SSE：每 0.05s 吐一小块。"""

    def __init__(self, pieces, delay=0.05, with_tool_call=False):
        self.pieces = pieces
        self.delay = delay
        self.with_tool_call = with_tool_call

    async def chat_stream(self, request, **kw):
        for i, p in enumerate(self.pieces):
            await asyncio.sleep(self.delay)
            if self.with_tool_call and i == 0:
                yield LLMStreamChunk(tool_calls_delta=[{
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "search", "arguments": ""},
                }])
            yield LLMStreamChunk(delta_text=p)


async def main() -> None:
    print("1) 段切分：只有闭合的 <msg> 才发")
    em = SegmentEmitter()
    em.feed("<msg><text>你好</text>")            # 未闭合 → 不发
    check("未闭合不发", len(em.pop_pending()) == 0)
    em.feed("</msg>")                            # 闭合 → 待发
    pend = em.pop_pending()
    check("闭合即待发", len(pend) == 1 and "你好" in pend[0], f"实际={pend}")
    em.feed("<msg/>")                            # 空 msg → 不发送
    check("空 msg 不发送", len(em.pop_pending()) == 0)
    check("remaining 为空", em.remaining() == "", f"实际={em.remaining()!r}")

    print("2) 已发段从 text_response 剥离（框架不重复发）")
    sent2 = []

    async def emit2(s):
        sent2.append(s)

    pieces = ["<msg><text>第一段</text></msg>", "<msg><text>第二段</text></msg>",
              "<msg><text>第三段还在"]      # 最后一段未闭合
    resp = await StreamFirstRunner(FakeClient(pieces, delay=0.02), emit2).run(LLMRequest(messages=[]))
    check("抢先发了 2 段", len(sent2) == 2, f"实际={len(sent2)}")
    check("text_response 只留尾巴（框架发第 3 段）",
          "第三段" in resp.text_response and "第一段" not in resp.text_response,
          f"实际={resp.text_response!r}")
    check("框架不会重复发已发段",
          "第一段" not in resp.text_response and "第二段" not in resp.text_response)

    print("3) tool_calls 回合不抢先发")
    sent3 = []

    async def emit3(s):
        sent3.append(s)

    client3 = FakeClient(["<msg><text>我应该先查一下</text></msg>", "<msg><text>x</text></msg>"],
                         delay=0.02, with_tool_call=True)
    resp3 = await StreamFirstRunner(client3, emit3).run(LLMRequest(messages=[]))
    check("有工具调用时一段都不发", len(sent3) == 0, f"实际={sent3}")
    check("内容仍完整保留给框架", "我应该先查一下" in resp3.text_response)
    check("tool_calls 已聚合",
          len(resp3.tool_calls) == 1 and resp3.tool_calls[0]["function"]["name"] == "search")

    print("4) 发送失败立刻停手（且不丢字）")
    em4 = SegmentEmitter()
    em4.feed("<msg><text>a</text></msg>")
    check("待发段已就绪", len(em4.pop_pending()) == 1)
    em4.mark_failed()
    em4.feed("<msg><text>b</text></msg>")
    check("失败后不再产生待发段", len(em4.pop_pending()) == 0)
    check("尾巴仍交回框架（不丢字）", "b" in em4.remaining(), f"实际={em4.remaining()!r}")

    print("5) 首字延迟：抢先发 vs 等全部")
    pieces5 = ["<msg><text>这是第一段回复内容</text></msg>", "<msg><text>这是第二段</text></msg>",
               "<msg><text>这是第三段</text></msg>"]
    t0 = time.perf_counter()
    sent5 = []

    async def emit5(s):
        sent5.append((time.perf_counter(), s))

    await StreamFirstRunner(FakeClient(pieces5, delay=0.1), emit5).run(LLMRequest(messages=[]))
    first_user_sees = sent5[0][0] - t0
    all_done = time.perf_counter() - t0
    check("抢先发的时刻早于全部收完", first_user_sees < all_done,
          f"首段 {first_user_sees:.2f}s vs 全部 {all_done:.2f}s")
    print(f"     （模拟 3 段 × 0.1s：用户 {first_user_sees:.2f}s 看到第一段，{all_done:.2f}s 才收完）")

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 条未通过: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
