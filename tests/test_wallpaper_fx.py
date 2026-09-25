"""壁纸切换特效：**不能有硬切**，且每种特效都要真的在动。

为什么单独一个套件：用户连续反馈"切换还是硬切/连淡入淡出都没有"，
而根因每次都不一样（reduced-motion 抹掉 transform、首图直接放上、
条带残留……）。字符串判据证明不了"看起来在过渡"，所以这里查三件事：
  ① 有没有哪条规则把过渡**整体关掉**（历史踩坑点）
  ② 每种特效是否真的驱动了视觉量（opacity / clip-path / transform / filter）
  ③ 每种特效是否上报了真实时长（否则收尾早到 ⇒ 闪一下 = 硬切观感）
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

ROOT = _env.ROOT
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
JS = "\n".join(re.findall(r"<script>([\s\S]*?)</script>", HTML))
NC = re.sub(r"/\*[\s\S]*?\*/", "", HTML)          # 剥注释：注释里提到也会误报

_fail = []


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        _fail.append(name)


print("═══ 壁纸切换：不得有硬切 ═══")

# ① 不得有任何规则把壁纸层的过渡整体抹掉（历史真 bug：reduced-motion 里
#    `.wp-img{transform:none!important}` ⇒ 缩放/擦除全废 ⇒ 只剩瞬切）
_bad = []
for m in re.finditer(r"@media \(prefers-reduced-motion:reduce\)\{((?:[^{}]|\{[^{}]*\})*)\}", NC):
    b = m.group(1)
    if re.search(r"\.wp-(img|layer)[^{]*\{[^}]*transform:none", b):
        _bad.append("transform:none")
    if re.search(r"\.wp-(img|layer)[^{]*\{[^}]*transition:none", b):
        _bad.append("transition:none")
check("★ 减少动效下**不抹掉**壁纸过渡（那会变成硬切）", not _bad, str(_bad))

# ② 可见性必须能过渡：不能用"类名切换"直接决定 opacity（那是瞬变）
check("★ 新图可见性由动画驱动（revealIn 写 opacity 关键帧）",
      "function revealIn" in JS and "{opacity:0},{opacity:1}" in JS)

# ③ 每种特效都要真的驱动视觉量，并上报时长
FX = {
    "fxFade":   ["opacity"],
    "fxIris":   ["clipPath"],
    "fxWipe":   ["clipPath"],
    "fxZoom":   ["transform"],
    "fxBurn":   ["clipPath", "filter"],      # 挖空 + 受热暖调
}
for fn, needs in FX.items():
    i = JS.find("function " + fn)
    seg = JS[i:i + 6000] if i > 0 else ""
    check(f"★ {fn} 存在", i > 0)
    for nd in needs:
        check(f"  {fn} 驱动 {nd}", nd in seg)
    check(f"  {fn} 上报真实时长 wpLastDur", "wpLastDur" in seg)

# ④ 条带类必须有真实铺满时长（否则旧图先退 ⇒ 露底色 ⇒ 观感就是硬切）
check("★ 条带特效上报真实铺满时长（coverAt）",
      "wpLastDur = coverAt" in JS)

# ⑤ 收尾必须先等**这一套自己的**时长，而不是固定值
check("★ 收尾等待本套特效的实际时长（不是写死常量）",
      "await sleep(wpLastDur + " in JS)

# ⑥ 首图不得"直接放上"（那是硬切）；必须有可见底图时才走过渡
check("★ 首图走过渡揭示（不硬贴）", "wpTransition(wpUrl(wpActive[0]))" in JS)

print()
print("═══ 燃纸 burn ═══")
_b = JS[JS.find("function fxBurn"):JS.find("/* ── 斜擦")]
check("★ 已注册在 WP_EFFECTS", '"burn"' in JS)
check("★ 无焦边层（用户明确不要）", "scorch" not in _b)
check("★ 两层可见性都显式写动画（否则烧在透明层上=硬切）",
      "anim(from, [{opacity:1},{opacity:1}]" in _b)
check("★ 无明火（用户明确不要）", not re.search(r"\bfire\b|flame", _b, re.I))
check("★ 有燃烧动感：受热暖调", "sepia(" in _b)
check("★ 有燃烧动感：迸发节奏（幂拉伸，非匀速）", "Math.pow(" in _b)
check("★ 边缘不规则（随机噪声）", re.search(r"Math\.random\(\) \* 0\.5", _b) is not None)
# ★ 用户要求改成"多个地方随机点燃" ⇒ 起点不再是单个
check("★ 多处随机点燃（3~6 个火源）",
      re.search(r"3 \+ Math\.floor\(Math\.random\(\) \* 4\)", _b) is not None)
check("★ 每处速率不同（非同心圆）", "rate:" in _b and "0.62 + Math.random()" in _b)
check("★ 火线相遇取最早（自然形成延迟脊）", "if (t < best) best = t;" in _b)
check("★ 分帧批处理（不每块一个定时器）", "requestAnimationFrame" in _b and "done < 24" in _b)
check("★ 可取消（收尾清理）", "cancelAnimationFrame" in _b)

print()
print("═══ 特效清单一致性 ═══")
import json
_m = re.search(r"const WP_EFFECTS = \[([^\]]*)\]", JS)
_js = [x.strip().strip('"') for x in _m.group(1).split(",")]
_sc = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
_sx = [x for x in _sc["section_appearance"]["fields"]["wallpaper_effect"]["options"] if x != "random"]
check("★ JS 与 schema 的特效清单一致", sorted(_js) == sorted(_sx), f"{_js} vs {_sx}")

print()
if _fail:
    print(f"❌ {len(_fail)} 项未通过: {_fail[:6]}")
    sys.exit(1)
print("🎉 全部通过 —— 壁纸切换不会硬切，燃纸符合要求")
