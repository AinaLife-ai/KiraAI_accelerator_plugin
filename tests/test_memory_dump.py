"""记忆落盘优化：内容必须**逐字节解析一致**，只是快很多。

背景（2026-09-23 审计）：
  框架每轮把**全部会话**的记忆 `json.dumps(indent=4)` 后整文件重写，
  同步执行（阻塞事件循环）。`indent=4` 一个人就占了大头：
      50 会话 × 400 条：带缩进 165.8ms → 不带缩进 26.6ms（快 6.2 倍）
      文件 3033K → 1077K（小 3 倍）

这条优化的**唯一**正当理由是"解析结果一模一样"，所以本测试不只看速度，
更要证明：用新格式写出来的文件，读回来与旧格式**完全相同**。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.makedirs("/tmp/itest/data", exist_ok=True)
os.chdir("/tmp/itest")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

FW = _env.framework()
if FW is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
sys.path.insert(0, FW)

main_mod = _env.load("main")
from core.chat.session_manager import SessionManager    # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


def make_memory(sessions=40, per=300):
    m = {}
    for s in range(sessions):
        m["adapter:group:g%d" % s] = {
            "timestamp": 1758600000,
            "title": "群 %d" % s,
            "description": "",
            "memory": [[{"role": "user", "content": "一条普通长度的聊天消息，大约三十个字。"},
                        {"role": "assistant", "content": "机器人的回复，长度也差不多。"}]
                       for _ in range(per)],
        }
    return m


class Holder:
    """冒充 SessionManager 实例（只要 chat_memory / chat_memory_path 两个属性）。"""
    pass


def main():
    mem = make_memory()

    print("1) 安装补丁（走插件自己的 _install_memory_dump）")
    plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={})
    plugin.patches.uninstall_all()
    plugin.takeover_memory_dump = True
    plugin._install_memory_dump()
    check("补丁已安装", getattr(SessionManager._save_memory, "__kira_accel__", False) is True)

    h = Holder()
    h.chat_memory = mem
    h.chat_memory_path = os.path.join(tempfile.mkdtemp(), "chat_memory.json")

    print("2) 落盘并读回")
    t0 = time.perf_counter()
    ok = SessionManager._save_memory(h)
    fast_ms = (time.perf_counter() - t0) * 1000
    check("落盘返回 True", ok is True)
    with open(h.chat_memory_path, encoding="utf-8") as f:
        raw = f.read()
    back = json.loads(raw)
    check("★ 读回来的对象与原始数据**完全相同**", back == mem,
          "深度相等" if back == mem else "不一致！")

    print("3) 与框架原实现对比：内容一致 + 速度差")
    import copy
    orig_mem = copy.deepcopy(mem)
    orig_path = h.chat_memory_path + ".orig"
    t0 = time.perf_counter()
    with open(orig_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(orig_mem, indent=4, ensure_ascii=False))
    slow_ms = (time.perf_counter() - t0) * 1000
    with open(orig_path, encoding="utf-8") as f:
        back2 = json.load(f)
    check("★★ 新旧两种写法的**解析结果**逐字段相同", back2 == back,
          "相同" if back2 == back else "不同！")
    print(f"     原实现(缩进) {slow_ms:.1f} ms   |   新实现(紧凑) {fast_ms:.1f} ms"
          f"   |   快 {slow_ms/max(fast_ms,0.01):.1f} 倍")
    print(f"     文件大小 {len(open(orig_path, encoding='utf-8').read())/1024:.0f}K"
          f"  →  {len(raw)/1024:.0f}K")
    check("★ 确实明显更快（≥2 倍；实测约 6 倍）", slow_ms > fast_ms * 2,
          f"{slow_ms:.1f}ms → {fast_ms:.1f}ms")
    check("★ 文件明显更小", len(raw) < len(open(orig_path, encoding='utf-8').read()) * 0.6)

    print("4) 边界：空记忆也不该出错")
    h2 = Holder()
    h2.chat_memory = {}
    h2.chat_memory_path = h.chat_memory_path + ".empty"
    check("空记忆落盘正常", SessionManager._save_memory(h2) is True)
    check("空记忆读回是空对象",
          json.load(open(h2.chat_memory_path, encoding="utf-8")) == {})

    print("5) 还原")
    plugin.patches.uninstall_all()
    check("★ 卸载后回到框架原实现",
          not getattr(SessionManager._save_memory, "__kira_accel__", False))

    print("\n" + "=" * 58)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for x in FAIL:
            print("   ✗", x)
        sys.exit(1)
    print("🎉 全部通过 —— 只是更快更小，内容一字不差")


main()
