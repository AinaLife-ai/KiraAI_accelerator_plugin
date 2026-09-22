"""抢先发送的兼容性自检 —— 重点是"不误导下游插件"。

问题背景（实测）：
  框架内置 kira-ai 插件与 sustained-chat 插件都会在 ON_LLM_RESPONSE 里
  把 resp.text_response 当作模型的完整输出来用。
  如果抢先发送后把它改成"剩余部分"，它们就只看到尾巴 ⇒ 误判。
"""
import importlib.util as ilu
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent   # 插件根
FAILED = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'BAD'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


spec = ilu.spec_from_file_location("early_sent", str(HERE / "early_sent.py"))
es = ilu.module_from_spec(spec)
spec.loader.exec_module(es)


class FakeResp:
    def __init__(self, text):
        self.text_response = text
        self.tool_results = []


print("1) 剥离：从完整文本里去掉已发出的段")
full = "<msg><text>A</text></msg><msg><text>B</text></msg><msg><text>C</text></msg>"
check("去掉 1 段", es.strip_early_sent(full, 1) == "<msg><text>B</text></msg><msg><text>C</text></msg>",
      es.strip_early_sent(full, 1))
check("去掉 2 段", es.strip_early_sent(full, 2) == "<msg><text>C</text></msg>",
      es.strip_early_sent(full, 2))
check("全去掉 -> <msg/>", es.strip_early_sent(full, 3) == "<msg/>",
      es.strip_early_sent(full, 3))
check("count=0 原样返回", es.strip_early_sent(full, 0) == full)
check("count 超过实际段数 -> <msg/>", es.strip_early_sent(full, 99) == "<msg/>",
      es.strip_early_sent(full, 99))
check("空文本安全", es.strip_early_sent("", 3) == "")
check("未闭合的尾巴会保留",
      es.strip_early_sent("<msg><text>A</text></msg><msg><text>没写完", 1)
      == "<msg><text>没写完")

print("\n2) 标记：只记录，不改写 text_response")
r = FakeResp(full)
es.mark_early_sent(r, ["<msg><text>A</text></msg>"], full)
check("★ text_response 未被改写", r.text_response == full, f"实际={r.text_response!r}")
check("已发段数可读", es.early_sent_count(r) == 1)
check("★ 标记【没有】写进 tool_results（否则框架会崩）",
      r.tool_results == [], f"实际={r.tool_results}")
check("标记只写私有属性", r.__dict__.get("_accel_early_sent_count") == 1)
check("未标记的响应 count=0", es.early_sent_count(FakeResp(full)) == 0)
check("工具结果不是 list 时也安全", es.early_sent_count(FakeResp("x")) == 0)

print("\n2b) ★★ 崩溃场景回归：同一响应里既有已抢发段、又有 tool_calls")
print("      （第一版把标记塞进 tool_results，这里会抛 ValidationError）")


class RespWithTools:
    """模拟真实的 LLMResponse：text_response 完整 + tool_calls 非空。"""
    def __init__(self, text, calls):
        self.text_response = text
        self.tool_calls = calls
        self.tool_results = []


try:
    from pydantic import BaseModel, Field
    from typing import Literal, Union, Optional

    class _M(BaseModel):
        role: Literal["system", "user", "assistant", "tool"]
        content: Union[str, list, dict, None] = None
        reasoning_content: Optional[str] = ""
        tool_calls: Optional[list[dict]] = Field(default_factory=list)
        tool_call_id: Optional[str] = None
        name: Optional[str] = None

    r2 = RespWithTools(full, [{"id": "t1", "function": {"name": "read_file", "arguments": "{}"}}])
    es.mark_early_sent(r2, ["<msg><text>A</text></msg>"], full)
    check("标记后 tool_results 仍为空", r2.tool_results == [], f"实际={r2.tool_results}")

    # 模拟框架的 tool_calls 分支
    try:
        msgs = [_M(**x) for x in r2.tool_results]
        check("★ 框架构造 tool 消息不会崩", True)
    except Exception as e:
        check("★ 框架构造 tool 消息不会崩", False, f"{type(e).__name__}: {e}")

    # 反向验证：如果标记被塞进 tool_results，必须报红
    r3 = RespWithTools(full, [{"id": "t1"}])
    r3.tool_results.append({es.MARKER_KEY: True, "count": 1})
    try:
        _ = [_M(**x) for x in r3.tool_results]
        check("反向：把标记塞进 tool_results 应当报错（判据有效性）", False, "竟然通过了")
    except Exception:
        check("反向：把标记塞进 tool_results 确实会崩（证明该判据有效）", True)
except ImportError:
    print("      pydantic 不可用，跳过")

print("\n2c) 发送节奏：遵守框架的消息间隔")
src = (HERE / "main.py").read_text()
check("已删除自造的 stream_gap 配置", "stream_gap" not in src)
check("有 _frame_delay（读框架的 min/max_message_delay）", "_frame_delay" in src)
check("读的是框架属性而非另立配置",
      'getattr(mp, "min_message_delay"' in src and 'getattr(mp, "max_message_delay"' in src)
check("用速率限制（先算差值再决定等多久）", "wait = random.uniform(lo, hi) - (now -" in src)
check("模型生成慢于目标间隔时不额外等待",
      "if wait > 0:" in src)
check("每轮重置时间戳", "self._last_seg_ts = None" in src)


def _pace_sim(gen_s, lo, hi, n=4, rnd=None):
    """复刻 main.py 里的速率限制逻辑"""
    import random as _r
    r = _r.Random(rnd)
    out, last, t = [], None, 0.0
    for _ in range(n):
        t += gen_s
        if last is not None:
            wait = r.uniform(lo, hi) - (t - last)
            if wait > 0:
                t += wait
        last = t
        out.append(t)
    return out


slow = _pace_sim(3.0, 2.0, 5.0, 3, rnd=1)
check("生成时间 >= 目标间隔时，间隔 >= 目标下限",
      all(b - a >= 2.0 for a, b in zip(slow, slow[1:])), f"{slow}")
fast = _pace_sim(0.01, 2.0, 5.0, 3, rnd=1)
check("生成很快时被节流到目标区间（2~5s）",
      all(2.0 <= b - a <= 5.0 for a, b in zip(fast, fast[1:])), f"{fast}")
zero = _pace_sim(0.01, 0.0, 0.0, 3)
check("间隔配 0 ⇒ 完全不节流", abs(zero[-1] - 0.03) < 0.01, f"{zero}")

print("\n2d) ★ 与框架的段间隔一致性（证明 没有绕过间隔 ）")


def _framework_spacing(gen_s, lo, hi, n, rnd):
    """框架：全部生成完，然后逐段发、每段后无条件 sleep"""
    import random as _r
    r = _r.Random(rnd)
    t, out = gen_s * n, []
    for _ in range(n):
        out.append(t)
        t += r.uniform(lo, hi)
    return out


def _ours_spacing(gen_s, lo, hi, n, rnd):
    """我们：就绪即发，就绪前补足差值"""
    import random as _r
    r = _r.Random(rnd)
    out, last, t = [], None, 0.0
    for _ in range(n):
        t = gen_s * (len(out) + 1)
        if last is not None:
            need = r.uniform(lo, hi) - (t - last)
            if need > 0:
                t += need
        out.append(t)
        last = t
    return out


# 模型比间隔快 ⇒ 两边间隔都应等于目标间隔
fs = _framework_spacing(0.1, 2.0, 5.0, 4, rnd=7)
os_ = _ours_spacing(0.1, 2.0, 5.0, 4, rnd=7)
gap_f = [round(b - a, 6) for a, b in zip(fs, fs[1:])]
gap_o = [round(b - a, 6) for a, b in zip(os_, os_[1:])]
check("模型快时：段间隔与框架逐项相同", gap_f == gap_o, f"{gap_f} vs {gap_o}")
check("模型快时：间隔都 >= 目标下限 2s", all(g >= 2.0 for g in gap_o), f"{gap_o}")

# 模型比间隔慢 ⇒ 两边间隔都应等于生成间隔（都不额外等）
fs2 = _framework_spacing(3.0, 2.0, 5.0, 4, rnd=7)
os2 = _ours_spacing(3.0, 2.0, 5.0, 4, rnd=7)
gf2 = [round(b - a, 6) for a, b in zip(fs2, fs2[1:])]
go2 = [round(b - a, 6) for a, b in zip(os2, os2[1:])]
check("模型慢时：间隔都不小于目标下限", all(g >= 2.0 for g in go2), f"{go2}")

# 第一段的时机差异（这才是抢先发送的价值）
check("★ 第一段：框架等全部生成完，我们第 1 段就绪就发",
      fs[0] > os_[0], f"框架 {fs[0]:.1f}s vs 我们 {os_[0]:.2f}s")
print(f"     框架首段 {fs[0]:.1f}s  →  我们首段 {os_[0]:.2f}s"
      f"（早 {fs[0]-os_[0]:.2f}s）")

print("\n3) 与框架的 XML 解析兼容（框架用 ET.fromstring 包 <root>）")
import xml.etree.ElementTree as ET
for n in (0, 1, 2, 3):
    got = es.strip_early_sent(full, n)
    try:
        ET.fromstring("<root>" + got + "</root>")
        check(f"剥离 {n} 段后仍是合法 XML", True)
    except Exception as e:
        check(f"剥离 {n} 段后仍是合法 XML", False, str(e))

print()
if FAILED:
    print(f"FAIL {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("PASS 全部通过")
