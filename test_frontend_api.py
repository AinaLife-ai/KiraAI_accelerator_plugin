"""★ 守卫：前端 URL 前缀必须和后端注册方式对得上。

线上事故复盘（2026-09-23 用户实测）：
  面板「未连接」+ 壁纸 0 张。根因是前缀写错：
    框架自带端点  → /api/plugins/<id>/…（复数 s）
    插件自定义端点 → /api/plugin/<id>/… （单数）
  前端对两者都用了复数 ⇒ 插件端点全部 404 ⇒ health 挂、壁纸列表空。

这个测试不需要框架、不需要网络 —— 纯静态比对「前端拼的 URL」与
「后端声明的路由」，正是当初缺的那一环（当时只用假数据渲染面板，
只能证明 HTML/CSS 没问题，证明不了 URL 能通）。
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HTML = HERE / "web" / "index.html"
MAIN = HERE / "main.py"

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


html = HTML.read_text(encoding="utf-8")
main_src = MAIN.read_text(encoding="utf-8")

print("1) 前缀常量本身")
api = re.search(r'const\s+API\s*=\s*"([^"]*)"', html)
papi = re.search(r'const\s+PAPI\s*=\s*"([^"]*)"', html)
check("前端定义了 API 常量（框架端点）", api is not None)
check("前端定义了 PAPI 常量（插件自定义端点）", papi is not None)
if not (api and papi):
    print("无法继续"); sys.exit(1)

check("★ API  == /api/plugins/（复数，框架自带端点）",
      api.group(1) == "/api/plugins/", api.group(1))
check("★ PAPI == /api/plugin/（单数，@register.api 端点）",
      papi.group(1) == "/api/plugin/", papi.group(1))
check("两个前缀确实不同（不是笔误复制）", api.group(1) != papi.group(1))

print("\n2) 后端 real 声明：@register.api 的 path")
declared = set(re.findall(r'@register\.api\([^)]*path="([^"]+)"', main_src))
declared |= set(re.findall(r'@register\.api\(path="([^"]+)"', main_src))
print(f"     main.py 声明的插件端点: {sorted(declared)}")
check("main.py 至少声明了 health / wallpapers", {"health", "wallpapers"} <=
      {d.lstrip('/') for d in declared}, str(sorted(declared)))

print("\n3) ★★ 前端用 PAPI 拼的每个端点，后端都真的声明过")
papi_used = sorted(set(re.findall(r'(?<![A-Za-z_])PAPI\s*\+\s*"([^"]+)"', html)))
papi_used = [p for p in papi_used if "{" not in p and not p.endswith("/")]
print(f"     前端 PAPI 用到: {papi_used}")
declared_norm = {("/" + d.lstrip("/")) for d in declared}
for p in papi_used:
    check(f"PAPI {p} 后端有对应 @register.api", p in declared_norm,
          f"声明集合={sorted(declared_norm)}")

print("\n4) 前端用 API 拼的每个端点，都属于框架自带（白名单）")
# 框架自带端点白名单（来源 webui/routes/plugins.py 的真实 path= 声明）
FRAMEWORK_OK = {"/config"}
api_used = sorted(set(re.findall(r'(?<![A-Za-z_])API\s*\+\s*"([^"]+)"', html)))
api_used = [p for p in api_used if "{" not in p and not p.endswith("/")]
print(f"     前端 API 用到: {api_used}")
for p in api_used:
    check(f"API {p} 是框架自带端点（复数前缀）", p in FRAMEWORK_OK,
          f"白名单={sorted(FRAMEWORK_OK)}")

print("\n5) ★ 反向验证：把守卫跑在「线上出错的写法」上，必须报红")


def audit(src):
    """守卫核心：返回 [(端点, 是否通过)]，前缀错则全红。"""
    a = re.search(r'const\s+API\s*=\s*"([^"]*)"', src)
    pa = re.search(r'const\s+PAPI\s*=\s*"([^"]*)"', src)
    if not (a and pa):
        return [("<常量缺失>", False)]
    res = [("PAPI 前缀 == /api/plugin/", pa.group(1) == "/api/plugin/")]
    for q in set(re.findall(r'(?<![A-Za-z_])PAPI\s*\+\s*"([^"]+)"', src)):
        if "{" in q or q.endswith("/"):
            continue
        res.append((f"PAPI {q} 声明存在", q in declared_norm))
    for q in set(re.findall(r'(?<![A-Za-z_])API\s*\+\s*"([^"]+)"', src)):
        if "{" in q or q.endswith("/"):
            continue
        res.append((f"API {q} 在框架白名单", q in FRAMEWORK_OK))
    return res


# 现状（已修）必须全绿
good_res = audit(html)
check("修复后的 HTML 通过守卫", all(ok for _, ok in good_res),
      f"{sum(1 for _, ok in good_res if ok)}/{len(good_res)}")

# 还原成线上出错的写法（PAPI 用复数）—— 必须报红
bad = html.replace('const PAPI = "/api/plugin/"', 'const PAPI = "/api/plugins/"')
bad_res = audit(bad)
bad_fails = [n for n, ok in bad_res if not ok]
check("★ 反向验证：篡改成复数后守卫确实报红", len(bad_fails) > 0,
      f"报红项={bad_fails}")

print("\n" + "=" * 56)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    print("失败项:")
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过（前端 URL 前缀与后端声明一致）")
