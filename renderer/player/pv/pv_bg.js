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

  PV.BGORDER = ['wash', 'mega', 'ink', 'halftone', 'grid', 'hatch', 'panel', 'rings',
               'leak', 'dust', 'edge'];
})();
