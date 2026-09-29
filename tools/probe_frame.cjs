/* _probe_frame.cjs —— 跳到跳变帧前后，全画布像素 diff 定位「到底哪块在变」
 * 用法: node _probe_frame.cjs --job _render_job.json --from 5105 --to 5114 --shots 5108,5109,5110,5111
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

function argOf(n) { const i = process.argv.indexOf('--' + n); return i >= 0 ? process.argv[i + 1] : null; }

(async () => {
  const job = JSON.parse(fs.readFileSync(argOf('job'), 'utf-8'));
  const playerDir = job.playerDir || path.join(__dirname, '..', 'renderer', 'player');
  const F = +(argOf('from') || 5105), T = +(argOf('to') || 5114);
  const shotList = (argOf('shots') || '').split(',').map(Number).filter(x => !isNaN(x));

  const browser = await chromium.launch({
    args: ['--mute-audio', '--autoplay-policy=no-user-gesture-required',
           '--disable-dev-shm-usage', '--force-device-scale-factor=1',
           '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
           '--disable-background-timer-throttling'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  await page.goto('file:///' + path.join(playerDir, 'index.html').replace(/\\/g, '/') + '?render=1', { waitUntil: 'load' });
  await page.waitForFunction('window.HL && HL.ready', null, { timeout: 60000 });
  await page.setInputFiles('#mediaFile', job.audio);
  await page.waitForTimeout(600);

  const durl = p => { try { return 'data:image/png;base64,' + fs.readFileSync(p).toString('base64'); } catch (e) { return null; } };
  const meta = {
    title: job.title || '', artist: job.artist || '', trackLabel: job.trackLabel || '',
    romaji: job.romaji || '', vocal: job.vocal != null ? job.vocal : '',
    producer: job.producer || '', tags: job.tags || '', note: job.note || '',
    footerLeft: job.footerLeft != null ? job.footerLeft : 'SONG ARCHIVE',
    footerRight: job.footerRight || '', lrc: job.lrc || null, cover: job.cover || null,
    badge: job.badge || '', mark: job.mark || '',
    sticker: durl(job.sticker || path.join(playerDir, 'assets', 'sticker.png')),
    watermark: durl(job.watermark || path.join(playerDir, 'assets', 'watermark.png')),
    artImage: job.artImage || null, scheme: (job.scheme == null ? null : Number(job.scheme)),
  };
  const setup = await page.evaluate(async m => await window.__setup(m), meta);
  if (!setup.ok) { console.error('[setup 失败]', setup.error); process.exit(3); }
  console.log(`[setup] ${setup.total} 帧`);

  await page.evaluate(() => {
    window.__lyrLast = null;
    window.__lyrDbg = o => { window.__lyrLast = { frame: o.frame, t: +o.t.toFixed(4), idx: o.idx, anchor: o.anchor, scrollY: +o.scrollY.toFixed(3), bl: o.bl, act: o.act }; };
  });

  const out = await page.evaluate(async ({ F, T, shotList }) => {
    const cv = document.querySelector('canvas');
    const ctx = cv.getContext('2d');
    const W = cv.width, H = cv.height;
    const res = { pairs: [], shots: {}, frames: [] };
    let prev = null;
    for (let i = 0; i <= T; i++) {
      window.__frame(i);
      if (i < F) { continue; }
      const cur = ctx.getImageData(0, 0, W, H);
      res.frames.push({ i, dbg: window.__lyrLast });
      if (prev) {
        const a = prev.d.data, b = cur.data;
        let cnt = 0, sum = 0, minx = 1e9, maxx = -1, miny = 1e9, maxy = -1;
        const rows = new Float64Array(H), cols = new Float64Array(W);
        for (let y = 0; y < H; y++) {
          const off = y * W * 4;
          for (let x = 0; x < W; x++) {
            const p = off + x * 4;
            const d = Math.abs(a[p] - b[p]) + Math.abs(a[p + 1] - b[p + 1]) + Math.abs(a[p + 2] - b[p + 2]);
            if (d > 24) {
              cnt++; sum += d; rows[y] += d; cols[x] += d;
              if (x < minx) minx = x; if (x > maxx) maxx = x;
              if (y < miny) miny = y; if (y > maxy) maxy = y;
            }
          }
        }
        const top = (arr, n) => Array.from(arr).map((v, k) => [k, v]).sort((p, q) => q[1] - p[1]).slice(0, n);
        res.pairs.push({
          from: prev.i, to: i, dbg: window.__lyrLast,
          count: cnt, sum: sum, bbox: [minx, miny, maxx, maxy],
          rows: top(rows, 6), cols: top(cols, 6),
        });
      }
      prev = { i, d: cur };
      if (shotList.indexOf(i) >= 0) res.shots[i] = cv.toDataURL('image/png');
      if (i % 3 === 0) await new Promise(r => setTimeout(r, 0));
    }
    return res;
  }, { F, T, shotList });

  console.log('\n=== 相邻帧像素差异（阈值 24）===');
  for (const p of out.pairs) {
    console.log(`f${p.from}→f${p.to}  t=${p.dbg.t.toFixed(3)} idx=${p.dbg.idx}  变化像素=${p.count}  强度=${p.sum}  bbox=(x${p.bbox[0]}-${p.bbox[2]}, y${p.bbox[1]}-${p.bbox[3]})`);
    if (p.count > 0) {
      console.log('     行热点(y:强度): ' + p.rows.map(r => `${r[0]}:${r[1]}`).join('  '));
      console.log('     列热点(x:强度): ' + p.cols.map(r => `${r[0]}:${r[1]}`).join('  '));
    }
  }
  console.log('\n=== 帧状态 ===');
  for (const f of out.frames) {
    const d = f.dbg || {};
    console.log(`  f=${f.i} t=${(d.t ?? -1).toFixed?.(3)} idx=${d.idx} anchor=${d.anchor} scrollY=${d.scrollY} bl=[${(d.bl || []).map(v => v.toFixed(2)).join(',')}] act=[${(d.act || []).map(v => v.toFixed(3)).join(',')}]`);
  }
  for (const k of Object.keys(out.shots)) {
    fs.writeFileSync(`_diffframe_f${k}.png`, Buffer.from(out.shots[k].split(',')[1], 'base64'));
    console.log(`[shot] _diffframe_f${k}.png`);
  }
  fs.writeFileSync('_probe_frame.json', JSON.stringify(out.pairs, null, 1));
  await browser.close();
})().catch(e => { console.error('[fatal]', e); process.exit(5); });
