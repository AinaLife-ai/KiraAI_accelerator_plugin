import re, pathlib, json, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "web/index.html").read_text(encoding="utf-8")
SCHEMA = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
PY_ALL = {p.name: p.read_text(encoding="utf-8") for p in ROOT.glob("*.py")}
OK = []; BAD = []
def check(n, c, d=""):
    (OK if c else BAD).append(n)
    print(("  \u2713 " if c else "  \u2717 ") + n + (("  [" + str(d) + "]") if d else ""))

print("=== v1.0.2 新增功能 ===")

# 1) providers 多选
_f = SCHEMA["section_stream"]["fields"]["providers"]
check("① providers 用框架规范的 multi_select", _f.get("type") == "multi_select", _f.get("type"))
check("① source=model（选项来自已配置模型）", _f.get("source") == "model")
check("① allow_custom 兜底（拿不到列表仍可手填）", _f.get("allow_custom") is True)
_m = re.search(r"not \(\{pname, pid, f\"\{pid\}:\{mid\}\"\} & allowed\)", PY_ALL.get("main.py", ""))
check("① 后端同时认 model/provider/name 三种取值", bool(_m))
# ★★★ 这里曾是**真 bug**：我把 `list` 与 `multi_select` 并成同一分支 ⇒
#   工具黑白名单、壁纸列表等**自由填写的 list 字段**被误改成"选提供商"下拉。
#   判据必须区分两者：multi_select 走下拉；list 保持文本框。
check("① multi_select 走多选下拉", 'type === "multi_select"' in HTML)
check("★★★ list 保持逗号分隔文本框（不得被并进下拉）",
      'type === "list" || type === "multi_select"' not in HTML and 'type === "list"' in HTML)
check("① 面板有 /providers 加载（拿不到就退化为手填）",
      "loadProviderOptions" in HTML and "__ACCEL_PROVIDERS__" in HTML)
check("① 新增 GET /providers API", 'path="/providers"' in PY_ALL.get("main.py", ""))

# 2) 标题扫光
check("② neon-drop 关键帧存在（由上往下）", "@keyframes neonDrop" in HTML)
check("② drop 每轮重掷随机量", "startDropCycle" in HTML and "--drop-x" in HTML and "--drop-dur" in HTML)
check("② drop 靠 .neon-drop-run 触发（否则只跑一次）", "neon-drop-run" in HTML)
check("② drop 有描边 ⇒ 字不会被遮住", re.search(r"neon-drop[^{]*\{[^}]*text-stroke", HTML) is not None)
# ★ 用户明确否掉"绕中心旋转"（"太难看了"）⇒ 换成赛博朋克式**撕裂脉冲**。
#   判据随之改：**不得再有旋转**，且确有 glitch。
#   （剥注释后再判 —— 注释里提到 orbitSpin 会假红）
_nc_v = re.sub(r"/\*[\s\S]*?\*/", "", HTML)
check("② orbit 不再绕中心旋转（用户否掉了）", "orbitSpin" not in _nc_v)
check("② orbit 换成撕裂脉冲（glitch）",
      "@keyframes neonGlitch" in HTML and "neon-glitch" in HTML)
check("② ring 真正启用（neonRing 被引用）",
      re.search(r"neon-ring[^{]*\{[^}]*animation:\s*neonRing", HTML) is not None)
check("② NEON_MODES 含 neon-drop", "neon-drop" in re.search(r"const NEON_MODES\s*=\s*\[([^\]]*)\]", HTML).group(1))

# 3) 外链按钮
# ★ 用户要求：位置改到**小字下方居中**、尺寸缩小、随欣赏模式隐藏；
#   并且曾经误出现**两个**按钮（open-front + open-panel 重复）。
check("③ 只有一个外链按钮（曾误出现两个）",
      HTML.count('id="btn-open-front"') == 1 and "icon-btn" not in HTML)
check("③ 按钮在小字下方居中（.front-row）",
      'class="front-row"' in HTML and ".front-row{display:flex;justify-content:center" in HTML)
check("③ 随欣赏模式一起隐藏",
      re.search(r"body\.zen \.shell > \.front-row", HTML) is not None)
check("③ 尺寸已缩小（24px）", "width:24px;height:24px" in HTML)
check("③ 用 i-external 图标且图标已定义", 'href="#i-external"' in HTML and 'id="i-external"' in HTML)
check("③ 新标签页打开（window.open + noopener）",
      "window.open(" in HTML and "noopener" in HTML)
check("③ 走 /plugin-page/ 路径", "/plugin-page/" in HTML)
# ★ 去掉外圈（那正是"看起来像两个按钮"的原因），持续动效改做在**图标自身**
check("③ 持续特效做在图标自身（无外圈）",
      "@keyframes ofGlow" in HTML and "ofBreath" not in HTML
      and "inset:-2px;border-radius:11px" not in HTML)
check("③ 有 title 无障碍提示", "在新标签页打开前端" in HTML)

# 4/5) 工具轮 / 复读
check("④ 工具轮已发内容会被标记（避免重复发送/复读）",
      "_accel_tool_turn_early" in PY_ALL.get("stream_engine.py", ""))
check("④ 有工具标签转义的诊断日志",
      "xml_tag_fixer" in PY_ALL.get("stream_engine.py", ""))
check("④ 本插件确实从不 escape（只 unescape）",
      "escape" not in PY_ALL.get("early_sent.py", "").replace("unescape", "")
      and "unescape" in PY_ALL.get("early_sent.py", ""))
# ★ 判据要**通用**：不该把版本号写死（每升一次就要改判据 = 判据在追着代码跑）。
#   这里只断言"比基线 1.0.1 新"，版本继续往前也不会误报。
_v = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["version"]
def _vt(s):
    return tuple(int(x) for x in s.split("."))

# ★ 用户反馈"boost 字内扫描好像还没看到" ⇒ 原来是一次性动画（跑完就静默），
#   已改成 infinite + 定期换参数 ⇒ 只要轮到该模式就一直在扫。
check("② 字内扫描持续（infinite，不是跑一次就停）",
      re.search(r"neon-drop-run[^{]*\{[^}]*animation:neonDrop[^;]*infinite", HTML) is not None)
check("② 撕裂不旋转（用户否掉旋转）",
      "orbitSpin" not in re.sub(r"/\*[\s\S]*?\*/", "", HTML))
check("② 撕裂是短促脉冲（静默占多数，不是一直抖）",
      re.search(r"0%,86%,100%\{transform:translate\(0,0\)", HTML) is not None)


print()
print("═══ ★★★ 标题：reduced-motion 下必须**全部** 9 个模式都被抢救 ═══")
# 这是本次的真 bug：抢救名单只有 5 个，另 4 个（orbit/drop/breathe/ring）
# 被 `.wordmark>span{animation:none!important}` 干掉 ⇒ 用户"完全没看到"。
# 判据写成**通用**的：从 NEON_MODES 推出应有清单，逐个核对，不写死名字。
_nc = re.sub(r"/\*[\s\S]*?\*/", "", HTML)
_allb = "".join(m.group(1) for m in
               re.finditer(r"@media \(prefers-reduced-motion:reduce\)\{((?:[^{}]|\{[^{}]*\})*)\}", _nc))
_modes = [x.strip().strip('"') for x in
          re.search(r"const NEON_MODES\s*=\s*\[([^\]]*)\]", HTML).group(1).split(",")]
_missing = [m for m in _modes if m not in _allb]
check("★★★ 减少动效下**每个**霓虹模式都被保留（只放慢、不关闭）",
      not _missing, f"缺失={_missing}" if _missing else f"{len(_modes)}/{len(_modes)} 全在")

print()
print("═══ 英文箴言总数 ═══")
_mo = re.search(r"const MOTTOES = \[([\s\S]*?)\];", HTML)
_items = re.findall(r'"([^"]+)"', _mo.group(1)) if _mo else []
check("★ 英文箴言 19 条", len(_items) == 19, f"{len(_items)} 条")

check("⑤ 版本号已前进（> 1.0.1）", _vt(_v) > _vt("1.0.1"), _v)

print()
print(("🎉 全部通过（%d 项）" % len(OK)) if not BAD else ("❌ %d 项未通过: %s" % (len(BAD), BAD[:5])))
sys.exit(1 if BAD else 0)
