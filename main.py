"""KiraAI 加速器 —— 在不影响质量与兼容的前提下提速。

分层设计：
  L1 观测层    始终可开，零风险：记录每步耗时/token/抢先发送情况
  L2 请求优化  纯官方 API：工具裁剪（改 tool_set）、结果瘦身、节流收敛
  L3 接管层    护栏式打补丁：HTTP 客户端复用 / 记忆落盘异步化
  L4 流式引擎  ★ 强制流式（任意 provider）+ 抢先发送（首字即刻可见）
  L5 自动思考  ★ 对标并超越 Alife：多信号打分决定这轮要不要开思考

接管铁律（patches.py 实现）：幂等 / 精确还原 / 熔断旁路。

兼容性保证（已核对插件市场 7 个重点插件的真实源码）：
  - 插件依赖 `client.model.*`（sustained-chat queue_merge.py:136、
    midflight main.py:261、model-fallback main.py:35）⇒ 代理类透传 `.model`
  - 插件依赖 `ON_LLM_REQUEST` 改 tool_set ⇒ 我们用 Priority.LOW 后执行，不抢
  - 插件依赖 `ON_MESSAGE_SENT` ⇒ 抢先发送时我们自己补广播
  - 插件依赖 `req.tool_set` 而非 `req.tools` ⇒ 我们也只改 tool_set
"""
from __future__ import annotations

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
        c_app = cfg.get("section_appearance", {}) or {}

        self.observe_enabled = bool(c_obs.get("enabled", True))
        self.observe_window = int(c_obs.get("window", 50))

        self.tame_send_delay = bool(c_req.get("tame_send_delay", False))
        self.send_delay_target = float(c_req.get("send_delay_target", 0.25))

        self.takeover_build_client = bool(c_tak.get("reuse_http_client", False))

        # L4
        self.force_stream = bool(c_str.get("force_stream", False))
        self.early_send = bool(c_str.get("early_send", False))
        self.stream_providers = list(c_str.get("providers", []) or [])
        self.stream_gap = float(c_str.get("gap", 0.25))

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
            tool_count_threshold=int(c_thk.get("tool_count_threshold", 6)),
            context_char_threshold=int(c_thk.get("context_char_threshold", 6000)),
            long_message_chars=int(c_thk.get("long_message_chars", 220)),
            effort_low=float(c_thk.get("effort_low", 2.0)),
            effort_high=float(c_thk.get("effort_high", 4.5)),
            max_per_minute=int(c_thk.get("max_per_minute", 60)),
        )

        # ── 运行时状态 ──
        self._samples: deque[dict] = deque(maxlen=self.observe_window)
        self._orig_send_delay: Optional[tuple[float, float]] = None
        self._client_cache: dict[tuple, Any] = {}
        self._current_sid: Optional[str] = None
        self._current_event: Any = None
        self._current_tag_set: Any = None
        self._proxy_cache: dict[tuple, LLMClientProxy] = {}
        self._stats = {
            "turns": 0, "steps": 0,
            "tool_signals": 0,
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
        if self.tame_send_delay:
            self._install_send_delay()
        if self.takeover_build_client:
            self._install_client_cache()
        if self.force_stream or self.early_send:
            self._install_stream_engine()
        if self.thinking_enabled:
            self._install_thinking_injector()

    async def terminate(self) -> None:
        self.patches.uninstall_all()
        self._restore_send_delay()
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
        try:
            text = getattr(tool_result, "text", "") or ""
            head = text[:200].lower()
            if "error" in head or "失败" in head or "denied" in head or "not allowed" in head:
                self.thinking.note_tool_error(sid)
            else:
                self.thinking.note_tool_ok(sid)
        except Exception:  # noqa: BLE001
            logger.exception("[accel] 工具结果信号读取失败（已忽略）")

    # ── 发送节流收敛（解除 session_lock 被 sleep 占用）──
    def _install_send_delay(self) -> None:
        mp = getattr(self.ctx, "message_processor", None)
        if mp is None:
            logger.warning("[accel] 拿不到 message_processor，跳过节流收敛")
            return
        self._orig_send_delay = (mp.min_message_delay, mp.max_message_delay)
        mp.min_message_delay = self.send_delay_target
        mp.max_message_delay = self.send_delay_target
        logger.info("[accel] 发送节流 %s → %.2fs/段（卸载时还原）",
                    self._orig_send_delay, self.send_delay_target)

    def _restore_send_delay(self) -> None:
        mp = getattr(self.ctx, "message_processor", None)
        if mp is not None and self._orig_send_delay is not None:
            mp.min_message_delay, mp.max_message_delay = self._orig_send_delay
        self._orig_send_delay = None

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
        emit = None
        if self.early_send:
            async def _emit(seg: str) -> None:
                await self._emit_segment(seg)
            emit = _emit
        return StreamEngine(force_stream=self.force_stream, emit=emit)

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

            await asyncio.sleep(self.stream_gap)
            self._stats["early_sent"] += 1
            es = self._stats
            if es["first_seg_s"] is None:
                es["first_seg_s"] = 0.0

    # ══════════════════════════════════════════════════════════
    # L5 自动思考注入
    # ══════════════════════════════════════════════════════════
    def _install_thinking_injector(self) -> None:
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
            logger.info("[accel] 自动思考已安装（风格=%s 阈值=%.1f）",
                        self.thinking_style, self.thinking.threshold)

    # ══════════════════════════════════════════════════════════
    # 侧边栏面板
    # ══════════════════════════════════════════════════════════
    @register.page(
        "/index",
        # ★ 图标改用插件自带的 SVG 文件（框架支持：PageMenu.icon 给相对路径时，
        #   会通过 /api/plugins/<id>/menu-icon/<route> 提供）。这样侧边栏菜单里
        #   显示的是与插件图标同一套视觉，而不是通用图标字体里的某个图标。
        menu=PageMenu(
            label={"zh": "加速器", "en": "Accelerator"},
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
            } if d else None),
        }

    @register.tool(
        "accel_report",
        "报告 KiraAI 加速器状态：已生效的优化项、抢先发送与自动思考统计（用于排查 为什么这轮很慢）",
        {"type": "object", "properties": {}, "required": []},
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
