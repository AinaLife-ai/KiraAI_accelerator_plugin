"""兼容性修复：给下游保留「模型原始输出」，同时不破坏框架的"不重复发送"。

## 问题

抢先发送后，我们已经把前面几段发给了用户。为了让框架不要重复发送，
`resp.text_response` 被替换成了"还没发出去的那部分"。

但这会**误导下游**——实测有两处依赖 `resp.text_response` 是**模型的完整输出**：

1. **框架内置 `kira-ai` 插件**（`builtin_plugins/kira-ai/main.py:111`）：
   在 `ON_LLM_RESPONSE` 里对整个回复做 XML 校验，解析失败时用另一个模型去修。
   只拿到尾巴 ⇒ `<root>` 包不住 ⇒ 误判成解析失败 ⇒ **多花一次 LLM 调用去"修"一个本来没坏的东西**。

2. **sustained-chat 插件**（`main.py:1681`）：
   `ai_text = (resp.text_response or "").strip()` 用来判断 AI 是否发了内容（决定是否继续维持对话窗口）。
   只看到尾巴 ⇒ 可能**误判为"AI 没说话"**。

## 修法

把"给框架发送用的文本"与"给下游看的完整文本"**分开**：

- `resp.text_response`  ← **模型的完整输出**（下游看到的就是它）
- `resp.__dict__` 上的私有标记 ← 告诉**我们自己的发送层**："前 N 段已经发出去了"

真正的剥离发生在**发送时**（`_strip_early_sent`），而不是在构造响应时。
这样：
  - 下游插件的语义**完全不变**（拿到的就是完整输出）
  - 框架仍然**不会重复发送**已发出的段

### ⚠️ 标记绝不能放进 `resp.tool_results`（我踩过的坑）

第一版我把标记 append 进了 `resp.tool_results`，理由是"框架对没有 tool_calls 的响应
不会读它"。**这个推理是错的**：模型完全可能"先说一句，再决定调工具"——同一个响应里
`text_response` 有已抢发的段、`tool_calls` 又非空。这时 `agent_executor` 会走
tool_calls 分支并执行：

    tool_msgs = [OpenAIMessage(**r) for r in llm_resp.tool_results]

标记 dict 没有 `role` 字段 ⇒ pydantic 抛 `ValidationError` ⇒ **整个回合崩掉**。

所以标记只放 `resp.__dict__`——框架从不遍历它，绝无副作用。
"""
from __future__ import annotations

# 历史遗留：曾经的标记键名。**已废弃** —— 标记现在只放 resp.__dict__，
# 放 tool_results 会让框架的 OpenAIMessage(**r) 崩（见 mark_early_sent 的说明）。
MARKER_KEY = "_accel_early_sent"

# 正则：匹配一个**已闭合**的 <msg> 段（与 stream_first 保持一致）
import re  # noqa: E402

_MSG_CLOSED = re.compile(r"<msg(?:\s[^>]*)?>.*?</msg>|<msg(?:\s[^>]*)?/>", re.DOTALL)


def mark_early_sent(resp, segments: list[str], full_text: str) -> None:
    """在响应上记录"这些段已经抢先发出去了"。

    ★★ 只写**私有属性**，绝不碰 `resp.tool_results`！

    我第一版把标记塞进了 `tool_results`，注释还写着"框架对没有 tool_calls 的响应
    不会读它"——**这个推理是错的**：模型完全可能"先说一句，再决定调工具"，
    于是同一个响应里 `text_response` 有已抢发的段、`tool_calls` 又非空。
    这时 `agent_executor` 会走 tool_calls 分支并执行：

        tool_msgs = [OpenAIMessage(**r) for r in llm_resp.tool_results]

    我的标记 dict 没有 `role` 字段 ⇒ pydantic 抛 `ValidationError`
    ⇒ **整个回合崩掉**。

    所以标记只放 `resp.__dict__`：框架从不遍历它，绝无副作用。
    """
    if not segments:
        return
    try:
        resp.__dict__["_accel_full_text"] = full_text or ""
        resp.__dict__["_accel_early_sent_count"] = len(segments)
    except Exception:  # noqa: BLE001
        pass


def early_sent_count(resp) -> int:
    """读取"已经抢先发出了几段"。"""
    try:
        return int(resp.__dict__.get("_accel_early_sent_count", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def strip_early_sent(text: str, count: int) -> str:
    """从完整文本里去掉**前 count 个已闭合的段**，返回仍需框架发送的部分。

    - 全部发完 ⇒ 返回 "<msg/>"（框架解析为空消息，不会发东西）
    - 一个都没发 ⇒ 原样返回
    """
    if count <= 0 or not text:
        return text
    out = text
    removed = 0
    while removed < count:
        m = _MSG_CLOSED.search(out)
        if not m:
            break
        out = out[:m.start()] + out[m.end():]
        removed += 1
    return out if out.strip() else "<msg/>"
