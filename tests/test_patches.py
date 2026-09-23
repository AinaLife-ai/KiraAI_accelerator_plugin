"""接管护栏的行为自检 —— 不需要 KiraAI 环境，纯 Python 可跑。

覆盖三条铁律 + 两条反向自检：
  R1 幂等        → 重复 install 不叠加
  R2 精确还原    → 别人改过之后 uninstall 不破坏
  R3 熔断旁路    → 连续失败后永久走原实现
  反向自检 1：把 MARK 检查去掉（模拟无护栏）→ 重复安装会叠加 ⇒ 判据必须能报出来
  反向自检 2：把熔断关掉 → 失败会一直重试 ⇒ 判据必须能报出来
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 插件根

import breaker as _breaker  # noqa: E402
import patches as P  # noqa: E402
from time import monotonic as P_time  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class Target:
    def __init__(self):
        self.calls = 0

    async def hot(self, x):
        self.calls += 1
        return f"orig:{x}"


async def main() -> None:
    print("R1 幂等")
    t = Target()
    h1 = P.PatchHandle("hot")
    r1 = P.install(h1, t, "hot", lambda orig: (lambda x: orig(x + 1)))
    r2 = P.install(h1, t, "hot", lambda orig: (lambda x: orig(x + 1)))
    check("首次安装成功", r1 is not None)
    # ★ 契约变更（2026-09-23 审计）：幂等时不再返回 None，而是**认领**已有补丁。
    #   原因：框架重载前虽会先 terminate，但那次 terminate 若抛异常，框架只记日志
    #   就继续初始化；此时旧补丁还在，新实例若不认领就**再也没人能还原**它
    #   —— 表现为"插件关了行为还在"，且毫无报错。认领安全：uninstall 有守卫。
    check("二次安装返回 handle（认领）", r2 is not None)
    check("★ 认领的是同一个实现（没有重复包装）", r2.installed is r1.installed)
    check("认领后仍带原始实现引用，能还原", r2.original is r1.original)
    check("只有一层包装", await t.hot(1) == "orig:2", f"实际={await t.hot(1)}")

    print("R2 精确还原")
    h2 = P.PatchHandle("hot2")
    t2 = Target()
    h2r = P.install(h2, t2, "hot", lambda orig: (lambda x: orig(x * 10)))
    check("已安装", h2r is not None and h2.active)
    # 模拟"别人后来改了"
    async def third_party(x):
        return f"third:{x}"
    t2.hot = third_party
    h2.uninstall()
    check("别人改了就不还原（不破坏别人）", await t2.hot(1) == "third:1",
          f"实际={await t2.hot(1)}")

    # 没被别人改时应当正常还原
    h3 = P.PatchHandle("hot3")
    t3 = Target()
    P.install(h3, t3, "hot", lambda orig: (lambda x: orig(x * 10)))
    h3.uninstall()
    check("无人改动时正常还原", await t3.hot(1) == "orig:1", f"实际={await t3.hot(1)}")

    print("R3 熔断旁路（带半开恢复）")
    h4 = P.PatchHandle("hot4")
    t4 = Target()
    calls = {"n": 0}

    def boom(orig):
        async def impl(x):
            calls["n"] += 1
            raise RuntimeError("boom")
        return impl

    P.install(h4, t4, "hot", boom)
    for _ in range(P.BREAKER_THRESHOLD):
        r = await t4.hot(1)
        check("失败时回落原实现", r == "orig:1", f"实际={r}")
    br = P.breaker_for("hot4")
    check("达到阈值后熔断", br.tripped, f"state={br.state} failures={br.failures}")
    check("熔断后处于 OPEN 状态", br.state.value == "open", f"实际={br.state}")

    before = calls["n"]
    r = await t4.hot(2)
    check("冷却期内不再调用接管实现", calls["n"] == before, f"接管被调用 {calls['n']-before} 次")
    check("冷却期内仍返回原实现结果", r == "orig:2", f"实际={r}")

    print("  ★ 半开恢复：冷却结束后自动试探（用户质疑过 永久熔断太严格）")
    # 把冷却设成 0，模拟时间流逝
    br.cooldown = 0.0
    br.opened_at = P_time() - 1
    check("冷却到点后 should_bypass 放行", not br.should_bypass(), "仍在旁路")
    check("自动进入半开状态", br.state.value == "half_open", f"实际={br.state}")

    # 让接管实现恢复正常
    def good(orig):
        async def impl(x):
            calls["n"] += 1
            return f"accel:{x}"
        return impl

    t4.hot.__kira_accel_original__ = None if False else None
    # 直接替换内部 impl：重新装一层不行（幂等），所以直接改 breaker 的判定
    # 用一个"会成功"的接管点单独验证恢复路径
    br2 = P.breaker_for("hot4b")
    t4b = Target()

    def boom_b(orig):
        async def impl(x):
            raise RuntimeError("boom")
        return impl

    h4b = P.PatchHandle("hot4b")
    P.install(h4b, t4b, "hot", boom_b)
    for _ in range(P.BREAKER_THRESHOLD):
        await t4b.hot(1)
    check("第二个接管点也熔断", br2.tripped)
    br2.cooldown = 0.0
    br2.opened_at = P_time() - 1
    br2.should_bypass()                      # 转 HALF_OPEN
    check("第二点进入半开", br2.state.value == "half_open", f"实际={br2.state}")
    br2.ok()                                 # 模拟试探成功
    check("★ 试探成功后恢复正常（不再是永久死亡）", not br2.tripped,
          f"实际={br2.state}")
    check("冷却时间被重置回基准", br2.cooldown == br2.base_cooldown,
          f"实际={br2.cooldown}")
    check("记录了恢复次数", br2.recovered_count >= 1)

    print("  ★ 试探失败则冷却翻倍（避免持续抖动）")
    br3 = P.breaker_for("hot4c")
    t4c = Target()
    P.install(P.PatchHandle("hot4c"), t4c, "hot", boom_b)
    for _ in range(P.BREAKER_THRESHOLD):
        await t4c.hot(1)
    c1 = br3.cooldown
    br3.opened_at = P_time() - (br3.cooldown + 1)   # 确保冷却真的已过
    assert br3.state.value == "open"
    br3.should_bypass()                      # 冷却已到 → HALF_OPEN
    assert br3.state.value == "half_open", br3.state
    br3.fail(RuntimeError("still bad"))      # 试探失败
    check("试探失败后冷却翻倍", br3.cooldown == min(c1 * 2, br3.max_cooldown),
          f"{c1} → {br3.cooldown}")
    check("试探失败仍是 OPEN 而非永久", br3.state.value == "open")

    print("反向自检（判据必须能报红）")
    # 反向 1：无 MARK 幂等保护的朴素安装会叠加
    t5 = Target()
    base5 = t5.hot

    def wrap_no_mark(fn):
        async def inner(x):
            return await fn(x + 1)
        return inner

    t5.hot = wrap_no_mark(t5.hot)    # 第一次安装
    t5.hot = wrap_no_mark(t5.hot)    # 第二次安装（无护栏 ⇒ 叠加两层）
    got5 = await t5.hot(1)
    check("反向1：无护栏时重复安装确实叠加（应报红）",
          got5 != "orig:2", f"实际={got5}（叠加后应是 orig:3）")

    # 反向 2：把熔断阈值设得极大 ⇒ 失败会一直调用接管实现
    P.breaker_for("hot6")
    _breaker._BREAKERS["hot6"] = _breaker.Breaker("hot6", threshold=10**9)
    t6 = Target()
    n6 = {"n": 0}

    def boom6(orig):
        async def impl(x):
            n6["n"] += 1
            raise RuntimeError("boom")
        return impl

    P.install(P.PatchHandle("hot6"), t6, "hot", boom6)
    for _ in range(5):
        await t6.hot(1)
    check("反向2：无熔断时接管实现被反复调用（应报红）", n6["n"] == 5, f"实际={n6['n']}")

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 条未通过: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
