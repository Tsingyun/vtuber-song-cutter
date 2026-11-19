/*! pv_parts_ext.js —— 部件库扩充（P0-3 hold / P1-4 decor·bg·treat·fx）。
 *
 *  与 JIZURA 的 11p_decorB / 11p_enterB 同构：核心件留在 pv_parts.js / pv_bg.js，
 *  扩充件独立成包，注册进同一批注册表（PV.HOLD / PV.DECOR / PV.BG / PV.TREAT / PV.FX）。
 *
 *  纪律（与核心件一致）：
 *   · 随机一律走 PV.rng(seed, ...)，禁止 Math.random —— 双渲必须字节一致；
 *   · hold 类「无输入时等于恒等变换」，否则静止帧会抖；
 *   · decor/bg 只建立构图，不做「贴纸」。
 */
(() => {
  'use strict';
  const PV = window.PV;
  const { rgba, clamp, ease } = PV;
  const TAU = 6.283185307;

  /* ============================================================
     hold 扩充（P0-3）：字停留期间的呼吸感
     ============================================================ */
  const Ho = PV.HOLD;

  /* 摆：整行像挂在绳上一样缓慢左右摆（逐字相位差读作「一串字」） */
  Ho.sway = (g, t, ph) => {
    g.rot += Math.sin((t / 5.2 + ph * 0.4) * TAU) * 0.016;
    g.dx += Math.sin((t / 5.2 + ph * 0.4) * TAU) * g.size * 0.030;
  };

  /* 脉冲：极缓的「心跳」缩放，周期 2.8s，幅度比 breathe 大但更离散 */
  Ho.pulse = (g, t, ph) => {
    const k = Math.sin((t / 2.8 + ph * 0.25) * TAU);
    g.sc *= 1 + k * 0.018;
  };

  /* 倾漂：缓慢倾斜 + 反向平移，读作「纸片在风里」 */
  Ho.tiltDrift = (g, t, ph) => {
    const k = Math.sin((t / 6.6 + ph * 0.6) * TAU);
    g.rot += k * 0.022;
    g.dy += Math.cos((t / 6.6 + ph * 0.6) * TAU) * g.size * 0.018;
  };

  /* 反漂：相邻字反向漂移，制造「字与字之间在互相拉扯」的张力 */
  Ho.counterDrift = (g, t, ph, spec, amp, idx) => {
    const s = (idx != null && idx % 2) ? -1 : 1;
    g.dx += s * Math.sin((t / 4.4 + ph) * TAU) * g.size * 0.026;
    g.dy += s * Math.cos((t / 5.1 + ph) * TAU) * g.size * 0.014;
  };

  /* 微闪：alpha 的高频极小幅抖动，像老胶片的密度变化（幅度必须小） */
  Ho.shimmer = (g, t, ph) => {
    g.alpha *= 0.975 + 0.025 * Math.sin(t * 9.7 + ph * TAU);
  };

  /* 整行呼吸：所有字同相位缩放（breathe 是逐字相位差，这个是「整块在呼吸」） */
  Ho.breatheRow = (g, t, ph, spec, amp, idx) => {
    g.sc *= 1 + Math.sin(t / 3.6 * TAU) * 0.014;
  };

  /* 沉降：极缓的向下沉，给长句一点「重量」 */
  Ho.settle = (g, t, ph) => {
    g.dy += clamp(t / 9, 0, 1) * g.size * 0.030;
    g.rot *= 1 - clamp(t / 12, 0, 1) * 0.4;
  };

  /* ============================================================
     decor 扩充（P1-4）：版面骨架与「元信息」暗示
     ============================================================ */
  const D = PV.DECOR;

  /* 角标刻度：四角的小刻度线组，比 cropMarks 更密，读作「印刷规矩」 */
  D.cornerTicks = (ctx, s, st) => {
    const m = s.m == null ? 56 : s.m, L = s.len == null ? 12 : s.len, n = s.n || 3;
    const a = s.a == null ? 0.20 : s.a, gap = s.gap == null ? 9 : s.gap;
    const P = [[m, m, 1, 1], [PV.W - m, m, -1, 1], [m, PV.H - m, 1, -1], [PV.W - m, PV.H - m, -1, -1]];
    for (const [x, y, sx, sy] of P) {
      for (let i = 0; i < n; i++) {
        const o = i * gap;
        PV.line(ctx, x + o * sx, y, x + o * sx, y + L * sy, st.S.ink, a * (1 - i * 0.22), 1);
        PV.line(ctx, x, y + o * sy, x + L * sx, y + o * sy, st.S.ink, a * (1 - i * 0.22), 1);
      }
    }
  };

  /* 十字准心：一个点的定位标记，给画面「这是取景器」的暗示 */
  D.crosshair = (ctx, s, st) => {
    const x = s.x == null ? PV.W * 0.5 : s.x, y = s.y == null ? PV.H * 0.5 : s.y;
    const r = s.r == null ? 26 : s.r, a = s.a == null ? 0.28 : s.a;
    PV.line(ctx, x - r, y, x + r, y, st.S.ink, a, 1);
    PV.line(ctx, x, y - r, x, y + r, st.S.ink, a, 1);
    ctx.globalAlpha = a * 0.8; ctx.strokeStyle = st.S.ink; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(x, y, r * 0.45, 0, TAU); ctx.stroke();
    ctx.globalAlpha = 1;
  };

  /* 胶带：一条半透明的斜色块，像贴在纸上的胶带（印刷品的「手工感」） */
  D.tape = (ctx, s, st) => {
    const x = s.x == null ? PV.W * 0.12 : s.x, y = s.y == null ? PV.H * 0.16 : s.y;
    const w = s.w == null ? 260 : s.w, h = s.h == null ? 34 : s.h;
    const rot = ((s.rot == null ? -7 : s.rot) * Math.PI) / 180;
    ctx.save();
    ctx.translate(x + w / 2, y + h / 2); ctx.rotate(rot);
    ctx.globalAlpha = s.a == null ? 0.16 : s.a;
    ctx.fillStyle = s.col === 'accent' ? rgba(st.S.accentRGB, 1) : st.S.ink;
    ctx.fillRect(-w / 2, -h / 2, w, h);
    /* 胶带两端的锯齿 */
    ctx.globalAlpha = (s.a == null ? 0.16 : s.a) * 0.55;
    for (let i = -2; i <= 2; i++) {
      ctx.fillRect(-w / 2 + i * 7, -h / 2, 3, 4);
      ctx.fillRect(w / 2 - 10 + i * 7, h / 2 - 4, 3, 4);
    }
    ctx.restore();
  };

  /* 标签片：小圆角块 + 字，像贴在样品上的标签 */
  D.chip = (ctx, s, st) => {
    const x = s.x == null ? 80 : s.x, y = s.y == null ? 90 : s.y;
    const h = s.h == null ? 30 : s.h;
    const txt = s.text || 'LABEL';
    ctx.font = `600 ${s.size || 14}px ${st.S.body || 'sans-serif'}`;
    const w = ctx.measureText(txt).width + (s.pad == null ? 22 : s.pad * 2);
    const r = h / 2;
    ctx.globalAlpha = s.a == null ? 0.82 : s.a;
    ctx.fillStyle = s.fill === 'accent' ? rgba(st.S.accentRGB, 1) : st.S.ink;
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.arc(x + w - r, y + r, r, -Math.PI / 2, Math.PI / 2);
    ctx.lineTo(x + r, y + h); ctx.arc(x + r, y + r, r, Math.PI / 2, -Math.PI / 2);
    ctx.closePath(); ctx.fill();
    ctx.globalAlpha = 1;
    ctx.fillStyle = s.fill === 'accent' ? (st.S.bg || '#000') : (st.S.bg || '#fff');
    PV.label(ctx, txt, x + w / 2, y + h * 0.62, s.size || 14, ctx.fillStyle, 0.95, 600, 'center', 0.14);
  };

  /* 标尺：带刻度的横尺，给画面「度量」的暗示 */
  D.ruler = (ctx, s, st) => {
    const x0 = s.x0 == null ? 120 : s.x0, x1 = s.x1 == null ? PV.W - 120 : s.x1;
    const y = s.y == null ? PV.H - 96 : s.y, n = s.n || 24;
    const a = s.a == null ? 0.16 : s.a;
    PV.line(ctx, x0, y, x1, y, st.S.ink, a, 1);
    for (let i = 0; i <= n; i++) {
      const x = x0 + (x1 - x0) * i / n;
      const big = i % 6 === 0;
      PV.line(ctx, x, y, x, y + (big ? 12 : 6), st.S.ink, a * (big ? 1.0 : 0.6), 1);
      if (big && s.numbers) {
        PV.label(ctx, String(i), x, y + 26, 11, st.S.ink, a * 1.2, 500, 'center', 0.2);
      }
    }
  };

  /* 箭头簇：2~3 个方向一致的小箭头，给画面「指向」 */
  D.arrows = (ctx, s, st) => {
    const n = s.n || 3, x = s.x == null ? 160 : s.x, y = s.y == null ? 220 : s.y;
    const a = s.a == null ? 0.24 : s.a, dir = s.dir == null ? 1 : s.dir;
    for (let i = 0; i < n; i++) {
      const yy = y + i * (s.gap == null ? 26 : s.gap);
      const L = (s.len == null ? 34 : s.len) * (1 - i * 0.16);
      PV.line(ctx, x, yy, x + L * dir, yy, st.S.ink, a * (1 - i * 0.22), 1.5);
      PV.line(ctx, x + L * dir, yy, x + (L - 9) * dir, yy - 5, st.S.ink, a * (1 - i * 0.22), 1.5);
      PV.line(ctx, x + L * dir, yy, x + (L - 9) * dir, yy + 5, st.S.ink, a * (1 - i * 0.22), 1.5);
    }
  };

  /* 括号：一对直角括号，把一块空白「框」起来 */
  D.brackets = (ctx, s, st) => {
    const x = s.x == null ? PV.W * 0.30 : s.x, y = s.y == null ? PV.H * 0.30 : s.y;
    const w = s.w == null ? PV.W * 0.40 : s.w, h = s.h == null ? PV.H * 0.40 : s.h;
    const L = s.len == null ? 30 : s.len, a = s.a == null ? 0.22 : s.a;
    const corners = [[x, y, 1, 1], [x + w, y, -1, 1], [x, y + h, 1, -1], [x + w, y + h, -1, -1]];
    for (const [cx, cy, sx, sy] of corners) {
      PV.line(ctx, cx, cy, cx + L * sx, cy, st.S.ink, a, 1.5);
      PV.line(ctx, cx, cy, cx, cy + L * sy, st.S.ink, a, 1.5);
    }
  };

  /* 星芒：四角星，给爆点一个「闪」的标记（少量、小） */
  D.sparkle = (ctx, s, st) => {
    const x = s.x == null ? PV.W * 0.7 : s.x, y = s.y == null ? PV.H * 0.3 : s.y;
    const r = s.r == null ? 18 : s.r, a = s.a == null ? 0.5 : s.a;
    const col = s.col === 'ink' ? st.S.ink : rgba(st.S.accentRGB, 1);
    ctx.save(); ctx.globalAlpha = a; ctx.fillStyle = col;
    ctx.beginPath();
    for (let i = 0; i < 8; i++) {
      const ang = (i / 8) * TAU, rr = i % 2 ? r * 0.34 : r;
      const px = x + Math.cos(ang) * rr, py = y + Math.sin(ang) * rr;
      i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    }
    ctx.closePath(); ctx.fill();
    ctx.restore();
  };

  /* 警示斜纹：一窄条 45° 斜纹，读作「印刷警示带」 */
  D.warnStripe = (ctx, s, st) => {
    const x = s.x == null ? 0 : s.x, y = s.y == null ? PV.H - 40 : s.y;
    const w = s.w == null ? PV.W : s.w, h = s.h == null ? 18 : s.h;
    const a = s.a == null ? 0.16 : s.a, step = s.step == null ? 14 : s.step;
    ctx.save();
    ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip();
    ctx.globalAlpha = a; ctx.strokeStyle = st.S.ink; ctx.lineWidth = step * 0.42;
    for (let i = -h; i < w + h; i += step) {
      ctx.beginPath(); ctx.moveTo(x + i, y + h); ctx.lineTo(x + i + h, y); ctx.stroke();
    }
    ctx.restore();
  };

  /* 印章：一个圆形戳记的暗示（双环 + 短字） */
  D.stamp = (ctx, s, st) => {
    const x = s.x == null ? PV.W * 0.82 : s.x, y = s.y == null ? PV.H * 0.22 : s.y;
    const r = s.r == null ? 54 : s.r, a = s.a == null ? 0.18 : s.a;
    const rot = ((s.rot == null ? -12 : s.rot) * Math.PI) / 180;
    ctx.save();
    ctx.translate(x, y); ctx.rotate(rot);
    ctx.globalAlpha = a; ctx.strokeStyle = st.S.ink; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(0, 0, r, 0, TAU); ctx.stroke();
    ctx.beginPath(); ctx.arc(0, 0, r * 0.78, 0, TAU); ctx.stroke();
    ctx.restore();
    PV.label(ctx, s.text || 'OK', x, y + 5, s.size || 15, st.S.ink, a * 2.4, 700, 'center', 0.3);
  };

  /* 票券：一条带齿孔的横条，像票根 */
  D.ticket = (ctx, s, st) => {
    const x = s.x == null ? 140 : s.x, y = s.y == null ? PV.H * 0.78 : s.y;
    const w = s.w == null ? 360 : s.w, h = s.h == null ? 52 : s.h;
    const a = s.a == null ? 0.14 : s.a;
    ctx.save();
    ctx.globalAlpha = a; ctx.fillStyle = st.S.ink;
    ctx.fillRect(x, y, w, h);
    /* 两端的齿孔 */
    ctx.globalCompositeOperation = 'destination-out';
    for (let i = 0; i < 4; i++) {
      ctx.beginPath(); ctx.arc(x + 8 + i * 11, y + h / 2, 4, 0, TAU); ctx.fill();
      ctx.beginPath(); ctx.arc(x + w - 8 - i * 11, y + h / 2, 4, 0, TAU); ctx.fill();
    }
    ctx.restore();
    PV.line(ctx, x + 52, y + 8, x + 52, y + h - 8, st.S.ink, a * 2.2, 1);
    PV.label(ctx, s.text || 'ADMIT ONE', x + 70, y + h * 0.62, s.size || 13, st.S.ink, a * 3.2, 600, 'left', 0.24);
  };

  /* 时间轴：一条带节点的横线，读作「这一句在时间上的位置」 */
  D.timeline = (ctx, s, st) => {
    const x0 = s.x0 == null ? 130 : s.x0, x1 = s.x1 == null ? PV.W - 130 : s.x1;
    const y = s.y == null ? PV.H - 70 : s.y, n = s.n || 8;
    const a = s.a == null ? 0.18 : s.a, p = clamp(s.p == null ? 0.3 : s.p, 0, 1);
    PV.line(ctx, x0, y, x1, y, st.S.ink, a * 0.8, 1);
    for (let i = 0; i <= n; i++) {
      const q = i / n, x = x0 + (x1 - x0) * q;
      const on = q <= p;
      ctx.globalAlpha = on ? 0.6 : a * 0.55;
      ctx.fillStyle = on ? rgba(st.S.accentRGB, 1) : st.S.ink;
      ctx.beginPath(); ctx.arc(x, y, on ? 3.4 : 2.2, 0, TAU); ctx.fill();
    }
    ctx.globalAlpha = 1;
  };

  /* ============================================================
     decor 晨光绘本五件（Innocence v2）
     与上面的「印刷元信息」相反：这五件是**手账/相册**语汇 —— 缝线、角贴、
     微星、泪滴、圆角框。它们的任务是把「纸」变成「被珍藏的一页」。
     ============================================================ */

  /* 缝线：沿内框走一圈虚线针脚（手账的「缝边」）。极淡，只在近看时读得出 */
  D.stitchLines = (ctx, s, st) => {
    const m = s.m == null ? 64 : s.m;
    const dash = s.dash == null ? 14 : s.dash, gap = s.gap == null ? 10 : s.gap;
    const a = s.a == null ? 0.16 : s.a;
    const x0 = m, y0 = m, x1 = PV.W - m, y1 = PV.H - m;
    ctx.save();
    ctx.strokeStyle = st.S.ink;
    ctx.globalAlpha = a;
    ctx.lineWidth = s.lw == null ? 1.6 : s.lw;
    ctx.setLineDash([dash, gap]);
    ctx.lineDashOffset = -(st.t || 0) * 6;    // 针脚极缓「走线」
    const r = s.r == null ? 22 : s.r;
    ctx.beginPath();
    ctx.moveTo(x0 + r, y0);
    ctx.lineTo(x1 - r, y0); ctx.quadraticCurveTo(x1, y0, x1, y0 + r);
    ctx.lineTo(x1, y1 - r); ctx.quadraticCurveTo(x1, y1, x1 - r, y1);
    ctx.lineTo(x0 + r, y1); ctx.quadraticCurveTo(x0, y1, x0, y1 - r);
    ctx.lineTo(x0, y0 + r); ctx.quadraticCurveTo(x0, y0, x0 + r, y0);
    ctx.closePath();
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();
  };

  /* 相册角贴：四角的三角形护角（老相册固定照片的黑角）。
        「初见 / 小事物」用它 —— 语义是「这张照片被好好收着」 */
  D.photoCorners = (ctx, s, st) => {
    const m = s.m == null ? 56 : s.m, L = s.len == null ? 46 : s.len;
    const a = s.a == null ? 0.22 : s.a;
    const P = [[m, m, 1, 1], [PV.W - m, m, -1, 1], [m, PV.H - m, 1, -1], [PV.W - m, PV.H - m, -1, -1]];
    ctx.save();
    ctx.fillStyle = st.S.ink;
    ctx.globalAlpha = a;
    for (const [x, y, sx, sy] of P) {
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x + L * sx, y);
      ctx.lineTo(x, y + L * sy);
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  };

  /* 微星群：撒在版面外围的小四角星（「小事物」的"小确幸"）。
        位置由 hash(i) 决定、闪烁由 st.t 相位决定 —— 双渲字节一致 */
  D.tinyStars = (ctx, s, st) => {
    const n = s.n || 10, a = s.a == null ? 0.30 : s.a;
    const R = s.r == null ? 5.5 : s.r;
    const rnd = PV.rng(0x9e3779b9, s.seed | 0, 13);
    ctx.save();
    ctx.fillStyle = st.S.accent;
    for (let i = 0; i < n; i++) {
      const x = (0.05 + 0.90 * rnd()) * PV.W;
      const y = (0.05 + 0.90 * rnd()) * PV.H;
      const ph = rnd() * TAU, sz = R * (0.55 + 0.75 * rnd());
      const tw = 0.55 + 0.45 * Math.sin((st.t || 0) * 1.7 + ph);
      // 四角星（两条正交的细菱形）
      ctx.globalAlpha = a * tw;
      ctx.beginPath();
      for (let k = 0; k < 4; k++) {
        const ang = k * Math.PI / 2 + Math.PI / 4;
        const ox = Math.cos(ang) * sz, oy = Math.sin(ang) * sz;
        if (k === 0) ctx.moveTo(x + ox, y + oy); else ctx.lineTo(x + ox, y + oy);
        const ang2 = ang + Math.PI / 4;
        ctx.lineTo(x + Math.cos(ang2) * sz * 0.30, y + Math.sin(ang2) * sz * 0.30);
      }
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  };

  /* 泪滴：下落的水滴。「美到想哭」用 —— 不是悲伤的雨，是"幸福到涌出来"。
        下落是闭式解（(y0 + t*spd) % 1），不累积状态 */
  D.teardrop = (ctx, s, st) => {
    const n = s.n || 5, a = s.a == null ? 0.26 : s.a;
    const spd = s.spd == null ? 0.035 : s.spd;
    const sz = s.size == null ? 12 : s.size;
    const rnd = PV.rng(0x51ed270b, s.seed | 0, 17);
    ctx.save();
    ctx.fillStyle = st.S.acc2;
    for (let i = 0; i < n; i++) {
      const x0 = 0.12 + 0.76 * rnd(), y0 = rnd(), ph = rnd() * TAU;
      const k = (y0 + (st.t || 0) * spd * (0.7 + 0.6 * rnd())) % 1;
      const x = x0 * PV.W + Math.sin((st.t || 0) * 0.8 + ph) * PV.W * 0.008;
      const y = k * PV.H * 1.1 - PV.H * 0.05;
      const r = sz * (0.6 + 0.7 * rnd());
      ctx.globalAlpha = a * Math.sin(Math.PI * k);    // 首尾淡出
      // 泪滴 = 圆 + 上尖
      ctx.beginPath();
      ctx.moveTo(x, y - r * 1.9);
      ctx.quadraticCurveTo(x + r * 0.95, y - r * 0.2, x, y + r * 0.55);
      ctx.quadraticCurveTo(x - r * 0.95, y - r * 0.2, x, y - r * 1.9);
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  };

  /* 圆角框：一圈柔和的圆角矩形描边。「安全之地 / 绘本页」用 ——
        直角框是"版面"，圆角框是"被捧着的一页" */
  D.softCorners = (ctx, s, st) => {
    const m = s.m == null ? 48 : s.m;
    const a = s.a == null ? 0.18 : s.a;
    const r = s.r == null ? 36 : s.r;
    ctx.save();
    ctx.strokeStyle = st.S.ink;
    ctx.globalAlpha = a;
    ctx.lineWidth = s.lw == null ? 2 : s.lw;
    const w = PV.W - m * 2, h = PV.H - m * 2;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(m, m, w, h, r);
    else {   // 兜底：老内核没有 roundRect
      ctx.moveTo(m + r, m);
      ctx.arcTo(m + w, m, m + w, m + h, r); ctx.arcTo(m + w, m + h, m, m + h, r);
      ctx.arcTo(m, m + h, m, m, r); ctx.arcTo(m, m, m + w, m, r);
      ctx.closePath();
    }
    ctx.stroke();
    ctx.restore();
  };

  /* ============================================================
     bg 扩充（P1-4）：背景图形层
     ============================================================ */
  const BG = PV.BG;

  /* 斜纹场：45° 细斜纹，给底一点「织物」密度 */
  BG.diagonal = (ctx, s, env) => {
    const a = s.a == null ? 0.06 : s.a, step = s.step == null ? 26 : s.step;
    const col = s.col === 'accent' ? rgba(env.S.accentRGB, 1) : env.S.ink;
    ctx.save();
    ctx.globalAlpha = a; ctx.strokeStyle = col; ctx.lineWidth = s.w || 1.2;
    const W = PV.W, H = PV.H;
    for (let i = -H; i < W + H; i += step) {
      ctx.beginPath(); ctx.moveTo(i, 0); ctx.lineTo(i + H, H); ctx.stroke();
    }
    ctx.restore();
  };

  /* 波带：2~3 条极缓的正弦横带，给底一点「流体」 */
  BG.waveBand = (ctx, s, env) => {
    const n = s.n || 3, a = s.a == null ? 0.07 : s.a;
    const col = s.col === 'accent' ? rgba(env.S.accentRGB, 1) : env.S.ink;
    ctx.save();
    ctx.globalAlpha = a; ctx.fillStyle = col;
    for (let i = 0; i < n; i++) {
      const yBase = PV.H * (0.22 + 0.26 * i), amp = PV.H * (0.035 + 0.012 * i);
      const ph = i * 1.7 + env.t * 0.18;
      ctx.beginPath();
      ctx.moveTo(0, PV.H);
      for (let x = 0; x <= PV.W; x += 24) {
        ctx.lineTo(x, yBase + Math.sin(x / PV.W * TAU * 1.3 + ph) * amp);
      }
      ctx.lineTo(PV.W, PV.H); ctx.closePath(); ctx.fill();
    }
    ctx.restore();
  };

  /* 噪声场：极细的随机点，给底一点「纸的颗粒」（静态、由 seed 决定） */
  BG.noiseField = (ctx, s, env) => {
    const a = s.a == null ? 0.05 : s.a, n = s.n || 240;
    const rnd = PV.rng(0x51f3a2, env.seed | 0, 7);
    ctx.save();
    ctx.globalAlpha = a; ctx.fillStyle = env.S.ink;
    for (let i = 0; i < n; i++) {
      const x = rnd() * PV.W, y = rnd() * PV.H, r = 0.6 + rnd() * 1.4;
      ctx.fillRect(x, y, r, r);
    }
    ctx.restore();
  };

  /* 聚光：一束从上往下的柔光，给底一点「舞台」 */
  BG.spotlight = (ctx, s, env) => {
    const x = (s.x == null ? 0.5 : s.x) * PV.W, a = s.a == null ? 0.10 : s.a;
    const r = (s.r == null ? 0.85 : s.r) * PV.H;
    const col = s.col === 'accent' ? env.S.accentRGB : [255, 255, 255];
    const g = ctx.createRadialGradient(x, -PV.H * 0.10, 10, x, PV.H * 0.30, r);
    g.addColorStop(0, rgba(col, a * 1.6));
    g.addColorStop(1, rgba(col, 0));
    ctx.save(); ctx.fillStyle = g; ctx.fillRect(0, 0, PV.W, PV.H); ctx.restore();
  };

  /* 等高线：几条闭合的不规则圈，像地图等高线（静态） */
  BG.contour = (ctx, s, env) => {
    const n = s.n || 4, a = s.a == null ? 0.08 : s.a;
    const rnd = PV.rng(0x77c1d3, env.seed | 0, 5);
    const cx = (s.x == null ? 0.62 : s.x) * PV.W, cy = (s.y == null ? 0.48 : s.y) * PV.H;
    ctx.save();
    ctx.globalAlpha = a; ctx.strokeStyle = env.S.ink; ctx.lineWidth = 1.1;
    for (let i = 0; i < n; i++) {
      const R = PV.H * (0.16 + 0.14 * i);
      ctx.beginPath();
      for (let k = 0; k <= 24; k++) {
        const ang = (k / 24) * TAU;
        const rr = R * (0.86 + 0.22 * rnd());
        const px = cx + Math.cos(ang) * rr, py = cy + Math.sin(ang) * rr * 0.82;
        k ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
      }
      ctx.closePath(); ctx.stroke();
    }
    ctx.restore();
  };

  /* 日芒：从一点发散的粗细交替射线，给爆发段一点「冲击」 */
  BG.sunburst = (ctx, s, env) => {
    const n = s.n || 18, a = s.a == null ? 0.055 : s.a;
    const cx = (s.x == null ? 0.5 : s.x) * PV.W, cy = (s.y == null ? 0.5 : s.y) * PV.H;
    const R = Math.hypot(PV.W, PV.H) * 0.7;
    ctx.save();
    ctx.globalAlpha = a; ctx.fillStyle = s.col === 'accent' ? rgba(env.S.accentRGB, 1) : env.S.ink;
    for (let i = 0; i < n; i++) {
      const a0 = (i / n) * TAU + env.t * 0.05, aw = (TAU / n) * 0.34;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(a0 - aw) * R, cy + Math.sin(a0 - aw) * R);
      ctx.lineTo(cx + Math.cos(a0 + aw) * R, cy + Math.sin(a0 + aw) * R);
      ctx.closePath(); ctx.fill();
    }
    ctx.restore();
  };

  /* 网点阵：菱形排布的点阵，比 dots 更「印刷」 */
  BG.mesh = (ctx, s, env) => {
    const step = s.step == null ? 34 : s.step, a = s.a == null ? 0.055 : s.a;
    ctx.save();
    ctx.globalAlpha = a; ctx.fillStyle = env.S.ink;
    let row = 0;
    for (let y = 0; y < PV.H; y += step * 0.86) {
      const off = (row++ % 2) * step * 0.5;
      for (let x = off; x < PV.W; x += step) {
        ctx.fillRect(x, y, 2, 2);
      }
    }
    ctx.restore();
  };

  /* 渐层洗：一条极淡的线性渐层，给底一点「光照方向」 */
  BG.gradWash = (ctx, s, env) => {
    const a = s.a == null ? 0.08 : s.a;
    const ang = ((s.ang == null ? 118 : s.ang) * Math.PI) / 180;
    const cx = PV.W / 2, cy = PV.H / 2, R = Math.hypot(PV.W, PV.H) * 0.62;
    const x0 = cx - Math.cos(ang) * R, y0 = cy - Math.sin(ang) * R;
    const x1 = cx + Math.cos(ang) * R, y1 = cy + Math.sin(ang) * R;
    const col = s.col === 'accent' ? env.S.accentRGB : [255, 255, 255];
    const g = ctx.createLinearGradient(x0, y0, x1, y1);
    g.addColorStop(0, rgba(col, a));
    g.addColorStop(1, rgba(col, 0));
    ctx.save(); ctx.fillStyle = g; ctx.fillRect(0, 0, PV.W, PV.H); ctx.restore();
  };

  /* ============================================================
     treat 扩充（P1-4）：字形上色
     签名与核心件一致：(ctx, g, bx, by, a, S)
     ============================================================ */
  const T = PV.TREAT;

  /* 浮雕：亮边在左上、暗边在右下，读作「压进纸里」 */
  T.emboss = (ctx, g, bx, by, a, S) => {
    const d = Math.max(1.5, g.size * 0.022);
    ctx.globalAlpha = a * 0.55; ctx.fillStyle = '#FFFFFF';
    ctx.fillText(g.c, bx - d, by - d);
    ctx.globalAlpha = a * 0.45; ctx.fillStyle = '#000000';
    ctx.fillText(g.c, bx + d, by + d);
    ctx.globalAlpha = a; ctx.fillStyle = S.ink;
    ctx.fillText(g.c, bx, by);
  };

  /* 内线：字中间掏一条细缝（描边后用底色再描一次细线） */
  T.inline = (ctx, g, bx, by, a, S) => {
    ctx.globalAlpha = a; ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
    ctx.globalAlpha = a * 0.9; ctx.strokeStyle = S.bg || '#000';
    ctx.lineWidth = Math.max(1.2, g.size * 0.014);
    ctx.lineJoin = 'round';
    ctx.strokeText(g.c, bx, by);
    ctx.globalAlpha = a; ctx.strokeStyle = S.ink;
    ctx.lineWidth = Math.max(2, g.size * 0.030);
    ctx.strokeText(g.c, bx, by);
    ctx.globalAlpha = a; ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
  };

  /* 挖空：只有描边，中间透出底（比 outline 更「轻」） */
  T.cutout = (ctx, g, bx, by, a, S) => {
    ctx.globalAlpha = a; ctx.strokeStyle = S.ink;
    ctx.lineWidth = Math.max(2.6, g.size * 0.038);
    ctx.lineJoin = 'round';
    ctx.strokeText(g.c, bx, by);
  };

  /* 渐层填：字内从 ink 到 accent 的线性渐层 */
  T.gradFill = (ctx, g, bx, by, a, S) => {
    const h = g.size || 60;
    const grad = ctx.createLinearGradient(bx, by - h, bx, by + h * 0.22);
    grad.addColorStop(0, S.ink);
    grad.addColorStop(1, rgba(S.accentRGB || [255, 255, 255], 1));
    ctx.globalAlpha = a; ctx.fillStyle = grad;
    ctx.fillText(g.c, bx, by);
  };

  /* 噪声填：字内叠一层极细的点，读作「丝网印」 */
  T.noiseFill = (ctx, g, bx, by, a, S) => {
    ctx.globalAlpha = a; ctx.fillStyle = S.ink;
    ctx.fillText(g.c, bx, by);
    const rnd = PV.rng(0x9a2b11, (g.c.charCodeAt(0) | 0), 13);
    ctx.globalAlpha = a * 0.22; ctx.fillStyle = S.bg || '#000';
    for (let i = 0; i < 26; i++) {
      const x = bx - g.size * 0.42 + rnd() * g.size * 0.84;
      const y = by - g.size * 0.82 + rnd() * g.size * 0.92;
      ctx.fillRect(x, y, 1.6, 1.6);
    }
    ctx.globalAlpha = a;
  };

  /* 海报：双层错位实心（主色 + accent 偏移一层），读作「丝网错版」 */
  T.poster = (ctx, g, bx, by, a, S) => {
    const d = Math.max(2, g.size * 0.045);
    ctx.globalAlpha = a * 0.72; ctx.fillStyle = rgba(S.accentRGB || [255, 255, 255], 1);
    ctx.fillText(g.c, bx + d, by + d * 0.6);
    ctx.globalAlpha = a; ctx.fillStyle = S.ink;
    ctx.fillText(g.c, bx, by);
  };

  /* ============================================================
     fx 扩充（P1-4）：整帧后期
     签名与核心件一致：(ctx, s, st)，st.fx.a 由引擎按包络给
     ============================================================ */
  const F = PV.FX;

  /* 漏光：一侧的柔光溢出，读作「镜头进光」 */
  F.lightLeak = (ctx, s, st) => {
    const a = st.fx && st.fx.a != null ? st.fx.a : 0;
    if (a <= 0.002) return;
    const x = (s.side === 'r' ? 1 : 0) * PV.W;
    const g = ctx.createRadialGradient(x, PV.H * 0.32, 10, x, PV.H * 0.32, PV.W * 0.72);
    g.addColorStop(0, rgba(st.S.accentRGB || [255, 200, 120], 0.30 * a));
    g.addColorStop(1, rgba(st.S.accentRGB || [255, 200, 120], 0));
    ctx.save(); ctx.fillStyle = g; ctx.fillRect(0, 0, PV.W, PV.H); ctx.restore();
  };

  /* 扫描线：极淡的横向细线，读作「CRT」 */
  F.scanline = (ctx, s, st) => {
    const a = (st.fx && st.fx.a != null ? st.fx.a : 0) * 0.5;
    if (a <= 0.002) return;
    const step = s.step == null ? 4 : s.step;
    ctx.save();
    ctx.globalAlpha = a * 0.35; ctx.fillStyle = '#000';
    for (let y = 0; y < PV.H; y += step) ctx.fillRect(0, y, PV.W, 1);
    ctx.restore();
  };

  /* 切片故障：几条横带水平错位（只在重拍上开） */
  F.glitchSlice = (ctx, s, st) => {
    const a = st.fx && st.fx.a != null ? st.fx.a : 0;
    if (a <= 0.002) return;
    const n = s.n || 4, seed = s.seed | 0;
    const rnd = PV.rng(seed, 31, 37);
    ctx.save();
    for (let i = 0; i < n; i++) {
      const y = rnd() * PV.H, h = 6 + rnd() * 22, dx = (rnd() - 0.5) * PV.W * 0.10 * a;
      ctx.drawImage(ctx.canvas, 0, y, PV.W, h, dx, y, PV.W, h);
    }
    ctx.restore();
  };

  /* 柔光晕：整体亮度轻微抬升 + 边缘柔光，读作「过曝」 */
  F.bloom = (ctx, s, st) => {
    const a = st.fx && st.fx.a != null ? st.fx.a : 0;
    if (a <= 0.002) return;
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    ctx.globalAlpha = a * 0.10;
    ctx.fillStyle = '#FFFFFF';
    ctx.fillRect(0, 0, PV.W, PV.H);
    ctx.restore();
  };

  /* 去饱和：用 saturation 混合把全屏饱和度往下压（「眼泪里的世界」）。
        混合模式的 source 灰度 = 0 饱和度，dest 就被拉向灰；alpha 控强度。
        4K 下就是一次 fillRect，比任何滤镜都便宜。 */
  F.desat = (ctx, s, st) => {
    const a = st.fx && st.fx.a != null ? st.fx.a : 0;
    if (a <= 0.002) return;
    ctx.save();
    ctx.globalCompositeOperation = 'saturation';
    ctx.globalAlpha = clamp(a, 0, 1);
    ctx.fillStyle = '#808080';
    ctx.fillRect(0, 0, PV.W, PV.H);
    ctx.restore();
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
  };

  /* ============================================================
     手绘语言（P1-5）：把「笔」还给画面
     ------------------------------------------------------------
     接触表复盘（2026-10-30）：49 镜里 47 镜只有「一行字 + 一条发丝下划线」，
     底色是 alpha≤0.20 的水彩 —— 这就是「大字贴在奶油纸上」的全部成因。
     手书 PV 与排印海报的差别不在字体，在于**画面里有没有笔**：
     圈住一个词、划一道红线、涂一块荧光、随手勾一根波浪。这些都是 stroke，
     4K 下一次几十段，成本可忽略（实测 draw 段从 0.5ms → 0.9ms）。

     grow：装饰不是「淡入」，是「一笔画过去」。st.phase 是本镜相对时刻，
     给了 grow 就只画到第 k 段 —— 这才是手书的手感来源。
     ============================================================ */

  /* 笔锋剖面：起笔 0.42 → 行笔 1.0（偏后）→ 收笔出锋 0.10。
     用 sin 的一次幂 + 端点线性收细，读起来像毛笔墨量在走。 */
  const _tip = (t) => {
    const body = Math.sin(Math.PI * Math.pow(t, 0.78));
    const head = t < 0.10 ? 0.42 + 5.8 * t : 1;      // 起笔略顿
    const tail = t > 0.86 ? (1 - t) / 0.14 : 1;      // 出锋
    return body * (head < 1 ? head : 1) * tail;
  };

  /* 颜色解引用：编排稿里 col 只会是 'accent' / 'ink' / 具体色值 / null。
     直接 `s.col || st.S.ink` 会把字符串 'accent' 原样塞给 strokeStyle，
     浏览器静默忽略 ⇒ 用的是上一次的 strokeStyle（实测整条笔触消失）。 */
  const _ink = (s, st, def) => {
    if (s.col === 'accent') return rgba(st.S.accentRGB || [200, 90, 60], 1);
    if (s.col === 'ink') return st.S.ink;
    return s.col || def || st.S.ink;
  };

  /* 二次贝塞尔取点：控制点由 bow 决定，bow 为正向右弯 */
  const _bez = (x0, y0, x1, y1, bow, t) => {
    const mx = (x0 + x1) / 2, my = (y0 + y1) / 2;
    const dx = x1 - x0, dy = y1 - y0, L = Math.hypot(dx, dy) || 1;
    const cx = mx - (dy / L) * bow, cy = my + (dx / L) * bow;
    const u = 1 - t;
    return [u * u * x0 + 2 * u * t * cx + t * t * x1,
            u * u * y0 + 2 * u * t * cy + t * t * y1];
  };

  /* 一条笔触。x0/y0→x1/y1，bow 弯曲量（px），w 最大线宽（px）。
     wob：手抖幅度（px），按整条线固定相位，不做逐帧随机（确定性要求）。
     分段画不同 lineWidth 来模拟笔锋 —— 单用 lineWidth 一条线是画不出粗细变化的，
     而分段之间只要 lineCap=round 且段段重叠，4K 下看不出台阶。 */
  D.brush = (ctx, s, st) => {
    const x0 = s.x0, y0 = s.y0, x1 = s.x1, y1 = s.y1;
    const bow = s.bow || 0, w = s.w == null ? 14 : s.w;
    const a = s.a == null ? 0.75 : s.a;
    const wob = s.wob == null ? 1.6 : s.wob;
    const grow = s.grow == null ? 0 : s.grow;
    const k = grow > 0 ? clamp((st.phase || 0) / grow, 0, 1) : 1;
    if (k <= 0) return;
    const L = Math.hypot(x1 - x0, y1 - y0);
    const sd = (s.seed | 0) || 7;
    const ph = (sd % 360) / 57.29578;
    /* 笔触必须用**填充轮廓**而不是分段 stroke。
       分段 stroke 靠每段不同的 lineWidth 模拟笔锋，段与段之间的台阶在 4K 上
       会读成一排短竖线（实测第一版画出来是「拉链」/「毛毛虫」，整条线报废）。
       正确做法：沿二次贝塞尔采 n 点，按法线方向各偏移半宽，正向走一遍左边界、
       反向走一遍右边界，闭合成一个多边形一次 fill —— 粗细连续、边缘光滑，
       而且只有一次 fill，比 n 次 stroke 还便宜。 */
    const n = clamp(Math.round(L / 12), 12, 96);
    const end = Math.max(2, Math.round(k * n));
    // 左边界 / 右边界交替存放，最后逆序另一半
    const pts = [];
    for (let i = 0; i <= end; i++) {
      const t = i / n;
      const p = _bez(x0, y0, x1, y1, bow, t);
      const q = _bez(x0, y0, x1, y1, bow, Math.min(1, t + 0.004));
      const dx = q[0] - p[0], dy = q[1] - p[1];
      const d = Math.hypot(dx, dy) || 1;
      const nx = -dy / d, ny = dx / d;
      const hw = w * _tip(t) * 0.5;
      const wobY = Math.sin(ph + t * 5.1) * wob;      // 手抖：整条线一起摆
      pts.push([p[0] + nx * hw, p[1] + ny * hw + wobY]);
    }
    const back = [];
    for (let i = end; i >= 0; i--) {
      const t = i / n;
      const p = _bez(x0, y0, x1, y1, bow, t);
      const q = _bez(x0, y0, x1, y1, bow, Math.min(1, t + 0.004));
      const dx = q[0] - p[0], dy = q[1] - p[1];
      const d = Math.hypot(dx, dy) || 1;
      const nx = -dy / d, ny = dx / d;
      const hw = w * _tip(t) * 0.5;
      const wobY = Math.sin(ph + t * 5.1) * wob;
      back.push([p[0] - nx * hw, p[1] - ny * hw + wobY]);
    }
    ctx.save();
    ctx.globalAlpha = a;
    ctx.fillStyle = _ink(s, st);
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    for (let i = 0; i < back.length; i++) ctx.lineTo(back[i][0], back[i][1]);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  };

  /* 手画圈：半径带噪声、首尾留缺口、末端出锋。用来「把某个词圈起来」——
     手书 PV 里出现频率极高，而且比任何色块都更像人画的。 */
  D.circleLoose = (ctx, s, st) => {
    const cx = s.x == null ? PV.W * 0.5 : s.x;
    const cy = s.y == null ? PV.H * 0.5 : s.y;
    const rx = s.rx == null ? PV.W * 0.22 : s.rx;
    const ry = s.ry == null ? (s.rx == null ? PV.W * 0.22 : s.rx) * 0.64 : s.ry;
    const a = s.a == null ? 0.65 : s.a;
    const w = s.w == null ? 10 : s.w;
    const grow = s.grow == null ? 0 : s.grow;
    const k = grow > 0 ? clamp((st.phase || 0) / grow, 0, 1) : 1;
    if (k <= 0) return;
    const sd = (s.seed | 0) || 13;
    const rot = ((sd % 100) / 100 - 0.5) * 0.5;
    // 起笔位置随机，缺口约 18°；转角 > 1 圈，读作「绕回来还多画了一点」
    const a0 = rot + ((sd % 57) / 57) * 6.283;
    const sweep = 6.283 - 0.31 + (sd % 7) * 0.02;
    const n = 72;
    const end = Math.max(2, Math.round(k * n));
    const nz = (q) => 1 + Math.sin(sd + q * 9.3) * 0.035 + Math.sin(sd * 1.7 + q * 21) * 0.012;
    // 同 brush：填充轮廓，不用分段 stroke（否则整圈是一排小台阶）
    const outer = [], inner = [];
    for (let i = 0; i <= end; i++) {
      const q = i / n;
      const th = a0 + sweep * q;
      const ux = Math.cos(th), uy = Math.sin(th);
      const rad = nz(q), hw = w * _tip(q * 0.94 + 0.03) * 0.5;
      const px = cx + ux * rx * rad, py = cy + uy * ry * rad;
      outer.push([px + ux * hw, py + uy * hw]);
      inner.push([px - ux * hw, py - uy * hw]);
    }
    ctx.save();
    ctx.globalAlpha = a;
    ctx.fillStyle = _ink(s, st);
    ctx.beginPath();
    ctx.moveTo(outer[0][0], outer[0][1]);
    for (let i = 1; i < outer.length; i++) ctx.lineTo(outer[i][0], outer[i][1]);
    for (let i = inner.length - 1; i >= 0; i--) ctx.lineTo(inner[i][0], inner[i][1]);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  };

  /* 荧光涂痕：比色块窄、比线宽，两端毛糙 —— 读作「用马克笔划了一下」。
     用三段错位矩形拼，不做模糊（4K 上 blur 是最贵的东西）。 */
  D.marker = (ctx, s, st) => {
    const x = s.x, y = s.y, w = s.w, h = s.h == null ? 34 : s.h;
    const a = s.a == null ? 0.30 : s.a;
    const grow = s.grow == null ? 0 : s.grow;
    const k = grow > 0 ? clamp((st.phase || 0) / grow, 0, 1) : 1;
    if (k <= 0) return;
    const sd = (s.seed | 0) || 5;
    ctx.save();
    ctx.globalAlpha = a;
    ctx.fillStyle = _ink(s, st, rgba(st.S.accentRGB || [200, 90, 60], 1));
    const ww = w * k;
    ctx.fillRect(x, y + h * 0.10, ww, h * 0.62);
    ctx.fillRect(x + ww * 0.02, y, ww * 0.94, h * 0.30);
    ctx.fillRect(x, y + h * 0.66, ww * 0.97, h * 0.34);
    // 两端毛边：几根短竖条，长度按 seed 变
    for (let i = 0; i < 5; i++) {
      const dy = ((Math.sin(sd + i * 2.3) * 0.5 + 0.5) * h) * 0.9;
      ctx.fillRect(x + ww - 2 - i * 1.5, y + dy, 2, h * 0.10);
    }
    ctx.restore();
  };

  /* 随手勾的波浪：一根贯穿画面的长曲线，用来给空白区「压」出层次。
     这是背景里最便宜的一层「画」—— 一条 stroke，不吃填充率。 */
  D.scrawl = (ctx, s, st) => {
    const y0 = s.y == null ? PV.H * 0.5 : s.y;
    const amp = s.amp == null ? PV.H * 0.05 : s.amp;
    const ph = s.ph == null ? 0 : s.ph;
    const a = s.a == null ? 0.22 : s.a;
    const w = s.w == null ? 3 : s.w;
    const grow = s.grow == null ? 0 : s.grow;
    const k = grow > 0 ? clamp((st.phase || 0) / grow, 0, 1) : 1;
    if (k <= 0) return;
    const n = 160, end = Math.max(2, Math.round(n * k));
    ctx.save();
    ctx.globalAlpha = a;
    ctx.strokeStyle = _ink(s, st);
    ctx.lineWidth = w; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.beginPath();
    for (let i = 0; i <= end; i++) {
      const u = i / n;
      const x = -PV.W * 0.02 + PV.W * 1.04 * u;
      const y = y0
        + Math.sin(u * 6.1 + ph) * amp
        + Math.sin(u * 17.3 + ph * 2.1) * amp * 0.22;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.restore();
  };
})();
