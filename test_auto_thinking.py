"""自动按需思考自检。

覆盖：多信号打分、简单/复杂分流、工具失败加成、预算控制、effort 分级、
      五种厂商风格的请求体生成，以及"比 Alife 更好的三点"（按sid独立 / 分级 / 预算）。
"""
from __future__ import annotations

import importlib.util as ilu
import sys
import types
from pathlib import Path

HERE = Path(__file__).parent
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class _Prompt:
    def __init__(self, content, name="message"):
        self.content = content
        self.name = name


class _Msg:
    def __init__(self, content):
        self.content = content


class _Req:
    def __init__(self, user_text="", tools=None, context=None):
        self.user_prompt = [_Prompt(user_text)] if user_text else []
        self.tools = tools or []
        self.messages = [_Msg(context or "")]


pkg = types.ModuleType("_accelpkg2")
pkg.__path__ = [str(HERE)]
sys.modules["_accelpkg2"] = pkg
spec = ilu.spec_from_file_location("_accelpkg2.auto_thinking", str(HERE / "auto_thinking.py"))
_at = ilu.module_from_spec(spec)
sys.modules["_accelpkg2.auto_thinking"] = _at
spec.loader.exec_module(_at)

Ctrl = _at.AutoThinkingController
build_on = _at.build_thinking_extra_body
build_off = _at.build_nothinking_extra_body


def main() -> None:
    print("1) 简单寒暄 ⇒ 不开思考（不能被'请思考'类内容刷爆）")
    c = Ctrl(threshold=2.0)
    for t in ["你好", "在吗", "哈哈", "谢谢", "ok", "早"]:
        d = c.decide("s1", _Req(t))
        check(f"『{t}』不开思考", not d.enabled, f"score={d.score} signals={d.signals}")

    print("\n2) 复杂意图 ⇒ 开思考")
    for t in ["为什么会这样", "帮我分析一下这个架构", "怎么优化这段代码",
              "compare these two approaches", "解释一下量子纠缠"]:
        d = c.decide("s1", _Req(t))
        check(f"『{t[:12]}…』开思考", d.enabled, f"score={d.score} signals={d.signals}")

    print("\n3) 上轮工具失败 ⇒ 加权（对标 Alife 的『需要处理函数异常』）")
    c2 = Ctrl(threshold=2.0)
    base = c2.decide("s2", _Req("看看这个"))
    c2.note_tool_error("s2")
    after = c2.decide("s2", _Req("看看这个"))
    check("失败后分数上升", after.score > base.score, f"{base.score} → {after.score}")
    check("失败信号被记录", "上轮工具失败" in after.signals)
    c2.note_tool_ok("s2")
    ok = c2.decide("s2", _Req("看看这个"))
    check("恢复正常后分数回落", ok.score == base.score, f"{ok.score} vs {base.score}")

    print("\n4) 消息长 ⇒ 加权")
    c3 = Ctrl(threshold=2.0, long_message_chars=50)
    d_long = c3.decide("s3", _Req("随便说说" + "啊" * 60))
    check("消息长加权", "消息长" in d_long.signals)

    print("\n4b) 上一轮用过工具 ⇒ 加权（取代原来的 工具数>=N，那是常数）")
    c3b = Ctrl(threshold=2.0)
    base4 = c3b.decide("s3b", _Req("看看这个"))
    c3b.note_turn_tool("s3b")
    c3b.commit_turn("s3b")
    after4 = c3b.decide("s3b", _Req("看看这个"))
    check("上轮用过工具 ⇒ 加权", after4.score > base4.score, f"{base4.score} -> {after4.score}")
    check("信号被记录", "上轮用过工具" in after4.signals, f"{after4.signals}")
    c3b.commit_turn("s3b")
    after5 = c3b.decide("s3b", _Req("看看这个"))
    check("后面没用工具 ⇒ 权重回落", after5.score == base4.score, f"{after5.score} vs {base4.score}")
    check("已无 工具多 这种常数信号", "工具多" not in " ".join(after4.signals))

    print("\n4c) 上下文：相对自己基线 + 滞回 + 能回落")
    c3c = Ctrl(threshold=2.0, context_ratio_on=1.6, context_ratio_off=1.15, context_min_samples=5)

    def ctx_req(n):
        return _Req("随便说说", context="x" * n)

    for _ in range(6):
        c3c.decide("s3c", ctx_req(35000))
    d_norm = c3c.decide("s3c", ctx_req(35000))
    check("正常水平不命中", not any("上下文偏离基线" in s for s in d_norm.signals), f"{d_norm.signals}")

    d_spike = c3c.decide("s3c", ctx_req(int(35000 * 1.8)))
    check("涨到 1.8x => 命中", any("上下文偏离基线" in s for s in d_spike.signals), f"{d_spike.signals}")

    # 按**当前实际基线**构造"高于关阈值、低于开阈值"的上下文
    cur_base = c3c.context_baseline("s3c")
    d_hold = c3c.decide("s3c", ctx_req(int(cur_base * 1.30)))
    check("滞回：1.30x（>关阈值1.15、<开阈值1.6）仍保持命中",
          any("上下文偏离基线" in s for s in d_hold.signals), f"{d_hold.signals} base={cur_base:.0f}")

    d_off = c3c.decide("s3c", ctx_req(int(c3c.context_baseline("s3c") * 1.0)))
    check("回落到基线以下 => 自动取消命中（能回来）", not any("上下文偏离基线" in s for s in d_off.signals), f"{d_off.signals}")

    print("\n4d) 本轮消息撑大上下文 => 本轮即判定（无滞后）")
    # 先让基线稳定下来（攒样本 + 稳定期）
    c3d = Ctrl(threshold=2.0, context_ratio_on=1.6, context_min_samples=3)
    for _ in range(12):
        c3d.decide("s3d", _Req("随便说说", context="y" * 10000))
    d_now = c3d.decide("s3d", _Req("随便说说" + "长" * 10000, context="y" * 10000))
    check("本轮 user_prompt 也算进上下文（不是等下一轮）",
          any("上下文偏离基线" in s for s in d_now.signals), f"{d_now.signals}")

    print("\n4d2) ★ 中位数种子：首轮尖峰不带跑基线")
    c3e = Ctrl(threshold=2.0, context_ratio_on=1.6, context_ratio_off=1.15,
               context_min_samples=5)
    seq = [100000, 35000, 35000, 35000, 35000]
    for v in seq:
        c3e.decide("s3e", ctx_req(v))
    base_after = c3e.context_baseline("s3e")
    check("★ 首轮 10 万尖峰不影响种子",
          abs(base_after - 35000) < 500,
          f"基线={base_after:.0f}（应为 35000 附近；改前会是 68000+）")
    # 种子建立后，还要等基线稳定（连续 3 轮变化 ≤5%）才开始判定
    for _ in range(3):
        c3e.decide("s3e", ctx_req(35000))
    check("基线稳定后 ready 锁定", c3e._ctx_ready.get("s3e") is True)
    d_after = c3e.decide("s3e", ctx_req(58000))
    check("稳定后真实尖峰能判定",
          any("上下文偏离基线" in s for s in d_after.signals), f"{d_after.signals}")

    print("\n4d3) 前几轮不判定（还在攒样本）")
    c3f = Ctrl(threshold=2.0, context_ratio_on=1.6, context_min_samples=5)
    no_judge = []
    for v in (35000, 35000, 35000, 35000):
        dd = c3f.decide("s3f", ctx_req(v * 3))     # 故意给个会命中的值
        no_judge.append(any("上下文偏离基线" in s for s in dd.signals))
    check("预热期内不判定上下文信号", not any(no_judge), f"{no_judge}")

    print("\n4d4) ★ 会话起步爬升期：不误判（修前的坑）")
    c3g = Ctrl(threshold=2.0, context_ratio_on=1.6, context_ratio_off=1.15,
               context_min_samples=5)
    climb = [5000, 10000, 15000, 20000, 25000] + [30000] * 12
    climb_hits = []
    for i, v in enumerate(climb, 1):
        dd = c3g.decide("s3g", ctx_req(v))
        if any("上下文偏离基线" in s for s in dd.signals):
            climb_hits.append(i)
    check("★ 爬升期一次都不误判（修前会误判 9~11 次）",
          len(climb_hits) == 0, f"误判轮次={climb_hits}")

    print("\n4d5) ★★ 修完爬升误判后，真实尖峰仍要能命中（防修过头）")
    dd = c3g.decide("s3g", ctx_req(72000))          # 稳定后真实暴涨
    check("★ 真实尖峰仍然命中", any("上下文偏离基线" in s for s in dd.signals),
          f"{dd.signals} 基线={c3g.context_baseline('s3g'):.0f}")
    dd2 = c3g.decide("s3g", ctx_req(33000))         # 回落
    check("回落不再命中", not any("上下文偏离基线" in s for s in dd2.signals),
          f"{dd2.signals}")

    print("\n4d6) ready 锁定：尖峰不会把信号关掉")
    c3h = Ctrl(threshold=2.0, context_ratio_on=1.6, context_min_samples=5)
    for _ in range(14):
        c3h.decide("s3h", ctx_req(30000))
    base_before = c3h.context_baseline("s3h")
    d1 = c3h.decide("s3h", ctx_req(90000))
    check("第一次尖峰命中", any("上下文偏离基线" in s for s in d1.signals))
    # 尖峰把基线抬高后，settle 计数会被清零 —— 但 ready 已锁定，不该因此关闭
    check("ready 已锁定为 True（不会因尖峰回退）", c3h._ctx_ready.get("s3h") is True)

    print("\n4e) 基线可读（面板要显示）")
    check("context_baseline 返回正数", c3c.context_baseline("s3c") > 0, f"{c3c.context_baseline('s3c')}")
    check("未知会话返回 0", c3c.context_baseline("nope") == 0)


    print("\n5) effort 分级（比 Alife 的固定值更细）")
    c4 = Ctrl(threshold=2.0, effort_low=2.0, effort_high=4.0)
    low = c4.decide("s4", _Req("怎么弄"))                       # 2.0 -> medium
    # 现存加权信号：复杂词 2.0 + 工具失败 1.5 + 上轮用过工具 1.0 + 消息长 0.5 = 5.0
    c4.note_tool_error("s4")
    c4.note_turn_tool("s4")
    c4.commit_turn("s4")
    high = c4.decide("s4", _Req("为什么" + "x" * 300))
    check("中等信号 -> medium", low.effort == "medium", f"score={low.score} effort={low.effort}")
    check("高信号 -> high", high.effort == "high", f"score={high.score} effort={high.effort}")

    print("\n6) 预算控制（防刷）")
    c5 = Ctrl(threshold=2.0, max_per_minute=3)
    opened = sum(1 for _ in range(6) if c5.decide("s5", _Req("为什么会这样")).enabled)
    check("一分钟内最多开 3 次", opened == 3, f"实际={opened}")
    d_budget = c5.decide("s5", _Req("为什么会这样"))
    check("超预算时标记出来", "超预算" in d_budget.signals, f"signals={d_budget.signals}")

    print("\n7) ★ 按 sid 独立（比 Alife 的全局布尔更细）")
    c6 = Ctrl(threshold=2.0, max_per_minute=1)
    a1 = c6.decide("sid_A", _Req("为什么会这样")).enabled
    b1 = c6.decide("sid_B", _Req("为什么会这样")).enabled
    check("A 会话开思考", a1)
    check("B 会话不受 A 的预算影响", b1, "按 sid 独立失败")
    a2 = c6.decide("sid_A", _Req("为什么会这样"))
    check("A 自己超预算被拦", not a2.enabled and "超预算" in a2.signals)

    print("\n8) 六种厂商风格的请求体生成 + ★取值合法性")
    for style, key in [("compatible", "reasoning_effort"),
                       ("reasoning_effort", "reasoning_effort"),
                       ("deepseek", "reasoning_effort"),
                       ("enable_thinking", "enable_thinking"),
                       ("thinking_object", "thinking"),
                       ("vllm_chat_template", "chat_template_kwargs")]:
        on = build_on(style, "medium")
        check(f"风格 {style} 开思考含 {key}", key in on, f"实际={on}")

    # ★ 关键回归：DeepSeek 只认 high/max，绝不能发出 medium
    DS_OK = {"high", "max"}
    for e in ("low", "medium", "high"):
        v = build_on("deepseek", e).get("reasoning_effort")
        check(f"DeepSeek effort={e} → {v}（必须∈{{high,max}}）", v in DS_OK, f"实际={v}")
    check("DeepSeek 开思考用 extra_body 的 thinking 字段",
          build_on("deepseek", "high")["thinking"] == {"type": "enabled"})
    check("DeepSeek 关思考只发 thinking，不发 reasoning_effort",
          "reasoning_effort" not in build_off("deepseek"),
          f"实际={build_off('deepseek')}")

    # compatible 也不能发厂商非法值
    for e in ("low", "medium", "high"):
        v = build_on("compatible", e).get("reasoning_effort")
        check(f"compatible effort={e} → {v}（用厂商交集，不含 medium）",
              v in {"low", "high"}, f"实际={v}")

    print("   关思考：只发最通用字段（刻意避开 reasoning_effort='none'，部分网关会 400）")
    for style, key in [("reasoning_effort", "reasoning_effort"),
                       ("deepseek", "thinking"),
                       ("enable_thinking", "enable_thinking"),
                       ("thinking_object", "thinking"),
                       ("vllm_chat_template", "chat_template_kwargs")]:
        off = build_off(style)
        check(f"风格 {style} 关思考含 {key}", key in off, f"实际={off}")
    check("没有任何风格发出 reasoning_effort='none'（避免 400）",
          all("none" not in str(build_off(s)) for s in
              ("compatible", "reasoning_effort", "deepseek", "enable_thinking",
               "thinking_object", "vllm_chat_template")))

    print("\n8b) ★ 会话上限保护（防 dict 无界增长）")
    c8 = Ctrl(threshold=2.0, max_sessions=10)
    for i in range(40):
        c8.decide(f"sid_{i}", _Req("为什么会这样"))
    check("会话数被压在上限内", len(c8._last_decision) <= 10,
          f"实际={len(c8._last_decision)}")
    check("关联字典同步裁剪", len(c8._last_tool_error) <= 10 and len(c8._opened) <= 10)
    check("裁剪后仍能正常工作", c8.decide("fresh_sid", _Req("为什么会这样")).enabled)

    print("\n9) 与 Alife 的对齐与超越（对照表自检）")
    d = Ctrl(threshold=2.0).decide("s7", _Req("为什么会这样"))
    check("① 多信号打分（Alife 只有单一触发点）", len(d.signals) >= 1)
    check("② 按 sid 独立", hasattr(Ctrl(threshold=2.0), "_last_decision"))
    check("③ effort 分级", d.effort in ("low", "medium", "high"))
    check("④ 预算控制", hasattr(Ctrl(threshold=2.0), "max_per_minute"))
    check("⑤ 可观测（理由可读）", isinstance(d.signals, list) and isinstance(d.score, float))

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 条未通过: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


main()
