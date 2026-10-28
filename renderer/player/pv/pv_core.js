/*! pv_core.js —— 文字 PV 引擎底座：确定性随机 / 缓动 / 颜色 / 逐字量测 / 绘图原语。
 *
 *  设计坐标系恒为 1920×1080，4K 靠画布放大 + setTransform 整体缩放（与 v10 播放器同规）。
 *  所有「看起来该随机」的量（抖动、偏移、旋转相位）一律走 seeded rng，绝不用 Math.random
 *  —— 否则同一帧两次渲染不一致，长片的确定性验收直接失败。
 */
(() => {
  'use strict';

  const PV = (window.PV = window.PV || {});
  PV.W = 1920;
  PV.H = 1080;

  /* ---------------- 确定性随机 ---------------- */
  function mulberry32(a) {
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      let t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  /* 每「行 × 用途」一个独立流：改动某一行不会连锁改变其它行的取值 */
  PV.rng = (seed, a, b) => mulberry32((seed * 2654435761 ^ (a | 0) * 40503 ^ (b | 0) * 2246822519) >>> 0);
  PV.hash = s => {
    let h = 2166136261 >>> 0;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
    return h >>> 0;
  };

  /* ---------------- 标量 ---------------- */
  PV.clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
  PV.lerp = (a, b, t) => a + (b - a) * t;
  PV.smoothstep = (a, b, x) => { const t = PV.clamp((x - a) / (b - a || 1e-6), 0, 1); return t * t * (3 - 2 * t); };
  PV.pulse = (t, period, phase) => 0.5 + 0.5 * Math.sin((t / period + (phase || 0)) * 6.283185307);

  PV.ease = {
    linear: t => t,
    inQuad: t => t * t,
    outQuad: t => 1 - (1 - t) * (1 - t),
    inCubic: t => t * t * t,
    outCubic: t => 1 - Math.pow(1 - t, 3),
    inOutCubic: t => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
    outQuint: t => 1 - Math.pow(1 - t, 5),
    outExpo: t => (t >= 1 ? 1 : 1 - Math.pow(2, -10 * t)),
    outBack: t => { const c = 1.70158; return 1 + (c + 1) * Math.pow(t - 1, 3) + c * Math.pow(t - 1, 2); },
    outElastic: t => (t <= 0 ? 0 : t >= 1 ? 1
      : Math.pow(2, -10 * t) * Math.sin((t * 10 - 0.75) * 2.0943951) + 1),
  };

  /* ---------------- 颜色 ---------------- */
  PV.hex = h => {
    const s = h.replace('#', '');
    const v = s.length === 3 ? s.split('').map(c => c + c).join('') : s;
    return [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16), parseInt(v.slice(4, 6), 16)];
  };
  PV.rgba = (c, a) => 'rgba(' + (c[0] | 0) + ',' + (c[1] | 0) + ',' + (c[2] | 0) + ',' + (a == null ? 1 : a).toFixed(3) + ')';
  PV.mix = (a, b, t) => [PV.lerp(a[0], b[0], t), PV.lerp(a[1], b[1], t), PV.lerp(a[2], b[2], t)];
  PV.lum = c => (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;

  /* ---------------- 绘图原语 ---------------- */
  PV.line = (ctx, x0, y0, x1, y1, col, a, lw) => {
    if (a <= 0.002) return;
    ctx.globalAlpha = a; ctx.strokeStyle = col; ctx.lineWidth = lw || 1;
    ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
  };
  PV.rect = (ctx, x, y, w, h, col, a) => {
    if (a <= 0.002 || w <= 0 || h <= 0) return;
    ctx.globalAlpha = a; ctx.fillStyle = col; ctx.fillRect(x, y, w, h);
  };
  PV.rr = (ctx, x, y, w, h, r) => {
    const rr = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
  };
  PV.disc = (ctx, x, y, r, col, a) => {
    if (a <= 0.002) return;
    ctx.globalAlpha = a; ctx.fillStyle = col;
    ctx.beginPath(); ctx.arc(x, y, r, 0, 6.283185307); ctx.fill();
  };
  /* 字距（tracking）用 em 单位：正值加宽，负值压紧 */
  PV.label = (ctx, s, x, y, size, col, a, weight, align, trackingEm) => {
    if (a <= 0.002 || !s) return;
    ctx.globalAlpha = a; ctx.fillStyle = col;
    ctx.font = (weight || 500) + ' ' + size + 'px "Zen Kaku Gothic New", "Noto Sans SC", sans-serif';
    ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
    if (!trackingEm) { ctx.textAlign = align || 'left'; ctx.fillText(s, x, y); ctx.textAlign = 'left'; return; }
    const tr = trackingEm * size;
    let wsum = 0;
    for (const ch of s) wsum += ctx.measureText(ch).width + tr;
    let cx = align === 'right' ? x - wsum + tr : align === 'center' ? x - wsum / 2 + tr : x;
    for (const ch of s) { ctx.fillText(ch, cx, y); cx += ctx.measureText(ch).width + tr; }
  };

  /* ---------------- 离屏画布池 ---------------- */
  const _pool = {};
  PV.offscreen = (key, w, h, dpr) => {
    let o = _pool[key];
    const W = Math.max(1, Math.round(w)), H = Math.max(1, Math.round(h));
    if (!o) { o = _pool[key] = { c: document.createElement('canvas'), ctx: null }; o.ctx = o.c.getContext('2d'); }
    if (o.c.width !== W || o.c.height !== H) { o.c.width = W; o.c.height = H; }
    const g = o.ctx;
    g.setTransform(dpr || 1, 0, 0, dpr || 1, 0, 0);   // 单例：状态必须显式归零
    g.globalAlpha = 1;
    g.globalCompositeOperation = 'source-over';
    g.filter = 'none';
    g.clearRect(0, 0, W, H);
    return o;
  };

  /* ---------------- 纸纹 / 颗粒 ----------------
     seeded 生成一次后缓存为 CanvasPattern：逐帧重算噪声在 4K 下是纯粹的浪费。
     颗粒大小随 OUT_SCALE 放大，否则 4K 的颗粒会细到看不见。 */
  let _grain = null;
  PV.grain = (scale) => {
    // 返回 noise canvas 本体；pattern 由 drawGrain 按目标 ctx 缓存（pattern 与 ctx 绑定）
    if (_grain && _grain.scale === scale) return _grain.canvas;
    const N = 256;
    const c = document.createElement('canvas'); c.width = c.height = N;
    const g = c.getContext('2d');
    const img = g.createImageData(N, N);
    const r = mulberry32(0x9e3779b9);
    const cell = Math.max(1, Math.round(scale));
    for (let y = 0; y < N; y += cell) {
      for (let x = 0; x < N; x += cell) {
        const v = r();
        for (let dy = 0; dy < cell; dy++) {
          for (let dx = 0; dx < cell; dx++) {
            const i = ((y + dy) * N + (x + dx)) * 4;
            const val = v > 0.5 ? 255 : 0;
            img.data[i] = img.data[i + 1] = img.data[i + 2] = val;
            img.data[i + 3] = Math.round(Math.abs(v - 0.5) * 2 * 34);
          }
        }
      }
    }
    g.putImageData(img, 0, 0);
    _grain = { scale, canvas: c };
    return c;
  };

  /* 一条「胶片颗粒」层：用 noise canvas 平铺，极低 alpha，读作质感而不是噪点 */
  let _grainPat = null, _grainPatCtx = null, _grainPatSrc = null;
  PV.drawGrain = (ctx, x, y, w, h, a, scale) => {
    if (a <= 0.002) return;
    const c = PV.grain(scale);
    /* Pattern 与创建它的 ctx 绑定：换页/换 canvas 必须重建，否则拿到的是空图上色 */
    if (!_grainPat || _grainPatCtx !== ctx || _grainPatSrc !== c) {
      _grainPat = ctx.createPattern(c, 'repeat');
      _grainPatCtx = ctx;
      _grainPatSrc = c;
    }
    if (!_grainPat) return;
    ctx.save();
    ctx.globalAlpha = a;
    ctx.globalCompositeOperation = 'overlay';
    ctx.fillStyle = _grainPat;
    ctx.translate(x, y);
    ctx.fillRect(0, 0, w, h);
    ctx.restore();
  };

  /* 柔光团（径向渐变），用于「舞台灯扫过」这类环境光。
     ⚠ 不用 ctx.createRadialGradient 直接铺满画面：Chromium 对大面积极坐标渐变会做
     抖动（dithering），相位随调用次数变化 ⇒ 同一 t 连渲两次差 ±1~2，md5 不一致。
     改成「一次性烘焙的小贴图 + 拉伸贴」，既确定性又快 10 倍。 */
  const _sprite = {};
  PV.softSprite = (col, N) => {
    const n = N || 256;
    const key = col + '|' + n;
    if (_sprite[key]) return _sprite[key];
    const c = document.createElement('canvas'); c.width = c.height = n;
    const g = c.getContext('2d');
    const rg = g.createRadialGradient(n / 2, n / 2, 0, n / 2, n / 2, n / 2);
    rg.addColorStop(0, PV.rgba(col, 1));
    rg.addColorStop(0.55, PV.rgba(col, 0.42));
    rg.addColorStop(1, PV.rgba(col, 0));
    g.fillStyle = rg; g.fillRect(0, 0, n, n);
    return (_sprite[key] = c);
  };

  /* 圆窗内缘的暗角 / 竖幅底部的压暗：同样走「烘焙一次 + 贴图」，
     逐帧 createRadialGradient 会让大面积极坐标渐变出现抖动差（确定性验收会挂）。 */
  const _fade = {};
  PV.fadeSprite = (key, col, stops, vertical) => {
    const k = key + '|' + col;
    if (_fade[k]) return _fade[k];
    const N = 256;
    const c = document.createElement('canvas');
    c.width = vertical ? 1 : N; c.height = N;
    const g = c.getContext('2d');
    const rg = vertical
      ? g.createLinearGradient(0, 0, 0, N)
      : g.createRadialGradient(N / 2, N / 2, 0, N / 2, N / 2, N / 2);
    for (const [p, a] of stops) {
      const t = vertical ? Math.min(1, Math.max(0, p)) : Math.min(1, Math.max(0, p));
      rg.addColorStop(t, PV.rgba(col, a));
    }
    g.fillStyle = rg; g.fillRect(0, 0, c.width, c.height);
    return (_fade[k] = c);
  };

  PV.blob = (ctx, x, y, r, col, a) => {
    if (a <= 0.002 || r <= 0) return;
    const sp = PV.softSprite(col, 256);
    ctx.save();
    ctx.globalAlpha = a;
    ctx.drawImage(sp, x - r, y - r, r * 2, r * 2);
    ctx.restore();
  };
})();
