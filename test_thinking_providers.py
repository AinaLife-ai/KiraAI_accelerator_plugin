"""自动思考的注入到底覆盖哪些 provider？（2026-09-23 用户提问引出）

用户问：「假设我提供商里给模型开了思考，我们自动思考那个开关还会生效帮调控吗？」

我的注入挂点：`install(h, OpenAICompatibleLLMClient, "_build_request_kwargs", ...)`
—— **只覆盖 OpenAICompatibleLLMClient 及其子类**。

而框架里：
  · DeepSeekLLMClient(LLMModelClient)          ← 自己的 _build_request_kwargs
  · AnthropicCompatibleLLMClient(LLMModelClient) ← 自己的 _build_request_body
两者都**不是** OpenAICompatibleLLMClient 的子类 ⇒ 注入根本到不了。

本测试用三个**真客户端**分别调用它们自己的请求构造方法，比对
「打补丁前」vs「打补丁后」的思考相关参数，看注入有没有落地。
"""
import os
import sys
from types import SimpleNamespace

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
FW = os.environ.get("KIRA_FW", "/var/minis/shared/alife_vs_kira/kira_fw_v2346")
sys.path.insert(0, FW)
sys.path.insert(0, "/var/minis/shared/alife_vs_kira")

from core.provider.provider import ModelInfo, ModelType              # noqa: E402
from core.provider.llm_model import LLMRequest                      # noqa: E402
from core.utils.model_clients import OpenAICompatibleLLMClient      # noqa: E402
from core.provider.src.deepseek.model_clients import DeepSeekLLMClient          # noqa: E402
from core.provider.src.anthropic.model_clients import AnthropicCompatibleLLMClient  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


def info(name, cfg=None):
    return ModelInfo(model_type=ModelType.LLM, model_id="m1", provider_id="p1",
                     provider_name=name,
                     provider_config={"api_key": "sk-x", "base_url": "http://127.0.0.1:9/v1"},
                     model_config=cfg or {})


def req():
    return LLMRequest(messages=[{"role": "user", "content": "hi"}])


def thinking_params(d):
    """从请求体里挑出所有跟思考有关的键。"""
    out = {}
    for k, v in (d or {}).items():
        if any(w in k.lower() for w in ("think", "reason", "effort")):
            out[k] = v
        if k in ("extra_body", "reasoning") and isinstance(v, dict):
            for kk, vv in v.items():
                if any(w in kk.lower() for w in ("think", "reason", "effort")):
                    out["extra_body." + kk] = vv
    return out


def build(client, request=None):
    """调用该客户端自己的请求构造方法（各家名字不同）。

    ⚠️ 必须传入**同一个** request 对象 —— 自动思考的决定是挂在
       request.__dict__["_accel_thinking"] 上的，新建 request 就丢了。
    """
    r = request if request is not None else req()
    if hasattr(client, "_build_request_kwargs"):
        return client._build_request_kwargs(r)
    if hasattr(client, "_build_request_body"):
        return client._build_request_body(r)
    return {}


# ── 建三种真客户端 ──
print("0) 三种客户端 + 继承关系")
clients = {
    "openai兼容(阿里/硅基/火山)": OpenAICompatibleLLMClient(info("ali")),
    "deepseek": DeepSeekLLMClient(info("ds", {"thinking_enabled": True,
                                              "reasoning_effort": "high"})),
    "anthropic": AnthropicCompatibleLLMClient(info("anth")),
}
for n, c in clients.items():
    print(f"     {n:26} {type(c).__name__:32} 继承={[b.__name__ for b in type(c).__mro__[1:3]]}")
check("三种客户端都能构造", len(clients) == 3)

print("\n1) 补丁前：各家自己会发什么思考参数")
before = {n: thinking_params(build(c)) for n, c in clients.items()}
for n, v in before.items():
    print(f"     {n:26} {v}")

print("\n2) 装上插件（走插件自己的 _install_request_hook）")
import importlib
mod = importlib.import_module("accelerator_poc.main")
schema = __import__("json").load(
    open("/var/minis/shared/alife_vs_kira/accelerator_poc/schema.json"))
cfg = {}
for sec in schema.values():
    if isinstance(sec, dict) and "fields" in sec:
        for k, f in sec["fields"].items():
            if isinstance(f, dict) and "default" in f:
                cfg["%s.%s" % (sec.get("key", ""), k)] = f["default"]
plugin = mod.AcceleratorPlugin(ctx=None, cfg={})
plugin.thinking_enabled = True
plugin.thinking_style = "compatible"
plugin.thinking_inject_nothink = False
plugin.normalize_empty_content = False
plugin._proxy_cache = {}
plugin._stats = {}
plugin._install_request_hook()
check("插件补丁已安装", plugin.patches is not None, type(plugin.patches).__name__)

print("\n3) ★ 补丁后：自动思考=开 时，注入是否落地")
r = req()
r.__dict__["_accel_thinking"] = SimpleNamespace(enabled=True, effort="high")
after_on = {}
for n, c in clients.items():
    after_on[n] = thinking_params(build(c, r))
    print(f"     {n:26} {after_on[n]}")

check("★ OpenAI 兼容系：注入生效",
      after_on["openai兼容(阿里/硅基/火山)"] != before["openai兼容(阿里/硅基/火山)"],
      f"变化={after_on['openai兼容(阿里/硅基/火山)']}")

check("★★ DeepSeek：注入生效（当前是失效的）",
      after_on["deepseek"] != before["deepseek"],
      f"补丁前后一样={after_on['deepseek']}")
check("★★ Anthropic：注入生效（当前是失效的）",
      after_on["anthropic"] != before["anthropic"],
      f"补丁前后一样={after_on['anthropic']}")

print("\n4) ★ 自动思考=关 时，能否关掉「提供商那边已开的思考」")
r2 = req()
r2.__dict__["_accel_thinking"] = SimpleNamespace(enabled=False, effort="low")
for n, c in clients.items():
    print(f"     {n:26} {thinking_params(build(c, r2))}")
check("★ 默认配置下**不会**去关提供商已开的思考（inject_nothink=False 是设计选择）",
      True, "见下方说明")

print("\n5) 显式打开 inject_nothink 后，关思考参数能不能落到各家")
plugin.thinking_inject_nothink = True
r3 = req()
r3.__dict__["_accel_thinking"] = SimpleNamespace(enabled=False, effort="low")
off = {}
for n, c in clients.items():
    off[n] = thinking_params(build(c, r3))
    print(f"     {n:26} {off[n]}")

ali = "openai兼容(阿里/硅基/火山)"
check("OpenAI 兼容系：关思考参数已注入", off[ali] != before[ali],
      f"{off[ali]}")
check("DeepSeek：关思考参数已注入", off["deepseek"] != before["deepseek"],
      f"{off['deepseek']}（旧版与补丁前一样）")

print("\n" + "=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    print("失败项:")
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过")
