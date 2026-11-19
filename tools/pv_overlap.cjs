#!/usr/bin/env node
/* 画框 × 歌词 的矩形相交探针（设计坐标 1920×1080）。
 *
 * 用途：人物入画面之后，「字被框压住」是唯一一类**看不出来就废片**的错误 ——
 * 缩略图上圆窗压住半行字，读起来像排版随便，只有把两个 bbox 算出来才说得清。
 * 这里读的是引擎**自己解算后**的构图（window.__pvShots），不重算，避免漂移。
 *
 *   node tools/pv_overlap.cjs --sb _tmp/pv/sb.json --live _tmp/pv/live_seq/live.json
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const argOf = (k, d) => { const i = process.argv.indexOf('--' + k); return i > 0 ? process.argv[i + 1] : d; };

(async () => {
  const sbPath = path.resolve(ROOT, argOf('sb'));
  const livePath = argOf('live', '');
  const sb = JSON.parse(fs.readFileSync(sbPath, 'utf8'));
  let live = null;
  if (livePath && fs.existsSync(path.resolve(ROOT, livePath))) {
    const lj = JSON.parse(fs.readFileSync(path.resolve(ROOT, livePath), 'utf8'));
    live = {};
    /* 序列名由 live.json 决定（LIVE_SPANS 的 s1..s6），不再写死 head/body/wide。
       写死时新格式一条都装不进去，画面上「六处该有人的地方全空」而不报错。 */
    for (const k of Object.keys(lj)) {
      if (k === 'face' || k === 'cut_start' || !lj[k] || typeof lj[k] !== 'object') continue;
      live[k] = Object.assign({}, lj[k], { dir: path.resolve(ROOT, lj[k].dir).replace(/\\/g, '/') });
    }
    if (lj.face) live.face = lj.face;
  }

  const browser = await chromium.launch({ args: ['--allow-file-access-from-files', '--enable-gpu'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  const url = 'file:///' + path.join(ROOT, 'renderer/player/index_pv.html').replace(/\\/g, '/') + '?res=1';
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction('window.PV && PV.renderAt', null, { timeout: 30000 });

  const cfg = { storyboard: sb, live, dur: sb.meta.dur, t: 0 };
  const info = await page.evaluate(c => window.__pvShot(c), Object.assign({}, cfg, { t: -1 }));
  if (info && info.err) { console.error('[setup]', info.err); process.exit(1); }

  /* setup 的暖机已经逐镜渲过一次中点 ⇒ _figBox / _lay.bbox 都是引擎自己解算的，
     这里直接读，不重算（重算必然和真渲染漂移）。 */
  const shots = await page.evaluate(() => (window.__pvShots || []).map(s => ({
    i: s.i, line: s.line, t0: s.t0, t1: s.t1, text: s.text || '',
    mode: s.fig ? s.fig.mode : null,
    bbox: s._lay ? s._lay.bbox : null,
    box: s._figBox || null,
  })));

  const inter = (a, b) => {
    const x0 = Math.max(a[0], b[0]), y0 = Math.max(a[1], b[1]);
    const x1 = Math.min(a[2], b[2]), y1 = Math.min(a[3], b[3]);
    return Math.max(0, x1 - x0) * Math.max(0, y1 - y0);
  };
  const area = a => Math.max(0, a[2] - a[0]) * Math.max(0, a[3] - a[1]);

  let bad = 0, worst = 0;
  for (const s of shots) {
    if (!s.mode || !s.box || !s.bbox) continue;
    const b = s.box;
    /* wash 是**满幅淡底**：几何上必然覆盖文字，判据只能是「淡到字压得住」，
       那要人眼看帧（不透明度 0.34 + 纸色回提 0.26），不归这里的矩形判据管。 */
    if (s.mode === 'wash') { console.log(`wash    line ${String(s.line).padStart(2)}  满幅淡底（几何必相交，靠不透明度）`); continue; }
    const rect = b.kind === 'disc'
      ? [b.x - b.r, b.y - b.r, b.x + b.r, b.y + b.r]
      : [b.x, b.y, b.x + b.w, b.y + b.h];
    const ov = inter(rect, s.bbox);
    const pct = area(s.bbox) > 0 ? 100 * ov / area(s.bbox) : 0;
    if (pct > worst) worst = pct;
    const flag = pct > 0.5 ? 'OVERLAP' : 'ok';
    if (pct > 0.5) bad++;
    console.log(`${flag.padEnd(7)} line ${String(s.line).padStart(2)}  ${String(s.mode).padEnd(7)}` +
      ` ${pct.toFixed(1).padStart(5)}%  box=[${rect.map(Math.round).join(',')}]  text=[${s.bbox.map(Math.round).join(',')}]`);
  }
  console.log(`\n[overlap] 图字最大相交 ${worst.toFixed(1)}%  超阈镜头 ${bad} 个`);
  await browser.close();
  process.exit(bad ? 1 : 0);
})();
