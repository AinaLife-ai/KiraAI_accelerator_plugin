"""★★★ 抢发阶段"本段暂不可解析"的正确姿势（用户点名的报错，2026-09-28）。

## 现场（用户日志）

    2026-09-28 00:54:58 ERROR [plugin] [accel] 抢先发送：解析失败，本段交回框架
    Traceback (most recent call last):
      ...
      File ".../main.py", line 815, in _emit_segment
        actions = await mp._parse_xml_msg(seg, ctx.tag_set)
      File ".../core/message_manager.py", line 969, in _parse_xml_msg
        root = ET.fromstring(f"<root>{xml_data}</root>")
      ...
    xml.etree.ElementTree.ParseError: not well-formed (invalid token): line 2, column 97

## 为什么这不是"故障"，而是"可预期情形"（对照 KiraAI 框架与生态）

  · 模型在 `<msg>` 里写出**未转义的 `&` / `<`**（`Q&A`、`a < b`…）⇒ 这一段
    就是不可解析的 —— 这是**内容问题**，不是插件坏了。
  · 框架自己遇到同样的事（内置 kira-ai 插件 on_llm_response）只打**一行 error**，
    **不打堆栈**，然后走它的修复流程（另一个模型去修）。
  · 生态里另有 `xml_tag_fixer`（ON_LLM_RESPONSE 阶段做转义/补标签）——
    **它也在我们之后**。
  · 我们在**流式中途**（chat() 内），规范上**不得**自行转义/改写 XML：
    局部修复会与下游修复产生分歧 ⇒ 重复发送或内容对不上。

## 所以"正确姿势" = 安静地交回

  · 行为：解析失败 ⇒ 本段不投递、返回 False（内容完整交回框架，一个字符不丢）
  · 日志：一行 **warning**（说清"多为未转义的 &/<"），细节降级到 **debug**
  · **不得**再用 `logger.exception` 打整段 Traceback —— 线上看起来像故障，
    且把"内容问题"误报成"插件异常"（用户就是被这个误导的）。

## 本套件

  1. 行为：真 `_emit_segment` + 抛 ParseError 的解析器 ⇒ False、没发送、内容没丢
  2. 日志：解析失败路径**必须**是 warning + debug；**不得**有 logger.exception
  3. 反向验证：正常段仍然照发（判据不是"什么都不发"）
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)

import xml.etree.ElementTree as ET                            # noqa: E402

from core.chat import MessageChain                            # noqa: E402

main_mod = _env.load("main")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} " + name + (f"  [{detail}]" if detail else ""))


class Result:
    def __init__(self, mid):
        self.message_id = mid
        self.ok = True
        self.err = None


class RealishMP:
    """与框架同形态的解析：`<root>` 包起来 + 真 ET 解析。

    ★ 解析失败必须是**真的** ParseError（复刻用户日志里那条报错的来路）；
      解析成功则按 <text> 提取文字（与框架的 TagSet 语义一致，足够本测试用）。
    """

    min_message_delay = 0.0
    max_message_delay = 0.0

    def __init__(self):
        self.sent = []

    async def _parse_xml_msg(self, xml, tag_set):
        root = ET.fromstring(f"<root>{xml}</root>")     # ← BAD 段在这一行抛 ParseError
        out = []
        for msg in root:
            if msg.tag != "msg":
                continue
            chain = MessageChain()
            for child in msg:
                if child.tag == "text":
                    v = (child.text or "").strip()
                    if v:
                        chain.text(v)
            if not chain.is_empty():
                out.append(chain)
        return out

    async def send_message_chain(self, sid, chain):
        self.sent.append("".join(str(e) for e in chain.message_list))
        return Result("E%d" % len(self.sent))


class Ev:
    sid = "test:gm:1"
    is_stopped = False
    event_id = "ev-parse"


class Ctx:
    def __init__(self, mp):
        self.message_processor = mp


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


# ★ 插件的 logger 是框架的 `get_logger("plugin")`（见 core/plugin/__init__.py），
#   不是 "kira_accelerator" —— 挂错名字就会"抓不到"，从而误判成"没打日志"。
logger = logging.getLogger("plugin")
cap = LogCapture()
logger.addHandler(cap)
logger.setLevel(logging.DEBUG)

BAD = "<msg><text>Q&A 时间 a < b</text></msg>"      # 未转义的 & 与 < ⇒ 必然 ParseError
GOOD = "<msg><text>正常一段</text></msg>"

# 先证明 bad 段真的不可解析（与用户日志同形态）
try:
    ET.fromstring(f"<root>{BAD}</root>")
    parse_ok = True
except ET.ParseError as e:
    parse_ok = False
    err_msg = str(e)
check("前置：BAD 段确实不可解析（与用户日志同形态 ParseError）",
      not parse_ok, "" if parse_ok else err_msg)

print()
print("═══ 1) 行为：解析失败 ⇒ False、没发送、内容完整交回")
plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={})
plugin.patches.uninstall_all()
plugin.early_send = True
plugin._resp_by_sid = {}
plugin._early_results = {}
plugin._last_seg_ts = {}
plugin._sent_ledger = {}
plugin._ledger_ts = {}
plugin._stats = {"early_sent": 0, "first_seg_s": None}
mp = RealishMP()
plugin.ctx = Ctx(mp)
plugin._frame_delay = lambda: (0.0, 0.0)

ctx = main_mod.SendCtx("test:gm:1", Ev(), object())
cap.records.clear()
ok = asyncio.run(plugin._emit_segment(BAD, ctx))
check("★★ 解析失败 ⇒ 返回 False（本段不投递）", ok is False, f"返回 {ok!r}")
check("★★ 确实什么都没发出去（不丢也不重）", mp.sent == [], f"{mp.sent}")

print()
print("═══ 2) ★★★ 日志：warning 一行 + debug 细节；**不得**有 Traceback")
warns = [r for r in cap.records if r.levelno == logging.WARNING]
errs = [r for r in cap.records if r.levelno >= logging.ERROR]
exc_records = [r for r in cap.records if r.exc_info]
check("★★ 有一条 warning（说清「多为未转义的 & 或 <」）",
      any("未转义" in r.getMessage() for r in warns),
      f"warnings={[r.getMessage()[:40] for r in warns]}")
check("★★★ 没有 ERROR 级记录（不再把内容问题报成插件故障）",
      not errs, f"errors={[r.getMessage()[:40] for r in errs]}")
check("★★★ 没有带 Traceback 的记录（不再打整段堆栈）",
      not exc_records,
      f"带堆栈={[r.getMessage()[:40] for r in exc_records]}")
check("★ 细节降级到 debug（异常类型 + 消息）",
      any(r.levelno == logging.DEBUG and "ParseError" in r.getMessage() for r in cap.records),
      f"debugs={[r.getMessage()[:50] for r in cap.records if r.levelno == logging.DEBUG]}")

print()
print("═══ 3) 静态判据：源码里不得再有「logger.exception + 解析失败」")
MAIN = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
_i = MAIN.find("async def _emit_segment")
_j = MAIN.find("def _install_request_hook", _i)
_body = MAIN[_i:_j]
check("★★★ 解析失败不再用 logger.exception（那是「看起来像故障」的根源）",
      re.search(r'logger\.exception\([^)]*解析失败', _body) is None)
check("★★ 解析失败仍**交回框架**（返回 False，行为不变）",
      "return False" in _body)
check("★★ 有一行 warning（线上可读到、但不吓人）",
      "logger.warning(" in _body and "未转义" in _body)

print()
print("═══ 4) 反向验证：判据不是「什么都不发」——正常段照样抢发")
cap.records.clear()
ok2 = asyncio.run(plugin._emit_segment(GOOD, ctx))
check("★ 正常段仍抢发成功（返回 True）", ok2 is True, f"返回 {ok2!r}")
check("★ 且确实发出去了（文字被解析成消息元素）", len(mp.sent) == 1 and "正常一段" in mp.sent[0], f"{mp.sent}")

print()
print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 不可解析段安静交回 —— 行为不变、日志不再像故障")
