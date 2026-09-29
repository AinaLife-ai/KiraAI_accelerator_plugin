#!/usr/bin/env python3
"""★★★ 背景切换：图层顺序 / 收尾原子性 的**静态契约**测试。

针对 2026-09-29 用户实测报的两个问题：
  1. 切换后**新图一直被模糊效果覆盖**
  2. 切到新图后约 1 秒，**旧图又闪现一次**

根因（实机复现确认）：`.wp-stage` 被显式设了 `z-index:1` ⇒ "按 DOM 顺序绘制"
的兜底**永久失效**，两个图层从此由 DOM 顺序决定上下，而 wp-b 恒在 wp-a 之后
⇒ **wp-b 永远在最上层** ⇒
  · 旧层=wp-a → 新层=wp-b ：蒙对 ✓
  · 旧层=wp-b → 新层=wp-a ：旧图永远盖住新图 ⇒ 整个转场看不见 ✗
轮换每轮换层 ⇒ 每隔一次必然发生。
7 套特效（fade/iris/wipe/blinds/shutter/zoom/ink）**全部**赌在 DOM 顺序上；
只有 fxBurn 自己写了 z-index。

第二个问题来自 wpSettle 的**非原子收尾**：先改 CSS 状态（摘 .on / inline 隐藏），
最后才 `wpCleanup()` 撤载体 ⇒ 中间存在"旧层被放行、而新图还没人画"的空窗。

本测试**不依赖浏览器**：直接对源码做结构断言（顺序/存在性），
并做一次"顺序模拟"验证两件事的因果。
"""
from __future__ import annotations
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
HTML = (HERE / "web" / "index.html").read_text(encoding="utf-8")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


# ── 取 JS 并剥掉注释（注释里大量引用旧代码/反例，不剥会误报）────────────
_raw = re.search(r"<script>([\s\S]*?)</script>\s*</body>", HTML)
_raw = _raw.group(1) if _raw else HTML
JS_NOCOMMENT = re.sub(r"/\*[\s\S]*?\*/", "", _raw)
JS_NOCOMMENT = re.sub(r"(?m)^\s*//.*$", "", JS_NOCOMMENT)


def fn_body(name):
    """取出一个顶层 function 的完整函数体（按大括号配平）。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", JS_NOCOMMENT)
    if not m:
        return None
    i = m.end() - 1
    depth, j = 0, i
    while j < len(JS_NOCOMMENT):
        c = JS_NOCOMMENT[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return JS_NOCOMMENT[i:j + 1]
        j += 1
    return None


def pos(body, needle):
    """needle 在 body 中的下标；找不到返回 -1。"""
    return body.find(needle) if body else -1


print("═══ 1) ★★★★ 图层顺序必须由**显式 z-index**保证，不能靠 DOM 顺序")
_setorder = fn_body("wpSetLayerOrder")
check("存在 wpSetLayerOrder()（图层顺序的唯一真相）", _setorder is not None)
if _setorder:
    check("它给**新层**抬到 WP_TO_Z", "WP_TO_Z" in _setorder and "style.zIndex = WP_TO_Z" in _setorder)
    check("它把**另一层**压到 WP_FROM_Z", "WP_FROM_Z" in _setorder and "style.zIndex = WP_FROM_Z" in _setorder)
    # 必须先抬新层、再压旧层（否则中间有一瞬是反的）
    _p_new = pos(_setorder, "style.zIndex = WP_TO_Z")
    _p_old = pos(_setorder, "style.zIndex = WP_FROM_Z")
    check("顺序：**先抬新层、再压旧层**（中间不存在瞬时反转）",
          0 <= _p_new < _p_old, f"new@{_p_new} old@{_p_old}")

check("常量定义：TO_Z 必须大于 FROM_Z",
      bool(re.search(r'WP_FROM_Z\s*=\s*"(\d+)"\s*,\s*WP_TO_Z\s*=\s*"(\d+)"', JS_NOCOMMENT)
           and (lambda m: int(m.group(2)) > int(m.group(1)))(
               re.search(r'WP_FROM_Z\s*=\s*"(\d+)"\s*,\s*WP_TO_Z\s*=\s*"(\d+)"', JS_NOCOMMENT))))

# ★★★★ 2026-09-29（v1.0.76，**契约反转**）：这条原来断言
#   "wpLoad 第一件事就是 wpSetLayerOrder(id)"（即**无条件**翻层级）。
#   那**正是"新图永久模糊直到下张"的根因**：
#     startWallpaperRotation / playSplash 也会调 wpLoad，它们不知道谁是新层；
#     无条件翻转就会把在飞转场的层序倒置 ⇒ 上层留旧图 ⇒ 一直糊 ✗
#   ⇒ 新契约：层级翻转必须**显式 opt-in**（只有 _wpTransition 传 {order:true}）。
_load = fn_body("wpLoad")
check("★★★ wpLoad 只在显式 {order:true} 时才翻层级（不得无条件翻）",
      _load is not None and "opts && opts.order" in _load and
      "if (opts && opts.order) wpSetLayerOrder(id);" in _load)
check("★★★ wpLoad 里不得出现无条件的 wpSetLayerOrder(id) 调用",
      _load is not None and "if (opts && opts.order) wpSetLayerOrder(id);" in _load and
      len(re.findall(r"(?<!opts && opts\.order\)) wpSetLayerOrder\(id\)", _load)) == 0)
# 唯一真正知道"谁是新层"的地方必须传这个标志
_t = fn_body("_wpTransition")
check("★★★ _wpTransition 传 {order:true}（唯一知道新层的地方）",
      _t is not None and "{order:true}" in _t)

# 收尾必须"持锁跑完重排再放锁"（否则 deferred restart 会留出并发窗口）
_settle2 = fn_body("wpSettle")
_p_core = pos(_settle2, "startWallpaperRotationCore()")
_p_unlock = pos(_settle2, "wpBusy = false;")
check("★★★ 收尾先同步跑完重排、再放锁（不留并发窗口）",
      0 <= _p_core < _p_unlock, f"core@{_p_core} unlock@{_p_unlock}")
check("★★★ 收尾显式清掉另一层的 .on（有且只有一层 .on 不变量）",
      _settle2 is not None and 'const other = (id === "wp-a") ? "wp-b" : "wp-a";' in _settle2)
check("★★★ 重排拆成 Core（外部入口判锁用，不让收尾路径自我循环）",
      fn_body("startWallpaperRotationCore") is not None)

print("\n═══ 2) ★★★★ 轮换必换层 ⇒ 两个方向都必须工作")
# wpOther 必须真的换到另一层（否则不会来回切），且 wpCur 在收尾时推进
check("wpOther() 在两层之间轮换（wpCur==='wp-a' ? 'wp-b' : 'wp-a'）",
      bool(re.search(r"wpCur\s*===\s*\"wp-a\"\s*\?\s*\"wp-b\"\s*:\s*\"wp-a\"", JS_NOCOMMENT)))
_settle = fn_body("wpSettle")
check("wpSettle() 收尾推进 wpCur = id（下一轮必然换到另一层）",
      _settle is not None and "wpCur = id" in _settle)

print("\n═══ 3) ★★★★ 收尾必须**原子**：先撤载体、再放行旧层")
if _settle:
    _p_cleanup = pos(_settle, "wpCleanup()")
    _p_off = pos(_settle, 'classList.remove("on")')
    check("wpSettle() 里 wpCleanup() 存在", _p_cleanup >= 0)
    check("wpSettle() 里摘 .on 存在", _p_off >= 0)
    check("★★ 顺序正确：**先 wpCleanup() 撤载体，再摘 .on 放行旧层**",
          0 <= _p_cleanup < _p_off, f"cleanup@{_p_cleanup} removeOn@{_p_off}")
    # 归一化旧层也必须在撤载体之后
    _p_norm = pos(_settle, "wpNormalize(fromId)")
    check("★ 归一化旧层也在撤载体之后", _p_norm > _p_cleanup, f"norm@{_p_norm}")
else:
    check("wpSettle() 存在", False)

print("\n═══ 4) 特效不得再自行改变图层顺序（会与 wpSetLayerOrder 打架）")
# fxBurn 曾写 aZ=1 / bZ=2（反的）。修好后应由 wpSetLayerOrder 统一负责，
# fxBurn 里即便保留也必须与常量一致，绝不能再出现"纸=2 / 新图=1"这种硬编码反向值。
_burn_hard_2 = len(re.findall(r'stFrom\.style\.zIndex\s*=\s*"2"', JS_NOCOMMENT))
_burn_hard_1 = len(re.findall(r'stTo\.style\.zIndex\s*=\s*"1"', JS_NOCOMMENT))
check("fxBurn 不再硬编码反向着色（stTo=\"1\" 与旧层的 \"2\"）",
      not (_burn_hard_2 and _burn_hard_1),
      f'stFrom="2"×{_burn_hard_2}  stTo="1"×{_burn_hard_1}')

print("\n═══ 5) 旧图必须**真正退场**（不能以 blur 的 fill:forwards 状态常驻）")
# eatAway / fxStrips 给旧层加的模糊动画是永久的；只要旧层还在最上就是"一直被模糊覆盖"。
check("wpHideEl 写的是 !important（动画顶不回来）",
      "setProperty(\"opacity\", \"0\", \"important\")" in JS_NOCOMMENT)
_fade = fn_body("fxFade")
check("fxFade 结尾让旧层淡到 0（不是停在模糊态）",
      _fade is not None and re.search(r'\{opacity:0\}\s*\],', _fade) is not None)

print("\n═══ 6) 回归：既有不变量不能被破坏")
check("wpNormalize 仍然**不写** inline opacity（可见性唯一来源是 .on）",
      re.search(r"function\s+wpNormalize[\s\S]*?im\.style\.cssText\s*=\s*\"\"", JS_NOCOMMENT) is not None)
check("wpShowLayer 与 wpHideEl 对称（清 stage + img 两层）",
      "removeProperty(\"opacity\")" in JS_NOCOMMENT and "removeProperty(\"visibility\")" in JS_NOCOMMENT)
check("_wpTransition 开头仍自愈清理两层 inline 隐藏",
      "removeProperty(\"display\")" in JS_NOCOMMENT)

print("\n" + "═" * 62)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  ✗", f)
sys.exit(1 if FAIL else 0)