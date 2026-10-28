#!/usr/bin/env node
/* JIZURA 探针：判断「能不能用」，不是「好不好看」。
 *   node tools/pv_probe.cjs --lrc <file> --beats <json> --times 20,44,78 --out _tmp/probe [--style noir]
 * 输出若干 PNG + cuts.json，用来决定后续是否真的把 JIZURA 作为歌词动效层。
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const argOf = (k, d) => { const i = process.argv.indexOf('--' + k); return i > 0 ? process.argv[i + 1] : d; };
const flag = k => process.argv.includes('--' + k);

(async () => {
  const lrcPath = argOf('lrc');
  const beatsPath = argOf('beats', '_tmp/pv_beats.json');
  const outDir = path.resolve(ROOT, argOf('out', '_tmp/probe'));
  const times = String(argOf('times', '20,44,78')).split(',').map(Number);
  const style = argOf('style', 'noir');
  const lang = argOf('lang', 'auto');
  const mode = argOf('mode', 'transparent');
  fs.mkdirSync(outDir, { recursive: true });

  const lrc = fs.readFileSync(lrcPath, 'utf8').replace(/^\uFEFF/, '');
  const bj = JSON.parse(fs.readFileSync(path.resolve(ROOT, beatsPath), 'utf8'));

  const browser = await chromium.launch({
    args: ['--allow-file-access-from-files', '--enable-gpu', '--js-flags=--expose-gc'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  page.on('console', m => { if (m.type() === 'error') console.error('[console.error]', m.text()); });
  const url = 'file:///' + path.join(ROOT, 'renderer/pv/probe.html').replace(/\\/g, '/');
  await page.goto(url, { waitUntil: 'load' });

  const t0 = Date.now();
  const sum = await page.evaluate(c => window.__init(c), {
    lrc, beats: bj.beats, bpm: bj.bpm, dur: 232.17,
    style, lang, title: '', artist: '',
  });
  console.log('[init] %s  style=%s lang=%s lines=%d cuts=%d beats=%d  %dms',
    sum.W + 'x' + sum.H, sum.style, sum.lang, sum.lines, sum.cuts, sum.beats, Date.now() - t0);
  console.log('[fonts] %s', JSON.stringify(sum.fonts));
  if (sum.missingFonts.length) console.warn('[fonts] 缺失 %s', JSON.stringify(sum.missingFonts));

  const cuts = await page.evaluate(() => window.__cuts());
  fs.writeFileSync(path.join(outDir, 'cuts.json'), JSON.stringify(cuts, null, 1), 'utf8');
  const byLayout = {};
  for (const c of cuts) byLayout[c.layout] = (byLayout[c.layout] || 0) + 1;
  console.log('[cuts] layout 分布 %s', JSON.stringify(byLayout));

  for (const t of times) {
    const ts = Date.now();
    const data = await page.evaluate(([tt, m]) => window.__shot(tt, m), [t, mode]);
    const b = Buffer.from(data.split(',')[1], 'base64');
    const f = path.join(outDir, `jz_${String(t).replace('.', '_')}.png`);
    fs.writeFileSync(f, b);
    console.log('[shot] t=%6.2fs  %5.0fms  %6.0fKB  %s', t, Date.now() - ts, b.length / 1024, path.basename(f));
  }

  // 确定性复核：同一 t 画两次，逐字节比较
  const a1 = await page.evaluate(() => window.__shot(44.0, 'transparent'));
  const a2 = await page.evaluate(() => window.__shot(44.0, 'transparent'));
  console.log('[determinism] t=44.0 两次渲染 %s', a1 === a2 ? '一致 ✅' : '不一致 ❌');

  await browser.close();
})().catch(e => { console.error('[fatal]', e.message); process.exit(1); });
