/* recipes.js -- a GENERIC fx-factory library (upstream effect-cookbook §0 pattern).
 *
 * Why: the old driver had 8 hand-written factories, all borrowed from the zanko case study
 * (snow / moon halo / ink wash / cinnabar / neon / dial / rain-on-glass / motes). Upstream
 * ships 94 recipes in STYLE_RESEARCH §8 + 60 condensed in effect-cookbook.md and explicitly
 * warns against re-using the case study's motif set. This file is the reusable half: 18
 * factories written against MOTIF CLASSES (falling, rising, radial, linear, texture, fracture,
 * colour) instead of one film's imagery, so any song can draw a different subset.
 *
 * Every factory is a pure function of S.t / S.lt / S.p / S.clock and an integer seed.
 * No Math.random, no Date.now -- the renderer must be frame-identical across workers.
 *
 * Usage (songcut.timeline.js):
 *   const R = window.RECIPES.build(cfg);
 *   R.pollen(cfg.palette.glow)  ->  { type: 'custom', fn }
 */
(() => {
  'use strict';
  const Z = window.Z;
  const W = 1920, H = 1080;

  // index: which recipes suit which job. songcut.timeline uses this to draw a different
  // subset per film instead of always the same five.
  const CLASSES = {
    falling: ['petalFall', 'confettiBurst', 'sparkTrail'],
    rising: ['pollen', 'sparkTrail', 'breathFog'],
    radial: ['ripples', 'clockTicks', 'speedLines', 'halftone'],
    linear: ['sweepLines', 'arrowFlow', 'gridPersp', 'sweepBars'],
    texture: ['paperFiber', 'vinylCrackle', 'scanlines', 'dustShaft'],
    fracture: ['sliceGlitch', 'inkBleed'],
    colour: ['bokehOrbs', 'edgeBloom', 'halftone'],
  };

  function build(cfg) {
    const P = cfg.palette || {};
    const A = (c, a) => Z.rgba(c, a);
    const spec = fn => ({ type: 'custom', fn });
    const q = (S, fps = 12) => Z.quant(S.t, fps);
    const r = (i, s) => Z.rnd(i, s);

    return {
      // ---------------------------------------------------------------- rising: soft fluff
      pollen: (color) => spec((ctx, S) => {
        const t = q(S);
        ctx.save(); ctx.fillStyle = A(color || P.glow, 0.65);
        for (let i = 0; i < 110; i++) {
          const sp = 10 + 34 * r(i, 101);
          const x = (r(i, 102) * W + 26 * Math.sin(t * (0.3 + r(i, 103)) + i)) % W;
          const y = ((r(i, 104) * H - t * sp) % (H + 60) + H + 60) % (H + 60) - 30;
          const rad = 0.9 + 2.4 * Math.pow(r(i, 105), 2);
          ctx.globalAlpha = 0.10 + 0.42 * r(i, 106) * (0.6 + 0.4 * Math.sin(t * 1.3 + i));
          ctx.beginPath(); ctx.arc(x, y, rad, 0, Z.TAU); ctx.fill();
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- radial: water rings
      ripples: (color) => spec((ctx, S) => {
        const t = S.t, c = S.clock;
        ctx.save(); ctx.lineWidth = 2;
        for (let k = 0; k < 5; k++) {
          const age = t - (c.nearest(t) - k * c.spb * 2);
          if (age < 0 || age > 3.2) continue;
          const a = 1 - age / 3.2;
          const rad = age * 190;
          for (let m = 0; m < 3; m++) {
            const yy = H * (0.62 + 0.1 * m);
            ctx.strokeStyle = A(color || P.accent, 0.20 * a * (1 - m * 0.22));
            ctx.beginPath();
            ctx.ellipse(W * 0.5, yy, rad * (1 + m * 0.16), rad * 0.16, 0, 0, Z.TAU);
            ctx.stroke();
          }
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- radial: dial ticks
      clockTicks: (color) => spec((ctx, S) => {
        const t = S.t, c = S.clock;
        ctx.save();
        const cx = W * 0.5, cy = H * 0.5;
        for (let k = 0; k < 3; k++) {
          const age = t - (c.nearest(t) - k * c.spb * 4);
          if (age < 0 || age > 2.4) continue;
          const a = 1 - age / 2.4, rad = 130 + age * 160;
          for (let i = 0; i < 12; i++) {
            const ang = (i / 12) * Z.TAU + k * 0.1;
            const x0 = cx + Math.cos(ang) * rad, y0 = cy + Math.sin(ang) * rad * 0.62;
            const x1 = cx + Math.cos(ang) * (rad + 26), y1 = cy + Math.sin(ang) * (rad + 26) * 0.62;
            ctx.strokeStyle = A(color || P.accent, 0.26 * a);
            ctx.lineWidth = i % 3 === 0 ? 3 : 1.4;
            ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
          }
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- radial: manga speed lines
      speedLines: (color) => spec((ctx, S) => {
        const t = q(S), cx = W * 0.5, cy = H * 0.46;
        ctx.save(); ctx.strokeStyle = A(color || P.glow, 0.5);
        for (let i = 0; i < 54; i++) {
          const ang = r(i, 201) * Z.TAU;
          const r0 = H * (0.62 + 0.5 * r(i, 202));
          const len = H * (0.1 + 0.34 * r(i, 203)) * (0.6 + 0.4 * Math.sin(t * 2 + i));
          ctx.globalAlpha = 0.05 + 0.16 * r(i, 204);
          ctx.lineWidth = 1 + 4 * r(i, 205);
          ctx.beginPath();
          ctx.moveTo(cx + Math.cos(ang) * r0, cy + Math.sin(ang) * r0);
          ctx.lineTo(cx + Math.cos(ang) * (r0 + len), cy + Math.sin(ang) * (r0 + len));
          ctx.stroke();
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- linear: perspective grid
      gridPersp: (color) => spec((ctx, S) => {
        const t = S.t, p = Z.clamp(S.p);
        const vx = W * (0.5 + 0.06 * Math.sin(t * 0.4)), vy = H * 0.44;
        ctx.save(); ctx.strokeStyle = A(color || P.accent, 0.22);
        ctx.lineWidth = 1.2;
        for (let i = -9; i <= 9; i++) {
          ctx.globalAlpha = 0.12 + 0.10 * (1 - Math.abs(i) / 9);
          ctx.beginPath(); ctx.moveTo(vx, vy);
          ctx.lineTo(vx + i * W * 0.20, H + 40); ctx.stroke();
        }
        for (let k = 0; k < 12; k++) {
          const f = ((k / 12 + t * 0.18 * (0.6 + p)) % 1);
          const y = vy + (H - vy) * f * f;
          ctx.globalAlpha = 0.06 + 0.18 * (1 - f);
          ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- linear: arrows streaming
      arrowFlow: (color) => spec((ctx, S) => {
        const t = S.t;
        ctx.save(); ctx.fillStyle = A(color || P.accent, 0.30);
        for (let i = 0; i < 16; i++) {
          const speed = 150 + 260 * r(i, 301);
          const x = ((r(i, 302) * W + t * speed) % (W + 220)) - 110;
          const y = H * (0.1 + 0.82 * r(i, 303));
          const s = 9 + 16 * r(i, 304);
          ctx.globalAlpha = 0.10 + 0.26 * r(i, 305);
          ctx.beginPath();
          ctx.moveTo(x + s, y); ctx.lineTo(x - s * 0.7, y - s * 0.62);
          ctx.lineTo(x - s * 0.7, y + s * 0.62); ctx.closePath(); ctx.fill();
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- linear: bars sweeping
      sweepBars: (color) => spec((ctx, S) => {
        const t = S.t;
        ctx.save(); ctx.globalCompositeOperation = 'screen';
        for (let i = 0; i < 9; i++) {
          const h = 3 + 21 * r(i, 401);
          const y = ((r(i, 402) * H + t * (110 + 220 * r(i, 403))) % (H + 120)) - 60;
          ctx.fillStyle = A(i % 2 ? (color || P.accent) : P.glow, 0.10 + 0.16 * r(i, 404));
          ctx.fillRect(0, y, W, h);
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- linear: long sweeps
      sweepLines: (color) => spec((ctx, S) => {
        const t = q(S);
        ctx.save(); ctx.strokeStyle = A(color || P.glow, 0.4);
        for (let i = 0; i < 70; i++) {
          const y = r(i, 501) * H;
          const len = 80 + 420 * r(i, 502);
          const x = ((r(i, 503) * (W + len) + t * (200 + 400 * r(i, 504))) % (W + len)) - len;
          ctx.globalAlpha = 0.04 + 0.14 * r(i, 505);
          ctx.lineWidth = 0.8 + 2.2 * r(i, 506);
          ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + len, y); ctx.stroke();
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- radial: halftone dots
      halftone: (color) => spec((ctx, S) => {
        const t = S.t, step = 34;
        ctx.save(); ctx.fillStyle = A(color || P.accent, 1);
        for (let y = 0; y < H + step; y += step) {
          for (let x = 0; x < W + step; x += step) {
            const cx = x + (Math.floor(y / step) % 2 ? step / 2 : 0);
            const d = Math.hypot(cx - W * 0.5, y - H * 0.5) / Math.hypot(W * 0.5, H * 0.5);
            const rad = step * 0.46 * Z.clamp(0.15 + d * 0.85 + 0.12 * Math.sin(t * 1.1 - d * 6));
            if (rad < 0.6) continue;
            ctx.globalAlpha = 0.10 + 0.16 * (1 - d);
            ctx.beginPath(); ctx.arc(cx, y, rad, 0, Z.TAU); ctx.fill();
          }
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- texture: scanlines
      scanlines: (color) => spec((ctx, S) => {
        const t = S.t, off = (t * 46) % 4;
        ctx.save(); ctx.fillStyle = A(color || P.void, 0.5);
        for (let y = -4; y < H + 4; y += 4) {
          ctx.globalAlpha = 0.05 + 0.05 * (0.5 + 0.5 * Math.sin((y + off) * 0.5));
          ctx.fillRect(0, y + off, W, 1.4);
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- texture: paper grain
      paperFiber: (color) => spec((ctx, S) => {
        ctx.save(); ctx.strokeStyle = A(color || P.mid, 0.7);
        for (let i = 0; i < 220; i++) {
          const x = r(i, 601) * W, y = r(i, 602) * H;
          const len = 6 + 46 * r(i, 603), ang = (r(i, 604) - 0.5) * 0.5;
          ctx.globalAlpha = 0.02 + 0.05 * r(i, 605);
          ctx.lineWidth = 0.6 + 1.1 * r(i, 606);
          ctx.beginPath(); ctx.moveTo(x, y);
          ctx.lineTo(x + Math.cos(ang) * len, y + Math.sin(ang) * len); ctx.stroke();
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- texture: film scratches
      vinylCrackle: (color) => spec((ctx, S) => {
        const t = q(S);
        ctx.save(); ctx.fillStyle = A(color || P.glow, 0.6);
        for (let i = 0; i < 150; i++) {
          const x = (r(i, 701) * W + t * 3 * r(i, 702)) % W;
          const y = r(i, 703) * H;
          const on = 0.3 + 0.7 * Math.abs(Math.sin(t * (0.7 + 2 * r(i, 704)) + i));
          ctx.globalAlpha = (0.02 + 0.10 * r(i, 705)) * on;
          ctx.fillRect(x, y, 1 + 2 * r(i, 706), 1 + 2 * r(i, 707));
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- texture: dust in a shaft
      dustShaft: (color) => spec((ctx, S) => {
        const t = S.t;
        const sx = W * 0.34, sy = -H * 0.1, ex = W * 0.62, ey = H * 1.1;
        const g = ctx.createLinearGradient(sx, sy, ex, ey);
        g.addColorStop(0, A(color || P.glow, 0.20));
        g.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.save(); ctx.globalCompositeOperation = 'screen';
        ctx.fillStyle = g;
        ctx.beginPath();
        ctx.moveTo(W * 0.30, 0); ctx.lineTo(W * 0.44, 0);
        ctx.lineTo(W * 0.78, H); ctx.lineTo(W * 0.28, H);
        ctx.closePath(); ctx.fill();
        for (let i = 0; i < 80; i++) {
          const k = (r(i, 801) + t * 0.04 * (0.4 + r(i, 802))) % 1;
          const x = Z.lerp(W * 0.36, W * 0.62, k) + (r(i, 803) - 0.5) * 260;
          const y = Z.lerp(-40, H + 40, k);
          ctx.fillStyle = A(color || P.glow, 0.06 + 0.22 * r(i, 804));
          ctx.beginPath(); ctx.arc(x, y, 0.8 + 1.8 * r(i, 805), 0, Z.TAU); ctx.fill();
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- fracture: slice glitch
      sliceGlitch: (color) => spec((ctx, S) => {
        const tq = Z.quant(S.t, 10);
        ctx.save();
        for (let i = 0; i < 9; i++) {
          const seed = Math.floor(tq * 6) * 31 + i;
          if (r(seed, 901) > 0.55) continue;
          const y = r(seed, 902) * H;
          const h = 8 + 54 * r(seed, 903);
          const dx = (r(seed, 904) - 0.5) * 220;
          ctx.fillStyle = A(color || P.accent, 0.06 + 0.10 * r(seed, 905));
          ctx.fillRect(0, y, W, h);
          ctx.fillStyle = A(color || P.accent, 0.05);
          ctx.fillRect(dx, y, W, h);
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- fracture: ink bleed
      inkBleed: (color) => spec((ctx, S) => {
        const k = Z.clamp((S.t - S.shot.t0) / Math.max(0.6, S.dur));
        ctx.save(); ctx.globalCompositeOperation = 'multiply';
        for (let i = 0; i < 7; i++) {
          const x = W * (0.1 + 0.8 * r(i, 1001)), y = H * (0.15 + 0.72 * r(i, 1002));
          const rad = H * (0.06 + 0.36 * k) * (0.5 + r(i, 1003));
          const g = ctx.createRadialGradient(x, y, 0, x, y, rad);
          g.addColorStop(0, A(color || P.void, 0.30 * (1 - k * 0.45)));
          g.addColorStop(1, 'rgba(0,0,0,0)');
          ctx.fillStyle = g; ctx.fillRect(x - rad, y - rad, rad * 2, rad * 2);
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- falling: petals
      petalFall: (color) => spec((ctx, S) => {
        const t = q(S);
        ctx.save(); ctx.fillStyle = A(color || P.accent, 0.55);
        for (let i = 0; i < 46; i++) {
          const sp = 40 + 110 * r(i, 1101);
          const x = (r(i, 1102) * W + t * 30 * (0.4 + r(i, 1103)) + 40 * Math.sin(t * 0.8 + i)) % W;
          const y = ((r(i, 1104) * H + t * sp) % (H + 80)) - 40;
          const s = 5 + 12 * r(i, 1105);
          ctx.globalAlpha = 0.12 + 0.34 * r(i, 1106);
          ctx.save(); ctx.translate(x, y); ctx.rotate(t * (0.6 + r(i, 1107)) + i);
          ctx.beginPath(); ctx.ellipse(0, 0, s, s * 0.52, 0, 0, Z.TAU); ctx.fill(); ctx.restore();
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- falling: sparks
      sparkTrail: (color) => spec((ctx, S) => {
        const t = S.t;
        ctx.save(); ctx.globalCompositeOperation = 'screen';
        for (let i = 0; i < 34; i++) {
          const life = 1.4 + 1.2 * r(i, 1201);
          const ph = (t / life + r(i, 1202)) % 1;
          const x = W * (0.1 + 0.8 * r(i, 1203)) + 60 * Math.sin(t * 1.4 + i);
          const y = H * (1.05 - ph * 0.9);
          const a = Math.sin(ph * Math.PI) * (0.24 + 0.5 * r(i, 1204));
          ctx.strokeStyle = A(color || P.warm || P.accent, a);
          ctx.lineWidth = 1.2 + 1.6 * r(i, 1205);
          ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - 4, y + 22); ctx.stroke();
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- falling: confetti
      confettiBurst: (color) => spec((ctx, S) => {
        const t = S.t;
        ctx.save();
        for (let i = 0; i < 40; i++) {
          const sp = 90 + 260 * r(i, 1301);
          const x = W * (0.1 + 0.8 * r(i, 1302)) + 50 * Math.sin(t + i);
          const y = ((r(i, 1303) * H + t * sp) % (H + 100)) - 50;
          const s = 6 + 12 * r(i, 1304);
          ctx.globalAlpha = 0.2 + 0.4 * r(i, 1305);
          ctx.fillStyle = i % 3 === 0 ? A(P.accent, 0.8) : (i % 3 === 1 ? A(color || P.glow, 0.8) : A(P.warm || P.gold || P.accent, 0.8));
          ctx.save(); ctx.translate(x, y); ctx.rotate(t * (1 + r(i, 1306)) + i);
          ctx.fillRect(-s / 2, -s / 6, s, s / 3); ctx.restore();
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- rising: breath fog
      breathFog: (color) => spec((ctx, S) => {
        const t = S.t;
        ctx.save(); ctx.globalCompositeOperation = 'screen';
        for (let i = 0; i < 5; i++) {
          const ph = ((t * 0.16 + r(i, 1401)) % 1);
          const x = W * (0.2 + 0.6 * r(i, 1402)) + 40 * Math.sin(t * 0.6 + i);
          const y = H * (0.9 - ph * 0.55);
          const rad = 90 + 260 * ph;
          const g = ctx.createRadialGradient(x, y, 0, x, y, rad);
          g.addColorStop(0, A(color || P.glow, 0.10 * (1 - ph)));
          g.addColorStop(1, 'rgba(0,0,0,0)');
          ctx.fillStyle = g; ctx.fillRect(x - rad, y - rad, rad * 2, rad * 2);
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- colour: bokeh
      bokehOrbs: (color) => spec((ctx, S) => {
        const t = S.t;
        ctx.save(); ctx.globalCompositeOperation = 'screen';
        for (let i = 0; i < 16; i++) {
          const x = W * (0.05 + 0.9 * r(i, 1501)) + 70 * Math.sin(t * (0.16 + 0.2 * r(i, 1502)) + i);
          const y = H * (0.08 + 0.84 * r(i, 1503)) + 50 * Math.cos(t * (0.12 + 0.18 * r(i, 1504)) + i);
          const rad = 26 + 120 * Math.pow(r(i, 1505), 1.6);
          const g = ctx.createRadialGradient(x, y, rad * 0.15, x, y, rad);
          g.addColorStop(0, A(color || P.accent, 0.14 + 0.16 * r(i, 1506)));
          g.addColorStop(1, 'rgba(0,0,0,0)');
          ctx.fillStyle = g; ctx.fillRect(x - rad, y - rad, rad * 2, rad * 2);
        }
        ctx.restore();
      }),

      // ---------------------------------------------------------------- colour: edge bloom
      edgeBloom: (color) => spec((ctx, S) => {
        const p = S.clock.pulse(S.t, 8);
        const a = 0.10 + 0.22 * p;
        ctx.save(); ctx.globalCompositeOperation = 'screen';
        const g = ctx.createRadialGradient(W / 2, H / 2, H * 0.32, W / 2, H / 2, H * 0.92);
        g.addColorStop(0, 'rgba(0,0,0,0)');
        g.addColorStop(1, A(color || P.accent, a));
        ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
        ctx.restore();
      }),
    };
  }

  window.RECIPES = { CLASSES, build };
})();
