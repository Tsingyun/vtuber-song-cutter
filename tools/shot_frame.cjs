/* 4K 改造验证工具
 *  1) 按指定 res 渲染目标帧（从 f0 顺序渲染预热缓动状态，禁跳帧）→ 存 PNG
 *  2) 在 4K 分辨率下基准测试各编码器候选配置（决定软编/硬编优先级）
 * 用法: node _shot4k.cjs --job _render_job_v9.json --res 2 --frames 200,5110 --out _tmp4k/r2 [--bench]
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

function argOf(n) { const i = process.argv.indexOf('--' + n); return i >= 0 ? process.argv[i + 1] : null; }
const has = n => process.argv.includes('--' + n);

(async () => {
  const job = JSON.parse(fs.readFileSync(argOf('job'), 'utf-8'));
  const RES = Number(argOf('res') || 1);
  const frames = (argOf('frames') || '200').split(',').map(Number).sort((a, b) => a - b);
  const outDir = argOf('out');
  fs.mkdirSync(outDir, { recursive: true });

  const browser = await chromium.launch({
    args: ['--mute-audio', '--autoplay-policy=no-user-gesture-required', '--disable-dev-shm-usage',
           '--force-device-scale-factor=1', '--disable-renderer-backgrounding',
           '--disable-backgrounding-occluded-windows', '--disable-background-timer-throttling',
           '--enable-gpu', '--enable-hardware-encoder-state'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  page.on('console', m => { if (m.type() === 'error') console.error('[console.error]', m.text()); });

  const url = 'file:///' + path.join(job.playerDir, 'index.html').replace(/\\/g, '/') + '?render=1&res=' + RES;
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction('window.HL && HL.ready', null, { timeout: 60000 });
  await page.setInputFiles('#mediaFile', job.audio);
  const meta = {
    title: job.title, artist: job.artist, trackLabel: job.trackLabel, romaji: job.romaji,
    vocal: job.vocal, producer: job.producer, tags: job.tags, note: job.note,
    footerLeft: job.footerLeft, footerRight: job.footerRight,
    badge: job.badge, mark: job.mark,
    lrc: job.lrc, cover: job.cover, scheme: job.scheme, bitrate: job.bitrate,
    sticker: null, watermark: null, artImage: job.artImage,
  };
  // 贴图用 dataURL（file:// 图会污染 canvas）
  for (const [k, f] of [['sticker', 'assets/sticker.png'], ['watermark', 'assets/watermark.png']]) {
    const p = path.join(job.playerDir, f);
    if (fs.existsSync(p)) meta[k] = 'data:image/png;base64,' + fs.readFileSync(p).toString('base64');
  }
  const st = await page.evaluate(async m => await window.__setup(m), meta);
  console.log('[setup]', JSON.stringify(st));
  if (!st.ok) { await browser.close(); process.exit(3); }

  const info = await page.evaluate(() => ({ w: cv.width, h: cv.height, scale: typeof OUT_SCALE !== 'undefined' ? OUT_SCALE : -1 }));
  console.log(`[canvas] ${info.w}x${info.h}  OUT_SCALE=${info.scale}`);

  // 顺序渲染到各目标帧（预热缓动），命中即导出 PNG
  const saved = [];
  let cur = -1;
  const t0 = Date.now();
  for (const f of frames) {
    for (let k = cur + 1; k <= f; k++) {
      await page.evaluate(i => window.__frame(i), k);
      if (k % 600 === 0) {
        const el = (Date.now() - t0) / 1000;
        console.log(`  ...f${k}  ${(k / Math.max(0.001, el)).toFixed(1)} fps  ${el.toFixed(0)}s`);
      }
    }
    cur = f;
    const dataUrl = await page.evaluate(() => cv.toDataURL('image/png'));
    const out = path.join(outDir, `f${f}.png`);
    fs.writeFileSync(out, Buffer.from(dataUrl.split(',')[1], 'base64'));
    saved.push(out);
    console.log(`[shot] f${f} → ${out}`);
  }
  const el = (Date.now() - t0) / 1000;
  console.log(`[render] 共 ${cur + 1} 帧，耗时 ${el.toFixed(1)}s（${((cur + 1) / el).toFixed(1)} fps，仅绘制）`);

  if (has('bench')) {
    const bench = await page.evaluate(async () => {
      const OW = cv.width, OH = cv.height;
      const src = document.createElement('canvas');
      src.width = OW; src.height = OH;
      src.getContext('2d').drawImage(cv, 0, 0);
      const LVL = OW >= 2560 ? '34' : '2a';
      const cands = [
        ['High@5.2 软编', { codec: 'avc1.6400' + LVL, width: OW, height: OH, bitrate: 18e6, framerate: 60, latencyMode: 'quality', hardwareAcceleration: 'prefer-software' }],
        ['High@5.2 默认', { codec: 'avc1.6400' + LVL, width: OW, height: OH, bitrate: 18e6, framerate: 60, latencyMode: 'quality' }],
        ['High@5.2 硬编', { codec: 'avc1.6400' + LVL, width: OW, height: OH, bitrate: 18e6, framerate: 60, latencyMode: 'quality', hardwareAcceleration: 'prefer-hardware' }],
        ['High@5.2 硬编 p7-like', { codec: 'avc1.6400' + LVL, width: OW, height: OH, bitrate: 18e6, framerate: 60, hardwareAcceleration: 'prefer-hardware' }],
      ];
      const res = [];
      for (const [name, cfg] of cands) {
        try {
          const sup = await VideoEncoder.isConfigSupported(cfg);
          if (!sup || !sup.supported) { res.push({ name, ok: false, why: '不支持' }); continue; }
          let n = 0, bytes = 0, failed = null;
          const enc = new VideoEncoder({ output: c => { n++; bytes += c.byteLength; }, error: e => { failed = String(e); } });
          enc.configure(cfg);
          const N = 120, t = performance.now();
          for (let i = 0; i < N; i++) {
            const vf = new VideoFrame(src, { timestamp: Math.round(i * 1e6 / 60), duration: 16667 });
            enc.encode(vf, { keyFrame: i % 30 === 0 }); vf.close();
          }
          await enc.flush(); enc.close();
          const dt = (performance.now() - t) / 1000;
          res.push({ name, ok: !failed, fps: +(N / dt).toFixed(1), mbps: +(bytes * 8 / dt / 1e6).toFixed(2), err: failed });
        } catch (e) { res.push({ name, ok: false, why: String(e).slice(0, 120) }); }
      }
      return res;
    });
    console.log('\n=== 编码器基准（同一帧重复编码 120 帧）===');
    for (const b of bench) {
      console.log(b.ok ? `  ${b.name.padEnd(16)} ${String(b.fps).padStart(6)} fps   实测码率 ${b.mbps} Mbps`
                       : `  ${b.name.padEnd(16)} 不可用（${b.why || b.err || '?'}）`);
    }
    console.log('  注：静态同帧的码率不代表成片码率，仅供编码速度取舍。');
  }

  await browser.close();
})().catch(e => { console.error('[fatal]', e); process.exit(5); });
