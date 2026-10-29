#!/usr/bin/env node
/* 转场契约单测：`p=0 全盖` / `p=1 一点不剩` / **最后一个 koma 样本已经走完**。
 *
 * 契约写死在 pv_trans.js 文件头（p<=0 全盖、p>=1 全让），但**没有任何东西强制它**：
 * 擦除区域写反、遮罩长到 p=1 还没盖住画幅、墨斑半径不够、装饰件（前沿亮线 /
 * 光圈环 / 白闪）到 p=1 还亮着 —— 每一种都表现为「转场走完，画面还是上一镜的
 * 一部分」，然后覆盖层被丢弃、瞬切。肉眼在四相接触表上看不出来（1/3、2/3 两格
 * 构图都是对的），只有运动才暴露。
 *
 * 所以这里用纯几何量它：B 涂成整幅蓝、A 涂成整幅红，逐 p 量两件事：
 *   · cover = 红像素占比            → A 还占多大面积
 *   · dist  = 与纯蓝的平均差 / 255  → 这一帧离「新镜」有多远（把装饰件也算进来）
 *
 * **koma 栅格是这里唯一的难点**：一张画占 1/KOMA 秒，转场只会被画
 * floor(dur·KOMA) 次，进度增量 dq = 1/(KOMA·dur)。引擎把进度除以 (1-dq)
 * 归一（见 pv_engine.js 的 crossFade），所以真正会被画出来的最后一个样本是
 *   p_vis = (1 - 2dq) / (1 - dq)
 * 断言 p_vis 上 dist 已经很小 —— 否则「最后一格还剩一截，下一帧整层丢掉」，
 * 就是切点上那一下闪。只在 p=1 上验收会全绿，但成片照样闪：这是本文件第二条
 * 存在的理由。
 *
 *   node tools/pv_trans_test.cjs
 */
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');

(async () => {
  const browser = await chromium.launch({ args: ['--allow-file-access-from-files'] });
  const page = await browser.newPage({ viewport: { width: 900, height: 600 } });
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  const url = 'file:///' + path.join(ROOT, 'renderer/player/index_pv.html').replace(/\\/g, '/') + '?res=1';
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction('window.PV && PV.TRANS && PV.TRANS.wipe', null, { timeout: 30000 });

  const KOMA = await page.evaluate(() => (window.PV && PV.TRANS && PV.TRANS.__koma) || 12);

  const rows = await page.evaluate((koma) => {
    const W = 320, H = 180;
    const A = document.createElement('canvas'); A.width = W; A.height = H;
    const ag = A.getContext('2d'); ag.fillStyle = '#FF0000'; ag.fillRect(0, 0, W, H);
    const T = document.createElement('canvas'); T.width = W; T.height = H;
    const g = T.getContext('2d');
    const BG = [0x00, 0x33, 0xCC];
    const probe = p => {
      g.setTransform(1, 0, 0, 1, 0, 0);
      g.globalAlpha = 1; g.globalCompositeOperation = 'source-over';
      g.fillStyle = 'rgb(' + BG.join(',') + ')'; g.fillRect(0, 0, W, H);
      PV.TRANS[k].draw(g, A, p, { W, H, accent: '#0000FF', seed: 12345, S: {}, dq });
      const d = g.getImageData(0, 0, W, H).data;
      let red = 0, dist = 0;
      for (let i = 0; i < d.length; i += 4) {
        if (d[i] > 140 && d[i + 1] < 110 && d[i + 2] < 110) red++;
        dist += Math.abs(d[i] - BG[0]) + Math.abs(d[i + 1] - BG[1]) + Math.abs(d[i + 2] - BG[2]);
      }
      return { red: red / (W * H), dist: dist / (W * H) / 765 };
    };
    var k, dq, j;
    const out = [];
    for (k of PV.TRANS.ORDER) {
      if (!PV.TRANS[k] || !PV.TRANS[k].draw) continue;
      const dur = PV.TRANS[k].dur || 0.4;
      dq = Math.min(0.45, (koma > 0 ? 1 / koma : 0) / dur);
      /* 引擎把进度除以 (1-dq) 归一 ⇒ 真正被画出来的样本是 j*dq/(1-dq)，
         最后一个（j*dq 越过 1-dq 之后）直接不画。这里按同一条栅格取样。 */
      const ps = [];
      for (j = 0; ; j++) {
        const p = (j * dq) / (1 - dq);
        if (p >= 1 - 1e-9) break;
        ps.push(p);
        if (ps.length > 40) break;
      }
      const seq = ps.map(probe);
      seq.push({ red: 0, dist: 0 });                    // 归一之后的最后一格：什么都不画
      const steps = [];
      for (let i = 1; i < seq.length; i++) steps.push(Math.abs(seq[i].dist - seq[i - 1].dist));
      const sorted = steps.slice().sort((a, b) => a - b);
      const med = sorted[Math.floor(sorted.length / 2)] || 0;
      const mx = Math.max.apply(null, steps);
      out.push({
        k, dur, dq, n: ps.length, p0: seq[0], p1: seq[seq.length - 1],
        lastDrawn: seq[seq.length - 2].dist,
        p50: probe(0.5), p995: probe(0.995),
        steps, med, mx, worst: steps.indexOf(mx) + 1,
      });
    }
    return out;
  }, KOMA);

  // Node 的 console.log 只认 %s/%d，不认 `%-11s` 这类宽度修饰 —— 全部用拼接。
  const pc = v => (v * 100).toFixed(1).padStart(5) + '%';
  console.log('[trans] ' + rows.length + ' 种转场 · koma=' + KOMA +
    ' · A=纯红 / B=纯蓝；dist = 这一帧离新镜有多远，steps = 每格的变化量\n');
  console.log('  ' + 'name'.padEnd(11) + 'dur  格数  p=0   末格   最大步  中位步  倍数  判定');
  let bad = 0;
  /* 判据三条，各自对应一种真实的缺陷：
     ① p=0 必须全盖 —— 否则切点那一帧就等于新镜，转场白做；
     ② p→1 必须让干净 —— 否则覆盖层被丢弃时瞬切；
     ③ **最后一个被画出来的样本**不能还留着一大截 —— 这才是「动画走完还是上一镜」
        的量化形态。koma 下转场只画 4~5 次，若曲线在末端才归零，最后一格必然
        剩一截再消失。
     注意**第一拍的幅度不判失败**：0.44s 的转场在 koma=12 下只有 5 张画，
     第一拍天然要吃掉一大块位移；这是采样率决定的，不是几何缺陷。
     `flash` 只验 ①②：闪白本来就要有一次亮度冲击，步长不可能均匀。 */
  for (const r of rows) {
    const missStart = r.p0.red < 0.55;
    const resid = r.p995.dist >= 0.02 || r.p1.dist >= 0.02;
    const endLeft = r.lastDrawn >= 0.12;
    const rest = r.steps.slice(1);                       // 去掉第一拍
    const restMax = Math.max.apply(null, rest) || 0;
    const restMed = rest.slice().sort((a, b) => a - b)[Math.floor(rest.length / 2)] || 0;
    const spike = (r.k === 'flash' || restMed <= 0.004) ? 1 : restMax / restMed;
    const ok = !missStart && !resid && (!endLeft || r.k === 'flash') && spike <= 2.6;
    if (!ok) bad++;
    const why = [];
    if (missStart) why.push('p=0 没盖住（切点会瞬切）');
    if (resid) why.push('p→1 残留 ' + (r.p995.dist * 100).toFixed(1) + '%');
    if (endLeft && r.k !== 'flash') why.push('末格还剩 ' + (r.lastDrawn * 100).toFixed(1) + '%');
    if (spike > 2.6) why.push('中段突跳 ' + spike.toFixed(1) + '×');
    console.log('  ' + String(r.k).padEnd(11) + r.dur.toFixed(2) + '  ' + String(r.n).padStart(2) +
      '  ' + pc(r.p0.red) + ' ' + pc(r.lastDrawn) + ' ' + pc(r.mx) + '  ' + pc(r.med) + ' ' +
      String(spike.toFixed(1)).padStart(5) + '×  ' + (ok ? 'OK' : '❌ ' + why.join(' / ')));
  }
  console.log(bad ? ('\n[FAIL] ' + bad + ' 种不满足契约') : '\n[OK] 全部满足契约');
  await browser.close();
  process.exit(bad ? 1 : 0);
})().catch(e => { console.error('[fatal]', e.message); process.exit(1); });
