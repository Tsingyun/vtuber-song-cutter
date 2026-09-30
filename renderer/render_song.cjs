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

/* JPEG 编码 Worker：接过主线程转移来的 ImageBitmap，用 OffscreenCanvas 编码后回传字节。
   ⚠ 主线程绘制必须串行（歌词排版有跨帧累积状态），只有 JPEG 编码能并行 —— 这是本管线最大的提速点。 */
const WORKER_SRC = `
let oc = null, c = null;
self.onmessage = async (e) => {
  const { bmp, i, type, q } = e.data;
  if (!oc) { oc = new OffscreenCanvas(bmp.width, bmp.height); c = oc.getContext('2d'); }
  c.drawImage(bmp, 0, 0);
  bmp.close();
  const bl = await oc.convertToBlob({ type, quality: q });
  const buf = await bl.arrayBuffer();
  self.postMessage({ i, buf }, [buf]);
};
`;

/* 静态文件服务的 MIME（播放器目录以 http:// 提供，file:// 下 Blob Worker 会被拒绝） */
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
               '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp',
               '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg',
               '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf',
               '.otf': 'font/otf', '.json': 'application/json', '.svg': 'image/svg+xml' };

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
  /* 视频编码器：h264_nvenc（NVENC 硬编，默认） / libx264（CPU 软编回退）。
     实测 4K60 同画质：x264 preset=fast 0.83×实时，NVENC p1 2.74×实时；
     PSNR 24.00 vs 24.00、SSIM 0.96075 vs 0.95997（差 0.0008，肉眼无差）。 */
  const VCODEC = argOf('vcodec') || job.vcodec || 'h264_nvenc';
  /* NVENC 的 -preset 与 x264 不是一套命名（p1 最快 ~ p7 最慢）；x264 的 preset 名直接透传。 */
  const NVPRESET = /^p[1-7]$/.test(PRESET) ? PRESET : (argOf('nvpreset') || 'p1');
  /* 并行渲染实例数（仅 ffmpeg 通路可用）。3 是实测拐点：13.1→25.2→33.7→34.8 fps（P=1/2/3/4）。 */
  const PAGES = ENCODER === 'ffmpeg' ? Math.max(1, Number(argOf('pages') || job.pages || 2)) : 1;
  const JQ = Number(argOf('jq') || job.jpegQuality || 0.98);
  const outAbs = path.resolve(out);

  // 1. 本地收片服务（成片可能几百 MB，走 HTTP POST 落盘）
  let got = 0, recvBytes = 0, tFrame = 0;
  let resolveUpload;
  const uploadPromise = new Promise((res) => { resolveUpload = res; });

  /* 并行渲染的帧重排缓冲：nextFrame = 下一个要喂给 ffmpeg 的帧号 */
  const pend = new Map();          // 帧号 -> Buffer（乱序到达）
  const held = [];                 // 因领先过多被挂起的 HTTP 响应
  let nextFrame = 0, writing = false;
  const LEAD = Number(argOf('lead') || 300);   // 允许某实例领先 nextFrame 的最大帧数
  function pump() {
    if (writing || !ff) return;
    while (pend.has(nextFrame)) {
      const b = pend.get(nextFrame); pend.delete(nextFrame); nextFrame++;
      try {
        if (!ff.stdin.write(b)) {
          writing = true;
          ff.stdin.once('drain', () => { writing = false; pump(); });
          return;
        }
      } catch (e) { return; }
    }
    for (let k = held.length - 1; k >= 0; k--) {
      if (held[k].i - nextFrame <= LEAD) { held[k].res.writeHead(200); held[k].res.end('OK'); held.splice(k, 1); }
    }
  }

  /* 两阶段的中间产物（setup 拿到总帧数后初始化） */
  let SEG_PER = 1;
  const partStreams = [], partMjpeg = [];

  /* ---- ffmpeg 通路 ----
   * PAGES=1：经典流式 —— 收帧即写 stdin，边渲染边编码。
   * PAGES>1（两阶段）：实测任何编码进程与多个渲染实例并存都会互拖崩
   *   （NVENC 版 13 fps / x264 版 7.5 fps，而纯渲染 33.7 fps、纯编码 50 fps）。
   *   故先并行渲染把 JPEG 段落盘，渲染完再并行编码各段 + concat 拼接。 */
  const TWO_PHASE = ENCODER === 'ffmpeg' && PAGES > 1;
  const FFMPEG = ENCODER === 'ffmpeg' ? pickFfmpeg() : null;
  const _os = require('os');
  let ff = null, ffDone = null;
  const encArgs = (VCODEC === 'h264_nvenc')
    ? ['-c:v', 'h264_nvenc', '-preset', NVPRESET, '-tune', 'hq', '-rc', 'cbr',
       '-b:v', String(BITRATE), '-minrate', String(BITRATE), '-maxrate', String(BITRATE), '-bufsize', '36000k']
    : ['-c:v', 'libx264', '-preset', PRESET,
       '-b:v', String(BITRATE), '-minrate', String(BITRATE), '-maxrate', String(BITRATE), '-bufsize', '36000k',
       '-x264-params', 'nal-hrd=cbr:colorprim=bt709:transfer=bt709:colormatrix=bt709:force-cfr=1'];
  /* JPEG 为 BT.601 全范围(0~255)，视频需 BT.709 有限范围(16~235)：
     必须用 scale 显式做范围+矩阵转换，否则成片会发灰/发白。x264 的 VUI 走 -x264-params，
     NVENC 不吃 x264 专有参数，VUI 一律走通用 -color* 系列。 */
  const outArgs = (isPart) => [
    '-vf', 'scale=in_range=pc:out_range=tv:in_color_matrix=bt601:out_color_matrix=bt709:flags=bicubic,format=yuv420p',
    ...encArgs, ...(isPart ? ['-g', '120'] : []),   // 分段时收紧 GOP，保证拼接边界落在 IDR 上
    /* ⚠ h264_nvenc 不写 transfer/primaries 的 VUI（ffprobe 读 None），用 bsf 直接注入 SPS；
       libx264 已通过 -x264-params 写全，无需此步。 */
    ...(VCODEC === 'h264_nvenc' ? ['-bsf:v', 'h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1'] : []),
    '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-level:v', '5.2',
    '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-color_range', 'tv',
    '-r', '60', '-an', '-movflags', '+faststart',
  ];
  function spawnFfmpeg(inputSrc, outPath, isPart) {
    const args = ['-y', '-hide_banner', '-loglevel', 'warning', '-stats',
                  '-f', 'mjpeg', '-framerate', '60',
                  '-i', inputSrc === 'pipe' ? 'pipe:0' : inputSrc,
                  ...outArgs(isPart), outPath];
    const f = spawn(FFMPEG, args, { stdio: [inputSrc === 'pipe' ? 'pipe' : 'ignore', 'inherit', 'inherit'] });
    /* 编码是关键路径：渲染实例会与它抢 CPU，提高优先级保证编码不落后。 */
    try { _os.setPriority(f.pid, _os.constants.priority.PRIORITY_HIGH); }
    catch (e) { /* 不影响运行 */ }
    let spawnErr = '';
    f.on('error', e => { spawnErr = String(e); });
    const done = new Promise((res, rej) => {
      f.on('close', code => code === 0 ? res() : rej(new Error('ffmpeg 退出码 ' + code + ' ' + spawnErr)));
    });
    return { ff: f, done };
  }
  if (ENCODER === 'ffmpeg') {
    console.log(`[encoder] ffmpeg ${FFMPEG} / ${VCODEC === 'h264_nvenc' ? 'NVENC ' + NVPRESET : 'libx264 ' + PRESET} / CBR ${(BITRATE / 1e6).toFixed(1)} Mbps` +
                (TWO_PHASE ? ` / 两阶段 ×${PAGES}` : ' / 流式'));
    if (!TWO_PHASE) {
      ({ ff, done: ffDone } = spawnFfmpeg('pipe', outAbs, false));
    }
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
        /* 多实例（两阶段）：每实例自身有序，按帧号路由到对应段的 JPEG 落盘流。
           单实例：按帧号重排后喂给 ffmpeg stdin，用「领先上限」做背压防内存爆。 */
        const idx = Number((req.url.match(/[?&]i=(\d+)/) || [])[1]);
        if (Number.isFinite(idx)) {
          if (TWO_PHASE) {
            const p = Math.min(PAGES - 1, Math.floor(idx / SEG_PER));
            partStreams[p].write(buf);
            res.writeHead(200); res.end('OK');
            return;
          }
          pend.set(idx, buf);
          pump();
          if (idx - nextFrame > LEAD) { held.push({ i: idx, res }); return; }   // 等 pump 推进到允许范围再 ack
          res.writeHead(200); res.end('OK');
          return;
        }
        // 无帧号（兼容旧调用）：直接按到达顺序写
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
  const LAUNCH = {
    args: ['--mute-audio', '--autoplay-policy=no-user-gesture-required',
           '--disable-dev-shm-usage', '--force-device-scale-factor=1',
           // headless 会被视为后台页：定时器钳到 1s/渲染降频，必须全部关掉
           '--disable-renderer-backgrounding',
           '--disable-backgrounding-occluded-windows',
           '--disable-background-timer-throttling',
           '--enable-gpu'],
  };
  const url = 'file:///' + path.join(playerDir, 'index.html').replace(/\\/g, '/') + '?render=1&res=' + RES;

  // 3. 注入音频 + 元数据 + 歌词 + 封面
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
  /* 每个实例独立 Chromium（同一 browser 的多 page 可能共用 renderer 进程，拿不到并行）。
     实测 4K60：1 实例 13.1 fps → 2 实例 25.2 → 3 实例 33.7 → 4 实例 34.8（饱和）→ 默认 3。
     瓶颈是 cv.toBlob（72 ms/帧，单进程内不可并行：Worker 池实测反而更慢）。 */
  const openPage = async () => {
    const browser = await chromium.launch(LAUNCH);
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    page.on('pageerror', e => console.error('[pageerror]', e.message));
    page.on('console', m => { if (m.type() === 'error') console.error('[console.error]', m.text()); });
    await page.goto(url, { waitUntil: 'load' });
    await page.waitForFunction('window.HL && HL.ready', null, { timeout: 60000 });
    await page.setInputFiles('#mediaFile', job.audio);
    const st = await page.evaluate(async m => await window.__setup(m), meta);
    if (!st.ok) { await browser.close(); throw new Error('setup 失败: ' + (st.error || JSON.stringify(st))); }
    return { browser, page, setup: st };
  };

  const handles = [];
  try {
    for (let p = 0; p < PAGES; p++) handles.push(await openPage());
  } catch (e) {
    console.error('[setup 失败]', e.message);
    for (const h of handles) await h.browser.close();
    server.close(); if (ff) ff.kill();
    process.exit(3);
  }
  const setup = handles[0].setup;
  console.log(`[setup] ${1920 * RES}x${1080 * RES} 时长 ${setup.dur.toFixed(2)}s，共 ${setup.total} 帧（60fps），` +
              `歌词 ${setup.lyrics} 行，码率 ${(BITRATE / 1e6).toFixed(1)} Mbps，${PAGES} 个渲染实例`);

  // 4. 渲染
  const t0 = Date.now();
  tFrame = t0;
  let result;
  if (ENCODER === 'ffmpeg') {
    const total = setup.total;
    const segOf = p => [Math.round(total * p / PAGES), Math.round(total * (p + 1) / PAGES)];
    if (PAGES > 1) {
      console.log('[render] 分段 ' + Array.from({ length: PAGES }, (_, p) => segOf(p).join('~')).join(' | '));
      if (TWO_PHASE) {
        SEG_PER = Math.round(total / PAGES);
        for (let p = 0; p < PAGES; p++) {
          partMjpeg.push(outAbs + `.p${p}.mjpeg`);
          partStreams.push(fs.createWriteStream(partMjpeg[p], { highWaterMark: 8 * 1024 * 1024 }));
        }
      }
    }
    const rs = await Promise.all(handles.map(async (h, p) => {
      const [s, e] = segOf(p);
      return await h.page.evaluate(async ({ s, e, up, q }) => {
        // 歌词排版有跨帧累积状态，每个实例都必须从 f0 顺序快进到自己的起点（绘制仅 0.3 ms/帧）
        for (let i = 0; i < s; i++) window.__frame(i);
        for (let i = s; i < e; i++) {
          window.__frame(i);
          const blob = await new Promise(res => cv.toBlob(res, 'image/jpeg', q));
          if (!blob) return { ok: false, error: 'toBlob 返回 null @' + i };
          const resp = await fetch(up + '?i=' + i, { method: 'POST', body: blob });
          if (!resp.ok) return { ok: false, error: 'HTTP ' + resp.status + ' @' + i };
        }
        return { ok: true, n: e - s };
      }, { s, e, up: frameUrlBase, q: JQ });
    }));
    const bad = rs.find(r => !r.ok);
    result = bad || { ok: true, frames: total, dur: total / 60 };

    /* 阶段 2：并行编码各段 → concat 拼接（此时浏览器已关，编码独享资源） */
    if (TWO_PHASE && result.ok) {
      const tEnc = Date.now();
      await Promise.all(partStreams.map(s => new Promise(r => s.end(r))));
      const parts = [], encJobs = [];
      for (let p = 0; p < PAGES; p++) {
        const partOut = outAbs + `.p${p}.mp4`;
        parts.push(partOut);
        console.log(`[encode] 段 ${p + 1}/${PAGES} 启动编码`);
        encJobs.push(spawnFfmpeg(partMjpeg[p], partOut, true).done);
      }
      await Promise.all(encJobs);
      const listFile = outAbs + '.concat.txt';
      fs.writeFileSync(listFile, parts.map(pp => `file '${pp.replace(/\\/g, '/')}'`).join('\n') + '\n');
      const cc = spawn(FFMPEG, ['-y', '-hide_banner', '-loglevel', 'warning',
                                '-f', 'concat', '-safe', '0', '-i', listFile,
                                '-c', 'copy', '-movflags', '+faststart', outAbs],
                       { stdio: ['ignore', 'inherit', 'inherit'] });
      await new Promise((res, rej) => cc.on('close', code => code === 0 ? res() : rej(new Error('concat 退出码 ' + code))));
      console.log(`[encode] ${PAGES} 段编码 + 拼接完成，耗时 ${((Date.now() - tEnc) / 1000).toFixed(1)}s`);
      try {
        fs.unlinkSync(listFile);
        for (const pp of parts) fs.unlinkSync(pp);
        for (const m of partMjpeg) fs.unlinkSync(m);
      } catch (e) { console.log('[cleanup] 中间文件清理失败（不影响成片）:', e.message); }
    }
  } else {
    // 后台轮询进度
    let done = false, lastLog = '';
    const progTimer = setInterval(async () => {
      if (done) return;
      try {
        const st = await handles[0].page.evaluate(() => ({ phase: HL.phase, prog: HL.prog, total: HL.total, err: HL.err,
                                                 q: HL.q, cfg: HL.encCfg }));
        const line = `[render] ${st.phase} ${st.prog}/${st.total} 帧 (${(st.prog / Math.max(1, st.total) * 100).toFixed(1)}%) q=${st.q} cfg=${st.cfg || '?'}`;
        if (line !== lastLog) { console.log(line); lastLog = line; }
      } catch (e) { console.error('[probe 失败]', e.message && e.message.slice(0, 200)); }
    }, 5000);
    result = await handles[0].page.evaluate(u => window.__render(u), uploadUrl);
    done = true;
    clearInterval(progTimer);
  }
  const secs = ((Date.now() - t0) / 1000).toFixed(1);
  for (const h of handles) { try { await h.browser.close(); } catch (e) {} }
  server.close();

  if (!result.ok) {
    console.error('[render 失败]', result.error);
    if (ff) { try { ff.stdin.end(); } catch (e) {} ff.kill(); }
    process.exit(4);
  }

  // 5. 收尾
  let size;
  if (ENCODER === 'ffmpeg') {
    if (ff) { try { ff.stdin.end(); } catch (e) {} await ffDone; }
    size = fs.statSync(outAbs).size;
  } else {
    const uploaded = await uploadPromise;
    size = uploaded.size;
  }
  const vbr = size * 8 / result.dur / 1e6;
  console.log(`[done] ${result.frames} 帧 / ${result.dur.toFixed(2)}s，耗时 ${secs}s，` +
              `成片 ${(size / 1048576).toFixed(1)} MB，视频码率 ${vbr.toFixed(2)} Mbps → ${outAbs}`);
})().catch(e => { console.error('[fatal]', e); process.exit(5); });
