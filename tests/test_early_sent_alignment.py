"""抢先发送后，message_id 必须仍然贴在对的消息上。

背景（2026-09-23 拆解 Alife 时发现的自己的 bug）：
  早前为了不让框架重复发送，发送层会把"已抢先发出的段"从文本里剥掉，
  只把**剩余段**交给原实现。但框架随后这样用结果：

      raw_output = self._add_message_ids(text, message_results)
      #  core/message_manager.py:769   text 是【完整】原文
      #  实现（:783 附近）：按位置遍历原文里的每个 <msg>，
      #      if i < len(message_results): msg.set("message_id", message_results[i].message_id)

  ⇒ 只还回"剩余段"的结果，**位置就全错**：
      第 1 个 <msg>（早就发出去了）被贴上"剩余段第 1 条"的 ID，
      而真正抢发的那几段反而没有 ID。

  为什么这要紧：提示词明确告诉模型「message_id 由系统发出消息后自动添加」，
  而 raw_output 还会**写回对话记忆**（new_messages[idx].content）——
  于是模型在自己历史里读到的是**错位的 ID**，用它去引用/回复就会指错消息。

  修法：把抢先发出的结果按顺序拼回返回列表（只是"还账"，框架并没有重发，
  所以锁里也不会多睡 —— 提速收益不受影响）。

本测试用**框架自己的** `_add_message_ids` 验证：每个 <msg> 拿到的是自己的 ID。
"""
import asyncio
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

from core.chat import MessageChain                              # noqa: E402
from core.message_manager import MessageProcessor               # noqa: E402

main_mod = _env.load("main")
es = _env.load("early_sent")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class Result:
    def __init__(self, mid, ok=True, err=None):
        self.message_id = mid
        self.ok = ok
        self.err = err


class FakeMP:
    """站在 MessageProcessor 的位置：只实现被用到的两个方法。"""

    # 框架的发送循环会读这两个属性来做节流（这里不想真等）
    min_message_delay = 0.0
    max_message_delay = 0.0

    def __init__(self, sid, plugin):
        self.sid = sid
        self.plugin = plugin
        self.sent = []                    # 记录框架真正发出去的段

    async def _parse_xml_msg(self, xml, tag_set):
        segs = re.findall(r"<msg[^>]*>.*?</msg>|<msg[^>]*/>", xml, re.S)
        out = []
        for s in segs:
            c = MessageChain()
            if s.rstrip() not in ("<msg/>", "<msg />"):
                c.text(s)
            out.append(c)
        return out

    async def send_message_chain(self, sid, chain):
        txt = "".join(str(e) for e in chain.message_list)
        self.sent.append(txt)
        return Result("F" + str(len(self.sent)))      # 框架发出的段：F1, F2, ...


# 直接用框架的 _add_message_ids / send_xml_messages（不复制逻辑）
_add_ids = MessageProcessor._add_message_ids


async def main():
    full = "".join("<msg>第%d段</msg>" % i for i in range(1, 6))   # 5 段
    check("构造出 5 段的原文", full.count("<msg>") == 5)

    print("\n1) 装补丁（走插件自己的 _install_early_sent_strip）")
    plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    plugin.patches.uninstall_all()
    plugin.early_send = True
    plugin._resp_by_sid = {}
    plugin._early_results = {}
    plugin._last_seg_ts = {}
    plugin._install_early_sent_strip()
    check("补丁已安装", getattr(MessageProcessor.send_xml_messages,
                                "__kira_accel__", False) is True)

    mp = FakeMP("test:gm:1", plugin)

    print("\n2) 模拟：前 3 段已抢先发出，框架只发剩下 2 段")
    plugin._early_results["test:gm:1"] = [Result("E1"), Result("E2"), Result("E3")]
    class Resp:
        pass
    resp = Resp()
    resp.__dict__["_accel_early_sent_count"] = 3
    plugin._resp_by_sid["test:gm:1"] = resp

    class Ev:
        sid = "test:gm:1"          # 框架要求 <adapter>:<dm|gm>:<id>
        is_stopped = False
        event_id = "e1"

    results = await MessageProcessor.send_xml_messages(mp, Ev(), full, None)

    print(f"     框架真正发出去的: {mp.sent}")
    check("★ 框架只发了剩余的 2 段（没有重发已抢发的）",
          len(mp.sent) == 2, f"{len(mp.sent)} 段")
    check("★★ 返回的结果条数 == 原文里的 <msg> 条数（位置能对上）",
          len(results) == 5, f"返回 {len(results)} 条 vs 原文 5 段")

    print("\n3) 用框架自己的 _add_message_ids 贴 ID，逐个核对")
    raw = _add_ids(full, results)
    print("     贴完 ID 的历史文本:")
    for m in re.finditer(r'<msg message_id="([^"]*)">([^<]*)</msg>', raw):
        print(f"       {m.group(2)} → {m.group(1)}")

    got = re.findall(r'<msg message_id="([^"]*)">([^<]*)</msg>', raw)
    expect = [("E1", "第1段"), ("E2", "第2段"), ("E3", "第3段"),
              ("F1", "第4段"), ("F2", "第5段")]
    check("★★★ 每一段拿到的都是**它自己**的 ID", got == expect,
          f"{got}")
    check("★ 抢先发出的 3 段没有丢 ID",
          all(g[0] for g in got[:3]), str([g[0] for g in got[:3]]))
    check("★ 后 2 段用的是框架发出的 ID",
          [g[0] for g in got[3:]] == ["F1", "F2"], str([g[0] for g in got[3:]]))

    print("\n4) 对照：不拼回结果会长什么样（证明这确实是 bug）")
    # 只把"剩余段"的结果交给框架（= 修复前的行为），看错位长什么样
    rest_only = [Result("F1"), Result("F2")]
    bad = _add_ids(full, rest_only)
    # 注意：没有 ID 的 <msg> 不会有 message_id 属性 ⇒ 用宽松的正则再取一次
    bad_msgs = re.findall(r'<msg([^>]*)>([^<]*)</msg>', bad)
    bad_pairs = [(re.search(r'message_id="([^"]*)"', a).group(1) if 'message_id=' in a else '',
                  c) for a, c in bad_msgs]
    print("     修复前（只还回剩余段）:", [(b[1], b[0] or "无ID") for b in bad_pairs])
    check("★★ 反向验证：只拼剩余段 ⇒ 第1段被贴上 F1（错位）",
          bad_pairs and bad_pairs[0][0] == "F1",
          f"第1段拿到 {bad_pairs[0][0] if bad_pairs else '?'}，应为 E1")
    check("★★ 且抢发的 3 段全都没有 ID（模型读到的是无 ID 的消息）",
          len(bad_pairs) == 5 and all(not b[0] for b in bad_pairs[2:]),
          str([(b[1], b[0] or "无ID") for b in bad_pairs]))

    print("\n5) 还原")
    plugin.patches.uninstall_all()
    check("卸载后回到框架原实现",
          not getattr(MessageProcessor.send_xml_messages, "__kira_accel__", False))

    print("\n" + "=" * 58)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for x in FAIL:
            print("   ✗", x)
        sys.exit(1)
    print("🎉 全部通过 —— message_id 贴在该贴的消息上")


asyncio.run(main())
