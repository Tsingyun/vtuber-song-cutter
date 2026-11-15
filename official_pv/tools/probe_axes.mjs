#!/usr/bin/env node
// probe_axes.mjs -- 12-axis declared-divergence matrix, measured on real song configs.
//
// Why this exists: `tools/audit_official.mjs:190` only compares a film against the films
// that happen to be in `render/`, and it reports the worst pair as a single number. That makes
// two failures invisible: (a) a pair of old films collapsing to 2/12 never enters the report,
// (b) the number moves as files get cleaned out of `render/`. This script always evaluates the
// full N*(N-1)/2 matrix over every `songs/*.json`, so the collapse shows up every time.
//
// It deliberately replicates the real engine path rather than calling DIR.buildActs blindly:
//   songcut.timeline.js:317-318  LRC parse (BOM strip + [ti]/[ar]/... header filter)
//   songcut.timeline.js:332      mode === 'auto' ? buildActs : (cfg.acts || buildActs)
// Skipping either step silently under-reports: with no `lines`, buildActs returns `cfg.acts`
// early (direction.js:145) and the hand-written acts carry no `cadence`, which would make
// every song look like it had an empty cadence axis.
//
// Usage:
//   node tools/probe_axes.mjs              # all songs, full matrix + degenerate-axis census
//   node tools/probe_axes.mjs natianxiayule faruxue
//   node tools/probe_axes.mjs --gate 9     # exit 1 when any pair is below the gate
//
// Read-only. Writes nothing.

import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(import.meta.dirname, '..');
const AXES_SRC = path.join(ROOT, 'engine/direction.js');
const UTIL_SRC = path.join(ROOT, 'engine/core/util.js');
const SONG_DIR = path.join(ROOT, 'songs');

// direction.js is an IIFE that hangs off `window`; load it into a bare object.
function loadEngine() {
  const win = {};
  for (const f of [UTIL_SRC, AXES_SRC]) {
    new Function('window', fs.readFileSync(f, 'utf8'))(win);
  }
  if (!win.DIR || !win.DIR.AXES) throw new Error('engine/direction.js did not expose window.DIR');
  return win.DIR;
}

// Mirrors songcut.timeline.js:313-318 exactly.
function parseLrc(absPath) {
  const raw = fs.readFileSync(absPath, 'utf8').replace(/^\uFEFF/, '');
  const kept = raw.split(/\r?\n/)
    .filter(l => l.trim() && !/^\[(ti|ar|al|by|offset|length):/i.test(l.trim()))
    .join('\n');
  const lines = [];
  for (const l of kept.split(/\r?\n/)) {
    const m = l.match(/^\[(\d+):(\d+(?:\.\d+)?)\](.*)$/);
    if (m) lines.push({ t: +m[1] * 60 + +m[2], text: (m[3] || '').trim() });
  }
  return lines;
}

function measure(DIR, id) {
  const cfg = JSON.parse(fs.readFileSync(path.join(SONG_DIR, `${id}.json`), 'utf8'));
  const lrcPath = path.resolve(ROOT, (cfg.lrc || '').replace(/^\.\//, ''));
  const lines = cfg.lrc && fs.existsSync(lrcPath) ? parseLrc(lrcPath) : [];

  DIR.normalize(cfg, id);                                  // fills DEFAULTS, mutates cfg.direction
  const mode = cfg.direction.structure.mode;
  const acts = mode === 'auto'
    ? DIR.buildActs(cfg, lines)
    : (cfg.acts && cfg.acts.length ? cfg.acts : DIR.buildActs(cfg, lines));
  cfg.acts = acts;                                          // same as songcut.timeline.js:333

  return {
    id, mode, lines: lines.length, acts: acts.length,
    withCadence: acts.filter(a => a.cadence != null).length,
    sig: DIR.signature(cfg, acts),
  };
}

// A pair that "differs" on an axis both sides left blank is not a difference. Report both
// the raw DIR.diff count and a strict count that drops null/empty-on-both-sides.
function strictDiff(DIR, a, b) {
  const raw = DIR.diff(a.sig, b.sig);
  const degenerate = [];
  const counted = [];
  for (const ax of DIR.AXES) {
    const x = a.sig[ax], y = b.sig[ax];
    const xDead = x == null || x === '';
    const yDead = y == null || y === '';
    if (xDead && yDead) degenerate.push(ax);
    else if (raw.differing.includes(ax)) counted.push(ax);
  }
  return { raw: raw.count, strict: counted.length, known: DIR.AXES.length - degenerate.length, counted, degenerate };
}

const args = process.argv.slice(2);
const gateIdx = args.indexOf('--gate');
const gate = gateIdx >= 0 ? Number(args[gateIdx + 1]) || 9 : null;
const want = args.filter((a, i) => !a.startsWith('--') && !(gateIdx >= 0 && i === gateIdx + 1));

const DIR = loadEngine();
const W = 26;   // wide enough for `natianxiayule.palette` without colliding into the next column

// songs/ also holds two other kinds of file: session configs (`2026-11-09.json`, keys
// date/source_video/whitelist/songs) and palette override fragments (`*.palette.json`, ~450 B).
// Neither is a film, and letting them into the matrix produces fake 0/12 rows.
function isSongConfig(id) {
  try {
    const c = JSON.parse(fs.readFileSync(path.join(SONG_DIR, `${id}.json`), 'utf8'));
    return typeof c.duration === 'number' && typeof c.lrc === 'string' && !!c.lrc;
  } catch { return false; }
}

const ids = (want.length ? want : fs.readdirSync(SONG_DIR)
  .filter(f => f.endsWith('.json') && !f.startsWith('_'))
  .map(f => path.basename(f, '.json'))
  .filter(isSongConfig)).sort();

const rows = ids.map(id => measure(DIR, id));

console.log(`\n== ${rows.length} songs ==`);
console.log('id'.padEnd(W) + 'mode'.padEnd(8) + 'lrc'.padStart(5) + 'acts'.padStart(6) + 'cadence-acts'.padStart(14));
for (const r of rows) {
  console.log(r.id.padEnd(W) + r.mode.padEnd(8) + String(r.lines).padStart(5)
    + String(r.acts).padStart(6) + String(r.withCadence).padStart(6) + (r.withCadence === 0 ? '  <-- DEAD' : ''));
}

const deadAxes = DIR.AXES.filter(ax => rows.every(r => r.sig[ax] == null || r.sig[ax] === ''));
if (deadAxes.length) console.log(`\n[!] axes empty in ALL songs (can never contribute): ${deadAxes.join(', ')}`);

// Only report the axis set that at least one song populates.
const liveAxes = DIR.AXES.filter(ax => !deadAxes.includes(ax));
console.log(`[i] ${liveAxes.length}/${DIR.AXES.length} axes are live: ${liveAxes.join(', ')}`);

console.log('\n== pairwise (raw | strict/known) ==');
const fails = [];
for (let i = 0; i < rows.length; i++) {
  for (let j = i + 1; j < rows.length; j++) {
    const d = strictDiff(DIR, rows[i], rows[j]);
    const flag = d.raw < (gate ?? 9) ? '  <-- BELOW GATE' : '';
    console.log(`${rows[i].id.padEnd(W)}${rows[j].id.padEnd(W)}`
      + `${String(d.raw).padStart(2)}/12 | ${String(d.strict).padStart(2)}/${String(d.known).padStart(2)}${flag}`);
    if (d.raw < (gate ?? 9)) fails.push({ a: rows[i].id, b: rows[j].id, d });
  }
}

for (const f of fails) {
  console.log(`\n-- ${f.a} x ${f.b} : only ${f.d.raw}/12 --`);
  console.log('   genuinely differing : ' + (f.d.counted.join(', ') || '(none)'));
  console.log('   blank on both sides : ' + (f.d.degenerate.join(', ') || '(none)'));
}

if (gate != null && fails.length) {
  console.log(`\nFAIL: ${fails.length} pair(s) below ${gate}/12`);
  process.exit(1);
}
