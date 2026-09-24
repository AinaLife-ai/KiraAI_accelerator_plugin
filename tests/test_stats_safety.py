"""边界安全：保留期清理**只动计数**，不误伤别的东西。

为什么单独一个套件：清理类功能最危险的失败不是"没清掉"，而是**误清**——
文件损坏、时间戳是垃圾值、写盘失败…… 这些路径平时不跑，
一旦出问题就是用户数据没了。所以这里专门打异常路径。

★ 实测价值：本套件的 `_since=-1` 用例**抓到过一个真 bug** ——
  负时间戳会让 `(now - since)` 大得离谱 ⇒ 误判超期 ⇒ 把计数清掉。
  修复是"宁可不清，也不误清"（校验不通过就只重记起算点）。
"""
import json
import os
import sys
import time
import pathlib
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _env                                            # noqa: E402

FRAMEWORK = _env.framework()
if FRAMEWORK is None:
    _env.skip("需要 KiraAI 框架源码（设 KIRA_FW=/path/to/KiraAI）")
os.makedirs("data", exist_ok=True)
sys.path.insert(0, FRAMEWORK)

M = _env.load("main")


class _L:                                              # 静音日志
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def exception(self, *a, **k): pass


M.logger = _L()

_N = 0


def ck(name, cond, extra=""):
    global _N
    _N += 1
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        globals()["_FAILED"] = globals().get("_FAILED", 0) + 1


def make(keep, seed):
    """造一个真实的插件实例，数据目录里预置 seed。"""
    td = tempfile.mkdtemp()
    p = M.AcceleratorPlugin.__new__(M.AcceleratorPlugin)
    p._stats = {k: (None if k == "first_seg_s" else 0) for k in
                ["turns", "steps", "tool_signals", "strip_calls", "parallel_batches",
                 "content_normalized", "streamed_calls", "early_sent", "first_seg_s",
                 "thinking_on", "thinking_off", "thinking_skipped_budget"]}
    p.stats_keep_days = keep

    class Ctx:
        def get_plugin_data_dir(self):
            return pathlib.Path(td)

    p.ctx = Ctx()
    f = pathlib.Path(td) / "kpi_stats.json"
    f.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return p, f, pathlib.Path(td)


print("═══ ① 文件损坏 / 非 JSON / 非 dict ⇒ 不崩、不清、按 0 起步")
for bad in ("{ 坏掉的 json", "[1,2,3]", '"字符串"', ""):
    p, f, _ = make(30, {})
    f.write_text(bad, encoding="utf-8")
    try:
        p._load_stats()
        ck(f"损坏输入不抛异常（{bad[:10]!r}）", True)
    except Exception as e:                             # noqa: BLE001
        ck(f"损坏输入不抛异常（{bad[:10]!r}）", False, f"{type(e).__name__}: {e}")

print("\n═══ ② _since 是垃圾值 ⇒ **绝不误清**（宁可不清，也不误伤）")
for since in ("昨天", -1, 0, 1e18, None, [], {}, 1e-9):
    p, f, _ = make(30, {"turns": 500, "_since": since})
    try:
        p._load_stats()
        ck(f"_since={since!r} 计数未被误清", p._stats["turns"] == 500,
           f"turns={p._stats['turns']}")
    except Exception as e:                             # noqa: BLE001
        ck(f"_since={since!r} 不崩", False, f"{type(e).__name__}: {e}")

print("\n═══ ③ 未知 / 多余键 ⇒ 不污染 _stats（将来加字段也安全）")
p, f, _ = make(30, {"turns": 500, "_since": time.time(),
                    "未来字段": {"x": 1}, "别的插件的键": "别的东西"})
p._load_stats()
ck("未知键不被灌进 _stats", "未来字段" not in p._stats and "别的插件的键" not in p._stats)
ck("已知计数照常恢复", p._stats["turns"] == 500)

print("\n═══ ④ 计数是错误类型 ⇒ 不污染成字符串/列表")
p, f, _ = make(30, {"turns": "很多", "steps": [], "_since": time.time()})
p._load_stats()
ck("非数字不被灌入", isinstance(p._stats["turns"], int) and isinstance(p._stats["steps"], int),
   f"turns={p._stats['turns']!r}")

print("\n═══ ⑤ 写盘失败（目录只读）⇒ **不删原文件**、不抛异常")
p, f, td = make(30, {"turns": 500, "_since": time.time() - 45 * 86400})
os.chmod(f, 0o444)
os.chmod(td, 0o555)
try:
    p._load_stats()
    ck("写盘失败时原文件仍在（数据不丢）", f.exists())
    ck("写盘失败时插件仍能起来（不抛）", True)
finally:
    os.chmod(td, 0o755)
    os.chmod(f, 0o644)

print("\n═══ ⑥ 清零时**只动计数**，同目录其他数据一律不碰")
p, f, td = make(30, {"turns": 999, "_since": time.time() - 45 * 86400})
(td / "wallpapers_meta.json").write_text('{"重要":"数据"}', encoding="utf-8")
(td / "user_note.txt").write_text("别删我", encoding="utf-8")
(td / "subdir").mkdir(exist_ok=True)
(td / "subdir" / "nested.txt").write_text("嵌套也别删", encoding="utf-8")
p._load_stats()
ck("计数已清", p._stats["turns"] == 0)
ck("同目录 wallpapers_meta.json 未被碰",
   (td / "wallpapers_meta.json").read_text(encoding="utf-8") == '{"重要":"数据"}')
ck("同目录 user_note.txt 未被碰",
   (td / "user_note.txt").read_text(encoding="utf-8") == "别删我")
ck("嵌套目录内容未被碰",
   (td / "subdir" / "nested.txt").read_text(encoding="utf-8") == "嵌套也别删")
ck("★ 没有删除任何文件（只做写入，无 unlink）",
   all(x.exists() for x in (td / "wallpapers_meta.json", td / "user_note.txt",
                            td / "subdir" / "nested.txt")))

print("\n═══ ⑦ 只写自己的文件：清理路径里不含任何删除调用")
_main = pathlib.Path(M.__file__).read_text(encoding="utf-8")
_i = _main.find("def _apply_keep_window")
_j = _main.find("\n    def ", _i + 10)
_body = _main[_i:_j if _j > 0 else len(_main)]
for fn in ("unlink", "rmtree", "os.remove", "shutil.rm", "os.rmdir"):
    ck(f"保留期逻辑里没有 {fn}()", fn not in _body)

print()
_failed = globals().get("_FAILED", 0)
if _failed:
    print(f"❌ {_failed} 项未通过")
    sys.exit(1)
print(f"🎉 全部通过 —— 清理只动计数，{_N} 项边界检查")
