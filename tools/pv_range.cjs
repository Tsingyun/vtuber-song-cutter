#!/usr/bin/env node
/* 按帧号**顺序**渲染一段（不编码），用来复现/验收转场处的逐帧行为。
 *
 * 为什么不能用 pv_stills：它是「跳到某个 t 渲一帧」，跳进去时上一镜的快照（XC）
 * 根本没建立，`crossFade` 直接短路 —— 所以静帧永远看不到转场，也永远复现不了
 * 「退场淡出后快照又被盖回来」这类问题。转场只能顺序跑出来。
 *
 *   node tools/pv_range.cjs --sb _tmp/pv/sb.json --live _tmp/pv/live_seq/live.json \
 *        --from 330 --to 380 --out _tmp/pv/range [--res 1] [--shot-id]
 *
 * 产出：r_00353.png … + frames.json（每帧的帧号/时间/所属镜头/与前一帧的字节是否相同）
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const argOf = (k, d) => { const i = process.argv.indexOf('--' + k); return i > 0 ? process.argv[i + 1] : d; };

(async () => {
  const sbPath = path.resolve(ROOT, argOf('sb'));
  const outDir = path.resolve(ROOT, argOf('out', '_tmp/pv/range'));
  const res = Number(argOf('res', '1'));
  /* fps 必须与成片一致：sb.meta.fps 是个没被用的默认值（60），
     真正生效的是 job 里的 fps（本片 30）。传错会让整段跑到别的时间上去。 */
  const fps = Number(argOf('fps', '30'));
  const i0 = Number(argOf('from', '0'));
  const i1 = Number(argOf('to', '30'));
  fs.mkdirSync(outDir, { recursive: true });

  const sb = JSON.parse(fs.readFileSync(sbPath, 'utf8'));
  let live = null;
  const livePath = argOf('live', '');
  if (livePath && fs.existsSync(path.resolve(ROOT, livePath))) {
    const lj = JSON.parse(fs.readFileSync(path.resolve(ROOT, livePath), 'utf8'));
    live = {};
    /* 序列名由 live.json 决定（LIVE_SPANS 的 s1..s6），不再写死 head/body/wide。
       写死时新格式一条都装不进去，画面上「六处该有人的地方全空」而不报错。 */
    for (const k of Object.keys(lj)) {
      if (k === 'face' || k === 'cut_start' || !lj[k] || typeof lj[k] !== 'object') continue;
      live[k] = Object.assign({}, lj[k], { dir: path.resolve(ROOT, lj[k].dir).replace(/\\/g, '/') });
    }
    if (lj.face) live.face = lj.face;
  }

  const browser = await chromium.launch({ args: ['--allow-file-access-from-files', '--enable-gpu'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  page.on('console', m => { const s = m.text(); if (m.type() === 'error') console.log('  ' + s); });

  const url = 'file:///' + path.join(ROOT, 'renderer/player/index_pv.html').replace(/\\/g, '/') + '?res=' + res;
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction('window.PV && PV.renderAt', null, { timeout: 30000 });

  const cfg = { storyboard: sb, live, dur: sb.meta.dur, fps, t: 0 };
  const r0 = await page.evaluate(c => window.__pvShot(c), Object.assign({}, cfg, { t: -1 })).catch(e => ({ err: String(e) }));
  if (r0 && r0.err) { console.error('[setup]', r0.err); process.exit(1); }
  const info = await page.evaluate(() => window.__pvInfo());
  console.log('[range] %s  shots=%d  帧 %d..%d', (1920 * info.scale) + 'x' + (1080 * info.scale),
    info.stats.shots, i0, i1 - 1);

  const rows = [];
  let prev = null;
  for (let i = i0; i < i1; i++) {
    const t0 = Date.now();
    await page.evaluate(n => window.__frame(n), i);
    const durl = await page.evaluate(() => window.__grab());
    const b = Buffer.from(durl.split(',')[1], 'base64');
    const f = path.join(outDir, 'r_' + String(i).padStart(5, '0') + '.png');
    fs.writeFileSync(f, b);
    const meta = await page.evaluate(n => {
      const sh = window.PV.shotAt ? PV.shotAt(n / HL.fps) : null;
      return sh ? { i: sh.i, t0: sh.t0, t1: sh.t1, trans: (sh.trans || {}).k || null,
                    xd: window.PV.__xdbg || null } : null;
    }, i);
    const same = prev && Buffer.compare(prev, b) === 0;
    rows.push({ f: i, t: +(i / fps).toFixed(3), shot: meta, bytes: b.length, sameAsPrev: !!same });
    prev = b;
    const xd = meta && meta.xd;
    console.log('  f' + String(i).padStart(5, '0') + '  ' + (i / fps).toFixed(3) + 's  shot#' +
      (meta ? meta.i : '-') + ' ' + (meta ? (meta.trans || '-') : '') +
      (xd ? '  q=' + xd.q.toFixed(3) + ' (' + xd.k + ' dur=' + xd.dur + ' from#' + xd.from + ')' : '') +
      '  ' + (same ? '同前一帧' : '新画') + '  ' + String(Date.now() - t0).padStart(5) + 'ms');
  }
  fs.writeFileSync(path.join(outDir, 'frames.json'), JSON.stringify(rows, null, 1));
  await browser.close();
})().catch(e => { console.error('[fatal]', e.message); process.exit(1); });
