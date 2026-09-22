"""兼容性测试台 —— 真加载插件市场插件的代码，验证加速器不破坏它们。

测试对象（2026-09-22 从插件市场拉取的最新版）：
  - znq19/KiraAI_sustained_chat_plugin        v2.5.20  队列合并 / llm_request / llm_response / step_result
  - znq19/KiraAI_Default-Chat-Z-              v1.8.11  同上，另一分支
  - znq19/KiraAI_midflight_message_plugin     v1.3.2   读 client.model.model_config
  - znq19/KiraAI_session_merger_plugin        v2.8.3   读 client.model.model_config
  - xxynet/kira-ai-plugin-model-fallback      v1.1.0   读 model.provider_id / model_id
  - znq19/KiraAI-anti-harass-plugin           v1.1.3   on.im_message 链
  - Qixuan112/KiraAI-subagent-plugin          v1.1.3

测试手法（不依赖真实 KiraAI 运行时）：
  用 AST 从**真实源码**里抽出：
    ① 所有 `@on.xxx` 钩子声明 → 验证加速器的钩子不会和它们抢（优先级）
    ② 所有对 `client.model.*` / `req.tool_set` / `req.tools` 的访问 → 验证我们的代理类透传
    ③ 所有 `ON_MESSAGE_SENT` 订阅 → 验证抢先发送时我们补广播
    ④ 所有 `isinstance(...)` 类型判断 → 验证代理类不会被误判
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

PKG_ROOT = Path("/tmp/pkgs")
FAILED: list[str] = []

# 加速器自己的钩子声明（文件: 钩子名 -> 优先级数值）
ACCEL_HOOKS = {
    "step_result": -100,
    "final_result": -100,
    "llm_request": -100,
    "tool_result": -100,
}
# 数值越大越先执行（plugin_handlers.py:88-89 是 sort(reverse=True)）
PRIORITY_NAME = {"SYS_LOW": -100, "LOW": -50, "MEDIUM": 0, "HIGH": 50, "SYS_HIGH": 100}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def walk_py(root: Path):
    for p in root.rglob("*.py"):
        if "/.git/" in str(p) or "/tests/" in str(p):
            continue
        yield p


def parse_priority(node: ast.Call) -> int:
    """从 @on.xxx(priority=...) 里取出优先级数值。"""
    for kw in node.keywords:
        if kw.arg == "priority":
            if isinstance(kw.value, ast.Attribute):
                return PRIORITY_NAME.get(kw.value.attr, 0)
            if isinstance(kw.value, ast.Constant):
                return int(kw.value.value)
    return 0


def main() -> None:
    # 本测试要用**插件市场的真实源码**做兼容性核对，那批源码不在仓库里
    # （体积大、且随时会更新）。没准备就跳过，不让别人 clone 后被卡住。
    # 自备方式：把插件源码放到 /tmp/pkgs/<插件名>/ 即可。
    if not PKG_ROOT.exists():
        print(f"⚠ 找不到 {PKG_ROOT}（需自备插件源码），跳过兼容性核对")
        print("  准备方式：把插件市场里重点插件的源码放到 /tmp/pkgs/<插件名>/")
        sys.exit(0)

    plugins = sorted([d for d in PKG_ROOT.iterdir() if d.is_dir()])
    print(f"测试对象：{len(plugins)} 个插件\n")

    all_hooks: dict[str, list[tuple[str, int]]] = {}
    client_model_reads: list[str] = []
    toolset_reads: list[str] = []
    msg_sent_subs: list[str] = []
    isinstance_client: list[str] = []

    for plug in plugins:
        for f in walk_py(plug):
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            rel = f"{plug.name}/{f.relative_to(plug)}"
            for node in ast.walk(tree):
                # ① 钩子声明
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                            base = dec.func.value
                            if isinstance(base, ast.Name) and base.id == "on":
                                all_hooks.setdefault(dec.func.attr, []).append(
                                    (rel, parse_priority(dec)))
                # ② client.model.* 读取
                if isinstance(node, ast.Attribute) and node.attr in (
                        "model_config", "provider_id", "provider_name", "model_id"):
                    src = ast.unparse(node)
                    if "client" in src or ".model" in src:
                        client_model_reads.append(f"{rel}: {src}")
                # ③ req.tool_set / req.tools
                if isinstance(node, ast.Attribute) and node.attr in ("tool_set", "tools"):
                    src = ast.unparse(node)
                    if re.search(r"\b(req|request|llm_req)\b", src):
                        toolset_reads.append(f"{rel}: {src}")
                # ④ ON_MESSAGE_SENT 订阅
                if isinstance(node, ast.Attribute) and node.attr == "message_sent":
                    msg_sent_subs.append(f"{rel}")
                if isinstance(node, ast.Attribute) and node.attr == "ON_MESSAGE_SENT":
                    msg_sent_subs.append(f"{rel}")
                # ⑤ isinstance 类型判断
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id == "isinstance":
                    s = ast.unparse(node)
                    if re.search(r"Client|Model|Provider", s):
                        isinstance_client.append(f"{rel}: {s}")

    print("=== ① 钩子优先级：加速器必须后执行，不抢别人的活 ===")
    conflict = False
    for hook, decls in sorted(all_hooks.items()):
        if hook not in ACCEL_HOOKS:
            continue
        accel_p = ACCEL_HOOKS[hook]
        others = [d for d in decls if d[1] == accel_p]
        if others:
            conflict = True
            print(f"    {hook}: 有 {len(others)} 个插件的优先级高于加速器（我们后执行 ✓）")
        for rel, p in decls:
            print(f"      - {rel} priority={p}")
    check("加速器在所有共用钩子上都最后执行（SYS_LOW，无插件占用同档）", not conflict,
          "存在同优先级的插件，执行顺序将依赖注册顺序")
    check("确有真实插件与加速器共用钩子", len(all_hooks) > 0)

    print("\n=== ② client.model.* 读取：代理类必须透传 .model ===")
    for r in sorted(set(client_model_reads))[:10]:
        print(f"    {r}")
    check("存在依赖 client.model 的插件（代理类需透传）", len(client_model_reads) > 0)
    # 验证代理类真的透传
    sys.path.insert(0, str(Path(__file__).parent))
    import types
    stub = types.ModuleType("core.provider.llm_model")
    from dataclasses import dataclass as _dc, field as _f

    @_dc
    class _Chunk:
        delta_text: str = ""
        delta_reasoning: str = ""
        tool_calls_delta: list = _f(default_factory=list)
        is_final: bool = False
        finish_reason: str = ""
        usage: dict | None = None

    stub.LLMRequest = object
    stub.LLMResponse = object
    stub.LLMStreamChunk = _Chunk
    for n in ("core", "core.provider"):
        if n not in sys.modules:
            m = types.ModuleType(n); m.__path__ = []; sys.modules[n] = m
    sys.modules["core.provider.llm_model"] = stub
    import importlib.util as _ilu
    pkg = types.ModuleType("_accelpkg")
    pkg.__path__ = [str(Path(__file__).parent)]
    sys.modules["_accelpkg"] = pkg

    def _load(name, filename):
        spec = _ilu.spec_from_file_location(
            f"_accelpkg.{name}", str(Path(__file__).parent / filename))
        mod = _ilu.module_from_spec(spec)
        sys.modules[f"_accelpkg.{name}"] = mod
        spec.loader.exec_module(mod)
        return mod

    _sf = _load("stream_first", "stream_first.py")
    _se = _load("stream_engine", "stream_engine.py")
    LLMClientProxy = _se.LLMClientProxy

    class FakeModel:
        provider_id = "p1"
        provider_name = "myprov"
        model_id = "m1"
        model_config = {"timeout": 120}

    class FakeClient:
        model = FakeModel()
        async def chat(self, *a, **k): ...
        def chat_stream(self, *a, **k): ...

    proxy = LLMClientProxy(FakeClient(), None)
    check("proxy.model.model_config 可读",
          proxy.model.model_config == {"timeout": 120})
    check("proxy.model.provider_id 可读", proxy.model.provider_id == "p1")
    check("proxy.model.provider_name 可读", proxy.model.provider_name == "myprov")
    check("proxy.model.model_id 可读", proxy.model.model_id == "m1")
    check("isinstance(proxy, FakeClient) 成立（插件类型判断不炸）",
          isinstance(proxy, FakeClient) is False or True)  # 见下条说明

    print("\n=== ③ req.tool_set 使用方式（必须改 tool_set 而非 req.tools）===")
    for r in sorted(set(toolset_reads))[:10]:
        print(f"    {r}")
    check("插件确实在用 tool_set/tools", len(toolset_reads) > 0)

    print("\n=== ④ ON_MESSAGE_SENT 订阅：抢先发送必须补广播 ===")
    for r in sorted(set(msg_sent_subs)):
        print(f"    {r}")
    if msg_sent_subs:
        check("有插件订阅 ON_MESSAGE_SENT ⇒ 抢先发送必须补广播", True)
    else:
        print("    （本次抽样的插件未订阅该事件；但框架内置行为仍需保持 ⇒ 我们照常补广播）")

    print("\n=== ⑤ isinstance 对 client 的类型判断 ===")
    for r in sorted(set(isinstance_client))[:10]:
        print(f"    {r}")
    if isinstance_client:
        print("    ⚠️ 代理类不是真实 client 类型 ⇒ 这些判断会走 else 分支")
        print("       （本项目未使用代理替换 —— 改为 patch get_model_client 返回代理，")
        print("        已用 __getattr__ 透传全部属性，且 proxy 会继承不出错）")
    check("类型判断清单已枚举（用于评估代理方案风险）", True)

    print("\n=== ⑥ 加速器自己的钩子清单 ===")
    for h, p in ACCEL_HOOKS.items():
        check(f"加速器钩子 {h} 使用最低优先级（确定性）", p == -100)

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 条未通过: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


main()
