/*! test_pv_engine.cjs —— 引擎纯逻辑的回归测试（不需要浏览器、不渲染）。
 *
 *  跑法：
 *    "C:/Users/Tsing/.workbuddy/binaries/node/versions/22.22.2-22/node.exe" \
 *        tests/test_pv_engine.cjs
 *    （任意 node ≥ 18 都行；项目里常用的是 workbuddy 自带的那个）
 *
 *  锁住什么
 *  --------
 *  这一轮新加的三件事，全都有一个共同点：**做错了不会报错，只会看起来不太对**。
 *    1. 歌词分层（pickTier）：切点算错 → 半句话永远不出现，画面「正常」；
 *       空格参与了计数 → 英文行的切点整体偏移；
 *    2. 人物镜头耦合（figCamGain）：默认值写错 → 旧编排稿逐帧全变；
 *    3. 相机复算（applyCam）：gain=1 时与主循环的变换不等价 → 人物比背景
 *       慢半拍地飘，不报错、不崩，只是「看着不对」。
 *
 *  所以这里直接对着 PV.__probe 里的**同一份实现**断言（不在测试里镜像逻辑）。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const FILES = ['renderer/player/pv/pv_core.js', 'renderer/player/pv/pv_engine.js'];

const sandbox = { window: {}, console, Math, JSON, Date, Object, Array, Number,
  String, Boolean, isFinite, isNaN, parseFloat, parseInt, Set, Map, Promise,
  document: { createElement: () => ({ getContext: () => ({}) }) } };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
for (const f of FILES) {
  const src = fs.readFileSync(path.join(ROOT, f), 'utf8');
  vm.runInContext(src, sandbox, { filename: f });
}
const PV = sandbox.window.PV;
const P = PV.__probe;

let fails = [];
function ck(cond, name, detail) {
  if (cond) console.log('  ✓ ' + name);
  else { console.log('  ✗ ' + name + (detail === undefined ? '' : ' ' + JSON.stringify(detail))); fails.push(name); }
}
const G = (c) => ({ c, isSpace: false });
const SP = () => ({ c: ' ', isSpace: true });

// ── 1. 歌词分层
console.log('[pv_engine] 歌词分层（同一行拆人物前后两段）');
{
  const gs = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map(G);
  const front = P.pickTier(gs, 0.5, 'front').map(g => g.c).join('');
  const back = P.pickTier(gs, 0.5, 'behind').map(g => g.c).join('');
  ck(front === 'ABCD' && back === 'EFGH', '8 字按 0.5 切成 ABCD / EFGH', { front, back });
  const all = P.pickTier(gs, 0.5, 'front').length + P.pickTier(gs, 0.5, 'behind').length;
  ck(all === gs.length, '两段合计 = 原字形数（不会丢字）', all);

  // 空格不计数：否则英文行的切点会被空格挤歪
  const en = [G('H'), G('I'), SP(), G('T'), G('H'), SP(), G('E'), G('R')];
  const ef = P.pickTier(en, 0.5, 'front').map(g => g.c).join(',');
  ck(ef === 'H,I,T', '空格不参与计数（6 个可见字 → 前段 3 个：H,I,T）', ef);
  ck(P.pickTier(en, 0.5, 'front').every(g => !g.isSpace), '空格不会被画出来');

  // cut=null（不分层）必须原样返回：旧编排稿走的就是这条路
  ck(P.pickTier(gs, null, 'front') === gs, 'cut=null → 原样返回（旧行为零变化）');

  // 极端切点：0.02 时前段至少 1 个字，0.98 时后段至少 1 个字
  ck(P.pickTier(G0(100), 0.02, 'front').length >= 1, '0.02 时前段非空');
  ck(P.pickTier(G0(100), 0.98, 'behind').length >= 1, '0.98 时后段非空');
}
function G0(n) { const a = []; for (let i = 0; i < n; i++) a.push({ c: 'x' + i, isSpace: false }); return a; }

// ── 2. 人物镜头耦合增益
console.log('[pv_engine] 人物图层与镜头的关系');
{
  ck(P.figCamGain({}) === 1, '无声明 → 完全跟随镜头（旧行为）');
  ck(P.figCamGain({ camSpace: 'screen' }) === 0, 'camSpace=screen → 屏幕空间固定');
  ck(P.figCamGain({ camSpace: 'scene' }) === 1, 'camSpace=scene → 跟随镜头');
  ck(P.figCamGain({ camGain: 0.35 }) === 0.35, 'camGain 优先于 camSpace');
  ck(P.figCamGain({ camGain: 1.9 }) === 1 && P.figCamGain({ camGain: -3 }) === 0,
     'camGain 越界被夹住（不会把人物缩到画外）');
  ck(P.figCamGain(null) === 1, '没有人物的镜头按跟随处理（不会被调用）');
}

// ── 3. 相机复算：gain=1 必须与主循环逐项等价
console.log('[pv_engine] 相机复算（人物参与镜头运动）');
{
  const rec = () => {
    const ops = [];
    const t = {
      setTransform: (...a) => ops.push(['set', ...a]),
      translate: (...a) => ops.push(['tr', ...a]),
      rotate: (...a) => ops.push(['ro', ...a]),
      scale: (...a) => ops.push(['sc', ...a]),
      transform: (...a) => ops.push(['xf', ...a]),
    };
    return { ops, ctx: t };
  };
  const cam = { x: 40, y: -18, s: 1.12, sx: 0.98, sy: 1.03, rot: 1.4, skx: 0.6 };

  // 主循环（renderAt 里的那段）在 gain=1 时做的事，逐项抄在这里做对照
  const A = rec();
  A.ctx.setTransform(PV.SCALE || 1, 0, 0, PV.SCALE || 1, 0, 0);
  A.ctx.translate(PV.W / 2 + cam.x, PV.H / 2 + cam.y);
  A.ctx.rotate(cam.rot * Math.PI / 180);
  A.ctx.transform(1, 0, Math.tan(cam.skx * Math.PI / 180), 1, 0, 0);
  A.ctx.scale(cam.s * cam.sx, cam.s * cam.sy);
  A.ctx.translate(-PV.W / 2, -PV.H / 2);

  const B = rec();
  P.applyCam(B.ctx, cam, 1);

  const same = JSON.stringify(A.ops) === JSON.stringify(B.ops);
  ck(same, 'gain=1 与主循环的变换序列完全一致（旧成片零漂移）', { A: A.ops, B: B.ops });

  const C = rec();
  P.applyCam(C.ctx, cam, 0);
  const flat = C.ops.length === 1 && C.ops[0][0] === 'set' && Math.abs(C.ops[0][1] - (PV.SCALE || 1)) < 1e-9;
  ck(flat, 'gain=0 只复位到「设计坐标 × 输出倍率」，不叠任何相机（屏幕空间固定）', C.ops);

  const D = rec();
  P.applyCam(D.ctx, cam, 0.5);
  const sc = D.ops.find(o => o[0] === 'sc');
  ck(Math.abs(sc[1] - (1 + (cam.s * cam.sx - 1) * 0.5)) < 1e-9,
     'gain=0.5 的缩放是 1 与满幅之间的中点（视差，不是「减半再减半」）', sc);
  const tr = D.ops.find(o => o[0] === 'tr');
  ck(Math.abs(tr[1] - (PV.W / 2 + cam.x * 0.5)) < 1e-9, 'gain=0.5 的平移也按比例', tr);
}

// ── 4. 切分点闸门
console.log('[pv_engine] 切分点来源');
{
  ck(P.lyricCut({ fig: { layer: 'avoid' } }) === null, '非 split 镜头不分层');
  ck(P.lyricCut({}) === null, '无人物镜头不分层');
  ck(P.lyricCut({ fig: { layer: 'split' } }) === 0.5, '缺省切分点 0.5');
  ck(P.lyricCut({ fig: { layer: 'split', splitAt: 3 } }) <= 0.98, '越界切分点被夹住');
  ck(P.lyricCut({ fig: { layer: 'split', splitAt: 'x' } }) === 0.5, '非法值回落 0.5');
}

console.log('');
if (fails.length) { console.log('✗ ' + fails.length + ' 项失败：' + fails.join('、')); process.exit(1); }
console.log('✓ 全部通过');
