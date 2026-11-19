#!/usr/bin/env node
/* Read-only: is kanjiSlam actually slamming one glyph PER BEAT, or one glyph per lyric line?
 * Renders two moments inside the same shot and compares what is on screen.
 */
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { ROOT, launchBrowser, gpuArgs } from './kit_env.mjs';

const song = process.argv[2] || 'natianxiayule';
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg', '.lrc': 'text/plain; charset=utf-8', '.wav': 'audio/wav', '.ttf': 'font/ttf', '.otf': 'font/otf', '.woff2': 'font/woff2' };
const server = http.createServer((req, res) => {
  const p = path.join(ROOT, decodeURIComponent(req.url.split('?')[0]));
  if (!p.startsWith(ROOT) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { 'content-type': MIME[path.extname(p).toLowerCase()] || 'application/octet-stream' });
  fs.createReadStream(p).pipe(res);
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await launchBrowser({ headless: true, args: [...gpuArgs()] });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
await page.goto(`${origin}/engine/index.html?song=${song}`);
await page.evaluate(() => window.ready);

// pick a kanjiSlam shot and sample it at several times inside its own span
const info = await page.evaluate(() => {
  const sh = Z.SHOTS.find(s => s.scene === 'kanjiSlam' && (s.t1 - s.t0) > 3);
  if (!sh) return null;
  const beatsIn = Z.clock.beats.filter(x => x >= sh.t0 - 0.045 && x < sh.t1 - 0.045);
  return { id: sh.id, t0: sh.t0, t1: sh.t1, dur: sh.t1 - sh.t0, glyphs: sh.args.glyphs || null,
    beatsIn, clockBeatsTotal: Z.clock.beats.length, bpm: 88 };
});
console.log('shot:', JSON.stringify(info));

const outDir = path.join(ROOT, 'render', '_probe_beats');
fs.mkdirSync(outDir, { recursive: true });
const times = [info.t0 + 0.4, info.t0 + info.dur * 0.5, info.t1 - 0.4];
for (const t of times) {
  const data = await page.evaluate(async (tt) => {
    await window.renderFrame(tt);
    const c = document.getElementById('out');
    return c.toDataURL('image/png');
  }, +t.toFixed(2));
  fs.writeFileSync(path.join(outDir, `slam_${t.toFixed(2)}.png`), Buffer.from(data.split(',')[1], 'base64'));
  console.log('rendered t=' + t.toFixed(2));
}
// how many clock beats land inside a typical chorus shot
const dense = await page.evaluate(() => {
  const rows = Z.SHOTS.filter(s => s.scene === 'kanjiSlam').map(s => ({
    id: s.id, dur: +(s.t1 - s.t0).toFixed(2),
    beats: Z.clock.beats.filter(x => x >= s.t0 - 0.045 && x < s.t1 - 0.045).length,
    glyphs: (s.args.glyphs || []).length,
  }));
  return { rows, totalClockBeats: Z.clock.beats.length };
});
console.log('clock beats total =', dense.totalClockBeats, '(should be ~322 for 219.8s @88bpm)');
for (const r of dense.rows) console.log(`  ${r.id} dur=${r.dur}s beatsInShot=${r.beats} glyphs=${r.glyphs}`);
await browser.close(); server.close();
