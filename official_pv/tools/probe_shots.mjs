#!/usr/bin/env node
/* Read-only inspection: dump every shot's real timeline metadata so we can measure
 * duration distribution / plan assignment / section structure without guessing.
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
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
await page.goto(`${origin}/engine/index.html?song=${song}`);
await page.evaluate(() => window.ready);

const data = await page.evaluate(() => {
  const rows = Z.SHOTS.map(s => ({
    id: s.id, t0: +s.t0.toFixed(2), t1: +s.t1.toFixed(2), dur: +(s.t1 - s.t0).toFixed(2),
    scene: s.scene,
    plan: s.plan || (typeof s.lyric === 'object' && s.lyric ? (s.lyric.plan || '?') : String(s.lyric)),
    trans: s.trans ? s.trans.type : '',
    chars: s.args && s.args.chars ? s.args.chars.length : 0,
    boil: s.post && typeof s.post === 'object' ? s.post.boil : null,
    postFn: typeof s.post === 'function',
  }));
  return { rows, acts: (window.__cutAudit && window.__cutAudit.acts) || null,
    total: Z.SHOTS[Z.SHOTS.length - 1].t1 };
});
console.log(`shots=${data.rows.length} total=${data.total.toFixed(2)}s`);
console.log('id     t0      dur   scene        plan        trans    chars boil');
for (const r of data.rows) {
  console.log(`${r.id.padEnd(6)} ${String(r.t0).padStart(7)} ${String(r.dur).padStart(5)}  ${r.scene.padEnd(12)} ${String(r.plan).padEnd(11)} ${r.trans.padEnd(8)} ${String(r.chars || '').padEnd(5)} ${r.boil}`);
}
const durs = data.rows.map(r => r.dur).sort((a, b) => a - b);
const q = p => durs[Math.min(durs.length - 1, Math.floor(p * durs.length))];
console.log(`\ndur  min=${durs[0]} p25=${q(.25)} median=${q(.5)} p75=${q(.75)} p95=${q(.95)} max=${durs[durs.length - 1]}`);
console.log('>7s:', data.rows.filter(r => r.dur > 7).map(r => `${r.id}:${r.dur}s(${r.scene})`).join(' ') || 'none');
console.log('<0.9s:', data.rows.filter(r => r.dur < 0.9).map(r => `${r.id}:${r.dur}s`).join(' ') || 'none');
await browser.close(); server.close();
