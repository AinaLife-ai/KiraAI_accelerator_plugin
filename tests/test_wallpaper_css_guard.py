#!/usr/bin/env python3
# 守卫：.wp-stage / .sp-sway 的 will-change / backface-visibility 是**承重**的。
# 2026-09-27 事故：当成"性能提示"删掉 => 全站壁纸含开屏全部消失，
# 只有墨渗/燃纸切换时载层还能看见。锁死这两条规则，防止再次被"优化"。
import pathlib, sys

HTML = pathlib.Path(__file__).resolve().parent.parent / "web" / "index.html"
t = HTML.read_text(encoding="utf-8")
ok = True

RULES = {
    ".wp-stage": ".wp-stage{position:absolute;inset:calc(-1 * max(72px, 6%));z-index:1;will-change:transform;backface-visibility:hidden}",
    ".sp-sway": ".sp-sway{position:absolute;inset:calc(-1 * max(72px, 6%));will-change:transform}",
    ".wp-img": "opacity:0;will-change:transform,opacity,clip",
}
for name, rule in RULES.items():
    hit = rule in t
    print(("  ok   " if hit else "  FAIL ") + name + " 的合成层属性保留")
    ok = ok and hit

print("\n" + ("PASS 壁纸 CSS 承重属性未被移除" if ok else "FAIL 壁纸 CSS 承重属性被改 => 全站无背景"))
sys.exit(0 if ok else 1)
