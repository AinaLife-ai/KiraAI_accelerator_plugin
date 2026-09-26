"""多步 agent loop 里的重复发送 —— 本次用户报的现场。

框架的调用形状（message_manager.py:793）：
    async for step in agent_executor.run(agent_ctx, max_steps=max_steps):
        ...
        if not await send_llm_text(llm_resp):   # ← **每一步**都调 send_xml_messages
            break

也就是说 `send_xml_messages` 在一轮里可能被调用多次（每步一次）。
而我们在它的 `finally` 里 `pop` 掉台账 ⇒ **第二次调用时 n=0 ⇒ 不剥离
⇒ 该步的文本被整段重发**。

这个套件就是为此写的：**同 sid 连续两次调用**，第二次必须**不重复**。
"""
import pathlib
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

print("═══ 多步循环：同 sid 连续两次调用 send_xml_messages ═══")

# 第 1 步：抢发了 2 段，框架拿到的仍是完整文本
segs = ["<msg>你好呀</msg>", "<msg>在的</msg>"]
full = "".join(segs)

n1 = 2
rest1 = ES.strip_early_sent_smart(full, n1, segs)
check("步1：剥离后没有剩余（两段都已抢发）",
      rest1 is None or rest1.strip() in ("", "<msg/>"), repr(rest1))

# 第 2 步：框架又来一次，文本是**本步新生成的**
step2 = "<msg>那我先看看</msg>"
# ⚠️ 若台账/标记已被清（n=0），剥离不发生 ⇒ 这段原样发出去（这是**对的**，
#    因为它确实没发过）。真正的问题在于：**如果第二步的文本与第一步相同**
#    （模型复读 / 框架重放），n=0 就会把它**再发一遍**。
rest2_when_n0 = ES.strip_early_sent_smart(step2, 0, [])
check("步2：n=0 时原样交出（新的内容就该发）",
      rest2_when_n0 == step2, repr(rest2_when_n0))

# ★ 关键场景：第二步的文本**与已抢发的相同**（框架重放 / 模型复读）
#   此时若 n=0 ⇒ 会**再发一遍** = 用户看到的"整批重复"
replay = full
rest_replay_n0 = ES.strip_early_sent_smart(replay, 0, [])
check("★★ n=0 且文本与已抢发相同 ⇒ 会重复（这就是线上现象）",
      ES._norm_seg(rest_replay_n0) == ES._norm_seg(replay),
      "⇒ 说明台账必须活到该轮结束，不能在第一次调用后就清")

# ✅ 正确做法：台账要**保留到该轮结束**，第二次仍能剥离
rest_replay_ok = ES.strip_early_sent_smart(replay, n1, segs)
check("★★★ 台账保留时：同样文本第二次也能被剥掉 ⇒ 不重复",
      rest_replay_ok is None or rest_replay_ok.strip() in ("", "<msg/>"), repr(rest_replay_ok))

print()
if BAD:
    print(f"❌ {len(BAD)} 项未通过: {BAD}")
    sys.exit(1)
print(f"🎉 全部通过（{len(OK)} 项）—— 多步循环下不会整批重复")


print()
print("═══ 静态核查：清理时机是否修对了 ═══")
import re as _re
_main = (pathlib.Path(ROOT) / "main.py").read_text(encoding="utf-8")
_i = _main.find("finally:")
_fin = _main[_i:_i + 900]
check("★★★ finally 里不再清台账（整批重复的根因）",
      "_sent_ledger.pop" not in _fin,
      "仍有 pop" if "_sent_ledger.pop" in _fin else "已移走")
check("★ 轮结束有统一清理 _clear_round_state", "_clear_round_state" in _main)
check("★ final_result 里调用清理（不受 observe 开关影响）",
      _re.search(r"async def observe_final[\s\S]{0,500}?_clear_round_state", _main) is not None)
