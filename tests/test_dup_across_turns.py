"""跨轮次的重复发送 —— 用户报"更明显了"的那一类。

现场特征（截图）：
  · 同一批消息在聊天里出现**两次**
  · 日志里 `LLM ->` 只打印**一次**、`steps=1`（不是多步场景）
  ⇒ 说明"框架发送 + 我们抢发"各发了一次 = **剥离没生效**。

根因（我上一版引入的）：
  1. 1.0.8 加了 `_ledger_consumed` 标记（消费一次后置位），清理挂在
     `@on.final_result` 上；
  2. 但事件处理器循环是「按优先级降序、遇 `event.stop()` 就 break」
     ⇒ 排在**最后**的处理器**可能不执行**；
  3. 我当时用的是 `Priority.SYS_LOW`（= -100，框架**内部专用**，
     注释明确写着 "DO NOT use SYS_LOW or SYS_HIGH in user plugins"）
     ⇒ 永远排最后 ⇒ 清理可能永不执行；
  4. 标记没清 ⇒ **从第二轮起永久为真** ⇒ 每轮都走"已消费 ⇒ 不剥离"
     ⇒ **每一轮都重复**（这就是"更明显了"）。

本套件锁住三件事：
  ① 用户插件**不得**使用 SYS_LOW / SYS_HIGH
  ② 消费标记必须能**自愈**（带时间戳，不依赖钩子一定执行）
  ③ 轮开始时有陈陈旧状态的兜底清理
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

ROOT = _env.ROOT
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
OK, BAD = [], []


def check(name, cond, detail=""):
    print(("  \u2713 " if cond else "  \u2717 ") + name + (f"  [{detail}]" if detail else ""))
    (OK if cond else BAD).append(name)


print("═══ 1) 优先级：用户插件不得占用框架保留值 ═══")
# plugin_handlers.py 的注释：DO NOT use SYS_LOW or SYS_HIGH in user plugins
check("★★★ 不使用 Priority.SYS_LOW（框架内部专用，排最后可能不执行）",
      "Priority.SYS_LOW" not in MAIN)
check("★★★ 不使用 Priority.SYS_HIGH（同上）", "Priority.SYS_HIGH" not in MAIN)
check("★ 仍有 final_result 清理（正常路径）",
      re.search(r"@on\.final_result[\s\S]{0,400}?_clear_round_state", MAIN) is not None)

print()
print("═══ 2) 消费标记必须自愈（不依赖钩子一定执行）═══")
check("★★★ 消费标记存的是**时间戳**（不是 True）",
      "_ledger_consumed[sid_now] = time.time()" in MAIN,
      "若是纯布尔 ⇒ 钩子没跑就永久为真 ⇒ 每轮重复")
# 变量名可能是 _cons_ts 之类，正则放宽：只要"取标记"后面不远处出现
# `time.time() - <变量> < 数字` 就算带时效。
# 直接找"时效判断"语句本身（不依赖它与标记取值之间的注释长度 ——
# 第一次写的时候窗口只给了 200 字符，中间夹着大段注释 ⇒ 判据假红）
_ttl = re.search(r"if\s+\w+\s+and\s+\(?time\.time\(\)\s*-\s*(\w+)\)?\s*<\s*(\d+)", MAIN)
check("★★★ 读标记时带时效判断（超时自动失效）",
      _ttl is not None,
      f"窗口={_ttl.group(2) if _ttl else '未匹配'}s")
check("★ 声明为 dict[str, float]（不是 bool）",
      "_ledger_consumed: dict[str, float]" in MAIN)

print()
print("═══ 3) 轮开始时的兜底清理（双保险）═══")
check("★★★ 有陈旧状态清理函数", "def _clear_stale_round_state" in MAIN)
check("★★★ 在 llm_request（每轮开始）里调用",
      re.search(r"async def capture_and_optimize[\s\S]{0,600}?_clear_stale_round_state\(\)", MAIN)
      is not None)
check("★ 台账记录最后活动时间（供判断陈旧）", "_ledger_ts" in MAIN)
check("★ 陈旧阈值足够长（不会误清正在用的状态）",
      re.search(r"now - float\(ts or 0\) > (\d+)", MAIN) is not None
      and int(re.search(r"now - float\(ts or 0\) > (\d+)", MAIN).group(1)) >= 300)

print()
if BAD:
    print(f"❌ {len(BAD)} 项未通过: {BAD}")
    sys.exit(1)
print(f"🎉 全部通过（{len(OK)} 项）—— 跨轮次不会重复")
