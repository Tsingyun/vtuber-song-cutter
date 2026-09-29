/* _probe_lyr.cjs —— 歌词渲染层逐帧取证
 * 对指定帧中心 ±span 逐帧渲染，抓：整区平均亮度 + 9 条带亮度 + __lyrDbg 状态量
 * 用法: node _probe_lyr.cjs --job _render_job.json --centers 5110,5773 --span 40 --out _probe_lyr.json
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

function argOf(n) { const i = process.argv.indexOf('--' + n); return i >= 0 ? process.argv[i + 1] : null; }

(async () => {
  const job = JSON.parse(fs.readFileSync(argOf('job'), 'utf-8'));
  const playerDir = job.playerDir || path.join(__dirname, '..', 'renderer', 'player');
  const centers = (argOf('centers') || '').split(',').map(Number).filter(x => !isNaN(x));
  const span = +(argOf('span') || 40);
  const outFile = argOf('out') || '_probe_lyr.json';

  const browser = await chromium.launch({
    args: ['--mute-audio', '--autoplay-policy=no-user-gesture-required',
           '--disable-dev-shm-usage', '--force-device-scale-factor=1',
           '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
           '--disable-background-timer-throttling'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', e => console.error('[pageerror]', e.message));

  const url = 'file:///' + path.join(playerDir, 'index.html').replace(/\\/g, '/') + '?render=1';
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction('window.HL && HL.ready', null, { timeout: 60000 });

  // 注入音频（setup 从 <input type=file> 读取解码）
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
    artImage: job.artImage || null,
    scheme: (job.scheme == null ? null : Number(job.scheme)),
  };
  const setup = await page.evaluate(async m => await window.__setup(m), meta);
  if (!setup.ok) { console.error('[setup 失败]', setup.error); process.exit(3); }
  console.log(`[setup] ${setup.total} 帧 / ${setup.dur.toFixed(2)}s / ${setup.lyrics} 行`);

  await page.evaluate(() => {
    window.__lyrLast = null;
    window.__lyrDbg = o => { window.__lyrLast = { frame: o.frame, t: +o.t.toFixed(4), idx: o.idx, anchor: o.anchor, target: +o.target.toFixed(2), scrollY: +o.scrollY.toFixed(3), bl: o.bl, act: o.act }; };
  });

  const data = await page.evaluate(async ({ centers, span }) => {
    const cv = document.querySelector('canvas');
    const ctx = cv.getContext('2d');
    const W = 880, H = 900, OX = 940, OY = 130;
    const cset = new Set(centers);
    function sample(i, wantShot) {
      const img = ctx.getImageData(OX, OY, W, H);
      const d = img.data;
      const bsum = new Float64Array(9), bcnt = new Float64Array(9);
      let sum = 0, n = 0;
      for (let y = 0; y < H; y += 3) {
        const b = Math.min(8, Math.floor(y / 100));
        for (let x = 0; x < W; x += 3) {
          const p = (y * W + x) * 4;
          const v = 0.299 * d[p] + 0.587 * d[p + 1] + 0.114 * d[p + 2];
          bsum[b] += v; bcnt[b]++; sum += v; n++;
        }
      }
      return { i, mean: +(sum / n).toFixed(3), bands: Array.from(bsum, (v, k) => +(v / bcnt[k]).toFixed(3)), dbg: window.__lyrLast };
    }
    const out = []; let cur = -1;
    window.__shots = {};
    for (const c of centers) {
      const lo = Math.max(0, c - span), hi = c + span;
      for (let k = cur + 1; k <= hi; k++) {
        window.__frame(k);
        if (k >= lo) out.push(sample(k, k === c));
        if (k === c) window.__shots[c] = cv.toDataURL('image/png');
      }
      cur = hi;
      await new Promise(r => setTimeout(r, 0));
      window.__prog = c;
    }
    return { samples: out, shots: window.__shots };
  }, { centers, span });

  const shots = data.shots || {};
  for (const k of Object.keys(shots)) {
    const b64 = shots[k].split(',')[1];
    fs.writeFileSync(`_shot_f${k}.png`, Buffer.from(b64, 'base64'));
    console.log(`[shot] _shot_f${k}.png`);
  }
  const data2 = data.samples;

  fs.writeFileSync(outFile, JSON.stringify(data2));
  console.log(`[done] ${data2.length} 帧采样 → ${outFile}`);
  await browser.close();
})().catch(e => { console.error('[fatal]', e); process.exit(5); });
