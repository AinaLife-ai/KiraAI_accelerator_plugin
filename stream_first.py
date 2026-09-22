"""L4 旁路流式层 —— 这是整个加速器里**唯一真正能让用户"感觉快了"**的部分。

原理（一句话）：
    框架用 `await model.chat(request)` 拿结果（非流式）。
    我们在 chat() 内部改用 chat_stream() 收 SSE，**边收边把已成型的 <msg> 段发出去**，
    收完后再 return 一个"完整的 LLMResponse"，让框架后续流程照常走。

对其它插件透明的三个设计点（关键）：
    ① 返回完整 LLMResponse —— 框架的 ON_LLM_RESPONSE / XML 解析 / 记忆写入全照常，
       别的插件看到的 text_response 依然是完整语义。
    ② 已发送的段从 text_response 里剥离 —— 框架不会重复发。整段发完则返回 <msg/>。
    ③ 由调用方（main.py）补广播 ON_MESSAGE_SENT —— 绕过框架发送层就必须补这个钩子，
       否则 sustained-chat 那类订阅 ON_MESSAGE_SENT 的插件会"瞎掉"。

风险与边界（诚实标注）：
    - tool_calls 回合不发：流里一旦出现工具调用增量，本回合就不抢先发（这不是最终回复）。
    - 发送失败立刻停手：不再继续抢先发，**所有内容完整交回框架**（不丢字）。
    - 熔断：连续失败 N 次永久旁路（复用 patches.Breaker）。
"""
from __future__ import annotations

import re
import time
from typing import Any, Awaitable, Callable, Optional

from core.provider.llm_model import LLMRequest, LLMResponse, LLMStreamChunk

# 匹配一个**已闭合**的 <msg> 段：
#   完整：<msg ...> ... </msg>
#   空  ：<msg/> 或 <msg />
# 非贪婪 + DOTALL ⇒ "流到哪发到哪"：只要有一个完整段出现就立刻可以发。
_MSG_CLOSED = re.compile(r"<msg(?:\s[^>]*)?>.*?</msg>|<msg(?:\s[^>]*)?/>", re.DOTALL)


class SegmentEmitter:
    """把流式增量切成"已成型的 <msg> 段"。

    职责分离：本类只负责**切分**，不做发送（发送是异步的，交给调用方 await）。
    """

    def __init__(self, min_len: int = 1):
        self.min_len = min_len
        self.buf = ""
        self.pending: list[str] = []   # 待发送（已闭合、非空）
        self.emitted: list[str] = []   # 已交给发送方的段
        self.send_failed = False

    def feed(self, delta: str) -> None:
        # ⚠️ 必须**先缓冲**再判断：发送失败后如果直接 return，
        #    这段增量就永远丢失了（既没发出去，也不在 remaining 里 → 用户看不到）。
        #    失败只是"不再抢先发"，内容仍要完整交回框架。
        self.buf += delta
        if self.send_failed:
            return
        while True:
            m = _MSG_CLOSED.search(self.buf)
            if not m:
                break
            seg = m.group(0)
            # 从"未发送缓冲"里摘掉（无论后面发不发，都不该再重复出现在 remaining）
            self.buf = self.buf[:m.start()] + self.buf[m.end():]
            if len(seg) < self.min_len or seg.rstrip() in ("<msg/>", "<msg />"):
                self.emitted.append(seg)      # 空消息：算已处理，但不发送
                continue
            self.pending.append(seg)

    def pop_pending(self) -> list[str]:
        out = self.pending
        self.pending = []
        return out

    def mark_failed(self) -> None:
        self.send_failed = True

    def remaining(self) -> str:
        """收流结束后，仍未发送的尾巴（交回框架，走框架原发送流程）。"""
        return self.buf


class StreamFirstRunner:
    """包装一个 LLM 客户端，把它的 chat() 变成"流式收集 + 抢先发送"。

    :param emit: 异步发送回调 —— 接一个 `<msg>...</msg>` 字符串，负责真正发出去
                 （并补广播 ON_MESSAGE_SENT）。
    """

    def __init__(self, client: Any, emit: Callable[[str], Awaitable[None]]):
        self.client = client
        self.emit = emit

    async def run(self, request: LLMRequest, **kwargs) -> LLMResponse:
        kwargs.pop("stream", None)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        usage: dict | None = None
        saw_tool_call = False

        emitter = SegmentEmitter()

        started = time.perf_counter()
        first_seg_at: Optional[float] = None

        async for chunk in self.client.chat_stream(request, **kwargs):
            _absorb(chunk, text_parts, reasoning_parts, tool_acc)
            if chunk.tool_calls_delta:
                saw_tool_call = True
            if chunk.usage:
                usage = chunk.usage

            # 出现工具调用 ⇒ 本轮不是最终回复 ⇒ 立刻停止抢先发（已发的保持已发）
            if saw_tool_call or emitter.send_failed:
                continue

            if chunk.delta_text:
                emitter.feed(chunk.delta_text)
                for seg in emitter.pop_pending():
                    if first_seg_at is None:
                        first_seg_at = time.perf_counter()
                    try:
                        await self.emit(seg)
                        emitter.emitted.append(seg)
                    except Exception:  # noqa: BLE001
                        # 发送失败：停手，后续内容全部交回框架（不丢字）
                        emitter.mark_failed()
                        emitter.pending.clear()
                        break

        full_text = "".join(text_parts)
        resp = LLMResponse(full_text)
        resp.reasoning_content = "".join(reasoning_parts)
        for idx in sorted(tool_acc):
            a = tool_acc[idx]
            resp.tool_calls.append({
                "id": a["id"],
                "type": "function",
                "function": {"name": a["name"], "arguments": a["arguments"]},
            })
        if usage:
            resp.input_tokens = usage.get("input_tokens")
            resp.output_tokens = usage.get("output_tokens")
            resp.cached_tokens = usage.get("cached_tokens")
        resp.time_consumed = round(time.perf_counter() - started, 2)

        # ── 关键：把已抢先发出的段从 text_response 里剥离，避免框架重复发送 ──
        early = len(emitter.emitted)
        if early:
            remaining = emitter.remaining()
            resp.text_response = remaining if remaining.strip() else "<msg/>"
            resp.__dict__["_accel_early_sent"] = early
            resp.__dict__["_accel_first_seg_s"] = (
                round(first_seg_at - started, 3) if first_seg_at else None
            )
        return resp


def _absorb(chunk: LLMStreamChunk, text_parts, reasoning_parts, tool_acc) -> None:
    if chunk.delta_text:
        text_parts.append(chunk.delta_text)
    if chunk.delta_reasoning:
        reasoning_parts.append(chunk.delta_reasoning)
    for frag in chunk.tool_calls_delta or []:
        idx = frag.get("index", 0)
        acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        if frag.get("id"):
            acc["id"] = frag["id"]
        fn = frag.get("function") or {}
        if fn.get("name"):
            acc["name"] = fn["name"]
        if fn.get("arguments"):
            acc["arguments"] += fn["arguments"]


def make_chat_impl(runner_factory: Callable[[Any], StreamFirstRunner]):
    """生成 chat() 的替代实现，供 patches.install 使用。"""

    async def impl(self, request: LLMRequest, **kwargs) -> Any:
        runner = runner_factory(self)
        return await runner.run(request, **kwargs)

    return impl
