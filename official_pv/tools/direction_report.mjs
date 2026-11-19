#!/usr/bin/env node
/* direction_report.mjs -- enforce upstream 02 §10 across a whole session.
 *
 *   "pick one option per row from the song's own material;
 *    if you collide with the example on more than 3 rows, push further."
 *
 * This tool materialises every song's 12-axis signature WITHOUT rendering anything, then
 * compares every pair. A pair that collides on more than 3 axes fails the run, so a
 * templated batch cannot reach the renderer.
 *
 *   node tools/direction_report.mjs --date 2026-11-10
 *   node tools/direction_report.mjs --date 2026-11-10 --allow-collisions 4   (debug only)
 *
 * Writes render/direction_<id>.json (per song) and render/direction_diff.json (matrix).
 */
import fs from 'node:fs';
import path from 'node:path';
import { ROOT, cliArg } from './kit_env.mjs';

const date = cliArg('date', '2026-11-10');
const allow = +(cliArg('allow-collisions', '3'));

// ---- load the browser-side differentiation core into node (both files are DOM-free) --------
const win = {};
globalThis.window = win;
const ev = f => new Function('window', fs.readFileSync(path.join(ROOT, f), 'utf8'))(win);
ev('engine/core/util.js');
ev('engine/direction.js');
const Z = win.Z, DIR = win.DIR;

const session = JSON.parse(fs.readFileSync(path.join(ROOT, 'songs', `${date}.json`), 'utf8'));
const ids = session.whitelist;

function linesOf(cfg) {
  const p = path.join(ROOT, 'songs', `${cfg.id}.lrc`);
  if (!fs.existsSync(p)) return [];
  const off = +(cfg.lrcOffset || 0);
  const out = [];
  for (const l of fs.readFileSync(p, 'utf8').replace(/\ufeff/g, '').split(/\r?\n/)) {
    const m = l.match(/^\[(\d+):(\d+(?:\.\d+)?)\]/);
    if (m) { const t = +m[1] * 60 + +m[2] - off; if (t >= 0) out.push({ t }); }
  }
  return out.sort((a, b) => a.t - b.t);
}

const rows = [];
for (const id of ids) {
  const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'songs', `${id}.json`), 'utf8'));
  cfg.id = cfg.id || id;
  const lines = linesOf(cfg);
  const dir = DIR.normalize(cfg, id);
  const acts = dir.structure.mode === 'auto' ? DIR.buildActs(cfg, lines) : (cfg.acts || DIR.buildActs(cfg, lines));
  const sig = DIR.signature(cfg, acts);
  const report = {
    id, title: cfg.title, duration: cfg.duration, seed: dir.seed, mode: dir.structure.mode,
    acts: acts.map(a => ({ role: a.role, t0: a.t0, t1: +(a.t1 - a.t0).toFixed(2), look: a.look, intensity: a.intensity, cadence: a.cadence, lut: a.lut, pool: a.pool, trans: a.trans })),
    signature: sig,
  };
  fs.writeFileSync(path.join(ROOT, 'render', `direction_${id}.json`), JSON.stringify(report, null, 1), 'utf8');
  rows.push({ id, title: cfg.title, acts, sig, seed: dir.seed, mode: dir.structure.mode });
}

console.log(`\n=== DIRECTION REPORT · ${date} (${rows.length} songs) ===\n`);
for (const r of rows) {
  console.log(`${r.title}  [${r.id}]  seed=${r.seed}  ${r.acts.length} acts  (${r.mode})`);
  console.log('  roles      : ' + r.acts.map(a => a.role).join(' → '));
  console.log('  lengths(s) : ' + r.acts.map(a => (a.t1 - a.t0).toFixed(1)).join(' '));
  console.log('  intensity  : ' + r.acts.map(a => a.intensity.toFixed(2)).join(' '));
  console.log('  cadence    : ' + r.acts.map(a => a.cadence).join('') + `   (density ${r.sig.density})`);
  console.log('  type       : ' + r.sig.type);
  console.log('  scene vocab: ' + r.sig.sceneVocab);
  console.log('  trans      : ' + r.sig.trans);
  console.log('  ending     : ' + r.sig.ending);
  console.log('');
}

// ---- pairwise 12-axis divergence --------------------------------------------------------
const pairs = [];
for (let i = 0; i < rows.length; i++) for (let j = i + 1; j < rows.length; j++) {
  const d = DIR.diff(rows[i].sig, rows[j].sig);
  pairs.push({ a: rows[i].id, b: rows[j].id, differ: d.count, matching: d.matching, differing: d.differing });
  const ok = d.count >= DIR.AXES.length - allow;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${rows[i].title}  ×  ${rows[j].title}   ${d.count}/${DIR.AXES.length} axes differ` +
    (d.matching.length ? `   (same: ${d.matching.join(', ')})` : ''));
}

// structural fingerprints that are NOT on the matrix but are what "templated" looked like
const shapes = new Map();
for (const r of rows) {
  const k = `${r.acts.length}|${r.acts.map(a => a.role).join(',')}|${r.acts.map(a => a.intensity.toFixed(2)).join(',')}|${r.acts.map(a => a.cadence).join('')}`;
  if (!shapes.has(k)) shapes.set(k, []);
  shapes.get(k).push(r.id);
}
const dupShapes = [...shapes.values()].filter(v => v.length > 1);
console.log(`\nskeleton fingerprint: ${shapes.size} distinct shape(s) for ${rows.length} song(s)` +
  (dupShapes.length ? `  ** IDENTICAL: ${dupShapes.map(v => v.join('=')).join(' ')}` : ''));

// ---- session capability coverage: a minimal film is fine, a minimal BATCH is not ----------
const union = (key, split = ',') => [...new Set(rows.flatMap(r => String(r.sig[key] || '').split(split)).filter(Boolean))].sort();
const coverScenes = [...new Set(rows.flatMap(r => String(r.sig.sceneVocab || '').split(',')).filter(Boolean))].sort();
const coverTrans = union('trans');
const coverLut = [...new Set(rows.flatMap(r => r.acts.map(a => a.lut)))].sort();
const coverCadence = [...new Set(rows.flatMap(r => r.acts.map(a => a.cadence)))].sort();
console.log(`\nsession coverage: ${coverScenes.length} scenes · ${coverTrans.length} transitions · ${coverLut.length} lut rows · cadence values ${coverCadence.join('/')}`);
console.log(`  scenes     : ${coverScenes.join(', ')}`);
console.log(`  transitions: ${coverTrans.join(', ')}`);
console.log(`  luts       : ${coverLut.join(', ')}`);

const CAP_MIN = { scenes: 12, transitions: 9, luts: 4 };
const capFail = [];
if (coverScenes.length < CAP_MIN.scenes) capFail.push(`scenes ${coverScenes.length}<${CAP_MIN.scenes}`);
if (coverTrans.length < CAP_MIN.transitions) capFail.push(`transitions ${coverTrans.length}<${CAP_MIN.transitions}`);
if (coverLut.length < CAP_MIN.luts) capFail.push(`luts ${coverLut.length}<${CAP_MIN.luts}`);

const bad = pairs.filter(p => p.differ < DIR.AXES.length - allow);
fs.writeFileSync(path.join(ROOT, 'render', 'direction_diff.json'), JSON.stringify({
  date, axes: DIR.AXES, allowCollisions: allow, skeletons: shapes.size, identicalSkeletons: dupShapes, pairs,
  coverage: { scenes: coverScenes, transitions: coverTrans, luts: coverLut, minimum: CAP_MIN },
}, null, 1), 'utf8');

if (bad.length || dupShapes.length || capFail.length) {
  if (bad.length) console.error(`\n${bad.length} PAIR(S) COLLIDE ON MORE THAN ${allow} AXIS/ES -- push the directions further before rendering.`);
  if (dupShapes.length) console.error(`\n${dupShapes.length} IDENTICAL ACT SKELETON(S): ${dupShapes.map(v => v.join('=')).join(' ')}`);
  if (capFail.length) console.error(`\nSESSION COVERAGE TOO NARROW: ${capFail.join(', ')}`);
  process.exitCode = 1;
} else {
  console.log(`\nOK: every pair differs on >= ${DIR.AXES.length - allow}/${DIR.AXES.length} axes; ${shapes.size} distinct skeletons.`);
}
