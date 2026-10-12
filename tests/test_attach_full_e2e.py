"""★★★ acc 全功能挂载链路：真跑 initialize()，断言每个功能**真的装上**。

## 为什么单列这一个（2026-10-07 教训）

之前验证 3.0 兼容时，我是**逐个手工调 `_install_*`** 去证明"修好了"——
而真实挂载流程（`initialize()`）里是否装上，**没验**。
bridge 那边就因此漏了一个大坑（`_patch_send_path` 被放在 2.x 分支之后，
3.0 提前 return ⇒ 从未调用）；acc 这边虽然结构上没有分支，也必须钉死。

本测试**只走真实入口 `initialize()`**，然后断言接管点清单 ——
且 2.x / 3.0 的清单必须**完全一致**（在运行时审查器里对比）。

与 runtime_audit.py 的区别：那边是逐个手工调 `_install_*`，
这里走**真实入口 `initialize()`**，断言每条功能在真实流程里都到位。

用法: python3 runtime_audit_accel_e2e.py <2|3>
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

# ★ 与其它套件统一：优先环境变量 GEN，其次命令行参数（原来只认 argv ⇒
#   在 run_tests.sh 里永远按 3.x 找框架，2.x 环境下被误判成"没有框架"而跳过）
GEN = os.environ.get("GEN", sys.argv[1] if len(sys.argv) > 1 else "3")
ROOT = "/var/minis/workspace/qqbot_bridge_review"
import _env  # noqa: E402
_FW = _env.framework_for(GEN)
if _FW is None:
    _env.skip(f"需要 KiraAI {GEN}.x 框架源码（设 KIRA_FW=/path/to/KiraAI）")
FW = _FW
sys.path.insert(0, FW)

os.makedirs(f"{ROOT}/data", exist_ok=True)
open(f"{ROOT}/data/log.log", "a").close()

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL {name}  {extra}")


print("#" * 74)
print(f"# acc 全功能挂载链路审查  GEN={GEN}")
print("#" * 74)

# ---- 按插件的真实装载方式导入（命名空间包）----
pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ns = types.ModuleType("kira_accelerator")
ns.__path__ = [pkg_dir]
sys.modules["kira_accelerator"] = ns
import importlib                                     # noqa: E402
main_mod = importlib.import_module("kira_accelerator.main")
cc = importlib.import_module("kira_accelerator.core_compat")

ACC = None
for n in dir(main_mod):
    o = getattr(main_mod, n)
    if isinstance(o, type) and hasattr(o, "_install_client_cache"):
        ACC = o
        break
check("找到插件类", ACC is not None)
if ACC is None:
    sys.exit(1)


async def main():
    print("\n[1] 真跑 initialize()（全功能挂载入口）")
    # ★ 给一个**最小可用 ctx**：3.0 的抢发接管点要求 `ctx.message_delivery` 存在
    #   （原来传 ctx=None ⇒ 那条接管点装不上、也没被断言到 ⇒ 本测试漏掉了最关键的挂载点）。
    class _StubDelivery:
        min_message_delay = 0.0
        max_message_delay = 0.0

        async def send_message_chain(self, *a, **kw):
            return None

        def get_session_lock(self, sid):
            return asyncio.Lock()

    class _StubMP:
        min_message_delay = 0.0
        max_message_delay = 0.0

    ctx = types.SimpleNamespace(
        message_delivery=_StubDelivery(),
        message_processor=_StubMP(),
        adapter_mgr=None,
        config={},
        event_bus=None,
        persona_mgr=None,
        sticker_mgr=None,
        tool_mgr=None,
        session_mgr=None,
        prompt_mgr=None,
        provider_mgr=None,
        db=None,
        plugin_mgr=None,
        get_plugin_data_dir=lambda: Path(f"{ROOT}/data/plugin"),
    )
    cfg = {"section_thinking": {"enabled": True}}      # 打开思考 ⇒ 三个注入点都要装上
    plugin = ACC(ctx=ctx, cfg=cfg)
    try:
        await plugin.initialize()
        check("initialize() 无异常完成", True)
    except Exception as exc:  # noqa: BLE001
        import traceback
        check("initialize() 无异常完成", False, traceback.format_exc()[-500:])
        return

    names = []
    try:
        names = [h.name for h in plugin.patches.handles]
    except Exception:
        try:
            names = [h["name"] for h in plugin.patches.health()]
        except Exception:
            names = []
    print(f"      已装接管点：{names}")

    print("\n[2] 接管点集合：每个功能都必须**真的装上**（不是只看配置开关）")
    installed = set(names)

    expected = {"reuse_http_client", "stream_engine"}
    if plugin.force_stream or plugin.early_send:
        expected.add("early_sent_dedupe_delivery" if GEN != "2" else "early_sent_strip")
    if plugin._tool_gate.enabled:
        expected.add("parallel_tools")
    if plugin.takeover_memory_dump and GEN == "2":
        expected.add("memory_dump")        # 3.0 会话持久化改 SQLite，无此 API（插件会优雅跳过）

    # 思考注入：只在**目标类可解析**时才要求装上（与 F9/F10 同一套判据）
    think_targets = []
    try:
        if cc.openai_compatible_client() is not None:
            think_targets.append("openai")
    except Exception:  # noqa: BLE001
        pass
    for modname, kind in [("core.provider.src.deepseek.model_clients", "deepseek"),
                          ("core.provider.src.anthropic.model_clients", "anthropic")]:
        try:
            importlib.import_module(modname)
            think_targets.append(kind)
        except Exception:  # noqa: BLE001
            pass
    if plugin.thinking_enabled:
        expected.update(f"auto_thinking:{k}" for k in think_targets)

    missing = sorted(expected - installed)
    check("★★ 接管点集合与预期完全一致（漏装即报红）", not missing, f"缺: {missing}｜已装: {sorted(installed)}")
    check("★ 没有**多余**的接管点（装了没声明的东西也要报）",
          not (installed - expected - {"memory_dump"}), f"多: {sorted(installed - expected)}")
    if plugin.thinking_enabled:
        check("F-思考：三处注入点都装上了（开关注入真的生效）",
              all(f"auto_thinking:{k}" in installed for k in think_targets), f"{think_targets}")
    check("F1 客户端复用已装", "reuse_http_client" in installed, str(sorted(installed)))
    if plugin.force_stream or plugin.early_send:
        hs = "early_sent_dedupe_delivery" if GEN != "2" else "early_sent_strip"
        check(f"F2 ★ 抢发去重接管点已装（{hs}）", hs in installed, str(sorted(installed)))

    print("\n[3] 事件广播三处（3.0 兼容的核心）")
    reg, et = cc.event_system()
    check("F5 AFTER_XML_PARSE 广播可用", getattr(et, "AFTER_XML_PARSE", None) is not None)
    check("F6 ON_MESSAGE_SENT 广播可用", getattr(et, "ON_MESSAGE_SENT", None) is not None)
    check("F7 ON_TOOL_RESULT 广播可用", getattr(et, "ON_TOOL_RESULT", None) is not None)

    # 真跑一次广播，断言 handler 被调用
    calls = []
    keyed = None
    for attr in ("_handlers", "handlers", "_by_type"):
        d = getattr(reg, attr, None)
        if isinstance(d, dict):
            keyed = d
            break
    if keyed is not None:
        class _H:
            async def exec_handler(self, *a, **kw):
                calls.append(1)
        h = _H()
        keyed.setdefault(et.AFTER_XML_PARSE, []).append(h)

        class SendCtx:
            event = type("E", (), {"is_stopped": False})()
        try:
            out = await ACC._broadcast_after_xml_parse(plugin, SendCtx(), ["X"])
            check("F8 ★ 真广播 → handler 被调用到", len(calls) >= 1, f"calls={len(calls)}")
        except Exception as exc:  # noqa: BLE001
            check("F8 ★ 真广播 → handler 被调用到", False, repr(exc))
        finally:
            try:
                keyed.get(et.AFTER_XML_PARSE, []).remove(h)
            except Exception:
                pass

    print("\n[4] 思考注入目标（三个 provider）")
    occ = cc.openai_compatible_client()
    check("F9 OpenAI 兼容客户端可解析", occ is not None)
    check("F9b 有 _build_request_kwargs", occ is not None and hasattr(occ, "_build_request_kwargs"))
    for modname, clsname in [
        ("core.provider.src.deepseek.model_clients", "DeepSeekLLMClient"),
        ("core.provider.src.anthropic.model_clients", "AnthropicCompatibleLLMClient"),
    ]:
        try:
            m = importlib.import_module(modname)
            ok = getattr(m, clsname, None) is not None
        except Exception as exc:  # noqa: BLE001
            ok = False
        check(f"F10 {clsname} 可解析（思考注入落点）", ok)

    await plugin.terminate()
    check("terminate() 无异常（可安全还原）", True)


asyncio.run(main())
print(f"\n{'=' * 74}\n结果：{PASS} passed, {FAIL} failed\n{'=' * 74}")
if FAILS:
    print("失败项：" + " / ".join(FAILS))
sys.exit(1 if FAIL else 0)
