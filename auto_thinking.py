"""自动按需思考 —— 对齐 Alife 的 OccupationNotepad 机制，并做得更好。

## Alife 是怎么做的（源码依据）

`XmlFunctionCaller.cs:114-115, 235-248` + `OpenAILanguageModel.cs:51, 219-222`：

```csharp
// 1) 模块通过思考请求器"租用"思考开关，并给出原因
thinkingOccupationMarker = ChatBot.LanguageModel.GetThinkingRequester().Rent("即将使用隐式功能");

// 2) 模型请求时统一查一次：有没有人租着？
bool thinking = GetThinkingRequester().IsOccupied;
if (thinking && !string.IsNullOrEmpty(Configuration.reasoningEffort))
    payload["reasoning_effort"] = Configuration.reasoningEffort;
string extraBody = thinking ? Configuration.extraBody : Configuration.extraBodyNotThinking;
```

触发点（`XmlFunctionCaller.cs` 内）：
- `:275` 「即将使用隐式功能」—— AI 调用某个隐式 tag 时
- `:235` 「隐式功能激活中」—— 上一轮注入过工具文档
- `:198` 「需要处理函数异常」—— 工具执行报错
- `:188` 「重新激活隐式功能」

## 我们的做法（并做出三点改进）

| # | Alife | 本实现 |
|---|---|---|
| 1 | 只按「是否要调工具」判断 | **多信号打分**：意图复杂度 + 上下文规模 + 触发词 + 上轮工具失败 + 工具数量 |
| 2 | 思考开关是全局布尔 | **按 sid 独立**，不同会话互不干扰 |
| 3 | reasoning_effort 是固定配置 | **按分数分级**（low/medium/high），并可配阈值 |
| 4 | — | **预算控制**：每分钟最多开 N 次，防止被"请思考"类提示词刷爆 token |
| 5 | — | **可观测**：面板显示本轮为什么开/不开思考 + 命中信号 |

## 怎么把开关塞进请求

KiraAI 的 `OpenAICompatibleLLMClient._build_request_kwargs()` 会读取：
```python
extra_body = model_config.get("section_advanced", {}).get("extra_body")   # model_clients.py:61
if extra_body: kwargs["extra_body"] = extra_body                           # :72-73
```
所以 **patch 这个方法**即可在不改任何配置文件的前提下注入思考参数。

⚠️ reasoning 的**字段名因厂商而异**，所以做成可配（默认 `compatible` 会自动挑一套最通用的）：
    - `{"reasoning_effort": "medium"}`   —— OpenAI o 系列 / 多数兼容网关
    - `{"enable_thinking": true}`        —— Qwen / 阿里系
    - `{"thinking": {"type": "enabled"}}` —— Anthropic / 部分网关
    - `{"chat_template_kwargs": {"enable_thinking": true}}` —— vLLM 部署的 Qwen
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

# ── 触发词：命中即认为"这轮需要想一下" ──
COMPLEX_HINTS = (
    "为什么", "怎么", "如何", "解释", "分析", "推理", "证明", "推导", "设计", "架构",
    "比较", "对比", "区别", "优缺点", "评估", "方案", "计划", "步骤", "原因",
    "why", "how", "explain", "analyze", "reason", "derive", "prove", "design",
    "compare", "versus", " vs ", "evaluate", "plan", "steps", "trade-off", "tradeoff",
    "debug", "报错", "错误", "异常", "失败", "bug", "fix", "修复", "优化", "重构",
)

# ── 简单寒暄：命中即认为"不用想"（优先于复杂词判断）──
SIMPLE_HINTS = (
    "你好", "在吗", "哈哈", "谢谢", "再见", "晚安", "早安", "早", "嗯", "好的", "收到",
    "hi", "hello", "thanks", "thank you", "bye", "good night", "ok", "okay", "lol",
)


class ThinkingDecision:
    """一次思考判定的结果。"""

    __slots__ = ("enabled", "score", "signals", "effort")

    def __init__(self, enabled: bool, score: float, signals: list[str], effort: str):
        self.enabled = enabled
        self.score = score
        self.signals = signals
        self.effort = effort

    def __repr__(self):
        return (f"ThinkingDecision(enabled={self.enabled}, score={self.score:.1f}, "
                f"effort={self.effort!r}, signals={self.signals})")


class AutoThinkingController:
    """多信号打分的自动思考开关。

    打分规则（全部可配，默认值在 schema.json 里）：
        +2.0  用户消息命中复杂触发词
        +1.5  上一轮出现了工具执行错误
        +1.0  可用工具很多（>= tool_count_threshold，说明可能要走多步）
        +0.5  上下文很长（>= context_char_threshold，容易迷路）
        +0.5  单条用户消息很长（>= long_message_chars，信息量大）
        -3.0  命中简单寒暄（直接压到阈值下）
        score >= threshold ⇒ 开思考
    """

    def __init__(
        self,
        threshold: float = 2.0,
        long_message_chars: int = 220,
        # ── 上下文信号：相对**自己会话的基线**，不再用绝对字符数 ──
        #    绝对值对不同用户意义完全不同（新装用户 6000 就很长，
        #    装了插件的人每轮都 3 万+ ⇒ 信号永远是"真" ⇒ 等于偷偷把阈值挪走）。
        context_ratio_on: float = 1.6,     # 超过基线这个倍数 ⇒ 命中
        context_ratio_off: float = 1.15,   # 回落到这个倍数以下 ⇒ 取消命中（滞回，防抖）
        context_min_samples: int = 5,      # 攒够这么多样本才启用（否则无基线、不判定）
        # 基线 EMA 的更新系数。越大越"健忘"（单轮尖峰一过就把基线带跑，
        # 于是后续即使仍高于历史水平也不再算"长"）；越小越"稳"。
        # 0.15 = 单轮尖峰只把基线推 15% 的差值，代表"平时水平"。
        context_ema_alpha: float = 0.15,
        # ── 工具信号：换成「上一轮是否用过工具」（逐轮变化，真正的信号）──
        #    原来的「工具数 >= N」在一个会话里基本恒定，不是信号而是常数。
        score_last_turn_tool: float = 1.0,
        effort_map: Optional[dict[str, str]] = None,
        effort_low: float = 2.0,
        effort_high: float = 4.5,
        max_per_minute: int = 60,
        max_sessions: int = 500,
    ):
        self.threshold = threshold
        self.long_message_chars = long_message_chars
        self.context_ratio_on = float(context_ratio_on)
        self.context_ratio_off = float(context_ratio_off)
        self.context_min_samples = max(1, int(context_min_samples))
        self.context_ema_alpha = min(1.0, max(0.01, float(context_ema_alpha)))
        self.score_last_turn_tool = float(score_last_turn_tool)
        self.effort_low = effort_low
        self.effort_high = effort_high
        self.max_per_minute = max_per_minute
        self.effort_map = effort_map or {}

        # sid -> 状态。★ 上限保护：会话多了这些 dict 会缓慢增长，超过就清最旧。
        self._last_tool_error: dict[str, bool] = {}
        self._last_decision: dict[str, ThinkingDecision] = {}
        self._opened: dict[str, list[float]] = {}
        # 上下文基线（EMA）与"当前是否判定为长"（滞回状态）
        self._ctx_ema: dict[str, float] = {}
        self._ctx_samples: dict[str, int] = {}
        self._ctx_long: dict[str, bool] = {}
        # 上一轮是否用过工具（由 on_tool_result / on_final_result 维护）
        self._last_turn_tool: dict[str, bool] = {}
        self._turn_had_tool: dict[str, bool] = {}
        self.max_sessions = max_sessions

    # ── 外部信号接口（对标 Alife 的 Rent/Return）──
    def note_tool_error(self, sid: str) -> None:
        self._last_tool_error[sid] = True

    def note_tool_ok(self, sid: str) -> None:
        self._last_tool_error[sid] = False

    def last_decision(self, sid: str) -> Optional[ThinkingDecision]:
        return self._last_decision.get(sid)

    # ── 主判定 ──
    def decide(self, sid: str, request: Any) -> ThinkingDecision:
        signals: list[str] = []
        score = 0.0

        text = self._user_text(request)

        # 1) 简单寒暄优先（避免"你好"也被送去思考）
        if self._is_simple(text):
            signals.append("简单寒暄")
            score -= 3.0
        else:
            if self._has_complex(text):
                score += 2.0
                signals.append("复杂意图词")

        # 2) 上轮工具出错
        if self._last_tool_error.get(sid):
            score += 1.5
            signals.append("上轮工具失败")

        # 3) 上一轮用过工具 ⇒ 说明在做多步任务，这一轮很可能还要走多步
        #    （原来的"工具数 >= N"在一个会话里恒定不变，是常数而不是信号，
        #      它会恒定地+1.0，等于偷偷把阈值挪走）
        if self._last_turn_tool.get(sid):
            score += self.score_last_turn_tool
            signals.append("上轮用过工具")

        # 4) 上下文相对**自己会话的基线**是否明显变长
        ctx_chars = self._context_chars(request)
        ctx_sig = self._update_context_signal(sid, ctx_chars)
        if ctx_sig:
            score += 0.5
            signals.append(ctx_sig)

        # 5) 单条消息长度
        if len(text) >= self.long_message_chars:
            score += 0.5
            signals.append("消息长")

        enabled = score >= self.threshold

        # 预算控制：超了就强制关（防刷）
        if enabled and not self._budget_ok(sid):
            enabled = False
            signals.append("超预算")

        effort = self._pick_effort(score)
        decision = ThinkingDecision(enabled, score, signals, effort)
        self._last_decision[sid] = decision
        self._trim_sessions()
        return decision

    def _trim_sessions(self) -> None:
        """会话数超过上限时，丢弃一半最不活跃的记录（简单、够用）。"""
        if len(self._last_decision) <= self.max_sessions:
            return
        keep = set(sorted(self._last_decision, key=lambda s: self._opened.get(s, [0])[-1] if self._opened.get(s) else 0)[-self.max_sessions // 2:])
        for d in (self._last_decision, self._last_tool_error, self._opened,
                  self._ctx_ema, self._ctx_samples, self._ctx_long,
                  self._last_turn_tool, self._turn_had_tool):
            for s in list(d):
                if s not in keep:
                    d.pop(s, None)

    # ── 上下文信号：相对基线 + 滞回 ──
    def _update_context_signal(self, sid: str, ctx_chars: int) -> str:
        """返回信号描述（命中时），或空串（未命中/还在攒基线）。

        为什么用**相对基线**而不是绝对字符数：
          绝对值（比如 6000）对不同用户意义完全不同 —— 新装用户觉得"很长"，
          装了插件的人每轮 3 万+，于是这个信号**永远为真** ⇒ 零信息量，
          而且会恒定地把阈值往下挪。

        为什么用**滞回**（开 1.6× / 关 1.15× 两个不同阈值）：
          只用单一阈值时，上下文在临界点附近波动会导致每轮开关抖动。

        基线何时更新：**先判定、后更新**（否则本轮尖峰会立刻抬高基线，
        把自己这次命中抹掉）。
        """
        base = self._ctx_ema.get(sid)
        n = self._ctx_samples.get(sid, 0)

        # 攒够样本前不判定，只积累
        if base is None or n < self.context_min_samples:
            self._ctx_samples[sid] = n + 1
            self._ctx_ema[sid] = (ctx_chars if base is None
                                  else base + self.context_ema_alpha * (ctx_chars - base))
            return ""

        was_long = self._ctx_long.get(sid, False)
        ratio = (ctx_chars / base) if base > 0 else 0.0

        if was_long:
            if ratio < self.context_ratio_off:
                self._ctx_long[sid] = False            # 回落到关阈值以下 ⇒ 恢复
                hit = False
            else:
                hit = True
        else:
            if ratio > self.context_ratio_on:
                self._ctx_long[sid] = True             # 突破开阈值 ⇒ 命中
                hit = True
            else:
                hit = False

        # 先判定、后更新基线（EMA，平滑掉单轮尖峰）
        self._ctx_samples[sid] = n + 1
        self._ctx_ema[sid] = base + self.context_ema_alpha * (ctx_chars - base)

        if hit:
            return f"上下文偏离基线({ratio:.2f}×)"
        return ""

    def context_baseline(self, sid: str) -> float:
        """给面板看：该会话当前的上下文基线。"""
        return float(self._ctx_ema.get(sid, 0.0) or 0.0)

    # ── 工具信号：维护"上一轮是否用过工具" ──
    def note_turn_tool(self, sid: str) -> None:
        """本轮调用了工具（由 on_tool_result 调用）。"""
        if sid:
            self._turn_had_tool[sid] = True

    def commit_turn(self, sid: str) -> None:
        """本轮结束（由 on_final_result 调用）：把本轮的工具有无记为"上一轮"。"""
        if not sid:
            return
        self._last_turn_tool[sid] = bool(self._turn_had_tool.pop(sid, False))

    def _budget_ok(self, sid: str) -> bool:
        now = time.time()
        hist = [t for t in self._opened.get(sid, []) if now - t < 60]
        if len(hist) >= self.max_per_minute:
            self._opened[sid] = hist
            return False
        hist.append(now)
        self._opened[sid] = hist
        return True

    def _pick_effort(self, score: float) -> str:
        if score >= self.effort_high:
            return "high"
        if score >= self.effort_low:
            return "medium"
        return "low"

    @staticmethod
    def _is_simple(text: str) -> bool:
        if not text or len(text) > 40:
            return False
        low = text.lower().strip()
        return any(h in low for h in SIMPLE_HINTS)

    @staticmethod
    def _has_complex(text: str) -> bool:
        if not text:
            return False
        low = text.lower()
        return any(h in low for h in COMPLEX_HINTS)

    @staticmethod
    def _user_text(request: Any) -> str:
        parts = []
        for p in getattr(request, "user_prompt", None) or []:
            parts.append(getattr(p, "content", "") or "")
        return "\n".join(parts)

    @staticmethod
    def _context_chars(request: Any) -> int:
        """量"模型本轮真正会看到的上下文"。

        ★ 必须把 `user_prompt` 也算进来：判断发生在 ON_LLM_REQUEST，
          那一刻 `request.messages` 里**只有历史**，本轮刚收到的消息
          还在 `request.user_prompt` 里（框架到 assemble_prompt 才合并）。
          只量 messages 会导致"本轮消息把上下文撑大"要等到下一轮才被察觉 ——
          也就是判断滞后一轮。
        """
        total = 0
        for p in getattr(request, "user_prompt", None) or []:
            try:
                total += len(getattr(p, "content", "") or "")
            except Exception:  # noqa: BLE001
                pass
        for m in getattr(request, "messages", None) or []:
            try:
                c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            except Exception:  # noqa: BLE001
                c = ""
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total += len(part["text"])
        return total


# ── 把思考参数注入 extra_body ──
def build_thinking_extra_body(style: str, effort: str) -> dict:
    """按厂商风格生成"开思考"的请求体片段。

    ★ 关键事实（读框架源码 + 官方文档确认）：
      `reasoning_effort` 的**取值范围因厂商而异**，不是统一的 low/medium/high！
        - OpenAI o 系列：low / medium / high（还有 none / minimal / xhigh）
        - **DeepSeek V4：只有 high / max**（KiraAI 的 schema 就写了
          `"options": ["high","max"]`，见 core/provider/src/deepseek/schema.json）
        - 部分网关只认 none / high / max
      所以本函数会把内部档位(low/medium/high)**按风格映射**到厂商合法值，
      绝不直接把 medium 发给 DeepSeek（那会被静默忽略或报错）。

    style:
      - `compatible`（默认）：最通用的一套，多数兼容网关认
      - `reasoning_effort`：OpenAI o 系列（low/medium/high）
      - `deepseek`：**DeepSeek V4 专用**（high/max）—— 见下方说明
      - `enable_thinking`：Qwen / 阿里系
      - `thinking_object`：Anthropic / 部分网关
      - `vllm_chat_template`：vLLM 部署的 Qwen
    """
    # 内部档位 → 厂商合法值 的映射
    if style == "reasoning_effort":
        return {"reasoning_effort": {"low": "low", "medium": "medium", "high": "high"}.get(effort, "medium")}

    if style == "deepseek":
        # DeepSeek: thinking 是 extra_body 字段；reasoning_effort 是顶层参数（只能 high/max）
        # 框架自己就是这么发的（core/provider/src/deepseek/model_clients.py:55-71）：
        #   extra_body["thinking"] = {"type": "enabled"}; kwargs["reasoning_effort"] = "high"
        return {
            "thinking": {"type": "enabled"},
            "reasoning_effort": {"low": "high", "medium": "high", "high": "max"}.get(effort, "high"),
        }

    if style == "enable_thinking":
        return {"enable_thinking": True}

    if style == "thinking_object":
        return {"thinking": {"type": "enabled", "budget_tokens": _budget_for(effort)}}

    if style == "vllm_chat_template":
        return {"chat_template_kwargs": {"enable_thinking": True}}

    # compatible：只发最保险的两种拼写，且 reasoning_effort 用**厂商交集**
    # （high 在 OpenAI 与 DeepSeek 都合法；low/medium 在 DeepSeek 非法）
    return {
        "reasoning_effort": {"low": "low", "medium": "high", "high": "high"}.get(effort, "high"),
        "enable_thinking": True,
    }


def build_nothinking_extra_body(style: str) -> dict:
    """关思考（用于"这轮不需要想"时显式关掉，省 token 又更快）。

    ⚠️ 刻意保守：只发**最通用、最不容易 400** 的那一个字段。
      实测部分网关对 `reasoning_effort: "none"` 会报错，故不采用。
    """
    if style == "reasoning_effort":
        return {"reasoning_effort": "low"}          # 最低档而不是 none
    if style == "deepseek":
        # DeepSeek 关思考：只发 extra_body 的 thinking（不发 reasoning_effort）
        return {"thinking": {"type": "disabled"}}
    if style == "enable_thinking":
        return {"enable_thinking": False}
    if style == "thinking_object":
        return {"thinking": {"type": "disabled"}}
    if style == "vllm_chat_template":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {"enable_thinking": False}


def _budget_for(effort: str) -> int:
    return {"low": 1024, "medium": 4096, "high": 16384}.get(effort, 4096)
