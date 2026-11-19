#!/usr/bin/env node
/* 文字 PV 抽帧预览：不编码视频，只把指定时间点的画面落成 PNG + 一张接触表。
 *   node tools/pv_stills.cjs --sb _tmp/pv/sb.json --live _tmp/live45/live.json \
 *        --times 2,10,20,38 --out _tmp/pv/stills [--res 1]
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const argOf = (k, d) => { const i = process.argv.indexOf('--' + k); return i > 0 ? process.argv[i + 1] : d; };

(async () => {
  const sbPath = path.resolve(ROOT, argOf('sb'));
  const outDir = path.resolve(ROOT, argOf('out', '_tmp/pv/stills'));
  const res = Number(argOf('res', '1'));
  const times = String(argOf('times', '2,10,20,38')).split(',').map(Number);
  fs.mkdirSync(outDir, { recursive: true });

  const sb = JSON.parse(fs.readFileSync(sbPath, 'utf8'));
  let live = null;
  const livePath = argOf('live', '');
  if (livePath && fs.existsSync(path.resolve(ROOT, livePath))) {
    const lj = JSON.parse(fs.readFileSync(path.resolve(ROOT, livePath), 'utf8'));
    live = {};
    /* 序列名不再是固定的 head/body/wide：LIVE_SPANS 是六段各一条序列（s1..s6）。
       写死三个名字时，传入新格式的 live.json 会得到「一个序列都没装」，
       而且不报错 —— 表现为「六处该有人的地方全是空版面」。 */
    for (const k of Object.keys(lj)) {
      if (k === 'face' || k === 'cut_start' || !lj[k] || typeof lj[k] !== 'object') continue;
      live[k] = Object.assign({}, lj[k], { dir: path.resolve(ROOT, lj[k].dir).replace(/\\/g, '/') });
    }
    if (lj.face) live.face = lj.face;
  }

  /* GPU 首触 ±1 抖动由 __pvShot 内部的「首渲丢弃」双渲补丁兜住（RULES 76）；
     4K 长片必须 GPU（CPU 光栅化 0.4fps），补丁与 GPU 成对使用。 */
  const browser = await chromium.launch({ args: ['--allow-file-access-from-files', '--enable-gpu'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  page.on('console', m => { const s = m.text(); if (m.type() === 'error' || s.startsWith('[pv]')) console.log('  ' + s); });

  const url = 'file:///' + path.join(ROOT, 'renderer/player/index_pv.html').replace(/\\/g, '/') + '?res=' + res;
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction('window.PV && PV.renderAt', null, { timeout: 30000 });

  const cfg = { storyboard: sb, live, dur: sb.meta.dur, t: 0 };
  const info0 = await page.evaluate(c => window.__pvShot(c), Object.assign({}, cfg, { t: -1 })).catch(e => ({ err: String(e) }));
  if (info0 && info0.err) { console.error('[setup]', info0.err); process.exit(1); }
  const info = await page.evaluate(() => window.__pvInfo());
  console.log('[info] %s  shots=%d glyphs=%d  fonts=%d  total=%d',
    (1920 * info.scale) + 'x' + (1080 * info.scale), info.stats.shots, info.stats.glyphs, info.fonts, info.total);

  const files = [];
  for (const t of times) {
    const t0 = Date.now();
    const durl = await page.evaluate(c2 => window.__pvShot(c2), Object.assign({}, cfg, { t }));
    const b = Buffer.from(durl.split(',')[1], 'base64');
    const f = path.join(outDir, `pv_${String(t).replace(/\./g, '_')}.png`);
    fs.writeFileSync(f, b);
    files.push(f);
    console.log('[shot] t=' + String(t).padStart(6) + '  ' +
      String(Date.now() - t0).padStart(5) + 'ms  ' +
      String(Math.round(b.length / 1024)).padStart(5) + 'KB  ' + path.basename(f));
  }

  // 确定性：同 t 两次渲染必须字节一致
  const a1 = await page.evaluate(c2 => window.__pvShot(c2), Object.assign({}, cfg, { t: times[0] }));
  const a2 = await page.evaluate(c2 => window.__pvShot(c2), Object.assign({}, cfg, { t: times[0] }));
  console.log('[determinism] t=' + times[0] + ' 两次渲染 ' + (a1 === a2 ? '字节一致 ✅' : '不一致 ❌'));

  await browser.close();
})().catch(e => { console.error('[fatal]', e.message); process.exit(1); });
