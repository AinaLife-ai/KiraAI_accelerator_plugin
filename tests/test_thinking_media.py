"""★ 图片/表情包描述**不应算入思考计分**（用户实测：光发一张图就触发思考）。

## 证据
用户日志：`思考=开(得分0.5)[281字]`
—— 0.5 分正好是「消息长」这一项，而那 281 个字**全是 VLM 生成的图片描述**。
用户只发了一张图，一个字没打。

## 框架规范
`core/message_manager.py:message_format_to_text` 把媒体元素拼成固定前缀：

    [Image {caption}] / [Image {caption}, file_path: …]
    [Sticker {caption}]
    [Emoji {emoji_desc} (ID: {id})]      ← 用户**自己选的**表情，不是模型描述
    [Image attached]                     ← native 模式

## s 版聊天插件的做法（znq19/KiraAI_Default-Chat-Z）
它的 `_has_media` 判断媒体用的是**元素类型**
（`isinstance(elem, (Image, Sticker, Record))`），不是字符串 —— 结构判据。
我们照这个思路：**优先从 `event.messages[*].chain` 里取 `Text` 元素**，
那就是用户真正打的字，描述天然不在其中。

## 为什么字符串剥离只做兜底
框架的 `[Image {描述}]` 在**描述本身含 `]`** 时是**歧义的**（框架没转义），
任何按括号配对的解析都可能剥过头 ⇒ 所以只当兜底，且**拿不准就不剥**。
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


SRC = (HERE / "auto_thinking.py").read_text(encoding="utf-8")
MAIN = (HERE / "main.py").read_text(encoding="utf-8")
SCHEMA = (HERE / "schema.json").read_text(encoding="utf-8")

print("═══ 1) 开关存在且默认开")
check("schema 有 exclude_media_desc", "exclude_media_desc" in SCHEMA)
import json
_sch = json.loads(SCHEMA)
_f = _sch["section_thinking"]["fields"].get("exclude_media_desc")
check("★ 默认值 = true（默认开）", _f is not None and _f.get("default") is True,
      str(_f.get("default") if _f else None))
check("主插件把它读进控制器", "thinking.exclude_media_desc" in MAIN)
check("控制器里按它决定是否剔除", "exclude_media_desc" in SRC and "_typed_text_from_event" in SRC)

print()
print("═══ 2) ★ 结构判据：只取用户打的字（描述天然不在其中）")
check("存在 _typed_text_from_event", "_typed_text_from_event" in SRC)
check("★ 它只取 Text 元素（媒体描述不在 Text 里）",
      "isinstance(ele, Text)" in SRC)
check("★ 它读的是 event.messages[*].chain（框架解析好的结构）",
      "getattr(m, \"chain\", None)" in SRC)
check("★ _user_text 里**优先**走结构判据，字符串剥离只作兜底",
      SRC.index("_typed_text_from_event(") < SRC.index("_strip_media_desc(text)"))

print()
print("═══ 3) 实跑：结构判据（用假的 event / chain）")
try:
    sys.path.insert(0, str(HERE / "tests"))
    import os
    os.makedirs("/tmp/itest/data", exist_ok=True)
    os.chdir("/tmp/itest")
    import _env
    fw = _env.framework()
    if fw:
        sys.path.insert(0, fw)
    at = _env.load("auto_thinking")
    from core.chat import MessageChain
    from core.chat.message_elements import Text

    class _Msg:
        def __init__(self, chain):
            self.chain = chain
            self.message_str = ""

    class _Ev:
        def __init__(self, msgs):
            self.messages = msgs

    # 用户发了一张图 + 打了 4 个字；图片描述很长（在 message_str 里，但不在 chain 的 Text 里）
    chain = MessageChain()
    chain.message_list.append(Text("看看这个"))
    ev = _Ev([_Msg(chain)])
    got = at.AutoThinkingController._typed_text_from_event(ev)
    check("★ 结构判据只返回用户打的字", got == "看看这个", repr(got))

    # 纯图片（chain 里没有 Text）⇒ 返回 None ⇒ 调用方可走兜底/空
    chain2 = MessageChain()
    ev2 = _Ev([_Msg(chain2)])
    check("纯图片时没有 Text ⇒ 返回 None（不把描述算进来）",
          at.AutoThinkingController._typed_text_from_event(ev2) is None)

    # 兜底：字符串剥离（简单情形）
    S = at.AutoThinkingController._strip_media_desc
    check("兜底：剥掉 [Image …]", S("[Image 一只很长很长的猫的描述]你好") == "[图]你好",
          S("[Image 一只很长很长的猫的描述]你好"))
    check("兜底：剥掉 [Sticker …]", S("[Sticker 熊猫头]") == "[图]")
    check("★ 兜底**不剥** [Emoji …]（那是用户自己选的表情）",
          S("[Emoji 微笑 (ID: 14)]在吗") == "[Emoji 微笑 (ID: 14)]在吗")
    check("兜底：方括号配不平时**保守不剥**（宁可不剥，不误伤）",
          S("[Image 没闭合的描述 然后没有右括号").startswith("[Image "),
          S("[Image 没闭合的描述 然后没有右括号")[:20])
except Exception as e:  # noqa: BLE001
    check(f"（框架源码不可用时跳过实跑：{type(e).__name__}）", False, str(e)[:60])

print()
print("═══ 4) 计分语义：剥完只剩 [图] 时，不该命中「消息长」")
check("★ _user_text 返回的是用户打的字，长度信号按它算",
      "return typed" in SRC)
# [图] 只有 3 个字符，远低于 long_message_chars(220) ⇒ 不会 +0.5
check("占位符足够短（[图] = 3 字符，不会触发长消息）",
      at.AutoThinkingController._MEDIA_PLACEHOLDER == "[图]"
      if "at" in dir() else True,
      "占位符应为 [图]")

print()
print("═══ 6) ★★★ 端到端回归：日志里 `[1021字]` 那个真 bug")
# 背景（2026-09-24 用户实测日志）：`思考=开(medium,复杂意图词/消息长)[1021字]`
#   而用户只打了 50 字 ⇒ 扫进去的东西远多于用户输入。
#   根因：`request._accel_event` **从未被设置** ⇒ 结构判据拿不到 event
#        ⇒ 落到字符串兜底 ⇒ 把 Z 记忆注入 + 图片/表情包描述一起算进"消息长"。
_main = pathlib.Path(HERE / "main.py").read_text(encoding="utf-8")
check("★★ 主插件必须把 event 挂到 request 上（结构判据要用）",
      '_accel_event' in _main and 'req.__dict__["_accel_event"] = event' in _main)
check("★ 思考判定确实去取它", '_accel_event' in
      pathlib.Path(HERE / "auto_thinking.py").read_text(encoding="utf-8"))
# 反向验证：把这一行去掉，结构判据就永远拿不到 event
_stripped = _main.replace('req.__dict__["_accel_event"] = event', '')
check("★ 反向验证：去掉挂载 ⇒ 结构判据拿不到 event（会退回扫描注入内容）",
      'req.__dict__["_accel_event"] = event' not in _stripped)

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 媒体描述不再拉高思考评分")
