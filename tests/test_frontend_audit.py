"""★★ 前端审计：壁纸层级 / 开屏时序 / 墨渗极性 / 特效时长。

这一组是"不用浏览器也能判定对错"的静态 + 数值检查，
针对 2026-09-23 用户实测报的四个问题：

  1. **没有开屏**：playSplash 排在两个 await fetch 之后 ⇒ 主内容早渲染完，开屏只是闪一下
  2. **切换时重叠 / 旧图闪现**：wpNormalize 写了 inline `opacity:1`，压过 CSS 的
     `opacity:0` ⇒ 旧层永远可见；而 DOM 里 wp-b 在后 ⇒ wp-b 永远在上层，
     于是"新层是 wp-a"时被旧图盖住
  3. **墨渗不像墨晕**：只是圆 + 边缘位移；而且阈值用的是 `discrete` 的 tableValues，
     断点固定 ⇒ 调高反而**满屏墨**（极性反了）
  4. **特效太快**：WP_DUR 2200
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


print("═══ 1) ★★ 图片可见性只能有**一个**来源（.wp-stage.on）")
# 取 JS 部分（避免 CSS 注释里的说明文字干扰）
_js_raw = re.search(r"<script>([\s\S]*?)</script>\s*</body>", HTML)
_js_raw = _js_raw.group(1) if _js_raw else HTML
# ★ 检查代码特征前**必须剥掉注释**：注释里举了反例（"原来这里写了
#   im.style.opacity = \"1\""），不剥就会把自己的说明文字当成代码误报。
js = re.sub(r"/\*[\s\S]*?\*/", "", _js_raw)
js = re.sub(r"(?m)^\s*//.*$", "", js)

# ★★ 把墨渗的偏置**从代码里读出来**：后面多处判据要用。
#    不能写死在测试里 —— 否则代码改了测试照样绿（只验证了"我以为的值"，已踩过）。
_m = re.search(r"const BIAS0 = (-?[\d.]+), BIAS1 = (-?[\d.]+);", js)
check("能从 fxInk 里读出 BIAS0 / BIAS1", _m is not None)
if not _m:
    print("   无法继续"); sys.exit(1)
B0, B1 = float(_m.group(1)), float(_m.group(2))
print(f"     墨渗偏置（从代码读到）: BIAS0={B0}  BIAS1={B1}")

# 找所有"对 .wp-img 写 opacity"的语句
# ⚠️ 正则必须用 `\bim`：只写 `im\.style` 会匹配到 `r`**`im`**`.style.opacity`
#    （wpRim 的光环有 `rim.style.opacity = "0"`，那是**光环**，不是图片层）。
#    上一版测试就是这么自己误报的。
img_opacity_writes = re.findall(r"\bim\.style\.opacity\s*=", js)
check("★ 没有任何一行给**图片层**（im）写 inline opacity",
      not img_opacity_writes, f"找到 {len(img_opacity_writes)} 处")

# ⚠️ **不能删空格**：`.wp-stage.on .wp-img` 里的空格是**后代选择器**，
#    删掉就变成另一个选择器（上一版测试自己写错、自己报红）。
check("★ CSS 里唯一控制可见性的是 `.wp-stage.on .wp-img{opacity:1}`",
      re.search(r"\.wp-stage\.on\s+\.wp-img\s*\{\s*opacity:1", HTML) is not None)
check("`.wp-img` 默认是 opacity:0",
      re.search(r"\.wp-img\{[^}]*opacity:0", HTML.replace("\n", "")) is not None)

# wpNormalize 必须只清不设
norm = re.search(r"function wpNormalize\(id\)\{([\s\S]*?)\n\}", js)
check("wpNormalize 存在", norm is not None)
if norm:
    body = norm.group(1)
    check("★ wpNormalize 只做 cssText 清空，不写 opacity",
          "cssText" in body and "style.opacity" not in body)

print()
print("═══ 2) ★ 切换前必须先清掉另一层的 .on（否则旧层盖住新层）")
# settle 里：先 remove(from) 再 add(id)
settle = re.search(r"function wpSettle\(id, fromId\)\{([\s\S]*?)\n\}", js)
check("wpSettle 存在", settle is not None)
if settle:
    b = settle.group(1)
    i_rm = b.find('classList.remove("on")')
    i_add = b.find('classList.add("on")')
    check("★ 先移除旧层的 .on，再给新层加 .on", 0 <= i_rm < i_add,
          f"remove@{i_rm} add@{i_add}")

# 首图：必须给两层都 wpLoad（清 .on）后再设 wp-a
rot = re.search(r"function startWallpaperRotation\(\)\{([\s\S]*?)\n\}", js)
check("startWallpaperRotation 存在", rot is not None)
if rot:
    b = rot.group(1)
    n_load = len(re.findall(r'wpLoad\("wp-[ab]"', b))
    check("★ 首图给**两层**都 wpLoad（清掉可能残留的 .on）", n_load >= 2, f"{n_load} 次")
    check("首图只给 wp-a 加 .on（不会两层同时可见）",
          b.count('classList.add("on")') == 1)

print()
print("═══ 3) ★★ 开屏必须在**第一帧**出现（不能等网络）")
i_el = HTML.index('<div class="splash" id="splash"')
i_early = HTML.index("window.__accelSplash = true")
i_main = HTML.index("function playSplash")
check("★ 早期显示脚本在开屏元素**之后**（解析到即同步执行）", i_el < i_early)
check("★ 且在主脚本之前", i_early < i_main)
early = HTML[i_early:HTML.index("</script>", i_early)]
# ★★ 设计反转（学 Z 插件）：开屏**默认可见**（CSS display:grid），
#    视觉全部由 CSS 完成；JS 只负责"填内容 + 收尾"。
#    ⇒ 旧判据（"早期脚本要加 .show"）已经过时，且方向相反：
#      靠 JS 显示才是**脆弱**的 —— JS 任何一步出问题 = 开屏不出现（连续报了两轮）。
check("★ 开屏基础规则是 display:grid（**默认可见**，不靠 JS 显示）",
      re.search(r"\.splash\{[^}]*display:\s*grid", HTML) is not None)
check("★ 只有 [hidden] 才隐藏（一个明确的隐藏来源）",
      re.search(r"\.splash\[hidden\]\{\s*display:\s*none", HTML) is not None)
check("★ 没有任何地方用 .show 来显示开屏（旧设计）",
      'classList.add("show")' not in js)
check("★ 标题文字**静态写在 HTML** 里（JS 不在也有字，不白屏）",
      len(re.findall(r'class="L\b', HTML)) >= 9,
      f'{len(re.findall(chr(34)+"class="+chr(34)+"L", HTML))} 个字母')
check("★ 箴言静态有默认内容（JS 不在也有话）",
      re.search(r'<div class="motto" id="motto">[^<]+</div>', HTML) is not None)
check("★ 基础入场动画由 CSS 直接挂（不靠 JS 加类触发）",
      len(re.findall(r"\.splash:not\(\[hidden\]\)[^{]*\{[^}]*animation:", HTML)) >= 3)
check("★ 早期脚本立刻 arm 收尾定时器（不依赖主脚本）",
      "__accelSplashFailsafe = setTimeout" in early)
check("主脚本只做增强+收尾（判 hidden，不判要不要显示）",
      "if (!sp || sp.hidden) return;" in js)
check("★ 所有「不显示」的路径都会收掉开屏（配置关 / 无壁纸 / 异常 / 超时）",
      js.count("finishSplash()") >= 4, f"{js.count('finishSplash()')} 处")
check("★ finishSplash 会 clearTimeout 掉早期兜底",
      "clearTimeout(window.__accelSplashFailsafe)" in js)

print()
print("═══ 4) ★★ 墨渗：必须是「随机纹路」而不是圆，且极性正确")
# ★ 必须用**原生 SVG mask**（挂在 SVG 的 <image> 上），
#   而不是 HTML 元素的 CSS `mask-image: url(#…)` ——
#   Chromium 对后者支持不完整 ⇒ 遮罩不生效 ⇒ 新图**瞬间全亮**（用户实测过）。
check("★ 墨渗用原生 SVG mask（<image mask=\"url(#wpInkMask)\">）",
      re.search(r'<image[^>]*mask="url\(#wpInkMask\)"', HTML) is not None)
check("★ 不再用不可靠的 HTML CSS mask-image 引 SVG mask",
      'maskImage = "url(#wpInkMask)"' not in js)
# 旧的圆实现应已消失
ink = re.search(r"function fxInk\(to, from, rim\)\{([\s\S]*?)\n\}", js)
check("fxInk 存在", ink is not None)
if ink:
    b = ink.group(1)
    check("★ fxInk 里不再用 circle() 做形状", "circle(" not in b, 
          repr(re.findall(r"circle\([^)]*\)", b)[:2]))
    check("★ 用两层分形噪声（粗纹 + 细丝）",
          HTML.count('type="fractalNoise"') >= 2)
    check("★ 通过 feColorMatrix 的 bias 逐帧抬高（可动画的阈值）",
          'cm.setAttribute("values"' in b and "BIAS0" in b and "BIAS1" in b)
    check("★ 不再动画 discrete 的 tableValues（断点固定 ⇒ 极性会反）",
          'th.setAttribute("tableValues"' not in b)
    check("每次随机换种子（纹路不重复）",
          'n1.setAttribute("seed"' in b and "Math.random" in b)
    # ★ 复位值也要跟 BIAS0 一致（不能写死数字）
check("结束时会复位 bias 到 BIAS0（不影响下一次）",
      ("setBias(" + str(B0) + ")") in b or b.count("setBias(") >= 2, 
      f"BIAS0={B0}")

print()
print()
print("═══ 4b) ★★ 响应标记必须真的被写入（否则剥离永远走兜底 + 日志刷屏）")
_main = pathlib.Path(HERE / "main.py").read_text(encoding="utf-8")
_eng = pathlib.Path(HERE / "stream_engine.py").read_text(encoding="utf-8")
check("★ 引擎写入了已抢发段数（主判据）",
      '_accel_early_sent_count' in _eng)
check("★ 引擎写入了已抢发段原文（按内容剥离要用）",
      '_accel_early_sent_segments' in _eng)
check("★ 且是在 on_complete 之前写入（发送层读得到）",
      _eng.index('_accel_early_sent_count') < _eng.index("self.on_complete("))
check("台账每轮开始会清空（否则上一轮残留会让本轮多切=丢内容）",
      "_reset_turn_ledger" in _main and "self._reset_turn_ledger(sid_now)" in _main)

print("═══ 5) ★ 墨渗的数值校验：极性必须对（开场是一粒墨，不是满屏）")
# alpha = 0.55R + 0.35G + 0.10B + bias，噪声三通道均值≈0.5
# discrete(tableValues=[0,.55,1]) 的断点 = 1/3、2/3（固定）
LEVELS = 3
TH = [(i + 1) / LEVELS for i in range(LEVELS)]     # [0.333, 0.667, 1.0]


def alpha_of(bias, r=0.5, g=0.5, b=0.5):
    return 0.55 * r + 0.35 * g + 0.10 * b + bias


def opaque_fraction(bias):
    """噪声近似正态（均值 .5，标准差 .15）时，超过第一档断点的比例。"""
    import math
    th = TH[0]
    mu = alpha_of(bias)
    sd = 0.55 * 0.15 + 0.35 * 0.15 + 0.10 * 0.15       # 线性组合后的标准差
    z = (th - mu) / sd
    return 0.5 * (1 - math.erf(z / math.sqrt(2)))


f_start = opaque_fraction(B0)
f_mid = opaque_fraction(B0 + (B1 - B0) * 0.5)     # 进度 50% 处
f_end = opaque_fraction(B1)
print(f"     bias={B0:+.2f} ⇒ 不透明占比 ≈ {f_start*100:.2f}%   （开场：一粒墨）")
print(f"     bias={B0+(B1-B0)*0.5:+.2f} ⇒ 不透明占比 ≈ {f_mid*100:.1f}%    （中段：斑块散开）")
print(f"     bias={B1:+.2f} ⇒ 不透明占比 ≈ {f_end*100:.1f}%     （结束：铺满）")
check("★ 开场只有零星墨点（<5%）—— 不是「开场就满屏墨」", f_start < 0.05)
# ★ 中段应该"正在散开"：既不是几乎全透明、也不该已经铺满。
#   第一次把终点调成 0.55 时，中段就冲到 92.6% ⇒ "散开"只占很短一段 = 等于没过程。
check("★ 中段处于「正在散开」的状态（10%~80%）", 10 < f_mid * 100 < 80,
      f"实际 {f_mid*100:.1f}%")
check("★ 结束时基本铺满（>95%）", f_end > 0.95)
check("★ 单调递增（墨量只增不减 = 真的在扩散）",
      f_start < f_mid < f_end)

print()
print()
print("═══ 5b) ★★ 所有特效统一「色彩覆盖」：新图必须淡入（不许硬切）")
# 用户实测："好像还有一个效果是直接硬切的"。
# 根因：新层 keyframes 只写 transform，`fill:"forwards"` 让 opacity 停在 CSS 的 0，
#       动画结束才跳到 1 ⇒ 新图"啪"地出现。所以每套都必须**显式淡入**。
_fx_names = ["fxIris", "fxWipe", "fxStrips", "fxZoom", "fxInk"]
_reveal = re.search(r"function revealIn\(([\s\S]*?)\n\}", js)
check("存在 revealIn（统一的新图淡入辅助）", _reveal is not None)
check("★ revealIn 里显式写了 opacity 0 → 1",
      _reveal and "opacity:0" in _reveal.group(1) and "opacity:1" in _reveal.group(1))
_eat = re.search(r"function eatAway\(([\s\S]*?)\n\}", js)
check("存在 eatAway（统一的旧图被吃掉辅助）", _eat is not None)
check("★ eatAway 里先保持实、后模糊降饱和淡出（真正的覆盖感）",
      _eat and "opacity:1" in _eat.group(1) and "blur(" in _eat.group(1)
      and "saturate(" in _eat.group(1))
# ★ 真正的要求是"**不许硬切**"：每套特效要么调 revealIn，要么自己的关键帧里
#   显式写 opacity。只数 revealIn 会误判 —— fxInk 走 SVG 遮罩（在函数外掌控节奏），
#   fxZoom 本来就有自己的 opacity 关键帧。
def _fx_body(name):
    i = js.index("function " + name)
    return js[i:i + 1800]

no_fade = []
for f in _fx_names:
    b = _fx_body(f)
    if "revealIn(" in b:
        continue
    if f == "fxInk":
        continue                      # 墨渗按"噪声长出"覆盖，不走淡入
    if re.search(r"opacity:\s*0", b) and re.search(r"opacity:\s*1", b):
        continue                      # 自己有淡入关键帧
    no_fade.append(f)
check("★ 每套特效的新图都**不会硬切**（revealIn 或自带 opacity 关键帧）",
      not no_fade, f"缺淡入: {no_fade}")

print()
print("═══ 5c) ★ 开屏：首帧就要有画面（否则观感是先有字、后有画）")
check("★ .sp-wp 有**纯 CSS 静态底**（不依赖网络/JS 就有画面）",
      re.search(r"\.sp-wp\{[^}]*background-image:[^;]*radial-gradient", HTML) is not None)
check("★ 标题字母默认 opacity:0 + 延后到壁纸之后（750ms 起）",
      re.search(r"\.wm-main \.L\{[^}]*opacity:0", HTML) is not None
      and "750ms" in js or "750ms" in HTML)
check("★ 副标题/分隔线/箴言都默认透明（从无到有）",
      all(re.search(r"\.splash:not\(\[hidden\]\) " + sel + r"\{[^}]*opacity:0", HTML) is not None
          for sel in [r"\.wm-sub", r"\.sp-line", r"\.motto"]))
check("★ CSS 兜底的背景入场也写了 opacity（否则还是硬切）",
      re.search(r"@keyframes spWpIn\{[^}]*opacity:0", HTML, re.S) is not None)

print()
print("═══ 5d) ★ 开屏时长与停留（用户要求久一点）")
m_total = re.search(r"after\((\d+), finishSplash\)", js)
# ⚠️ 总时长 = 收尾**起点** + **化开过渡**。只看 after(N) 会漏掉那 1.2s，
#    把本来正确的改动判红（上一版就这么写错了）。
_tr2 = re.search(r"\.splash\{[^}]*transition:opacity (\d+(?:\.\d+)?)s", HTML, re.S)
_dis = float(_tr2.group(1)) * 1000 if _tr2 else 0
_tot = (int(m_total.group(1)) + _dis) if m_total else 0
check("★ 总时长 ≥5s（收尾起点 + 化开过渡）", _tot >= 5000,
      f"{_tot:.0f}ms = {m_total.group(1) if m_total else '?'} + {_dis:.0f}")
check("★ 收尾过渡 ≥1s（化开而不是硬跳）",
      re.search(r"\.splash\{[^}]*transition:opacity (\d+(?:\.\d+)?)s", HTML) is not None
      and float(re.search(r"\.splash\{[^}]*transition:opacity (\d+(?:\.\d+)?)s", HTML).group(1)) >= 1.0)
check("★ 收尾同时过渡 opacity/transform/filter（化开）",
      re.search(r"\.splash\{[^}]*transition:[^;]*transform[^;]*filter", HTML, re.S) is not None)
check("★ .splash.out 带缩放+模糊（不只是透明）",
      re.search(r"\.splash\.out\{[^}]*transform:[^;]*scale[^}]*filter:[^;]*blur", HTML, re.S) is not None)
# ⚠️ 必须精确抓 `__accelSplashFailsafe = setTimeout(...)}, NNNN)` 这一处：
#    非贪婪正则会匹配到后面那个 1200ms 的隐藏定时器，得出错误结论（上一版就写错了）。
m_fb = re.search(r"__accelSplashFailsafe\s*=\s*setTimeout\(function \(\) \{[\s\S]*?\n\s*\}, (\d+)\);", HTML)
if m_fb and m_total:
    check("★ 内联兜底晚于正常收尾（否则会抢在收尾前把开屏掐掉）",
          int(m_fb.group(1)) > int(m_total.group(1)),
          f"兜底 {m_fb.group(1)}ms vs 正常 {m_total.group(1)}ms")
else:
    check("★ 能找到内联兜底超时值", False, f"m_fb={bool(m_fb)} m_total={bool(m_total)}")

print("═══ 6) ★ 特效要「慢一点」（用户明确要求）")
m = re.search(r"const WP_DUR = (\d+)", js)
check("WP_DUR 至少 3000ms（原来 2200 被反馈太快）", m and int(m.group(1)) >= 3000,
      m.group(1) if m else "未找到")
check("条带铺满整个时长（不再挤在前 60%）",
      "WP_DUR * .72" in js and "WP_DUR * .55 / N" in js)
check("墨渗比基准更长（≈1.15×）", "WP_DUR * 1.15" in js)

print()
print("═══ 7) ★ 「颜色相互消除/覆盖」感：旧图要被吃掉")
# 除墨渗外，每套的旧图都应走 eatAway（模糊 + 降饱和 + 淡出）；
# 墨渗按设计**让旧图保持可见**、由墨盖上去（那才是真正的"被下一张吃掉"）。
eat_ok, eat_missing = [], []
for f in ["fxIris", "fxWipe", "fxStrips", "fxZoom"]:
    b = _fx_body(f)
    (eat_ok if "eatAway(" in b else eat_missing).append(f)
check("★ 除墨渗外，每套特效的旧图都被 eatAway 吃掉", not eat_missing,
      f"缺: {eat_missing}")
_ink = _fx_body("fxInk")
# ⚠️ 不能简单查 "opacity:0" —— fxInk 里有 `to.style.opacity = "0"`
#    （把**新层本体**藏起来，因为显示由 SVG <image> 负责，这是设计如此）。
#    准确的判据是：**旧层根本没有动画**（它保持原样，等着被墨盖住）。
# ⚠️ 判据要精确：fxInk 里**有意**让旧图做一个"极轻微推近"（保持不透明，
#    等着被墨盖住）。所以不能禁止 `anim(from,`，而要禁止**旧图动画里出现 opacity:0**
#    （那才叫"淡出"）。另：`to.style.opacity = "0"` 是**新层本体隐藏**
#    （显示交给 SVG <image>），与旧图无关，不能误伤。
_from_block = re.search(r"anim\(from,\s*\[([\s\S]*?)\]", _ink)
check("★ 墨渗不淡出旧图（旧图动画里没有 opacity:0）",
      "eatAway(" not in _ink
      and (_from_block is None or "opacity:0" not in _from_block.group(1)))
print(f"     走 eatAway 的: {eat_ok}  |  墨渗: 旧图不淡出（被墨覆盖）")

print()
print()
print("═══ 8) ★★ 特效必须播放完（本轮修的三处真 bug）")
# ① 条带特效不能被自己的"整张淡入"盖住 —— 那会让条带看起来没播放
_strips = _fx_body("fxStrips")
check("★ fxStrips 不对主层做整张淡入（否则盖住条带 ⇒ 等于没播放）",
      "revealIn(" not in _strips)
check("★ fxStrips 自己把主层设为隐藏（to.style.opacity = \"0\"）",
      'to.style.opacity = "0"' in _strips)

# ② 收尾必须按**每套特效的实际时长**等，而不是固定值
check("★ 存在 wpLastDur（记录每套的实际时长，含错峰延迟）",
      "let wpLastDur" in js)
check("★ 收尾按 wpLastDur 等待（固定等会截断条带/墨渗）",
      re.search(r"await sleep\(wpLastDur\s*\+", js) is not None)
_setters = re.findall(r"wpLastDur = ([^;]+);", js)
check("★ 每套特效都设置了自己的 wpLastDur", len(_setters) >= 5,
      f"{len(_setters)} 套: {_setters}")
check("★ 条带的时长含最晚一条的延迟（不只是单条时长）",
      any("* .72" in s or "*.72" in s for s in _setters), str(_setters))

print()
print("═══ 9) ★★ 开屏时序：一个元素只能有一个驱动者 + 必须有停留")
check("★ 存在 html.jsfx 开关（JS 接管时关掉 CSS 入场动画）",
      "html.jsfx" in HTML)
check("★ jsfx 关掉的正是会冲突的那几项（字母/副标题/分隔线/箴言/背景）",
      all(sel in HTML for sel in ["html.jsfx .wm-main .L", "html.jsfx .wm-sub",
                                  "html.jsfx .sp-line", "html.jsfx .motto",
                                  "html.jsfx .sp-wp"]))
check("★ 主脚本会打上 jsfx 信号",
      'document.documentElement.classList.add("jsfx")' in js)

# ⚠️ 致命点：CSS 里 .wm-main .L 默认 opacity:0，若 JS 动效用 fill:"backwards"，
#    播完会**回到 opacity:0 ⇒ 字母全消失**。
_n_both = js.count('fill:"both"')
check("★ 标题/箴言特效用 fill:both（backwards 会让字母播完消失）",
      'fill:"backwards"' not in js and _n_both >= 8,
      f"both={_n_both}")

# 箴言必须"入场 → 停住 → 淡出"
_mfx = js[js.index("const MOTTO_FX_FN"):js.index("\n};", js.index("const MOTTO_FX_FN"))]
# ⚠️ 不写死 offset 数值（改一次停留就要改测试）；直接数 offset 次数：
#   每条时间线应有"起点 offset + 停留终点 offset"两处。
_h_mfx = re.findall(r"offset:\.(\d+)", _mfx)
check("★ 箴言特效带停住（每条两处 offset = 起点 + 停留终点）",
      len(_h_mfx) >= 6, f"offset: .{_h_mfx}")
check("★ 箴言特效结尾会淡出（不再一直亮着）",
      _mfx.count("opacity:0") >= 4)

check("★ 标题特效延后到 750ms 才跑（画面先出、字再长出来）",
      re.search(r"after\(750,", js) is not None)
check("★ 副标题/分隔线有各自的 delay（错峰、有停留）",
      "delay:1850" in js and "delay:2050" in js)
check("★ 箴言延后到 2.25s（前面留出停留）",
      re.search(r"after\(2250,", js) is not None)
m_fin = re.search(r"after\((\d+), finishSplash\)", js)
check("★ 整体收尾在 4.6s 左右（化开 1.2s ⇒ 总约 5.8s）",
      m_fin and 4200 <= int(m_fin.group(1)) <= 5200,
      m_fin.group(1) + "ms" if m_fin else "找不到")

print()
print("═══ 10) ★ 跳过提示要有微微闪烁（不张扬）")
check("★ .sp-hint 有闪烁动画", re.search(r"\.sp-hint\{[^}]*animation:spHint", HTML) is not None)
# ⚠️ 不能只抓 `[^}]*` —— keyframes 里有多个 `}`，那样只截到第一档，
#    会得出"找不到 .56"的错误结论（上一版就写错了）。
_hint = re.search(r"@keyframes spHint\{([\s\S]*?)\n\}", HTML)
check("★ 闪烁幅度小（不张扬）",
      _hint is not None and ".34" in _hint.group(1) and ".56" in _hint.group(1),
      _hint.group(1) if _hint else "找不到")
check("★ 提示不被 jsfx 关掉（它始终要闪）", "html.jsfx .sp-hint" not in HTML)
check("★ 减少动效下提示不闪但仍可见（不消失）",
      re.search(r"prefers-reduced-motion[\s\S]{0,400}\.sp-hint\{[^}]*animation:none", HTML) is not None)

print()
print("═══ 11) ★ 开屏字特效：不能太快，且必须自带停留")
_wm = js[js.index("const WM_FX_FN"):js.index("\n};", js.index("const WM_FX_FN"))]
# 每条动画的 duration 都应 >= 1000ms（原来 820~1050 偏快）
_durs = [int(x) for x in re.findall(r"duration:\s*(\d+)", _wm)]
check("★ 字特效每条动画都不低于 1s（原来偏快）",
      bool(_durs) and min(_durs) >= 1000, f"最短 {min(_durs) if _durs else '?'}ms")
check("★ 字特效用 fill:\"both\"（backwards 会让字母播完消失）",
      'fill:"backwards"' not in _wm and _wm.count('fill:"both"') >= 6,
      f"both={_wm.count(chr(34)+'fill:'+chr(34))}")
# 至少一半的特效在时间线里显式写了"保持"（末帧不透明）
check("★ 字特效整体不会太快（单条 <=1500ms，否则等太久）",
      max(_durs) <= 1500, f"最长 {max(_durs) if _durs else '?'}ms")

print()
print("═══ 12) ★★ 箴言必须装进起点→收尾的窗口（否则被截断=看着像消失）")
_motto = js[js.index("const MOTTO_FX_FN"):js.index("\n};", js.index("const MOTTO_FX_FN"))]
_mdur = [int(x) for x in re.findall(r"duration:\s*(\d+)", _motto)]
_m_start = re.search(r"after\((\d+),\s*\(\)\s*=>\s*\{\s*try\s*\{\s*MOTTO_FX_FN", js)
_fin = re.search(r"after\((\d+), finishSplash\)", js)
_m_begin = re.search(r"after\((\d+),\s*\(\)\s*=>", js)
# 取箴言那条 after 的起点
_motto_at = None
for m in re.finditer(r"after\((\d+),", js):
    seg = js[m.end():m.end() + 160]
    if "MOTTO_FX_FN" in seg:
        _motto_at = int(m.group(1))
if _motto_at and _mdur and _fin:
    _latest = max(_mdur)
    _window = int(_fin.group(1)) - _motto_at
    check("★ 箴言时长 <= 窗口（起点→收尾）⇒ 不会被截断",
          _latest <= _window,
          f"箴言 {_latest}ms vs 窗口 {_window}ms（起点 {_motto_at} → 收尾 {_fin.group(1)}）")
    check("★ 窗口内留有可见的停留（>=100ms）",
          _window - _latest >= 100, f"余量 {_window - _latest}ms")
else:
    check("★ 能定位箴言起点/时长/收尾", False,
          f"at={_motto_at} durs={_mdur} fin={bool(_fin)}")
_holds2 = re.findall(r"offset:\.(\d+)", _motto)
check("★ 箴言每条都有停住（每条两处 offset = 起点 + 停留终点）",
      len(_holds2) >= 6, f"offset: .{_holds2}")

print()
print("═══ 13) ★★ 开屏收尾必须用显式 Web Animation（CSS 同帧加类可能被跳过）")
check("★ finishSplash 用 sp.animate([...]) 做收尾",
      re.search(r"sp\.animate\(\[", js) is not None)
check("★ 收尾动画覆盖 opacity + transform + filter（化开而非单纯透明）",
      "scale(1.045)" in js and "blur(12px)" in js and "opacity:0" in js[js.index("const OUT_MS"):js.index("const OUT_MS") + 400])
check("★ 隐藏等待按 OUT_MS 计算（不再硬编码 1200）",
      "OUT_MS + 180" in js or "OUT_MS +" in js)
check("CSS 里仍保留 .splash.out 作为极老环境的后备",
      re.search(r"\.splash\.out\{", HTML) is not None)

print("=" * 60)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 开屏/层级/墨渗/时长 四项都符合预期")
