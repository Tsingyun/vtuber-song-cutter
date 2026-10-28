/*! pv_type.js —— 排版引擎：把一个词/字变成可独立变换的字形单元，并做 12 种构图。
 *
 *  核心矛盾：术力口文字 PV 要「逐字」控制（每个字单独出场/旋转/碎裂），
 *  但拉丁文一旦逐字画就丢掉 kerning，大字号下字距会散架。
 *  解法：位置用 **前缀测量**（ctx.measureText(text.slice(0,i))）算，
 *  这样累积宽度里含 kerning；绘制仍逐字，于是既保住字距又能单字变换。
 */
(() => {
  'use strict';
  const PV = window.PV;
  const hex = PV.hex, rgba = PV.rgba;

  /* ---------------- 字体表 ----------------
     Dela Gothic One / Archivo Black / Anton 都只有一个字重（本身就是超粗），权重字段照填不误。
     CJK 兜底走播放器已有的 Noto Sans SC（同目录 fonts/），日文假名另有 Zen 系列覆盖。 */
  PV.FACES = {
    heavy:     { family: '"Dela Gothic One"', weight: 400, kind: 'gothic' },
    grotesk:   { family: '"Archivo Black"', weight: 400, kind: 'gothic' },
    condensed: { family: '"Anton"', weight: 400, kind: 'condensed' },
    tall:      { family: '"Bebas Neue"', weight: 400, kind: 'condensed' },
    sans:      { family: '"Zen Kaku Gothic New"', weight: 900, kind: 'gothic' },
    sansMed:   { family: '"Zen Kaku Gothic New"', weight: 700, kind: 'gothic' },
    sansLt:    { family: '"Zen Kaku Gothic New"', weight: 500, kind: 'gothic' },
    mincho:    { family: '"Zen Old Mincho"', weight: 900, kind: 'serif' },
    minchoLt:  { family: '"Zen Old Mincho"', weight: 700, kind: 'serif' },
    /* Didone：细横粗竖的高对比衬线，大字号才显出「印刷」的骨。
       文字 PV 里「細明朝 × 極太ゴシック」的对撞靠它成立。 */
    didone:    { family: '"Bodoni Moda"', weight: 900, kind: 'serif' },
    didoneLt:  { family: '"Bodoni Moda"', weight: 400, kind: 'serif' },
    didoneIt:  { family: '"Bodoni Moda"', weight: 700, kind: 'serif', italic: true },
    mono:      { family: '"IBM Plex Mono"', weight: 400, kind: 'mono' },
    monoB:     { family: '"IBM Plex Mono"', weight: 600, kind: 'mono' },
  };
  const FALLBACK = '"Noto Sans SC", "HarmonyOS Sans SC", sans-serif';
  PV.cssFace = (key, size) => {
    const f = PV.FACES[key] || PV.FACES.sans;
    return (f.italic ? 'italic ' : '') + f.weight + ' ' + size + 'px ' + f.family + ', ' + FALLBACK;
  };

  /* ---------------- 光学字距 ----------------
     浏览器给的 kerning 是「排版正确」，但展示级大字号要的是「视觉正确」：
     A/V/W/Y 这类斜边字并排时，字形外接框贴合、视觉却空出一块。
     表里是额外收紧量（单位 em），只在字对首字母上起作用。
     为什么要自己做：这些字对在 200px 以上时，默认间距一眼就能看出「软件默认感」，
     而文字 PV 的字距本身就是设计语言的一部分。 */
  const KERN = {
    AV: -0.075, AW: -0.070, AY: -0.080, AT: -0.060, AP: -0.045, AF: -0.050,
    LT: -0.075, LV: -0.070, LW: -0.070, LY: -0.090, Lu: -0.030,
    PA: -0.055, FA: -0.055, TA: -0.060, Te: -0.045, To: -0.050, Tr: -0.040,
    Va: -0.045, Ve: -0.045, Vo: -0.050, Wa: -0.040, We: -0.040, Wo: -0.045,
    Ya: -0.060, Yo: -0.065, Ye: -0.055,
    'r.': -0.075, 'r,': -0.075, '.,': -0.050, 'Y.': -0.080, 'V.': -0.070,
    'o.': -0.020, 'e.': -0.020,
  };
  /* 展示级字号要整体收紧一点（optical sizing）：小字松、大字紧，是排版的常识。
     阈值取 120px —— 低于它的都是正文尺度，收紧了反而糊。 */
  const opticalTighten = size => (size > 120 ? -0.006 - Math.min(0.010, (size - 120) / 30000) : 0);
  function kernAt(text, i, size) {
    if (i <= 0 || i >= text.length) return 0;
    const pair = text[i - 1] + text[i];
    const k = KERN[pair];
    return (k == null ? 0 : k) * size;
  }

  const _mcache = new Map();
  /* 累计宽度（含 kerning）。key 里带 face 与 size，调用点尺寸种类有限，缓存不会爆 */
  function prefixWidths(ctx, text, face, size) {
    const key = face + '|' + size + '|' + text;
    let v = _mcache.get(key);
    if (v) return v;
    ctx.save();
    ctx.font = PV.cssFace(face, size);
    v = new Array(text.length + 1);
    v[0] = 0;
    for (let i = 1; i <= text.length; i++) v[i] = ctx.measureText(text.slice(0, i)).width;
    ctx.restore();
    if (_mcache.size > 4000) _mcache.clear();
    _mcache.set(key, v);
    return v;
  }
  PV.measure = (ctx, text, face, size) => prefixWidths(ctx, text, face, size)[text.length];

  /* ---------------- 构建字形单元 ----------------
     输入一行（含 words 时间）→ 字形数组。tracking 以 em 计，是排版性格的主要旋钮之一。 */
  PV.buildGlyphs = (ctx, line, st) => {
    const face = st.face || 'sans';
    const size = st.size || 120;
    // 负字距是「压紧」的性格，但超过 -0.09em 就会真的压字，全片统一封死
    const track = PV.clamp(st.track == null ? 0 : st.track, -0.09, 0.6) * size;
    /* 纵向压缩：术力口文字 PV 的招牌手法之一（明朝体压到 70~85% 当显示体用）。
       不做这个，抒情段只能靠「换字体」制造差异，而字体一共就那几把。 */
    const sqz = PV.clamp(st.squeeze == null ? 1 : st.squeeze, 0.45, 1.4);
    const tight = opticalTighten(size) * size;
    const text = st.text != null ? st.text : line.text;
    const pw = prefixWidths(ctx, text, face, size);

    const glyphs = [];
    /* line.words 可能是空数组（片头标题卡、纯装饰行），空数组是 truthy ⇒ 不能用 || 兜底，
       必须判长度。否则 words[...] 取到 undefined，全片直接崩在 setup。 */
    const words = (line.words && line.words.length)
      ? line.words
      : [{ text: text, t0: line.t0 == null ? 0 : line.t0, t1: line.t1 == null ? 0 : line.t1 }];
    let wi = 0, wStart = 0;
    let kernAcc = 0;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      kernAcc += kernAt(text, i, size) + tight;      // 累积光学间距（含大字号整体收紧）
      // 推进词指针：按「词在串里的实际位置」滑动，不依赖 line.text 的长度（可能被 st.text 覆盖）
      while (wi < words.length - 1 && i >= wStart + words[wi].text.length) {
        wStart += words[wi].text.length;
        while (wStart < text.length && text[wStart] === ' ') wStart++;
        wi++;
      }
      const w = words[wi] || words[words.length - 1];
      const isSpace = ch === ' ';
      glyphs.push({
        c: ch, isSpace,
        i, wi: Math.min(wi, words.length - 1),
        x: pw[i] + track * i + kernAcc, y: 0,
        w: Math.max(0, pw[i + 1] - pw[i]),
        size, face, sqz,
        t0: w.t0, t1: w.t1, wt0: w.t0, wt1: w.t1,
        rot: 0, sc: 1, sx: 1, sy: 1, alpha: 1, dx: 0, dy: 0, skew: 0,
        // 出场进度：由 parts 在每帧写入（0=未出现 1=完全出现）
        e: 1, vis: 1,
      });
    }
    return glyphs;
  };

  /* 字形总宽（含 tracking） */
  PV.glyphWidth = gs => {
    if (!gs.length) return 0;
    const l = gs[gs.length - 1], f = gs[0];
    return (l.x + l.w) - f.x;
  };

  /* 字形视觉中心（旋转/缩放的轴心）：横向取中点，纵向取大写字高的中部 */
  PV.glyphCenter = g => [g.x + g.w / 2 + g.dx, g.y - g.size * 0.36 + g.dy];

  /* ---------------- 绘制 ----------------
     paint 负责「样式」（填充/描边/双线/挤压），位置变换由 drawGlyphs 统一处理。
     这样样式与运动正交，换样式不必重写运动。 */
  PV.drawGlyphs = (ctx, glyphs, paint, opts) => {
    const o = opts || {};
    for (const g of glyphs) {
      if (g.alpha <= 0.004 || g.vis <= 0.004) continue;
      if (g.isSpace) continue;
      ctx.save();
      const [cx, cy] = PV.glyphCenter(g);
      ctx.translate(cx, cy);
      if (g.rot) ctx.rotate(g.rot);
      if (g.skew) ctx.transform(1, 0, g.skew, 1, 0, 0);
      const sx = g.sx * g.sc, sy = g.sy * g.sc * (g.sqz == null ? 1 : g.sqz);
      if (sx !== 1 || sy !== 1) ctx.scale(sx, sy);
      ctx.font = PV.cssFace(g.face, g.size);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      const bx = -g.w / 2, by = g.size * 0.36;
      /* 擦除/切片类入场靠字形自身裁剪，而不是整层裁剪：
         整层裁剪会让同一行的其它字一起被切，读起来是「画面在动」而不是「字在出现」。 */
      if (g.clip) {
        /* clip = [x0, y0, x1, y1]，归一化到「字形视觉框」：
           x 按前进宽度，y 从 cap 顶（0）到降部底（1）。
           早先按「基线±size」解释，导致 sliceUp 只露出基线以下的降部 ⇒ 整行字变成几条细线。 */
        const TOP = by - g.size * 1.0, BH = g.size * 1.26;
        const c = g.clip;
        ctx.beginPath();
        ctx.rect(bx + c[0] * g.w, TOP + c[1] * BH, (c[2] - c[0]) * g.w, (c[3] - c[1]) * BH);
        ctx.clip();
      }
      paint(ctx, g, bx, by, g.alpha * g.vis);
      ctx.restore();
    }
    ctx.globalAlpha = 1;
  };

  /* ---------------- 构图（layout） ----------------
     每个 layout 只负责「把字形摆到该在的位置并定字号」，不管出场退场。
     返回 { w, h, bbox:[x0,y0,x1,y1] } 供人物层避让与安全边检查。 */
  const L = (PV.LAYOUT = {});
  const W = PV.W, H = PV.H;

  /* cy 兼容两种写法：0~1 视作画面高度比例，>1.5 视作绝对像素。
     导演层写的是比例（cy:0.46），早先被当成像素用，标题整行被推到 y≈0 出画面。 */
  const cyOf = (st, def) => {
    const v = st.cy == null ? def : st.cy;
    return v > 1.5 ? v : v * H;
  };

  /* 位置参数统一：0~1 视作比例，>1.5 视作像素。
     导演层写的是比例（x:0.085 / y:0.24），若当像素用，整行字会被塞进左上角 1 像素内。 */
  const frac = (v, def, span) => (v == null ? def * span : (v > 1.5 ? v : v * span));
  const xOf = (st, def) => frac(st.x, def, W);
  const yOf = (st, def) => frac(st.y, def, H);

  const bboxOf = gs => {
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (const g of gs) {
      if (g.isSpace) continue;
      const [cx, cy] = PV.glyphCenter(g);
      const hw = (g.w * g.sx) / 2, hh = (g.size * 0.72 * g.sy * (g.sqz == null ? 1 : g.sqz)) / 2;
      x0 = Math.min(x0, cx - hw); x1 = Math.max(x1, cx + hw);
      y0 = Math.min(y0, cy - hh); y1 = Math.max(y1, cy + hh);
    }
    return x0 > 1e8 ? [0, 0, 0, 0] : [x0, y0, x1, y1];
  };

  /* 按可用宽度反推字号：先在 100px 下量一次，再按比例缩放（advance 与字号线性） */
  const fitSize = (ctx, text, face, availW, maxSize, trackEm) => {
    const base = 100;
    const w100 = PV.measure(ctx, text, face, base) + (trackEm || 0) * base * text.length;
    if (w100 <= 0) return maxSize;
    return Math.min(maxSize, (availW / w100) * base);
  };

  /* ① 居中单行 */
  L.center = (ctx, gs, st) => {
    const avail = W * (st.availW || 0.82);
    const size = st.size || fitSize(ctx, st.text, st.face, avail, st.maxSize || 150, st.track);
    const g = PV.buildGlyphs(ctx, st.line, Object.assign({}, st, { size }));
    const tw = PV.glyphWidth(g);
    const x0 = (st.cx != null ? st.cx : W / 2) - tw / 2;
    const y = cyOf(st, 0.52);
    for (const q of g) { q.x += x0; q.y = y; }
    return { glyphs: g, bbox: bboxOf(g), size };
  };

  /* ② 满幅大字：字号取到「高度占满」，宽度溢出就让它溢出（边缘裁切是文字 PV 的基本语汇） */
  L.centerBig = (ctx, gs, st) => {
    const size = st.size || Math.round((st.fillH || 0.62) * H);
    const g = PV.buildGlyphs(ctx, st.line, Object.assign({}, st, { size }));
    const tw = PV.glyphWidth(g);
    const x0 = (st.cx != null ? st.cx : W / 2) - tw / 2;
    const y = cyOf(st, 0.62);
    for (const q of g) { q.x += x0; q.y = y; }
    return { glyphs: g, bbox: bboxOf(g), size, bleed: tw > W };
  };

  /* ③ 竖排（縦書き）：从右往左分栏；拉丁字母整体旋 90°（縦中横的简化处理） */
  L.column = (ctx, gs, st) => {
    const size = st.size || 84;
    const lh = size * (st.lead || 1.06);
    const colH = st.colH || H * 0.74;
    const perCol = Math.max(1, Math.floor(colH / lh));
    const g = PV.buildGlyphs(ctx, st.line, Object.assign({}, st, { size }));
    const chars = g.filter(q => !q.isSpace);
    const nCol = Math.ceil(chars.length / perCol);
    const gap = size * (st.colGap || 1.5);
    const rightX = xOf(st, 0.84);
    const topY = yOf(st, 0.18);
    chars.forEach((q, k) => {
      const c = Math.floor(k / perCol), r = k % perCol;
      q.x = rightX - c * gap;
      q.y = topY + (r + 1) * lh;
      if (/[0-9A-Za-z]/.test(q.c)) q.rot = Math.PI / 2;      // 拉丁字站直
    });
    return { glyphs: chars, bbox: bboxOf(chars), size, cols: nCol };
  };

  /* ④ 堆叠：逐词换行，左对齐，字号大、行距紧
     必须双向适配：行数多时按高度收，某行太长时按宽度收，否则字会跑到画外
     （实测 11 个词的句子在 size=129 下排到 y=1668，整段不见）。 */
  L.stack = (ctx, gs, st) => {
    const words = st.wordsForLayout
      || ((st.line.words && st.line.words.length) ? st.line.words.map(w => w.text)
                                                  : String(st.text || st.line.text || '').split(' '));
    const rows = Math.max(1, words.length);
    const availH = H * (st.availH || 0.74);
    const availW = W * (st.availW || 0.80);
    let size = Math.min(st.size || 190, availH / (rows * 1.02));
    let maxW = 0;
    for (const w of words) maxW = Math.max(maxW, PV.measure(ctx, w, st.face || 'sans',
      100) + (st.track || 0) * 100 * w.length);
    if (maxW > 0) size = Math.min(size, (availW / maxW) * 100);
    size = Math.max(24, Math.round(size));
    const lh = size * 1.02;
    const out = [];
    // 垂直居中于 yOf 指定的中心，而不是从顶部往下堆
    const cY = yOf(st, 0.50);
    let y = cY - ((rows - 1) * lh) / 2 + size * 0.36;
    const x = xOf(st, 0.09);
    words.forEach((wordTxt, k) => {
      const sub = { text: wordTxt };
      const g = PV.buildGlyphs(ctx, st.line, Object.assign({}, st, { size, text: wordTxt }));
      // 时间按词取，供逐词入场
      const wref = st.line.words[k] || { t0: st.line.t0, t1: st.line.t1 };
      for (const q of g) { q.t0 = wref.t0; q.t1 = wref.t1; q.wi = k; }
      const off = st.stair ? k * size * 0.42 : 0;            // 阶梯错位
      for (const q of g) { q.x += x + off; q.y = y; q.row = k; q.rowGap = lh; }
      out.push(...g);
      y += lh;
    });
    return { glyphs: out, bbox: bboxOf(out), size };
  };

  /* ⑤ 分栏错位：两行，第二行贴右边界
     为什么第二行要贴右：两行都左对齐时，读者只会读到「两行字」；
     一行顶左、一行顶右，画面自动产生一条对角线的视线，这才是版面。 */
  L.split = (ctx, gs, st) => {
    const txt = st.text || st.line.text;
    const words = txt.split(' ').filter(Boolean);
    const face = st.face || 'sans';
    const track = st.track || 0;

    /* 切点按宽度挑：让两行宽度最接近。按词数对半分在长句上会把一行拉得极长，
       另一行只剩两个词（读作「断句断错了」）。 */
    const wOf = s => PV.measure(ctx, s, face, 100) + track * 100 * s.length;
    let cut = Math.ceil(words.length / 2), best = Infinity;
    for (let i = 1; i < words.length; i++) {
      const d = Math.abs(wOf(words.slice(0, i).join(' ')) - wOf(words.slice(i).join(' ')));
      if (d < best) { best = d; cut = i; }
    }
    const r1 = words.slice(0, cut).join(' ');
    const r2 = words.slice(cut).join(' ');

    const availW = W * (st.availW || 0.84);
    const maxw = Math.max(wOf(r1), wOf(r2));
    let size = st.size || 150;
    if (maxw > 0) size = Math.min(size, (availW / maxw) * 100);
    size = Math.max(24, Math.round(size));

    const out = [];
    const pad = W * 0.085;
    /* 行距必须跟着字号走。原先固定 H*0.21（=227px）配 size 320 的两行，
       基线只差 227 而字高约 230 —— 第二行直接骑到第一行的降部上，
       末词像「砸」在句首（实测 181.4s 的 it 压在 don't 上）。
       取 max(固定值, 1.12×size)，并把两行整体垂直居中。 */
    const lh = Math.max(H * 0.21, size * 1.12);
    const yTop = H * 0.50 - lh * 0.5 + size * 0.36;
    /* 右端限位：给了 cx/availW 说明右侧留给了人物，第二行「顶右」只能顶到版心右缘，
       不能再顶到画布右缘 —— 否则末词会被圆窗整个盖掉（实测 "life" 只剩 "lif"）。 */
    const xRight = (st.cx != null && st.availW)
      ? Math.min(W - pad, st.cx + W * st.availW / 2)
      : W - pad;
    /* 左端同样要限位：cx/availW 定义的是「文字版心」这条带子，不是只管右边界。
       原实现第一行永远从 pad 起，于是「左边站着一个 slab 人物」时，
       整行字直接压在人脸上（实测 43.3s 的 split：This moment 正好盖住眼睛）。
       给了 cx 就两头都按版心收，字才会真的让开人物。 */
    const xLeft = (st.cx != null && st.availW)
      ? Math.max(pad, st.cx - W * st.availW / 2)
      : pad;
    [[r1, 0, pad], [r2, 1, null]].forEach(([rowTxt, ri, fixedX]) => {
      if (!rowTxt) return;
      const g = PV.buildGlyphs(ctx, st.line, Object.assign({}, st, { size, text: rowTxt }));
      const w = PV.glyphWidth(g);
      // 第一行顶左、第二行顶右；宽度不够时退回左对齐，避免两行只剩一角
      const x = (ri === 0) ? xLeft : Math.max(xLeft, xRight - w);
      const y = yTop + ri * lh;
      const wl = st.line.words.slice(ri ? cut : 0, ri ? words.length : cut);
      const wiOff = ri ? cut : 0;
      /* 词归属必须**按词的实字数**精确分配。原先用「平均每词几个字」估算
         （rowTxt.length / wl.length），只要词长参差就会把一个词劈成两半：
         实测「life」的 e 被算到下一个词，于是 22.48s 的帧上停在 "lif"，
         读起来像渲染截断而不是「唱到哪儿长到哪儿」。*/
      const sizes = wl.map(w => (w.text || '').replace(/\s/g, '').length || 1);
      let ci = 0;
      for (const q of g) {
        if (!q.isSpace) {
          let k = 0, acc = 0;
          while (k < sizes.length - 1 && ci >= acc + sizes[k]) { acc += sizes[k]; k++; }
          const w = wl[Math.min(k, wl.length - 1)] || { t0: st.line.t0, t1: st.line.t1 };
          q.wi = wiOff + Math.min(k, wl.length - 1);
          if (w) { q.t0 = w.t0; q.t1 = w.t1; }
          ci++;
        }
        q.x += x; q.y = y;
        /* rowGap 供入场动效做「不跨行」约束（pv_engine 的 rowClamp）。
           没有这个标注时，dropBounce 会让第二行的词穿过第一行掉下来。 */
        q.row = ri; q.rowGap = lh;
      }
      out.push(...g);
    });
    return { glyphs: out, bbox: bboxOf(out), size };
  };

  /* ⑥ 密排墙：整行重复铺满，用于副歌/间奏的「冲击块」 */
  L.dense = (ctx, gs, st) => {
    const out = [];
    const txt = st.text || st.line.text;
    const gap0 = Math.round(W * 0.045);
    /* 密排墙的冲击力来自「行数 × 密度」，不是「字跑出画面」。
       此前允许行宽到 2W，行首被推出一屏，首字母整块被切（Feel → eel），
       读作渲染乱码而不是排版。现在先把字号收到「一行正好铺满」，再谈错位。 */
    const fitW = fitSize(ctx, txt, st.face || 'sans', W - 2 * gap0, st.size || H, st.track);
    const size = Math.max(28, Math.round(st.size ? Math.min(st.size, fitW) : fitW));
    /* 行距 1.14：密排墙的字是「贴」不是「叠」。1.06 时上一行的降部会插进下一行的
       中宫，远看是一团黑，近看认不出字 —— 而验收要求「文字没有严重遮挡或无法阅读」。
       1.14 仍然读作密排，但每一行都完整。 */
    const lh = size * (st.lead || 1.14);
    // 行数按可用高度封顶，避免 size 偏大时上下两行被画布切掉
    const rows = Math.max(3, Math.min(st.rows || 7, Math.floor((H * 0.94) / lh)));
    const top = (H - rows * lh) / 2 + size * 0.72;
    for (let r = 0; r < rows; r++) {
      const g = PV.buildGlyphs(ctx, st.line, Object.assign({}, st, { size, text: txt }));
      const tw = PV.glyphWidth(g) + size * 0.7;
      /* 奇偶行左右反向错开，读作跑马灯；**不要**再叠加逐行漂移 ——
         每行都漂一点会让整面墙读作「随机错位」，失去版面的规则感。

         错位量必须按「溢出量」算，不能按行宽百分比：长句 tw 是 W 的两倍时，
         -tw*0.72 直接把行首推到画外一整屏，首字母全被切（读作乱码而不是排版）。
         规则：偶数行行首保留留白，奇数行才往左压，且最多压 0.28W。 */
      const dir = r % 2 ? 1 : -1;
      /* 行宽已被收进画面，错位只做「轻微左右浮动」：0.03W 以内，
         既读得出跑马灯，又不会切掉任何一个词首。 */
      const slack = Math.max(0, (W - tw) / 2);
      const x0 = (W - tw) / 2 + dir * Math.min(W * 0.03, slack);
      for (const q of g) { q.x += x0; q.y = top + r * lh; }
      out.push(...g);
    }
    return { glyphs: out, bbox: bboxOf(out), size, bleed: true };
  };

  /* ⑦ 边缘延伸：基线贴住左右边界并向外溢出，读作「句子从画外伸进来」 */
  L.edge = (ctx, gs, st) => {
    const size = st.size || Math.round(H * 0.19);
    const g = PV.buildGlyphs(ctx, st.line, Object.assign({}, st, { size }));
    const tw = PV.glyphWidth(g);
    const left = (st.side || 'left') === 'left';
    const x0 = left ? -tw * (st.bleed || 0.22) : W - tw * (1 - (st.bleed || 0.22));
    const y = cyOf(st, 0.55);
    for (const q of g) { q.x += x0; q.y = y; }
    return { glyphs: g, bbox: bboxOf(g), size, bleed: true };
  };

  /* ⑧ 斜排：整行倾斜，适合「情绪开始上扬」的过渡段 */
  L.diagonal = (ctx, gs, st) => {
    const ang = (st.angle || -9) * Math.PI / 180;
    let size = st.size || Math.round(H * 0.15);
    // 旋转后的水平投影 = tw·cos + 字高·sin，按它收字号
    const w100 = PV.measure(ctx, st.text != null ? st.text : st.line.text, st.face || 'sans', 100)
      + (st.track || 0) * 100;
    const proj100 = Math.abs(w100 * Math.cos(ang)) + 100 * 1.2 * Math.abs(Math.sin(ang));
    if (proj100 > 0) size = Math.min(size, (W * (st.availW || 0.94) / proj100) * 100);
    size = Math.max(24, Math.round(size));
    const g = PV.buildGlyphs(ctx, st.line, Object.assign({}, st, { size }));
    const tw = PV.glyphWidth(g);
    const x0 = W / 2 - tw / 2, y0 = H * 0.54;
    for (const q of g) {
      const ox = q.x - x0;
      q.x = x0 + Math.cos(ang) * ox;
      q.y = y0 + Math.sin(ang) * ox;
      q.rot = ang;
    }
    return { glyphs: g, bbox: bboxOf(g), size };
  };

  /* ⑨ 角落小字：最安静的一档，用于主歌弱句 */
  L.corner = (ctx, gs, st) => {
    const size = st.size || 62;
    const g = PV.buildGlyphs(ctx, st.line, Object.assign({}, st, { size }));
    const tw = PV.glyphWidth(g);
    const pos = st.pos || 'bl';
    const x = pos === 'bl' || pos === 'tl' ? W * 0.08 : W * 0.92 - tw;
    const y = pos === 'bl' || pos === 'br' ? H * 0.80 : H * 0.26;
    for (const q of g) { q.x += x; q.y = y; }
    return { glyphs: g, bbox: bboxOf(g), size };
  };

  /* ⑩ 极限裁切：字号远大于画幅，只露出中间几个字母（副歌最狠的一招） */
  L.megaCrop = (ctx, gs, st) => {
    const size = st.size || Math.round(H * 0.78);
    const g = PV.buildGlyphs(ctx, st.line, Object.assign({}, st, { size }));
    const tw = PV.glyphWidth(g);
    const focus = st.focus == null ? 0.5 : st.focus;
    const x0 = W * 0.5 - tw * focus;
    const y = cyOf(st, 0.70);
    for (const q of g) { q.x += x0; q.y = y; }
    return { glyphs: g, bbox: bboxOf(g), size, bleed: true };
  };

  /* ⑪ 横带：只见一条水平带里的字，其余被遮（用于转场前后） */
  L.band = (ctx, gs, st) => {
    // 横带同样按可用宽度收：300px 的长句会顶到画面两侧，字挤成一团
    const availW = W * (st.availW || 0.88);
    const txt0 = st.text != null ? st.text : st.line.text;
    const w100 = PV.measure(ctx, txt0, st.face || 'sans', 100) + (st.track || 0) * 100 * txt0.length;
    let size = st.size || Math.round(H * 0.16);
    if (w100 > 0) size = Math.min(size, (availW / w100) * 100);
    size = Math.max(24, Math.round(size));
    const g = PV.buildGlyphs(ctx, st.line, Object.assign({}, st, { size }));
    const tw = PV.glyphWidth(g);
    const x0 = W / 2 - tw / 2;
    const y = cyOf(st, 0.52);
    for (const q of g) { q.x += x0; q.y = y; }
    return { glyphs: g, bbox: bboxOf(g), size, band: st.band || [y - size * 0.8, y + size * 0.3] };
  };

  /* ⑫ 两端对齐的字块：按目标宽度拉开字距，读作「海报」
     铁律：对齐只能「拉开」，不能「压紧」。自然宽度超过目标宽度时必须缩字号，
     否则 (targetW-tw)/(n-1) 会算出几十像素的负字距，整行字互相压成一团。

     长句必须折行：51 个字符按 0.84W 塞成一行，字号会掉到 62px（1080p 下就是一条
     小字幕，观感立刻回到卡拉OK）。判据取 maxSize*0.74 —— 低于它读作「字变小了」
     而不是「排版收紧」。折行按词边界二分，每行各自两端对齐。 */

  /* 把行切成「词 / 空隙」序列，词保留自己的时间轴，供折行后仍能逐词同步 */
  function splitTokens(line) {
    const text = line.text || '';
    const words = (line.words && line.words.length) ? line.words : null;
    if (!words || words.length < 3) return null;
    const toks = [];
    let p = 0;
    for (const w of words) {
      const i = text.indexOf(w.text, p);
      if (i < 0) return null;                 // 词与串对不上，宁可放弃折行
      if (i > p) toks.push({ text: text.slice(p, i), ws: true });
      toks.push({ text: w.text, w });
      p = i + w.text.length;
    }
    if (p < text.length) toks.push({ text: text.slice(p), ws: true });
    return toks;
  }

  /* 单行铺满：字形以 x=0 为中心，返回 {g, size}；y 由调用方决定。
     maxSpread 用于折行：两行都不该被拉成「字距等宽的海报」，否则单看一行会散架。 */
  function fillLine(ctx, line, st, text, words, size, track, targetW, maxSpread) {
    const sub = { text: text, t0: line.t0, t1: line.t1 };
    if (words) sub.words = words;
    const g = PV.buildGlyphs(ctx, sub, Object.assign({}, st, { size, track, text }));
    const n = g.filter(q => !q.isSpace).length;
    const tw = PV.glyphWidth(g);
    let spread = n > 1 ? Math.max(0, (targetW - tw) / (n - 1)) : 0;
    if (maxSpread != null && spread > maxSpread) spread = maxSpread;
    let k = 0;
    const x0 = -(tw + spread * (n - 1)) / 2;
    for (const q of g) { q.x += x0 + spread * k; if (!q.isSpace) k++; }
    return { g, tw, n, spread };
  }

  const sizeFor = (ctx, text, face, track, maxSize, targetW) => {
    const w100 = PV.measure(ctx, text, face, 100) + track * 100 * text.length;
    if (w100 <= 0) return maxSize;
    const nat = w100 * maxSize / 100;
    return nat > targetW ? maxSize * (targetW / nat) : maxSize;
  };

  L.justify = (ctx, gs, st) => {
    const targetW = st.targetW || W * 0.84;
    const face = st.face || 'sans';
    const track = st.track || 0;
    const txt = st.text != null ? st.text : st.line.text;
    const maxSize = st.size || st.maxSize || 132;
    const cy = cyOf(st, 0.52);
    const size = sizeFor(ctx, txt, face, track, maxSize, targetW);

    if (size >= maxSize * 0.74) {
      const r = fillLine(ctx, st.line, st, txt, st.line.words, size, track, targetW);
      for (const q of r.g) { q.x += W / 2; q.y = cy; }
      return { glyphs: r.g, bbox: bboxOf(r.g), size };
    }

    /* 折两行：只在词边界切。评分 = 两行宽度差，外加「两行词数都不能少于总数的
       30%」的硬约束 —— 只看宽度差会把 7 个词切成 2/5（"…the little | things …"），
       读起来像把一个词拆到下一行。 */
    const toks = splitTokens(st.line);
    if (!toks) {                                     // 切不了就退回单行（缩字号总比溢出好）
      const r = fillLine(ctx, st.line, st, txt, st.line.words, size, track, targetW);
      for (const q of r.g) { q.x += W / 2; q.y = cy; }
      return { glyphs: r.g, bbox: bboxOf(r.g), size };
    }
    const nw = st.line.words.length;
    let cut = -1, best = Infinity;
    for (let i = 0; i < toks.length - 1; i++) {
      if (toks[i].ws) continue;
      const na = toks.slice(0, i + 1).filter(t => t.w).length;
      const nb = nw - na;
      if (na < nw * 0.3 || nb < nw * 0.3) continue;
      const a = toks.slice(0, i + 1).map(t => t.text).join('').trim();
      const b = toks.slice(i + 1).map(t => t.text).join('').trim();
      if (!a || !b) continue;
      const wa = PV.measure(ctx, a, face, 100), wb = PV.measure(ctx, b, face, 100);
      const sc = Math.abs(wa - wb);
      if (sc < best) { best = sc; cut = i; }
    }
    if (cut < 0) {
      const r = fillLine(ctx, st.line, st, txt, st.line.words, size, track, targetW);
      for (const q of r.g) { q.x += W / 2; q.y = cy; }
      return { glyphs: r.g, bbox: bboxOf(r.g), size };
    }
    const ta = toks.slice(0, cut + 1), tb = toks.slice(cut + 1);
    const lineA = { text: ta.map(t => t.text).join('').trim(), words: ta.filter(t => t.w).map(t => t.w), t0: st.line.t0, t1: st.line.t1 };
    const lineB = { text: tb.map(t => t.text).join('').trim(), words: tb.filter(t => t.w).map(t => t.w), t0: st.line.t0, t1: st.line.t1 };
    const szA = sizeFor(ctx, lineA.text, face, track, maxSize, targetW);
    const szB = sizeFor(ctx, lineB.text, face, track, maxSize, targetW);
    const sz = Math.min(szA, szB, maxSize);
    const ms = sz * 0.30;
    const ra = fillLine(ctx, st.line, st, lineA.text, lineA.words, sz, track, targetW, ms);
    const rb = fillLine(ctx, st.line, st, lineB.text, lineB.words, sz, track, targetW, ms);
    const lh = sz * 1.18;
    const all = [];
    for (const q of ra.g) { q.x += W / 2; q.y = cy - lh / 2; all.push(q); }
    for (const q of rb.g) { q.x += W / 2; q.y = cy + lh / 2; all.push(q); }
    return { glyphs: all, bbox: bboxOf(all), size: sz };
  };

  /* ⑬ 大小混排（大小混植）：日本海报最常见的一招 —— 同一句里挑一个词放大，
     其余的词缩到半号，基线共用。观众先读到「大词」，再读到整句。
     这不是「强调」的替代品：强调是换样式，混排是换**字号秩序**，
     所以它天然带有版面的说服力，且不依赖任何特效。

     约束：大词只能是 1 个（两个大词 = 没有大词）；总宽超了整体等比缩，
     绝不允许某一词单独缩 —— 那会把「秩序」变成「参差」。 */
  L.mixed = (ctx, gs, st) => {
    const words = (st.line.words && st.line.words.length)
      ? st.line.words.map(w => w.text)
      : String(st.text || st.line.text || '').split(' ').filter(Boolean);
    if (!words.length) return L.center(ctx, gs, st);

    const maxSize = st.size || 168;
    const ratio = st.ratio == null ? 0.52 : st.ratio;
    // 大词：默认取最长的一个（信息量最大），也可由导演层指定
    let key = st.key != null ? st.key : 0;
    if (st.key == null) {
      let best = -1;
      words.forEach((w, i) => { if (w.length > best) { best = w.length; key = i; } });
    }
    const gapEm = st.wordGap == null ? 0.30 : st.wordGap;
    const availW = W * (st.availW || 0.84);

    const build = (size) => {
      const out = [];
      let x = 0;
      words.forEach((txt, k) => {
        const sz = (k === key ? size : Math.max(18, size * ratio));
        const g = PV.buildGlyphs(ctx, st.line, Object.assign({}, st, { size: sz, text: txt }));
        const wref = st.line.words && st.line.words[k];
        for (const q of g) {
          // 逐词时间：折出来的子串没有自己的词条时退回原行的
          if (wref) { q.t0 = wref.t0; q.t1 = wref.t1; }
          q.wi = k;
          q.x += x; q.y = 0;
        }
        out.push(...g);
        x += PV.glyphWidth(g) + size * gapEm;
      });
      return { glyphs: out, width: x - size * gapEm };
    };

    let size = maxSize;
    let r = build(size);
    if (r.width > availW) {                       // 整体等比缩，保住大词与小词的比例
      size = Math.floor(size * (availW / r.width));
      r = build(size);
    }
    const cy = cyOf(st, 0.52);
    const left = st.align === 'left' ? xOf(st, 0.085) : W / 2 - r.width / 2;
    for (const q of r.glyphs) { q.x += left; q.y = cy; }
    return { glyphs: r.glyphs, bbox: bboxOf(r.glyphs), size, key };
  };

  PV.bboxOf = bboxOf;

  /* ---------------- 字体预热 ----------------
     必须逐字重 load 一次，否则首帧用 fallback、后面变字形 ⇒ 同一帧两次渲染不同。 */
  PV.warmFonts = async (texts, sizes) => {
    const list = [];
    for (const k of Object.keys(PV.FACES)) {
      for (const s of sizes || [40, 120, 240]) list.push(document.fonts.load(PV.cssFace(k, s), texts || 'A字あ'));
    }
    try { await Promise.all(list); } catch (e) { /* 缺字不阻断，交给 fallback */ }
    return Array.from(document.fonts).filter(f => f.status === 'loaded').length;
  };
})();
