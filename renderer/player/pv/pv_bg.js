/*! pv_bg.js —— 背景层次。
 *
 *  为什么需要这一层
 *  ----------------
 *  初版的背景只有「纸底渐变 + 一团柔光 + 暗角」，在大面积深色下读作死黑：
 *  白字压在死黑上会显得**廉价**（没有中间调托着，字像贴纸）。日本 PV 的做法是
 *  在纸面之下铺 2~3 层**低对比的印刷纹理**：网点、细网格、排线、尘埃、光漏。
 *  它们几乎看不见，但把「空」变成了「纸」。
 *
 *  三条纪律
 *  · 纹理**绝不逐帧现算**：一律烘焙成 sprite 后拉伸贴图。大面积渐变/噪点每帧
 *    重建会让 Chromium 的 dither 相位漂移，同一帧两次渲染差 ±1~2（RULES 51）。
 *  · 每一层都必须**低到几乎看不见**：背景的任务是让文字站得住，不是抢戏。
 *    单层 alpha 上限 ~0.10，叠起来才有「纸」的厚度。
 *  · 不用高斯模糊 / shadowBlur 做质感 —— 4K 下那是纯烧时间，而且糊得没有印刷味。
 */
(() => {
  'use strict';
  const PV = window.PV;
  const { rgba, mix, clamp } = PV;

  /* ---------------- 程序化纹理场 ----------------
     烘焙一张 N×N 的「中心实、边缘虚」纹理，绘制时按目标矩形拉伸。
     这样同一张纹理可以用在任意位置/尺寸，而边缘永远不会露出平铺接缝。 */
  const _field = {};
  const N = 1024;

  /* mask：
     radial —— 方块用（中心实、四周虚，贴上去像一块「印上去的网」）
     v      —— 满幅用（上下虚化）。满幅还用 radial 的话，四角会完全透明，
               于是画面四角依然是死黑 —— 这正是「背景没层次」的最后一块拼图。 */
  function fieldCanvas(kind, col, extra, mask) {
    const key = kind + '|' + col + '|' + (extra || '') + '|' + (mask || 'radial');
    if (_field[key]) return _field[key];
    const c = document.createElement('canvas');
    c.width = c.height = N;
    const g = c.getContext('2d');
    const rgb = PV.hex(col);
    const rnd = PV.rng(0x51ed270b, PV.hash(kind), Math.round(rgb[0] * 7 + rgb[1]));

    if (kind === 'halftone') {
      // 网点：行列错开 45°，点径随「到中心的距离」衰减 ⇒ 天然的渐变块
      const step = extra ? +extra : 13;
      for (let y = 0; y < N + step; y += step) {
        for (let x = 0; x < N + step; x += step) {
          const ox = (Math.round(y / step) % 2) * step * 0.5;
          const px = x + ox, py = y;
          const d = Math.hypot(px - N / 2, py - N / 2) / (N / 2);
          const k = clamp(1 - d * d * 1.05, 0, 1);         // 平方衰减：中心区更密实
          if (k <= 0.02) continue;
          g.globalAlpha = 0.55 + 0.45 * k;
          g.fillStyle = rgba(rgb, 1);
          g.beginPath();
          g.arc(px, py, step * 0.30 * (0.35 + 0.65 * k), 0, 6.283185307);
          g.fill();
        }
      }
    } else if (kind === 'grid') {
      const step = extra ? +extra : 64;
      g.strokeStyle = rgba(rgb, 1);
      g.lineWidth = 1.1;
      for (let i = 0; i <= N; i += step) {
        g.globalAlpha = 1; g.beginPath(); g.moveTo(i + 0.5, 0); g.lineTo(i + 0.5, N); g.stroke();
        g.beginPath(); g.moveTo(0, i + 0.5); g.lineTo(N, i + 0.5); g.stroke();
      }
    } else if (kind === 'hatch') {
      // 排线：手绘的斜向平行线，间距有轻微抖动，避免读作「网格」
      g.strokeStyle = rgba(rgb, 1);
      let x = -N;
      while (x < N * 2) {
        g.globalAlpha = 0.55 + rnd() * 0.45;
        g.lineWidth = 1 + rnd() * 1.2;
        g.beginPath(); g.moveTo(x, 0); g.lineTo(x + N, N); g.stroke();
        x += 9 + rnd() * 7;
      }
    } else if (kind === 'dust') {
      // 尘埃：极稀疏的小点，给「旧印刷品」的时间感
      for (let i = 0; i < 1400; i++) {
        const px = rnd() * N, py = rnd() * N;
        const d = Math.hypot(px - N / 2, py - N / 2) / (N / 2);
        const k = clamp(1 - d, 0, 1);
        if (rnd() > k) continue;
        g.globalAlpha = 0.15 + rnd() * 0.55;
        g.fillStyle = rgba(rgb, 1);
        const r = 0.6 + rnd() * 1.5;
        g.beginPath(); g.arc(px, py, r, 0, 6.283185307); g.fill();
      }
    } else if (kind === 'topo') {
      /* 等高线：参考 JIZURA 的 topoLines。闭合环 + 多频正弦扰动 = 「地形」；
         环数固定时相位必须一次算好，否则同一条环画出来的两半接不上（会露裂缝）。 */
      const rings = extra ? +extra : 13;
      g.strokeStyle = rgba(rgb, 1);
      g.lineWidth = 1.5;
      for (let k = 1; k <= rings; k++) {
        const r0 = (N * 0.5) * (k / rings);
        const p1 = k * 0.83, p2 = k * 1.37, p3 = k * 2.11;
        g.globalAlpha = 0.30 + 0.30 * rnd();
        g.beginPath();
        const seg = 112;
        for (let i = 0; i <= seg; i++) {
          const a = i / seg * 6.283185307;
          const wob = 1
            + 0.105 * Math.sin(a * 3 + p1)
            + 0.058 * Math.sin(a * 5 - p2)
            + 0.026 * Math.sin(a * 9 + p3);
          const px = N / 2 + Math.cos(a) * r0 * wob;
          const py = N / 2 + Math.sin(a) * r0 * wob;
          if (i) g.lineTo(px, py); else g.moveTo(px, py);
        }
        g.closePath(); g.stroke();
      }
    } else if (kind === 'rays') {
      /* 光芒：从中心发散的长条扇形（JIZURA 的 godRays / 光芒）。
         只做「几束很宽的光」，不做密密麻麻的放射线 —— 后者会读作 starburst 图标。 */
      const n = extra ? +extra : 14;
      for (let i = 0; i < n; i++) {
        const a = (i / n) * 6.283185307 + rnd() * 0.16;
        const w = 0.030 + rnd() * 0.055;
        g.globalAlpha = 0.07 + rnd() * 0.16;
        g.fillStyle = rgba(rgb, 1);
        g.beginPath();
        g.moveTo(N / 2, N / 2);
        g.arc(N / 2, N / 2, N * 0.80, a - w, a + w);
        g.closePath(); g.fill();
      }
    }

    /* 边缘羽化：用 destination-in 抹一圈径向遮罩。
       ⚠ 必须先离屏做，不能在主画布上用纸色填遮罩 —— 纸底是渐变，填出来会留一圈硬边。 */
    const m = document.createElement('canvas');
    m.width = m.height = N;
    const mg = m.getContext('2d');
    let rg;
    if (mask === 'v') {
      rg = mg.createLinearGradient(0, 0, 0, N);
      rg.addColorStop(0, 'rgba(0,0,0,0)');
      rg.addColorStop(0.18, 'rgba(0,0,0,1)');
      rg.addColorStop(0.82, 'rgba(0,0,0,1)');
      rg.addColorStop(1, 'rgba(0,0,0,0)');
    } else {
      rg = mg.createRadialGradient(N / 2, N / 2, 0, N / 2, N / 2, N / 2 * 1.35);
      rg.addColorStop(0, 'rgba(0,0,0,1)');
      rg.addColorStop(0.70, 'rgba(0,0,0,1)');
      rg.addColorStop(1, 'rgba(0,0,0,0)');
    }
    mg.fillStyle = rg; mg.fillRect(0, 0, N, N);
    g.globalAlpha = 1;
    g.globalCompositeOperation = 'destination-in';
    g.drawImage(m, 0, 0);
    g.globalCompositeOperation = 'source-over';
    return (_field[key] = c);
  }

  /* ---------------- 层 ----------------
     签名 (ctx, s, env)：s 是编排稿里的参数，env = { S, t, rel, scale }
     rel = 相对镜头起点的秒数（用来做「逐条进场」），scale = 输出倍率。 */
  const BG = (PV.BG = {});

  /* ① 环境色团：2~4 团极缓漂移的柔光，把死黑变成有色的暗。
        这是「层次」的第一来源 —— 纯黑之所以廉价，是因为没有任何中间调。 */
  BG.wash = (ctx, s, env) => {
    const S = env.S;
    const n = s.n || 3;
    const rnd = PV.rng(0x2c1b3a7d, env.seed | 0, 3);
    const col = s.col === 'accent' ? S.accentRGB : s.col === 'ghost' ? S.ghostARGB : S.inkRGB;
    const base = (s.a == null ? 0.105 : s.a);
    for (let i = 0; i < n; i++) {
      const ph = rnd() * 6.283;
      const sp = 0.012 + rnd() * 0.02;
      const cx = PV.W * (0.14 + 0.72 * rnd()) + Math.sin(env.t * sp * 6.283 + ph) * PV.W * 0.05;
      const cy = PV.H * (0.10 + 0.80 * rnd()) + Math.cos(env.t * sp * 5.1 + ph) * PV.H * 0.06;
      const r = PV.H * (0.42 + 0.40 * rnd());
      PV.blob(ctx, cx, cy, r, col, base * (0.55 + 0.75 * rnd()));
    }
  };

  /* ② 网点块：印刷的「半调」。位置由编排稿指定，尺寸按画面比例给。 */
  BG.halftone = (ctx, s, env) => {
    const full = (s.w == null || s.w >= 0.98) && (s.h == null || s.h >= 0.98);
    const img = fieldCanvas('halftone', s.col === 'accent' ? '#E2604A' : '#F4EEE1', s.step,
      s.mask || (full ? 'v' : 'radial'));
    const x = s.x == null ? 0 : (s.x <= 1.5 ? s.x * PV.W : s.x);
    const y = s.y == null ? 0 : (s.y <= 1.5 ? s.y * PV.H : s.y);
    const w = s.w == null ? PV.W : (s.w <= 1.5 ? s.w * PV.W : s.w);
    const h = s.h == null ? PV.H : (s.h <= 1.5 ? s.h * PV.H : s.h);
    ctx.save();
    ctx.globalAlpha = (s.a == null ? 0.20 : s.a);
    if (s.blend) ctx.globalCompositeOperation = s.blend;
    ctx.drawImage(img, x, y, w, h);
    ctx.restore();
  };

  /* ③ 细网格：版面的骨架。只在需要「图纸感」的段落出现。 */
  BG.grid = (ctx, s, env) => {
    const img = fieldCanvas('grid', '#F4EEE1', s.step, s.mask || 'v');
    const x = s.x == null ? 0 : (s.x <= 1.5 ? s.x * PV.W : s.x);
    const y = s.y == null ? 0 : (s.y <= 1.5 ? s.y * PV.H : s.y);
    const w = s.w == null ? PV.W : (s.w <= 1.5 ? s.w * PV.W : s.w);
    const h = s.h == null ? PV.H : (s.h <= 1.5 ? s.h * PV.H : s.h);
    ctx.save();
    ctx.globalAlpha = (s.a == null ? 0.11 : s.a);
    ctx.drawImage(img, x, y, w, h);
    ctx.restore();
  };

  /* ④ 排线：手绘斜线。比网格软，适合抒情段。 */
  BG.hatch = (ctx, s, env) => {
    const img = fieldCanvas('hatch', '#F4EEE1', '', s.mask || 'v');
    const x = s.x == null ? 0 : (s.x <= 1.5 ? s.x * PV.W : s.x);
    const y = s.y == null ? 0 : (s.y <= 1.5 ? s.y * PV.H : s.y);
    const w = s.w == null ? PV.W : (s.w <= 1.5 ? s.w * PV.W : s.w);
    const h = s.h == null ? PV.H : (s.h <= 1.5 ? s.h * PV.H : s.h);
    ctx.save();
    ctx.globalAlpha = (s.a == null ? 0.14 : s.a);
    ctx.drawImage(img, x, y, w, h);
    ctx.restore();
  };

  /* ⑤ 墨迹：几团压暗的柔光 + 一条擦痕，读作「纸被墨浸过」。
        暗底上用「比纸更暗」的色，而不是纯黑 —— 纯黑会像抠图。 */
  BG.ink = (ctx, s, env) => {
    const S = env.S;
    const rnd = PV.rng(0x7f4a1d09, env.seed | 0, 5);
    const col = mix(S.voidRGB, S.ghostARGB, 0.18);
    const n = s.n || 4;
    for (let i = 0; i < n; i++) {
      PV.blob(ctx, PV.W * rnd(), PV.H * rnd(), PV.H * (0.16 + 0.30 * rnd()),
        col, (s.a == null ? 0.16 : s.a) * (0.5 + rnd() * 0.7));
    }
  };

  /* ⑤b 巨型底字：把某个词放大到只剩几十个笔画，铺满整幅。
        这是「让纯黑有东西」最有效的一招 —— 加的不是纹理，是**另一个声部**。
        面积足够大时 alpha 0.05 就足以读出来；再高就会和歌词抢注意力。
        vert = 縦組み：日文 PV 的招牌排版，竖排的巨字沿画面一侧升起。 */
  BG.mega = (ctx, s, env) => {
    const txt = s.txt || '';
    if (!txt) return;
    let size = (s.size == null ? 0.55 : s.size) * PV.H;
    const face = s.face || 'heavy';
    /* 自动收进画面：巨大的字被画框切掉是日系 PV 的常用语汇，但只切「一半」时
       会读成乱码（实测 INNOCENCE 只露 INNO，配极窄的 Bebas 看着像 NOG）。
       规则：要么完整读得出，要么切掉得明显 —— 这里先保证读得出。 */
    const w0 = PV.measure(ctx, txt, face, size);
    if (w0 > PV.W * 1.02) size = size * (PV.W * 1.02) / w0;
    const col = s.col === 'accent' ? env.S.accent : env.S.ink;
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, PV.W, PV.H); ctx.clip();
    ctx.font = PV.cssFace(face, size);
    ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    ctx.globalAlpha = (s.a == null ? 0.055 : s.a);
    ctx.fillStyle = col;
    const x = (s.x == null ? 0.5 : s.x) * PV.W;
    const y = (s.y == null ? 0.5 : s.y) * PV.H;
    if (s.vert) {
      // 旋转 -90°：字头朝左，读起来是一条自下而上的竖列
      ctx.translate(x, y); ctx.rotate(-Math.PI / 2);
      ctx.fillText(txt, 0, 0);
    } else {
      const tw = PV.measure(ctx, txt, face, size);
      const ax = s.align == null ? 0.5 : s.align;
      ctx.fillText(txt, Math.max(-tw * 0.02, Math.min(PV.W - tw * 0.98, x - tw * ax)), y);
    }
    ctx.restore();
  };

  /* ⑤c 版式线框：沿安全区画细线 + 短刻度，像画册的裁切标记。
        它给画面一个「版面」，让留白读作「留白」而不是「没画完」。 */
  BG.edge = (ctx, s, env) => {
    const S = env.S;
    const m = (s.m == null ? 0.052 : s.m);
    const x0 = m * PV.W, x1 = PV.W - x0, y0 = m * PV.H, y1 = PV.H - y0;
    ctx.save();
    ctx.strokeStyle = S.ink; ctx.globalAlpha = (s.a == null ? 0.16 : s.a);
    ctx.lineWidth = s.w || 1.4;
    const seg = 0.26;                       // 四边各只画中段，画满会变成相框
    ctx.beginPath();
    ctx.moveTo(x0, y0); ctx.lineTo(x0 + (x1 - x0) * seg, y0);
    ctx.moveTo(x1, y0); ctx.lineTo(x1, y0 + (y1 - y0) * seg);
    ctx.moveTo(x1, y1); ctx.lineTo(x1 - (x1 - x0) * seg, y1);
    ctx.moveTo(x0, y1); ctx.lineTo(x0, y1 - (y1 - y0) * seg);
    ctx.stroke();
    // 四角的短刻度
    ctx.globalAlpha = (s.a == null ? 0.16 : s.a) * 1.6;
    const t = 14;
    ctx.beginPath();
    ctx.moveTo(x0, y0 + t); ctx.lineTo(x0, y0); ctx.lineTo(x0 + t, y0);
    ctx.moveTo(x1 - t, y1); ctx.lineTo(x1, y1); ctx.lineTo(x1, y1 - t);
    ctx.stroke();
    ctx.restore();
  };

  /* ⑥ 斜向色块：把画面切开的「面板」，给强拍用。
        半透明是刻意的 —— 不透明色块会变成几何海报，那就不是文字 PV 了。 */
  BG.panel = (ctx, s, env) => {
    const S = env.S;
    const col = s.col === 'accent' ? S.accentRGB : S.inkRGB;
    const ang = (s.rot == null ? -14 : s.rot) * Math.PI / 180;
    const x = s.x == null ? 0.5 : s.x, y = s.y == null ? 0.2 : s.y;
    const w = s.w == null ? 0.44 : s.w, h = s.h == null ? 0.24 : s.h;
    ctx.save();
    ctx.globalAlpha = (s.a == null ? 0.10 : s.a);
    ctx.fillStyle = rgba(col, 1);
    ctx.translate(x * PV.W, y * PV.H);
    ctx.rotate(ang);
    ctx.fillRect(-w * PV.W / 2, -h * PV.H / 2, w * PV.W, h * PV.H);
    ctx.restore();
  };

  /* ⑦ 尘埃：旧印刷品的时间感，全片常驻、几乎看不见。 */
  BG.dust = (ctx, s, env) => {
    const img = fieldCanvas('dust', '#F4EEE1', '', s.mask || 'v');
    ctx.save();
    ctx.globalAlpha = (s.a == null ? 0.20 : s.a);
    ctx.drawImage(img, 0, 0, PV.W, PV.H);
    ctx.restore();
  };

  /* ⑧ 光漏：从画面某一侧漏进来的暖光。电影感最便宜的一招，但必须极淡。 */
  BG.leak = (ctx, s, env) => {
    const S = env.S;
    const side = s.side || 'r';
    const cx = side === 'r' ? PV.W * 1.02 : side === 'l' ? -PV.W * 0.02 : PV.W * 0.5;
    const cy = s.y == null ? 0.10 : s.y;
    const col = mix(S.accentRGB, S.ghostARGB, 0.30);
    PV.blob(ctx, cx, cy * PV.H, PV.H * (s.r == null ? 0.95 : s.r), col,
      (s.a == null ? 0.085 : s.a));
  };

  /* ⑨ 声环：以一点为中心扩散的细圆环，节奏越强越密。
        不用频谱条 —— 那是播放器语汇；圆环是「声波在纸上荡开」。 */
  BG.rings = (ctx, s, env) => {
    const S = env.S;
    const cx = (s.x == null ? 0.5 : s.x) * PV.W;
    const cy = (s.y == null ? 0.5 : s.y) * PV.H;
    const n = s.n || 5;
    const spd = s.spd == null ? 0.55 : s.spd;
    const a0 = (s.a == null ? 0.10 : s.a);
    ctx.save();
    ctx.strokeStyle = s.col === 'accent' ? S.accent : S.ink;
    ctx.lineWidth = s.w || 1.2;
    for (let i = 0; i < n; i++) {
      const ph = ((env.t * spd + i / n) % 1);
      const r = PV.H * (0.10 + ph * (s.rMax || 0.85));
      ctx.globalAlpha = a0 * (1 - ph) * (1 - ph);
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, 6.283185307); ctx.stroke();
    }
    ctx.restore();
  };

  /* ⑫ 等高线：一张烘焙好的「地形」环纹，极慢自转。
        用途是给长镜头一个「地理感」的底 —— 它比网点更软，比排线更「有机」，
        抒情段用它可以不加亮任何东西就有了纵深。 */
  BG.topo = (ctx, s, env) => {
    const img = fieldCanvas('topo', s.col === 'accent' ? '#E2604A' : '#F4EEE1',
      s.rings == null ? '' : String(s.rings), s.mask || 'radial');
    const cx = (s.x == null ? 0.5 : s.x) * PV.W;
    const cy = (s.y == null ? 0.5 : s.y) * PV.H;
    const r = (s.r == null ? 1.20 : s.r) * Math.max(PV.W, PV.H) * 0.5;
    ctx.save();
    ctx.globalAlpha = (s.a == null ? 0.15 : s.a);
    ctx.translate(cx, cy);
    ctx.rotate((s.rot || 0) * Math.PI / 180 + env.t * (s.spin == null ? 0.010 : s.spin));
    ctx.drawImage(img, -r, -r, r * 2, r * 2);
    ctx.restore();
  };

  /* ⑬ 光芒：几束极宽的光从画面外斜切进来，随镜头极缓摆动。
        副歌/抬头看的句子用它，比「加亮背景」有效得多 —— 亮的是「光」，不是「底」。 */
  BG.rays = (ctx, s, env) => {
    const img = fieldCanvas('rays', s.col === 'accent' ? '#E2604A' : '#F4EEE1',
      s.n == null ? '' : String(s.n), s.mask || 'radial');
    const cx = (s.x == null ? 0.5 : s.x) * PV.W;
    const cy = (s.y == null ? 0.34 : s.y) * PV.H;
    const r = (s.r == null ? 1.35 : s.r) * Math.max(PV.W, PV.H) * 0.5;
    ctx.save();
    ctx.globalAlpha = (s.a == null ? 0.16 : s.a);
    if (s.blend) ctx.globalCompositeOperation = s.blend;
    ctx.translate(cx, cy);
    ctx.rotate((s.rot || 0) * Math.PI / 180 + Math.sin(env.t * 0.21 + (s.ph || 0)) * 0.035);
    ctx.drawImage(img, -r, -r, r * 2, r * 2);
    ctx.restore();
  };

  /* ============ 晨光绘本（Innocence v2）暖光六件 ============
     与上面的「印刷纹理」相反：这六件的主角是**光**，不是质感。
     钢琴抒情曲的情绪起伏全靠光表达（破晓→辉→梦→暮），所以它们的 α 比
     印刷纹理高一档（0.10~0.22），但仍守住「不抢戏」——光要能看见，不能盖过字。
     纪律照旧：渐变/光斑逐帧画没问题（便宜且相位稳定），
     但**噪点类纹理一律烘焙**；不用 shadowBlur。 */

  /* 取色器：s.col 支持 glow(默认)/accent/ghost/ink/acc2 或 #hex */
  const pickCol = (s, S, def) => {
    const k = s.col || def || 'glow';
    if (k === 'accent') return S.accentRGB;
    if (k === 'ghost') return S.ghostARGB;
    if (k === 'ghostB') return S.ghostBRGB;
    if (k === 'acc2') return S.acc2RGB;
    if (k === 'ink') return S.inkRGB;
    if (k === 'glow') return S.glowRGB || S.accentRGB;
    if (typeof k === 'string' && k[0] === '#') return PV.hex(k);
    return S.glowRGB || S.accentRGB;
  };

  /* 线性渐变烘焙：⚠ 逐帧 createLinearGradient 大面积铺开会让 Chromium 的
     抖动相位随调用次数漂移 ⇒ 同 t 连渲两次字节不一致（RULES 51，实测 t=127 挂）。
     纪律与 PV.softSprite 一致：渐变只烘焙一次成小竖条，绘制时拉伸。 */
  const _lin = {};
  function linSprite(col, stops) {
    const key = col.join(',') + '|' + stops.map(s => s[0] + ':' + s[1]).join(';');
    if (_lin[key]) return _lin[key];
    const c = document.createElement('canvas');
    c.width = 8; c.height = 512;
    const g = c.getContext('2d');
    const lg = g.createLinearGradient(0, 0, 0, 512);
    for (const [p, a] of stops) lg.addColorStop(p, rgba(col, a));
    g.fillStyle = lg;
    g.fillRect(0, 0, 8, 512);
    return (_lin[key] = c);
  }

  /* ⑭ 日出：地平线暖渐变 + 低角日晕。片头/醒来用。
        为什么是渐变不是一团光：「醒来」的语义是天亮了，整块底必须**自下而上**
        变暖；只加一个太阳团会读作「贴了张贴纸」。 */
  BG.sunrise = (ctx, s, env) => {
    const S = env.S;
    const col = pickCol(s, S, 'glow');
    const hy = (s.y == null ? 0.74 : s.y);            // 地平线高度（比例）
    const base = (s.a == null ? 0.16 : s.a);
    const breathe = 1 + 0.10 * Math.sin(env.t * 0.13 + (s.ph || 0));
    const W = PV.W, H = PV.H;
    const y0 = H * (hy - 0.55), hh = H * 0.83;
    const sp = linSprite(col, [[0, 0], [0.62, 0.55], [1, 1]]);
    ctx.save();
    ctx.globalAlpha = Math.min(1, base * breathe);
    ctx.drawImage(sp, 0, y0, W, hh);
    ctx.restore();
    // 低角日晕：贴着地平线的一团，半径大、极淡
    PV.blob(ctx, (s.x == null ? 0.5 : s.x) * W, H * hy, H * (s.r == null ? 0.52 : s.r),
      col, base * 0.72 * breathe);
  };

  /* ⑮ 天光：斜向光束 + 浮尘。「初见/慢下来」用。
        束是**烘焙渐变条的斜矩形**（不是模糊条、不是逐帧渐变）：
        4K 下模糊是纯烧时间，逐帧渐变会破确定性。浮尘用确定性散列位置 + t 漂移。 */
  BG.windowLight = (ctx, s, env) => {
    const S = env.S;
    const col = pickCol(s, S, 'glow');
    const W = PV.W, H = PV.H;
    const n = s.n || 3;
    const ang = ((s.ang == null ? -28 : s.ang) * Math.PI) / 180;
    const base = (s.a == null ? 0.13 : s.a);
    const rnd = PV.rng(0x77ae12d3, env.seed | 0, 11);
    const sp = linSprite(col, [[0, 1], [0.55, 0.55], [1, 0]]);
    ctx.save();
    ctx.translate(W * (s.x == null ? 0.18 : s.x), H * (s.y == null ? -0.12 : s.y));
    ctx.rotate(ang + Math.sin(env.t * 0.06) * 0.012);   // 极缓摆动
    for (let i = 0; i < n; i++) {
      const bw = W * (0.045 + 0.055 * rnd());
      const off = (i - (n - 1) / 2) * W * 0.17 + (rnd() - 0.5) * W * 0.05;
      ctx.globalAlpha = base * (0.75 + 0.5 * rnd());
      ctx.drawImage(sp, off - bw / 2, -H * 0.1, bw, H * 1.7);
    }
    ctx.restore();
    // 浮尘：8~16 粒，缓慢上浮 + 左右摇
    const m = s.motes == null ? 12 : s.motes;
    const r2 = PV.rng(0x51ed270b, (env.seed | 0) + 7, 3);
    for (let i = 0; i < m; i++) {
      const ph = r2() * 6.283;
      const sp2 = 0.014 + r2() * 0.022;
      const x = ((r2() + env.t * sp2 * 0.35) % 1) * W + Math.sin(env.t * sp2 * 3.1 + ph) * W * 0.012;
      const y = (1 - ((r2() + env.t * sp2) % 1)) * H;
      const rr = (0.0016 + 0.0028 * r2()) * H;
      ctx.save();
      ctx.globalAlpha = 0.10 + 0.10 * Math.sin(env.t * 0.9 + ph);
      ctx.fillStyle = rgba(col, 1);
      ctx.beginPath(); ctx.arc(x, y, rr, 0, 6.283185307); ctx.fill();
      ctx.restore();
    }
  };

  /* ⑯ 光晕：文字背后的柔光。「闪耀纯真」专用 —— brilliant 的"闪"不是粒子，
        是**字在发光**。中心默认在字行位置（0.45 高）。 */
  BG.halo = (ctx, s, env) => {
    const S = env.S;
    const col = pickCol(s, S, 'glow');
    const base = (s.a == null ? 0.20 : s.a);
    const r = PV.H * (s.r == null ? 0.58 : s.r);
    const cx = (s.x == null ? 0.5 : s.x) * PV.W;
    const cy = (s.y == null ? 0.45 : s.y) * PV.H;
    const pulse = 1 + 0.12 * Math.sin(env.t * 0.55 + (s.ph || 0));
    PV.blob(ctx, cx, cy, r * pulse, col, base);
    PV.blob(ctx, cx, cy, r * 0.45 * pulse, col, base * 0.55);
  };

  /* ⑰ 水彩晕开：梦紫的不规则湿边。「美到想哭」用。
        实现 = 3~5 团错位柔斑叠加（PV.blob 本身是径向衰减，错位叠加后
        轮廓就不再是正圆，读作「洇开的颜料」）。不做真正的湿边模拟——
        那需要逐帧噪声场，会破确定性。湿边色带走烘焙条（不逐帧渐变）。 */
  BG.watercolor = (ctx, s, env) => {
    const S = env.S;
    const col = pickCol(s, S, 'ghost');
    const W = PV.W, H = PV.H;
    const n = s.n || 4;
    const base = (s.a == null ? 0.14 : s.a);
    const rnd = PV.rng(0x2c1b3a7d, (env.seed | 0) + 21, 5);
    for (let i = 0; i < n; i++) {
      const ph = rnd() * 6.283;
      const sp = 0.010 + rnd() * 0.016;
      const cx = W * (0.12 + 0.76 * rnd()) + Math.sin(env.t * sp * 6.283 + ph) * W * 0.035;
      const cy = H * (0.14 + 0.72 * rnd()) + Math.cos(env.t * sp * 5.2 + ph) * H * 0.035;
      const r = H * (0.30 + 0.34 * rnd());
      PV.blob(ctx, cx, cy, r, col, base * (0.5 + 0.7 * rnd()));
    }
    // 湿边感：沿对角一条极淡的色带（颜料顺着纸纹流过的方向）——烘焙条旋转贴
    const sp2 = linSprite(col, [[0, 0], [0.5, 1], [1, 0]]);
    ctx.save();
    ctx.globalAlpha = base * 0.5;
    ctx.translate(W * 0.5, H * 0.5);
    ctx.rotate(Math.atan2(H, W * 0.7));
    ctx.drawImage(sp2, -W * 0.62, -H * 0.5, W * 1.24, H);
    ctx.restore();
  };

  /* ⑱ 拱：拱形庇护轮廓。「安全之地」用。
        一笔细线画出「被抱住的容器」，比填色块更克制——
        填色会把浅底压暗，而这一段的语义恰恰是"亮、安全、无泪"。 */
  BG.arch = (ctx, s, env) => {
    const S = env.S;
    const col = pickCol(s, S, 'accent');
    const W = PV.W, H = PV.H;
    const aw = (s.w == null ? 0.52 : s.w) * W;
    const ah = (s.h == null ? 0.74 : s.h) * H;
    const cx = (s.x == null ? 0.5 : s.x) * W;
    const bottom = (s.y == null ? 0.92 : s.y) * H;
    const base = (s.a == null ? 0.22 : s.a);
    const draw = (w, a) => {
      ctx.save();
      ctx.globalAlpha = a;
      ctx.strokeStyle = rgba(col, 1);
      ctx.lineWidth = w;
      ctx.lineCap = 'round';
      ctx.beginPath();
      const r = aw / 2;
      const top = bottom - ah;
      ctx.moveTo(cx - r, bottom);
      ctx.lineTo(cx - r, top + r);
      ctx.arc(cx, top + r, r, Math.PI, 0);
      ctx.lineTo(cx + r, bottom);
      ctx.stroke();
      ctx.restore();
    };
    draw(Math.max(1.5, H * 0.0022), base);
    draw(Math.max(1.0, H * 0.0012), base * 0.45);
    // 拱顶一个小光点（庇护的"顶灯"）
    PV.blob(ctx, cx, bottom - ah + aw * 0.18, H * 0.055, col, base * 0.9);
  };

  /* ⑲ 花瓣/光点：缓落的小圆片。「小事物 / 尾奏」用。
        位置 = f(hash(i), t) 的闭式解，不累积状态 —— 同 t 两次渲染字节一致。 */
  BG.petal = (ctx, s, env) => {
    const S = env.S;
    const col = pickCol(s, S, 'accent');
    const W = PV.W, H = PV.H;
    const n = s.n || 14;
    const base = (s.a == null ? 0.16 : s.a);
    const spd = s.spd == null ? 0.020 : s.spd;
    const rnd = PV.rng(0x1f2e3d4c, (env.seed | 0) + 31, 7);
    ctx.save();
    for (let i = 0; i < n; i++) {
      const x0 = rnd(), y0 = rnd(), ph = rnd() * 6.283, sz = 0.004 + 0.006 * rnd();
      const rot = rnd() * 6.283, rotSp = (rnd() - 0.5) * 0.6;
      const fall = (y0 + env.t * spd * (0.6 + 0.8 * ((i * 7919) % 13) / 13)) % 1;
      const x = x0 * W + Math.sin(env.t * 0.35 + ph) * W * 0.018;
      const y = fall * H * 1.05 - H * 0.02;
      ctx.save();
      ctx.translate(x, y);
      ctx.rotate(rot + env.t * rotSp);
      ctx.globalAlpha = base * (0.45 + 0.55 * Math.sin(ph + env.t * 0.8));
      ctx.fillStyle = rgba(col, 1);
      // 小圆片（竖向压扁的椭圆 = 花瓣侧影）
      ctx.beginPath();
      ctx.ellipse(0, 0, sz * H * 0.5, sz * H * 0.32, 0, 0, 6.283185307);
      ctx.fill();
      ctx.restore();
    }
    ctx.restore();
  };

  PV.BGORDER = ['wash', 'mega', 'topo', 'rays', 'ink', 'halftone', 'grid', 'hatch', 'panel', 'rings',
               'leak', 'dust', 'edge',
               'sunrise', 'windowLight', 'halo', 'watercolor', 'arch', 'petal'];
})();
