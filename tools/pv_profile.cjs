/* pv_profile.cjs —— 逐帧成本剖面（绘制 / JPEG / 传输 三段分离）
 *
 * 为什么要这个：整片耗时只有一个总数（例如 20m45s / 6965 帧），但「慢在画、
 * 慢在 toBlob、还是慢在 HTTP」决定了完全不同的优化方向。整片跑一次才知道太贵，
 * 于是用固定帧数、固定起点做微剖面，改一行代码就能重测对比。
 *
 * 用法：
 *   node tools/pv_profile.cjs --job _tmp/pv/job_final4k_v4.json --frames 60 --t0 30
 *        [--res 2] [--q 0.98] [--cpu 1] [--nofetch] [--repeat]
 *
 * 输出：每帧一行的 CSV（--csv 指定路径）+ 尾部汇总（均值 / 中位 / p95）。
 * 判据：draw 是优化引擎能动的，blob 是编码格式能动的，xfer 是传输方式能动的。
 */
const fs = require('fs');
const path = require('path');
const http = require('http');
const { chromium } = require('playwright');

function argOf(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 ? process.argv[i + 1] : dflt;
}
const has = name => process.argv.includes('--' + name);

const JOB = argOf('job');
if (!JOB) { console.error('缺少 --job'); process.exit(2); }
const job = JSON.parse(fs.readFileSync(JOB, 'utf-8'));
const RES = Number(argOf('res', job.res || 2));
const FPS = Number(job.fps || 30);
const N = Number(argOf('frames', 60));
const T0 = Number(argOf('t0', 20));
const Q = Number(argOf('q', 0.98));
const CPU = argOf('cpu', '0');
const TOUCHDUP = argOf('touchdup', '1');
/* --cellcache：模拟驱动器「一格一编码」（同 koma 格复用上一帧 JPEG 字节） */
const CELLCACHE = has('cellcache');
const NOFETCH = has('nofetch');
const CSV = argOf('csv', '');

const playerDir = job.playerDir || path.join(__dirname, '..', 'renderer', 'player');
const page = job.page || 'index_pv.html';

// 收帧端点：只为量传输成本，不落盘
let recvBytes = 0;
const server = http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', '*');
  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }
  const chunks = [];
  req.on('data', c => chunks.push(c));
  req.on('end', () => { recvBytes += Buffer.concat(chunks).length; res.writeHead(200); res.end('OK'); });
});

const med = a => { const s = [...a].sort((x, y) => x - y); return s.length ? s[Math.floor(s.length / 2)] : 0; };
const p95 = a => { const s = [...a].sort((x, y) => x - y); return s.length ? s[Math.min(s.length - 1, Math.floor(s.length * 0.95))] : 0; };
const avg = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0;

(async () => {
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  const up = `http://127.0.0.1:${port}/u`;
  const url = 'file:///' + path.join(playerDir, page).replace(/\\/g, '/') +
    `?render=1&res=${RES}&cpu=${CPU}&touchdup=${TOUCHDUP}`;

  const browser = await chromium.launch({
    args: ['--mute-audio', '--disable-dev-shm-usage', '--force-device-scale-factor=1',
      '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
      '--disable-background-timer-throttling', '--js-flags=--expose-gc',
      '--allow-file-access-from-files'],
  });
  const pg = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  pg.on('pageerror', e => console.error('[pageerror]', e.message));
  pg.on('console', m => { if (m.type() === 'error') console.error('[console.error]', m.text()); });
  await pg.goto(url, { waitUntil: 'load' });
  await pg.waitForFunction('window.HL && HL.ready', null, { timeout: 60000 });
  const meta = { storyboard: job.storyboard, live: job.live || null,
    pvDur: job.pvDur, fps: FPS, title: job.title || '', artist: job.artist || '' };
  const st = await pg.evaluate(m => window.__setup(m), meta);
  if (!st.ok) { console.error('setup 失败', st.error); process.exit(3); }

  /* --nofx=vignette,grain：把指定的全画布后期关掉，用来量它们各自的成本。
     只影响本次剖面（monkey-patch），不改仓库代码。 */
  const NOFX = (argOf('nofx', '') || '').split(',').filter(Boolean);
  if (NOFX.length) {
    await pg.evaluate(list => {
      if (list.includes('vignette') && window.PV && window.PV.FX) window.PV.FX.vignette = () => {};
      if (list.includes('grain') && window.PV) window.PV.drawGrain = () => {};
    }, NOFX);
    console.log('[profile] 关掉后期：' + NOFX.join(','));
  }
  const f0 = Math.max(0, Math.round(T0 * FPS));
  const rows = [];
  const res = await pg.evaluate(async ({ f0, N, q, up, nofetch, cellcache }) => {
    const out = [];
    const cvEl = document.getElementById('cv');
    // 快进到起点（与成片路径一致：只为推排版状态）
    for (let i = 0; i < f0; i++) await window.__frame(i, { fast: 1 });
    let ck = null, cbuf = null;
    for (let k = 0; k < N; k++) {
      const i = f0 + k;
      const tD0 = performance.now();
      const kk = (typeof window.__komaOf === 'function') ? window.__komaOf(i) : i;
      let blobMs = 0, abMs = 0, buf, draw = 0;
      if (cellcache && cbuf && kk === ck) {
        if (typeof window.__tick === 'function') window.__tick(i); else await window.__frame(i);
        buf = cbuf;                                  // 复用：本帧零光栅、零编码
      } else {
        const pend = window.__frame(i);
        if (pend && pend.then) await pend;
        draw = performance.now() - tD0;
        const tB0 = performance.now();
        const blob = await new Promise(r => cvEl.toBlob(r, 'image/jpeg', q));
        blobMs = performance.now() - tB0;
        const tA0 = performance.now();
        buf = await blob.arrayBuffer();
        abMs = performance.now() - tA0;
        ck = kk; cbuf = buf;
      }
      let xfer = 0;
      if (!nofetch) {
        const tX0 = performance.now();
        const r = await fetch(up + '?i=' + i, { method: 'POST', body: buf });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        xfer = performance.now() - tX0;
      }
      out.push({ i, draw, blob: blobMs, ab: abMs, xfer, bytes: buf.byteLength });
    }
    return out;
  }, { f0, N, q: Q, up, nofetch: NOFETCH, cellcache: CELLCACHE });

  const dims = await pg.evaluate(() => {
    const c = document.getElementById('cv');
    return { w: c.width, h: c.height, info: window.__pvInfo ? window.__pvInfo() : null };
  });
  await browser.close();
  server.close();

  rows.push(...res);
  console.log(`\n[profile] page=${page} res=${RES} ${dims.w}x${dims.h} q=${Q} cpu=${CPU} touchdup=${TOUCHDUP} ` +
    `frames=${N} t0=${T0}s nofetch=${NOFETCH}`);
  const cols = ['draw', 'blob', 'ab', 'xfer'];
  for (const c of cols) {
    const a = res.map(r => r[c]);
    console.log(`  ${c.padEnd(5)} avg ${avg(a).toFixed(1).padStart(7)} ms   med ${med(a).toFixed(1).padStart(7)} ms   p95 ${p95(a).toFixed(1).padStart(7)} ms`);
  }
  const tot = res.map(r => r.draw + r.blob + r.ab + r.xfer);
  console.log(`  total avg ${avg(tot).toFixed(1)} ms/帧 → 等效 ${(1000 / avg(tot)).toFixed(1)} fps/实例`);
  console.log(`  cellcache=${CELLCACHE}`);
  console.log(`  jpeg ${(avg(res.map(r => r.bytes)) / 1024).toFixed(0)} KB/帧  ` +
    `单实例整片(按本剖面) ${(avg(tot) * 232.17 / 1000 / 60).toFixed(1)} min/首(232s)`);
  if (CSV) {
    fs.writeFileSync(CSV, 'i,draw_ms,blob_ms,ab_ms,xfer_ms,bytes\n' +
      rows.map(r => [r.i, r.draw.toFixed(2), r.blob.toFixed(2), r.ab.toFixed(2), r.xfer.toFixed(2), r.bytes].join(',')).join('\n'));
    console.log('  csv → ' + CSV);
  }
})().catch(e => { console.error('[fatal]', e); process.exit(5); });
