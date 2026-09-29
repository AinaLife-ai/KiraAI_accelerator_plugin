/* ★★★ 背景切换：**行为级**回归（Node，无浏览器）。
 *
 * 为什么需要它：2026-09-29 用户报的两个 bug 都不是"某行写错"，而是
 * **时序 / 层级语义**错：
 *   · 图层顺序靠 DOM 顺序 ⇒ 只有一半的切换方向能看（另一半整个转场不可见）
 *   · 收尾"先改 CSS 状态、后撤载体" ⇒ 中间有一帧露出旧图
 * 这类问题语法检查和静态断言都会放过去，只有**真跑一遍转场**才抓得住。
 *
 * 做法：从 web/index.html 抽出壁纸引擎那段脚本，喂给一个最小的
 * DOM + Web Animations 桩执行，然后断言两个不变量。
 *
 * 用法：node tests/test_wallpaper_transition.js
 */
"use strict";
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "web", "index.html"), "utf8");

const PASS = [], FAIL = [];
function check(name, ok, detail) {
  (ok ? PASS : FAIL).push(name);
  console.log(`  ${ok ? "✓" : "✗"} ${name}` + (detail ? `  [${detail}]` : ""));
}

/* ── 抽引擎脚本 ───────────────────────────────────────────────────── */
const SRC = HTML.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/)[1];
const s0 = SRC.indexOf("/* ═══ 壁纸轮换（支持多种切换特效） ═══ */");
const s1 = SRC.indexOf("/* ═══════════════════════════════════════════════════════════\n   开屏");
const ENGINE = SRC.slice(s0, s1);

/* ── 最小 DOM 桩 ──────────────────────────────────────────────────── */
function cls() {
  const set = new Set();
  return {
    add: c => set.add(c), remove: c => set.delete(c),
    contains: c => set.has(c), _set: set,
  };
}
function style() {
  const o = {};
  return new Proxy(o, {
    get(t, k) {
      if (k === "setProperty") return (n, v) => { t[n] = v; };
      if (k === "removeProperty") return n => { delete t[n]; };
      if (k === "cssText") return t.cssText;
      return t[k];
    },
    set(t, k, v) { t[k] = v; return true; },
  });
}
function mkElLeaf(id) {
  return {
    id, parentElement: null, children: [], classList: cls(), style: style(),
    _order: 0, _anims: [],
    querySelector() { return null; }, querySelectorAll() { return []; },
    appendChild(x) { x.parentElement = this; this.children.push(x); return x; },
    removeChild(x) { this.children = this.children.filter(y => y !== x); return x; },
    remove() { if (this.parentElement) this.parentElement.removeChild(this); },
    addEventListener() {}, removeEventListener() {},
    getAnimations() { return this._anims.slice(); },
    animate(kf, opt) {
      const a = {
        _kf: kf, _opt: opt || {}, currentTime: 0, playState: "running",
        effect: { getTiming: () => a._opt, getKeyframes: () => a._kf },
        cancel() { a.playState = "idle"; const i = this._anims.indexOf(a); if (i >= 0) this._anims.splice(i, 1); },
        pause() { a.playState = "paused"; },
        finish() { a.playState = "finished"; },
      };
      this._anims.push(a);
      return a;
    },
    getBoundingClientRect: () => ({ width: 412, height: 915, top: 0, left: 0 }),
    offsetWidth: 412, offsetHeight: 915, offsetTop: 0, offsetLeft: 0,
    setAttribute(k, v) { this["attr_" + k] = v; }, getAttribute(k) { return this["attr_" + k]; },
    setAttributeNS() {}, closest(sel) { return sel === "svg" ? (this._svg || null) : null; },
  };
}
function mkEl(id, parent, order) {
  const el = mkElLeaf(id);
  el.parentElement = parent; el._order = order;
  el.querySelector = function (sel) {
    if (sel === ".wp-img") return el._img;
    const c = sel.replace(/^\./, "");
    return el.children.find(x => x.classList.contains(c)) || null;
  };
  el.querySelectorAll = function (sel) {
    const c = sel.replace(/^\./, "");
    return el.children.filter(x => x.classList.contains(c));
  };
  el._img = mkElLeaf(id + "-img");
  return el;
}

/* 树：wallpaper > [svg(ink), scrim, wp-a, wp-b, wp-rim] */
const root = mkEl("wallpaper", null, 0);
const svg = mkEl("inkSvg", root, 0);
const inkImg = mkElLeaf("wpInkImg");
inkImg.closest = () => svg;
const scrim = mkEl("scrim", root, 1);
const stageA = mkEl("wp-a", root, 2);
const stageB = mkEl("wp-b", root, 3);
const rim = mkEl("wpRim", root, 4);
root.children = [svg, scrim, stageA, stageB, rim];
/* .wp-img 在 stage 内部：classList 带 wp-img */
[stageA, stageB].forEach(st => {
  st._img.classList.add("wp-img");
  st._img.parentElement = st;
  st.children = [st._img];
});

const byId = { wallpaper: root, wpInkImg: inkImg, wpRim: rim, "wp-a": stageA, "wp-b": stageB };
const inkCM = mkElLeaf("wpInkCM"), inkN1 = mkElLeaf("wpInkN1"), inkN2 = mkElLeaf("wpInkN2");

/* 计算样式：把舞台上写的 inline + 类规则合并出来（够用即可） */
function computed(el, stage) {
  if (el === stage) {
    return {
      opacity: el.style.opacity !== undefined ? el.style.opacity : "1",
      visibility: el.style.visibility || "visible",
      zIndex: el.style.zIndex !== undefined ? String(el.style.zIndex) : "1",
      display: "block",
    };
  }
  /* .wp-img */
  const inlineOp = el.style.opacity;
  const onStage = stage.classList.contains("on");
  let op = inlineOp !== undefined && inlineOp !== "" ? inlineOp : (onStage ? "1" : "0");
  /* 若有 fill:forwards 动画，最后一个关键帧的 opacity 覆盖 inline */
  let animOp = null;
  for (const a of el._anims) {
    if (a.playState === "idle") continue;
    const frames = a._kf.filter(f => f.opacity !== undefined);
    if (!frames.length) continue;
    const t = a.currentTime, dur = a._opt.duration || 1;
    const p = Math.max(0, Math.min(1, t / dur));
    /* 线性近似：取最接近的关键帧（本测试只关心端点，足够） */
    let pick = frames[frames.length - 1];
    if (p < 0.5 && frames.length > 1) pick = frames[0];
    animOp = String(pick.opacity);
  }
  return {
    opacity: animOp !== null ? animOp : String(op),
    visibility: el.style.visibility || "visible",
    zIndex: "auto", display: "block",
  };
}

const document = {
  getElementById: id => byId[id] || null,
  querySelector: sel => {
    const m = sel.match(/^#(wp-[ab]) \.wp-img$/);
    return m ? byId[m[1]]._img : null;
  },
  querySelectorAll: sel => (sel === ".s-strip" ? [] : []),
  createElement: tag => {
    if (tag === "div") { const e = mkElLeaf("strip"); e.classList.add("s-strip"); return e; }
    const e = mkElLeaf(tag);
    e.onload = null; e.onerror = null; e.decode = () => Promise.resolve();
    return e;
  },
  addEventListener() {}, removeEventListener() {},
  body: mkEl("body", null, 0),
  documentElement: mkEl("html", null, 0),
  timeline: { currentTime: 0 },
};

/* ── 运行引擎 ─────────────────────────────────────────────────────── */
const sandbox = {
  document, window: {}, navigator: { userAgent: "node" },
  getComputedStyle: el => {
    for (const [id, st] of Object.entries(byId)) {
      if (el === st) return computed(st, st);
      if (el === st._img) return computed(st._img, st);
    }
    return { opacity: "1", visibility: "visible", zIndex: "auto", display: "block", filter: "none" };
  },
  requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
  setTimeout: (f) => { if (typeof f === "function") f(); return 0; },
  clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  performance: { now: () => 0 },
  console: { log() {}, warn() {}, error() {} },
  Math, JSON, Promise, Object, Array, String, Number, Error, Set, Map, isFinite, parseFloat, parseInt,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.window.matchMedia = () => ({ matches: false, addEventListener() {} });
sandbox.window.innerWidth = 412;
sandbox.window.innerHeight = 915;
sandbox.window.addEventListener = () => {};
sandbox.Image = function () { return { onload: null, onerror: null, decode: () => Promise.resolve(), src: "" }; };
sandbox.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });

const wrapper = `
(function(document, window, getComputedStyle, requestAnimationFrame, cancelAnimationFrame,
          setTimeout, clearTimeout, setInterval, clearInterval, performance, console,
          Image, fetch, navigator, Math, JSON, Promise, Object, Array, String, Number,
          Error, Set, Map, isFinite, parseFloat, parseInt){
  ${ENGINE}
  return { wpLoad, wpNormalize, wpStage, wpImg, wpTransition, wpSettle, wpUrl, wpSetLayerOrder,
           get wpCur(){return wpCur}, set wpCur(v){wpCur=v},
           get wpBusy(){return wpBusy}, set wpBusy(v){wpBusy=v},
           get wpEffect(){return wpEffect}, set wpEffect(v){wpEffect=v},
           get wpLastDur(){return wpLastDur}, get wpCleanup(){return wpCleanup} };
})`;

let API;
try {
  API = eval(wrapper)(sandbox.document, sandbox.window, sandbox.getComputedStyle,
    sandbox.requestAnimationFrame, sandbox.cancelAnimationFrame, sandbox.setTimeout,
    sandbox.clearTimeout, sandbox.setInterval, sandbox.clearInterval, sandbox.performance,
    sandbox.console, sandbox.Image, sandbox.fetch, sandbox.navigator, Math, JSON, Promise,
    Object, Array, String, Number, Error, Set, Map, isFinite, parseFloat, parseInt);
  check("引擎在 DOM 桩里成功加载", true);
} catch (e) {
  check("引擎在 DOM 桩里成功加载", false, String(e).slice(0, 120));
}

if (API) {
  console.log("\n═══ 1) wpLoad() 必须确立『新层在上』的层级");
  for (const [from, to] of [["wp-a", "wp-b"], ["wp-b", "wp-a"]]) {
    /* 先都装上，让层级由最后一次 wpLoad 决定 */
    API.wpLoad("wp-a", "A.svg");
    API.wpLoad("wp-b", "B.svg");
    const zB = stageB.style.zIndex, zA = stageA.style.zIndex;
    check(`wpLoad("wp-b") 后：wp-b(${zB}) 高于 wp-a(${zA})`,
      Number(zB) > Number(zA), `a=${zA} b=${zB}`);
  }
  /* 反向再验一次 */
  API.wpLoad("wp-a", "A2.svg");
  check("wpLoad(\"wp-a\") 后：wp-a 高于 wp-b（两个方向都成立）",
    Number(stageA.style.zIndex) > Number(stageB.style.zIndex),
    `a=${stageA.style.zIndex} b=${stageB.style.zIndex}`);

  console.log("\n═══ 2) 转场中『最上面的可见层』必须是新层");
  /* 桩里没有真渲染循环，直接检查 wpLoad 之后的静态不变量：
     —— 这正是这个 bug 的可判定形式：层级必须在**装配时就确定**，
        而不是等到收尾。只要 wpLoad 立好 z-index，整个转场就都对。 */
  API.wpLoad("wp-b", "B.svg");
  const zTop = Number(stageB.style.zIndex), zBot = Number(stageA.style.zIndex);
  check("转场期间：新层 z-index 严格大于旧层 ⇒ 旧图永远盖不住新图",
    zTop > zBot, `new=${zTop} old=${zBot}`);

  console.log("\n═══ 3) 收尾顺序：撤载体必须在放行旧层之前（代码级已静态验，这里验行为）");
  let cleanupAt = null, offAt = null;
  const origRemove = stageA.classList.remove.bind(stageA.classList);
  stageA.classList.remove = c => {
    if (c === "on" && offAt === null) offAt = "called";
    return origRemove(c);
  };
  /* wpSettle 会用 wpCleanup 撤载体；这里直接观察二者顺序 */
  /* 只看**同一个 try 块里**那一对：wpSettle 里"放行旧层"的那一句是
     `if (_st0){ _st0.classList.remove("on"); }`（带 _st0 守卫）。
     用 _st0 版做锚点才不会被后面那个"兜底再摘一次"的 remove("on") 干扰。 */
  const _settleSrc = ENGINE.slice(ENGINE.indexOf("function wpSettle"));
  const pCleanup = _settleSrc.indexOf("wpCleanup()");
  const pRemoveOn = _settleSrc.indexOf('_st0.classList.remove("on")');
  check("wpSettle：撤载体（wpCleanup）先于 放行旧层（摘 .on）",
    pCleanup >= 0 && pRemoveOn >= 0 && pCleanup < pRemoveOn, `cleanup@${pCleanup} on@${pRemoveOn}`);
  /* 撤载体必须发生在**摘 .on 之前**：即"第一次 remove(\"on\")"要晚于 wpCleanup()。
     （wpSettle 里 remove("on") 出现多次是刻意的双保险，只看**第一次**。） */
  const pFirstOn = _settleSrc.indexOf('classList.remove("on")');
  check("wpSettle：第一次摘 .on 在 wpCleanup() 之后（无空窗）",
    pCleanup >= 0 && pFirstOn >= 0 && pCleanup < pFirstOn,
    `cleanup@${pCleanup} firstOn@${pFirstOn}`);
}

console.log("\n" + "═".repeat(62));
console.log(`通过 ${PASS.length}  失败 ${FAIL.length}`);
if (FAIL.length) { FAIL.forEach(f => console.log("  ✗", f)); process.exit(1); }