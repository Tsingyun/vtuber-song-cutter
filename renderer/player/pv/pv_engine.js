/*! pv_engine.js —— 文字 PV 合成器。
 *
 *  帧 = t 的纯函数：给定 storyboard + 素材，任意时刻 t 的画面唯一确定，
 *  与渲染顺序无关（所以可以分段并行渲染后直接拼接）。
 *
 *  图层序（下→上）：纸底 → 背景图形 → 人物(后) → 文字 → 人物(前) → 前景装饰 → 后期
 *  「文字穿过她」靠的就是人物分前后两次绘制，而不是给照片加蒙版。
 */
(() => {
  'use strict';
  const PV = window.PV;
  const { clamp, lerp, ease, rgba, mix } = PV;

  const ST = {
    sb: null, S: null, live: null,
    shots: [], seq: {}, figCache: {}, face: { cx: 0.5, cy: 0.5 },
    dur: 0, fps: 60, dpr: 1, ready: false,
    lay: new Map(),            // shot.i → layout 结果缓存（构图只算一次）
    stats: { glyphs: 0, shots: 0 },
  };
  PV.ST = ST;

  /* ---------------- 调色板 ---------------- */
  function buildStyle(pal, dpr) {
    /* 九角色（JIZURA 的 scheme 模型）：bg / fg / sub / accent / accent2 / ink / dim / ghostA / ghostB。
       旧字段 paper / ink 仍兼容（上一版的编排稿还在），但新编排稿一律走 bg / fg。
       这就是「一套配色」和「一套配色体系」的差别：
       以前只有 paper + ink + accent 三个位置，能表达的层次只有三层。 */
    const ink = PV.hex(pal.ink || pal.fg || '#F2F6FA');
    const paper = PV.hex(pal.paper || pal.bg || '#0A0D12');
    const accent = PV.hex(pal.accent || '#FFB067');
    const accent2 = PV.hex(pal.accent2 || pal.accent || '#FFFFFF');
    const sub = PV.hex(pal.sub || pal.ink || '#BDB6B2');
    const dim = PV.hex(pal.dim || pal.void || '#2A2A2E');
    const ghostA = PV.hex(pal.ghostA || '#39C6E0');
    const ghostB = PV.hex(pal.ghostB || '#C8373A');
    /* glow：晨光绘本的光晕色（halo/sunrise/windowLight 读它）。缺省回落 accent。 */
    const glow = PV.hex(pal.glow || pal.accent || '#FFD9A0');
    const voidc = dim;
    return {
      inkRGB: ink, paperRGB: paper, accentRGB: accent, acc2RGB: accent2,
      subRGB: sub, dimRGB: dim,
      ghostARGB: ghostA, ghostBRGB: ghostB, voidRGB: voidc,
      glowRGB: glow,
      ink: rgba(ink, 1), paper: rgba(paper, 1), accent: rgba(accent, 1),
      acc2: rgba(accent2, 1), sub: rgba(sub, 1), dim: rgba(dim, 1),
      ghostA: rgba(ghostA, 1), ghostB: rgba(ghostB, 1), glow: rgba(glow, 1),
      flat: !!pal.flat, texture: pal.texture || null,
      /* 深底还是浅底，决定了「分离字与底」的手法完全不同：
         浅底靠描边（暗边压在纸上），深底靠加粗 + 硬投影。
         同一套 bag 用错底，会在字周围留一圈与背景同色的暗缝，笔画被吃掉 2~3px，
         远看就成了「空心字」—— 这是用户判定「字体廉价」的直接来源。 */
      deep: (paper[0] * 0.299 + paper[1] * 0.587 + paper[2] * 0.114) < 118,
      _dpr: dpr,
    };
  }

  /* ---------------- 素材：图片序列 ----------------
     离线渲染是同步循环，<video> 的 seek 是异步的、会丢帧 —— 一律用图片序列。
     缺帧时回落到最近的已加载帧，绝不黑屏。 */
  function loadSeq(name, info) {
    if (!info || !info.dir) return null;
    ST.seq[name] = {
      dir: String(info.dir).replace(/\\/g, '/'), n: info.n | 0,
      fps: info.fps || 30, w: info.w || info.size || 0, h: info.h || info.size || 0,
      pattern: info.pattern || '%05d.jpg',
      frames: [], loaded: 0,
    };
    return ST.seq[name];
  }
  const seqPath = (s, i) => 'file:///' + s.dir + '/' + (s.pattern.indexOf('%') >= 0
    ? s.pattern.replace(/%0(\d)d/, (_, n) => String(i + 1).padStart(+n, '0'))
    : s.pattern.replace('{}', String(i + 1)));

  function getFrame(name, t) {
    const s = ST.seq[name];
    if (!s || !s.n) return null;
    let i = Math.round(t * s.fps);
    i = clamp(i, 0, s.n - 1);
    const im = s.frames[i];
    if (im && im.complete && im.naturalWidth) return im;
    // 回落半径按序列帧率换算成 0.25s：10fps 时是 ±3 帧，用固定 ±12 会跨到上一句去
    const RAD = Math.max(2, Math.round(s.fps * 0.25));
    for (let d = 1; d <= RAD; d++) {
      for (const j of [i - d, i + d]) {
        if (j < 0 || j >= s.n) continue;
        const q = s.frames[j];
        if (q && q.complete && q.naturalWidth) return q;
      }
    }
    return null;
  }
  /* 预载：**按镜头滑动装载，离开即释放**。
     全曲人物镜头多起来之后，「一次性把所有用到的帧解码进内存」是行不通的：
     232s × 8fps 的头窗 384² 就是 1.1GB，再加半身序列直接 OOM —— 而 4K 渲染
     期间没有余量给 GC。改成窗口式之后，内存上限 = 单个镜头的帧数 × 序列数
     （实测约 130MB），代价是每次换镜头要等一次解码（几十毫秒）。
     因为离线渲染是同步循环，没法 await，所以装载由 __frame 返回 Promise 驱动
     （render_song.cjs 会等它，见那边的 await）。 */
  let _winKey = null, _winChain = Promise.resolve();

  /* 预载窗口。**必须带 tOff**：分段抽出来的序列，帧 0 对应的是某个中间时刻
     （比如 21.41s），不是片子开头。窗口要按 (t + tOff) 换算 —— 漏了 tOff 时
     不报错，只是窗口整体落在序列之外，表现为「这一段里她一直没出现」，
     getFrame 静默返回 null，排查起来会一直怀疑素材没抽上。 */
  function spanOf(sh, name, tOff) {
    const s = ST.seq[name];
    if (!s) return null;
    const o = tOff || 0;
    return [Math.max(0, Math.floor((sh.t0 + o - 1.6) * s.fps)),
      Math.min(s.n - 1, Math.ceil((sh.t1 + o + 0.9) * s.fps))];
  }

  async function holdShot(sh) {
    const want = {};
    if (sh && sh.fig && sh.fig.seq && ST.seq[sh.fig.seq]) {
      want[sh.fig.seq] = spanOf(sh, sh.fig.seq, sh.fig.tOff);
    }
    let n = 0, bytes = 0;
    for (const name of Object.keys(ST.seq)) {
      const s = ST.seq[name];
      const w = want[name] || null;
      const a = w ? w[0] : -1, b = w ? w[1] : -2;
      // 释放：不在本镜窗口内的全部丢引用（decode 过的位图要能被回收）
      for (let i = 0; i < s.frames.length; i++) {
        if (i >= a && i <= b) continue;
        if (s.frames[i]) s.frames[i] = null;
      }
      if (!w) continue;
      for (let i = a; i <= b; i++) {
        if (s.frames[i]) continue;
        const im = new Image();
        im.src = seqPath(s, i);
        s.frames[i] = im;
        n++;
        bytes += (s.w || s.size || 512) * (s.h || s.size || 512) * 4;
      }
    }
    const jobs = [];
    for (const name of Object.keys(want)) {
      const s = ST.seq[name];
      const [a, b] = want[name];
      for (let i = a; i <= b; i++) {
        const im = s.frames[i];
        if (!im || (im.complete && im.naturalWidth)) continue;
        jobs.push(new Promise(res => {
          if (im.decode) im.decode().then(res, res);
          else { im.onload = res; im.onerror = res; }
        }));
      }
    }
    await Promise.all(jobs);
    return { n, bytes, jobs: jobs.length };
  }

  /* 换镜头时返回 Promise（调用方必须等），同镜头返回 null（零开销） */
  function ensureWindow(t) {
    const sh = shotAt(t);
    const key = sh ? String(sh.i) : 'none';
    if (key === _winKey) return null;
    _winKey = key;
    _winChain = _winChain.then(() => holdShot(sh)).catch(() => {});
    return _winChain;
  }
  PV.ensureWindow = ensureWindow;


  /* ---------------- 人物层 ----------------
     位置不是拍脑袋定的：先把文字排好，拿到 bbox，再把人物放到**离字最远且不出安全区**的锚点。
     「文字压在人身上」只在 storyboard 明确标 overlap 时才发生（全片限次），
     否则「文字没有严重遮挡」这条验收过不去。 */
  const SAFE = { m: 76 };
  const TAU = 6.283185307;

  /* 画框几何（2026-10-29 三版）：不再只有「圆窗 / 竖幅」两种。
     侧位面板族（arch 拱窗 / pill 胶囊 / soft 羽化面 / diag 斜切带）由 w·h 推位置，
     split 纵向分屏全高出画、strip 横向满幅条上下出血 —— 版面层据此让位。
     一切仍以 1920×1080 设计坐标描述，4K 渲染由外层 setTransform 统一放大。 */
  function placeFigure(fig, bbox) {
    const sc = fig.scale || 1;
    const mode = fig.mode || 'disc';

    /* 满幅淡底：整帧铺满，不占版面（长句专用，字照常全宽） */
    if (mode === 'wash') {
      return { kind: 'wash', shape: 'rect', mode, x: 0, y: 0, w: PV.W, h: PV.H };
    }
    /* 横向满幅条：贴顶或贴底出血。**已停用**（RULES 80）：
       16:9 的现场画面竖裁只剩 ~190 源行，切在哪儿取决于那一秒的姿势，
       十次里有三次读作「图被切坏了」。要横贯全宽请用 wash + veilK。 */
    if (mode === 'strip') {
      const h = (fig.h || 380) * sc;
      const top = fig.at === 'top';
      const bl = fig.bleed == null ? 0.22 : fig.bleed;      // 出血比例（条高的一部分露出画外）
      const y = top ? -h * bl : PV.H - h + h * bl;
      return { kind: 'panel', shape: 'rect', mode, x: -8, y, w: PV.W + 16, h };
    }
    /* 纵向分屏：全高出血，占一侧，字在另一侧 */
    if (mode === 'split') {
      const w = PV.W * (fig.wf || 0.46);
      const h = PV.H * (fig.hf || 1.12);
      const x = fig.side === 'l' ? -10 : PV.W - w + 10;
      return { kind: 'panel', shape: 'rect', mode, x, y: (PV.H - h) / 2, w, h };
    }
    /* 侧位面板族：按 w/h 定尺寸、贴安全边（y 是**中心**） */
    if (mode === 'arch' || mode === 'pill' || mode === 'soft' || mode === 'diag' || mode === 'round') {
      let w, h;
      if (mode === 'pill' || mode === 'round') { w = (fig.w || 760) * sc; h = (fig.h || 320) * sc; }
      else if (mode === 'diag') { w = PV.W * (fig.wf || 0.44); h = PV.H * (fig.hf || 0.88); }
      else if (mode === 'soft') { w = PV.W * (fig.wf || 0.48); h = PV.H * (fig.hf || 0.68); }
      else { h = (fig.h || 640) * sc; w = h * (fig.wRatio || 0.62); }
      const yc = (fig.y != null ? fig.y : 0.5) * PV.H;
      const x = fig.side === 'l' ? SAFE.m : PV.W - SAFE.m - w;
      const shape = mode === 'arch' ? 'arch'
        : (mode === 'pill' ? 'pill' : (mode === 'diag' ? 'poly' : 'round'));
      return { kind: 'panel', shape, mode, x, y: yc - h / 2, w, h,
               rad: fig.rad, skew: fig.skew == null ? -w * 0.14 : fig.skew };
    }

    /* 竖幅族（band / slab / ghost / crop）共用一个矩形框，差别只在尺寸与贴边方式：
       band  = 3:4 竖幅，完整露出人；
       slab  = 更高的整块，字从上面压过去；
       ghost = 极大、极淡，当作「印在纸上的影子」；
       crop  = 半边出画（日本 PV 里最常见的「被画框切掉的人」）。 */
    if (mode !== 'disc' && mode !== 'duo') {
      const h = (fig.h || 620) * sc;
      const w = h * (fig.wRatio || 0.752);
      const y = (fig.y != null ? fig.y : 0.30) * PV.H;
      let x;
      if (mode === 'crop') x = fig.side === 'r' ? PV.W - w * 0.54 : -w * 0.46;
      else if (mode === 'ghost') x = fig.side === 'r' ? PV.W - w - 26 : 26;
      else if (fig.fixed) x = fig.side === 'r' ? PV.W - w - 24 : 24;
      else x = fig.side === 'r' ? PV.W - SAFE.m - w : SAFE.m;
      return { x, y, w, h, kind: 'band', shape: 'rect', mode };
    }

    const r = (fig.r || 168) * sc;
    if (fig.fixed) {
      // 满幅构图（密排墙等）没有空白可避：按 side 定点，让字直接从人身上过
      const x = fig.side === 'r' ? PV.W - r - 40 : r + 40;
      const y = (fig.y != null ? fig.y : 0.44) * PV.H;
      return { x, y, r, kind: 'disc', mode };
    }
    /* 定点在**要求的这一侧**、y 只做小幅纵向避让。
       早先的六锚点「挑离字最远的那个」会无视 side：版面层把字推到右边、给左边
       留了空，圆窗却因为右侧加 26 分跑到右边去 —— 结果字在中间、人被挤到角上，
       留白全在反侧（实测 line 7 / line 20）。既然版面层已经按 side 让过位，
       这里就该老老实实待在那一侧，只在真的相交时沿边上下滑。 */
    const wantY = (fig.y != null ? fig.y : 0.44) * PV.H;
    const x = fig.side === 'l' ? SAFE.m + r + 16 : PV.W - SAFE.m - r - 16;
    const yMin = SAFE.m + r + 8, yMax = PV.H - SAFE.m - r - 8;
    const ys = [clamp(wantY, yMin, yMax), yMax, yMin, (yMin + yMax) / 2];
    let best = clamp(wantY, yMin, yMax), bestScore = -1e9;
    for (const y of ys) {
      const dx = Math.max(bbox[0] - (x + r), (x - r) - bbox[2], 0);
      const dy = Math.max(bbox[1] - (y + r), (y - r) - bbox[3], 0);
      let d = Math.hypot(dx, dy);
      if (dx === 0 && dy === 0) d = -Math.hypot((bbox[0] + bbox[2]) / 2 - x, (bbox[1] + bbox[3]) / 2 - y) * 0.5;
      const score = d - Math.abs(y - wantY) * 0.04;
      if (score > bestScore) { bestScore = score; best = y; }
    }
    return { x, y: best, r, kind: 'disc', mode };
  }

  /* 等比覆盖裁切：源图比例和框不一致时，宁可裁掉边缘也不要拉变形（拉变形一眼假）。
     fy 是裁切窗口在源图里的纵向定位（0=顶 1=底，默认 0.5 居中）——
     横向满幅条要靠它把头切在眼睛那一带，否则居中裁出来只剩鼻梁。 */
  function drawCoverF(ctx, im, dx, dy, dw, dh, zoom, fy) {
    const iw = im.naturalWidth || im.width || 1;
    const ih = im.naturalHeight || im.height || 1;
    const z = zoom || 1;
    const dr = dw / dh;
    let sw, sh;
    if (iw / ih > dr) { sh = ih / z; sw = sh * dr; } else { sw = iw / z; sh = sw / dr; }
    const sx = (iw - sw) / 2, sy = (ih - sh) * (fy == null ? 0.5 : clamp(fy, 0, 1));
    ctx.drawImage(im, sx, sy, sw, sh, dx, dy, dw, dh);
  }
  function drawCover(ctx, im, dx, dy, dw, dh, zoom) {
    drawCoverF(ctx, im, dx, dy, dw, dh, zoom, 0.5);
  }

  PV.drawCover = drawCover;   // 几何蒙版器件（photoCircle 等）需要等比覆盖裁切

  /* 双色调（duotone）：直播截图直接贴上去永远是「贴了一张照片」，
     因为它的色温、对比、噪点都和纸面无关。去饱和后压一个冷调阴影 + 提一个暖调高光，
     画面立刻读作「印在纸上的她」，而不是贴纸。 */
  function applyTone(g, w, h, S, fig) {
    const tone = fig.tone;
    if (!tone || tone === 'none') return;
    g.globalCompositeOperation = 'saturation';
    g.fillStyle = 'rgb(128,128,128)'; g.fillRect(0, 0, w, h);
    if (tone === 'mono') { g.globalCompositeOperation = 'source-over'; return; }
    /* 只提暖，不压暗：亮度交给 fig.a 控制。
       早先在暗部叠一道 multiply，结果整张脸灰成一片（既不像纸上的印，也不像照片），
       是这一版里最难看的一处。 */
    g.globalCompositeOperation = 'screen';
    g.globalAlpha = fig.toneHi == null ? 0.14 : fig.toneHi;
    g.fillStyle = rgba(mix(S.accentRGB, S.inkRGB, 0.55), 1);
    g.fillRect(0, 0, w, h);
    g.globalCompositeOperation = 'source-over';
    g.globalAlpha = 1;
  }

  /* 羽化遮罩：把矩形人物块的边缘揉开。
     硬边的照片贴上去永远读作「贴纸」—— 真正让人物「进画面」的不是调色，
     而是边缘没有一条刀切一样的直线。遮罩烘焙一次，之后 destination-in 贴上去。 */
  const _feather = {};
  function featherSprite(k) {
    const key = 'fe' + k.toFixed(3);
    if (_feather[key]) return _feather[key];
    const N = 256;
    const c = document.createElement('canvas'); c.width = c.height = N;
    const g = c.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, N, N);
    g.globalCompositeOperation = 'destination-out';
    const w = Math.max(1, Math.round(N * k));
    /* 每条边的渐变必须**沿着擦除方向**：左/右是横的，上/下是竖的。
       早先左/右写成了 (0,0)→(0,N)（竖向），于是左右两侧只擦掉角、没擦边 ——
       destination-in 之后仍留着一条直边，soft 模式因此永远「有框」。
       实测：a=0.05 时整张图几乎消失，但仍能看见一个矩形残影，就是这个。 */
    const edges = [
      [0, 0, w, N, 0, 0, w, 0],             // 左：白→透，横向
      [N - w, 0, w, N, N, 0, N - w, 0],     // 右：白→透，横向（向内）
      [0, 0, N, w, 0, 0, 0, w],             // 上：纵向
      [0, N - w, N, w, 0, N, 0, N - w],     // 下：纵向（向内）
    ];
    for (const [x, y, ww, hh, gx0, gy0, gx1, gy1] of edges) {
      const lg = g.createLinearGradient(gx0, gy0, gx1, gy1);
      lg.addColorStop(0, 'rgba(0,0,0,1)');
      lg.addColorStop(1, 'rgba(0,0,0,0)');
      g.fillStyle = lg; g.fillRect(x, y, ww, hh);
    }
    g.globalCompositeOperation = 'source-over';
    return (_feather[key] = c);
  }

  /* 纵向渐隐遮罩：画面上半透成纸、下半留住她。
     这是「原片当底景」最省事也最好看的一招 —— 不需要裁切，不会把脸切出一条缝，
     字永远落在上半那片干净的纸里。k = alpha 到达 1 的高度比例。 */
  const _veil = {};
  /* 曲线不是线性的：线性渐变在 1/3 处就有 33% 不透明度，字压上去立刻发灰
     （实测 veilk=0.6 时歌词区域仍有近四成底片）。改成「前 30% 全透明、
     30%~78% 用 1.6 次幂抬起来」—— 上半是干净的纸，下半她才是实的。 */
  function veilSprite(k) {
    const f = Math.max(0.35, Math.min(0.95, k));
    const key = 'v' + f.toFixed(3);
    if (_veil[key]) return _veil[key];
    const N = 256;
    const c = document.createElement('canvas'); c.width = 1; c.height = N;
    const g = c.getContext('2d');
    const gr = g.createLinearGradient(0, 0, 0, N);
    const s0 = 0.42;   // 起坡点：40% 以下是纯纸，歌词版心（cy 0.30）才真的干净
    for (let i = 0; i <= 12; i++) {
      const t = i / 12;
      const q = Math.max(0, Math.min(1, (t - s0) / (f - s0)));
      gr.addColorStop(t, 'rgba(0,0,0,' + Math.pow(q, 1.6).toFixed(4) + ')');
    }
    g.fillStyle = gr; g.fillRect(0, 0, 1, N);
    return (_veil[key] = c);
  }

  /* 带调色的人物位图：只有需要双色调时才走离屏，其余情况直接贴图（省一次拷贝） */
  function figBitmap(im, dw, dh, S, fig, zoom) {
    const k = Math.min(1, 760 / Math.max(dw, dh));
    const ow = Math.max(4, Math.round(dw * k)), oh = Math.max(4, Math.round(dh * k));
    const o = PV.offscreen('figTone', ow, oh, 1);
    drawCoverF(o.ctx, im, 0, 0, ow, oh, zoom, fig.fy);
    applyTone(o.ctx, ow, oh, S, fig);
    /* 有羽化的图，色温要叠在**羽化之前**：羽化之后再叠一块纸色矩形，
       那块矩形的直角会变成一条硬边 —— soft 模式因此长出一条「看不见的框」。
       叠在这里，边缘仍由遮罩决定，色温也跟着一起化开。 */
    if (fig.feather && fig.paper) tintPlate(o.ctx, fig, 0, 0, ow, oh, 1, S);
    if (fig.feather) {
      // destination-in：只保留遮罩里的部分（遮罩中心实、边缘透）
      o.ctx.globalCompositeOperation = 'destination-in';
      o.ctx.drawImage(featherSprite(fig.feather), 0, 0, ow, oh);
      o.ctx.globalCompositeOperation = 'source-over';
    }
    return o.c;
  }

  /* 画框形状：面板族共用一套路径构造器 —— drawFigure 用它 clip，边框/角标再 stroke 一次。
     inset 把描边收进框内 1~2px（描边居中画在路径上，不收会有一半落在裁剪外）。 */
  function roundRect(ctx, x, y, w, h, r) {
    const rr = Math.max(0, Math.min(r, Math.min(w, h) / 2));
    ctx.moveTo(x + rr, y);
    ctx.lineTo(x + w - rr, y);
    ctx.arcTo(x + w, y, x + w, y + rr, rr);
    ctx.lineTo(x + w, y + h - rr);
    ctx.arcTo(x + w, y + h, x + w - rr, y + h, rr);
    ctx.lineTo(x + rr, y + h);
    ctx.arcTo(x, y + h, x, y + h - rr, rr);
    ctx.lineTo(x, y + rr);
    ctx.arcTo(x, y, x + rr, y, rr);
    ctx.closePath();
  }

  function panelPath(ctx, b, inset) {
    const k = inset || 0;
    const x = b.x + k, y = b.y + k;
    const w = Math.max(2, b.w - k * 2), h = Math.max(2, b.h - k * 2);
    ctx.beginPath();
    if (b.shape === 'arch') {
      /* 拱窗：上半圆 + 直边。比圆窗更「建筑」、比方框更软，正好落在晨光绘本的语法里。 */
      const r = w / 2;
      ctx.moveTo(x, y + h);
      ctx.lineTo(x, y + r);
      ctx.arc(x + r, y + r, r, Math.PI, 0, false);
      ctx.lineTo(x + w, y + h);
      ctx.closePath();
      return;
    }
    if (b.shape === 'pill') { roundRect(ctx, x, y, w, h, Math.min(w, h) / 2); return; }
    if (b.shape === 'round') { roundRect(ctx, x, y, w, h, b.rad == null ? Math.min(w, h) * 0.06 : b.rad); return; }
    if (b.shape === 'poly') {
      /* 斜切带：上下边各平移一个 skew，读作「被裁纸刀斜着切下来的一条」。 */
      const s = b.skew || 0;
      const o = Math.abs(s);
      ctx.moveTo(x + Math.max(0, s), y);
      ctx.lineTo(x + Math.max(0, s) + (w - o), y);
      ctx.lineTo(x + Math.max(0, -s) + (w - o), y + h);
      ctx.lineTo(x + Math.max(0, -s), y + h);
      ctx.closePath();
      return;
    }
    ctx.rect(x, y, w, h);
  }

  /* 「印在纸上」而不是「贴上去」：直播画面是冷蓝粉、满饱和，纸是暖褐 ——
     硬边并置时第一眼读到的是「贴纸」，脸再清楚也不进画面。
     这里原地 multiply 一层纸色、再 screen 一层暖光，把色温拉回纸面。
     为什么不用 figBitmap 的 tone（离屏 saturation）：它内部会把离屏尺寸压到
     760px 再放大（figBitmap 第 355 行），band/crop 这种 1200px 的窗会重新采样
     一遍，等于拿「色温统一」换「画面发糊」。原地叠色两个 fillRect 就够，
     而且逐帧成本与窗面积无关（GPU 填充，不读源图）。
     k=1 时约 -30% 饱和 + 暖高光；再高脸就发灰，再低看得出色温打架。 */
  function tintPlate(ctx, fig, x, y, w, h, a, S) {
    const k = fig.paper;
    if (!k) return;
    ctx.save();
    ctx.globalCompositeOperation = 'multiply';
    ctx.globalAlpha = clamp(a * 0.30 * k, 0, 1);
    ctx.fillStyle = rgba(S.paperRGB, 1);
    ctx.fillRect(x, y, w, h);
    ctx.globalCompositeOperation = 'screen';
    ctx.globalAlpha = clamp(a * 0.13 * k, 0, 1);
    ctx.fillStyle = rgba(S.accentRGB, 1);
    ctx.fillRect(x, y, w, h);
    ctx.restore();
  }

  function drawFigure(ctx, fig, box, t, en, S, shot) {
    S = S || ST.S;
    const name = fig.seq || 'head';
    if (!ST.seq[name]) return;
    const t0 = t + (fig.tOff || 0);
    const im = getFrame(name, t0);
    if (!im) return;

    /* 呼吸 + 漂移：完全静止的贴图会读作「贴上去的」。幅度必须小到说不出哪里在动，
       但一旦去掉就立刻变呆 —— 这是「她在画面里」和「一张截图」的全部差别。 */
    const mo = fig.motion == null ? 1 : fig.motion;
    const ph = (fig.ph || 0) * TAU;
    const br = 1 + 0.009 * mo * Math.sin(t * 0.72 + ph);
    const ox = Math.sin(t * 0.23 + ph) * 7 * mo;
    const oy = Math.cos(t * 0.19 + ph * 1.7) * 5 * mo;

    /* 本镜头内的进度：推镜 / 遮罩揭示 / 叠化全是 t 的纯函数 ⇒ 双渲一致。 */
    const st0 = shot ? shot.t0 : 0, st1 = shot ? shot.t1 : 1e9;
    const p = shot ? clamp((t - st0) / Math.max(0.05, st1 - st0), 0, 1) : 0.5;
    const sm = q => q * q * (3 - 2 * q);

    /* 框内推镜：素材自己在动，框才不会读作「贴上去的一张图」 */
    const z0 = fig.zoom0 == null ? 1.05 : fig.zoom0;
    const zoom = z0 + (fig.zoom1 || 0) * sm(p);

    /* 入场：reveal（fade 叠化 / wipe 硬边擦开 / grow 遮罩长开），fadeIn 是纯柔叠 */
    let ra = 1, rev = null;
    if (fig.reveal) {
      const rd = fig.reveal.dur == null ? 0.55 : fig.reveal.dur;
      const rq = clamp((t - st0) / rd, 0, 1);
      if (rq < 1) {
        if (fig.reveal.k === 'grow') {
          rev = { k: 'grow', q: sm(rq) };
          ra = clamp(rq * 1.5, 0, 1);
        } else if (fig.reveal.k === 'wipe') {
          rev = { k: 'wipe', q: sm(rq), dir: fig.reveal.dir || 'L' };
        } else {
          ra = sm(rq);                          // fade = 叠化
        }
      }
    }
    if (fig.fadeIn) ra *= sm(clamp((t - st0) / fig.fadeIn, 0, 1));
    /* 退场叠化：切点上硬切一块照片最扎眼，末段淡出才接得住转场。
       同样受 _xcap 约束：照片停在 ~45% 把剩下的交给转场，否则字还半透明、
       照片已经清零，切点上照片会被快照 A 弹回来一次。 */
    if (fig.out) {
      const oc = shot && shot._xcap != null ? shot._xcap : 1;
      const ow = Math.max(0.04, fig.out * oc);
      const oq = clamp((t - (st1 - ow)) / ow, 0, 1) * oc;
      ra *= sm(1 - oq);
    }

    const a = (fig.a == null ? 1 : fig.a) * (1 + en * 0.06) * ra;
    if (a <= 0.004) return;
    ctx.save();
    ctx.globalAlpha = clamp(a, 0, 1);
    ctx.imageSmoothingQuality = 'high';

    /* 满幅淡底：整帧压成一层很淡的水彩，字照常全宽（长句专用）。
       lift 把纸色按比例盖回去 —— 不然照片的暗部会把歌词吃掉。 */
    if (box.kind === 'wash') {
      if (fig.veilK) {
        /* 纵向渐隐：整帧照贴，再用一条竖向渐变把上半挖空。
           源本来就是 960×540 的「失焦现场」，离屏这一道不会额外损失清晰度。 */
        const o = PV.offscreen('figVeil', PV.W, PV.H, 1);
        drawCoverF(o.ctx, im, 0, 0, PV.W, PV.H, zoom, fig.fy);
        /* 色温：满幅淡底同样要压纸色。源在 ffmpeg 侧已降到 s=0.42，但仍是冷蓝，
           不压的话整块纸会被拉冷，字看起来像浮在视频上而不是纸里。
           **必须在离屏里叠**：画到主 ctx 上会连没有图的那半边纸一起压暗（veil 之后透明区）。 */
        tintPlate(o.ctx, fig, 0, 0, PV.W, PV.H, 1, S);
        o.ctx.globalCompositeOperation = 'destination-in';
        o.ctx.drawImage(veilSprite(fig.veilK), 0, 0, PV.W, PV.H);
        o.ctx.globalCompositeOperation = 'source-over';
        ctx.drawImage(o.c, 0, 0, PV.W, PV.H);
      } else if (fig.tone || fig.feather) ctx.drawImage(figBitmap(im, PV.W, PV.H, S, fig, zoom), 0, 0, PV.W, PV.H);
      else drawCover(ctx, im, 0, 0, PV.W, PV.H, zoom);
      const lift = fig.lift == null ? 0 : fig.lift;
      if (lift > 0) {
        ctx.globalAlpha = clamp(lift * ra, 0, 1);
        ctx.fillStyle = rgba(S.paperRGB, 1);
        ctx.fillRect(0, 0, PV.W, PV.H);
      }
      ctx.restore();
      return;
    }

    if (box.kind === 'disc') {
      const x = box.x + ox, y = box.y + oy, r = box.r * br;
      ctx.save();
      if (rev) {
        /* 圆窗的揭示遮罩：grow 用同心圆长大，wipe 用矩形边擦（两条 clip 相交） */
        ctx.beginPath();
        if (rev.k === 'grow') ctx.arc(x, y, r * (0.55 + 0.45 * rev.q), 0, TAU);
        else if (rev.dir === 'R') ctx.rect(x - r, y - r, r * 2 * rev.q, r * 2);
        else if (rev.dir === 'U') ctx.rect(x - r, y + r - r * 2 * rev.q, r * 2, r * 2 * rev.q);
        else if (rev.dir === 'D') ctx.rect(x - r, y - r, r * 2, r * 2 * rev.q);
        else ctx.rect(x + r - r * 2 * rev.q, y - r, r * 2 * rev.q, r * 2);
        ctx.clip();
      }
      ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.clip();
      const D = r * 2 * 1.10;                    // 源图显示边长 = 直径×1.10（脸占 69%，边缘交给头发）
      if (fig.tone || fig.feather) ctx.drawImage(figBitmap(im, D, D, S, fig, zoom), x - D / 2, y - D / 2, D, D);
      else drawCover(ctx, im, x - D / 2, y - D / 2, D, D, zoom);
      tintPlate(ctx, fig, x - r, y - r, r * 2, r * 2, a, S);
      /* 内缘暗角：贴图而不是现算径向渐变（逐帧极坐标渐变会引入 ±1 的抖动差） */
      ctx.drawImage(PV.fadeSprite('discVig', S.voidRGB, [[0, 0], [0.52, 0], [0.94, 0.16], [1, 0.58]], false),
        x - r, y - r, r * 2, r * 2);
      ctx.restore();

      // 环：只有 disc 有，极细一圈；粗环会变成「头像框」
      /* 平涂底上要「硬边 + 硬线」：印刷品的图永远是切好的方块/圆，边缘带羽化就成了贴纸 */
      ctx.globalAlpha = S.flat ? 0.62 : (0.20 + en * 0.14);
      ctx.strokeStyle = S.ink; ctx.lineWidth = S.flat ? 2 : 1.2;
      ctx.beginPath(); ctx.arc(x, y, r + 0.8, 0, TAU); ctx.stroke();
      ctx.globalAlpha = clamp(a, 0, 1);

      /* duo：主窗旁边一个更小的「前一刻」，读作同一口气的延续，不是特效。
         两张脸同时出现时，小窗必须更淡更远，否则会变成画中画 UI。 */
      if (box.mode === 'duo') {
        const im2 = getFrame(name, t0 - 0.62);
        /* 小窗**竖着叠在主窗下方**：横着放会横跨画面中段，把整行歌词劈成两半
           （实测 137.5s 的两个圆正好压在句子中间）。竖排之后这一对只占画面一侧，
           文字可以独占另一侧 —— 也正是日本 PV 里「同一口气的前后两格」的排法。 */
        const r2 = r * 0.38, x2 = x + r * 0.16, y2 = y + r * 1.14;
        if (im2 && y2 + r2 < PV.H - 12) {
          ctx.save();
          ctx.globalAlpha = clamp(a * 0.5, 0, 1);
          ctx.beginPath(); ctx.arc(x2, y2, r2, 0, TAU); ctx.clip();
          drawCover(ctx, im2, x2 - r2, y2 - r2, r2 * 2, r2 * 2, 1.12);
          ctx.restore();
          ctx.globalAlpha = clamp(a * 0.30, 0, 1);
          ctx.strokeStyle = S.ink; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.arc(x2, y2, r2, 0, TAU); ctx.stroke();
        }
      }
    } else {
      const w = box.w * br, h = box.h * br;
      const x = box.x + ox - (w - box.w) / 2, y = box.y + oy - (h - box.h) / 2;
      const geo = { shape: box.shape || 'rect', x, y, w, h, rad: box.rad, skew: box.skew };
      ctx.save();
      /* 揭示遮罩分两道：先按 reveal 收一条擦除边，再按画框形状裁 —— 两道 clip 相交，
         读作「从框里长出来」，而不是「整块照片淡入」。 */
      if (rev) {
        if (rev.k === 'grow') {
          const k = 0.86 + 0.14 * rev.q;
          ctx.translate(x + w / 2, y + h / 2); ctx.scale(k, k);
          ctx.translate(-(x + w / 2), -(y + h / 2));
        } else {
          const e = rev.q;
          ctx.beginPath();
          if (rev.dir === 'R') ctx.rect(x - 2, y - 2, (w + 4) * e, h + 4);
          else if (rev.dir === 'U') ctx.rect(x - 2, y + h + 2 - (h + 4) * e, w + 4, (h + 4) * e);
          else if (rev.dir === 'D') ctx.rect(x - 2, y - 2, w + 4, (h + 4) * e);
          else ctx.rect(x + w + 2 - (w + 4) * e, y - 2, (w + 4) * e, h + 4);
          ctx.clip();
        }
      }
      /* soft = 「没有框的一个人」：它存在于纸里，不该有一条边。
         所以不裁形状、不描边、不压底 —— 边缘完全交给羽化遮罩。
         原来这里对所有 panel 一视同仁地 panelPath+clip，于是 soft 也长出一条
         圆角硬边（还带一道 1.4px 线），「无框」名存实亡。 */
      const isSoft = box.mode === 'soft';
      if (!isSoft) { panelPath(ctx, geo, 0); ctx.clip(); }
      if (fig.tone || fig.feather) ctx.drawImage(figBitmap(im, w, h, S, fig, zoom), x, y, w, h);
      else drawCoverF(ctx, im, x, y, w, h, zoom, fig.fy);
      if (!isSoft) tintPlate(ctx, fig, x, y, w, h, a, S);
      /* 分屏内部再切两格：**默认不用**（RULES 80）。接缝落在眼睛高度时，
         上半闭眼、下半张嘴，读作「图被撕开」而不是两格。
         真要「两格」请用 duo（两个完整画框）。留着是因为将来若改成
         「上格全身 / 下格特写」这种异质构图的真两格，这条路是现成的。 */
      if (fig.split2 && box.mode === 'split') {
        const im2 = getFrame(name, t0 - (fig.dt == null ? 0.9 : fig.dt));
        if (im2) {
          const gap = Math.max(4, h * 0.007);
          ctx.save();
          ctx.beginPath(); ctx.rect(x, y, w, h / 2 - gap / 2); ctx.clip();
          ctx.globalAlpha = clamp(a * 0.96, 0, 1);
          drawCoverF(ctx, im2, x, y, w, h, zoom, fig.fy);
          ctx.restore();
          /* 缝：一道 ink 细线，读作「两格」，而不是「两层图没对齐」 */
          ctx.globalAlpha = clamp(a * 0.52, 0, 1);
          ctx.fillStyle = S.ink;
          ctx.fillRect(x, y + h / 2 - 1, w, 2);
          ctx.globalAlpha = clamp(a, 0, 1);
        }
      }
      /* 底部压暗：只在 55%~100% 这一段渐变，用 1×256 的竖直贴图拉伸。
         平涂底上不要这层 —— 它会把下缘糊成一片脏灰，和「硬边印刷图」的语法相反。
         拱窗/胶囊/圆角不压暗：它们的下缘是形状的一部分，压暗会读成污渍。 */
      if (!S.flat && box.mode !== 'strip' && !isSoft && h > 240) {
        ctx.drawImage(PV.fadeSprite('bandVig', S.voidRGB, [[0, 0], [1, 0.55]], true),
          x, y + h * 0.55, w, h * 0.45);
      }
      ctx.restore();
      /* 边框：平涂底 2px 实线（切好的版面元素），绘本底一道 1.4px 细线 */
      if (fig.edge !== 'none' && !isSoft) {
        ctx.save();
        ctx.globalAlpha = clamp(a * (S.flat ? 0.62 : 0.30), 0, 1);
        ctx.strokeStyle = S.ink;
        ctx.lineWidth = S.flat ? 2 : 1.4;
        panelPath(ctx, geo, 1);
        ctx.stroke();
        ctx.restore();
      }
      /* 分屏的内缘：一道 accent 竖线。分屏如果只是一条硬切边，读作「图没对齐」；
         有了这条线，它才是一个**版面结构**（左半/右半）。 */
      if (box.mode === 'split') {
        const ix = box.x < PV.W / 2 ? x + w - 3 : x;
        ctx.save();
        ctx.globalAlpha = clamp(a * 0.9, 0, 1);
        ctx.fillStyle = S.accent;
        ctx.fillRect(ix, y, 4, h);
        ctx.restore();
      }
      /* 角标：两道 L 形短线。有没有它，决定这块图读作「照片」还是「版面元素」。 */
      /* 角标只给**直角面板**：拱窗/胶囊/圆角的下缘是形状的一部分，
         角标会落在圆角外面，读作「多画了两笔」。 */
      if (fig.tick && (box.shape || 'rect') === 'rect') {
        const L = Math.min(w, h) * 0.11, tw = Math.max(2, Math.min(w, h) * 0.010);
        const bx0 = x + L * 0.55, bx1 = x + w - L * 1.55, by = y + h - L * 1.55;
        ctx.save();
        ctx.globalAlpha = clamp(a * 0.85, 0, 1);
        ctx.fillStyle = S.accent;
        ctx.fillRect(bx0, by, L, tw); ctx.fillRect(bx0, by, tw, L * 0.72);
        ctx.fillRect(bx1, by, L, tw); ctx.fillRect(bx1 + L - tw, by, tw, L * 0.72);
        ctx.restore();
      }
    }
    ctx.restore();
  }

  /* ---------------- 纸底 ---------------- */
  /* 纸底是一次性烘焙的：整幅渐变如果每帧现算，Chromium 的抖动相位会变，
     同一时刻两次渲染差 ±1~2（md5 不同、确定性验收直接挂）。烘焙一次后只贴图。 */
  let _paperC = null, _paperKey = null;
  function paperCanvas(S, pal) {
    const key = (pal.paper || '') + '|' + (pal.paper2 || '') + '|' + (pal.void || '');
    if (_paperKey === key && _paperC) return _paperC;
    const o = PV.offscreen('paperBase', PV.W, PV.H, 1);
    const c1 = S.paperRGB;
    const c2 = pal.paper2 ? PV.hex(pal.paper2) : mix(S.paperRGB, S.voidRGB, 0.55);
    /* 两道交叉的梯度：只有一道时，暗部会读作「一条斜的黑」，看起来像加了蒙版；
       交叉之后角落各自有不同的明度，才像一张有落差的纸。 */
    const g = o.ctx.createLinearGradient(0, 0, PV.W * 0.35, PV.H);
    g.addColorStop(0, rgba(mix(c1, c2, 0.25), 1));
    g.addColorStop(0.52, rgba(c1, 1));
    g.addColorStop(1, rgba(c2, 1));
    o.ctx.fillStyle = g; o.ctx.fillRect(0, 0, PV.W, PV.H);
    const g2 = o.ctx.createLinearGradient(PV.W, 0, PV.W * 0.10, PV.H);
    g2.addColorStop(0, rgba(mix(c1, c2, 0.55), 0.55));
    g2.addColorStop(0.6, rgba(c1, 0));
    g2.addColorStop(1, rgba(mix(c2, S.voidRGB, 0.35), 0.45));
    o.ctx.fillStyle = g2; o.ctx.fillRect(0, 0, PV.W, PV.H);
    // 顶部一道极淡的「光从上面来」，给纸面一点方向性
    const g3 = o.ctx.createLinearGradient(0, 0, 0, PV.H * 0.55);
    g3.addColorStop(0, rgba(mix(c1, [255, 255, 255], 0.10), 0.35));
    g3.addColorStop(1, rgba(c1, 0));
    o.ctx.fillStyle = g3; o.ctx.fillRect(0, 0, PV.W, PV.H * 0.55);
    /* 纸纤维：一次烘焙（PV.paperFiber 512² 平铺），逐帧零成本。
       只走 multiply —— 它是「纸浆厚度」，不是叠一层灰。 */
    if (PV.paperFiber) {
      const pat = o.ctx.createPattern(PV.paperFiber(ST.seed || 7717), 'repeat');
      if (pat) {
        o.ctx.save();
        o.ctx.globalCompositeOperation = 'multiply';
        o.ctx.globalAlpha = 0.85;
        /* 平铺时缩到 0.5：512² 的贴图直接铺 1920×1080 时，纤维有 3.7 倍那么大，
           读作「纸上有污渍」而不是「纸的纤维」。缩一半 → 纤维 1~2px，才是纸。 */
        o.ctx.scale(0.5, 0.5);
        o.ctx.fillStyle = pat;
        o.ctx.fillRect(0, 0, PV.W * 2, PV.H * 2);
        o.ctx.restore();
      }
    }
    _paperKey = key; _paperC = o.c;
    return _paperC;
  }

  /* 平涂底（JIZURA 的全部 style pack 都是「整幅一个颜色」）。
     为什么必须平涂：渐变纸底会让每一个版面都自带一层「氛围」，
     于是无论排什么字，观众读到的都是「那张纸」而不是「这个版面」。
     JIZURA 的版面之所以立得住，正是因为底是哑的、平的、不参与叙事的。 */
  let _flatC = null, _flatKey = null;
  function flatCanvas(S, pal) {
    const tx = pal.texture || {};
    const key = (pal.bg || '') + '|' + (tx.scan || 0) + '|' + (tx.paper || 0) + '|' + (pal.pack || '');
    if (_flatKey === key && _flatC) return _flatC;
    const o = PV.offscreen('flatBase', PV.W, PV.H, 1);
    o.ctx.fillStyle = rgba(S.paperRGB, 1);
    o.ctx.fillRect(0, 0, PV.W, PV.H);
    /* 扫描线：每 4px 一条 2% 的暗线。这是「显示器/印刷网屏」的味道，
       只在 pack 声明 scan 时才加（crimson 0.4 / hud 0.2 / mono 0.3）。 */
    if (tx.scan > 0.01) {
      o.ctx.globalAlpha = 0.05 * tx.scan;
      o.ctx.fillStyle = rgba([0, 0, 0], 1);
      for (let y = 0; y < PV.H; y += 4) o.ctx.fillRect(0, y, PV.W, 1);
      o.ctx.globalAlpha = 1;
    }
    /* 纸纹：确定性点阵（不能用随机，否则两遍渲染 md5 不同） */
    if (tx.paper > 0.01) {
      const rnd = PV.rng(9187, 3);
      o.ctx.globalAlpha = 0.045 * tx.paper;
      o.ctx.fillStyle = rgba([0, 0, 0], 1);
      const n = Math.round(PV.W * PV.H / 900);
      for (let k = 0; k < n; k++) {
        const x = rnd() * PV.W, y = rnd() * PV.H;
        o.ctx.fillRect(x, y, 1 + (rnd() > 0.8 ? 1 : 0), 1);
      }
      o.ctx.globalAlpha = 1;
    }
    _flatKey = key; _flatC = o.c;
    return _flatC;
  }

  function drawPaper(ctx, t, shot) {
    const S = (shot && shot._S) || ST.S;
    /* 纸色必须跟着「本镜头的调色板」走。只读全局 ST.sb.palette 的话，
       反转镜头的字变了色、纸还是深色 —— 反转事件等于没发生。 */
    const pal = (shot && shot._pal) || ST.sb.palette || {};
    /* ---- 绿幕/黑幕导出（JIZURA 的 keyBg）----
       目的是把「文字层」单独出成色键素材，回 AE 合成到实拍/插画上。
       所以整幅只能有一个纯色：不能有纸纹、渐变、暗角、颗粒 ——
       任何一点灰阶变化都会在抠像时留下脏边。 */
    const kb = (shot && shot.keyBg) || ST.keyBg;
    if (kb) {
      const col = kb === 'black' ? '#000000' : (kb === 'white' ? '#FFFFFF' : '#00B140');
      ctx.fillStyle = col;
      ctx.fillRect(0, 0, PV.W, PV.H);
      return;
    }
    if (S.flat) { ctx.drawImage(flatCanvas(S, pal), 0, 0, PV.W, PV.H); return; }
    ctx.drawImage(paperCanvas(S, pal), 0, 0, PV.W, PV.H);
    /* 环境光：两团方向相反、周期互质的柔光缓慢漂移。
       一团光会读作「画面中间亮了一块」；两团互相穿过才有「空气在动」的错觉，
       而死黑给人的廉价感恰恰来自「没有空气」。 */
    const ph = t / 23;
    PV.blob(ctx, PV.W * (0.30 + 0.10 * Math.sin(ph * 6.283)), PV.H * 0.34,
      PV.H * 0.92, S.accentRGB, 0.078 + 0.022 * Math.sin(ph * 4.1));
    PV.blob(ctx, PV.W * (0.78 + 0.07 * Math.cos(t / 31 * 6.283)), PV.H * 0.72,
      PV.H * 0.78, S.ghostARGB, 0.052 + 0.016 * Math.cos(t / 26 * 6.283));
    if (shot && shot.wash != null) {
      PV.rect(ctx, 0, 0, PV.W, PV.H, S.accent, shot.wash * 0.10);
    }
  }

  /* ---------------- 一帧 ---------------- */
  function shotAt(t) {
    const sh = ST.shots;
    let lo = 0, hi = sh.length - 1;
    if (!sh.length) return null;
    if (t < sh[0].t0 || t >= sh[sh.length - 1].t1) {
      if (t < sh[0].t0) return null;
      return sh[sh.length - 1];
    }
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (sh[mid].t0 <= t) lo = mid; else hi = mid - 1;
    }
    return sh[lo];
  }

  function applyParts(shot, t) {
    let { glyphs } = shot._lay;
    const en = ease;
    const enter = shot.enter || { k: 'fade', dur: 0.3 };
    const hold = shot.hold || { k: 'none' };
    const exit = shot.exit || { k: 'fade', dur: 0.4 };
    const rndA = PV.rng(ST.sb.meta.seed, shot.i, 1);
    const rndB = PV.rng(ST.sb.meta.seed, shot.i, 2);
    const re = PV.ENTER[enter.k] || PV.ENTER.fade;
    const rh = PV.HOLD[hold.k] || PV.HOLD.none;
    const rx = PV.EXIT[exit.k] || PV.EXIT.fade;
    const breath = 1;
    /* 退场窗口与上界：有转场的镜头只走到 _xcap（见 setup 里的 X_KEEP），
       速率不变 —— 起点后移、终点停在 55%，切点那一刻屏幕与快照 A 同形。 */
    const xcap = shot._xcap == null ? 1 : shot._xcap;
    const xwin = shot._xout == null ? (exit.dur == null ? 0.42 : exit.dur) : shot._xout;
    const x0 = shot.t1 - xwin;
    let k = -1;                                   // 第几个可见字（空格不计入节奏）
    const EN_DUR = enter.dur == null ? 0.30 : enter.dur;
    const STAG = enter.stagger == null ? 0 : enter.stagger;
    const LEAD = enter.lead == null ? 0.04 : enter.lead;
    /* 逐字错相只能有**一个**来源。
       g.t0 来自逐词时间轴（字已经按自己的唱点错开了），此时再加 STAG*idx 就是
       双重计时 —— 实测 34 字的行要多等 1.5s 才铺满，而整句只有 3.6s，
       结果「唱完了字还没出齐」（t=24 的 diagonal 镜头整行只剩一两个笔画）。
       所以：有逐字轴 → 错相交给唱点；没有逐字轴（整行同 t0）→ 才用 stagger。 */
    /* 每镜一个「词节奏」：整句在前 30% 时长内出齐，上限 0.26s/词（不抢戏），
       下限 0.06s/词（再快就成一起出现了）。 */
    let maxWi = 0;
    for (const gg of glyphs) if (gg.wi != null && gg.wi > maxWi) maxWi = gg.wi;
    const wantBy = Math.max(0.45, (shot.t1 - shot.t0) * 0.30);
    const PACE = Math.max(0.06, Math.min(0.26, wantBy / Math.max(1, maxWi)));
    const rnd1 = []; const rnd2 = [];
    for (let i = 0; i < glyphs.length; i++) { rnd1.push(rndA()); rnd2.push(rndB()); }

    /* 行距约束：多行排版里，入场位移与纵向拉伸都不许跨到相邻行。
       dropBounce 起手在 -1.9×size，而 split 的行距只有 1.12~2.6×size ——
       第二行的词是「穿过第一行」掉下来的：半透明 + 纵向拉长 1.4 倍，
       两行字在半空中绞在一起（实测 20.0s 的 split 镜头，读作排版坏了）。
       规则：纵向位移 ≤ 行距 38%，纵向拉伸让字高留在行距 84% 以内。
       单行排版（无 rowGap）不受限，那里位移大反而是力度。 */
    const rowClamp = g => {
      const gap = g.rowGap;
      if (!(gap > 0)) return;
      const lim = gap * 0.38;
      if (g.dy > lim) g.dy = lim; else if (g.dy < -lim) g.dy = -lim;
      const maxSy = (gap * 0.84) / Math.max(1, g.size * 0.72);
      if (g.sy > maxSy) g.sy = maxSy;
    };

    for (let i = 0; i < glyphs.length; i++) {
      const g = glyphs[i];
      if (!g.isSpace) k++;
      const idx = k;
      // 重置：所有变换每帧从零开始，避免上一帧残留
      g.dx = 0; g.dy = 0; g.rot = 0; g.sc = 1; g.sx = 1; g.sy = 1;
      g.alpha = 1; g.vis = 1; g.clip = null;
      /* shot.sc：整镜缩放系数（晨光绘本「桥段逐句放大」用）。
         必须在重置之后乘进去 —— 放在重置前会被下一帧清掉。 */
      if (shot.sc) g.sc *= shot.sc;
      /* 零件驱动源同理必须每帧清空：它是数组（入场、退场各能 push 一层），
         留着上一帧的会逐帧累积，零件越飞越野且没有任何报错。 */
      g.pfns = null;

      const charAxis = g.t0 != null;          // sync 镜头里 t0 已被整块覆写成 shot.t0，天然无错相
      /* 首词必须跟着镜头一起出现。
         逐词时间轴是「唱到才长出来」，但镜头的起点来自整行的 t0，往往比第一个词的
         唱点早 0.2~0.6s —— 于是每句开头都有一小段只剩一个字母的空画面（实测 50.5s
         整屏一根竖条）。只钳第一个词：后面的词仍然严格跟随演唱。 */
      /* 逐词揭示不能完全跟着唱点走：源词时间轴的间隔常到 1.2~1.5s（"I need you now"
         四个词铺了 4 秒），于是整句大部分时间画面上只有一个词，读作「卡住了」。
         规则：每个词最多只比**前一个词**晚 0.26s 出场 —— 句子在开头 1 秒内搭好，
         之后靠 size/颜色/保持动作继续动，而不是靠「一个字一个字蹦」。 */
      /* 节奏上限还要**随句长自适应**：固定 0.26s/词 在 12 个词的长句上要铺 3.1s，
         镜头才 4.7s —— 观众看到的是「第一行满了、第二行只有一个词」，读作排版坏了
         （实测 20.0s 的 split 镜头就是 this）。改成「整句在镜头前 30% 时间里出齐」：
         短句仍是 0.26s/词，长句自动压到 0.1s/词，节奏跟着句子密度走而不是跟着词数走。 */
      const gT0 = charAxis
        ? Math.min(g.t0, shot.t0 + 0.10 + (g.wi || 0) * PACE)
        : g.t0;
      /* 错相不等距：等距 stagger 读作「均匀打字机」，加一点随机让字像是被甩进来的。
         幅度只 ±38%，不会破坏「整句一起出现」的节奏感。 */
      const inT0 = (charAxis ? gT0 : shot.t0) - LEAD
                 + (charAxis ? 0 : STAG * idx * (0.62 + 0.76 * rnd1[i]));
      const e = clamp((t - inT0) / EN_DUR, 0, 1);
      if (e <= 0) { g.vis = 0; continue; }
      re(g, e, rnd1[i], rnd2[i], enter);
      rowClamp(g);

      /* 运动残影：入场期按**瞬时速度**做一次挤压—拉伸。
         一帧内位移越大，字被拉得越长越窄 —— 这是「动得快」最便宜的表达，
         也是 fast 入场不读作「瞬移」的关键。取速度的方式是「把更早的进度再跑一遍」，
         不做积分，所以同一 t 永远同一结果（确定性不受影响）。
         擦除类入场（有 clip）自带方向感，跳过。 */
      if (e < 0.999 && !g.clip && EN_DUR > 0.03) {
        const bx0 = g.dx, by0 = g.dy, bs = g.sc, b_sx = g.sx, b_sy = g.sy, brot = g.rot, ba = g.alpha;
        const eP = Math.max(0, e - (1 / (ST.fps || 60)) / EN_DUR);
        g.dx = 0; g.dy = 0; g.rot = 0; g.sc = 1; g.sx = 1; g.sy = 1; g.alpha = 1;
        /* 这一遍只为取「早一帧」的位姿来算瞬时速度，**不是**真正的当前帧：
           piece 型入场会把自己的驱动源 push 进 g.pfns，若不掐掉，
           每帧就多挂一层 eP 版本的过期闭包 —— 零件的相位被早一帧的进度劫持，
           表现为「零件永远差一点点归位」且随时间越积越多。 */
        const bPfn = g.pfns;
        g.pfns = null;
        re(g, eP, rnd1[i], rnd2[i], enter);
        g.pfns = bPfn;
        rowClamp(g);
        const mx = bx0 - g.dx, my = by0 - g.dy;
        g.dx = bx0; g.dy = by0; g.sc = bs; g.sx = b_sx; g.sy = b_sy; g.rot = brot; g.alpha = ba;
        const sp = Math.sqrt(mx * mx + my * my) / Math.max(1, g.size);
        /* 上限 0.26（原 0.42）：0.42 时字高被拉到 1.42 倍，行距不够就会骑到上一行；
           再加 dropBounce 自带的 0.26 拉伸，两个来源叠起来正好 1.5 倍 —— 太浊。 */
        if (sp > 0.010) {
          const k2 = Math.min(0.26, sp * 1.15);
          g.sy *= 1 + k2;
          g.sx *= 1 - k2 * 0.32;
        }
        rowClamp(g);
      }

      /* 签名是 (g,t,ph,spec,amp)：第 4 个参数必须是 hold 规格本身。
         早先传成 (…, breath, hold, 1) —— spec 收到数字 1、amp 收到对象，
         jitter/trackIn 里出现 NaN，整行甚至整面墙一个字都不画（且毫无报错）。 */
      if (e >= 0.999) rh(g, t, rnd1[i], hold, 1, idx);

      const xe = clamp((t - x0) / Math.max(0.02, xwin), 0, 1) * xcap;
      if (xe > 0) rx(g, xe, rnd1[i], rnd2[i], exit, PV.W / 2, PV.H * 0.5);

      // 逐字错相：让同一行不像一整块板
      if (shot.wobble) {
        g.dy += Math.sin((t / 5.2 + idx * 0.31) * 6.283) * g.size * shot.wobble * 0.012;
      }
    }

    // 逐字强调：highlight 的少数词换样式
    if (shot.emphasis && shot.treatEmph) {
      const setw = new Set(shot.emphasis);
      for (const g of glyphs) if (setw.has(g.wi)) g.emph = true;
    }
    return glyphs;
  }

  /* 一趟色散残影：把整行重画在 lag 秒之前的位姿上，整体错位(dx,dy)并染成色散色。
     lag > 0 才有意义 —— lag=0 的重画和主层完全重合，只剩下「加粗」。 */
  function ghostPass(ctx, shot, t, lag, ox, oy, col, blend, S) {
    if (!(lag > 0)) return;
    const gs = applyParts(shot, t - lag);
    ctx.save();
    ctx.translate(ox, oy);
    ctx.globalCompositeOperation = blend;
    PV.drawGlyphs(ctx, gs, (c2, gg, bx, by, aa) => {
      /* 0.82 而不是更淡：残影本来就该被读出来，太淡就退化成「字边缘有点脏」。
         JIZURA 这里是不打折的满 alpha，我们留一点余量是怕它在大面积文字上发糊。
         要记住真正让残影一眼可读的是 **lag（1/24 秒前的位姿）**，静止画面上
         看不出色散是正常的 —— 它在运动里才有意义。 */
      c2.globalAlpha = aa * 0.82;
      c2.fillStyle = col;
      c2.fillText(gg.c, bx, by);
    }, { S });
    ctx.restore();
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
  }

  function paintFor(kind) { return PV.TREAT[kind] || PV.TREAT.solid; }
  /* TRIET 部件统一签名 (ctx,g,bx,by,a,S)，drawGlyphs 只给前 5 个 ⇒ 在这里把 S 绑进去 */
  const bindS = (fn, sp) => (c, g, bx, by, a) => fn(c, g, bx, by, a, sp);

  function drawLyrics(ctx, shot, t) {
    const S = shot._S || ST.S;
    const lay = shot._lay;
    const dur = Math.max(0.01, shot.t1 - shot.t0);
    const lt = t - shot.t0;
    const xcap = shot._xcap == null ? 1 : shot._xcap;
    const outDur = shot._xout != null ? shot._xout
      : ((shot.exit && shot.exit.dur != null) ? shot.exit.dur : 0.42);
    /* 版面层：巨型暗影在最下，规则线/注记在字下，遮带在字上。
       全部由 layout 自己产出 —— 这样「字和它的线」永远同源，不会各摆各的。
       xcap 必须一起传下去：字停在 55% 而线淡到 0，就是「字没了线还在」的反向版。 */
    if (lay && PV.drawGiant) PV.drawGiant(ctx, lay.giant, lt, outDur, dur, xcap);
    if (lay && PV.drawMarks) PV.drawMarks(ctx, lay.marks, lt, outDur, dur, 'under', xcap);
    const glyphs = applyParts(shot, t);
    const base = paintFor(shot.treat && shot.treat.k);
    const emph = shot.treatEmph ? paintFor(shot.treatEmph.k) : base;
    const spec = Object.assign({}, S, shot.treat || {});
    const specE = shot.treatEmph ? Object.assign({}, S, shot.treatEmph) : spec;

    // 残影（只给需要的镜头）：极低 alpha 的同字形前置副本，读作「运动留下的影」
    if (shot.trail) {
      const n = shot.trail.n || 3, dt = shot.trail.dt || 0.055, a0 = shot.trail.a || 0.16;
      for (let q = n; q >= 1; q--) {
        /* 残影必须是「稍早时刻的位姿」，否则和主层完全重合、只剩加粗 ⇒ 要把 t 拨回去重算变换 */
        const gq = applyParts(shot, t - dt * q);
        for (const g of gq) {
          if (g.emph) continue;
          const ge = Object.assign({}, g, { alpha: g.alpha * a0 * (1 - q / (n + 1)) });
          PV.drawGlyphs(ctx, [ge], (c2, gg, bx, by, aa) => {
            c2.globalAlpha = aa; c2.fillStyle = rgba(S.accentRGB, 1); c2.fillText(gg.c, bx, by);
          });
        }
      }
      applyParts(shot, t);   // 主层要用当前时刻，必须再算回来
    }

    /* ---- 色散残影：JIZURA 的 ghost A / B 两趟 ----
       这不是「给字加一圈发光」。两趟各自把时间**往回拨** 1.6/24 与 0.8/24 秒
       重算整行位姿，再以色散色错位叠上去 —— 于是被画出来的是
       「上一瞬它真的在这个位置飞过」，而不是同一个字的轮廓副本
       （同帧复制两份只等于把字加粗，这正是早前 trail 的做法）。
       合成模式跟着底色走：亮底用 multiply，否则残影读作浮在纸上的一层白雾。 */
    /* 0.7 是 JIZURA 在深色平涂底上的值：深底上两趟色散读作「霓虹先到一步」。
       换到浅色纸底之后，multiply 把两趟 ghost 压成深色，整行字就长出一圈
       脏边（1:1 裁切下是「深棕字 + 橙外圈 + 浅外沿」的三层贴纸感）。
       浅底（lum>0.55）走 0.45，深底保留 0.7 —— 色散是日系语汇，不该整个拿掉。 */
    const _light = PV.lum(S.paperRGB) > 0.55;
    const chroma = (shot.chroma == null ? (_light ? 0.45 : 0.7) : shot.chroma)
      * (S.ghost == null ? 1 : S.ghost);
    if (chroma > 0.02 && S.ghostA && S.ghostB) {
      const u = PV.H / 1080;
      const mul = PV.lum(S.paperRGB) > 0.55 ? 'multiply' : 'source-over';
      /* 第五个参数必须是 S：ghost 这一趟也要走 piece 分支。
         漏了它 ⇒ 残影用整字 fillText 画了一颗**完整**的字母垫在底下，
         已经飞散的碎片反而变成漂浮的碎屑（实测 "THIS IS THE BEST FEELING"
         读作「字没碎、旁边多了几粒小白点」）。JIZURA 每一趟都是完整重绘，
         内容是上一次位姿下的**同一批零件**。 */
      ghostPass(ctx, shot, t, 1.6 / 24, -3.4 * chroma * u, -1.3 * chroma * u, S.ghostB, mul, S);
      ghostPass(ctx, shot, t, 0.8 / 24, 3.2 * chroma * u, 1.9 * chroma * u, S.ghostA, mul, S);
      applyParts(shot, t);           // 上面重算了两次位姿，主层必须回到当前时刻
    }

    PV.drawGlyphs(ctx, glyphs.filter(g => !g.emph), bindS(base, spec), { S: spec });
    if (shot.treatEmph) PV.drawGlyphs(ctx, glyphs.filter(g => g.emph), bindS(emph, specE), { S: specE });
    ctx.filter = 'none';
    ctx.globalAlpha = 1;
    if (lay && PV.drawMarks) PV.drawMarks(ctx, lay.marks, lt, outDur, dur, 'over', xcap);
  }

  /* ---------------- 背景层次 ----------------
     纸底之上、装饰之下的一层。任务只有一个：把「死黑」变成「纸」。
     纹理全部来自 pv_bg.js 的烘焙场，这里只负责按编排稿顺序贴图。 */
  function drawBG(ctx, list, t, start, S) {
    if (!list || !list.length || !PV.BG) return;
    const rel = t - (start || 0);
    for (const d of list) {
      const fn = PV.BG[d.k];
      if (!fn) continue;
      let dd = d;
      // at/fade：和装饰同一套语义，背景也能逐层「搭」起来
      if (d.at != null) {
        const k = clamp((rel - d.at) / (d.fade == null ? 0.6 : d.fade), 0, 1);
        if (k <= 0) continue;
        if (k < 1) dd = Object.assign({}, d, { a: (d.a == null ? 1 : d.a) * k });
      }
      fn(ctx, dd, { S: S || ST.S, t, rel, seed: (start || 0) * 1000 | 0, shot: start });
    }
  }

  function drawDecor(ctx, list, t, start, S) {
    if (!list) return;
    S = S || ST.S;
    const rel = t - (start || 0);
    for (const d of list) {
      const fn = PV.DECOR[d.k];
      if (!fn) continue;
      let dd = d;
      /* at/fade：装饰按「镜头内相对时刻」逐条出现。片头的版式是一层一层搭起来的，
         全都在 t0 一起蹦出来就会读作「静态海报」而不是「正在排版」。 */
      if (d.at != null) {
        const k = clamp((rel - d.at) / (d.fade == null ? 0.5 : d.fade), 0, 1);
        if (k <= 0) continue;
        if (k < 1) dd = Object.assign({}, d, { a: (d.a == null ? 1 : d.a) * k });
      }
      fn(ctx, dd, { S, t, phase: rel, shot: start });
    }
  }

  function drawPost(ctx, list, t, en) {
    if (!list) return;
    for (const f of list) {
      const fn = PV.FX[f.k];
      if (!fn) continue;
      // 包络：flash 这类只在指定时刻附近有值
      let a = f.a == null ? 1 : f.a;
      if (f.at != null) {
        const d = Math.abs(t - f.at);
        a *= clamp(1 - d / (f.decay || 0.12), 0, 1);
      }
      fn(ctx, f, { S: ST.S, t, fx: { a }, en });
    }
  }

  /* ---------------- 对外：渲染一帧 ---------------- */
  PV.renderAt = function (ctx, t) {
    const sb = ST.sb;
    const S = ST.S;
    /* ---- 两拍格（JIZURA 的 koma）：把动画时间量化到 1/KOMA 秒的栅格上 ----
       这是「日本文字 PV 味道」最主要的结构来源，也是逆向时得到的第一条结论。
       传统有限动画每秒只画 8~12 张，中间的运动靠重复，而不是逐帧插值。
       我们原先在 60fps 上逐帧重算每一个位移 ⇒ 顺得像 UI 动效，
       再怎么写缓动曲线也读不出 animator 的手感，因为**采样率本身就是信号**。
       量化后同一张画连续出现 5 帧，抖动类的手势才有了「格」。
       副作用也是设计的一部分：相机、装饰、人物全都跟着一起被量化 ——
       有限动画本来就是整帧整体在跳，只把文字抽帧而背景照旧持顺会读作两层不同源。
       KOMA=0 表示不抽帧（逐输出帧一张画）。 */
    const KOMA = ST.koma || 0;
    if (KOMA > 0) t = Math.floor(t * KOMA + 1e-6) / KOMA;
    const shot = shotAt(t);
    /* 复位到「设计坐标 × 输出倍率」，**不是单位矩阵**。
       写成 setTransform(1,0,0,1,0,0) 时，4K 母版上整张画面只画在左上 1920×1080，
       其余 3/4 是纯黑 —— 而且逐帧循环里毫无报错、码率和时长全对，只有肉眼看画面
       才发现（2026-10-28 实际踩过，4K 母版整片作废，白跑 13 分钟）。
       页面在 setup 里把输出倍率写进 PV.SCALE，这里必须沿用它。 */
    const sc = PV.SCALE || 1;
    ctx.setTransform(sc, 0, 0, sc, 0, 0);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    /* 整帧先铺底：相机 zoom<1 时纸底会缩进，露出的边框若不先铺色，
       留下的是**上一帧**的像素 —— 于是「同一时刻两次渲染」取决于上一帧画了什么，
       确定性直接崩（实测左边缘 6px 全高条带每次都不同）。 */
    ctx.fillStyle = rgba(S.paperRGB, 1);
    ctx.fillRect(0, 0, PV.W, PV.H);

    if (!shot) {                                   // 片头/片尾之外：只有纸底
      drawPaper(ctx, t, null);
      drawBG(ctx, sb.meta && sb.meta.bgl, t, 0, S);
      PV.drawGrain(ctx, 0, 0, PV.W, PV.H, 0.5, ST.dpr);
      return;
    }
    const en = shot._en == null ? 0.5 : shot._en;

    // 相机：整帧运动（pv_cam.js）。每个镜头都有一台在动的机器 —— 静止的镜头连起来就是幻灯片。
    const cam = (PV.camAt ? PV.camAt(shot, t) : { x: 0, y: 0, s: 1, sx: 1, sy: 1, rot: 0, skx: 0 });

    ctx.save();
    if (cam.x || cam.y || cam.rot || cam.skx || cam.s !== 1 || cam.sx !== 1 || cam.sy !== 1) {
      ctx.translate(PV.W / 2 + cam.x, PV.H / 2 + cam.y);
      if (cam.rot) ctx.rotate(cam.rot * Math.PI / 180);
      if (cam.skx) ctx.transform(1, 0, Math.tan(cam.skx * Math.PI / 180), 1, 0, 0);
      ctx.scale(cam.s * cam.sx, cam.s * cam.sy);
      ctx.translate(-PV.W / 2, -PV.H / 2);
    }

    drawPaper(ctx, t, shot);
    drawBG(ctx, shot.bgl, t, shot.t0, shot._S);
    drawDecor(ctx, shot.bg, t, shot.t0, shot._S);

    // 人物（后）
    let figBox = null;
    if (shot.fig) {
      figBox = placeFigure(shot.fig, shot._lay ? shot._lay.bbox : [0, 0, 0, 0]);
      shot._figBox = figBox;
      if (shot.fig.layer !== 'front') drawFigure(ctx, shot.fig, figBox, t, en, shot._S, shot);
    }

    // 文字
    drawLyrics(ctx, shot, t);

    // 人物（前）：「文字从她身前过」只在 layer=front 的镜头成立
    if (shot.fig && shot.fig.layer === 'front') drawFigure(ctx, shot.fig, figBox, t, en, shot._S, shot);

    // 前景装饰
    drawDecor(ctx, shot.decor, t, shot.t0, shot._S);

    ctx.restore();

    // 后期（不受相机影响）
    drawPost(ctx, shot.post, t, en);
    /* 转场放在**画框饰件之后**：暗角/颗粒/角落标记都是「贴在整幅画面上的」，
       必须齐了之后再把上一镜整幅盖回去。顺序错一步就多跳一次亮度 ——
       先转场后暗角时，切点帧 = A + 新镜的暗角（两层暗角），实测差 0.77；
       再往前 A 干脆没带暗角，差 3.0。两次都被误当成转场几何问题。 */
    /* 平涂底（JIZURA 风格）不加暗角：暗角是「电影感」的语法，
       而 JIZURA 的语法是「印刷品」—— 四角必须和中心一样平，版面才立得住。 */
    /* 注意这里必须用**本镜头**的 S：renderAt 顶上的 S 是全局 ST.S，
       它的 flat 永远是 undefined —— 拿它判会得到「平涂底照样压暗角」，
       实测四角 155 vs 中心 236，一整片奶油底被压成脏灰。 */
    /* 绿幕/黑幕导出时连暗角和颗粒都不要：抠像要的是纯色，一点灰阶都是脏边。 */
    const kb = shot.keyBg || ST.keyBg;
    if (!kb && !((shot._S || S).flat) && !(shot.post || []).some(f => f.k === 'vignette')) {
      /* 默认暗角：让画面四周沉下去，是「电影感」最便宜也最有效的一步 */
      /* 暗角 0.40 / r0 0.46：原来是 0.60，四角直接压成纯黑 ——
         背景纹理全部被吃掉，等于白做。 */
      PV.FX.vignette(ctx, { a: 0.40, r0: PV.H * 0.46, r1: PV.H * 1.10 }, { S });
    }
    if (!kb) PV.drawGrain(ctx, 0, 0, PV.W, PV.H, (shot._S || S).flat ? 0.30 : 0.55, ST.dpr);

    /* 角落极淡标记：歌名/来源改成片头标题卡承载后，全片只留这一行。
       压在画面右下角、alpha 0.16 —— 存在但不抢戏，观众要看得到、平时看不见。 */
    const mark = sb.meta && sb.meta.mark;
    if (mark) {
      PV.label(ctx, mark, PV.W - 64, PV.H - 52, 15, S.ink, 0.17, 500, 'right', 0.16);
    }
    ctx.globalAlpha = 1;
    /* 切间过渡（TRANS 分组）：把上一镜快照按转场几何让掉，露出这一镜。
       p=0 时整幅盖 A、p=1 时什么都不画（契约见 pv_trans.js 文件头）。 */
    crossFade(ctx, shot, t);
    /* 存 A：**屏幕上真正的那一帧**（含暗角/颗粒/角落标记），且必须在 crossFade
       之后 —— 拍的是「这一帧最终长什么样」，不是「镜头内容长什么样」。 */
    saveCross(ctx, shot, t);
  };

  /* ---------------- 切间过渡（TRANS 分组，pv_trans.js） ----------------
     JIZURA 的 TRANS 分组是把「上一 cut 的静止帧」和当前帧合成。我们逐帧顺序渲染，
     画布上本来就有新镜头的画面，所以只要在退场开始前存一张上一镜快照，
     切镜时按转场几何把它让掉 —— 比重新渲染上一镜便宜一个数量级。

     为什么存「切点前最后一两帧」而不是「退场开始前」：转场是在**切点之后**
     才把 A 盖上去的，此刻屏幕上是退场末态。若拿退场起点的满墨帧当 A，
     退场已经淡掉的东西会被 A 原样弹回来 —— 也就是「切点上闪一下」。
     存最后一帧则 A ≡ 屏幕上正在显示的那张画，转场从它开始让位，零跳变。
     （A 会不会是一张空画面，取决于退场有没有把画面淡空 —— 那一半由 X_KEEP
      在编排阶段就压住了：有转场的镜头退场只走到 55%，永远留得下东西给转场擦。）

     转场种类在 pv_trans.js（wipe/blinds/clock/iris/blocks/flash/push/zoom/slice/ink
     十种，对齐 JIZURA 的 TRANS 分组）。编排稿写 `shot.trans = {k, ...params}`；
     不写时按镜头号确定性推一个 —— 同一 t 两次渲染永远同一种转场。 */
  const XFADE = { dur: 0.30 };
  /* 有转场时退场最多走到这里（55%）：剩下的 45% 墨量留给转场去抹掉。
     0 会让转场无内容可擦（等于硬切），1 会让画面在切点前先变空再被快照弹回来。 */
  /* 有下一镜的镜头，退场走到哪里停。
     0.55 = 切点时上一镜还剩 45% 可见度，剩下的交给转场带走。
     试过改成 0（满墨撑到切点），帧差**反而更大**：zoom 把满墨的 A 放大 1.28 倍，
     与上一张满墨原尺寸的差异比「半透放大」更明显。所以退场保留，连续性靠转场
     按 1-X_KEEP 起手（见 crossFade 的 I.ka）。 */
  const X_KEEP = 0.55;

  /* 快照抓取窗：只在这个窗口里抓，且越晚越好（同字母的后续帧覆盖前者） */
  const XC_WIN = 0.20;
  let XC = null, XCFrom = -1, XCValid = false, XCAt = -1;
  /* 本帧是否正在放转场：正在放的时候画面不是「纯某一镜」，不能拿去当快照，
     否则极短的镜头会把自己的转场残影拍进 A，一路叠下去。 */
  let XBusy = false;

  /* 分段渲染的起点没有「上一镜」：预热用的是缩小画布，快照必然是错的，
     必须显式作废，否则第一帧会拿一张 192×108 的残图当 A 去放转场。 */
  PV.resetCross = () => { XCValid = false; XCFrom = -1; XCAt = -1; };

  function saveCross(src, shot, t) {
    if (XBusy) return;
    if (shot.t1 - t > XC_WIN) return;
    if (XCFrom === shot.i && t <= XCAt) return;
    if (!XC) XC = document.createElement('canvas');
    const w = src.canvas.width, h = src.canvas.height;
    if (XC.width !== w || XC.height !== h) { XC.width = w; XC.height = h; }
    XC.getContext('2d').drawImage(src.canvas, 0, 0);
    XCFrom = shot.i;
    XCAt = t;
    XCValid = true;
  }

  function crossFade(ctx, shot, t) {
    XBusy = false;
    if (!XCValid || XCFrom !== shot.i - 1) return;
    /* 片头卡不参与：它是标题+档案行，被拦腰切开只会读作「排版炸了」，
       而且它和大字歌词交错出现，怎么看都像 bug。片头直接硬切进第一句。 */
    if (shot.i <= 1) return;
    const td = shot.trans && PV.TRANS && PV.TRANS[shot.trans.k] ? shot.trans : null;
    const dur = (td && td.dur != null) ? td.dur
      : (PV.TRANS && td && PV.TRANS[td.k].dur) ? PV.TRANS[td.k].dur : XFADE.dur;
    const raw = (t - shot.t0) / Math.max(0.08, dur);
    /* ---- 进度按 koma 栅格归一（RULES §84 第四条根因）----
       koma 下转场只会被真正画出来 floor(dur·KOMA) 次，相邻两张画的间隔是
       1/KOMA 秒 ⇒ 每一步的进度增量 dq = 1/(KOMA·dur)。于是**最后一个样本**
       永远停在 p = 1 - dq 上：0.30s 的转场（dq≈0.28）最后一格还在 p≈0.72，
       0.46s 的（dq≈0.18）停在 p≈0.82。转场「差一格没走完」，剩下那部分
       在下一帧被整层丢掉 —— 这就是切点上「闪一下」的最后一种来源，
       而且它跟几何写错无关，是**采样率**造成的：不管把曲线修得多对，
       只要终点落在两个样本之间，就一定会剩。
       除以 (1-dq)：最后一个样本正好 p=1（各转场此时都画不出东西），
       切点前后两侧严丝合缝；此前的样本略微提前，步长仍然均匀。 */
    const stepT = (ST.koma || 0) > 0 ? 1 / ST.koma : 0;
    const dq = stepT > 0 ? Math.min(0.45, stepT / Math.max(0.08, dur)) : 0;
    const q = PV.clamp(dq > 0 ? raw / (1 - dq) : raw);
    if (q >= 1) return;
    /* 参数：derive 按镜头号推一套确定性默认值做基底，编排稿显式参数再覆盖它。
       早先只有「完全没写 trans」才走 derive —— 于是编排稿写 {k:'wipe'} 时
       dir/n/seed 全是 undefined，落回硬编码默认值（每次擦除都同一方向）。 */
    let par;
    if (td) {
      par = (PV.TRANS && PV.TRANS.derive) ? PV.TRANS.derive(td.k, shot) : { k: td.k };
      for (const kk in td) if (td[kk] != null) par[kk] = td[kk];
    } else {
      const pool = PV.TRANS ? PV.TRANS.ORDER : ['wipe'];
      const kk = pool[(shot.i * 3 + 1) % pool.length];
      par = (PV.TRANS && PV.TRANS.derive) ? PV.TRANS.derive(kk, shot) : { k: kk };
    }
    const fn = PV.TRANS && PV.TRANS[par.k];
    if (!fn || !fn.draw) return;
    XBusy = true;
    /* 只读诊断口：门禁脚本要能看见「这一刻转场走到几分、用的是哪一种」。
       出问题时最贵的信息就是「q 卡在 0.06 一直不动」这类事实，靠肉眼看缩略图
       只能看到「画面不动」，看不到「为什么不动」。 */
    PV.__xdbg = { t, q, k: par.k, dur, dq, from: shot.i - 1 };
    const S = shot._S || ST.S;
    fn.draw(ctx, XC, q, {
      W: PV.W, H: PV.H, S,
      accent: rgba(S.accentRGB, 1),
      seed: par.seed != null ? par.seed : ((shot.i | 0) * 2654435761) >>> 0,
      dir: par.dir, n: par.n, nx: par.nx, ny: par.ny,
      x: par.x, y: par.y, a0: par.a0, spin: par.spin, flashCol: par.flashCol,
      /* 装饰件的收尾窗口要按 dq 自适应：只有 4~6 个样本时，「p→1 才归零」
         必然表现为一次跳变（见 TR.deco）。 */
      dq,
      /* ka = 切点那一刻 A 还在画面上的不透明度。
         有下一镜的镜头退场只走到 X_KEEP，所以切点瞬间屏幕上的 A 是 1-X_KEEP；
         快照也就是这个态。转场若从 alpha=1 开始画这张快照，字会**突然变实**——
         zoom 尤其明显（它同时还在放大，读作「上一镜又跳回来一下」）。
         门禁 pv_flash 把它记作「切点弹回」，实测 11.72s / 142.79s 两处 6.3 与 11.1 倍。 */
      ka: 1 - X_KEEP,
    });
  }
  PV.crossFade = crossFade;

  /* 统一的安全网：任何布局算完都过一遍「塞进安全区」检查。
     布局内部已经各自适配过字号，这里是最后一道闸 —— 宁可等比缩一点，
     也不能让整行字跑到画外（读作「渲染坏了」而不是「设计如此」）。
     bleed=true 的构图（megaCrop / edge / dense）是有意溢出，跳过。 */
  const SAFE_TXT = { x0: 88, y0: 64, x1: PV.W - 88, y1: PV.H - 56 };
  function fitShot(r) {
    if (!r || !r.glyphs || !r.glyphs.length || r.bleed) return r;
    const bb = r.bbox || [0, 0, 0, 0];
    const w = Math.max(1, bb[2] - bb[0]), h = Math.max(1, bb[3] - bb[1]);
    const aw = SAFE_TXT.x1 - SAFE_TXT.x0, ah = SAFE_TXT.y1 - SAFE_TXT.y0;
    let k = 1;
    if (w > aw) k = Math.min(k, aw / w);
    if (h > ah) k = Math.min(k, ah / h);
    if (k < 0.995) {
      const cx = (bb[0] + bb[2]) / 2, cy = (bb[1] + bb[3]) / 2;
      for (const g of r.glyphs) {
        g.x = cx + (g.x - cx) * k;
        g.y = cy + (g.y - cy) * k;
        g.w *= k; g.size *= k;
      }
      r.size *= k;
    }
    const b2 = PV.bboxOf(r.glyphs);
    let dx = 0, dy = 0;
    if (b2[0] < SAFE_TXT.x0) dx = SAFE_TXT.x0 - b2[0];
    else if (b2[2] > SAFE_TXT.x1) dx = SAFE_TXT.x1 - b2[2];
    if (b2[1] < SAFE_TXT.y0) dy = SAFE_TXT.y0 - b2[1];
    else if (b2[3] > SAFE_TXT.y1) dy = SAFE_TXT.y1 - b2[3];
    if (dx || dy) for (const g of r.glyphs) { g.x += dx; g.y += dy; }
    r.bbox = PV.bboxOf(r.glyphs);
    return r;
  }

  /* ---------------- 初始化 ---------------- */
  PV.setupPV = async function (opt) {
    ST.dpr = opt.dpr || 1;
    ST.sb = opt.storyboard;
    ST.fps = opt.fps || 60;
    PV.FPS = ST.fps;                       // piece 型入场取瞬时速度时需要知道帧率
    /* 两拍格：默认 12（每秒 12 张画，等价 JIZURA 的 onTwos）。0 = 不抽帧。
       编排稿 meta.koma 可以覆盖 —— 抒情段想要更顺时值得降到 8 或 0。 */
    const meta = opt.storyboard && opt.storyboard.meta;
    ST.koma = opt.koma != null ? opt.koma : (meta && meta.koma != null ? meta.koma : 12);
    ST.dur = opt.dur;
    ST.S = buildStyle(opt.storyboard.palette || {}, ST.dpr);
    /* 绿幕导出（JIZURA 的 keyBg）：job.meta.keyBg = 'green'|'black'|'white'。
       单镜头也能用 shot.keyBg 覆写（演示片里混一段色键素材就是这么做的）。 */
    ST.keyBg = opt.keyBg || (opt.storyboard.meta && opt.storyboard.meta.keyBg) || null;

    /* live 序列（head 圆窗 / body 竖幅 / wide 底衬）：优先用调用方传的，
       没传时回落到 storyboard.meta.live —— 静帧工具只交 storyboard，靠这条兜底。 */
    const live = (opt.live && Object.keys(opt.live).length)
      ? opt.live
      : ((opt.storyboard && opt.storyboard.meta && opt.storyboard.meta.live) || {});
    /* 序列名由 live.json 决定（LIVE_SPANS 的 s1..s6），不再写死 head/body/wide。
       写死时新格式一条都载不进去，而 getFrame 只是返回 null —— 六处该有人的地方
       全空，渲染日志里一个字都看不出来，只能靠抽帧才发现。 */
    let _nseq = 0;
    for (const k of Object.keys(live)) {
      if (k === 'face' || k === 'cut_start') continue;
      const v = live[k];
      if (v && typeof v === 'object' && v.dir) { loadSeq(k, v); _nseq++; }
    }
    if (live.face) ST.face = live.face;
    if (_nseq) ST.seqCount = _nseq;


    const ctx = opt.ctx;
    const LINES = opt.storyboard.lines || [];

    /* 顺序铁律：字体必须先就位，再排构图。
       构图要 measureText 反推字号，字体没加载完量到的是 fallback 的宽度 ——
       字号会算错，而且首帧字形栅格是空的，导致「第一次渲染和第二次不一样」。
       实测症状：同一 t 连渲 3 次得 3 个不同 md5。 */
    const texts = (opt.storyboard.lines || []).map(l => l.text).join('') +
      (opt.storyboard.meta.title || '') + (opt.storyboard.meta.artist || '') +
      'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:.,·—/「」';
    const nf = await PV.warmFonts(texts, [32, 64, 120, 200, 320]);
    try { await document.fonts.ready; } catch (e) { /* 老浏览器没有 ready，忽略 */ }

    // 预排所有镜头的构图：构图与时间无关，一次算完，逐帧只做变换
    ST.shots = (opt.storyboard.shots || []).slice().sort((a, b) => a.t0 - b.t0);
    let total = 0;
    for (const sh of ST.shots) {
      const layFn = (PV.LAYOUT_NEXT && PV.LAYOUT_NEXT[(sh.layout && sh.layout.k) || 'center'])
        || PV.LAYOUT[(sh.layout && sh.layout.k) || 'center'];
      if (!layFn) continue;
      // line=-1 是片头标题卡：数组下标 -1 会取到最后一行，必须显式挡掉
      const line = (sh.line >= 0 ? LINES[sh.line] : null)
        || { text: sh.text || '', t0: sh.t0, t1: sh.t1, words: [], chars: [] };
      /* 调色板必须先于构图解算：版面要拿九角色去画自己的线/注记/暗影。
         编排稿里 pal 只写 {pack, idx}（30 字节），色值由 pv_scheme.js 解算 ——
         改一个色值只改一处，不必回写 48 个镜头的 JSON。 */
      sh._pal = sh.pal
        ? (sh.pal.pack ? Object.assign({}, PV.STYLE(sh.pal.pack, sh.pal.idx | 0), sh.pal) : sh.pal)
        : null;
      sh._S = sh._pal ? Object.assign({}, ST.S, buildStyle(sh._pal, ST.dpr)) : ST.S;
      const stp = Object.assign({ line, text: (sh.text || line.text) }, sh.layout || {}, sh.type || {});
      stp.pal = sh._pal || Object.assign({}, PV.STYLE('noir', 0));
      const r = fitShot(layFn(ctx, null, stp));
      /* sync：冲击型构图（密排墙/极限裁切/满幅大字）必须整块同时出现。
         沿用逐词时间的话，墙是一格一格长出来的 —— 读作「卡了」而不是「砸下来」。 */
      if (sh.sync) for (const g of r.glyphs) { g.t0 = sh.t0; g.t1 = sh.t1; }
      sh._lay = r;
      sh._en = sh.energy == null ? 0.5 : sh.energy;
      total += r.glyphs.length;
    }
    /* 退场与转场的**交接点**（单一真源，RULES §84）。
       判定看「下一镜会不会转场进来」，所以除片尾外每镜都要留墨：
         · 片尾（没有下一镜）→ 退场完整淡出，片子才收得住；
         · 其余镜头 → 退场只走到 X_KEEP，剩下的交给下一镜的转场带走。
       退场速率不变，只是起点后移、终点停在 55% —— 于是「切点那一刻屏幕上的样子」
       与「快照 A」是同一张画。
       不这么做就会：退场先把整幅淡空 → 转场把退场**起点**的满墨快照盖回来
       → 再抹掉。观感正是「动画已经走完了，画的还是上一镜，然后闪一下」。 */
    for (let k = 0; k < ST.shots.length; k++) {
      const sh = ST.shots[k];
      const hasNext = k + 1 < ST.shots.length;
      const xd0 = (sh.exit && sh.exit.dur != null) ? sh.exit.dur : 0.42;
      sh._xcap = (hasNext && PV.TRANS) ? X_KEEP : 1;
      sh._xout = Math.max(0.05, xd0 * sh._xcap);
    }
    ST.stats = { glyphs: total, shots: ST.shots.length };
    /* 调试口：抽帧探针要读每镜解算后的构图（巨影 bbox / 版心 / 人物占位），
       不该让外部去重算一遍 —— 重算必然和真渲染漂移。只读，不参与渲染。 */
    if (typeof window !== 'undefined') window.__pvShots = ST.shots;

    /* 只装载第一镜的帧；其余镜头在渲染循环里按需换装（见 holdShot）。
       暖机渲染不需要人物层 —— 它只是为了把字形栅格/贴图/pattern 先跑一遍。 */
    const sh0 = shotAt(0.001);
    _winKey = sh0 ? String(sh0.i) : 'none';
    const pre = await holdShot(sh0);

    /* 暖机渲染：把每个镜头的字形栅格、渐变、pattern 都先跑一遍。
       不做这一步，渲染循环里的「第 0 帧」会和后面所有帧不一样（约 100ms 的代价）。 */
    for (const sh of ST.shots) { try { PV.renderAt(ctx, (sh.t0 + sh.t1) / 2); } catch (e) { /* 单镜失败不阻断 */ } }
    try { PV.renderAt(ctx, 0); } catch (e) { /* 同上 */ }
    ST.ready = true;
    return {
      fonts: nf, glyphs: total, shots: ST.shots.length,
      seqs: Object.keys(ST.seq), dpr: ST.dpr,
      preload: pre.n, settled: pre.jobs, memMB: Math.round(pre.bytes / 1048576),
      /* 探测文本必须用**这首歌实际会排的字**。早先固定写 'A字'：英文歌的歌词里没有汉字，
         日文分片从头就没被请求过，于是永远报 heavy/mincho/minchoLt「未就绪」——
         纯误报，白查了两轮（实际 808 个分片全 loaded，14 族 check 全 true）。
         字体是分 unicode-range 的，check 的字符串决定了它去找哪些分片。 */
      missing: Object.keys(PV.FACES).filter(k => !document.fonts.check(PV.cssFace(k, 64), texts.slice(0, 240) || 'A0')),
    };
  };

  PV.STAT = () => ST.stats;
  /* 调试用只读出口（tools/pv_stills.cjs 与人工核对构图时用） */
  PV.SHOTS = () => ST.shots || [];
  PV.shotAt = t => shotAt(t);
})();
