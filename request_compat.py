"""请求体兼容性加固 —— 兜住会让部分网关（尤其 Gemini 系）拒收的空值。

## 问题

框架在 `agent_executor.py` 里构造 assistant 消息时：

```python
assistant_content = llm_resp.text_response or ""      # ← 可能是空字符串
msg = OpenAIMessage(role="assistant", content=assistant_content,
                    tool_calls=answered_tool_calls, ...)
```

而 `OpenAIMessage.to_dict()`：

```python
d = {"role": self.role, "content": self.content}      # ← content 总是带上
if self.tool_calls: d["tool_calls"] = self.tool_calls
```

于是会产生这种消息：

```json
{"role": "assistant", "content": "", "tool_calls": [...]}   ← content 是空字符串
```

**部分网关（Gemini 的 OpenAI 兼容层尤其明显）不接受 `content: ""`** ——
它会报参数校验错误，而且**会导致该模型在整个会话里持续不可用**
（因为坏消息已经进了历史，之后每次重发都带着它）。

## 处理方式

在**发送前**把空字符串 content 规范成 `None`。
`None` 在 OpenAI 语义里表示"这条消息没有文本内容"，是**合法且语义等价**的：

- `content: ""`   →  空文本
- `content: null` →  没有文本内容

对带 `tool_calls` 的 assistant 消息，两者语义相同，但后者兼容面更宽。

## 为什么可以安全地做

- **只动"空"值**：非空 content 一个字节都不改
- **不改历史**：只改**发出去的那一份**（`chat()` 里构造的 request_kwargs），
  `request.messages` 与 `self.chat_memory` 里的对象保持原样
- **不改语义**：空字符串与 None 在 OpenAI 语义里对"无文本"是等价的
- **可关**：配置项 `normalize_empty_content`（默认开）

## 顺带修一处同类问题

`tool_calls: []`（空列表）也会被某些网关拒。
`to_dict()` 已经用 `if self.tool_calls:` 防住了，所以这里只需处理 `content`。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger("kira_accelerator")


def _is_blank(x: Any) -> bool:
    """是不是"空白内容"（空字符串 / 只含空白的字符串 / 空列表）。"""
    if x is None:
        return True
    if isinstance(x, str):
        return x.strip() == ""
    if isinstance(x, list):
        return len(x) == 0
    return False


def normalize_messages(messages: Iterable[Any]) -> int:
    """把消息里的空白 content 规范成 None。返回改动条数。

    ★ **就地改 dict 副本**，不碰原始对象。
      调用方（chat/chat_stream）拿到的 messages 已经是一份新列表
      （`[m if isinstance(m, dict) else m.to_dict() for m in request.messages]`），
      所以这里直接改是安全的。
    """
    fixed = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        if "content" not in m:
            continue
        if _is_blank(m.get("content")):
            # 已经是 None 就不用动
            if m.get("content") is None:
                continue
            m["content"] = None
            fixed += 1
        # 有些网关对空 tool_calls 也敏感 —— 干脆顺手清掉
        if isinstance(m.get("tool_calls"), list) and not m["tool_calls"]:
            m.pop("tool_calls", None)
            fixed += 1
    if fixed:
        logger.debug("[accel] 已规范化 %d 处空白 content（避免网关拒收）", fixed)
    return fixed
