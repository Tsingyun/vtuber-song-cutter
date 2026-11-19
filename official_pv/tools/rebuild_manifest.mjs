#!/usr/bin/env node
// Rebuild cuts/<date>/official/manifest.json from the delivered files themselves.
//
// Why: manifest.json is written once, at the end of a full cut.mjs run. If a
// later single-song run or a cleanup drops entries, the manifest stops
// describing what is actually on disk. This tool re-derives every field from
// ground truth — ffprobe on the mp4, songs/<id>.json for the credits,
// songs/<date>.json for the cut range, _audit_<id>.json for scenes_used — so
// the result is measurement, not memory.
//
// It never touches the mp4 / lrc / audit files. Only manifest.json is written.
//
//   node tools/rebuild_manifest.mjs 2026-11-10
//
// Songs listed in the whitelist but missing on disk are reported and skipped
// (non-zero exit), so a half-delivered date cannot silently look complete.

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const deliverRoot = path.resolve(ROOT, '..', 'cuts');

const date = process.argv[2];
if (!date) {
  console.error('usage: node tools/rebuild_manifest.mjs <YYYY-MM-DD>');
  process.exit(2);
}

const dayFile = path.join(ROOT, 'songs', `${date}.json`);
if (!fs.existsSync(dayFile)) {
  console.error(`no day config: ${dayFile}`);
  process.exit(2);
}
const day = JSON.parse(fs.readFileSync(dayFile, 'utf8'));
const outDir = path.join(deliverRoot, date, 'official');
if (path.basename(outDir) !== 'official') throw new Error(`refusing to write ${outDir}`);
if (!fs.existsSync(outDir)) {
  console.error(`no delivery dir: ${outDir}`);
  process.exit(2);
}

function probe(file) {
  const raw = execFileSync('ffprobe', [
    '-v', 'error', '-select_streams', 'v:0', '-count_frames',
    '-show_entries', 'stream=width,height,r_frame_rate,nb_read_frames,bit_rate:format=size',
    '-of', 'json', file,
  ], { encoding: 'utf8', maxBuffer: 1 << 26 });
  const s = JSON.parse(raw).streams[0];
  return {
    width: s.width, height: s.height, fps: s.r_frame_rate,
    frames: +s.nb_read_frames, video_bitrate: +s.bit_rate,
  };
}

const songs = [];
const missing = [];
for (const id of day.whitelist) {
  const cfgFile = path.join(ROOT, 'songs', `${id}.json`);
  const meta = (day.songs || {})[id];
  if (!fs.existsSync(cfgFile) || !meta) { missing.push(`${id} (no config)`); continue; }
  const cfg = JSON.parse(fs.readFileSync(cfgFile, 'utf8'));
  const mp4 = path.join(outDir, `${meta.file}.mp4`);
  const lrc = path.join(outDir, `${meta.file}.lrc`);
  const aud = path.join(outDir, `_audit_${id}.json`);
  if (!fs.existsSync(mp4)) { missing.push(`${id} (no mp4)`); continue; }
  const p = probe(mp4);
  const report = fs.existsSync(aud)
    ? JSON.parse(fs.readFileSync(aud, 'utf8')).report
    : null;
  songs.push({
    id, title: cfg.title, original_artist: cfg.artist, credit: cfg.credit,
    source_range: [meta.abs_start, meta.abs_end], duration: cfg.duration,
    file: `${meta.file}.mp4`, lrc: `${meta.file}.lrc`,
    ...p, size: fs.statSync(mp4).size,
    audit: fs.existsSync(aud) ? `_audit_${id}.json` : null,
    scenes_used: (report && report.officialScenesUsed) || [],
  });
  console.log(`  ${id.padEnd(20)} ${p.width}x${p.height} ${p.fps} ${p.frames}f `
    + `${(p.video_bitrate / 1e6).toFixed(2)} Mb/s ${(fs.statSync(mp4).size / 1e6).toFixed(0)} MB `
    + `${songs.at(-1).scenes_used.length} scenes`);
}

if (missing.length) {
  console.error(`\nMISSING on disk (whitelist expects them): ${missing.join(', ')}`);
  console.error('refusing to write a manifest that does not match the whitelist');
  process.exit(1);
}

const manifest = {
  date,
  generated_by: 'official_pv/cut.mjs',
  upstream: 'https://github.com/EGSECDA/vocaloid-style-mv-pipeline',
  verified_against_local_clone: '13b380dab077d4c8ea956825c9693c720e0c2b91',
  completeness_report: 'official_pv/docs/COMPLETENESS.md',
  songs,
};
fs.writeFileSync(path.join(outDir, 'manifest.json'), JSON.stringify(manifest, null, 2), 'utf8');
console.log(`\nWROTE ${path.join(outDir, 'manifest.json')} — ${songs.length} song(s), exactly the whitelist`);
