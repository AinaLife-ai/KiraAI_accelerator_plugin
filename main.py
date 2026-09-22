"""KiraAI 提速器 —— 在不影响质量与兼容的前提下提速。

分层设计：
  L1 观测层    始终可开，零风险：记录每步耗时/token/抢先发送情况
  L3 接管层    护栏式打补丁：HTTP 客户端复用 / 记忆落盘异步化
  L4 流式引擎  ★ 强制流式（任意 provider）+ 抢先发送（首字即刻可见）
  L5 自动思考  ★ 对标并超越 Alife：多信号打分决定这轮要不要开思考

接管铁律（patches.py 实现）：幂等 / 精确还原 / 熔断旁路。

兼容性保证（已核对插件市场 7 个重点插件的真实源码）：
  - 插件依赖 `client.model.*`（sustained-chat queue_merge.py:136、
    midflight main.py:261、model-fallback main.py:35）⇒ 代理类透传 `.model`
  - 插件依赖 `ON_LLM_REQUEST` 改 tool_set ⇒ 我们用 Priority.LOW 后执行，不抢
  - 插件依赖 `ON_MESSAGE_SENT` ⇒ 抢先发送时我们自己补广播
"""
from __future__ import annotations

import random
import time
from collections import deque
from typing import Any, Optional

from core.plugin import BasePlugin, logger, on, Priority, register, PluginPage, PageMenu
from core.provider import LLMRequest, LLMResponse
from core.chat.message_utils import KiraMessageBatchEvent

from .patches import PatchHandle, PatchRegistry, install, breaker_for
from .stream_engine import StreamEngine, LLMClientProxy
from .auto_thinking import (
    AutoThinkingController,
    build_thinking_extra_body,
    build_nothinking_extra_body,
)

PLUGIN_ID = "kira_accelerator"

class AcceleratorPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.patches = PatchRegistry()

        # ── 配置 ──
        c_obs = cfg.get("section_observe", {}) or {}
        c_req = cfg.get("section_request", {}) or {}
        c_tak = cfg.get("section_takeover", {}) or {}
        c_str = cfg.get("section_stream", {}) or {}
        c_thk = cfg.get("section_thinking", {}) or {}
        c_req = cfg.get("section_compat", {}) or {}
        c_app = cfg.get("section_appearance", {}) or {}

        self.observe_enabled = bool(c_obs.get("enabled", True))
        self.observe_window = int(c_obs.get("window", 50))


        self.takeover_build_client = bool(c_tak.get("reuse_http_client", False))

        # L4
        self.force_stream = bool(c_str.get("force_stream", False))
        self.early_send = bool(c_str.get("early_send", False))
        self.stream_providers = list(c_str.get("providers", []) or [])

        # 请求体兼容加固（默认开，零风险）
        self.normalize_empty_content = bool(c_req.get("normalize_empty_content", True))

        # 同轮工具并行（默认关）
        c_par = cfg.get("section_parallel", {}) or {}
        from .parallel_tools import ToolGate, DEFAULT_BLACKLIST
        self._tool_gate = ToolGate(
            enabled=bool(c_par.get("enabled", False)),
            whitelist=list(c_par.get("whitelist", []) or []),
            blacklist=list(c_par.get("blacklist", []) or []) or list(DEFAULT_BLACKLIST),
            match_exact=bool(c_par.get("match_exact", False)),
            max_parallel=int(c_par.get("max_parallel", 8)),
        )

        # 外观
        self.wallpaper_enabled = bool(c_app.get("wallpaper_enabled", True))
        self.wallpaper_interval = int(c_app.get("wallpaper_interval", 30))
        self.wallpaper_files = list(c_app.get("wallpaper_files", []) or [])

        # L5
        self.thinking_enabled = bool(c_thk.get("enabled", False))
        self.thinking_style = str(c_thk.get("style", "compatible"))
        self.thinking_inject_nothink = bool(c_thk.get("inject_nothinking", False))
        self.thinking = AutoThinkingController(
            threshold=float(c_thk.get("threshold", 2.0)),
            long_message_chars=int(c_thk.get("long_message_chars", 220)),
            context_ratio_on=float(c_thk.get("context_ratio_on", 1.6)),
            context_ratio_off=float(c_thk.get("context_ratio_off", 1.15)),
            context_min_samples=int(c_thk.get("context_min_samples", 5)),
            score_last_turn_tool=float(c_thk.get("score_last_turn_tool", 1.0)),
            effort_low=float(c_thk.get("effort_low", 2.0)),
            effort_high=float(c_thk.get("effort_high", 4.5)),
            max_per_minute=int(c_thk.get("max_per_minute", 60)),
        )

        # ── 运行时状态 ──
        self._samples: deque[dict] = deque(maxlen=self.observe_window)
        self._client_cache: dict[tuple, Any] = {}
        self._current_sid: Optional[str] = None
        self._current_event: Any = None
        self._current_tag_set: Any = None
        self._current_resp: Any = None
        self._last_seg_ts: Optional[float] = None
        self._proxy_cache: dict[tuple, LLMClientProxy] = {}
        self._stats = {
            "turns": 0, "steps": 0,
            "tool_signals": 0, "strip_calls": 0, "parallel_batches": 0, "content_normalized": 0,
            "streamed_calls": 0, "early_sent": 0, "first_seg_s": None,
            "thinking_on": 0, "thinking_off": 0, "thinking_skipped_budget": 0,
        }

    # ══════════════════════════════════════════════════════════
    async def initialize(self) -> None:
        logger.info(
            "[accel] initialize（观测=%s 强制流式=%s 抢先发送=%s 自动思考=%s）",
            self.observe_enabled, self.force_stream,
            self.early_send, self.thinking_enabled,
        )
        if self.takeover_build_client:
            self._install_client_cache()
        if self.force_stream or self.early_send:
            self._install_stream_engine()
        if self.thinking_enabled or self.normalize_empty_content:
            self._install_request_hook()
        if self.early_send:
            self._install_early_sent_strip()
        if self._tool_gate.enabled:
            self._install_parallel_tools()

    async def terminate(self) -> None:
        self.patches.uninstall_all()
        self._proxy_cache.clear()
        logger.info("[accel] terminate，所有接管点已还原")

    # ══════════════════════════════════════════════════════════
    # L1 观测
    # ══════════════════════════════════════════════════════════
    @on.step_result(priority=Priority.SYS_LOW)
    async def observe_step(self, event: KiraMessageBatchEvent, step_result, *_):
        if not self.observe_enabled:
            return
        try:
            self._stats["steps"] += 1
            self._samples.append({
                "ts": time.time(),
                "sid": getattr(event, "sid", "?"),
                "segments": len(getattr(step_result, "message_results", []) or []),
                "chars": len(getattr(step_result, "raw_output", "") or ""),
            })
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 观测失败")

    @on.final_result(priority=Priority.SYS_LOW)
    async def observe_final(self, event: KiraMessageBatchEvent, final_result, *_):
        if not self.observe_enabled:
            return
        try:
            self._stats["turns"] += 1
            sid = getattr(event, "sid", "?")
            # 把"本轮有没有用工具"记为"上一轮"，供下一轮判定使用
            self.thinking.commit_turn(sid)
            d = self.thinking.last_decision(sid)
            logger.info(
                "[accel] 一轮结束 sid=%s steps=%d 抢先发=%d 首段=%.2fs 思考=%s",
                sid, len(getattr(final_result, "step_results", []) or []),
                self._stats["early_sent"],
                self._stats["first_seg_s"] or 0.0,
                (f"开({d.effort},{'/'.join(d.signals)})" if d and d.enabled else "关"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 观测失败")

    # ══════════════════════════════════════════════════════════
    # L2 请求优化
    # ══════════════════════════════════════════════════════════
    @on.llm_request(priority=Priority.SYS_LOW)
    async def capture_and_optimize(self, event: KiraMessageBatchEvent, req: LLMRequest,
                                   tag_set=None, *_):
        """排在 SYS_LOW（实测无任何插件占用）⇒ 保证在所有插件之后收口，顺序确定。

        只做两件事（**刻意不做工具裁剪**，见 README「为什么不做工具裁剪」）：
          1. 记录本轮上下文（sid/event/tag_set），供抢先发送使用
          2. 自动思考判定（结果在 request 上打标记，由 _build_request_kwargs 注入）
        """
        self._current_sid = getattr(event, "sid", None)
        self._current_event = event
        self._current_tag_set = tag_set

        if self.thinking_enabled and self._current_sid:
            decision = self.thinking.decide(self._current_sid, req)
            req.__dict__["_accel_thinking"] = decision
            if decision.enabled:
                self._stats["thinking_on"] += 1
            elif "超预算" in decision.signals:
                self._stats["thinking_skipped_budget"] += 1
            else:
                self._stats["thinking_off"] += 1

    @on.tool_result(priority=Priority.SYS_LOW)
    async def on_tool_result(self, event: KiraMessageBatchEvent, tool_result, *_):
        """**只读**：把"上轮工具失败"这个信号喂给自动思考判定。

        刻意**不修改** tool_result.text（见 README「为什么不做结果瘦身」）：
          - 框架把结果写回 resp 是在所有 ON_TOOL_RESULT handler 之后
            （session_merger 的注释也点明了这个时序陷阱）
          - 实测 midflight 与 session_merger 都会读 `tool_result.text`，
            改动会影响它们的判断
          - 内容一旦被截断，模型会以为自己拿到了全文
        """
        sid = getattr(event, "sid", "")
        if not sid:
            return
        # 「上一轮用过工具」信号的数据来源
        self.thinking.note_turn_tool(sid)
        try:
            text = getattr(tool_result, "text", "") or ""
            head = text[:200].lower()
            if "error" in head or "失败" in head or "denied" in head or "not allowed" in head:
                self.thinking.note_tool_error(sid)
            else:
                self.thinking.note_tool_ok(sid)
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 工具结果信号读取失败（已忽略）")

    # ══════════════════════════════════════════════════════════
    # L3 接管
    # ══════════════════════════════════════════════════════════
    def _install_client_cache(self) -> None:
        try:
            from core.utils import model_clients as mc
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 导入 model_clients 失败")
            return

        cache = self._client_cache

        def factory(original):
            def make(self):
                key = (self.model.provider_config.get("base_url", ""),
                       self.model.provider_config.get("api_key", ""),
                       id(self.model.provider_config))
                c = cache.get(key)
                if c is None:
                    c = original(self)
                    cache[key] = c
                return c
            return make

        h = PatchHandle("reuse_http_client")
        if install(h, mc.OpenAICompatibleLLMClient, "_build_client", factory) is not None:
            self.patches.add(h)

    # ══════════════════════════════════════════════════════════
    # L4 流式引擎（强制流式 + 抢先发送）
    # ══════════════════════════════════════════════════════════
    def _install_stream_engine(self) -> None:
        """patch `ProviderManager.get_model_client`，把返回的 client 包一层代理。

        为什么选这里而不是 patch 某个 client 类的 chat()：
          ① **一次性覆盖所有 provider**（OpenAI / DeepSeek / Anthropic / 任何插件注册的）
          ② 插件自己注册的 provider 也自动生效
          ③ **不改任何客户端的 `chat_stream`** ⇒ 与 openai-stream-compat 之类的
             provider 插件天然兼容（它重写的 chat 会先跑，我们再包一层）
        """
        try:
            from core.provider.provider_manager import ProviderManager
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 导入 provider_manager 失败")
            return

        plugin = self
        proxy_cache = self._proxy_cache
        allowed = set(self.stream_providers)

        # ★ 缓存上限：provider 反复重建时旧 proxy 会累积（且键是 id(client)，
        #   id 复用会造成脏命中）。超过上限就整体清空重建 —— 代价只是下次多包一层。
        MAX_PROXY_CACHE = 64

        def factory(original):
            def get_model_client(self, provider_id, model_id, model_type=None):
                client = original(self, provider_id, model_id, model_type)
                if client is None:
                    return None
                # 只包装 LLM 客户端（其它类型如 TTS/STT 无流式语义）
                if type(client).__name__.endswith("EmbeddingClient") or \
                   type(client).__name__.endswith("ImageClient"):
                    return client
                if not hasattr(client, "chat") or not hasattr(client, "chat_stream"):
                    return client

                # provider 白名单（空=全部）
                if allowed:
                    pname = getattr(getattr(client, "model", None), "provider_name", "") or ""
                    pid = getattr(getattr(client, "model", None), "provider_id", "") or ""
                    if pname not in allowed and pid not in allowed:
                        return client

                # 用 (provider_id, model_id) 而非 id(client) 做键：
                # id 会被解释器复用，拿它当键在对象被 GC 后可能命中"别人的"代理。
                m = getattr(client, "model", None)
                key = (getattr(m, "provider_id", ""), getattr(m, "model_id", ""))
                if len(proxy_cache) > MAX_PROXY_CACHE:
                    proxy_cache.clear()
                    logger.info("[accel] 流式代理缓存超过 %d，已重置", MAX_PROXY_CACHE)
                cached = proxy_cache.get(key)
                if cached is not None:
                    return cached
                proxy = LLMClientProxy(client, plugin._make_engine)
                proxy_cache[key] = proxy
                logger.info("[accel] 已为 %s/%s 启用流式代理",
                            getattr(m, "provider_name", "?"), getattr(m, "model_id", "?"))
                return proxy
            return get_model_client

        h = PatchHandle("stream_engine")
        if install(h, ProviderManager, "get_model_client", factory) is not None:
            self.patches.add(h)
            logger.info("[accel] 流式引擎已安装（强制流式=%s 抢先发送=%s providers=%s）",
                        self.force_stream, self.early_send, sorted(allowed) or "ALL")

    def _make_engine(self) -> StreamEngine:
        # 每次模型调用都是新的一轮 ⇒ 重置节奏时间戳，
        # 避免上一轮的发送时刻被算进本轮的"距上次发送"
        self._last_seg_ts = None
        emit = None
        if self.early_send:
            async def _emit(seg: str) -> None:
                await self._emit_segment(seg)
            emit = _emit
        def _remember(resp):
            # 记录本轮响应，供发送层（send_xml_messages）读取标记做剥离
            self._current_resp = resp

        return StreamEngine(force_stream=self.force_stream, emit=emit, on_complete=_remember)

    async def _emit_segment(self, seg: str) -> None:
        """把一段已闭合的 <msg> 真正发出去，并补广播 ON_MESSAGE_SENT。"""
        import asyncio

        mp = getattr(self.ctx, "message_processor", None)
        if mp is None or not self._current_sid or self._current_tag_set is None:
            raise RuntimeError("抢先发送上下文不完整")

        actions = await mp._parse_xml_msg(seg, self._current_tag_set)
        from core.chat import MessageChain

        for action in actions:
            if not isinstance(action, MessageChain) or action.is_empty():
                continue
            result = await mp.send_message_chain(self._current_sid, action)

            # ★ 补广播 ON_MESSAGE_SENT（绕过框架发送层就必须补）
            try:
                from core.plugin.plugin_handlers import event_handler_reg, EventType
                if self._current_event is not None:
                    for handler in event_handler_reg.get_handlers(EventType.ON_MESSAGE_SENT):
                        await handler.exec_handler(self._current_event, action, result)
            except Exception:  # noqa: BLE001
                logger.exception("[accel] 补广播 ON_MESSAGE_SENT 失败")

            # ★ 段间隔：**遵守框架自己的配置**（bot_config.min_message_delay /
            #   max_message_delay），插件不自造第二个数字。
            #
            #   与框架的唯一差别是**等待的时机点**，等待时长完全相同：
            #     框架：发完第 K 段之后，无条件 sleep(uniform(min, max))
            #     我们：第 K+1 段就绪、准备发之前，补足"距上次发送还差的量"
            #
            #   两者数学等价：
            #     · 第 K+1 段在间隔内就绪 ⇒ 两边都等到满间隔才发（间隔 = 目标值）
            #     · 第 K+1 段来得比间隔晚 ⇒ 两边都立刻发（间隔 = 生成间隔）
            #   ⇒ **段间隔与框架完全一致，不存在"绕过间隔"**
            #   ⇒ 真正的差别只在第一段的时机：框架要等全部生成完，
            #     我们第一段生成好就发，于是后续各段的绝对时刻一起前移。
            #
            #   （写成"就绪前补差值"而不是"发送后无条件 sleep"，
            #     是为了不在没有内容可发的时候占住流式读取循环。）
            lo, hi = self._frame_delay()
            if hi > 0:
                now = time.monotonic()
                if self._last_seg_ts is not None:
                    wait = random.uniform(lo, hi) - (now - self._last_seg_ts)
                    if wait > 0:
                        await asyncio.sleep(wait)
                self._last_seg_ts = time.monotonic()
            else:
                self._last_seg_ts = None

            self._stats["early_sent"] += 1
            es = self._stats
            if es["first_seg_s"] is None:
                es["first_seg_s"] = 0.0

    # ══════════════════════════════════════════════════════════
    # L5 自动思考注入
    # ══════════════════════════════════════════════════════════
    def _install_request_hook(self) -> None:
        """patch `_build_request_kwargs` 注入思考参数。

        框架原文（utils/model_clients.py:61-73）：
            extra_body = section_advanced.get("extra_body")
            if extra_body: kwargs["extra_body"] = extra_body
        我们在这里合并/覆盖 —— 不改任何配置文件，热重载即生效。
        """
        try:
            from core.utils import model_clients as mc
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 导入 model_clients 失败")
            return

        plugin = self

        def factory(original):
            def build(self, request, **overrides):
                kwargs = original(self, request, **overrides)

                # ★ 请求体兼容加固：把空白 content 规范成 None
                #   （Gemini 等网关不接受 content:""，会让该模型持续不可用）
                if plugin.normalize_empty_content and isinstance(kwargs.get("messages"), list):
                    from .request_compat import normalize_messages
                    n = normalize_messages(kwargs["messages"])
                    if n:
                        plugin._stats["content_normalized"] = (
                            plugin._stats.get("content_normalized", 0) + n)

                if not plugin.thinking_enabled:
                    return kwargs
                decision = request.__dict__.get("_accel_thinking")
                if decision is None:
                    return kwargs

                extra = dict(kwargs.get("extra_body") or {})
                if decision.enabled:
                    extra.update(build_thinking_extra_body(plugin.thinking_style, decision.effort))
                elif plugin.thinking_inject_nothink:
                    extra.update(build_nothinking_extra_body(plugin.thinking_style))
                else:
                    return kwargs
                kwargs["extra_body"] = extra
                return kwargs
            return build

        h = PatchHandle("auto_thinking")
        if install(h, mc.OpenAICompatibleLLMClient, "_build_request_kwargs", factory) is not None:
            self.patches.add(h)
            logger.info("[accel] 请求钩子已安装（自动思考=%s 规范空白=%s）",
                        self.thinking_enabled, self.normalize_empty_content)

    def _install_parallel_tools(self) -> None:
        """同轮工具并行执行（默认关）。

        ★ 只并行"执行工具"这一段；其余全部保持串行且按序：
          - `max_tool_calls_per_turn` 超限的告警条目：原地保留
          - `ON_TOOL_RESULT` 钩子：**串行、按序**（插件会在这里 event.stop()，
            框架的实现会因此"提前返回 + 只写部分 tool_results"，
            session_merger 的注释明确点出过这个时序）
          - `assemble_result()`：串行、按序
          - `resp.tool_results` 的写入顺序：**与 tool_calls 对齐**（乱了会 400）

        ⇒ 所以并行只发生在"调用工具函数"这一层，可观测行为与串行版**完全一致**，
          区别只是"等待时间"。
        """
        try:
            from core.agent.func_tool_manager import FuncToolManager
            from core.provider.llm_model import LLMResponse  # noqa: F401
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 导入 func_tool_manager 失败，跳过工具并行")
            return

        import json as _json
        from .parallel_tools import ToolGate

        try:
            from core.utils.tool_utils import BaseTool  # noqa: F401
        except Exception:  # noqa: BLE001
            pass

        gate = self._tool_gate
        plugin = self

        def factory(original):
            async def execute_tool(self, event, resp, tool_set=None):
                calls = getattr(resp, "tool_calls", None) or []

                # 先读框架自己的两个配置（与原实现一致）
                max_calls = self.kira_config.get_config("bot_config.agent.max_tool_calls_per_turn")
                try:
                    max_calls = int(max_calls)
                except (TypeError, ValueError):
                    max_calls = 5

                to_run = calls[:max_calls] if max_calls >= 0 else []
                ok, reason = gate.can_parallelize(to_run)
                if not ok:
                    return await original(self, event, resp, tool_set=tool_set)

                plugin._stats["parallel_batches"] = plugin._stats.get("parallel_batches", 0) + 1
                logger.info("[accel] 同轮工具并行：%s", reason)

                # ── 阶段一：并发执行（只这一层并行）──
                timeout = self.kira_config.get_config("bot_config.agent.tool_call_timeout")
                try:
                    timeout = float(timeout)
                    if timeout <= 0:
                        timeout = None
                except (TypeError, ValueError):
                    timeout = 60

                def parse_args(tc):
                    raw = (tc.get("function") or {}).get("arguments") or ""
                    try:
                        return {} if not raw.strip() else _json.loads(raw)
                    except Exception:  # noqa: BLE001
                        return {}

                async def run_one(tc):
                    fn = tc.get("function") or {}
                    name = fn.get("name")
                    args = parse_args(tc)
                    if not (tool_set and name in tool_set):
                        return {"__error": f"Tool {name} not implemented"}
                    try:
                        inst = tool_set.get(name)
                        coro = inst.execute(event, **args)
                        import asyncio as _a
                        return await (_a.wait_for(coro, timeout) if timeout else coro)
                    except Exception as e:  # noqa: BLE001 —— 与框架一致：任何异常都变成结果
                        return {"__error": f"Failed to call tool '{name}': {e}"}

                import asyncio as _a
                raw_results = await _a.gather(
                    *[run_one(tc) for tc in to_run], return_exceptions=False)

                # ── 阶段二：按序做"不能并行"的部分（与框架逐行等价）──
                from asyncio import wait_for, TimeoutError as AsyncTimeoutError
                from core.agent.tool import ToolResult
                from core.plugin.plugin_handlers import event_handler_reg, EventType
                import core.agent.func_tool_manager as _ftm

                for idx, tool_call in enumerate(calls):
                    tool_call_id = tool_call.get("id")
                    name = (tool_call.get("function") or {}).get("name")

                    if max_calls >= 0 and idx >= max_calls:
                        warn_msg = (f"Tool call limit exceeded: maximum {max_calls} "
                                    f"tool calls per turn, skipping tool '{name}'.")
                        _ftm.tool_logger.warning(warn_msg)
                        resp.tool_results.append({
                            "role": "tool", "tool_call_id": tool_call_id,
                            "name": name, "content": warn_msg,
                        })
                        continue

                    result = raw_results[idx]
                    if isinstance(result, dict) and "__error" in result:
                        result = {"error": result["__error"]}
                        _ftm.tool_logger.error(result["error"])

                    tool_result_obj = result if isinstance(result, ToolResult) else ToolResult(str(result))

                    # ON_TOOL_RESULT：串行、按序（保持 event.stop 语义）
                    for handler in event_handler_reg.get_handlers(event_type=EventType.ON_TOOL_RESULT):
                        await handler.exec_handler(event, tool_result_obj)
                        if event.is_stopped:
                            logger.info("Event stopped while ON_TOOL_RESULT stage")
                            return

                    content = await tool_result_obj.assemble_result()
                    _ftm.tool_logger.info(f"tool_result: {content}")
                    resp.tool_results.append({
                        "role": "tool", "tool_call_id": tool_call_id,
                        "name": name, "content": content,
                    })
            return execute_tool

        from .patches import PatchHandle, install
        h = PatchHandle("parallel_tools")
        if install(h, FuncToolManager, "execute_tool", factory) is not None:
            self.patches.add(h)
            logger.info("[accel] 同轮工具并行已安装（白名单=%s 黑名单=%d条 全字匹配=%s）",
                        gate.whitelist or "全部", len(gate.blacklist), gate.match_exact)

    def _frame_delay(self) -> tuple[float, float]:
        """读框架的消息发送间隔（用户自己的配置）。

        框架在 MessageProcessor.__init__ 里一次性读取：
            self.min_message_delay = bot_config.min_message_delay (默认 2)
            self.max_message_delay = bot_config.max_message_delay (默认 5)
        我们直接取这两个属性 —— 这样用户调 KiraAI 的配置就能直接生效，
        插件不再引入第二个"要调的地方"。
        """
        mp = getattr(self.ctx, "message_processor", None)
        if mp is None:
            return 0.0, 0.0          # 拿不到就不等（安全默认）
        try:
            lo = float(getattr(mp, "min_message_delay", 0.0) or 0.0)
            hi = float(getattr(mp, "max_message_delay", lo) or lo)
            return (min(lo, hi), max(lo, hi))
        except Exception:  # noqa: BLE001
            return 0.0, 0.0

    def _install_early_sent_strip(self) -> None:
        """发送时剥离"已抢先发出的段"，避免重复发送。

        ★ 为什么不在构造响应时就剥离（那是我的第一版做法，有兼容问题）：
          实测框架内置 kira-ai 插件（builtin_plugins/kira-ai/main.py:111）和
          sustained-chat 插件（main.py:1681）都会在 ON_LLM_RESPONSE 里
          把 `resp.text_response` 当作**模型的完整输出**来用。
          提前剥离会让它们只看到尾巴 ⇒ XML 校验误判、或误判"AI 没说话"。

          所以现在：`resp.text_response` 保持完整；"哪些段已发出"记在
          响应对象的私有标记里；真正发送时（本函数）才把它们去掉。

        插在 `MessageProcessor.send_xml_messages` 上（它是类方法，可干净 patch）：
          - 正常轮次：剥离后交给原实现发送剩余部分
          - 全部发完：传 "<msg/>"，框架解析为空消息，不会重复发
        """
        try:
            from core.message_manager import MessageProcessor
            from .early_sent import early_sent_count, strip_early_sent
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 导入 MessageProcessor 失败，跳过发送层剥离")
            return

        plugin = self

        def factory(original):
            async def send_xml_messages(self, event, xml_data, tag_set):
                resp = getattr(plugin, "_current_resp", None)
                n = early_sent_count(resp) if resp is not None else 0
                if n > 0:
                    xml_data = strip_early_sent(xml_data, n)
                    # 记录一下：本条只发剩余部分（纯观测用）
                    plugin._stats["strip_calls"] = plugin._stats.get("strip_calls", 0) + 1
                try:
                    return await original(self, event, xml_data, tag_set)
                finally:
                    # 本轮已消费完，清掉标记，避免影响下一步/下一轮
                    if resp is not None:
                        resp.__dict__.pop("_accel_early_sent_count", None)
            return send_xml_messages

        h = PatchHandle("early_sent_strip")
        if install(h, MessageProcessor, "send_xml_messages", factory) is not None:
            self.patches.add(h)
            logger.info("[accel] 发送层已接管（剥离已抢发段，避免重复发送）")

    # ══════════════════════════════════════════════════════════
    # 侧边栏面板
    # ══════════════════════════════════════════════════════════
    @register.page(
        "/index",
        # ★ 图标改用插件自带的 SVG 文件（框架支持：PageMenu.icon 给相对路径时，
        #   会通过 /api/plugins/<id>/menu-icon/<route> 提供）。这样侧边栏菜单里
        #   显示的是与插件图标同一套视觉，而不是通用图标字体里的某个图标。
        menu=PageMenu(
            label={"zh": "提速器", "en": "Accelerator"},
            icon="icon.svg",
            order=95,
        ),
    )
    def page(self):
        return PluginPage.from_folder("./web")

    # ══════════════════════════════════════════════════════════
    # 壁纸服务（面板挂载在 /page/... 下，无法直接访问插件目录 ⇒ 必须走 API）
    # ══════════════════════════════════════════════════════════
    @register.api(method="GET", path="/wallpapers", auth=True)
    async def api_wallpapers(self):
        """列出可用壁纸（面板据此轮换）。"""
        from . import wallpapers_api as wp
        return {"files": wp.discover()}

    @register.api(method="GET", path="/wallpapers/{name}", auth=True)
    async def api_wallpaper_file(self, name: str):
        """返回一张壁纸文件。★ 安全：只认 discover() 过的文件名，不做路径拼接。"""
        from fastapi.responses import Response
        from . import wallpapers_api as wp

        path = wp.resolve(name)
        if path is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="wallpaper not found")
        data = path.read_bytes()
        return Response(
            content=data,
            media_type=wp.mime_for(name),
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @register.api(method="POST", path="/breaker/reset", auth=True)
    async def api_breaker_reset(self, payload: dict | None = None):
        """手动重置熔断的接管点（面板按钮调用）。

        payload: {"name": "stream_engine"} 或 {"all": true}
        """
        from .patches import breaker_for
        names = [h["name"] for h in self.patches.health()]
        if payload and payload.get("all"):
            for n in names:
                breaker_for(n).reset()
            return {"reset": names}
        target = (payload or {}).get("name", "")
        if target not in names:
            return {"error": f"unknown patch: {target}"}
        breaker_for(target).reset()
        return {"reset": [target]}

    # ══════════════════════════════════════════════════════════
    # API / 工具
    # ══════════════════════════════════════════════════════════
    @register.api(method="GET", path="/health", auth=True)
    async def api_health(self):
        sid = self._current_sid
        d = self.thinking.last_decision(sid) if sid else None
        return {
            "stats": self._stats,
            "patches": self.patches.health(),
            "thinking_now": ({
                "enabled": d.enabled, "score": d.score,
                "signals": d.signals, "effort": d.effort,
                # 供面板显示"相对基线在怎么判断"
                "context_baseline": int(self.thinking.context_baseline(sid)) if sid else 0,
                "ratio_on": self.thinking.context_ratio_on,
                "ratio_off": self.thinking.context_ratio_off,
            } if d else None),
        }

    @register.tool(
        "accel_report",
        "报告 KiraAI 提速器状态：已生效的优化项、抢先发送与自动思考统计（用于排查 为什么这轮很慢）",
        # ★ 刻意**不写 `required`**（而不是写 `required: []`）：
        #   空数组是部分网关（Gemini 的函数声明校验尤其严）拒收的模式，
        #   而"没有必填参数"用**省略**表达与"空数组"完全等价。
        #   框架自带工具与市场插件里都没有"无参数工具"的先例，
        #   所以这里取最保守的写法。
        {"type": "object", "properties": {}},
    )
    async def accel_report(self, event, **_) -> str:
        s = self._stats
        lines = [
            f"轮数={s['turns']} 步数={s['steps']}",
            f"抢先发送={s['early_sent']} 段；工具失败信号={s['tool_signals']} 次",
            f"自动思考：开={s['thinking_on']} 关={s['thinking_off']} 超预算拦截={s['thinking_skipped_budget']}",
        ]
        for h in self.patches.health():
            br = breaker_for(h["name"])
            flag = "已熔断⚠" if h["tripped"] else ("生效" if h["active"] else "未启用")
            lines.append(f"接管点 {h['name']}: {flag}（失败 {h['failures']} 次）")
        return "\n".join(lines)
