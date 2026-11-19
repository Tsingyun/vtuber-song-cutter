/* 逐样本量「这一帧离新镜有多远」（dist），打印每个样本的变化量与其均匀性。
   为什么不用「红像素占比」：alpha 类转场（dissolve/zoom/slice）在阈值化下只有
   跨过 0.5 的那一步才跳变，量出来的「一格吃掉 100%」是量具问题，不是画面问题。 */
const path = require('path'); const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ args: ['--allow-file-access-from-files'] });
  const p = await b.newPage({ viewport: { width: 900, height: 600 } });
  await p.goto('file:///' + path.join(process.cwd(), 'renderer/player/index_pv.html').replace(/\\/g, '/') + '?res=1', { waitUntil: 'load' });
  await p.waitForFunction('window.PV && PV.TRANS && PV.TRANS.wipe');
  const r = await p.evaluate(() => {
    const W = 320, H = 180, KOMA = 12;
    const A = document.createElement('canvas'); A.width = W; A.height = H;
    const ag = A.getContext('2d'); ag.fillStyle = '#FF0000'; ag.fillRect(0, 0, W, H);
    const T = document.createElement('canvas'); T.width = W; T.height = H; const g = T.getContext('2d');
    const BG = [0x00, 0x33, 0xCC];
    const dist = (k, pr, dq) => {
      g.setTransform(1, 0, 0, 1, 0, 0); g.globalAlpha = 1; g.globalCompositeOperation = 'source-over';
      g.fillStyle = 'rgb(' + BG.join(',') + ')'; g.fillRect(0, 0, W, H);
      PV.TRANS[k].draw(g, A, pr, { W, H, accent: '#0000FF', seed: 12345, S: {}, dq, dir: 'L' });
      const d = g.getImageData(0, 0, W, H).data;
      let s = 0;
      for (let i = 0; i < d.length; i += 4)
        s += Math.abs(d[i] - BG[0]) + Math.abs(d[i + 1] - BG[1]) + Math.abs(d[i + 2] - BG[2]);
      return s / (W * H) / 765;
    };
    const out = {};
    for (const k of PV.TRANS.ORDER) {
      if (!PV.TRANS[k] || !PV.TRANS[k].draw) continue;
      const dur = PV.TRANS[k].dur || 0.4;
      const dq = Math.min(0.45, (1 / KOMA) / dur);
      const arr = []; let last = null;
      for (let p0 = 0; p0 <= 1.0001; p0 += dq) {
        const pr = Math.min(1, p0 / (1 - dq));
        const c = dist(k, pr, dq);
        arr.push({ p: +pr.toFixed(3), d: +c.toFixed(4), step: last == null ? 0 : Math.abs(c - last) });
        last = c;
      }
      out[k] = arr;
    }
    return out;
  });
  for (const k in r) {
    const steps = r[k].slice(1).map(x => x.step);
    const mx = Math.max(...steps), avg = steps.reduce((a, c) => a + c, 0) / steps.length;
    console.log(k.padEnd(9) + ' dist: ' + r[k].map(x => x.d.toFixed(2)).join(' ') +
      '  | 每格: ' + steps.map(v => v.toFixed(2)).join(' ') +
      '  最大/均 ' + (mx / Math.max(avg, 1e-6)).toFixed(2));
  }
  await b.close();
})();
