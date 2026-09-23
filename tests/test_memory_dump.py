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

    print("5) ★★ 安全边界：WebUI 的会话历史必须仍然能读（用户特别问的）")
    # WebUI 读历史走的是 webui/routes/sessions.py:104
    #   session_manager.get_existing_memory_snapshot(session_id)  → **内存快照**
    # 它根本不读文件，所以格式变化影响不到它。这里把整条链路真跑一遍。
    import threading
    sm = SessionManager.__new__(SessionManager)
    sm.chat_memory = SessionManager._load_memory(h.chat_memory_path)   # 从紧凑文件加载
    sm.memory_lock = threading.Lock()
    check("★ 框架自己的 _load_memory 能读紧凑文件", sm.chat_memory == mem,
          "读回与原始数据深度相等")
    snap = SessionManager.get_existing_memory_snapshot(sm, "adapter:group:g0")
    check("★★ WebUI 取会话历史：拿到的条数与内容都对",
          snap == mem["adapter:group:g0"]["memory"],
          f"{len(snap)} 条")
    check("历史里的消息内容完好",
          snap and snap[0][0].get("content") == "一条普通长度的聊天消息，大约三十个字。",
          str(snap[0][0])[:60] if snap else "空")
    # 两种格式读回来必须一致（证明"我只是换了写法"）
    sm2 = SessionManager.__new__(SessionManager)
    sm2.chat_memory = SessionManager._load_memory(orig_path)           # 旧格式
    check("★ 新旧格式读回的对象完全相同", sm2.chat_memory == sm.chat_memory)

    print("\n6) ★ 默认必须是【关】（保住 chat_memory.json 的可读性）")
    import json as _json
    sch = _json.loads((_env.ROOT / "schema.json").read_text(encoding="utf-8"))
    d = sch["section_takeover"]["fields"]["compact_memory_dump"]["default"]
    check("★ schema 默认关", d is False, f"default={d}")
    src_main = (_env.ROOT / "main.py").read_text(encoding="utf-8")
    check("★ 代码兜底默认也是关",
          'c_tak.get("compact_memory_dump", False)' in src_main)
    check("★ 说明里如实写了实测数据（不是笼统吹收益）",
          "0.7ms" in _json.dumps(sch["section_takeover"]["fields"]["compact_memory_dump"],
                                 ensure_ascii=False),
          "hint 里带真实数字")

    print("\n7) ★ 锁死：即使配置写成 true 也不生效（用户要求：暂不开放）")
    plugin.patches.uninstall_all()      # 先卸掉第 1 步显式装的补丁
    check("前置：已回到框架原实现",
          getattr(SessionManager._save_memory, "__kira_accel__", False) is False)
    from core.chat.session_manager import SessionManager as _SM
    _orig = _SM._save_memory
    lock_plugin = main_mod.AcceleratorPlugin(ctx=None, cfg={
        "section_takeover": {"compact_memory_dump": True}})
    check("★ 配置 true ⇒ 仍然不接管（代码锁死）",
          getattr(_SM._save_memory, "__kira_accel__", False) is False)
    check("★ 该功能开关在实例上是 False", lock_plugin.takeover_memory_dump is False)

    panel = (_env.ROOT / "web" / "index.html").read_text(encoding="utf-8")
    check("★ 面板里有「待验证」清单", "PENDING_VERIFICATION" in panel)
    check("★ 该字段在待验证清单里",
          '"section_takeover.compact_memory_dump"' in panel)
    check("★ 面板会把控件置灰（disabled）", "inp.disabled = true" in panel)
    check("★ 面板会显示「待验证」徽标", "待验证" in panel and "badge-pending" in panel)
    check("★ 保存时会强制回默认值（防手改配置/绕过面板）",
          "Object.keys(PENDING_VERIFICATION).forEach" in panel)
    check("★ schema 名称里也标了「待验证」",
          "待验证" in _json.dumps(
              _json.loads((_env.ROOT / "schema.json").read_text(encoding="utf-8"))
              ["section_takeover"]["fields"]["compact_memory_dump"],
              ensure_ascii=False))

    print("\n8) 还原")
    check("★ 全程结束后仍是框架原实现",
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
