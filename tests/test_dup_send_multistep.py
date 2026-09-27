"""多步 agent loop：**每一步都会各自抢发** —— 这是本套件现在的正确模型。

## ⚠️ 这个文件的前一版是有害的，它锁住了一个错误的契约

旧版假设：

    第二步的文本**与第一步相同**（"框架重放 / 模型复读"）
    ⇒ 据此断言"台账必须活到轮结束、且只消费一次"

**这个前提是错的**，它让真 bug 连续躲过 4 个版本（1.0.8 → 1.0.12）。
事实（都有代码取证）：

  ① `_install_stream_engine` patch 的是 `ProviderManager.get_model_client`
     ⇒ **每一次 LLM 请求**都会新建引擎（`_make_engine`），
     `emit` 回调 = `_emit_segment`
     ⇒ **多步 loop 的每一步都会抢发它自己那一段**；
  ② 框架 `core/message_manager.py:766`：
     `async for step in agent_executor.run(agent_ctx, ...)` 里的 `event`
     是**循环外**创建的**同一个对象**
     ⇒ 同轮各步的 `event_id` **完全相同**；
  ③ 于是"已消费（轮次 id 相同）⇒ n=0 ⇒ 不剥离"会命中**第二步**
     ⇒ 第二步自己刚抢发的 C,D 被框架**再发一次** ⇒ 用户看到 `C,D | C,D`。

## 现在锁住的契约

  · 每次 `send_xml_messages` 对应一步，**按步消费**台账（调用后清）；
  · **不存在**"同轮第二步就跳过剥离"的任何判断；
  · 轮次 id 只用来判断"台账是否属于本轮"（丢弃跨轮残留，防误剪丢内容）。

行为层的收口测试在 `tests/test_dup_per_step.py`（用**真实** patch 层 +
真实 `_emit_segment` 跑两步，并做反向验证）。本文件负责"剥离语义 + 静态契约"。
"""
import pathlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

ROOT = _env.ROOT
FIX = _env.framework()
if FIX is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")

OK, BAD = [], []


def check(name, cond, detail=""):
    print(("  \u2713 " if cond else "  \u2717 ") + name + (f"  [{detail}]" if detail else ""))
    (OK if cond else BAD).append(name)


ES = _env.load("early_sent")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")

print("═══ 1) 剥离语义：本步的段必须能剥干净")
segs = ["<msg>你好呀</msg>", "<msg>在的</msg>"]
full = "".join(segs)
rest1 = ES.strip_early_sent_smart(full, len(segs), segs)
check("本步抢发的段 ⇒ 剥离后没有剩余",
      rest1 is None or rest1.strip() in ("", "<msg/>"), repr(rest1))

step2 = "<msg>那我先看看</msg>"
check("没有抢发过的内容 ⇒ 原样交出（新的内容就该发）",
      ES.strip_early_sent_smart(step2, 0, []) == step2)

print()
print("═══ 2) 两步各自抢发：各剥各的，互不影响")
step2_segs = ["<msg>第三步</msg>", "<msg>第四步</msg>"]
r = ES.strip_early_sent_smart("".join(step2_segs), len(step2_segs), step2_segs)
check("第二步用它**自己**的台账，同样剥干净",
      r is None or r.strip() in ("", "<msg/>"), repr(r))

print()
print("═══ 3) 静态核查（窗口按**函数体**取 —— 旧版截 900 字符，够不到就假绿）")
_EI = MAIN.find("def _install_early_sent_strip")
_EJ = MAIN.find("def page(", _EI)
BODY = MAIN[_EI:_EJ] if _EJ > _EI else MAIN[_EI:]
check("★ 定位到了 send_xml_messages 的接管函数（不是别的 finally）",
      "send_xml_messages" in BODY and len(BODY) > 3000, f"函数体 {len(BODY)} 字符")
check("★★★ 不存在「已消费 ⇒ 跳过剥离」的判定（整批/逐步重复的根因）",
      re.search(r"_cons_ev\s*==\s*_ev", BODY) is None)
check("★★ 台账在 finally 里按步消费",
      re.search(r"finally:[\s\S]{0,2000}?_sent_ledger\.pop\(sid_now, None\)", BODY) is not None)
check("★ 轮次键在同一处清掉（消除 finally 之后的死代码）",
      re.search(r'_sent_ledger\.pop\(sid_now \+ "\\x00ev", None\)', BODY) is not None)
check("★★ 轮次 id 仅用于「台账是否属于本轮」", "cur_ev" in BODY and "_raw_ev" in BODY)
check("★ 轮结束仍有兜底清理", "_clear_round_state" in MAIN)

print()
print("═══ 4) 反向自检：本文件必须真的能发现旧实现")
# 把旧版的判定"注入"一段文本，检查判据会报红 —— 证明判据不是恒真
_FAKE = BODY + "\n_cons_ev = plugin._ledger_consumed.get(sid_now)\nif _cons_ev == _ev:\n    n = 0\n"
check("★ 若旧判定复活，本套件会报红（判据非恒真）",
      re.search(r"_cons_ev\s*==\s*_ev", _FAKE) is not None)

print()
if BAD:
    print(f"❌ {len(BAD)} 项未通过: {BAD}")
    sys.exit(1)
print(f"🎉 全部通过（{len(OK)} 项）—— 多步每一步各自抢发，剥离互不干扰")
