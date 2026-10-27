/* live_bg.js —— 直播原始画面融合层（v10 深色版专用）
 *
 * 目标：把「直播时演唱的原始画面」融进现有播放器，而不是在旁边另开一个窗口
 *      把元素堆起来。三种融合方式，共用同一套取帧/缓存，只在「放在哪、怎么收口」上不同：
 *
 *   lens   圆窗     头部特写放进圆形取景框，外框直接复用 drawHalo 的 64 段频谱环 ——
 *                   不新增任何装饰，环就是框。（用户点名的那一版）
 *   strip  竖幅     3:4 半身竖幅占据原封面位，标题照旧压入其下缘；右侧留给时间码。
 *                   「一块版面」而不是「一个窗口」。
 *   behind 歌词背后 半身像在歌词视口内、圆形羽化、压暗，读作「她在景深处唱」。
 *
 * ⚠ 已否决：整幅画面做全屏底衬（backdrop / stage）。原因不是调色，是素材本身 ——
 *   Live2D 半身构图放大到 1920 后脸占掉半屏，和圆窗里是同一张脸 ⇒ 像鬼影；
 *   且原画面含聊天栏/粉丝榜/弹幕，铺满等于「贴了一张直播截图」。裁掉两侧 UI 后
 *   仍然抢歌词，实测不可用。竖幅只占一块版面，以上问题全部消失。
 *
 * 为什么用图片序列而不是 <video>：播放器是逐帧离线渲染（renderFrame(i) → toBlob），
 * <video> 需要 currentTime seek + 等 seeked 事件，异步且偶发丢帧；图片序列是同步
 * 可用的（Image 解码完即 drawImage），缺帧时回落到最近的已加载帧，绝不黑屏。
 * ⚠ 序列走 file:// ⇒ 必须给 Chromium 加 --allow-file-access-from-files，
 *   否则 canvas 被标记 tainted，toBlob 直接 SecurityError（见 render_song.cjs）。
 *
 * 数据（tools/prepare_live.py 生成，经 job.live 注入）：
 *   { mode, t0, face:{cx,cy},
 *     head:{dir,n,fps,pattern,size},   // 640² 头部方形（圆窗用）
 *     body:{dir,n,fps,pattern,w,h},    // 3:4 半身（竖幅 / 歌词背后用）
 *     wide:{dir,n,fps,w,h,pattern} }   // 已裁两侧 UI 的全景（备用）
 *
 * 切换：URL ?live=lens|strip|behind|none
 */
(function () {
  'use strict';

  const L = window.LIVEBG = { mode: '', ok: false, SEQ: null, face: { cx: .5, cy: .5 }, t0: 0 };

  const CACHE = { head: new Map(), body: new Map(), wide: new Map() };
  const PEND = new Set();
  const MAXKEEP = 150;          // ≈640²×4B×150 = 245MB 上限，超了按距离裁

  const clamp = (v, a, b) => v < a ? a : (v > b ? b : v);
  const rgba = (c, a) => 'rgba(' + (c[0] | 0) + ',' + (c[1] | 0) + ',' + (c[2] | 0) + ',' + a + ')';

  function urlOf(dir, pat, k) {
    const f = pat.replace('%05d', String(k).padStart(5, '0'));
    let d = String(dir || '').replace(/\\/g, '/');
    if (/^[a-z]:/i.test(d)) d = 'file:///' + d;               // Windows 绝对路径
    else if (!/^file:/i.test(d)) d = d.replace(/^\/+/, '');   // 相对路径交给浏览器
    return d.replace(/\/$/, '') + '/' + f;
  }

  /* ---------------- 注入 ---------------- */
  L.set = function (j) {
    if (!j || !j.mode || j.mode === 'none') return false;
    L.mode = j.mode;
    L.t0 = j.t0 || 0;
    if (j.face) L.face = { cx: j.face.cx || .5, cy: j.face.cy || .5 };
    L.SEQ = {};
    if (j.head && j.head.dir && j.head.n > 0) {
      L.SEQ.head = { n: j.head.n | 0, fps: j.head.fps || 30, size: j.head.size || 640,
                     dir: j.head.dir, pat: j.head.pattern || 'h_%05d.jpg' };
    }
    if (j.body && j.body.dir && j.body.n > 0) {
      L.SEQ.body = { n: j.body.n | 0, fps: j.body.fps || 30, w: j.body.w || 580, h: j.body.h || 772,
                     dir: j.body.dir, pat: j.body.pattern || 'b_%05d.jpg' };
    }
    if (j.wide && j.wide.dir && j.wide.n > 0) {
      L.SEQ.wide = { n: j.wide.n | 0, fps: j.wide.fps || 15, w: j.wide.w || 960, h: j.wide.h || 540,
                     dir: j.wide.dir, pat: j.wide.pattern || 'w_%05d.jpg' };
    }
    L.ok = !!(L.SEQ.head || L.SEQ.body || L.SEQ.wide);
    return L.ok;
  };

  /* ---------------- 取帧（同步返回，缺帧回落最近已加载帧） ---------------- */
  function frame(kind, t) {
    const S = L.SEQ && L.SEQ[kind];
    if (!S || !S.n) return null;
    const k = clamp(Math.round((t - L.t0) * S.fps), 0, S.n - 1);
    const hit = CACHE[kind].get(k);
    if (hit) return hit;
    const key = kind + ':' + k;
    if (!PEND.has(key)) {
      PEND.add(key);
      const img = new Image();
      img.onload = function () { CACHE[kind].set(k, img); PEND.delete(key); };
      img.onerror = function () { PEND.delete(key); };
      img.src = urlOf(S.dir, S.pat, k + 1);           // 文件名从 1 开始
    }
    for (let d = 1; d <= 15; d++) {
      for (let s = -1; s <= 1; s += 2) {
        const near = CACHE[kind].get(k + s * d);
        if (near) return near;
      }
    }
    return null;
  }

  function prefetch(kind, t, span) {
    const S = L.SEQ && L.SEQ[kind];
    if (!S || !S.n) return;
    const k0 = clamp(Math.round((t - L.t0) * S.fps), 0, S.n - 1);
    const k1 = Math.min(k0 + span, S.n - 1);
    for (let k = k0; k <= k1; k++) frame(kind, L.t0 + k / S.fps);
  }

  let trimTick = 0;
  function trimAll(t) {
    if (++trimTick % 90) return;                    // 每 90 帧清一次，别每帧排序
    for (const kind in CACHE) {
      const S = L.SEQ && L.SEQ[kind], M = CACHE[kind];
      if (!S || M.size <= MAXKEEP) continue;
      const cur = Math.round((t - L.t0) * S.fps);
      const keys = Array.from(M.keys()).sort((a, b) => Math.abs(b - cur) - Math.abs(a - cur));
      for (let i = 0; i < keys.length - MAXKEEP; i++) M.delete(keys[i]);
    }
  }

  /* 供离线渲染器预热：一次性把前 n 帧塞进缓存 */
  L.warm = function (t, span) {
    if (!L.ok) return 0;
    let n = 0;
    for (const kind in L.SEQ) { prefetch(kind, t, span); n++; }
    return n;
  };

  /* ---------------- 离屏画布（behind 的羽化必须离屏做，
     destination-in 直接作用在主画布会擦掉底下的背景） ---------------- */
  let OC = null;
  function offscreen(px) {
    if (!OC || OC.c.width !== px) {
      const c = document.createElement('canvas');
      c.width = c.height = px;
      OC = { c: c, g: c.getContext('2d'), px: px };
    }
    return OC;
  }

  /* 带字距的文本（先判空再设 alpha，和 pro_bg.js 的 Tx 同约定） */
  function text(ctx, s, x, y, size, col, alpha, weight, align, ls) {
    if (!s || alpha <= 0.004) return;
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.fillStyle = col;
    ctx.font = (weight || 500) + ' ' + size + 'px "Inter","HarmonyOS Sans SC",sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    if (!ls) { ctx.textAlign = align || 'left'; ctx.fillText(s, x, y); }
    else {
      const ch = Array.from(s);
      let total = 0;
      for (const c of ch) total += ctx.measureText(c).width + ls;
      total -= ls;
      let px = align === 'right' ? x - total : (align === 'center' ? x - total / 2 : x);
      for (const c of ch) { ctx.fillText(c, px, y); px += ctx.measureText(c).width + ls; }
    }
    ctx.restore();
  }
  function measure(ctx, s, size, weight, ls) {
    ctx.save();
    ctx.font = (weight || 500) + ' ' + size + 'px "Inter","HarmonyOS Sans SC",sans-serif';
    let total = 0;
    for (const c of Array.from(s)) total += ctx.measureText(c).width + (ls || 0);
    ctx.restore();
    return total - (ls || 0);
  }

  /* 录制标记：琥珀呼吸点 + LIVE TAKE。放在画面底部渐隐带里，读作取景器信息 */
  function liveBadge(ctx, x, y, BR, t, align) {
    const label = 'LIVE TAKE 01';
    const size = 11.5, ls = 1.9;
    const lw = measure(ctx, label, size, 600, ls);
    const x0 = align === 'center' ? x - (lw + 13) / 2 : x;
    const pu = 0.5 + 0.5 * Math.sin(t * 2.3);
    ctx.save();
    ctx.fillStyle = rgba(BR.roseRGB, (0.42 + 0.50 * pu).toFixed(3));
    ctx.beginPath(); ctx.arc(x0 + 2.8, y - 4.0, 3.2, 0, 6.284); ctx.fill();
    ctx.restore();
    text(ctx, label, x0 + 13, y, size, 'rgba(226,233,242,1)', 0.80, 600, 'left', ls);
  }

  /* ================= 圆窗：头部特写 + 既有频谱环当外框 ================= */
  function drawLens(o) {
    const ctx = o.ctx, C = o.cover, BR = o.BRAND, en = o.en || 0, t = o.t;
    if (!L.SEQ.head) return;
    const cx = C.x + C.s / 2, cy = C.y + C.s / 2;
    const R = C.s * 0.495;                       // 直径 554，与封面 560 基本同宽

    const im = frame('head', t);
    prefetch('head', t, 50);
    if (!im) return;

    /* 源图显示边长。1.02 = 源图刚好外接圆 ⇒ 脸占 76%，脸颊会贴住框边，读作
       「从照片上挖了个洞」；1.10 退到 69%，边缘交给头发和背景，主体完整且
       放大倍数只有 1.10（源 640 显示 604）。 */
    const D = R * 2 * 1.10;
    const fx = L.face.cx, fy = L.face.cy;

    /* ① 圆内：画面 + 底部渐隐带 + 内暗角 */
    ctx.save();
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, 6.284); ctx.clip();
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(im, cx - fx * D, cy - fy * D, D, D);

    /* 能量微光：副歌时右下透一点暖色，像舞台灯扫过来 */
    if (en > 0.02) {
      const wm = ctx.createRadialGradient(cx + R * 0.34, cy + R * 0.42, 0, cx + R * 0.34, cy + R * 0.42, R * 0.92);
      wm.addColorStop(0, rgba(BR.roseRGB, (0.08 + en * 0.14).toFixed(3)));
      wm.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = wm; ctx.fillRect(cx - R, cy - R, R * 2, R * 2);
    }

    /* 底部渐隐带：给窗内那行录制标记一个可读的底，同时把画面「压进」框里 */
    const bg2 = ctx.createLinearGradient(0, cy + R * 0.62, 0, cy + R);
    bg2.addColorStop(0, 'rgba(4,7,12,0)');
    bg2.addColorStop(1, 'rgba(4,7,12,0.50)');
    ctx.fillStyle = bg2; ctx.fillRect(cx - R, cy + R * 0.62, R * 2, R * 0.38);

    const vg = ctx.createRadialGradient(cx, cy, R * 0.46, cx, cy, R);
    vg.addColorStop(0.00, 'rgba(4,7,12,0)');
    vg.addColorStop(0.62, 'rgba(4,7,12,0.10)');
    vg.addColorStop(0.88, 'rgba(4,7,12,0.42)');
    vg.addColorStop(1.00, 'rgba(4,7,12,0.62)');
    ctx.fillStyle = vg; ctx.fillRect(cx - R, cy - R, R * 2, R * 2);
    ctx.restore();

    /* ② 边框：一道细环 + 上缘高光弧（只 84°，整圈描白会变成塑料环） */
    ctx.save();
    ctx.lineCap = 'round';
    ctx.strokeStyle = 'rgba(226,233,242,0.15)'; ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.arc(cx, cy, R + 0.8, 0, 6.284); ctx.stroke();
    ctx.strokeStyle = 'rgba(255,255,255,' + (0.20 + en * 0.14).toFixed(3) + ')';
    ctx.lineWidth = 1.8;
    ctx.beginPath(); ctx.arc(cx, cy, R + 0.8, -2.30, -0.84); ctx.stroke();
    ctx.restore();

    /* ③ 录制标记：收进窗内底缘 —— 放窗下会和「Innocence」巨题挤在一起 */
    liveBadge(ctx, cx, cy + R * 0.875, BR, t, 'center');
  }

  /* ================= 竖幅：3:4 半身占一块版面 ================= */
  function drawStrip(o) {
    const ctx = o.ctx, C = o.cover, BR = o.BRAND, en = o.en || 0, t = o.t;
    const im = frame('body', t);
    prefetch('body', t, 45);
    if (!im) return;
    const S = L.SEQ.body;

    /* 竖幅宽度 = 封面宽 ×0.75（3:4），左缘与封面左缘齐平；标题仍压在其下缘
       （沿用既有编辑排版），右侧 688~828 留给时间码 */
    const bw = C.s * 0.75, bh = C.s;
    const bx = C.x, by = C.y;

    /* cover 铺满：源 580×772(0.751) 与 420×560(0.750) 几乎同比例，裁切量可忽略 */
    const k = Math.max(bw / S.w, bh / S.h);
    const dw = S.w * k, dh = S.h * k;

    ctx.save();
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(bx, by, bw, bh, 4);
    else ctx.rect(bx, by, bw, bh);
    ctx.clip();
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(im, bx + (bw - dw) / 2, by + (bh - dh) / 2, dw, dh);

    /* 底部渐隐：让画面「长进」背景，而不是一张贴上去的卡片 */
    const bg2 = ctx.createLinearGradient(0, by + bh * 0.66, 0, by + bh);
    bg2.addColorStop(0, 'rgba(4,7,12,0)');
    bg2.addColorStop(1, 'rgba(4,7,12,0.88)');
    ctx.fillStyle = bg2; ctx.fillRect(bx, by + bh * 0.66, bw, bh * 0.34);
    /* 能量微光（右上：舞台灯从上方扫下来） */
    if (en > 0.02) {
      const wm = ctx.createRadialGradient(bx + bw * 0.72, by + bh * 0.18, 0, bx + bw * 0.72, by + bh * 0.18, bw * 0.9);
      wm.addColorStop(0, rgba(BR.roseRGB, (0.06 + en * 0.12).toFixed(3)));
      wm.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = wm; ctx.fillRect(bx, by, bw, bh);
    }
    ctx.restore();

    ctx.save();
    ctx.strokeStyle = 'rgba(226,233,242,0.14)'; ctx.lineWidth = 1;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(bx + 0.5, by + 0.5, bw - 1, bh - 1, 4);
    else ctx.rect(bx + 0.5, by + 0.5, bw - 1, bh - 1);
    ctx.stroke();
    /* 上缘高光：只一半宽，暗示画面「被一盏侧灯照到」 */
    ctx.strokeStyle = 'rgba(255,255,255,' + (0.16 + en * 0.12).toFixed(3) + ')';
    ctx.lineWidth = 1.6; ctx.lineCap = 'round';
    ctx.beginPath(); ctx.moveTo(bx + 10, by + 1.2); ctx.lineTo(bx + bw * 0.62, by + 1.2); ctx.stroke();
    ctx.restore();

    liveBadge(ctx, bx + 20, by + bh - 22, BR, t, 'left');

    /* 右侧窄栏：时间码与日期，像胶片边码 —— 竖幅与歌词列之间的过渡 */
    /* 挪到竖幅右上（胶片边码在片头的传统位置），底部留给渐隐与录制标记 */
    const ix = bx + bw + 22, iy = by + 30;
    text(ctx, '2026.10.27', ix, iy, 10.5, 'rgba(226,233,242,1)', 0.40, 500, 'left', 1.3);
    text(ctx, '21:23:44', ix, iy + 17, 11.5, 'rgba(226,233,242,1)', 0.28, 500, 'left', 1.5);
  }

  /* ================= 背后：只在歌词视口内，圆形羽化 ================= */
  function drawBehind(o) {
    const ctx = o.ctx, LY = o.LY, en = o.en || 0, t = o.t;
    const kind = L.SEQ.body ? 'body' : 'head';
    const im = frame(kind, t);
    prefetch(kind, t, 30);
    if (!im) return;
    const S = L.SEQ[kind];

    /* 必须在离屏做：羽化 = 把这一层的 alpha 削掉，主画布上没有任何合成模式
       能做到「只降低我这一层、不动下面背景」。用背景色填遮罩看似可行，实测
       会留下一圈可见的矩形（背景是有渐变的，纯色填不满色差）。 */
    const PX = 640, O = offscreen(PX), g = O.g;
    g.setTransform(1, 0, 0, 1, 0, 0);          // 离屏是单例，状态必须显式归零
    g.globalCompositeOperation = 'source-over';
    g.globalAlpha = 1;
    g.clearRect(0, 0, PX, PX);

    /* ×0.86：人像贴着视口四边时没有渐隐余量，看着就是一张方形贴图 */
    const k = Math.min(PX / S.w, PX / S.h) * 0.86;
    const dw = S.w * k, dh = S.h * k;
    g.globalAlpha = 0.58 + en * 0.18;
    g.drawImage(im, (PX - dw) / 2, (PX - dh) / 2, dw, dh);
    g.globalAlpha = 1;

    /* 压暗到「景深」：白字歌词压在这层之上，对比度不能被吃掉。
       source-atop 而不是直接 fillRect —— 后者会把透明区也涂成半透明暗色。 */
    g.globalCompositeOperation = 'source-atop';
    g.fillStyle = 'rgba(7,10,15,0.30)';
    g.fillRect(0, 0, PX, PX);

    /* 圆形羽化：destination-in 的 alpha 直接乘上去，边缘归零 ⇒ 没有矩形边 */
    g.globalCompositeOperation = 'destination-in';
    const rg = g.createRadialGradient(PX / 2, PX / 2, PX * 0.07, PX / 2, PX / 2, PX * 0.50);
    rg.addColorStop(0.00, 'rgba(0,0,0,1)');
    rg.addColorStop(0.44, 'rgba(0,0,0,0.70)');
    rg.addColorStop(1.00, 'rgba(0,0,0,0)');
    g.fillStyle = rg; g.fillRect(0, 0, PX, PX);
    g.globalCompositeOperation = 'source-over';

    /* 30s 一周期 ±7px 的横移：读作「摄像机在缓慢挪」，一帧内变化 0.05px，不会闪 */
    const dx = Math.sin(t / 30 * 6.2832) * 7;
    ctx.save();
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(O.c, LY.x + dx, LY.y, LY.w, LY.w);
    ctx.restore();
  }

  /* ================= 对外挂钩 ================= */
  /* 歌词背后 —— 插在 PROBG 之后、drawCover 之前 */
  L.bg = function (o) {
    if (!L.ok) return;
    trimAll(o.t);
    if (L.mode === 'behind') drawBehind(o);
  };

  /* 圆窗 / 竖幅 —— 接管封面位 */
  L.fg = function (o) {
    if (!L.ok) return;
    if (L.mode === 'lens') drawLens(o);
    else if (L.mode === 'strip') drawStrip(o);
  };

  /* 接管封面位时，原封面不再绘制 */
  L.takesCover = function () { return L.mode === 'lens' || L.mode === 'strip'; };
  /* 竖幅是矩形且占满封面位，外圈那 64 段频谱环会横穿画面 ⇒ 只在 lens 保留 */
  L.hidesHalo = function () { return L.mode === 'strip'; };
})();
