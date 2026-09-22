"""壁纸轮换的真逻辑回归测试（node 跑从 index.html 里**抽出来的真 JS**）。

背景（2026-09-23 用户实测）：
  面板有背景图，但**永远不换**。根因在 wpTransition 里：
      const cx = (18 + Math.random() * 64).toFixed(1);   // ← 字符串！
      const ex = (cx + (Math.random() * 24 - 12)).toFixed(1);
      //           ↑ 字符串 + 数字 = 拼接 ⇒ 字符串上没有 .toFixed ⇒ TypeError
  异常抛在 requestAnimationFrame 之前 ⇒
    · 新层拿不到 .on（opacity 0）⇒ 看不见换图
    · wpBusy 永远 true ⇒ 之后每次 wpTransition 都在第一行 return ⇒ **轮换静默死掉**

为什么以前没发现：面板只用「注入假数据 + 截图」验证过 ——
**静态渲染正常 ≠ 逻辑能跑**。（和 API 前缀那次是同一类错误。）
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 允许指向别的版本（用于反向验证：拿"改之前"的文件跑，必须报红）
HTML = Path(os.environ.get("PANEL_HTML") or (HERE / "web" / "index.html"))
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


if not shutil.which("node"):
    print("⚠ 找不到 node，跳过（这是真逻辑测试，需要 JS 运行时）")
    sys.exit(0)

html = HTML.read_text(encoding="utf-8")

print("1) 抽取面板真脚本")
scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
main = max(scripts, key=len)
check("抽到主脚本", len(main) > 2000, f"{len(main)} 字符")
check("脚本里有 wpTransition", "function wpTransition" in main)
check("脚本里有 startWallpaperRotation", "function startWallpaperRotation" in main)

# 把 let 声明的状态暴露出来（顶层 let 不会挂到 globalThis 上）
hook = """
;globalThis.__hook = {
  get wpBusy(){return wpBusy}, set wpBusy(v){wpBusy=v},
  get wpActive(){return wpActive}, set wpActive(v){wpActive=v},
  get wpIdx(){return wpIdx}, set wpIdx(v){wpIdx=v},
  get wpCur(){return wpCur}, set wpCur(v){wpCur=v},
  get wpEffect(){return wpEffect}, set wpEffect(v){wpEffect=v},
  get wpPick(){return wpPick}, set wpPick(v){wpPick=v},
  get wpEnabled(){return wpEnabled}, set wpEnabled(v){wpEnabled=v},
  get wpIntervalS(){return wpIntervalS}, set wpIntervalS(v){wpIntervalS=v},
  get WALLPAPERS(){return WALLPAPERS}, set WALLPAPERS(v){WALLPAPERS=v},
  wpTransition, startWallpaperRotation, stopWallpaperRotation, wpUrl,
};
"""
main = main + "\n" + hook

RUNNER = r"""
const fs = require('fs');

// ── 极简 DOM 垫片：只够让面板脚本加载 + 驱动壁纸逻辑 ──
const ELS = {};
function mkEl(id){
  const cls = new Set();
  const e = {
    id: id || '', tagName: 'DIV', _html: '', textContent: '', disabled: false,
    type: '', value: '', dataset: {},
    style: new Proxy({cssText:''}, {set(t,k,v){t[k]=v;return true},get(t,k){return t[k]}}),
    classList: {
      add(...c){ c.forEach(x=>cls.add(x)); },
      remove(...c){ c.forEach(x=>cls.delete(x)); },
      contains(c){ return cls.has(c); },
      toggle(c,f){ f ? cls.add(c) : cls.delete(c); },
    },
    set className(v){ cls.clear(); String(v).split(/\s+/).filter(Boolean).forEach(x=>cls.add(x)); },
    get className(){ return [...cls].join(' '); },
    appendChild(){}, addEventListener(){}, setAttribute(){}, getAttribute(){return null},
    querySelector(){ return mkEl(); },
    querySelectorAll(){ const a=[]; a.forEach=Array.prototype.forEach; return a; },
    get innerHTML(){ return this._html; }, set innerHTML(v){ this._html = String(v); },
  };
  if (id) ELS[id] = e;
  return e;
}
['wp-a','wp-b','save','save-label','form','conn','wp-dots','wp-picker'].forEach(mkEl);
// #wp-a / #wp-b 必须是**稳定同一对象**（wpTransition 靠它们切层）
const STABLE = { 'wp-a': ELS['wp-a'], 'wp-b': ELS['wp-b'] };

global.document = {
  body: mkEl('body'),
  documentElement: mkEl('html'),
  createElement: (t) => mkEl(),
  getElementById: (id) => STABLE[id] || ELS[id] || mkEl(id),
  querySelector: (s) => {
    if (s === '#wp-a') return STABLE['wp-a'];
    if (s === '#wp-b') return STABLE['wp-b'];
    const m = /^#([\w-]+)$/.exec(s);
    if (m) return ELS[m[1]] || mkEl(m[1]);
    return mkEl();
  },
  querySelectorAll: () => { const a=[]; a.forEach=Array.prototype.forEach; return a; },
  addEventListener: () => {},
};
global.window = global;
global.navigator = { language: 'zh-CN' };
global.requestAnimationFrame = (fn) => setTimeout(fn, 16);
global.getComputedStyle = () => ({ opacity: '1' });
global.bgData = [];

// fetch 垫片：/config 失败（走默认值），/wallpapers 返回 19 张
global.fetch = async (u) => {
  if (String(u).includes('/wallpapers') && !String(u).includes('/config')) {
    return { ok: true, json: async () => ({ files: Array.from({length:19},(_,i)=>'w'+i+'.webp') }) };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};

const src = fs.readFileSync(process.env.PANEL_JS, 'utf8');
try { (0, eval)(src); } catch (e) { console.log('SCRIPT_LOAD_ERROR: ' + e.message); process.exit(3); }
const H = globalThis.__hook;
if (!H) { console.log('HOOK_MISSING'); process.exit(4); }

(async () => {
  const out = {};
  // 让轮换快点
  H.WALLPAPERS = Array.from({length:19},(_,i)=>'w'+i+'.webp');
  H.wpEnabled = true; H.wpIntervalS = 1; H.wpPick = [];

  // ── A. 单次切换是否走完 ──
  H.startWallpaperRotation();
  const a = document.getElementById('wp-a'), b = document.getElementById('wp-b');
  out.afterStart = { busy: H.wpBusy, aOn: a.classList.contains('on'), aImg: a.style.backgroundImage };

  // 捕获切换异常：旧版会在这里抛 TypeError（用来做反向验证）
  let transitionError = null;
  try { H.wpTransition(H.wpUrl('w7.webp')); }
  catch (e) { transitionError = e.constructor.name + ': ' + e.message; }
  await new Promise(r => setTimeout(r, 2600));
  out.transitionError = transitionError;
  out.afterOneTransition = {
    busy: H.wpBusy, cur: H.wpCur,
    aOn: a.classList.contains('on'), bOn: b.classList.contains('on'),
    aImg: a.style.backgroundImage, bImg: b.style.backgroundImage,
  };

  // ── B. 定时器是否真的在换 ──
  const seen = [], busyTrack = [];
  const t0 = Date.now();
  const iv = setInterval(() => {
    const on = ['wp-a','wp-b'].find(id => document.getElementById(id).classList.contains('on'));
    const img = on ? document.getElementById(on).style.backgroundImage : '';
    seen.push(/wallpapers\/([^")]+)/.exec(img || '')?.[1] || null);
    busyTrack.push(H.wpBusy);
  }, 300);
  await new Promise(r => setTimeout(r, 6000));
  clearInterval(iv);
  out.rotationSamples = seen;
  out.distinct = [...new Set(seen.filter(Boolean))];
  out.busyTrack = busyTrack;
  // ★ 正确判据：锁必须**释放过**，而不是"采样结束那一瞬间恰好没在切"
  //   （切换本身要 2.15s，采到 busy=true 是正常的在飞状态）
  out.busyReleased = busyTrack.includes(false);
  out.busyEverHeld = busyTrack.includes(true);
  console.log(JSON.stringify(out));
  // ★ 必须显式退出：面板脚本里的 setInterval(poll,3000) 会让事件循环永远活着
  process.exit(0);
})();
"""

Path("/tmp/panel_js.txt").write_text(main, encoding="utf-8")
Path("/tmp/panel_runner.cjs").write_text(RUNNER, encoding="utf-8")

r = subprocess.run(["node", "/tmp/panel_runner.cjs"],
                   env={"PANEL_JS": "/tmp/panel_js.txt", "PATH": "/usr/bin:/bin"},
                   capture_output=True, text=True, timeout=90)
if r.returncode != 0:
    print("  node 退出码", r.returncode)
    print(r.stdout[-1500:]); print(r.stderr[-1500:])
    check("面板脚本能在 node 里加载", False, r.stdout.strip()[:120])
    print(f"\n通过 {len(PASS)} 失败 {len(FAIL)}")
    sys.exit(1)

data = json.loads(r.stdout.strip().splitlines()[-1])

print("\n2) 单次切换能否走完（修的正是这里）")
st = data["afterStart"]
check("startWallpaperRotation 后未卡锁", st["busy"] is False, f"busy={st['busy']}")
check("首图直接放上（第一张不做特效）", st["aOn"] and "wallpapers/" in (st["aImg"] or ""),
      str(st["aImg"])[:60])

t = data["afterOneTransition"]
check("★ 切换没有抛异常", not data.get("transitionError"),
      str(data.get("transitionError"))[:90])
check("★ 切换后锁已释放（旧版永远卡 true）", t["busy"] is False, f"busy={t['busy']}")
check("★ 新层拿到了 .on（旧版拿不到 ⇒ 永远看不见）",
      t["bOn"] or t["aOn"], f"aOn={t['aOn']} bOn={t['bOn']}")
check("新层确实换了图", "w7.webp" in (t["aImg"] or "") + (t["bImg"] or ""),
      f"a={t['aImg'][:38]} b={t['bImg'][:38]}")
check("当前层已切到另一块", t["cur"] in ("wp-a", "wp-b"), t["cur"])

print("\n3) ★ 定时自动随机轮换是否真的在换")
seen = data["rotationSamples"]
distinct = data["distinct"]
print(f"     5 秒采样: {seen}")
check("采样到多张不同壁纸（说明真的在换）", len(distinct) >= 2,
      f"出现 {len(distinct)} 张: {distinct}")
check("★ 轮换过程中锁会被释放（不是永久卡死）", data["busyReleased"] is True,
      f"busy 轨迹={data['busyTrack']}")
check("切换期间确实持锁（说明锁在起作用）", data["busyEverHeld"] is True)
# 连续两次采样相同的图是正常的（一次切换要 2.15s > 采样间隔 0.3s），
# 但整整 6 秒只有 1 张 = 没在换
check("★ 6 秒内换过多张（不是停在某一张）", len(data["distinct"]) >= 2,
      f"{len(data['distinct'])} 张: {data['distinct']}")

print("\n4) ★ 反向验证：把旧的写法喂回去，必须报错")
reverse = r"""
// 旧写法：toFixed 的结果（字符串）拿去参与算术
const cx = (18 + Math.random() * 64).toFixed(1);
let msg = 'no-error';
try { const ex = (cx + (Math.random() * 24 - 12)).toFixed(1); }
catch (e) { msg = e.constructor.name + ': ' + e.message; }
console.log(JSON.stringify({ cx, typeofCx: typeof cx, result: msg,
  branch: String(cx + 5).slice(0, 12) }));
"""
Path("/tmp/reverse.cjs").write_text(reverse, encoding="utf-8")
rr = subprocess.run(["node", "/tmp/reverse.cjs"], capture_output=True, text=True, timeout=30)
rev = json.loads(rr.stdout.strip())
check("★ 反向验证：旧写法确实抛 TypeError", "TypeError" in rev["result"],
      f"{rev['result']}（cx 的 typeof = {rev['typeofCx']}）")
check("★ 反向验证：字符串 + 数字变成拼接（不是加法）",
      len(rev["branch"]) > 2, f"'cx + 5' = {rev['branch']}")

print("\n5) 源码里不该再有「toFixed 结果参与算术」")
bad = re.findall(r"(\w+)\s*=\s*\([^)]*\)\.toFixed\([^)]*\)[^\n]*\n[^\n]*"
                 r"\(\s*\1\s*\+", main)
check("没再出现这种模式", not bad, f"{bad}")

print("\n" + "=" * 58)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    print("失败项:")
    for f in FAIL:
        print("   ✗", f)
    sys.exit(1)
print("🎉 全部通过 —— 轮换真的会定时随机换图")
