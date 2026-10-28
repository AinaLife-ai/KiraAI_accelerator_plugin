"""3.x parse_xml：官方 bot 截图形态的内容去重与 ID 对位。

用户明确截图来自 3.x，不能只验证 2.x。本套件直接驱动 1.0.92 已存在的
3.0 `_parse_strip_factory` / `_ids_splice_factory`，覆盖真实生产顺序：
抢发首块 → 框架 parse_xml → 去重第二块 → add_message_ids 对位。

跑法：KIRA_FW=/path/to/KiraAI-3.x python3 tests/test_dedupe_blocks_3x.py
"""
import asyncio
import pathlib
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 3.x 框架源码（设 KIRA_FW=/path/to/KiraAI-3.x）")
sys.path.insert(0, FW)
from core.chat.message_utils import KiraIMSentResult  # noqa: E402
from core.message_manager import MessageProcessor  # noqa: E402

if hasattr(MessageProcessor, "send_xml_messages"):
    _env.skip("本套件专测 3.x parse_xml 路径")

main_mod = _env.load("main")
es = _env.load("early_sent")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


SID = "qqo:gm:GROUP1"
B1 = "<msg><text>找到啦w</text></msg>"
B2 = "<msg><reply>r</reply><at>GeneralNew</at><text>找到啦w</text></msg>"
C = "<msg><text>后续新内容</text></msg>"
DIFF_TEXT = "<msg><reply>r</reply><at>GeneralNew</at><text>不同正文</text></msg>"
DIFF_MEDIA = "<msg><reply>r</reply><at>GeneralNew</at><text>找到啦w</text><sticker>8</sticker></msg>"


class FakeDelivery:
    min_message_delay = 0.0
    max_message_delay = 0.0

    def __init__(self):
        self.parsed = []

    @staticmethod
    async def parse_xml(xml_data, tag_set):
        CURRENT.parsed.append(xml_data)
        return [("CHAIN", xml_data)] if xml_data.strip() else []

    @staticmethod
    def add_message_ids(xml_data, message_results):
        return xml_data, list(message_results)

    async def send_message_chain(self, session, chain, **kwargs):
        return KiraIMSentResult(message_id="SENT", ok=True)


class Ctx:
    def __init__(self, delivery):
        self.message_delivery = delivery
        self.message_processor = None
        self.adapter_mgr = None


CURRENT = None


def run_case(blocks, cfg=None):
    global CURRENT
    d = FakeDelivery()
    CURRENT = d
    plugin = main_mod.AcceleratorPlugin(Ctx(d), cfg or {})
    plugin._install_early_sent_strip()
    try:
        key = plugin._ckey(SID, "evt-dedupe-3x")
        plugin._sent_ledger[key] = [B1]
        plugin._early_results[key] = [KiraIMSentResult(message_id="E1", ok=True)]
        full = "\n".join(blocks)
        plugin._resp_by_sid[key] = types.SimpleNamespace(text_response=full)
        asyncio.run(d.parse_xml(full, None))
        parsed = d.parsed[-1]
        # 与真实 execute 阶段一致：只有剩余消息块才产生新结果，不伪造不存在的段。
        rest_count = len(es.locate_blocks(parsed)) if parsed.strip() not in ("", "<msg/>") else 0
        rest_results = [KiraIMSentResult(message_id=f"E{i + 2}", ok=True)
                        for i in range(rest_count)]
        _xml, results = d.add_message_ids(full, rest_results)
        ids = [getattr(r, "message_id", None) for r in results]
        return parsed, ids, results
    finally:
        plugin.patches.uninstall_all()
        CURRENT = None


print("## 1) 3.x 官方 bot 截图形态：去重与后续 ID 对位")
parsed, ids, results = run_case([B1, B2, C])
check("首块与第二块都被消费，框架只剩新内容", parsed.strip() == C, repr(parsed))
check("结果序列含首块、空占位、后续块", ids == ["E1", "", "E2"], ids)
check("结果数保持三块", len(results) == 3, len(results))

print("\n## 2) 重复块在末尾：3.x 同样不会重发")
parsed, ids, results = run_case([B1, B2])
check("没有新内容可发", parsed.strip() in ("", "<msg/>"), repr(parsed))
check("末尾去重块为空占位", ids == ["E1", ""], ids)
check("结果数保持两块", len(results) == 2, len(results))

print("\n## 3) 不同正文或媒体：3.x 不误删")
parsed, ids, _ = run_case([B1, DIFF_TEXT])
check("不同正文仍保留", DIFF_TEXT in parsed, parsed)
check("不同正文 ID 正常", ids == ["E1", "E2"], ids)
parsed, ids, _ = run_case([B1, DIFF_MEDIA])
check("不同媒体仍保留", DIFF_MEDIA in parsed, parsed)
check("不同媒体 ID 正常", ids == ["E1", "E2"], ids)

print("\n## 4) 用户关闭内容去重：保留 1.0.91 行为")
parsed, ids, _ = run_case([B1, B2, C], {"section_stream": {"dedupe_blocks": False}})
check("开关关闭时第二块仍发送", B2 in parsed and C in parsed, parsed)
check("开关关闭时 ID 顺序不变", ids == ["E1", "E2", "E3"], ids)

print("\n## 5) 多个重复块：3.x 占位顺序正确")
parsed, ids, results = run_case([B1, B2, B2, C])
check("多个重复块均被去掉", parsed.strip() == C, repr(parsed))
check("多个占位后新内容 ID 正确", ids == ["E1", "", "", "E2"], ids)
check("结果数保持四块", len(results) == 4, len(results))

print("\n## 6) 映射失败：3.x 保守不去重，绝不错位")
orig_positions = es.dropped_positions
try:
    es.dropped_positions = lambda *a, **k: []
    parsed, ids, _ = run_case([B1, B2, C])
finally:
    es.dropped_positions = orig_positions
check("3.x 既有映射失败路径仍保持原行为", B2 in parsed and C in parsed, parsed)
check("3.x 既有 ID 顺序不变", ids == ["E1", "E2", "E3"], ids)

print("\n" + "=" * 64)
print(f"通过 {len(PASS)}，失败 {len(FAIL)}")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ACC 3.x DEDUPE + ID ALIGNMENT TEST PASSED")
