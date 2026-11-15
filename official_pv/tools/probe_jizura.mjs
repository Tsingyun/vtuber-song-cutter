#!/usr/bin/env node
/* Read-only: JIZURA capability usage census. How many of the engine's registered
 * layouts / schemes / decorations / backgrounds does this film actually reach?
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

const out = await page.evaluate(() => {
  const J = window.J; const JZ = window.JZ;
  const registryTotals = {};
  for (const g of J.GROUP_KEYS) registryTotals[g] = Object.keys(J.registry(g) || {}).length;
  // which plans does the film actually have, and what did each plan choose?
  const perPlan = {};
  const used = {};
  for (const [name, pobj] of Object.entries(JZ.plans)) {
    const pl = pobj.plan;
    if (!pl || !pl.cuts) { perPlan[name] = null; continue; }
    const layouts = {}, schemes = {}, decors = {}, bgs = {};
    for (const c of pl.cuts) {
      if (c.layout) layouts[c.layout] = (layouts[c.layout] || 0) + 1;
      if (c.scheme != null) schemes[c.scheme] = (schemes[c.scheme] || 0) + 1;
      if (c.decor) decors[c.decor] = (decors[c.decor] || 0) + 1;
      if (c.bg) bgs[c.bg] = (bgs[c.bg] || 0) + 1;
      if (c.layout) used[`${name}:${c.layout}`] = true;
    }
    perPlan[name] = { cuts: pl.cuts.length, layouts, schemes, decors, bgs,
      enabled: pl.enabled ? Object.fromEntries(Object.entries(pl.enabled).map(([g, m]) =>
        [g, Object.values(m).filter(Boolean).length + '/' + Object.keys(m).length])) : null,
      centerFree: !!pl.centerFree, zoneW: pl.zoneW, style: pl.style, seed: pl.seed };
  }
  return { registryTotals, perPlan, groupKeys: J.GROUP_KEYS };
});

console.log('JIZURA registry sizes:', JSON.stringify(out.registryTotals));
console.log('\nplan        cuts  cf    style      layouts used');
const layoutUnion = new Set();
for (const [name, p] of Object.entries(out.perPlan)) {
  if (!p) { console.log(`${name.padEnd(11)} (missing)`); continue; }
  const ls = Object.keys(p.layouts);
  ls.forEach(l => layoutUnion.add(l));
  console.log(`${name.padEnd(11)} ${String(p.cuts).padStart(4)}  ${p.centerFree ? 'Y' : '-'}   ${String(p.style).padEnd(10)} ${ls.join(',')}`);
}
console.log('\nlayout union across all plans:', layoutUnion.size, '->', [...layoutUnion].sort().join(', '));
console.log('\nenabled counts per group (plan "main"):', JSON.stringify(out.perPlan.main && out.perPlan.main.enabled));
const allDecors = new Set(), allBgs = new Set(), allSchemes = new Set();
for (const p of Object.values(out.perPlan)) if (p) {
  Object.keys(p.decors).forEach(k => allDecors.add(k));
  Object.keys(p.bgs).forEach(k => allBgs.add(k));
  Object.keys(p.schemes).forEach(k => allSchemes.add(k));
}
console.log('decor used:', allDecors.size, '->', [...allDecors].join(','));
console.log('bg used   :', allBgs.size, '->', [...allBgs].join(','));
console.log('schemes   :', [...allSchemes].join(','));
await browser.close(); server.close();
