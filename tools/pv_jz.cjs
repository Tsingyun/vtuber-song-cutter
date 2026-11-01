#!/usr/bin/env node
/* JIZURA 帧序列层：离线预渲 PNG 序列，交给 pv_direct / pv_engine 当一层贴图合成。
 *
 *   node tools/pv_jz.cjs --timing <timing.json> --out _tmp/jz/j1 \
 *        --start 90 --dur 4 --fps 6 --bpm 138.46 --res 1080 --style noir [--accent '#5A5897']
 *
 * 为什么是「预渲帧序列」而不是运行时直画 JIZURA：
 *   JIZURA 每帧要跑三趟色散 + 纸纹，4K 下逐帧直画会把渲染时间翻几倍；而帧序列在我们
 *   这边只是一层贴图，渲染热路径零新增成本，确定性又由「同一批 PNG」直接保证。
 *
 * 关键一点：**歌词时间轴用我们自己的**。给 JIZURA 的 LRC 由 timing.json 生成、
 * 并按窗口起点整体平移，不让它自己检测 —— 否则「加入动画导致歌词整体延迟」这条
 * 禁止项一定会被踩到（JIZURA 有它自己的前奏/间奏规划）。
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const argOf = (k, d) => { const i = process.argv.indexOf('--' + k); return i > 0 ? process.argv[i + 1] : d; };

const timingPath = argOf('timing', '');
const outDir = path.resolve(ROOT, argOf('out', '_tmp/jz/j1'));
const start = Number(argOf('start', '0'));
const dur = Number(argOf('dur', '4'));
const fps = Number(argOf('fps', '6'));
const bpm = Number(argOf('bpm', '0'));
const res = Number(argOf('res', '1080'));
const style = argOf('style', 'noir');
const accent = argOf('accent', '');
const seed = Number(argOf('seed', '20261027'));

if (!timingPath) { console.error('缺 --timing'); process.exit(2); }
const tdoc = JSON.parse(fs.readFileSync(path.resolve(ROOT, timingPath), 'utf8'));

/* ---- 从 timing.json 生成窗口内的 LRC（时间整体减 start） ---- */
const mmss = s => {
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return String(m).padStart(2, '0') + ':' + sec.toFixed(2).padStart(5, '0');
};
const lines = (tdoc.lines || []).filter(l => l.t1 > start + 0.05 && l.t0 < start + dur - 0.05);
if (!lines.length) { console.error('窗口内没有歌词行'); process.exit(3); }
const lrc = lines.map(l => {
  const t = Math.max(0, (l.t0 - start));
  return '[' + mmss(t) + ']' + String(l.text || '').trim();
}).join('\n');

/* 拍点：由 bpm 推等间距网格（bpm 是实测的，网格是推导的 —— 这一点写进输出 JSON 的 note）。
   不传 beats 会让 JIZURA 用自己的一致性检测，和我们的时间轴未必同步。 */
const beats = [];
if (bpm > 0) {
  const b = 60 / bpm;
  for (let t = 0; t <= dur + 1e-6; t += b) beats.push(Number(t.toFixed(4)));
}

(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch({ args: ['--allow-file-access-from-files', '--enable-gpu'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errs = [];
  page.on('pageerror', e => errs.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errs.push('[console] ' + m.text()); });

  await page.goto('file:///' + path.join(ROOT, 'renderer/pv/probe.html').replace(/\\/g, '/'));
  await page.waitForFunction('typeof window.__init === "function"', { timeout: 30000 });

  const sum = await page.evaluate(async cfg => window.__init(cfg), {
    dur, beats, bpm, lrc, style, res, seed,
    accent: accent || undefined,
    title: (tdoc.meta && tdoc.meta.title) || '', artist: (tdoc.meta && tdoc.meta.artist) || '',
    lang: 'auto',
  });

  const n = Math.max(1, Math.round(dur * fps));
  const t0 = Date.now();
  for (let i = 0; i < n; i++) {
    const t = i / fps;
    const url = await page.evaluate(tt => window.__shot(tt, 'transparent'), t);
    if (!url || url.indexOf('data:image/png;base64,') !== 0) {
      console.error('[jz] 第 %d 帧没拿到 PNG', i); process.exit(4);
    }
    const buf = Buffer.from(url.slice(22), 'base64');
    fs.writeFileSync(path.join(outDir, 'j_' + String(i + 1).padStart(5, '0') + '.png'), buf);
  }
  await browser.close();

  const meta = {
    kind: 'jizura-seq', dir: outDir.replace(/\\/g, '/'), n, fps,
    w: sum.W || res, h: sum.H || res, pattern: 'j_%05d.png',
    mode: 'transparent',
    source: { timing: timingPath, start, dur, style, bpm, res, seed, lines: lines.length },
    plan: sum,
    note: 'beats 由 bpm 推等间距网格（非 onset 检测）；歌词时间来自 timing.json，窗口内已平移',
    ms: Date.now() - t0,
  };
  fs.writeFileSync(path.join(outDir, 'jz.json'), JSON.stringify(meta, null, 1));
  console.log('[jz] %d 帧 %.1fs  %sx%s  style=%s lines=%d cuts=%d  %.0fms/帧',
    n, dur, meta.w, meta.h, sum.style, sum.lines, sum.cuts, meta.ms / Math.max(1, n));
  if (sum.missingFonts && sum.missingFonts.length) console.log('[jz] ! 缺字体 %s', sum.missingFonts.join(','));
  if (errs.length) console.log('[jz] ! 页面错误 %d 条：%s', errs.length, errs.slice(0, 3).join(' | '));
})();
