#!/usr/bin/env node
/* audit_official.mjs -- RUNTIME proof that the upstream engine is actually in use.
 *
 * This is the check that was missing last time. It does not grep source code: it opens
 * the real page in a headless GPU browser, waits for window.ready, walks Z.SHOTS,
 * renders one frame per distinct scene, and reads the engine's own registries.
 * Anything short of "official scenes really executed" fails the build.
 *
 *   node tools/audit_official.mjs --song faruxue
 */
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { ROOT, cliArg, launchBrowser, gpuArgs } from './kit_env.mjs';

const song = cliArg('song', 'faruxue');
const url = `engine/index.html?song=${encodeURIComponent(song)}`;
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp', '.ttf': 'font/ttf', '.otf': 'font/otf', '.woff2': 'font/woff2', '.lrc': 'text/plain; charset=utf-8', '.wav': 'audio/wav' };
const server = http.createServer((req, res) => {
  const p = path.join(ROOT, decodeURIComponent(req.url.split('?')[0]));
  if (!p.startsWith(ROOT) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { 'content-type': MIME[path.extname(p).toLowerCase()] || 'application/octet-stream' });
  fs.createReadStream(p).pipe(res);
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await launchBrowser({ headless: true, args: [...gpuArgs(), '--enable-gpu-rasterization'] });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
const errors = [];
page.on('pageerror', e => errors.push('pageerror: ' + String(e).slice(0, 300)));
page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text().slice(0, 300)); });

try {
  await page.goto(`${origin}/${url}`);
  const meta = await page.evaluate(() => window.ready);
  const rep = await page.evaluate(async () => {
    const A = window.__cutAudit || {};
    const scripts = [...document.scripts].map(s => s.src).filter(Boolean).map(u => u.split('/').pop());
    const officialFiles = ['scenes.js', 'intro.js', 'typo.js', 'glass.js', 'dark.js', 'night.js', 'memory.js', 'meta.js'];
    const registered = Object.keys(Z.SCENES);
    const missing = [...new Set(Z.SHOTS.filter(s => !Z.SCENES[s.scene]).map(s => s.scene))];
    const used = [...new Set(Z.SHOTS.map(s => s.scene))];
    const fxTypes = [...new Set(Z.SHOTS.flatMap(s => [...(s.args && s.args.fx || []), ...(s.args && s.args.fxBack || [])]).map(f => f && f.type).filter(Boolean))];
    const transTypes = [...new Set(Z.SHOTS.map(s => s.trans && s.trans.type).filter(Boolean))];
    const lutNames = [...new Set(Z.SHOTS.map(s => (typeof s.post === 'object' && s.post && s.post.lutA) || null).filter(Boolean))];
    const badLut = lutNames.filter(n => Z.LUT[n] === undefined);
    // EXECUTE every distinct scene at its own shot midpoint: a scene that is loaded but never
    // drawn still counts as "not used"; a broken scene surfaces as a pageerror here.
    const drawn = [];
    for (const name of used) {
      const sh = Z.SHOTS.find(s => s.scene === name);
      if (!sh) continue;
      try { await window.renderFrame((sh.t0 + sh.t1) / 2); drawn.push(name); }
      catch (e) { drawn.push(name + '!ERR:' + String(e).slice(0, 120)); }
    }
    // FULL-TIMELINE smoke render. One frame per shot is not enough: a scene can be
    // fine at its first midpoint and explode later (a palette key used by only some
    // fx factories). Render EVERY shot midpoint and collect any exception.
    const smoke = { tried: 0, errors: [] };
    for (const sh of Z.SHOTS) {
      const t = (sh.t0 + sh.t1) / 2;
      try { await window.renderFrame(t); smoke.tried++; }
      catch (e) { smoke.errors.push(`${sh.id}@${t.toFixed(2)} ${sh.scene}: ${String(e).slice(0, 160)}`); }
    }
    return {
      renderer: window.__zankoMeta.renderer, shotsMeta: window.__zankoMeta.shots,
      scripts, officialFilesLoaded: officialFiles.filter(f => scripts.includes(f)),
      registeredScenes: registered.length, registered: registered.sort(),
      officialScenesUsed: A.officialScenesUsed || [], sceneCounts: A.scenes || {},
      missingScenes: missing, drawn, smoke,
      fxTypes, fxTypeCounts: A.fxTypes || {}, customFactories: A.customFactories || {},
      transTypes, transitions: A.transitions || {},
      jizuraPlans: A.jizuraPlans || [], planCounts: A.plans || {},
      centreFreePlans: A.centreFreePlans || [], centreReserveOutsideLive: A.centreReserveOutsideLive,
      oncamShots: A.oncamShots, liveShots: A.liveShots,
      shotsWithPost: A.shotsWithPost, shotsWithTrans: A.shotsWithTrans, lyricNone: A.lyricNone,
      liveShots: A.liveShots, liveSec: A.liveSec, livePct: A.livePct,
      shots: A.shots, duration: A.duration,
      looks: Object.keys(Z.LOOKS || {}), hasGlobalFX: typeof Z.globalFX === 'function',
      lutNames, badLut,
      warpViolations: (Z.LIVEWIN && Z.LIVEWIN.audit && Z.LIVEWIN.audit.violations) || 0,
      warpCalls: (Z.LIVEWIN && Z.LIVEWIN.audit && Z.LIVEWIN.audit.calls) || 0,
    };
  });

  const checks = [
    ['8 official scene files loaded', rep.officialFilesLoaded.length === 8, rep.officialFilesLoaded.join(', ')],
    ['>=24 official scenes registered', rep.registeredScenes >= 24, String(rep.registeredScenes)],
    ['no missing scene names', rep.missingScenes.length === 0, rep.missingScenes.join(',') || 'none'],
    ['>=8 official scenes actually used', rep.officialScenesUsed.length >= 8, rep.officialScenesUsed.join(', ')],
    ['every distinct scene executed', !rep.drawn.some(d => d.includes('!ERR')), rep.drawn.join(', ')],
    ['every shot renders without exception', rep.smoke.errors.length === 0,
      `${rep.smoke.tried}/${rep.shots} frames` + (rep.smoke.errors.length ? ' :: ' + rep.smoke.errors.slice(0, 3).join(' | ') : '')],
    ['>=6 built-in fx types', rep.fxTypes.length >= 6, rep.fxTypes.join(', ')],
    ['>=4 custom fx factories', Object.keys(rep.customFactories).length >= 4, Object.keys(rep.customFactories).join(', ')],
    ['>=5 transition types', rep.transTypes.length >= 5, rep.transTypes.join(', ')],
    ['>=4 JIZURA plans', rep.jizuraPlans.length >= 4, rep.jizuraPlans.join(', ')],
    ['centre-reserve plans only on live inserts', rep.centreFreePlans.length >= 1 && rep.centreReserveOutsideLive === 0 && rep.oncamShots === rep.liveShots,
      `centerFree=[${rep.centreFreePlans.join(',')}] outsideLive=${rep.centreReserveOutsideLive} oncam=${rep.oncamShots}/${rep.liveShots} live shots`],
    ['every shot has post', rep.shotsWithPost === rep.shots, `${rep.shotsWithPost}/${rep.shots}`],
    ['Z.LOOKS + Z.globalFX present', rep.looks.length >= 5 && rep.hasGlobalFX, `${rep.looks.length} looks`],
    ['all lutA names valid', rep.badLut.length === 0, rep.badLut.join(',') || 'none'],
    ['live footage <= 15%', rep.livePct <= 15, `${rep.livePct}%`],
    ['no warp violations', rep.warpViolations === 0, `violations=${rep.warpViolations}, drawImage calls=${rep.warpCalls}`],
    ['no page errors', errors.length === 0, errors.slice(0, 3).join(' | ') || 'none'],
    ['real GPU renderer', !/swiftshader|warp|llvmpipe/i.test(rep.renderer || ''), rep.renderer || 'unknown'],
  ];
  console.log(`\n=== OFFICIAL-CAPABILITY AUDIT  ·  ${song} ===`);
  let bad = 0;
  for (const [name, ok, detail] of checks) { console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(38)} ${detail}`); if (!ok) bad++; }
  console.log(`\nscenes used: ${JSON.stringify(rep.sceneCounts)}`);
  console.log(`fx types   : ${JSON.stringify(rep.fxTypeCounts)}`);
  console.log(`custom fx  : ${JSON.stringify(rep.customFactories)}`);
  console.log(`transitions: ${JSON.stringify(rep.transitions)}`);
  console.log(`plans      : ${JSON.stringify(rep.planCounts)}`);
  const out = path.resolve(ROOT, 'render', `audit_${song}.json`);
  fs.writeFileSync(out, JSON.stringify({ song, meta, report: rep, checks: checks.map(([n, ok, d]) => ({ name: n, pass: ok, detail: d })), errors }, null, 2));
  console.log(`\nreport -> ${out}`);
  if (bad) { console.error(`\n${bad} CHECK(S) FAILED -- refusing to render.`); process.exitCode = 1; }
} finally {
  await browser.close();
  await new Promise(r => server.close(r));
}
