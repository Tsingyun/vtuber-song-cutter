/**
 * JIZURA 部件清单统计器
 *
 * 用 Node 加载 JIZURA 仓库的 src/*.js（纯数据部分），统计各职能注册表里实际注册了多少部件。
 * 用途：核对上游版本升级后部件库规模的变化，或验证 README 宣称的部件数。
 *
 * 用法：  node tools/jizura_parts.js [JIZURA仓库路径]
 *         node tools/jizura_parts.js D:/岁己歌切/_tmp/JIZURA
 *
 * 只执行注册逻辑，不触碰渲染，因此需要的最小 DOM 存根已在下方给出（canvas ctx 全部 no-op）。
 */
const fs = require('fs'), path = require('path'), vm = require('vm');

const root = String(process.argv[2] || '').replace(/[\\/]+$/, '');
if (!root) { console.error('用法: node tools/jizura_parts.js <JIZURA仓库路径>'); process.exit(2); }
const srcDir = path.join(root, 'src');
if (!fs.existsSync(srcDir)) { console.error('找不到 src/: ' + srcDir); process.exit(2); }

/* ---- 最小 DOM 存根：ctx 的每个方法都是 no-op，measureText 给一个固定宽度 ---- */
const ctxStub = {
  font: '', textAlign: '', textBaseline: '', fillStyle: '', strokeStyle: '', lineWidth: 0,
  globalAlpha: 1, globalCompositeOperation: '', filter: '', imageSmoothingEnabled: true,
  shadowColor: '', shadowBlur: 0, shadowOffsetX: 0, shadowOffsetY: 0,
  lineJoin: '', lineCap: '', miterLimit: 0, lineDashOffset: 0,
  measureText: () => ({ width: 50 }),
  getImageData: (x, y, w, h) => ({ data: new Uint8Array(Math.max(4, w * h * 4)) }),
  createImageData: (w, h) => ({ data: new Uint8Array(Math.max(4, w * h * 4)) }),
  getTransform: () => ({ a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 }),
  createLinearGradient: () => ({ addColorStop() {} }),
  createRadialGradient: () => ({ addColorStop() {} }),
  createPattern: () => null,
};
for (const m of ['putImageData', 'clearRect', 'fillRect', 'drawImage', 'save', 'restore', 'translate',
  'scale', 'rotate', 'transform', 'setTransform', 'beginPath', 'closePath', 'moveTo', 'lineTo',
  'quadraticCurveTo', 'arc', 'arcTo', 'rect', 'clip', 'fill', 'stroke', 'fillText', 'strokeText',
  'setLineDash']) ctxStub[m] = () => {};

const sandbox = { console, Math, Date, JSON, Object, Array, String, Number, Boolean, Map, Set, WeakMap,
  Promise, Symbol, Proxy, Reflect, RegExp, Error, TypeError, RangeError, isNaN, isFinite,
  parseInt, parseFloat, Int8Array, Uint8Array, Int32Array, Uint32Array, Float32Array, Float64Array,
  ArrayBuffer, DataView, setTimeout, clearTimeout, requestAnimationFrame: () => 0 };
sandbox.window = sandbox; sandbox.globalThis = sandbox; sandbox.self = sandbox;
sandbox.document = {
  createElement: () => ({ width: 0, height: 0, style: {}, getContext: () => ctxStub, appendChild() {}, addEventListener() {} }),
  head: { appendChild() {} }, body: { appendChild() {} },
  fonts: { load: () => Promise.resolve(), ready: Promise.resolve(), add() {} },
  addEventListener() {}, getElementById: () => null, querySelector: () => null,
  querySelectorAll: () => [], documentElement: {},
};
sandbox.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
sandbox.navigator = { userAgent: 'node', language: 'ja' };
sandbox.performance = { now: () => 0 };
vm.createContext(sandbox);

/* ---- 加载全部 src（按文件名排序，与官方 build.py 一致；跳过 UI 层） ---- */
const files = fs.readdirSync(srcDir).filter(f => f.endsWith('.js')).sort();
let failed = 0;
for (const f of files) {
  if (f === '12_ui.js') continue;
  try { vm.runInContext(fs.readFileSync(path.join(srcDir, f), 'utf8'), sandbox, { filename: f }); }
  catch (e) { failed++; console.log('  ! 跳过', f, '|', String(e.message).slice(0, 70)); }
}

const J = sandbox.J;
if (!J || !J.GROUP_KEYS) { console.error('加载失败：没能拿到 J.GROUP_KEYS'); process.exit(1); }

console.log('JIZURA 部件清单 @ ' + root);
if (failed) console.log('（有 ' + failed + " 个文件加载失败，见上）");
console.log('');
console.log('风格 styles : ' + Object.keys(J.STYLES).length);

let total = 0;
const rows = [];
for (const g of J.GROUP_KEYS) {
  const reg = J.registry(g), avail = J.order(g).length;
  const n = Object.keys(reg).length;
  total += n;
  rows.push([g, n, avail]);
}
const w = 8;
console.log('');
for (const [g, n, avail] of rows) {
  const label = { layout: '版面', enter: '入场', hold: '保持', exit: '退场', decor: '装饰',
    treat: '字饰', bg: '背景', cam: '相机', fx: '特效', trans: '转场' }[g] || g;
  console.log('  ' + (g + ' ' + label).padEnd(w + 4) + String(n).padStart(4) + '  (可抽选 ' + avail + ')');
}
console.log('  ' + ''.padEnd(w + 4) + '-'.repeat(6));
console.log('  合计 TOTAL     ' + String(total).padStart(4));

const packs = new Set(), sets = new Set();
for (const g of J.GROUP_KEYS) for (const k of Object.keys(J.registry(g))) {
  const d = J.registry(g)[k];
  packs.add(d.pack || '');
  if (d.set) sets.add(d.set);
}
console.log('');
console.log('packs : ' + packs.size + ' | ' + [...packs].filter(Boolean).join(', '));
console.log('sets  : ' + [...sets].join(', '));
