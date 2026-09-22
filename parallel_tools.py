"""同轮工具并行执行 —— 让"同一轮内的多个独立工具"真正并发跑。

## 为什么安全性成立（不依赖对模型的任何信任）

**核心事实：这批工具在数学上可交换。**

读 3 个文件、grep 3 个模式——任意顺序、任意并发，**结果完全相同**。
所以并行它们不需要"模型判断正确"，只需要"这些操作本身没有顺序语义"。

反过来，`write_file` / `exec` / `memory_*` 这些**顺序携带语义**（谁先谁后影响结果），
所以必须串行——不是因为模型会搞错，而是因为顺序本身是信息。

## 为什么需要拦（而不是指望模型自己排好）

1. 框架提示词**从没告诉模型工具是串行还是并行**——模型对执行模型是盲的；
2. OpenAI 工具调用协议里**没有表达依赖的字段**——模型只能靠"分不分轮"暗示依赖；
3. 一旦模型以为执行是并行的，它**反而失去排序的动机**。

所以模型"批量调用"只是**图省事**，不是**判断过安全性**。安全性得由我们这边保证。

## 匹配规则（用户设计）

- **白名单留空 = 全部放行**，只由黑名单控制（默认就是这样，用户不必维护长名单）
- **白名单非空 = 只放行白名单里的**
- 黑名单**优先于**白名单（在黑名单里就一定串行）
- **全字匹配开关**（`match_exact`）：
  - **默认关** ⇒ 关键词命中即可（`exec` 能命中 `manage_background_exec`）
  - 打开 ⇒ 工具名必须完全相等（`exec` 只命中 `exec` 本身）

## 保守规则

- `len(tool_calls) <= 1` ⇒ 直接走原路（没并行收益）
- 只要有**一个**工具被判为不安全 ⇒ **整批退回串行**（最坏情况就是没加速）
- 结果**按原顺序**写回 ⇒ `tool_calls` 与 `tool_results` 的配对不会乱
- 每个工具仍受框架自己的超时约束
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger("kira_accelerator")

# ── 默认黑名单 ──
# 只收**已验证有副作用 / 有共享状态 / 有顺序语义**的工具。
# 不按名字猜（`manage_background_exec` 就是反例：名字像只读，实际 action=stop 会停任务）。
DEFAULT_BLACKLIST: tuple[str, ...] = (
    # ── 框架内置 ──
    "exec",                      # 跑命令，可能改环境
    "write_file",                # 写文件；同文件并发会互相覆盖
    "edit_file",                 # 改文件；同上
    "manage_background_exec",    # 名字像只读，实际会 stop 任务
    "memory_add",                # 改同一个记忆文件
    "memory_update",             # 同上
    "memory_remove",             # 同上
    "session_send",              # 跨会话发消息，有投递顺序
    # ── 插件：语义未知，一律串行 ──
    "manage_ignore",             # 改屏蔽名单
    "spawn_subagent",            # 子代理生命周期（先 spawn 才能 call）
    "call_subagent",
    "stop_subagent",
    "resume_subagent",
    "collect_subagent_results",
    "register_subagent_tool",    # 写同一份 persona 配置 + 有先后依赖
    "save_subagent_persona_tool",
    "edit_subagent_tool",
    "remove_subagent_tool",
    "get_subagent_persona_tool",
    # ── 浏览器插件：全部操作同一个浏览器实例 ──
    "tool_navigate",             # 改当前页，后续工具依赖当前页
    "tool_backend",              # 切换后端，改变后续所有工具行为
    "tool_script",               # 执行任意 JS
    "tool_interact",             # 可能写剪贴板/页面
    "tool_cookie",               # import 会写 cookie
    "tool_file",                 # 可下载落盘
    "tool_wait",                 # 有等待语义
    "tool_tabs",                 # 可能切标签页
)


class ToolGate:
    """判定"这一轮的工具能不能并行"。"""

    def __init__(
        self,
        enabled: bool = False,
        whitelist: Optional[Iterable[str]] = None,
        blacklist: Optional[Iterable[str]] = None,
        match_exact: bool = False,
        max_parallel: int = 8,
    ):
        self.enabled = enabled
        self.whitelist = [s for s in (whitelist or []) if str(s).strip()]
        self.blacklist = [s for s in (blacklist or DEFAULT_BLACKLIST) if str(s).strip()]
        self.match_exact = match_exact
        self.max_parallel = max(1, int(max_parallel))

    # ── 单个工具判定 ──
    def _hit(self, name: str, keywords: list[str]) -> bool:
        if not keywords:
            return False
        low = (name or "").lower()
        for kw in keywords:
            k = str(kw).strip().lower()
            if not k:
                continue
            if self.match_exact:
                if low == k:
                    return True
            else:
                if k in low:          # 关键词命中即可
                    return True
        return False

    def is_safe(self, name: str) -> bool:
        """这个工具是否允许参与并行。"""
        if self._hit(name, self.blacklist):
            return False                                  # 黑名单优先
        if self.whitelist:
            return self._hit(name, self.whitelist)        # 白名单非空 ⇒ 只在白名单里放行
        return True                                       # 白名单留空 ⇒ 全部放行

    # ── 整批判定 ──
    def can_parallelize(self, tool_calls: list) -> tuple[bool, str]:
        """返回 (能否并行, 原因)。保守：有一个不安全就整批串行。"""
        if not self.enabled:
            return False, "disabled"
        if not tool_calls or len(tool_calls) <= 1:
            return False, "single-call"
        if len(tool_calls) > self.max_parallel:
            return False, f"too-many({len(tool_calls)}>{self.max_parallel})"

        names = []
        for tc in tool_calls:
            fn = (tc or {}).get("function") or {}
            names.append(str(fn.get("name") or ""))

        unsafe = [n for n in names if not self.is_safe(n)]
        if unsafe:
            return False, "unsafe:" + ",".join(sorted(set(unsafe))[:5])
        return True, "ok:" + ",".join(names)


def split_calls(tool_calls: list, tool_set) -> list:
    """把 tool_calls 转成"可并发执行的协程工厂"列表。

    每个元素是 (index, name, 调用工厂)，工厂返回一个 coroutine。
    保持**原索引**，方便结果按序写回。
    """
    jobs = []
    for idx, tc in enumerate(tool_calls):
        fn = (tc or {}).get("function") or {}
        name = fn.get("name") or ""
        raw = fn.get("arguments") or ""
        jobs.append((idx, name, raw, tc.get("id")))
    return jobs


async def run_parallel(make_coro, timeout: Optional[float], count: int) -> list:
    """并发跑 count 个任务，返回**按原索引顺序**的结果列表。

    :param make_coro: 接一个下标 i，返回该任务的 coroutine
    :param timeout: 单个任务的超时（None 表示不设）
    """
    async def one(i):
        coro = make_coro(i)
        if timeout:
            return await asyncio.wait_for(coro, timeout)
        return await coro

    gathered = await asyncio.gather(
        *[one(i) for i in range(count)], return_exceptions=True)
    return list(gathered)
