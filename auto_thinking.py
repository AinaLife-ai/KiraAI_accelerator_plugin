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
        tool_count_threshold: int = 6,
        context_char_threshold: int = 6000,
        long_message_chars: int = 220,
        effort_map: Optional[dict[str, str]] = None,
        effort_low: float = 2.0,
        effort_high: float = 4.5,
        max_per_minute: int = 60,
        max_sessions: int = 500,
    ):
        self.threshold = threshold
        self.tool_count_threshold = tool_count_threshold
        self.context_char_threshold = context_char_threshold
        self.long_message_chars = long_message_chars
        self.effort_low = effort_low
        self.effort_high = effort_high
        self.max_per_minute = max_per_minute
        self.effort_map = effort_map or {}

        # sid -> 状态。★ 上限保护：会话多了这些 dict 会缓慢增长，超过就清最旧。
        self._last_tool_error: dict[str, bool] = {}
        self._last_decision: dict[str, ThinkingDecision] = {}
        self._opened: dict[str, list[float]] = {}
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

        # 3) 工具数量
        tool_count = len(getattr(request, "tools", None) or [])
        if tool_count >= self.tool_count_threshold:
            score += 1.0
            signals.append(f"工具多({tool_count})")

        # 4) 上下文规模
        ctx_chars = self._context_chars(request)
        if ctx_chars >= self.context_char_threshold:
            score += 0.5
            signals.append(f"上下文长({ctx_chars})")

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
        for d in (self._last_decision, self._last_tool_error, self._opened):
            for s in list(d):
                if s not in keep:
                    d.pop(s, None)

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
        total = 0
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
