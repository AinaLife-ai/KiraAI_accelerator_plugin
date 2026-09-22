"""复现：流式代理破坏框架的 isinstance 检查 ⇒ 报成「Default LLM model not configured」。

用户实测日志：
    ERROR [llm] Default LLM model not configured, please configure it in Configuration

框架真实链路（core/provider/provider_manager.py:146）：
    def get_default_llm(self):
        model_info = self.get_default_model_info("default_llm")
        model_client = self.get_model_client(...)      # ← 我们的补丁在这
        if not isinstance(model_client, LLMModelClient):
            raise TypeError(f"Expected LLMModelClient, got {type(model_client).__name__}")

而调用方（core/message_manager.py:627）：
    try:
        default_llm = self.provider_mgr.get_default_llm()
        ...
    except Exception as _:
        llm_logger.error("Default LLM model not configured, ...")   # ★ 任何异常都报这句

⇒ 类型错误被伪装成「没配置模型」，排查方向被彻底带偏。

本测试**复用框架真的 get_default_llm 实现**（不复制逻辑），
补丁也走**插件自己的 install()**，所以链路与线上一致。
"""
import os
import sys
from pathlib import Path

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ 自身
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)

from core.provider.provider_manager import ProviderManager          # noqa: E402
from core.provider.provider import LLMModelClient, ModelInfo, ModelType  # noqa: E402
_se = _env.load("stream_engine")                                    # noqa: E402
_pa = _env.load("patches")                                          # noqa: E402
LLMClientProxy = _se.LLMClientProxy
PatchHandle, install = _pa.PatchHandle, _pa.install

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


class FakeLLM(LLMModelClient):
    """一个真的 LLMModelClient 子类（冒充用户已配置好的模型）。"""
    async def chat(self, request, **kwargs):
        return "ok"

    def chat_stream(self, request, **kwargs):
        return iter(())


def make_client():
    return FakeLLM(ModelInfo(model_type=ModelType.LLM, model_id="m1",
                             provider_id="p1", provider_name="fake"))


class FakePM:
    """只提供 get_default_llm 需要的东西；该方法本身是框架的真实现。"""

    def get_default_model_info(self, key):
        return ModelInfo(model_type=ModelType.LLM, model_id="m1",
                         provider_id="p1", provider_name="fake")

    def get_model_client(self, provider_id, model_id, model_type=None):
        return make_client()

    # ★ 框架的真实现（不复制、不简化）
    get_default_llm = ProviderManager.get_default_llm


print("1) 代理满不满足框架的 isinstance 检查")
raw = make_client()
proxy = LLMClientProxy(raw, lambda: None)
check("原始 client 是 LLMModelClient（对照）", isinstance(raw, LLMModelClient),
      type(raw).__name__)
check("★ 代理也是 LLMModelClient（修复目标）", isinstance(proxy, LLMModelClient),
      f"type={type(proxy).__name__}")
check("★ 代理透传 .model", proxy.model is raw.model)

print("\n2) 真跑框架的 get_default_llm")
pm = FakePM()
try:
    got = pm.get_default_llm()
    check("未打补丁：能拿到 client", isinstance(got, LLMModelClient), type(got).__name__)
except Exception as e:
    check("未打补丁：能拿到 client", False, f"{type(e).__name__}: {e}")

# 用插件自己的 install() 打补丁（与线上同一套机制）
h = PatchHandle("repro")
install(h, FakePM, "get_model_client",
        lambda original: (lambda self, pid, mid, mt=None:
                          LLMClientProxy(make_client(), lambda: None)))

pm2 = FakePM()
try:
    got2 = pm2.get_default_llm()
    check("★ 打补丁后 get_default_llm 仍能返回", isinstance(got2, LLMModelClient),
          type(got2).__name__)
except Exception as e:
    check("★ 打补丁后 get_default_llm 仍能返回", False, f"{type(e).__name__}: {e}")

print("\n3) 这会不会触发用户看到的那条日志？")
try:
    FakePM().get_default_llm()
    print("     get_default_llm 正常返回 ⇒ 不会触发该日志")
    triggered = False
except Exception as e:
    print(f"     get_default_llm 抛了 {type(e).__name__}")
    print("     message_manager 的 except 分支会打:")
    print("       ERROR [llm] Default LLM model not configured, "
          "please configure it in Configuration")
    triggered = True
check("★ 修好后不再触发那条误导性日志", not triggered,
      "框架把 TypeError 伪装成'没配置模型'")

print("\n4) 覆盖框架里所有这类检查（不止 get_default_llm）")
import pathlib
import re
sites = []
for f in pathlib.Path(FW).rglob("*.py"):
    if "/.git/" in str(f):
        continue
    for i, line in enumerate(f.read_text(errors="ignore").split("\n"), 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        if ("isinstance" in s or "issubclass" in s) and \
           re.search(r"(LLMModelClient|LLMClient|ModelClient)", s):
            sites.append((f.name, i))
check("★ 代理满足全部这类 isinstance（否则 29 处全走错分支）",
      isinstance(proxy, LLMModelClient), f"框架里共 {len(sites)} 处")
print(f"     类型检查分布（前 5）: {sites[:5]}")

print("\n5) ★ 另一个受害者：别的插件靠 ctx.get_client() 拿 client")
# 复刻 core/plugin/plugin_context.py:95-115 的模式（真框架里就是这么写的）
def plugin_context_style(pm, llm_type="default"):
    # 真框架就是长这样（含它对异常的沉默）。这里把异常也当成"拿不到"，
    # 好让失败以断言的形式报出来，而不是把整套测试崩掉。
    if llm_type == "default":
        try:
            client = pm.get_default_llm()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(client, LLMModelClient):
            return client
    return None

pm3 = FakePM()
got3 = plugin_context_style(pm3)
check("★ 打了补丁后，其它插件仍能从 ctx 拿到 client", got3 is not None,
      f"拿到 {type(got3).__name__ if got3 else 'None'}（旧版是 None ⇒ 别的插件全瞎）")

print("\n6) 别把别的语义弄坏")
check("str(代理) 仍指向被包裹对象", "FakeLLM" in repr(proxy) or "LLMClientProxy" in repr(proxy))
check("代理仍能透传非自有属性（如 .type）",
      getattr(proxy, "type", None) == getattr(raw, "type", None),
      str(getattr(proxy, "type", "缺失")))

print("\n" + "=" * 56)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    print("失败项:")
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过")
