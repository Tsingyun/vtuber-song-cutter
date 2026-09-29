/* 长片段真实码率基准：渲染 30s 活跃段落 → 编码 → 落盘 → 用真实文件大小算码率。
 * 目的：判定浏览器 VideoEncoder 是否真的无视 bitrate（此前 _bench_cbr.cjs 的 mbps 分母用了
 *       渲染墙钟时间，属测量口径错误，结论需要复核）。
 * 用法: node _bench_long.cjs --job _render_job_v9.json --res 2 --from 3000 --frames 1800
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

function argOf(n) { const i = process.argv.indexOf('--' + n); return i >= 0 ? process.argv[i + 1] : null; }

const OUTDIR = path.join(__dirname, '_tmp4k');

(async () => {
  const job = JSON.parse(fs.readFileSync(argOf('job'), 'utf-8'));
  const RES = Number(argOf('res') || 2);
  const F0 = Number(argOf('from') || 3000);
  const NF = Number(argOf('frames') || 1800);
  const F1 = F0 + NF - 1;
  fs.mkdirSync(OUTDIR, { recursive: true });

  const store = {};
  let seq = 0, pending = [];
  const server = http.createServer((req, res) => {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', '*');
    if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }
    if (req.method === 'POST') {
      const tag = req.url.split('/').pop().split('?')[0];
      const chunks = [];
      req.on('data', c => chunks.push(c));
      req.on('end', () => {
        const buf = Buffer.concat(chunks);
        store[tag] = buf.length;
        const p = path.join(OUTDIR, 'bench_' + tag + '.mp4');
        fs.writeFileSync(p, buf);
        res.writeHead(200); res.end('OK');
        while (pending.length) pending.shift()();
      });
    } else { res.writeHead(200); res.end('bench'); }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}/up/`;
  const waitFor = tag => new Promise(res => { if (store[tag] != null) return res(store[tag]); pending.push(res); });

  const browser = await chromium.launch({
    args: ['--mute-audio', '--autoplay-policy=no-user-gesture-required', '--disable-dev-shm-usage',
           '--force-device-scale-factor=1', '--disable-renderer-backgrounding',
           '--disable-backgrounding-occluded-windows', '--disable-background-timer-throttling',
           '--enable-gpu'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  await page.goto('file:///' + path.join(job.playerDir, 'index.html').replace(/\\/g, '/') + '?render=1&res=' + RES,
                  { waitUntil: 'load' });
  await page.waitForFunction('window.HL && HL.ready', null, { timeout: 60000 });
  await page.setInputFiles('#mediaFile', job.audio);
  const meta = { title: job.title, artist: job.artist, trackLabel: job.trackLabel, romaji: job.romaji,
                 vocal: job.vocal, producer: job.producer, tags: job.tags, note: job.note,
                 footerLeft: job.footerLeft, footerRight: job.footerRight, lrc: job.lrc,
                 cover: job.cover, scheme: job.scheme, artImage: job.artImage, bitrate: job.bitrate,
                 badge: job.badge, mark: job.mark, sticker: null, watermark: null };
  for (const [k, f] of [['sticker', 'assets/sticker.png'], ['watermark', 'assets/watermark.png']]) {
    const p = path.join(job.playerDir, f);
    if (fs.existsSync(p)) meta[k] = 'data:image/png;base64,' + fs.readFileSync(p).toString('base64');
  }
  const st = await page.evaluate(async m => await window.__setup(m), meta);
  if (!st.ok) { console.error('[setup]', st.error); await browser.close(); server.close(); process.exit(3); }

  const cases = [
    ['vbr18', { bitrate: 18e6 }],
    ['cbr18', { bitrate: 18e6, bitrateMode: 'constant' }],
    ['vbr40', { bitrate: 40e6 }],
  ];

  const rows = [];
  for (const [tag, extra] of cases) {
    const r = await page.evaluate(async ({ F0, F1, up, extra, tag }) => {
      const OW = cv.width, OH = cv.height;
      const LVL = OW >= 2560 ? '34' : '2a';
      const cfg = Object.assign({ codec: 'avc1.6400' + LVL, width: OW, height: OH,
                                  framerate: 60, latencyMode: 'quality',
                                  hardwareAcceleration: 'prefer-software' }, extra);
      const sup = await VideoEncoder.isConfigSupported(cfg);
      if (!sup || !sup.supported) return { ok: false, why: '不支持 ' + JSON.stringify(sup && sup.config) };
      const muxer = new Mp4Muxer.Muxer({
        target: new Mp4Muxer.ArrayBufferTarget(),
        video: { codec: 'avc', width: OW, height: OH, frameRate: 60 },
        fastStart: 'in-memory',
      });
      let err = null;
      const enc = new VideoEncoder({ output: (c, m) => muxer.addVideoChunk(c, m), error: e => { err = String(e); } });
      enc.configure(cfg);
      const t0 = performance.now();
      for (let i = F0; i <= F1; i++) {
        window.__frame(i);
        const vf = new VideoFrame(cv, { timestamp: Math.round((i - F0) * 1e6 / 60), duration: Math.round(1e6 / 60) });
        enc.encode(vf, { keyFrame: i === F0 });
        vf.close();
        while (enc.encodeQueueSize > 8) await new Promise(r => setTimeout(r, 1));
      }
      await enc.flush(); enc.close();
      const wall = (performance.now() - t0) / 1000;
      if (err) return { ok: false, why: err };
      muxer.finalize();
      const blob = new Blob([muxer.target.buffer], { type: 'video/mp4' });
      const resp = await fetch(up + tag, { method: 'POST', body: blob });
      return { ok: resp.ok, frames: F1 - F0 + 1, size: blob.size, wall: +wall.toFixed(1), cfg: sup.config };
    }, { F0, F1, up: base, extra, tag });
    if (!r.ok) { console.log(`  ${tag.padEnd(8)} 失败：${r.why}`); continue; }
    await waitFor(tag);
    const dur = r.frames / 60;
    const mbps = r.size * 8 / dur / 1e6;
    rows.push({ tag, mbps, size: r.size, wall: r.wall, target: extra.bitrate / 1e6, extra });
    console.log(`  ${tag.padEnd(8)} 目标 ${String(extra.bitrate / 1e6).padStart(4)} Mbps → 实际 ${mbps.toFixed(2).padStart(6)} Mbps` +
                `  ${(r.size / 1048576).toFixed(1)} MB / ${dur.toFixed(1)}s  渲染+编码 ${r.wall}s`);
  }

  console.log(`\n=== 结论（${F0}..${F1}，${NF} 帧 = ${(NF / 60).toFixed(1)}s 活跃段）===`);
  for (const r of rows) {
    const ratio = (r.mbps / r.target * 100).toFixed(0);
    console.log(`  ${r.tag}: 达成率 ${ratio}%`);
  }
  await browser.close();
  server.close();
})().catch(e => { console.error('[fatal]', e); process.exit(5); });
