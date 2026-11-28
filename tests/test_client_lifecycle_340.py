"""★★★ KiraAI 3.4.0：`async with self._build_client()` 会关闭客户端 —— 缓存复用不能被它关死。

## 线上现象（3.4.0）

    APIConnectionError: Connection error.

模型健康检查（第一次 chat）正常，随后正式回复全部不可用。

## 根因

3.4.0（#469 "explicitly close async clients to avoid connection leaks"）把
`chat` / `chat_stream` 改成：

    async with self._build_client() as client:
        ...

而加速器的 `reuse_http_client` 会**缓存并复用**同一个 AsyncOpenAI。
第一次请求退出 `async with` 时它被 close ⇒ 下一次拿到已关闭的客户端
⇒ `APIConnectionError: Connection error.`

## 本测试

用**真实 OpenAI SDK** + 本地假 HTTP 服务（不联网）跑：
  · 原生 3.4.0 客户端：两次 chat / 两次 stream 都应成功（基线）；
  · 装上加速器的缓存补丁：同样全部成功（修复前第二次 chat 就会 APIConnectionError）；
  · 缓存里的真实客户端在 `async with` 退出后**没有被关闭**；
  · 外部主动关闭后，下一次 `_build_client()` 会重建（不会复用死客户端）；
  · 卸载时缓存被显式关闭、清空。
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)
if _env.gen() != "3":
    _env.skip("3.4.0 的 async with 生命周期是 3.x 形态")

from core.agent.message import OpenAIMessage                      # noqa: E402
from core.provider.llm_model import LLMRequest                    # noqa: E402
from core.provider.provider import ModelInfo, ModelType           # noqa: E402
from core.provider.openai_compatible import OpenAICompatibleLLMClient  # noqa: E402

main_mod = _env.load("main")
PASS, FAIL = [], []

def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))

class FakeAPI(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:  # noqa: BLE001
            body = {}
        if body.get("stream"):
            chunks = [
                {"id": "1", "object": "chat.completion.chunk", "created": 0, "model": "m",
                 "choices": [{"index": 0, "delta": {"content": "o"}, "finish_reason": None}]},
                {"id": "1", "object": "chat.completion.chunk", "created": 0, "model": "m",
                 "choices": [{"index": 0, "delta": {"content": "k"}, "finish_reason": "stop"}]},
                {"id": "1", "object": "chat.completion.chunk", "created": 0, "model": "m",
                 "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                                          "total_tokens": 5}},
            ]
            data = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                    + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        payload = {
            "id": "1", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

def model_info(port):
    return ModelInfo(
        ModelType.LLM, "m", "p", "P",
        provider_config={"api_key": "sk-x",
                         "base_url": f"http://127.0.0.1:{port}/v1"},
        model_config={},
    )

def request():
    return LLMRequest(messages=[OpenAIMessage(role="user", content="hi")])

async def calls(client, label):
    errors = []
    for i in range(2):
        try:
            resp = await client.chat(request())
            if getattr(resp, "text_response", "") != "ok":
                errors.append(f"chat#{i} bad body {resp!r}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"chat#{i} {type(exc).__name__}: {exc}")
    for i in range(2):
        try:
            text = []
            async for chunk in client.chat_stream(request()):
                text.append(getattr(chunk, "delta_text", "") or "")
            if "".join(text) != "ok":
                errors.append(f"stream#{i} bad body {text!r}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"stream#{i} {type(exc).__name__}: {exc}")
    health = getattr(client, "health_check", None)
    if callable(health):
        for i in range(2):
            try:
                await health()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"health#{i} {type(exc).__name__}: {exc}")
    else:
        print("  -    跳过 health_check（当前框架没有该方法）")
    check(f"{label}：健康检查×2 + chat×2 + stream×2 全成功", not errors,
          " / ".join(errors))

async def main():
    print("#" * 70)
    print("# 3.4.0 客户端生命周期（async with 不得关闭缓存客户端）")
    print("#" * 70)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    plugin = None
    try:
        print("\n[1] 基线：原生 3.4.0（每个请求新建 + 关闭）")
        await calls(OpenAICompatibleLLMClient(model_info(port)), "原生")

        print("\n[2] 加速器缓存补丁：仍然全部成功（修复前第二次 chat 就 APIConnectionError）")
        plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={})
        plugin._client_cache = {}
        plugin.patches.uninstall_all()
        plugin._install_client_cache()
        client = OpenAICompatibleLLMClient(model_info(port))
        await calls(client, "缓存补丁")

        wrapper1 = client._build_client()
        wrapper2 = client._build_client()
        check("同一配置复用同一个外壳", wrapper1 is wrapper2)
        raw = main_mod._raw_http_client(wrapper1)
        check("缓存里的真实客户端没有被 async with 关闭",
              not main_mod._http_client_closed(wrapper1))
        check("_raw_http_client 不会把 AsyncOpenAI 拆成内部 httpx",
              main_mod._raw_http_client(raw) is raw)
        check("缓存只有 1 条", len(plugin._client_cache) == 1,
              str(len(plugin._client_cache)))

        print("\n[2a] 经过加速器的流式代理（真实回复路径）")
        se = _env.load("stream_engine")
        proxy = se.LLMClientProxy(client, lambda _req: se.StreamEngine(force_stream=True))
        try:
            r1 = await proxy.chat(request())
            r2 = await proxy.chat(request())
            proxy_ok = (getattr(r1, "text_response", "") == "ok"
                        and getattr(r2, "text_response", "") == "ok")
            detail = f"{getattr(r1, 'text_response', None)!r}/{getattr(r2, 'text_response', None)!r}"
        except Exception as exc:  # noqa: BLE001
            proxy_ok = False
            detail = f"{type(exc).__name__}: {exc}"
        check("代理 chat×2（健康检查同路径）全部成功", proxy_ok, detail)

        print("\n[2b] 并发复用同一客户端（共享连接池）")
        async def one_stream():
            parts = []
            async for ch in client.chat_stream(request()):
                parts.append(getattr(ch, "delta_text", "") or "")
            return "".join(parts)
        try:
            chats = await asyncio.gather(*[client.chat(request()) for _ in range(8)])
            streams = await asyncio.gather(*[one_stream() for _ in range(4)])
            concurrent_ok = (all(getattr(r, "text_response", "") == "ok" for r in chats)
                             and all(t == "ok" for t in streams))
            detail = f"chat={len(chats)} stream={len(streams)}"
        except Exception as exc:  # noqa: BLE001
            concurrent_ok = False
            detail = f"{type(exc).__name__}: {exc}"
        check("8 路 chat + 4 路 stream 并发全部成功", concurrent_ok, detail)

        print("\n[3] 外部把客户端关掉 ⇒ 必须重建（不复用死连接）")
        await raw.close()
        check("外部关闭后 is_closed() 为真", main_mod._http_client_closed(wrapper1))
        wrapper3 = client._build_client()
        check("下一次取到新外壳", wrapper3 is not wrapper1)
        check("新客户端可用", not main_mod._http_client_closed(wrapper3))
        resp = await client.chat(request())
        check("重建后 chat 正常", getattr(resp, "text_response", "") == "ok")

        print("\n[4] 卸载收尾：缓存被显式关闭并清空")
        cached_raw = main_mod._raw_http_client(wrapper3)
        await plugin._close_client_cache()
        check("缓存已清空", len(plugin._client_cache) == 0)
        try:
            closed = cached_raw.is_closed()
        except Exception:  # noqa: BLE001
            closed = True
        check("卸载时真实客户端已关闭", bool(closed))
    finally:
        try:
            if plugin is not None:
                plugin.patches.uninstall_all()
        except Exception:  # noqa: BLE001
            pass
        server.shutdown()

    print("\n" + "=" * 58)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + " / ".join(FAIL))
        sys.exit(1)
    print("🎉 全部通过 —— 3.4.0 下缓存复用不再被 async with 关死")

asyncio.run(main())
