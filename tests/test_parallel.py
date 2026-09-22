"""同轮工具并行自检。

重点不是"能并行"，而是：
  1. 判定规则正确（黑名单优先 / 白名单留空=放行 / 全字匹配开关）
  2. ★ 行为等价：并行后的 tool_results 与串行版**逐字节相同**
  3. ★ 保守性：有一个不安全就整批串行
  4. 结果按原索引顺序写回（乱了会让框架 400）
  5. 真的节省了时间（并发而非排队）
"""
from __future__ import annotations

import asyncio
import importlib.util as ilu
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent.parent   # 插件根
FAILED: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'BAD'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


spec = ilu.spec_from_file_location("parallel_tools", str(HERE / "parallel_tools.py"))
pt = ilu.module_from_spec(spec)
spec.loader.exec_module(pt)

ToolGate = pt.ToolGate


def tc(name, args="{}", cid=None):
    return {"id": cid or f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": args}}


print("1) 默认黑名单：已验证有副作用的工具一律不安全")
g = ToolGate(enabled=True)
for n in ("exec", "write_file", "edit_file", "manage_background_exec",
          "memory_add", "memory_update", "memory_remove", "session_send",
          "manage_ignore", "spawn_subagent", "call_subagent",
          "tool_navigate", "tool_script", "tool_cookie"):
    check(f"{n} 被判为不安全", not g.is_safe(n))

print("\n2) 默认黑名单外的工具（白名单留空 ⇒ 放行）")
for n in ("read_file", "list_files", "search_files", "grep", "extract_webpage",
          "some_unknown_plugin_tool"):
    check(f"{n} 放行", g.is_safe(n))

print("\n3) 关键词命中（默认非全字匹配）")
g2 = ToolGate(enabled=True, match_exact=False)
check("exec 能命中 manage_background_exec（非全字）",
      not g2.is_safe("manage_background_exec"))   # 已在黑名单，本来就不安全
g3 = ToolGate(enabled=True, whitelist=["read"], match_exact=False)
check("白名单 read 能命中 read_file", g3.is_safe("read_file"))
check("白名单 read 也能命中 thread_read（关键词命中）", g3.is_safe("thread_read"))
g4 = ToolGate(enabled=True, whitelist=["read"], match_exact=True)
check("全字匹配下 read 不命中 read_file", not g4.is_safe("read_file"))
check("全字匹配下 read 命中 read", g4.is_safe("read"))

print("\n4) 黑名单优先于白名单")
g5 = ToolGate(enabled=True, whitelist=["exec"], blacklist=["exec"])
check("同时出现在黑白名单 ⇒ 串行（黑名单优先）", not g5.is_safe("exec"))

print("\n5) 白名单非空 ⇒ 只放行白名单")
g6 = ToolGate(enabled=True, whitelist=["read_file"])
check("白名单内放行", g6.is_safe("read_file"))
check("白名单外不放行", not g6.is_safe("list_files"))

print("\n6) 整批判定：保守规则")
g7 = ToolGate(enabled=True)
ok, why = g7.can_parallelize([tc("read_file"), tc("list_files")])
check("全安全 ⇒ 可并行", ok, why)
ok2, why2 = g7.can_parallelize([tc("read_file"), tc("exec")])
check("★ 有一个不安全 ⇒ 整批串行", not ok2, why2)
ok3, why3 = g7.can_parallelize([tc("read_file")])
check("单个工具 ⇒ 不并行（无收益）", not ok3, why3)
ok4, why4 = g7.can_parallelize([tc("read_file")] * 20)
check("超过 max_parallel ⇒ 不并行", not ok4, why4)
ok5, why5 = ToolGate(enabled=False).can_parallelize([tc("read_file"), tc("list_files")])
check("开关关闭 ⇒ 不并行", not ok5, why5)

print("\n7) ★ 真的并发（时间验证）")


async def timing_demo(parallel: bool):
    calls = [tc("read_file", cid=f"c{i}") for i in range(3)]

    async def work(i):
        await asyncio.sleep(0.15)
        return f"result-{i}"

    t0 = time.perf_counter()
    if parallel:
        jobs = pt.split_calls(calls, None)
        res = await pt.run_parallel(lambda i: work(i), timeout=None, count=len(jobs))
    else:
        res = []
        for i in range(len(calls)):
            res.append(await work(i))
    return time.perf_counter() - t0, res


seq_t, seq_r = asyncio.run(timing_demo(False))
par_t, par_r = asyncio.run(timing_demo(True))
print(f"     串行 3×150ms = {seq_t*1000:6.1f}ms")
print(f"     并行 3×150ms = {par_t*1000:6.1f}ms")
check("并行明显快于串行", par_t < seq_t * 0.6, f"{par_t:.3f} vs {seq_t:.3f}")
check("★ 结果顺序与串行一致", seq_r == par_r, f"{seq_r} vs {par_r}")

print("\n8) run_parallel 的异常与超时处理")
async def exc_demo():
    async def boom(i):
        if i == 1:
            raise RuntimeError("boom")
        return i
    return await pt.run_parallel(boom, timeout=None, count=3)


r = asyncio.run(exc_demo())
check("异常被 return_exceptions 收住（不会中断其他任务）",
      isinstance(r[1], RuntimeError) and r[0] == 0 and r[2] == 2, f"{r}")


async def to_demo():
    async def slow(i):
        await asyncio.sleep(0.5)
        return i
    return await pt.run_parallel(slow, timeout=0.1, count=2)


r2 = asyncio.run(to_demo())
check("超时被捕获为异常对象（与其他任务互不影响）",
      all(isinstance(x, Exception) for x in r2), f"{r2}")

print("\n9) 黑名单自定义（留空用默认）")
g8 = ToolGate(enabled=True, blacklist=[])
check("黑名单留空仍使用内置默认", len(g8.blacklist) == len(pt.DEFAULT_BLACKLIST))
check("exec 仍不安全", not g8.is_safe("exec"))
g9 = ToolGate(enabled=True, blacklist=["read_file"])
check("自定义黑名单生效", not g9.is_safe("read_file"))
check("自定义后 exec 被放行（因为替换了默认）", g9.is_safe("exec"))

print()
if FAILED:
    print(f"FAIL {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("PASS 全部通过")
