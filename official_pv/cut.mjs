#!/usr/bin/env node
/* cut.mjs -- ONE COMMAND to prepare, prove, render and deliver a song-cut.
 *
 *   node cut.mjs --date 2026-11-09                 # all songs in that day's whitelist
 *   node cut.mjs --date 2026-11-09 --song faruxue  # one song
 *   node cut.mjs --date 2026-11-09 --audit-only    # prepare + prove, do NOT render
 *
 * Stage order is enforced: prepare -> RUNTIME official-capability audit -> render -> ffprobe -> deliver.
 * If the audit fails, no video is produced. That is the whole point of this system.
 */
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { ROOT, cliArg, cliFlag } from './tools/kit_env.mjs';

const date = cliArg('date', '2026-11-09');
const only = cliArg('song', '');
const auditOnly = cliFlag('audit-only');
const session = JSON.parse(fs.readFileSync(path.join(ROOT, 'songs', `${date}.json`), 'utf8'));
const ids = only ? [only] : session.whitelist;
for (const id of ids) if (!session.whitelist.includes(id)) { console.error(`song ${id} is not in the ${date} whitelist`); process.exit(2); }

// -- DIRECTORY BOUNDARY -- RULES.md 129 (2026-11-10 incident) ------------------
// This repo is shared with ANOTHER, PARALLEL, ACTIVE song-cut pipeline:
//   song_cutter.py / auto_cut.py / renderer/   (A-side, 4K60)
// It writes its products AND its IN-USE mux intermediates (.mp3, .render.mp4,
// .mjpeg, cutter.log, events.jsonl, manifest.json) into cuts/<date>/ TOP LEVEL.
// Those files are NOT ours. NEVER move, rename, delete, overwrite or "clean"
// any of them, even when the song is not in our whitelist. Never suggest
// stopping that pipeline. Our results live ONLY in cuts/<date>/official/.
const DELIVERY_SUBDIR = 'official';
function deliveryDir(d) {
  // Derive from ROOT (the official_pv dir) instead of hardcoding a Windows path.
  const dir = path.join(path.dirname(ROOT), 'cuts', d, DELIVERY_SUBDIR);
  if (path.basename(dir) !== DELIVERY_SUBDIR) throw new Error('delivery must stay inside official/');
  return dir;
}
// Read-only awareness: LIST and PRINT only; never touch those files.
try {
  const top = path.join(path.dirname(ROOT), 'cuts', date);
  const buddy = fs.readdirSync(top).filter(n =>
    /^(cutter[.]log|events[.]jsonl|manifest[.]json)$/.test(n) ||
    /[.](mjpeg|mp3|src[.]lrc|lrc|mp4|render[.]mp4)$/i.test(n));
  if (buddy.length) console.log('[boundary] cuts/' + date + '/ 顶层检测到平行管线 ' + buddy.length +
    ' 个文件（只读，不移动/不删除/不覆盖）；两边同时跑同一场请先与用户确认。');
} catch { /* top dir not present yet: fine */ }


function run(bin, args, label) {
  console.log(`\n======== ${label} ========`);
  const r = spawnSync(bin, args, { cwd: ROOT, stdio: 'inherit', shell: false });
  if (r.status !== 0) { console.error(`${label} FAILED (exit ${r.status})`); process.exit(r.status || 1); }
}
function probe(file) {
  const r = spawnSync('ffprobe', ['-v', 'error', '-count_frames', '-select_streams', 'v:0',
    '-show_entries', 'stream=width,height,r_frame_rate,nb_read_frames,bit_rate',
    '-show_entries', 'format=duration,size,bit_rate', '-of', 'json', file],
    { cwd: ROOT, encoding: 'utf8' });
  if (r.status !== 0) return null;
  return JSON.parse(r.stdout);
}

const delivered = [];
for (const id of ids) {
  const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'songs', `${id}.json`), 'utf8'));
  const meta = session.songs[id];
  console.log(`\n############  ${cfg.title}  ·  ${cfg.artist}  ·  ${cfg.duration}s  ############`);

  run('python', ['tools/prepare.py', '--date', date, '--song', id], `PREPARE ${id}`);
  run(process.execPath, ['tools/audit_official.mjs', '--song', id], `OFFICIAL-CAPABILITY AUDIT ${id}`);
  const audit = JSON.parse(fs.readFileSync(path.join(ROOT, 'render', `audit_${id}.json`), 'utf8'));
  const failed = audit.checks.filter(c => !c.pass);
  if (failed.length) { console.error('audit failed, refusing to render:', failed.map(c => c.name).join(', ')); process.exit(3); }
  console.log(`AUDIT PASSED: ${audit.checks.length} checks, ${audit.report.registeredScenes} official scenes registered, ${audit.report.officialScenesUsed.length} used.`);

  if (auditOnly) { console.log('(--audit-only) skipping render'); continue; }

  const tmpOut = path.join(ROOT, 'render', `${id}.mp4`);
  run(process.execPath, ['tools/render_frames.mjs',
    '--url', `engine/index.html?song=${id}`,
    '--out', `render/${id}.mp4`,
    '--start', '0', '--end', String(cfg.duration),
    '--mode', 'webcodecs', '--workers', '1', '--wc-mode', 'quantizer', '--wc-qp', '20',
    '--audio', 'audio/song.wav', '--stats', `render/${id}_stats.json`], `RENDER ${id}`);

  const pr = probe(tmpOut);
  if (!pr) { console.error('ffprobe failed on', tmpOut); process.exit(4); }
  const st = pr.streams[0], fmt = pr.format;
  const frames = +st.nb_read_frames, dur = +fmt.duration;
  const ok = st.width === 1920 && st.height === 1080 && st.r_frame_rate === '30/1'
          && Math.abs(dur - cfg.duration) < 0.3 && Math.abs(frames - cfg.duration * 30) < 6;
  console.log(`ffprobe: ${st.width}x${st.height} ${st.r_frame_rate} ${frames} frames ${dur}s ${(fmt.bit_rate/1e6).toFixed(2)} Mb/s -> ${ok ? 'OK' : 'MISMATCH'}`);
  if (!ok) { console.error('output spec mismatch, not delivering'); process.exit(5); }

  // ---- deliver ONLY the whitelisted songs, into a clean directory -----------------
  const outDir = deliveryDir(date);
  fs.mkdirSync(outDir, { recursive: true });
  const base = meta.file;
  const mp4 = path.join(outDir, `${base}.mp4`), lrc = path.join(outDir, `${base}.lrc`);
  fs.copyFileSync(tmpOut, mp4); fs.rmSync(tmpOut);
  fs.copyFileSync(path.join(ROOT, 'songs', `${id}.deliver.lrc`), lrc);
  fs.copyFileSync(path.join(ROOT, 'render', `audit_${id}.json`), path.join(outDir, `_audit_${id}.json`));
  delivered.push({
    id, title: cfg.title, original_artist: cfg.artist, credit: cfg.credit,
    source_range: [meta.abs_start, meta.abs_end], duration: cfg.duration,
    file: `${base}.mp4`, lrc: `${base}.lrc`,
    width: st.width, height: st.height, fps: st.r_frame_rate, frames,
    video_bitrate: +fmt.bit_rate, size: +fmt.size,
    audit: `_audit_${id}.json`,
    scenes_used: audit.report.officialScenesUsed,
  });
  console.log(`DELIVERED -> ${mp4}`);
}

// manifest contains EXACTLY the whitelist, nothing else
const outDir = deliveryDir(date);
if (!auditOnly) {
  fs.writeFileSync(path.join(outDir, 'manifest.json'), JSON.stringify({
    date, generated_by: 'official_pv/cut.mjs',
    upstream: 'https://github.com/EGSECDA/vocaloid-style-mv-pipeline',
    verified_against_local_clone: '13b380dab077d4c8ea956825c9693c720e0c2b91',
    completeness_report: 'official_pv/docs/COMPLETENESS.md',
    songs: delivered,
  }, null, 2), 'utf8');
  console.log(`\n=== DONE: ${delivered.length} song(s) ===`);
  for (const d of delivered) console.log(`  ${d.title}  ${d.duration}s  ${(d.size/1e6).toFixed(0)} MB  ${d.scenes_used.length} official scenes`);
}
