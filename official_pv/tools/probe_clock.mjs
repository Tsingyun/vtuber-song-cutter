#!/usr/bin/env node
/* Read-only: what does Z.clock actually report? The 3rd makeClock arg is a BEAT INDEX in
 * upstream docs (0..3), but prepare.py writes a TIME (1.6761 s). Check beatNo / bar / inBar.
 */
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { ROOT, launchBrowser, gpuArgs } from './kit_env.mjs';

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg', '.lrc': 'text/plain; charset=utf-8', '.wav': 'audio/wav', '.ttf': 'font/ttf' };
const server = http.createServer((req, res) => {
  const p = path.join(ROOT, decodeURIComponent(req.url.split('?')[0]));
  if (!p.startsWith(ROOT) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { 'content-type': MIME[path.extname(p).toLowerCase()] || 'application/octet-stream' });
  fs.createReadStream(p).pipe(res);
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await launchBrowser({ headless: true, args: [...gpuArgs()] });
const page = await browser.newPage({ viewport: { width: 800, height: 450 } });
await page.goto(`${origin}/engine/index.html?song=natianxiayule`);
await page.evaluate(() => window.ready);
const r = await page.evaluate(() => {
  const C = Z.clock;
  const rows = [];
  for (const t of [10, 30, 50, 80, 120, 180]) {
    rows.push({ t, idx: C.idx(t), beatNo: +C.beat(t).toFixed(3), bar: C.bar(t), inBar: +C.inBar(t).toFixed(3),
      downPulse: +C.downPulse(t).toFixed(4), pulse: +C.pulse(t, 10).toFixed(4), barPhase: +C.barPhase(t).toFixed(3) });
  }
  // does inBar ever equal exactly 0 over the whole film?
  let hits = 0, samples = 0;
  for (let t = 0; t < 219.8; t += 0.1) { samples++; if (C.inBar(t) === 0) hits++; }
  return { rows, hits, samples, spb: C.spb, nBeats: C.beats.length };
});
console.log('spb=' + r.spb.toFixed(4) + 's  nBeats=' + r.nBeats);
console.table(r.rows);
console.log(`inBar(t) === 0 over ${r.samples} samples: ${r.hits} times  (upstream: should be every 4th beat)`);
await browser.close(); server.close();
