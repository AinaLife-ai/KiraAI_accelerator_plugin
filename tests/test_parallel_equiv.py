"""★ 并行 vs 串行 —— 行为等价性测试（这是安全性的核心证据）

用**完整复刻框架 execute_tool 语义**的两个实现跑同一批工具调用，逐字段比对：
  A. 串行版（完全照抄框架 core/agent/func_tool_manager.py 的逻辑）
  B. 并行版（我们插件里 _install_parallel_tools 的逻辑）

判据：`resp.tool_results` 必须**逐字节相同**，包括顺序。
"""
from __future__ import annotations

import asyncio
import importlib.util as ilu
import inspect
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent   # 插件根
FAILED: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'OK ' if cond else 'BAD'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


spec = ilu.spec_from_file_location("parallel_tools", str(HERE / "parallel_tools.py"))
pt = ilu.module_from_spec(spec)
spec.loader.exec_module(pt)


# ── 极简的框架替身 ──
class ToolResult:
    def __init__(self, text=""):
        self.text = text

    async def assemble_result(self):
        return self.text


class Tool:
    """模拟框架的 BaseTool：execute 是 async，返回一个**具体值**（不是协程）。"""

    def __init__(self, name, fn, delay=0.0):
        self.name = name
        self._fn = fn
        self._delay = delay

    async def execute(self, event, **kwargs):
        if self._delay:
            await asyncio.sleep(self._delay)
        out = self._fn(**kwargs)
        # 若测试给的 fn 本身是 async，这里 await 出真实值 —— 否则会原样返回
        # 一个 coroutine 对象，导致"结果相同"的比对失去意义（上一版就栽在这）
        if inspect.isawaitable(out):
            out = await out
        return out


class ToolSet:
    def __init__(self, tools):
        self.tools = tools

    def __contains__(self, name):
        return any(t.name == name for t in self.tools)

    def get(self, name):
        return next(t for t in self.tools if t.name == name)


class Resp:
    def __init__(self, calls):
        self.tool_calls = calls
        self.tool_results = []


class Event:
    def __init__(self):
        self.is_stopped = False


def tc(name, args, cid):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


# ── A. 串行版：逐行照抄框架实现 ──
async def execute_serial(resp, tool_set, max_calls=5, timeout=None):
    for idx, tool_call in enumerate(resp.tool_calls):
        tool_call_id = tool_call.get("id")
        name = tool_call.get("function", {}).get("name")
        if idx >= max_calls:
            resp.tool_results.append({
                "role": "tool", "tool_call_id": tool_call_id, "name": name,
                "content": f"Tool call limit exceeded: maximum {max_calls} tool calls per turn, skipping tool '{name}'."})
            continue
        raw_args = tool_call.get("function", {}).get("arguments")
        try:
            args = {} if not raw_args.strip() else json.loads(raw_args)
        except json.JSONDecodeError:
            args = {}
        if tool_set and name in tool_set:
            try:
                inst = tool_set.get(name)
                coro = inst.execute(None, **args)
                result = await (asyncio.wait_for(coro, timeout) if timeout else coro)
            except Exception as e:
                result = {"error": f"Failed to call tool '{name}': {e}"}
        else:
            result = {"error": f"Tool {name} not implemented"}
        tro = result if isinstance(result, ToolResult) else ToolResult(str(result))
        content = await tro.assemble_result()
        resp.tool_results.append({
            "role": "tool", "tool_call_id": tool_call_id, "name": name, "content": content})


# ── B. 并行版：复刻插件里的两阶段逻辑 ──
async def execute_parallel(resp, tool_set, gate, max_calls=5, timeout=None):
    calls = resp.tool_calls
    to_run = calls[:max_calls] if max_calls >= 0 else []
    ok, reason = gate.can_parallelize(to_run)
    assert ok, reason

    async def run_one(tc_):
        name = (tc_.get("function") or {}).get("name")
        raw = (tc_.get("function") or {}).get("arguments") or ""
        try:
            args = {} if not raw.strip() else json.loads(raw)
        except Exception:
            args = {}
        if not (tool_set and name in tool_set):
            return {"__error": f"Tool {name} not implemented"}
        try:
            inst = tool_set.get(name)
            coro = inst.execute(None, **args)
            return await (asyncio.wait_for(coro, timeout) if timeout else coro)
        except Exception as e:
            return {"__error": f"Failed to call tool '{name}': {e}"}

    raw_results = await asyncio.gather(*[run_one(t) for t in to_run])

    for idx, tool_call in enumerate(calls):
        tool_call_id = tool_call.get("id")
        name = (tool_call.get("function") or {}).get("name")
        if idx >= max_calls:
            resp.tool_results.append({
                "role": "tool", "tool_call_id": tool_call_id, "name": name,
                "content": f"Tool call limit exceeded: maximum {max_calls} tool calls per turn, skipping tool '{name}'."})
            continue
        result = raw_results[idx]
        if isinstance(result, dict) and "__error" in result:
            result = {"error": result["__error"]}
        tro = result if isinstance(result, ToolResult) else ToolResult(str(result))
        content = await tro.assemble_result()
        resp.tool_results.append({
            "role": "tool", "tool_call_id": tool_call_id, "name": name, "content": content})


async def run_case(label, calls, tools, gate_kw=None, max_calls=5):
    ts = ToolSet(tools)
    a = Resp([dict(c) for c in calls])
    b = Resp([dict(c) for c in calls])
    await execute_serial(a, ts, max_calls=max_calls)
    gate = pt.ToolGate(enabled=True, **(gate_kw or {}))
    await execute_parallel(b, ts, gate, max_calls=max_calls)
    same = a.tool_results == b.tool_results
    check(f"{label}：结果逐字节相同", same,
          f"\n     串行={a.tool_results}\n     并行={b.tool_results}")
    return a, b


async def main():
    print("1) 基础等价性（3 个读操作，乱序返回）")

    async def vary(**kw):
        # 故意让不同工具返回耗时不同，制造"完成顺序 != 调用顺序"
        await asyncio.sleep(kw.get("d", 0))
        return f"content-of-{kw.get('tag')}"

    tools = [
        Tool("read_file", lambda tag=None, d=0: vary(tag=tag, d=d), ),
        Tool("list_files", lambda tag=None, d=0: vary(tag=tag, d=d)),
        Tool("grep", lambda tag=None, d=0: vary(tag=tag, d=d)),
    ]
    calls = [
        tc("read_file", {"tag": "A", "d": 0.15}, "c1"),   # 最慢，但排第一
        tc("list_files", {"tag": "B", "d": 0.01}, "c2"),
        tc("grep", {"tag": "C", "d": 0.05}, "c3"),
    ]
    await run_case("慢的在前面", calls, tools)

    print("\n2) 工具报错时")
    async def boom(**kw):
        raise RuntimeError("tool exploded")

    tools2 = [Tool("read_file", lambda **k: "ok"),
              Tool("grep", boom),
              Tool("list_files", lambda **k: "fine")]
    calls2 = [tc("read_file", {}, "x1"), tc("grep", {}, "x2"), tc("list_files", {}, "x3")]
    await run_case("中间一个工具抛异常", calls2, tools2)

    print("\n3) 工具不存在")
    tools3 = [Tool("read_file", lambda **k: "ok")]
    calls3 = [tc("read_file", {}, "y1"), tc("not_exist", {}, "y2")]
    await run_case("含未注册工具", calls3, tools3, gate_kw={"blacklist": []})

    print("\n4) 超出 max_tool_calls_per_turn（框架会插告警条目）")
    tools4 = [Tool("read_file", lambda **k: "r")]
    calls4 = [tc("read_file", {}, f"z{i}") for i in range(4)]
    await run_case("4 个调用但上限 2", calls4, tools4, max_calls=2)

    print("\n5) 参数非法（空 / 非 JSON）")
    tools5 = [Tool("read_file", lambda **k: "ok")]
    calls5 = [
        {"id": "w1", "type": "function", "function": {"name": "read_file", "arguments": ""}},
        {"id": "w2", "type": "function", "function": {"name": "read_file", "arguments": "not-json"}},
    ]
    await run_case("空参数与非 JSON 参数", calls5, tools5)

    print("\n6) 时间验证：并行确实更快且结果不变")
    slow = [
        Tool("read_file", lambda tag=None: f"a-{tag}", delay=0.12),
        Tool("list_files", lambda tag=None: f"b-{tag}", delay=0.12),
        Tool("grep", lambda tag=None: f"c-{tag}", delay=0.12),
    ]
    calls6 = [tc("read_file", {"tag": "1"}, "t1"), tc("list_files", {"tag": "2"}, "t2"), tc("grep", {"tag": "3"}, "t3")]

    import time
    ts = ToolSet(slow)
    r1 = Resp([dict(c) for c in calls6])
    t0 = time.perf_counter(); await execute_serial(r1, ts); st = time.perf_counter() - t0

    ts2 = ToolSet(slow)
    r2 = Resp([dict(c) for c in calls6])
    t0 = time.perf_counter()
    await execute_parallel(r2, ts2, pt.ToolGate(enabled=True)); ptt = time.perf_counter() - t0

    print(f"     串行 {st*1000:.0f}ms / 并行 {ptt*1000:.0f}ms")
    check("并行更快", ptt < st * 0.6, f"{ptt:.3f} vs {st:.3f}")
    check("结果仍然逐字节相同", r1.tool_results == r2.tool_results)


asyncio.run(main())

print()
if FAILED:
    print(f"FAIL {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("PASS 全部通过")
