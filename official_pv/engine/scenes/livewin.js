/* livewin.js — the stream footage as a SHORT GUEST, never as the film.
 *
 * Rewritten for two hard requirements:
 *
 * (A) Official kit: SKILL.md says this pipeline is "not for editing existing footage or clip
 *     compilations"; 02 §5-§6 wants verses, typographic moments, pure motion-graphics interludes and
 *     real holds. The footage therefore appears only at a few motivated nodes, off-centre, for a few
 *     seconds. The rest of the film belongs to the `plates` scenes and to JIZURA. The full-frame
 *     blurred-footage backdrop is OFF by default now: it was a second way of putting her on screen
 *     for 100 % of the runtime.
 *
 * (B) NO WARP. Every operation on a stream frame is one of:
 *       - uniform-scale + crop      (cover: one scale factor, equal for x and y)
 *       - colour / filter           (grade, multiply/screen, blur on the optional backdrop)
 *       - alpha mask                (destination-in / source-in; the shape changes, pixels do not)
 *       - 1:1 blit                  (same source and destination rectangle)
 *     There is no stretch, squeeze, shear, perspective, bend, slice displacement or non-uniform scale.
 *     The tear/dissolve/duo moulds from the previous version were REMOVED because they compressed
 *     bands (target w = src w - gap) and squeezed the big duo panel to 0.82x horizontally.
 *
 * A runtime guard (blit9) checks every 9-argument drawImage: if dw/sw !== dh/sh it refuses to draw
 * the distorted version, records the violation and redraws at a single uniform scale. Static audit:
 * tools/audit_no_warp.mjs.
 *
 * args: { mode, x, y, w, h, zoom, panX, panY, pal, bg, edge, glow, fadeX, fadeY, cy, rad, hard,
 *         maskAspect, bgA (0 = no full-frame footage plate), fx, fxBack }
 */
(() => {
  'use strict';
  const Z = window.Z;
  const W = 1920, H = 1080;
  const FPS = 30;
  const dirOf = () => (Z.LIVE_DIR || 'assets/live/frames/');   // set per song by songcut.timeline.js
  const lastFrame = () => (window.Z && Z.LIVE_LAST) || 7008;
  const fileOf = i => `${dirOf()}f_${String(i).padStart(5, '0')}.jpg`;
  // A live insert is a SHORT window. Preloading `i+1..i+16` used to walk off the
  // window and fire 404s; a shot that starts on an act boundary (70.0) used to
  // request frames before the extracted range (70.2) and show an empty plate.
  const windows = () => (Z.LIVE_WINDOWS && Z.LIVE_WINDOWS.length ? Z.LIVE_WINDOWS : null);
  const valid = j => { const ws = windows(); return !ws || ws.some(([a, b]) => j >= a && j <= b); };
  const snap = j => {
    const ws = windows(); if (!ws || valid(j)) return j;
    let best = j, d = Infinity;
    for (const [a, b] of ws) { const c = Math.max(a, Math.min(b, j)); if (Math.abs(c - j) < d) { d = Math.abs(c - j); best = c; } }
    return best;
  };

  // ---------------------------------------------------------------- frame cache
  const CACHE = new Map(), ORDER = [];
  const KEEP = 110;

  function load(i) {
    i = Math.max(1, Math.min(lastFrame(), Math.round(i)));
    const hit = CACHE.get(i);
    if (hit) return hit;
    if (!valid(i)) {
      const empty = { img: null, ok: true, p: Promise.resolve(null) };
      CACHE.set(i, empty); ORDER.push(i);
      return empty;
    }
    const rec = { img: new Image(), ok: false };
    rec.p = new Promise(res => {
      rec.img.onload = () => { rec.ok = true; res(rec.img); };
      rec.img.onerror = () => { rec.ok = true; res(null); };
    });
    rec.img.src = Z.ROOT + fileOf(i);
    CACHE.set(i, rec); ORDER.push(i);
    while (ORDER.length > KEEP) { const o = ORDER.shift(); if (o !== i) CACHE.delete(o); }
    return rec;
  }
  async function ensure(i) {
    const j = snap(i);
    const rec = load(j);
    const img = await rec.p;          // resolves to the Image, or null on a real load error
    for (let k = 1; k <= 16; k++) load(j + k);
    return img;
  }

  // ---------------------------------------------------------------- no-warp guard
  // One place for every scaled blit. A stream frame may only ever be drawn with ONE scale factor.
  const AUDIT = Z.WARP_AUDIT = { calls: 0, violations: 0, details: [], uniformScales: 0 };
  function blit9(ctx, src, sx, sy, sw, sh, dx, dy, dw, dh) {
    AUDIT.calls++;
    if (!(sw > 0 && sh > 0)) return;
    const kx = dw / sw, ky = dh / sh;
    if (Math.abs(kx - ky) > 1e-4) {
      AUDIT.violations++;
      if (AUDIT.details.length < 8) AUDIT.details.push({ sx, sy, sw, sh, dw, dh, kx, ky });
      const k = Math.min(kx, ky);                 // repair to a single uniform scale, never distort
      dw = sw * k; dh = sh * k;
    }
    AUDIT.uniformScales++;
    ctx.drawImage(src, sx, sy, sw, sh, dx, dy, dw, dh);
  }
  // 1:1 copy of the whole scratch canvas — 3-arg drawImage, no scale and no source crop at all
  function blit1(ctx, src) { AUDIT.calls++; AUDIT.uniformScales++; ctx.drawImage(src, 0, 0); }

  // ---------------------------------------------------------------- scratch canvases
  let ST = null, STX = null, SW = 0, SH = 0;
  let TP = null, TPX = null, TW = 0, TH = 0;
  function grab(which, w, h) {
    w = Math.ceil(w); h = Math.ceil(h);
    const isST = which === 'st';
    let C = isST ? ST : TP, X = isST ? STX : TPX, cw = isST ? SW : TW, ch = isST ? SH : TH;
    if (!C) { C = document.createElement('canvas'); X = C.getContext('2d'); }
    if (cw < w || ch < h) { cw = Math.max(cw, w); ch = Math.max(ch, h); C.width = cw; C.height = ch; }
    X.setTransform(1, 0, 0, 1, 0, 0);
    X.globalAlpha = 1; X.globalCompositeOperation = 'source-over'; X.filter = 'none';
    X.clearRect(0, 0, cw, ch);
    if (isST) { ST = C; STX = X; SW = cw; SH = ch; } else { TP = C; TPX = X; TW = cw; TH = ch; }
    return X;
  }

  const FACE_X = 0.5;

  // Cover = ONE uniform scale + a crop. This is the only way a stream frame ever changes size.
  function cover(ctx, img, w, h, zoom = 1, panX = 0, panY = 0, ax = FACE_X) {
    const s = Math.max(w / img.width, h / img.height) * zoom;   // single scale for BOTH axes
    const dw = img.width * s, dh = img.height * s;
    const dx = Z.clamp(w / 2 - ax * dw + panX, Math.min(0, w - dw), 0);
    const dy = (h - dh) / 2 + panY;
    ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(img, dx, dy, dw, dh);                          // 5-arg: uniform scale, no crop of source
    return { dx, dy, dw, dh, s, ox: img.width };
  }

  // ---------------------------------------------------------------- masks (alpha only)
  function featherX(ctx, w, h, l, r) {
    if (l <= 0 && r <= 0) return;
    const g = ctx.createLinearGradient(0, 0, w, 0);
    g.addColorStop(0, 'rgba(0,0,0,0)');
    g.addColorStop(Z.clamp(l / w, 0.0001, 0.999), 'rgba(0,0,0,1)');
    g.addColorStop(Z.clamp(1 - r / w, 0.001, 0.9999), 'rgba(0,0,0,1)');
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.save(); ctx.globalCompositeOperation = 'destination-in';
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h); ctx.restore();
  }
  function featherY(ctx, w, h, t, b) {
    if (t <= 0 && b <= 0) return;
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, 'rgba(0,0,0,0)');
    g.addColorStop(Z.clamp(t / h, 0.0001, 0.999), 'rgba(0,0,0,1)');
    g.addColorStop(Z.clamp(1 - b / h, 0.001, 0.9999), 'rgba(0,0,0,1)');
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.save(); ctx.globalCompositeOperation = 'destination-in';
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h); ctx.restore();
  }
  // The aspect here belongs to the MASK GRADIENT ONLY. It never touches the picture's transform.
  // (Previously named `stretch`; renamed so nobody mistakes a shaped alpha falloff for a warp.)
  function featherEllipse(ctx, w, h, cy = 0.44, hard = 0.50, r = 0.46, maskAspect = 1.35) {
    ctx.save(); ctx.globalCompositeOperation = 'destination-in';
    ctx.translate(w / 2, h * cy); ctx.scale(1, (h / w) * maskAspect);
    const R = w * Z.clamp(r, 0.18, 0.50);
    const g = ctx.createRadialGradient(0, 0, R * hard, 0, 0, R);
    g.addColorStop(0, 'rgba(0,0,0,1)');
    g.addColorStop(0.42, 'rgba(0,0,0,0.97)');
    g.addColorStop(0.68, 'rgba(0,0,0,0.72)');
    g.addColorStop(0.86, 'rgba(0,0,0,0.28)');
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g; ctx.fillRect(-R * 2, -R * 2, R * 4, R * 4);
    ctx.restore();
  }

  // ---------------------------------------------------------------- colour only
  function grade(ctx, w, h, pal, t, beat) {
    const P = pal || {};
    if (P.mulA) { ctx.save(); ctx.globalCompositeOperation = 'multiply'; ctx.globalAlpha = P.mulA; ctx.fillStyle = P.mul || '#241040'; ctx.fillRect(0, 0, w, h); ctx.restore(); }
    if (P.scrA) { ctx.save(); ctx.globalCompositeOperation = 'screen'; ctx.globalAlpha = P.scrA; ctx.fillStyle = P.scr || '#FFC978'; ctx.fillRect(0, 0, w, h); ctx.restore(); }
    ctx.save(); ctx.globalCompositeOperation = 'screen';
    const g = ctx.createRadialGradient(w * 0.5, h * 0.27, 0, w * 0.5, h * 0.32, w * 0.56);
    g.addColorStop(0, Z.rgba(P.face || '#FFE6BC', (P.faceA ?? 0.22) * (1 + 0.20 * beat)));
    g.addColorStop(0.34, Z.rgba(P.face || '#FFE6BC', (P.faceA ?? 0.22) * 0.24));
    g.addColorStop(0.70, Z.rgba(P.face || '#FFE6BC', (P.faceA ?? 0.22) * 0.06));
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
    ctx.globalCompositeOperation = 'overlay'; ctx.globalAlpha = P.contrastA ?? 0.22;
    const c = ctx.createRadialGradient(w * 0.5, h * 0.36, 0, w * 0.5, h * 0.44, w * 0.95);
    c.addColorStop(0, 'rgba(150,150,150,1)'); c.addColorStop(1, 'rgba(120,120,120,1)');
    ctx.fillStyle = c; ctx.fillRect(0, 0, w, h);
    ctx.restore();
    ctx.save(); ctx.globalCompositeOperation = 'multiply';
    const v = ctx.createLinearGradient(0, 0, 0, h);
    v.addColorStop(0, 'rgba(10,5,20,0.26)'); v.addColorStop(0.42, 'rgba(10,5,20,0)'); v.addColorStop(1, 'rgba(10,5,20,0.38)');
    ctx.fillStyle = v; ctx.fillRect(0, 0, w, h); ctx.restore();
  }

  // Optional full-frame blurred plate. Off by default (bgA = 0) because it put the stream on screen
  // for the whole film. When explicitly enabled it is still uniform-scale + blur only.
  let BD = null, BDX = null;
  function backdrop(ctx, img, a, beat) {
    const amount = a.bgA ?? 0;
    if (amount <= 0) return;
    if (!BD) { BD = document.createElement('canvas'); BD.width = W; BD.height = H; BDX = BD.getContext('2d'); }
    BDX.setTransform(1, 0, 0, 1, 0, 0); BDX.globalAlpha = 1; BDX.globalCompositeOperation = 'source-over';
    BDX.clearRect(0, 0, W, H);
    BDX.filter = `blur(${a.bgBlur ?? 34}px) saturate(0.86)`;
    const bgAx = a.bgAx ?? FACE_X;
    const needZoom = 1 / (1 - 2 * Math.abs(0.5 - bgAx));
    cover(BDX, img, W, H, Math.max(a.bgZoom ?? 1.16, needZoom), a.bgPanX || 0, a.bgPanY ?? 40, bgAx);
    BDX.filter = 'none';
    BDX.fillStyle = Z.rgba((a.pal || {}).mul || '#0B0618', a.bgDim ?? 0.62);
    BDX.fillRect(0, 0, W, H);
    ctx.save(); ctx.globalAlpha = amount * (0.92 + 0.08 * beat);
    ctx.drawImage(BD, 0, 0);                                    // 1:1 full frame, no scale
    ctx.restore();
  }

  function source(w, h, img, a, S) {
    const T = grab('tp', w, h);
    const M = cover(T, img, w, h, a.zoom || 1, a.panX || 0, a.panY || 0, a.ax ?? FACE_X);
    grade(T, w, h, a.pal, S.t, S.clock.pulse(S.t, 9));
    return M;
  }

  // ---------------------------------------------------------------- mask-only moulds
  const MOLDS = {
    soft(ctx, img, w, h, a, S) {
      source(w, h, img, a, S);
      blit1(ctx, TP, w, h);
      featherEllipse(ctx, w, h, a.cy ?? 0.44, a.hard ?? 0.50, a.rad ?? 0.47, a.maskAspect ?? 1.35);
      return { x: 0, y: 0, w, h };
    },
    seam(ctx, img, w, h, a, S) {
      source(w, h, img, a, S);
      blit1(ctx, TP, w, h);
      featherX(ctx, w, h, a.fadeX ?? 60, a.fadeX ?? 60);
      featherY(ctx, w, h, a.fadeY ?? 40, a.fadeY ?? 68);
      return { x: 0, y: 0, w, h };
    },
    iris(ctx, img, w, h, a, S) {
      source(w, h, img, a, S);
      blit1(ctx, TP, w, h);
      featherEllipse(ctx, w, h, 0.50, a.hard ?? 0.62, a.rad ?? 0.48, (w / h));
      return { x: 0, y: 0, w, h };
    },
    bloom(ctx, img, w, h, a, S) {
      source(w, h, img, a, S);
      const blobs = a.blobs || [
        [0.50, 0.40, 0.44, 1.00], [0.31, 0.52, 0.28, 0.94], [0.69, 0.31, 0.26, 0.88],
        [0.53, 0.71, 0.31, 0.78], [0.47, 0.17, 0.24, 0.66],
      ];
      ctx.save(); ctx.globalCompositeOperation = 'lighter';
      for (const [bx, by, br, k] of blobs) {
        const g = ctx.createRadialGradient(bx * w, by * h, 0, bx * w, by * h, br * w);
        g.addColorStop(0, `rgba(255,255,255,${k})`);
        g.addColorStop(0.52, `rgba(255,255,255,${k * 0.92})`);
        g.addColorStop(0.78, `rgba(255,255,255,${k * 0.45})`);
        g.addColorStop(1, 'rgba(255,255,255,0)');
        ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
      }
      ctx.globalCompositeOperation = 'source-in';
      blit1(ctx, TP, w, h);                                    // picture fills the light shape; 1:1
      ctx.restore();
      return { x: 0, y: 0, w, h };
    },
  };
  const SAFE_MODES = Object.keys(MOLDS);

  function halo(ctx, box, t, a, S) {
    const cx = box.x + box.w / 2, cy = box.y + box.h * 0.42;
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, box.w * (1.7 + 0.07 * S.clock.pulse(t, 8)));
    g.addColorStop(0, Z.rgba(a.edge || '#FFD27A', 0.125 * (a.glow ?? 1)));
    g.addColorStop(0.40, Z.rgba(a.edge || '#FFD27A', 0.045 * (a.glow ?? 1)));
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.save(); ctx.globalCompositeOperation = 'lighter'; ctx.fillStyle = g;
    ctx.fillRect(cx - box.w * 2.2, cy - box.w * 2.2, box.w * 4.4, box.w * 4.4); ctx.restore();
  }

  Z.scene('livewin', {
    async draw(ctx, S) {
      const a = S.args || {};
      const t = S.t + (a.tOff || 0);
      const img = await ensure(Math.round(t * FPS) + 1);
      const beat = S.clock.pulse(t, 9);

      Z.draw.vgrad(ctx, a.bg || [[0, '#04080A'], [0.46, '#0A1418'], [1, '#12242B']]);
      if (img) backdrop(ctx, img, a, beat);

      // uniform growth only: w and h share the same factor, and cover() re-fits at ONE scale
      const pg = Z.clamp(S.p ?? 0);
      const push = 1 + (a.push ?? 0) * Z.ease.inOutSine(pg);
      const grow = (1 + (a.beatGrow ?? 0) * beat) * push;
      const w0 = a.w ?? 560, h0 = a.h ?? 940;
      const w = w0 * grow, h = h0 * grow;
      const sx = Math.sin(t * 0.83 + (a.phase || 0)) * (a.swayX ?? 0);
      const sy = Math.cos(t * 0.61 + (a.phase || 0)) * (a.swayY ?? 0);
      const x = (a.x ?? (W - w0) / 2) + (w0 - w) / 2 + (a.dx || 0) + sx;
      const y = (a.y ?? (H - h0) / 2) + (h0 - h) / 2 + (a.dy || 0) + sy;
      const box = { x, y, w, h };

      if (a.glow !== 0) halo(ctx, box, t, a, S);
      if (a.fxBack) for (const f of a.fxBack) Z.fx(ctx, S, f);

      if (img) {
        const mode = SAFE_MODES.includes(a.mode) ? a.mode : 'seam';
        const c = grab('st', Math.max(w, 32), Math.max(h, 32));
        const r = MOLDS[mode](c, img, w, h, a, S) || { x: 0, y: 0, w, h };
        ctx.save();
        ctx.globalAlpha = a.alpha ?? 1;
        if (a.blend) ctx.globalCompositeOperation = a.blend;
        ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
        blit9(ctx, ST, 0, 0, r.w, r.h, x + r.x, y + r.y, r.w, r.h);   // 1:1 into the page
        ctx.restore();
      }
      if (a.fx) for (const f of a.fx) Z.fx(ctx, S, f);
    },
  });

  Z.LIVEWIN = { frameAt: t => Math.round(t * FPS) + 1, molds: SAFE_MODES, ensure, audit: AUDIT };
})();
