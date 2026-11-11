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

// the differentiation core, loaded into node exactly as the page loads it (both files are DOM-free)
const win = {};
globalThis.window = win;
const ev = f => new Function('window', fs.readFileSync(path.join(ROOT, f), 'utf8'))(win);
ev('engine/core/util.js');
ev('engine/direction.js');
const DIR = win.DIR;
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
    // character asset probe: the single illustration is user-supplied, so verify it can
    // actually be drawn as a sprite (alpha + resolution), not just that the file exists.
    let charProbe = null;
    if (A.charImg) {
      try {
        const img = Z.imgSync(A.charImg) || Z.loaded.get(A.charImg);
        if (!img) charProbe = { err: 'not preloaded' };
        else {
          const c = Z.canvas(img.width, img.height), cx = c.getContext('2d');
          cx.drawImage(img, 0, 0);
          const d = cx.getImageData(0, 0, img.width, img.height).data;
          let solid = 0, semi = 0;
          for (let i = 3; i < d.length; i += 4 * 89) { if (d[i] > 250) solid++; else if (d[i] > 8) semi++; }
          const cor = [[0, 0], [img.width - 1, 0], [0, img.height - 1], [img.width - 1, img.height - 1]]
            .map(([x0, y0]) => cx.getImageData(x0, y0, 1, 1).data[3]);
          charProbe = { w: img.width, h: img.height, solidRatio: +(solid / Math.max(1, solid + semi)).toFixed(3), corners: cor };
        }
      } catch (e) { charProbe = { err: String(e).slice(0, 120) }; }
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
      liveSec: A.liveSec, livePct: A.livePct,
      shots: A.shots, duration: A.duration,
      looks: Object.keys(Z.LOOKS || {}), hasGlobalFX: typeof Z.globalFX === 'function',
      lutNames, badLut,
      // --- differentiation evidence -------------------------------------------------
      fxDeclared: A.fxDeclared || [], fxDropped: A.fxDropped || [],
      actCount: A.actCount, actRoles: A.actRoles, intensityCurve: A.intensityCurve, cadence: A.cadence,
      direction: A.direction || null, seed: A.seed, directionMode: A.directionMode,
      jizuraStyles: A.jizuraStyles || [], schemes: A.schemes,
      charShots: A.charShots || 0, charForms: A.charForms || {}, charImg: A.charImg || null,
      charDeclared: A.charDeclared || null, charMissing: A.charMissing || null, charProbe,
      warpViolations: (Z.LIVEWIN && Z.LIVEWIN.audit && Z.LIVEWIN.audit.violations) || 0,
      warpCalls: (Z.LIVEWIN && Z.LIVEWIN.audit && Z.LIVEWIN.audit.calls) || 0,
    };
  });

  // ---- cross-film divergence (upstream 02 §10: collide on >3 rows -> push further) --------
  let divergence = { checked: 0, worst: null, pairs: [] };
  if (rep.direction && rep.direction.signature) {
    const mine = rep.direction.signature;
    for (const f of fs.readdirSync(path.join(ROOT, 'render'))) {
      if (!/^direction_.+\.json$/.test(f)) continue;
      const other = JSON.parse(fs.readFileSync(path.join(ROOT, 'render', f), 'utf8'));
      if (other.id === song || !other.signature) continue;
      const d = DIR.diff(mine, other.signature);
      divergence.pairs.push({ vs: other.id, differ: d.count, matching: d.matching });
    }
    divergence.checked = divergence.pairs.length;
    divergence.worst = divergence.pairs.reduce((a, b) => (!a || b.differ < a.differ) ? b : a, null);
  }
  const axisTotal = DIR.AXES.length;

  const checks = [
    ['8 official scene files loaded', rep.officialFilesLoaded.length === 8, rep.officialFilesLoaded.join(', ')],
    ['>=24 official scenes registered', rep.registeredScenes >= 24, String(rep.registeredScenes)],
    ['no missing scene names', rep.missingScenes.length === 0, rep.missingScenes.join(',') || 'none'],
    ['>=6 official scenes actually used', rep.officialScenesUsed.length >= 6, rep.officialScenesUsed.join(', ')],
    ['every distinct scene executed', !rep.drawn.some(d => d.includes('!ERR')), rep.drawn.join(', ')],
    ['every shot renders without exception', rep.smoke.errors.length === 0,
      `${rep.smoke.tried}/${rep.shots} frames` + (rep.smoke.errors.length ? ' :: ' + rep.smoke.errors.slice(0, 3).join(' | ') : '')],
    ['>=6 built-in fx types', rep.fxTypes.length >= 6, rep.fxTypes.join(', ')],
    ['no declared fx silently dropped', rep.fxDropped.length === 0,
      rep.fxDropped.length ? rep.fxDropped.join(',') : `declared: ${rep.fxDeclared.join(',') || 'none'}`],
    ['>=4 recipe factories', Object.keys(rep.customFactories).length >= 4, Object.keys(rep.customFactories).join(', ')],
    ['>=6 transition types', Object.keys(rep.transitions).length >= 6, Object.keys(rep.transitions).join(', ')],
    ['>=5 JIZURA plans', rep.jizuraPlans.length >= 5, rep.jizuraPlans.join(', ')],
    [`film diverges >= 9/${axisTotal} axes from every other film`,
      divergence.checked === 0 ? true : divergence.worst.differ >= 9,
      divergence.checked === 0 ? 'no other direction_*.json yet (run tools/direction_report.mjs)'
        : `worst: ${divergence.worst.vs} ${divergence.worst.differ}/${axisTotal} (same: ${divergence.worst.matching.join(',') || 'none'})`],
    ['character asset drawable (or absent)', !rep.charDeclared || !!rep.charMissing || (rep.charProbe && !rep.charProbe.err && rep.charProbe.h >= 1200),
      !rep.charDeclared ? 'not declared' : rep.charMissing ? `MISSING, degraded: ${rep.charMissing}` : JSON.stringify(rep.charProbe)],
    ['character windows active when asset exists', !rep.charImg || rep.charShots > 0,
      rep.charImg ? `${rep.charShots} windows ${JSON.stringify(rep.charForms)}` : 'no asset yet'],
    ['centre-reserve plans only on live inserts', rep.centreFreePlans.length >= 1 && rep.centreReserveOutsideLive === 0 && rep.oncamShots === rep.liveShots,
      `centerFree=[${rep.centreFreePlans.join(',')}] outsideLive=${rep.centreReserveOutsideLive} oncam=${rep.oncamShots}/${rep.liveShots} live shots`],
    ['every shot has post', rep.shotsWithPost === rep.shots, `${rep.shotsWithPost}/${rep.shots}`],
    ['Z.LOOKS + Z.globalFX present', rep.looks.length >= 5 && rep.hasGlobalFX, `${rep.looks.length} looks`],
    ['all lutA names valid', rep.badLut.length === 0, rep.badLut.join(',') || `${rep.lutNames.join(',')}`],
    ['live footage <= 15%', rep.livePct <= 15, `${rep.livePct}%`],
    ['no warp violations', rep.warpViolations === 0, `violations=${rep.warpViolations}, drawImage calls=${rep.warpCalls}`],
    ['no page errors', errors.length === 0, errors.slice(0, 3).join(' | ') || 'none'],
    ['real GPU renderer', !/swiftshader|warp|llvmpipe/i.test(rep.renderer || ''), rep.renderer || 'unknown'],
  ];
  console.log(`\n=== OFFICIAL-CAPABILITY AUDIT  ·  ${song} ===`);
  let bad = 0;
  for (const [name, ok, detail] of checks) { console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(46)} ${detail}`); if (!ok) bad++; }
  console.log(`\ndirection  : seed=${rep.seed} mode=${rep.directionMode} acts=${rep.actCount} (${(rep.actRoles || []).join('→')})`);
  console.log(`intensity  : ${(rep.intensityCurve || []).join(' ')}`);
  console.log(`cadence    : ${(rep.cadence || []).join('')}`);
  console.log(`type layer : ${(rep.jizuraStyles || []).join('+')} | ${rep.schemes} | ${rep.jizuraPlans.length} plans`);
  console.log(`scenes used: ${JSON.stringify(rep.sceneCounts)}`);
  console.log(`fx types   : ${JSON.stringify(rep.fxTypeCounts)}`);
  console.log(`recipes    : ${JSON.stringify(rep.customFactories)}`);
  console.log(`transitions: ${JSON.stringify(rep.transitions)}`);
  console.log(`plans      : ${JSON.stringify(rep.planCounts)}`);
  if (divergence.pairs.length) console.log(`divergence : ${divergence.pairs.map(p => `${p.vs} ${p.differ}/${axisTotal}`).join('  ')}`);
  console.log(`character  : ${rep.charDeclared ? (rep.charMissing ? 'MISSING ' + rep.charMissing : `${rep.charShots} windows ${JSON.stringify(rep.charForms)} @ ${rep.charImg}`) : 'not declared'}`);
  const out = path.resolve(ROOT, 'render', `audit_${song}.json`);
  fs.writeFileSync(out, JSON.stringify({ song, meta, report: rep, checks: checks.map(([n, ok, d]) => ({ name: n, pass: ok, detail: d })), errors }, null, 2));
  console.log(`\nreport -> ${out}`);
  if (bad) { console.error(`\n${bad} CHECK(S) FAILED -- refusing to render.`); process.exitCode = 1; }
} finally {
  await browser.close();
  await new Promise(r => server.close(r));
}
