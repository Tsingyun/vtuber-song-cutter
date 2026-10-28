/*! pv_parts.js —— 部件注册表。六组：treat（样式）/ enter（入场）/ hold（保持）/ exit（退场）/
 *  decor（装饰）/ fx（画面效果）。
 *
 *  纪律：
 *   · 每个部件只做一件事，参数化，不硬编码某句歌词；
 *   · 入场/保持/退场只写「字形变换」，样式只写「怎么上色」，两者正交；
 *   · 不做的比做的多 —— 每个 tier 只从白名单里取件，靠 tier 之间的反差制造节奏，
 *     而不是每句把所有效果叠一遍。
 */
(() => {
  'use strict';
  const PV = window.PV;
  const { rgba, mix, lerp, clamp, ease } = PV;

  /* ============================================================ treat：字形样式 */
  const T = (PV.TREAT = {});

  T.solid = (ctx, g, bx, by, a, S) => {
    ctx.globalAlpha = a; ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
  };

  /* 只描边不填充时，笔画窄的字（I / l / i / 1 / ! / 日文「！」）会被描成一圈
     空心矩形，读起来像「豆腐块 / 缺字」。判据用前进宽 / 字号：Archivo Black 的 I
     约 0.35em，w/m/o 都在 0.7em 以上，0.46 能把两类干净分开。 */
  const narrow = g => (g.w && g.size && g.w / g.size < 0.46);
  const fillNarrow = (ctx, g, bx, by, a, S) => {
    if (!narrow(g)) return;
    ctx.globalAlpha = a * 0.92; ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
  };

  /* 细描边：只留轮廓，用于「字很轻」的段落 */
  T.outline = (ctx, g, bx, by, a, S) => {
    ctx.globalAlpha = a; ctx.strokeStyle = S.ink;
    ctx.lineWidth = Math.max(2.4, g.size * (S.strokeW || 0.032));
    ctx.lineJoin = 'miter'; ctx.miterLimit = 3;
    ctx.strokeText(g.c, bx, by);
    fillNarrow(ctx, g, bx, by, a, S);
  };

  /* 粗描边：轮廓本身就是主体，文字 PV 最常见的一档。
     连接方式用 round 而不是 miter —— 极粗描边配尖角，外轮廓会在 A / V / W 的尖上
     炸出一根根刺（读作「渲染毛刺」，不是设计）。 */
  T.outlineThick = (ctx, g, bx, by, a, S) => {
    ctx.globalAlpha = a; ctx.strokeStyle = S.ink;
    ctx.lineWidth = Math.max(3, g.size * (S.strokeW || 0.052));
    ctx.lineJoin = 'round'; ctx.miterLimit = 2;
    ctx.strokeText(g.c, bx, by);
    fillNarrow(ctx, g, bx, by, a, S);
  };

  /* 实心 + 反色描边：字与底拉开，是最「海报」的一档 */
  T.fillOutline = (ctx, g, bx, by, a, S) => {
    ctx.globalAlpha = a;
    // 深底上「反色描边」= 用底色描一圈，边缘被啃掉 3px 且颜色发脏；
    // 改成同色描边，效果是笔画加厚，读作「墨压得重」。
    ctx.strokeStyle = S.deep ? S.ink : S.paper;
    ctx.lineWidth = Math.max(3, g.size * (S.deep ? 0.030 : 0.055));
    ctx.lineJoin = 'round'; ctx.strokeText(g.c, bx, by);
    ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
  };

  /* 双线套印：故意留一点失配，读作「印歪了」。1.5px 级别是印刷感，
     5px 以上就是廉价故障风 —— 所以位移量必须跟着字号走且封顶。 */
  T.double = (ctx, g, bx, by, a, S) => {
    const d = Math.min(g.size * 0.020, 4.5);
    ctx.globalAlpha = a * 0.55; ctx.strokeStyle = S.ghostA;
    ctx.lineWidth = Math.max(1.2, g.size * 0.012);
    ctx.lineJoin = 'round';
    ctx.strokeText(g.c, bx - d, by - d * 0.5);
    ctx.globalAlpha = a; ctx.strokeStyle = S.ink; ctx.strokeText(g.c, bx, by);
    fillNarrow(ctx, g, bx, by, a, S);
  };

  /* 袋文字（二重縁）：墨字 → 留白圈 → 外墨线。日本同人 PV / サムネ 的招牌做法，
     比「只描边」多一层留白，字因此能从任何底图上立起来。
     三趟 stroke 的顺序不能换：先画最外圈，再用纸色把中间挖空，最后填字。 */
  T.bag = (ctx, g, bx, by, a, S) => {
    if (S.deep) {
      // 深底：袋文字的「留白圈」颜色 = 底色，画上去等于把字擦细。
      // 这里换成同色描边加粗，得到的是一支更重的字，而不是一圈空心。
      const lw = Math.max(2, g.size * (S.heavyW == null ? 0.024 : S.heavyW));
      ctx.globalAlpha = a; ctx.lineJoin = 'round'; ctx.miterLimit = 2;
      ctx.strokeStyle = S.ink; ctx.lineWidth = lw * 2;
      ctx.strokeText(g.c, bx, by);
      ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
      ctx.strokeText(g.c, bx, by);
      return;
    }
    const gap = g.size * (S.bagGap == null ? 0.030 : S.bagGap);
    const ring = g.size * (S.bagRing == null ? 0.015 : S.bagRing);
    ctx.globalAlpha = a;
    ctx.lineJoin = 'round'; ctx.miterLimit = 2;
    ctx.strokeStyle = S.ink; ctx.lineWidth = (gap + ring) * 2;
    ctx.strokeText(g.c, bx, by);
    ctx.strokeStyle = S.paper; ctx.lineWidth = gap * 2;
    ctx.strokeText(g.c, bx, by);
    ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
  };

  /* 上下双色：只压「字脚」那一小段。
     早先的做法是把字的上半截整体换成灰调 —— 一条横切线穿过每个字的字腰，
     读作「渐变贴图」而不是「印刷」，是这一版里最显廉价的一处。
     现在改成在基线附近压一道 26% 高的暗部：墨在纸上会往笔画底部积一点，
     这一道就有了「墨厚」的错觉，而且不改变字的整体明度。 */
  T.twoTone = (ctx, g, bx, by, a, S) => {
    ctx.globalAlpha = a;
    ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
    const top = by - g.size * 0.26, bh = g.size * 0.30;
    ctx.save();
    ctx.beginPath(); ctx.rect(bx - g.size * 0.10, top, g.w + g.size * 0.20, bh); ctx.clip();
    ctx.fillStyle = rgba(mix(S.inkRGB, S.voidRGB, 0.52), 1);
    ctx.fillText(g.c, bx, by);
    ctx.restore();
  };

  /* 硬投影：整字错位压在下面一层，没有模糊 —— 剪纸 / 版画的做法，不脏。
     廉价感来自「模糊阴影」，所以这里坚决不用 shadowBlur。 */
  T.shadowSolid = (ctx, g, bx, by, a, S) => {
    const d = g.size * (S.shadowOff == null ? 0.032 : S.shadowOff);
    ctx.globalAlpha = a * 0.85;
    ctx.fillStyle = rgba(mix(S.voidRGB, S.accentRGB, S.deep ? 0.16 : 0.35), 1);
    ctx.fillText(g.c, bx + d, by + d * 0.72);
    ctx.globalAlpha = a; ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
  };

  /* 色差残影：青/品红各偏一点，只在重拍用 */
  T.chroma = (ctx, g, bx, by, a, S) => {
    const d = Math.max(1.6, g.size * (S.chromaAmp || 0.026));
    ctx.globalAlpha = a * 0.55; ctx.fillStyle = S.ghostA;
    ctx.fillText(g.c, bx - d, by);
    ctx.globalAlpha = a * 0.55; ctx.fillStyle = S.ghostB;
    ctx.fillText(g.c, bx + d, by);
    ctx.globalAlpha = a; ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
  };

  /* 立体挤出：向一个方向叠 N 层，层间 alpha 递减 —— 手绘字最常用的体积感 */
  T.extrude = (ctx, g, bx, by, a, S) => {
    const n = S.extrudeN || 7;
    const ox = (S.extrudeDX == null ? 0.055 : S.extrudeDX) * g.size / n;
    const oy = (S.extrudeDY == null ? 0.05 : S.extrudeDY) * g.size / n;
    for (let i = n; i >= 1; i--) {
      ctx.globalAlpha = a * (0.10 + 0.30 * (1 - i / n));
      ctx.fillStyle = S.extrudeCol || S.ghostA;
      ctx.fillText(g.c, bx + ox * i, by + oy * i);
    }
    ctx.globalAlpha = a; ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
  };

  /* 发光：只给关键短句，面积小才不糊 */
  T.glow = (ctx, g, bx, by, a, S) => {
    ctx.save();
    ctx.shadowColor = rgba(S.glowRGB || S.accentRGB, 0.85);
    ctx.shadowBlur = g.size * (S.glowR || 0.22) * (S._dpr || 1);
    ctx.globalAlpha = a; ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
    ctx.restore();
  };

  /* 下線：字底下压一道强调色细线（ルビ下線 / 手绘底线）。
     它比「荧光笔块」高级的地方在于：只声明「这一段重要」，不改变字本身的明度，
     而块状高亮会把字的中宫吃掉 —— 那是廉价短视频字幕的做法。 */
  T.marker = (ctx, g, bx, by, a, S) => {
    const t = Math.max(2, g.size * 0.062);
    const y = by + g.size * 0.14;
    ctx.globalAlpha = a * 0.92; ctx.fillStyle = S.accent;
    ctx.fillRect(bx - g.size * 0.012, y, g.w + g.size * 0.024, t);
    ctx.globalAlpha = a; ctx.fillStyle = S.ink; ctx.fillText(g.c, bx, by);
  };

  /* 强调色实心：关键字直接换颜色。最干净的一档，用在大字上比任何描边都醒目。 */
  T.accent = (ctx, g, bx, by, a, S) => {
    ctx.globalAlpha = a; ctx.fillStyle = S.accent; ctx.fillText(g.c, bx, by);
  };

  /* 渐变切字：旧实现逐帧 createLinearGradient，既贵又不稳（见 twoTone 注释）。
     现在整档改由 twoTone 承担，这里保留名字只做转发，老编排稿不会因此失效。 */
  T.stripe = (ctx, g, bx, by, a, S) => T.twoTone(ctx, g, bx, by, a, S);

  /* 挖空：字是「洞」，露出底下的画 —— 与人物层配合时最有效 */
  T.knockout = (ctx, g, bx, by, a, S) => {
    ctx.globalAlpha = a;
    ctx.globalCompositeOperation = 'destination-out';
    ctx.fillText(g.c, bx, by);
    ctx.globalCompositeOperation = 'source-over';
  };

  /* ============================================================ enter：入场
     e = 该字的出场进度（0 未出现，1 完全就位）。只写字形变换。 */
  const E = (PV.ENTER = {});

  E.fade = (g, e) => { g.alpha = e; };
  E.fadeSlow = (g, e) => { g.alpha = ease.inOutCubic(e); };

  E.rise = (g, e) => {
    const k = ease.outQuint(e);
    g.alpha = clamp(e * 2.2, 0, 1); g.dy = (1 - k) * g.size * 0.42;
  };

  E.drop = (g, e) => {
    const k = ease.outCubic(e);
    g.alpha = clamp(e * 3, 0, 1); g.dy = -(1 - k) * g.size * 0.55;
  };

  /* 砸下：起手超出画幅再压回来，重拍专用 */
  E.slam = (g, e) => {
    const k = ease.outQuint(e);
    g.alpha = clamp(e * 4, 0, 1);
    g.sc = lerp(3.0, 1, k);
    g.dy = -(1 - k) * g.size * 0.05;
  };

  E.scalePop = (g, e) => {
    g.alpha = clamp(e * 3, 0, 1);
    g.sc = lerp(0.55, 1, ease.outBack(e));
  };

  /* 横向拉出（拉伸到就位）：g.sx 从很扁拉回 1，有「扯出来」的手感 */
  E.stretchIn = (g, e) => {
    const k = ease.outQuint(e);
    g.alpha = clamp(e * 2.5, 0, 1);
    g.sx = lerp(0.06, 1, k);   // drawGlyphs 以字形中心缩放，天然从中心展开
  };

  /* 竖向切片擦除：字形自上而下长出来 */
  E.sliceDown = (g, e) => {
    g.alpha = 1;
    g.clip = [0, 0, 1, ease.outCubic(e)];
  };

  /* 自下而上的擦除 */
  E.sliceUp = (g, e) => {
    g.alpha = 1;
    const k = ease.outCubic(e);
    g.clip = [0, 1 - k, 1, 1];
  };

  /* 打字机：无过渡，只有 0/1 —— 最硬的节奏，适合副歌的口播感段落 */
  E.typeOn = (g, e) => { g.alpha = e > 0.001 ? 1 : 0; };

  /* 旋转落位 */
  E.spinIn = (g, e, rnd) => {
    const k = ease.outCubic(e);
    g.alpha = clamp(e * 3, 0, 1);
    g.rot = (1 - k) * (rnd > 0.5 ? 0.9 : -0.9);
    g.sc = lerp(0.4, 1, k);
  };

  /* 由「散」到「聚」：每字带一个随机偏移飞入 */
  E.scatterIn = (g, e, rnd, rnd2) => {
    const k = ease.outCubic(e);
    g.alpha = clamp(e * 2, 0, 1);
    g.dx = (rnd - 0.5) * g.size * 3.2 * (1 - k);
    g.dy = (rnd2 - 0.5) * g.size * 2.6 * (1 - k);
    g.rot = (rnd - 0.5) * 1.6 * (1 - k);
  };

  /* ============================================================ hold：保持
     所有模块都必须「无输入时等于恒等变换」，否则静止帧也会抖。 */
  const Ho = (PV.HOLD = {});

  Ho.none = () => {};

  /* 呼吸：整体极缓的缩放，周期 4.5s */
  Ho.breathe = (g, t, ph) => {
    g.sc *= 1 + Math.sin((t / 4.5 + ph) * 6.283185307) * 0.012;
  };

  /* 逐字漂移：每字相位不同，读作纸上的字在微微浮动 */
  Ho.drift = (g, t, ph) => {
    g.dy += Math.sin((t / 6.0 + ph) * 6.283185307) * g.size * 0.022;
    g.dx += Math.cos((t / 7.3 + ph * 1.7) * 6.283185307) * g.size * 0.014;
  };

  /* 抖动：高频小幅，只在鼓点上开 —— 平时必须关掉，否则整片发毛 */
  Ho.jitter = (g, t, ph, spec, amp) => {
    // spec.amp 是「设计档位」（0.8~2.2），不是一个乘法系数，必须再乘一个很小的基准
    const a = (amp == null ? 1 : amp) * (spec && spec.amp != null ? spec.amp : 1) * 0.0075;
    g.dx += Math.sin(t * 41.3 + ph * 6.283) * g.size * a;
    g.dy += Math.cos(t * 37.1 + ph * 6.283) * g.size * a;
  };

  /* 逐字起伏：像声波一样依次起落 */
  Ho.wave = (g, t, ph) => {
    g.dy -= Math.sin((t / 1.7 - ph * 0.55) * 6.283185307) * g.size * 0.036;
  };

  /* 缓推：整行缓慢放大（相机推近的替代，不必动相机） */
  Ho.trackIn = (g, t, ph, spec, amp) => {
    const k = (spec && spec.amt != null ? spec.amt : 0.045) * (amp == null ? 1 : amp);
    g.sc *= 1 + k * clamp(t / 6, 0, 1);
  };

  /* 相位残影：只改 alpha 做「叠影在动」的暗示，真残影由 draw 侧的 trail 处理 */
  Ho.flutter = (g, t, ph) => {
    g.alpha *= 0.94 + 0.06 * Math.sin((t / 2.3 + ph) * 6.283185307);
  };

  /* ============================================================ exit：退场 */
  const X = (PV.EXIT = {});

  X.cut = () => {};
  X.fade = (g, e) => { g.alpha *= 1 - e; };
  X.fadeUp = (g, e) => { g.alpha *= 1 - ease.inQuad(e); g.dy -= ease.inCubic(e) * g.size * 0.30; };

  X.scatterOut = (g, e, rnd, rnd2) => {
    const k = ease.inCubic(e);
    g.alpha *= 1 - e;
    g.dx += (rnd - 0.5) * g.size * 4.5 * k;
    g.dy += (rnd2 - 0.5) * g.size * 3.5 * k;
    g.rot += (rnd - 0.5) * 2.2 * k;
  };

  X.blurOut = (g, e) => {
    g.alpha *= 1 - e;
    g.sx *= 1 + ease.inCubic(e) * 0.5;
    g.sy *= 1 - ease.inCubic(e) * 0.6;
  };

  /* 溶解：每字按随机阈值先后消失，不是整行一起淡 */
  X.dissolve = (g, e, rnd) => {
    const thr = rnd * 0.55;
    g.alpha *= 1 - clamp((e - thr) / (1 - thr), 0, 1);
  };

  /* 吸入：向一个点收拢 */
  X.suckIn = (g, e, rnd, rnd2, spec, cx, cy) => {
    const k = ease.inCubic(e);
    const [gx, gy] = PV.glyphCenter(g);
    g.dx += (cx - gx) * k;
    g.dy += (cy - gy) * k;
    g.alpha *= 1 - k * 0.9;
    g.sc *= 1 - k * 0.7;
  };

  /* 压扁消失：像被门缝夹走 */
  X.flatten = (g, e) => {
    g.alpha *= 1 - e;
    g.sy *= 1 - ease.inQuad(e);
  };

  /* ============================================================ decor：装饰
     装饰不是「贴纸」，是用来建立构图的骨架：分割线、色块、点阵、边框。 */
  const D = (PV.DECOR = {});

  /* 细分割线（可带刻度），把画面切成版面 */
  D.rule = (ctx, s, st) => {
    const { x0, x1, y, a, w, ticks } = s;
    PV.line(ctx, x0, y, x1, y, st.S.ink, a == null ? 0.16 : a, w || 1);
    if (ticks) {
      const n = ticks;
      for (let i = 0; i <= n; i++) {
        const x = x0 + (x1 - x0) * i / n;
        PV.line(ctx, x, y - 3, x, y + 3, st.S.ink, (a == null ? 0.16 : a) * 0.9, 1);
      }
    }
  };

  /* 竖线：和歌词成垂直张力 */
  D.vrule = (ctx, s, st) => {
    PV.line(ctx, s.x, s.y0, s.x, s.y1, st.S.ink, s.a == null ? 0.14 : s.a, s.w || 1);
  };

  /* 色块：构图主体之一，压在字后面形成「版面」 */
  D.block = (ctx, s, st) => {
    ctx.globalAlpha = s.a == null ? 0.9 : s.a;
    ctx.fillStyle = s.col || rgba(st.S.accentRGB, 1);
    ctx.fillRect(s.x, s.y, s.w, s.h);
    ctx.globalAlpha = 1;
  };

  /* 边框内缩框：印刷品的「版心」 */
  D.frame = (ctx, s, st) => {
    const m = s.m == null ? 64 : s.m;
    ctx.globalAlpha = s.a == null ? 0.10 : s.a;
    ctx.strokeStyle = st.S.ink; ctx.lineWidth = s.w || 1;
    ctx.strokeRect(m, m, PV.W - m * 2, PV.H - m * 2);
    ctx.globalAlpha = 1;
  };

  /* 裁切角标：四角的十字，读作「取景框」 */
  D.cropMarks = (ctx, s, st) => {
    const m = s.m == null ? 48 : s.m, L = s.len == null ? 22 : s.len;
    const a = s.a == null ? 0.22 : s.a;
    const P = [[m, m, 1, 1], [PV.W - m, m, -1, 1], [m, PV.H - m, 1, -1], [PV.W - m, PV.H - m, -1, -1]];
    for (const [x, y, sx, sy] of P) {
      PV.line(ctx, x, y, x + L * sx, y, st.S.ink, a, 1);
      PV.line(ctx, x, y, x, y + L * sy, st.S.ink, a, 1);
    }
  };

  /* 点阵：半调网点，给空白处密度 */
  D.dots = (ctx, s, st) => {
    const step = s.step || 26, r = s.r || 1.1, a = s.a == null ? 0.18 : s.a;
    ctx.globalAlpha = a; ctx.fillStyle = st.S.ink;
    for (let y = s.y0; y < s.y1; y += step) {
      for (let x = s.x0; x < s.x1; x += step) {
        if (s.mask && !(x > s.mask[0] && x < s.mask[2] && y > s.mask[1] && y < s.mask[3])) continue;
        ctx.beginPath(); ctx.arc(x, y, r, 0, 6.283185307); ctx.fill();
      }
    }
    ctx.globalAlpha = 1;
  };

  /* 放射线：从一点发散，用于副歌爆点（低频、少量，不要做成廉价爆炸） */
  D.speedLines = (ctx, s, st) => {
    const n = s.n || 26, a = s.a == null ? 0.10 : s.a;
    ctx.globalAlpha = a; ctx.strokeStyle = st.S.ink; ctx.lineWidth = s.w || 1;
    for (let i = 0; i < n; i++) {
      const ang = (i / n) * 6.283185307 + (s.phase || 0);
      const r0 = s.r0 || 320, r1 = s.r1 || 1200;
      ctx.beginPath();
      ctx.moveTo(s.cx + Math.cos(ang) * r0, s.cy + Math.sin(ang) * r0);
      ctx.lineTo(s.cx + Math.cos(ang) * r1, s.cy + Math.sin(ang) * r1);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  };

  /* 条形码：极细的竖条簇，专业感的「元信息」暗示 */
  D.barcode = (ctx, s, st) => {
    const rnd = PV.rng(s.seed || 7, 11, 13);
    let x = s.x;
    ctx.globalAlpha = s.a == null ? 0.42 : s.a;
    ctx.fillStyle = st.S.ink;
    while (x < s.x + s.w) {
      const bw = 1 + Math.floor(rnd() * 3);
      const hh = s.h * (0.45 + rnd() * 0.55);
      ctx.fillRect(x, s.y + (s.h - hh), bw, hh);
      x += bw + 2 + Math.floor(rnd() * 3);
    }
    ctx.globalAlpha = 1;
  };

  /* 细进度条：把「这是第几秒」做成版面元素，而不是播放器控件 */
  D.progress = (ctx, s, st) => {
    const p = clamp(s.p, 0, 1);
    PV.line(ctx, s.x, s.y, s.x + s.w, s.y, st.S.ink, 0.14, 1);
    PV.line(ctx, s.x, s.y, s.x + s.w * p, s.y, rgba(st.S.accentRGB, 1), 0.85, 2);
  };

  /* 章节标记：小字 + 短线，像排版稿上的栏目标签 */
  D.chapter = (ctx, s, st) => {
    PV.line(ctx, s.x, s.y, s.x + 26, s.y, rgba(st.S.accentRGB, 1), 0.8, 2);
    PV.label(ctx, s.text, s.x + 34, s.y + 5, s.size || 15, st.S.ink,
      s.a == null ? 0.55 : s.a, 600, 'left', 0.22);
  };

  /* ============================================================ fx：画面效果（后期）
     返回需要叠加到整帧的变换/遮罩；引擎负责实际应用。 */
  const F = (PV.FX = {});

  F.none = () => null;

  /* 闪白/闪墨：单帧或极短，用于重拍；alpha 由引擎按包络给 */
  F.flash = (ctx, s, st) => {
    if (st.fx.a <= 0.002) return;
    PV.rect(ctx, 0, 0, PV.W, PV.H, s.col ? s.col : st.S.ink, st.fx.a);
  };

  /* 色差爆发：整帧在通道层错位（用两次低 alpha 重绘近似） */
  F.chromaBurst = (ctx, s, st) => {
    const a = st.fx.a;
    if (a <= 0.01) return;
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    ctx.globalAlpha = a * 0.10;
    ctx.fillStyle = rgba(st.S.ghostA, 1);
    ctx.fillRect(-a * 22, 0, PV.W, PV.H);
    ctx.fillStyle = rgba(st.S.ghostB, 1);
    ctx.fillRect(a * 22, 0, PV.W, PV.H);
    ctx.restore();
  };

  /* 暗角：常驻的极淡暗角，把注意力收到画面中心 */
  F.vignette = (ctx, s, st) => {
    const r0 = s.r0 || PV.H * 0.42, r1 = s.r1 || PV.H * 1.05;
    const g = ctx.createRadialGradient(PV.W / 2, PV.H * 0.48, r0, PV.W / 2, PV.H * 0.48, r1);
    g.addColorStop(0, 'rgba(0,0,0,0)');
    g.addColorStop(1, PV.rgba(st.S.voidRGB, s.a == null ? 0.55 : s.a));
    ctx.save(); ctx.globalAlpha = 1; ctx.fillStyle = g;
    ctx.fillRect(0, 0, PV.W, PV.H); ctx.restore();
  };
})();
