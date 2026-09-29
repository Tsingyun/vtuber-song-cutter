/* render_song.cjs —— 离线逐帧渲染驱动器
 * 用法: node render_song.cjs --job job.json [--encoder ffmpeg|webcodecs] [--preset fast]
 * job: { playerDir, audio, cover?, lrc?, title, artist?, trackLabel?, tags?, note?,
 *        footerLeft?, footerRight?, res?, bitrate?, encoder?, out }
 *
 * 流程: Playwright 无头加载 player/index.html?render=1&res=N → 注入素材与歌词
 *       → __setup（解码音频/频谱预计算/字体加载）→ 逐帧渲染 → 编码 → 落盘。
 *
 * 编码器两条路：
 *   ffmpeg  （4K 默认）画布渲染 → 逐帧 JPEG(q98) 经本地 HTTP 送给 node →
 *                       写入 ffmpeg image2pipe → libx264。
 *     为什么必须这样：Chromium 的软件 H.264（OpenH264）码率控制饱和，实测目标 18 Mbps
 *     只能产出 4.24 Mbps、目标 40 Mbps 只有 5.90 Mbps，bitrateMode:'constant' 亦被忽略，
 *     无法满足 B站 4K 不二压区间（16000~18500 kbps，超 19000 必二压）。
 *   webcodecs（1080P 回退 / 无 ffmpeg 环境）：页面内 WebCodecs 编码后 POST 回 node。
 *
 * 传输格式选型实测（4K，120 帧样本）：
 *     PNG 4.2fps / raw RGBA 0.8~12.5fps / WebP q100 0.7fps / JPEG q98 15.5fps  → 取 JPEG q98
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn, execFileSync } = require('child_process');
const { chromium } = require('playwright');

/* 贴图以 dataURL 注入：file:// 图会污染 canvas，VideoFrame 拒绝 tainted 源。
   job.sticker / job.watermark 可给文件路径或 dataURL；缺省用播放器自带中性占位素材。 */
function readAsset(spec, fallbackPath) {
  if (spec) {
    if (/^data:/.test(spec)) return spec;
    try { return 'data:image/png;base64,' + fs.readFileSync(spec).toString('base64'); }
    catch (e) { /* 落到占位素材 */ }
  }
  try { return 'data:image/png;base64,' + fs.readFileSync(fallbackPath).toString('base64'); }
  catch (e) { return null; }
}

function argOf(name) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 ? process.argv[i + 1] : null;
}

/* ffmpeg 定位：环境变量优先，其次 PATH，最后项目常用绝对路径 */
function pickFfmpeg() {
  const cands = [process.env.FFMPEG_BIN, 'ffmpeg', 'D:/ffmpeg/bin/ffmpeg.exe'].filter(Boolean);
  for (const c of cands) {
    try { execFileSync(c, ['-version'], { stdio: 'ignore' }); return c; } catch (e) { /* 继续找 */ }
  }
  throw new Error('找不到 ffmpeg（可用环境变量 FFMPEG_BIN 指定）');
}

(async () => {
  const jobFile = argOf('job');
  if (!jobFile) { console.error('缺少 --job'); process.exit(2); }
  const job = JSON.parse(fs.readFileSync(jobFile, 'utf-8'));
  const playerDir = job.playerDir || path.join(__dirname, 'player');
  const out = job.out;
  if (!out) { console.error('job 缺少 out'); process.exit(2); }
  /* 输出倍率：1 → 1920×1080，2 → 3840×2160（4K60）。设计坐标恒为 1920×1080。 */
  const RES = Number(argOf('res') || job.res || 1);
  const BITRATE = Number(argOf('bitrate') || job.bitrate || 18000000);
  const ENCODER = argOf('encoder') || job.encoder || (RES >= 2 ? 'ffmpeg' : 'webcodecs');
  const PRESET = argOf('preset') || job.preset || 'fast';
  const JQ = Number(argOf('jq') || job.jpegQuality || 0.98);
  const outAbs = path.resolve(out);

  // 1. 本地收片服务（成片可能几百 MB，走 HTTP POST 落盘）
  let got = 0, recvBytes = 0, tFrame = 0;
  let resolveUpload;
  const uploadPromise = new Promise((res) => { resolveUpload = res; });

  /* ---- ffmpeg 通路：收帧 → 写 stdin（拥塞时延迟响应实现背压） ---- */
  let ff = null, ffDone = null;
  if (ENCODER === 'ffmpeg') {
    const FFMPEG = pickFfmpeg();
    // JPEG 为 BT.601 全范围(0~255)，视频需 BT.709 有限范围(16~235)：
    // 必须用 scale 显式做范围+矩阵转换，否则成片会发灰/发白。
    // x264 的 VUI（colorprim/transfer/colormatrix）只能通过 -x264-params 写入，
    // 否则 ffprobe 读出 unknown。nal-hrd=cbr 保证平均码率钉在目标值。
    const VUI = 'colorprim=bt709:transfer=bt709:colormatrix=bt709:force-cfr=1';
    const ffArgs = [
      '-y', '-hide_banner', '-loglevel', 'warning', '-stats',
      '-f', 'mjpeg', '-framerate', '60', '-i', 'pipe:0',
      '-vf', 'scale=in_range=pc:out_range=tv:in_color_matrix=bt601:out_color_matrix=bt709:flags=bicubic,format=yuv420p',
      '-c:v', 'libx264', '-preset', PRESET,
      '-b:v', String(BITRATE), '-minrate', String(BITRATE), '-maxrate', String(BITRATE), '-bufsize', '36000k',
      '-x264-params', 'nal-hrd=cbr:' + VUI,
      '-pix_fmt', 'yuv420p', '-profile:v', 'high',
      '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
      '-r', '60', '-an', '-movflags', '+faststart',
      outAbs,
    ];
    console.log(`[encoder] ffmpeg ${FFMPEG} / libx264 preset ${PRESET} / CBR ${(BITRATE / 1e6).toFixed(1)} Mbps`);
    ff = spawn(FFMPEG, ffArgs, { stdio: ['pipe', 'inherit', 'inherit'] });
    let spawnErr = '';
    ff.on('error', e => { spawnErr = String(e); });
    ffDone = new Promise((res, rej) => {
      ff.on('close', code => code === 0 ? res() : rej(new Error('ffmpeg 退出码 ' + code + ' ' + spawnErr)));
    });
  } else {
    console.log(`[encoder] webcodecs（页面内 H.264）`);
  }

  const server = http.createServer((req, res) => {
    // file:// 页面跨域上传：必须带 CORS 头（非 simple 请求还会先发 OPTIONS 预检）
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', '*');
    if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }
    if (req.method !== 'POST') { res.writeHead(200); res.end('song-render-server'); return; }
    if (req.url.startsWith('/done')) { res.writeHead(200); res.end('OK'); return; }
    const chunks = [];
    req.on('data', c => chunks.push(c));
    req.on('end', () => {
      const buf = Buffer.concat(chunks);
      if (ENCODER === 'ffmpeg') {
        recvBytes += buf.length; got++;
        if (tFrame && got % 600 === 0) {
          const el = (Date.now() - tFrame) / 1000;
          console.log(`[render] ${got} 帧 ${(got / el).toFixed(1)} fps ${(recvBytes / 1048576).toFixed(0)} MB`);
        }
        const ack = () => { res.writeHead(200); res.end('OK'); };
        try {
          if (!ff.stdin.write(buf)) ff.stdin.once('drain', ack); else ack();
        } catch (e) { res.writeHead(500); res.end('PIPE'); }
        return;
      }
      // webcodecs 通路：整片一次 POST
      try {
        fs.writeFileSync(out, buf);
        res.writeHead(200); res.end('OK');
        resolveUpload({ size: buf.length });
      } catch (e) { res.writeHead(500); res.end('WRITE_FAIL'); resolveUpload(Promise.reject(e)); }
    });
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  const uploadUrl = `http://127.0.0.1:${port}/upload`;
  const frameUrlBase = `http://127.0.0.1:${port}/f/`;

  // 2. 启动无头浏览器
  const browser = await chromium.launch({
    args: ['--mute-audio', '--autoplay-policy=no-user-gesture-required',
           '--disable-dev-shm-usage', '--force-device-scale-factor=1',
           // headless 会被视为后台页：定时器钳到 1s/渲染降频，必须全部关掉
           '--disable-renderer-backgrounding',
           '--disable-backgrounding-occluded-windows',
           '--disable-background-timer-throttling',
           '--enable-gpu'],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', e => console.error('[pageerror]', e.message));
  page.on('console', m => { if (m.type() === 'error') console.error('[console.error]', m.text()); });

  const url = 'file:///' + path.join(playerDir, 'index.html').replace(/\\/g, '/') + '?render=1&res=' + RES;
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForFunction('window.HL && HL.ready', null, { timeout: 60000 });

  // 3. 注入音频 + 元数据 + 歌词 + 封面
  await page.setInputFiles('#mediaFile', job.audio);
  const meta = {
    title: job.title || '',
    artist: job.artist || '',
    trackLabel: job.trackLabel || '',
    romaji: job.romaji || '',   // 英文名/罗马字辅助行（drawInfo 标题下方）
    vocal: job.vocal != null ? job.vocal : '',             // 演唱者（档案行 VOCAL）
    producer: job.producer || '',                            // P 主（术力口曲，档案行 P）
    tags: job.tags || '',
    note: job.note || '',
    footerLeft: job.footerLeft != null ? job.footerLeft : 'SONG ARCHIVE',
    footerRight: job.footerRight != null ? job.footerRight : '',
    badge: job.badge != null ? job.badge : '',   // 刊眉带品牌签名
    mark: job.mark != null ? job.mark : '',      // 品牌短标记
    lrc: job.lrc || null,
    cover: job.cover || null,   // dataURL（网易云专辑图）
    sticker: readAsset(job.sticker, path.join(playerDir, 'assets', 'sticker.png')),
    watermark: readAsset(job.watermark, path.join(playerDir, 'assets', 'watermark.png')),
    artImage: job.artImage || null,  // dataURL（右侧装饰层，与封面互不干扰）
    scheme: (job.scheme == null ? null : Number(job.scheme)),  // 标题装饰方案 0~4（自动匹配/手动覆盖）
    bitrate: BITRATE,           // 成片视频码率(bps)：4K 走 B站不二压区间 16~18.5 Mbps
  };
  const setup = await page.evaluate(async m => await window.__setup(m), meta);
  if (!setup.ok) { console.error('[setup 失败]', setup.error); await browser.close(); server.close(); if (ff) ff.kill(); process.exit(3); }
  console.log(`[setup] ${1920 * RES}x${1080 * RES} 时长 ${setup.dur.toFixed(2)}s，共 ${setup.total} 帧（60fps），` +
              `歌词 ${setup.lyrics} 行，码率 ${(BITRATE / 1e6).toFixed(1)} Mbps`);

  // 4. 渲染
  const t0 = Date.now();
  tFrame = t0;
  let result;
  if (ENCODER === 'ffmpeg') {
    result = await page.evaluate(async ({ up, q }) => {
      const total = HL.total;
      for (let i = 0; i < total; i++) {
        window.__frame(i);
        const blob = await new Promise(res => cv.toBlob(res, 'image/jpeg', q));
        if (!blob) return { ok: false, error: 'toBlob 返回 null' };
        const resp = await fetch(up, { method: 'POST', body: blob });
        if (!resp.ok) return { ok: false, error: 'HTTP ' + resp.status };
      }
      return { ok: true, frames: total, dur: total / 60 };
    }, { up: frameUrlBase, q: JQ });
  } else {
    // 后台轮询进度
    let done = false, lastLog = '';
    const progTimer = setInterval(async () => {
      if (done) return;
      try {
        const st = await page.evaluate(() => ({ phase: HL.phase, prog: HL.prog, total: HL.total, err: HL.err,
                                                 q: HL.q, cfg: HL.encCfg }));
        const line = `[render] ${st.phase} ${st.prog}/${st.total} 帧 (${(st.prog / Math.max(1, st.total) * 100).toFixed(1)}%) q=${st.q} cfg=${st.cfg || '?'}`;
        if (line !== lastLog) { console.log(line); lastLog = line; }
      } catch (e) { console.error('[probe 失败]', e.message && e.message.slice(0, 200)); }
    }, 5000);
    result = await page.evaluate(u => window.__render(u), uploadUrl);
    done = true;
    clearInterval(progTimer);
  }
  const secs = ((Date.now() - t0) / 1000).toFixed(1);
  await browser.close();
  server.close();

  if (!result.ok) {
    console.error('[render 失败]', result.error);
    if (ff) { try { ff.stdin.end(); } catch (e) {} ff.kill(); }
    process.exit(4);
  }

  // 5. 收尾
  let size;
  if (ENCODER === 'ffmpeg') {
    try { ff.stdin.end(); } catch (e) {}
    await ffDone;
    size = fs.statSync(outAbs).size;
  } else {
    const uploaded = await uploadPromise;
    size = uploaded.size;
  }
  const vbr = size * 8 / result.dur / 1e6;
  console.log(`[done] ${result.frames} 帧 / ${result.dur.toFixed(2)}s，耗时 ${secs}s，` +
              `成片 ${(size / 1048576).toFixed(1)} MB，视频码率 ${vbr.toFixed(2)} Mbps → ${outAbs}`);
})().catch(e => { console.error('[fatal]', e); process.exit(5); });
