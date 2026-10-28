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
    const ink = PV.hex(pal.ink || '#F2F6FA');
    const paper = PV.hex(pal.paper || '#0A0D12');
    const accent = PV.hex(pal.accent || '#FFB067');
    const ghostA = PV.hex(pal.ghostA || '#39C6E0');
    const ghostB = PV.hex(pal.ghostB || '#C8373A');
    const voidc = PV.hex(pal.void || '#000000');
    return {
      inkRGB: ink, paperRGB: paper, accentRGB: accent, ghostARGB: ghostA, ghostBRGB: ghostB, voidRGB: voidc,
      ink: rgba(ink, 1), paper: rgba(paper, 1), accent: rgba(accent, 1),
      ghostA: rgba(ghostA, 1), ghostB: rgba(ghostB, 1),
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
  /* 预载：**只载真的会被画到的帧**（有人物层的那些镜头）。
     全量预载在 4K 下会把解码后的位图堆到 1GB 以上，而人物层只覆盖少数镜头；
     又因为离线渲染是同步循环、没法 await，所以必须在 __setup 里就全部 decode 完，
     否则渲染到那一帧时图还没解码完 —— 表现是人物一闪一闪，且每次跑闪的位置还不一样。 */
  function preloadForShots(shots, log) {
    const need = { head: new Set(), body: new Set(), wide: new Set() };
    for (const sh of shots) {
      if (!sh.fig || !sh.fig.seq) continue;
      const s = ST.seq[sh.fig.seq];
      if (!s) continue;
      const a = Math.max(0, Math.floor((sh.t0 - 0.2) * s.fps));
      const b = Math.min(s.n - 1, Math.ceil((sh.t1 + 0.2) * s.fps));
      for (let i = a; i <= b; i++) need[sh.fig.seq].add(i);
    }
    let total = 0, bytes = 0;
    for (const name of Object.keys(need)) {
      const s = ST.seq[name];
      if (!s) continue;
      const px = (s.w || s.size || 512) * (s.h || s.size || 512) * 4;
      for (const i of need[name]) {
        if (s.frames[i]) continue;
        const im = new Image();
        im.src = seqPath(s, i);
        s.frames[i] = im;
        total++; bytes += px;
      }
    }
    if (log) log('preload ' + total + ' 帧 ≈ ' + (bytes / 1048576).toFixed(0) + ' MB 解码后');
    return { total, bytes, need };
  }

  /* 等所有已排队的图解码完（同步渲染的前提） */
  async function settleImages(sets) {
    const jobs = [];
    for (const name of Object.keys(sets)) {
      const s = ST.seq[name];
      if (!s) continue;
      for (const i of sets[name]) {
        const im = s.frames[i];
        if (!im) continue;
        if (im.complete && im.naturalWidth) continue;
        jobs.push(new Promise(res => {
          if (im.decode) im.decode().then(res, res);
          else { im.onload = res; im.onerror = res; }
        }));
      }
    }
    await Promise.all(jobs);
    return jobs.length;
  }


  /* ---------------- 人物层 ----------------
     位置不是拍脑袋定的：先把文字排好，拿到 bbox，再把人物放到**离字最远且不出安全区**的锚点。
     「文字压在人身上」只在 storyboard 明确标 overlap 时才发生（全片限次），
     否则「文字没有严重遮挡」这条验收过不去。 */
  const SAFE = { m: 76 };
  function placeFigure(fig, bbox) {
    const dpr = ST.dpr;
    if (fig.mode === 'band') {
      const h = (fig.h || 620) * (fig.scale || 1);
      const w = h * ((fig.wRatio) || 0.752);
      const x = (fig.side === 'r') ? PV.W - SAFE.m - w : SAFE.m;
      const y = (fig.y != null ? fig.y : 0.30) * PV.H;
      return { x, y, w, h, kind: 'band' };
    }
    const r = (fig.r || 168) * (fig.scale || 1);
    if (fig.fixed) {
      // 满幅构图（密排墙等）没有空白可避：按 side 定点，让字直接从人身上过
      const x = fig.side === 'r' ? PV.W - r - 40 : r + 40;
      const y = (fig.y != null ? fig.y : 0.44) * PV.H;
      return { x, y, r, kind: 'disc' };
    }
    // 候选锚点：四角 + 左右中
    const cxL = SAFE.m + r + 16, cxR = PV.W - SAFE.m - r - 16;
    const cyTop = SAFE.m + r + 16, cyMid = PV.H * 0.44, cyBot = PV.H - SAFE.m - r - 16;
    const cands = [
      { x: cxR, y: cyMid }, { x: cxL, y: cyMid },
      { x: cxR, y: cyBot }, { x: cxL, y: cyBot },
      { x: cxR, y: cyTop }, { x: cxL, y: cyTop },
    ];
    if (fig.side === 'r') cands.sort((a, b) => (b.x - a.x) || (a.y - cyMid) - (b.y - cyMid) * 0);
    let best = cands[0], bestScore = -1e9;
    const bcx = (bbox[0] + bbox[2]) / 2, bcy = (bbox[1] + bbox[3]) / 2;
    for (const c of cands) {
      // 与 bbox 的「矩形距离」：负数说明相交，越负越糟
      const dx = Math.max(bbox[0] - (c.x + r), (c.x - r) - bbox[2], 0);
      const dy = Math.max(bbox[1] - (c.y + r), (c.y - r) - bbox[3], 0);
      let d = Math.hypot(dx, dy);
      if (dx === 0 && dy === 0) d = -Math.hypot(bcx - c.x, bcy - c.y) * 0.5;   // 相交
      // 偏好右侧（与阅读方向一致），但别为了右侧硬撞字
      const score = d + (c.x > PV.W / 2 ? 26 : 0) + (c.y > PV.H * 0.5 ? 8 : 0);
      if (score > bestScore) { bestScore = score; best = c; }
    }
    return { x: best.x, y: best.y, r, kind: 'disc' };
  }

  function drawFigure(ctx, fig, box, t, en, S) {
    S = S || ST.S;
    const name = fig.seq || 'head';
    const s = ST.seq[name];
    if (!s) return;
    if (fig.layer === 'front' || fig.layer === 'behind' || fig.layer === 'avoid') { /* 由调用时机决定 */ }
    const im = getFrame(name, t + (fig.tOff || 0));
    if (!im) return;
    const a = (fig.a == null ? 1 : fig.a) * (1 + en * 0.06);
    ctx.save();
    ctx.globalAlpha = clamp(a, 0, 1);
    ctx.imageSmoothingQuality = 'high';

    if (box.kind === 'disc') {
      const { x, y, r } = box;
      ctx.beginPath(); ctx.arc(x, y, r, 0, 6.283185307); ctx.clip();
      const D = r * 2 * 1.10;                    // 源图显示边长 = 直径×1.10（脸占 69%，边缘交给头发）
      const fx = ST.face.cx, fy = ST.face.cy;
      ctx.drawImage(im, x - fx * D, y - fy * D, D, D);
      /* 内缘暗角：贴图而不是现算径向渐变（逐帧极坐标渐变会引入 ±1 的抖动差） */
      ctx.drawImage(PV.fadeSprite('discVig', S.voidRGB, [[0, 0], [0.52, 0], [0.94, 0.16], [1, 0.58]], false),
        x - r, y - r, r * 2, r * 2);
    } else {
      const { x, y, w, h } = box;
      ctx.drawImage(im, x, y, w, h);
      /* 底部压暗：只在 55%~100% 这一段渐变，用 1×256 的竖直贴图拉伸 */
      ctx.drawImage(PV.fadeSprite('bandVig', S.voidRGB, [[0, 0], [1, 0.55]], true),
        x, y + h * 0.55, w, h * 0.45);
    }
    ctx.restore();

    // 环：只有 disc 有，极细一圈；粗环会变成「头像框」
    if (box.kind === 'disc') {
      ctx.save();
      ctx.globalAlpha = 0.20 + en * 0.14;
      ctx.strokeStyle = S.ink; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.arc(box.x, box.y, box.r + 0.8, 0, 6.283185307); ctx.stroke();
      ctx.restore();
    }
  }

  /* ---------------- 纸底 ---------------- */
  /* 纸底是一次性烘焙的：整幅渐变如果每帧现算，Chromium 的抖动相位会变，
     同一时刻两次渲染差 ±1~2（md5 不同、确定性验收直接挂）。烘焙一次后只贴图。 */
  let _paperC = null, _paperKey = null;
  function paperCanvas(S, pal) {
    const key = (pal.paper || '') + '|' + (pal.paper2 || '') + '|' + (pal.void || '');
    if (_paperKey === key && _paperC) return _paperC;
    const o = PV.offscreen('paperBase', PV.W, PV.H, 1);
    const g = o.ctx.createLinearGradient(0, 0, PV.W * 0.35, PV.H);
    g.addColorStop(0, rgba(S.paperRGB, 1));
    g.addColorStop(1, rgba(pal.paper2 ? PV.hex(pal.paper2) : mix(S.paperRGB, S.voidRGB, 0.55), 1));
    o.ctx.fillStyle = g; o.ctx.fillRect(0, 0, PV.W, PV.H);
    _paperKey = key; _paperC = o.c;
    return _paperC;
  }

  function drawPaper(ctx, t, shot) {
    const S = (shot && shot._S) || ST.S;
    /* 纸色必须跟着「本镜头的调色板」走。只读全局 ST.sb.palette 的话，
       反转镜头的字变了色、纸还是深色 —— 反转事件等于没发生。 */
    const pal = (shot && shot.pal) || ST.sb.palette || {};
    ctx.drawImage(paperCanvas(S, pal), 0, 0, PV.W, PV.H);
    // 环境光：极缓漂移的一团暖光，避免大面积死黑（死黑会让白字显得廉价）
    const ph = t / 23;
    PV.blob(ctx, PV.W * (0.30 + 0.06 * Math.sin(ph * 6.283)), PV.H * 0.36,
      PV.H * 0.86, S.accentRGB, 0.052 + 0.018 * Math.sin(ph * 4.1));
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
    const x0 = shot.t1 - (exit.dur == null ? 0.42 : exit.dur);
    let k = -1;                                   // 第几个可见字（空格不计入节奏）
    const EN_DUR = enter.dur == null ? 0.30 : enter.dur;
    const STAG = enter.stagger == null ? 0 : enter.stagger;
    const LEAD = enter.lead == null ? 0.04 : enter.lead;
    /* 逐字错相只能有**一个**来源。
       g.t0 来自逐词时间轴（字已经按自己的唱点错开了），此时再加 STAG*idx 就是
       双重计时 —— 实测 34 字的行要多等 1.5s 才铺满，而整句只有 3.6s，
       结果「唱完了字还没出齐」（t=24 的 diagonal 镜头整行只剩一两个笔画）。
       所以：有逐字轴 → 错相交给唱点；没有逐字轴（整行同 t0）→ 才用 stagger。 */
    const rnd1 = []; const rnd2 = [];
    for (let i = 0; i < glyphs.length; i++) { rnd1.push(rndA()); rnd2.push(rndB()); }

    for (let i = 0; i < glyphs.length; i++) {
      const g = glyphs[i];
      if (!g.isSpace) k++;
      const idx = k;
      // 重置：所有变换每帧从零开始，避免上一帧残留
      g.dx = 0; g.dy = 0; g.rot = 0; g.sc = 1; g.sx = 1; g.sy = 1;
      g.alpha = 1; g.vis = 1; g.clip = null;

      const charAxis = g.t0 != null;          // sync 镜头里 t0 已被整块覆写成 shot.t0，天然无错相
      const inT0 = (charAxis ? g.t0 : shot.t0) - LEAD + (charAxis ? 0 : STAG * idx);
      const e = clamp((t - inT0) / EN_DUR, 0, 1);
      if (e <= 0) { g.vis = 0; continue; }
      re(g, e, rnd1[i], rnd2[i], enter);

      /* 签名是 (g,t,ph,spec,amp)：第 4 个参数必须是 hold 规格本身。
         早先传成 (…, breath, hold, 1) —— spec 收到数字 1、amp 收到对象，
         jitter/trackIn 里出现 NaN，整行甚至整面墙一个字都不画（且毫无报错）。 */
      if (e >= 0.999) rh(g, t, rnd1[i], hold, 1);

      const xe = clamp((t - x0) / (exit.dur == null ? 0.42 : exit.dur), 0, 1);
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

  function paintFor(kind) { return PV.TREAT[kind] || PV.TREAT.solid; }
  /* TRIET 部件统一签名 (ctx,g,bx,by,a,S)，drawGlyphs 只给前 5 个 ⇒ 在这里把 S 绑进去 */
  const bindS = (fn, sp) => (c, g, bx, by, a) => fn(c, g, bx, by, a, sp);

  function drawLyrics(ctx, shot, t) {
    const S = shot._S || ST.S;
    const lay = shot._lay;
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

    PV.drawGlyphs(ctx, glyphs.filter(g => !g.emph), bindS(base, spec));
    if (shot.treatEmph) PV.drawGlyphs(ctx, glyphs.filter(g => g.emph), bindS(emph, specE));
    ctx.filter = 'none';
    ctx.globalAlpha = 1;
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
    const shot = shotAt(t);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    /* 整帧先铺底：相机 zoom<1 时纸底会缩进，露出的边框若不先铺色，
       留下的是**上一帧**的像素 —— 于是「同一时刻两次渲染」取决于上一帧画了什么，
       确定性直接崩（实测左边缘 6px 全高条带每次都不同）。 */
    ctx.fillStyle = rgba(S.paperRGB, 1);
    ctx.fillRect(0, 0, PV.W, PV.H);

    if (!shot) {                                   // 片头/片尾之外：只有纸底
      drawPaper(ctx, t, null);
      PV.drawGrain(ctx, 0, 0, PV.W, PV.H, 0.5, ST.dpr);
      return;
    }
    const en = shot._en == null ? 0.5 : shot._en;

    // 相机：推/摇/震，围绕画面中心
    const cam = shot.cam || null;
    const zoom = cam ? 1 + (cam.zoom || 0) : 1;
    let sx = 0, sy = 0;
    if (cam && cam.shake) {
      const r = PV.rng(ST.sb.meta.seed, shot.i, 9);
      const amp = cam.shake * (0.5 + en);
      sx = Math.sin(t * 53.1 + r() * 6.28) * amp;
      sy = Math.cos(t * 47.7 + r() * 6.28) * amp;
    }

    ctx.save();
    if (zoom !== 1 || sx || sy) {
      ctx.translate(PV.W / 2 + sx, PV.H / 2 + sy);
      ctx.scale(zoom, zoom);
      ctx.translate(-PV.W / 2, -PV.H / 2);
    }

    drawPaper(ctx, t, shot);
    drawDecor(ctx, shot.bg, t, shot.t0, shot._S);

    // 人物（后）
    let figBox = null;
    if (shot.fig) {
      figBox = placeFigure(shot.fig, shot._lay ? shot._lay.bbox : [0, 0, 0, 0]);
      shot._figBox = figBox;
      if (shot.fig.layer !== 'front') drawFigure(ctx, shot.fig, figBox, t, en, shot._S);
    }

    // 文字
    drawLyrics(ctx, shot, t);

    // 人物（前）：「文字从她身前过」只在 layer=front 的镜头成立
    if (shot.fig && shot.fig.layer === 'front') drawFigure(ctx, shot.fig, figBox, t, en, shot._S);

    // 前景装饰
    drawDecor(ctx, shot.decor, t, shot.t0, shot._S);

    ctx.restore();

    // 后期（不受相机影响）
    drawPost(ctx, shot.post, t, en);
    if (!(shot.post || []).some(f => f.k === 'vignette')) {
      /* 默认暗角：让画面四周沉下去，是「电影感」最便宜也最有效的一步 */
      PV.FX.vignette(ctx, { a: 0.60, r0: PV.H * 0.40, r1: PV.H * 1.02 }, { S });
    }
    PV.drawGrain(ctx, 0, 0, PV.W, PV.H, 0.55, ST.dpr);

    /* 角落极淡标记：歌名/来源改成片头标题卡承载后，全片只留这一行。
       压在画面右下角、alpha 0.16 —— 存在但不抢戏，观众要看得到、平时看不见。 */
    const mark = sb.meta && sb.meta.mark;
    if (mark) {
      PV.label(ctx, mark, PV.W - 64, PV.H - 52, 15, S.ink, 0.17, 500, 'right', 0.16);
    }
    ctx.globalAlpha = 1;
  };

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
    ST.dur = opt.dur;
    ST.S = buildStyle(opt.storyboard.palette || {}, ST.dpr);

    const live = opt.live || {};
    if (live.head) loadSeq('head', live.head);
    if (live.body) loadSeq('body', live.body);
    if (live.wide) loadSeq('wide', live.wide);
    if (live.face) ST.face = live.face;

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
      const layFn = PV.LAYOUT[(sh.layout && sh.layout.k) || 'center'];
      if (!layFn) continue;
      // line=-1 是片头标题卡：数组下标 -1 会取到最后一行，必须显式挡掉
      const line = (sh.line >= 0 ? LINES[sh.line] : null)
        || { text: sh.text || '', t0: sh.t0, t1: sh.t1, words: [], chars: [] };
      const stp = Object.assign({ line, text: (sh.text || line.text) }, sh.layout || {}, sh.type || {});
      const r = fitShot(layFn(ctx, null, stp));
      /* sync：冲击型构图（密排墙/极限裁切/满幅大字）必须整块同时出现。
         沿用逐词时间的话，墙是一格一格长出来的 —— 读作「卡了」而不是「砸下来」。 */
      if (sh.sync) for (const g of r.glyphs) { g.t0 = sh.t0; g.t1 = sh.t1; }
      sh._lay = r;
      sh._en = sh.energy == null ? 0.5 : sh.energy;
      // 逐镜头调色：全片只反转一次，所以这是一次「事件」而不是配色方案
      sh._S = sh.pal ? Object.assign({}, ST.S, buildStyle(sh.pal, ST.dpr)) : ST.S;
      total += r.glyphs.length;
    }
    ST.stats = { glyphs: total, shots: ST.shots.length };

    const pre = preloadForShots(ST.shots, m => console.log('[pv] ' + m));
    const settled = await settleImages(pre.need);

    /* 暖机渲染：把每个镜头的字形栅格、渐变、pattern 都先跑一遍。
       不做这一步，渲染循环里的「第 0 帧」会和后面所有帧不一样（约 100ms 的代价）。 */
    for (const sh of ST.shots) { try { PV.renderAt(ctx, (sh.t0 + sh.t1) / 2); } catch (e) { /* 单镜失败不阻断 */ } }
    try { PV.renderAt(ctx, 0); } catch (e) { /* 同上 */ }
    ST.ready = true;
    return {
      fonts: nf, glyphs: total, shots: ST.shots.length,
      seqs: Object.keys(ST.seq), dpr: ST.dpr,
      preload: pre.total, settled, memMB: Math.round(pre.bytes / 1048576),
      missing: Object.keys(PV.FACES).filter(k => !document.fonts.check(PV.cssFace(k, 64), 'A字')),
    };
  };

  PV.STAT = () => ST.stats;
  /* 调试用只读出口（tools/pv_stills.cjs 与人工核对构图时用） */
  PV.SHOTS = () => ST.shots || [];
  PV.shotAt = t => shotAt(t);
})();
