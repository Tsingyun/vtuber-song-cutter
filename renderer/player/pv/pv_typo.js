/*! pv_typo.js —— 版式目录（JIZURA 语法）
 *
 *  上一版的构图语言是「把一句话摆在画面里」：居中、堆叠、密排墙、极限裁切。
 *  它的问题不是不好看，而是**没有版面**——没有边距、没有索引、没有规则线、
 *  没有「这个字为什么在这里」的理由。看久了就是一张会动的字幕。
 *
 *  JIZURA 的版式语言（src/11p_typo1.js、11p_layoutsA.js）恰恰相反：
 *    ① **每个构图都是一张完整的版面**，不只是字：规则线、索引号、注记、
 *       色块遮带、目盛，都是构图的一部分，由 layout 自己画；
 *    ② **字很少，留白很多**。一句话往往只占画面 30~45%，其余是空的；
 *    ③ **尺寸是断层的**，不是渐变的：一个词 0.55H，其余 0.26 倍，注记 0.023U。
 *       中间没有过渡级——这正是「版面感」和「PPT 感」的分界；
 *    ④ **索引与注记是装饰的主体**：`01 / 12`、`No.07`、`SEC.B`、目盛数字。
 *
 *  所以本文件不是「若干种居中方式」，而是 12 个**版面**。
 *  每个版面返回 { glyphs, bbox, size, marks }，marks 由 pv_engine 在字之前/之后画。
 */
(() => {
  'use strict';
  const PV = window.PV;
  const W = PV.W, H = PV.H, U = Math.min(W, H);
  const L = (PV.LAYOUT_NEXT = {});

  PV.DEFAULT_PAL = PV.DEFAULT_PAL || PV.STYLE('noir', 0);

  /* ---------------- 版面常量（全部取自 JIZURA 的比例） ---------------- */
  const M = () => W * 0.075;                      // 版心边距 144px @1920
  const HAIR = () => Math.max(1.4, U * 0.0015);    // 细线 1.6px
  const LAB = () => PV.clamp(U * 0.023, 13, 32);   // 注记字号 25px（也用作「一个单位」）
  const DEG = Math.PI / 180;

  /* 排版宽度反解：measure 是线性的，量一次 100px 再按比例缩放 */
  function fitW(ctx, text, face, availW, maxSize, track) {
    const n = [...String(text || '')].length;
    if (!n) return maxSize;
    const w1 = PV.measure(ctx, text, face, 100) / 100 + (track || 0) * Math.max(0, n - 1);
    if (!(w1 > 1e-4)) return maxSize;
    return Math.max(8, Math.min(maxSize, availW / w1));
  }
  const words = line => (line.words && line.words.length)
    ? line.words.map((w, i) => ({ t: w.text, t0: w.t0, t1: w.t1, i }))
    : [{ t: line.text, t0: line.t0, t1: line.t1, i: 0 }];

  /* 一行词 → 字形。x 从 0 起，逐词带自己的时间轴。 */
  function row(ctx, st, ws, size) {
    const out = [];
    const sp = PV.measure(ctx, ' ', st.face, size);
    let x = 0;
    ws.forEach((w, k) => {
      /* 汉字之间**不插空格宽**：中文的词距不是空格，是字形自带边距 + 设计的 track。
         插了那个空格，一行十来个字就被拉开成「分散对齐」，宋体的横细竖粗全被吃掉
         （2026-10-31 实测：画心样片整行读作 PPT 而非法帖）。拉丁照旧。 */
      if (k) x += (isCJKUnit(ws[k - 1]) && isCJKUnit(w)) ? 0 : sp * (1 + (st.track || 0));
      const g = PV.buildGlyphs(ctx, { text: w.t, words: [{ text: w.t, t0: w.t0, t1: w.t1 }] },
        Object.assign({}, st, { size, text: w.t }));
      for (const q of g) { q.x += x; q.wi = w.i; }
      out.push(...g);
      x += PV.glyphWidth(g);
    });
    return { g: out, w: x };
  }
  const shift = (gs, dx, dy) => { for (const q of gs) { q.x += dx; q.y += dy; } return gs; };

  /* 词宽必须和「真正画出来的宽度」同口径。
     原来这里用 PV.measure(t) + track*(n-1) 估，而绘制走 PV.glyphWidth(buildGlyphs) ——
     后者含 opticalTighten 与逐字字距，两套数不相等。结果是 wrap 以为一行 1267、
     实际画出 1396，长句整行捅到人物身上（i=44 压了 145px）。
     现在统一用 row() 量：排一次版，多量几十次测量，换掉满屏溢出。 */
  /* ---------------- 中文断行（2026-10-31） ----------------
     中文按字成「词元」（pv_timing.split_units 一个汉字一个单元），走拉丁那套 wrap 会
     出两个一眼假的问题：
       ① 每个字之间插一个**空格宽**（拉丁的词距），整行看上去像被拉开的字距 ——
          宋体的横细竖粗全被这点空吃掉，「印刷感」变成「PPT 分散对齐」；
       ② 到边界就断，不管断在哪个字上（实测「看着你抱着我目 / 光似月色」）。
     所以中文单独一条：字距交给 track（设计量），断行做「避头尾」——行首不落
     的/了/着/地/得 这类收尾字，行尾不落在/是/和/与 这类必须黏后字的虚词。 */
  const CJK_CH = /[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]/;
  const NO_START = '的了着过地得们呢吧啊呀嘛哦';
  const NO_END = '在是和与就也都而把被给让向从对最很太不没无一旦并或：:、，。';
  const isCJKUnit = q => typeof q.t === 'string' && q.t.length === 1 && CJK_CH.test(q.t);

  /* 避头尾：逐断点修，最多两轮（一次挪动会改变下一处的位置）。
     行首不许落「的/了/着…」这类收尾字，行尾不许落「在/是/和/与…」这类必须黏后字的
     虚词 —— 中文排版的禁则，也是「这一版排过」与「这一版没排过」的分界。 */
  function kinsoku(rows) {
    for (let pass = 0; pass < 2; pass++) {
      for (let k = 0; k < rows.length - 1; k++) {
        const A = rows[k], B = rows[k + 1];
        if (!A.length || !B.length) continue;
        const head = B[0].t, tail = A[A.length - 1].t;
        if (NO_START.includes(head) && A.length > 1) {
          B.unshift(A.pop());
        } else if (NO_END.includes(tail) && B.length > 1) {
          A.push(B.shift());
        }
      }
    }
  }

  function wrapCJK(ctx, ws, face, size, track, maxW) {
    const st0 = { face, track: track || 0, squeeze: 1 };
    const wcache = new Map();
    const wOf = q => {
      const k = q.t;
      if (!wcache.has(k)) wcache.set(k, PV.glyphWidth(row(ctx, st0, [q], size).g));
      return wcache.get(k);
    };
    const rows = [];
    let cur = [], w = 0;
    for (const q of ws) {
      const ww = wOf(q);
      if (cur.length && w + ww > maxW) { rows.push(cur); cur = []; w = 0; }
      w += ww;
      cur.push(q);
    }
    if (cur.length) rows.push(cur);
    if (rows.length < 2) return rows;
    kinsoku(rows);
    /* 均衡：两行时把宽度差压到 1.3 倍以内，否则读作「上面挤、下面空」。
       只在两行、且两行都是中文的情况下做，三行以上交给原 greedy。 */
    if (rows.length === 2) {
      const wsum = r => r.reduce((a, q) => a + wOf(q), 0);
      let guard = 0;
      while (guard++ < 4) {
        const a = wsum(rows[0]), b = wsum(rows[1]);
        if (a <= b * 1.30 || rows[0].length < 3) break;
        rows[1].unshift(rows[0].pop());
      }
    }
    return rows.filter(r => r.length);
  }

  function wrap(ctx, ws, face, size, track, maxW) {
    if (ws.length > 2 && ws.every(isCJKUnit)) return wrapCJK(ctx, ws, face, size, track, maxW);
    const st0 = { face, track: track || 0, squeeze: 1 };
    const wcache = new Map();
    const wOf = q => {
      const k = q.t;
      if (!wcache.has(k)) wcache.set(k, PV.glyphWidth(row(ctx, st0, [q], size).g));
      return wcache.get(k);
    };
    const sp = PV.measure(ctx, ' ', face, size) * (1 + (track || 0));
    const rows = [];
    let cur = [], w = 0;
    for (const q of ws) {
      const ww = wOf(q);
      if (cur.length && w + sp + ww > maxW) { rows.push(cur); cur = []; w = 0; }
      w += (cur.length ? sp : 0) + ww;
      cur.push(q);
    }
    if (cur.length) rows.push(cur);
    /* 孤字回收：末行只剩一个词时，把它上一行的末词挪下来。
       「And I'll hold on to / it」这种断句一眼就是没排过版 —— JIZURA 任何版面都不会这样断。 */
    if (rows.length > 1 && rows[rows.length - 1].length === 1 && rows[rows.length - 2].length > 2) {
      rows[rows.length - 1].unshift(rows[rows.length - 2].pop());
    }
    return rows;
  }

  /* 均衡折行（Knuth 断行的简化版）。
     贪心会排出「Waking up I see / that everything / is ok」这种末行只剩两个字的断法 ——
     一眼就是没排过版。这里用「每行剩余空白的平方和」当代价，让各行宽度互相靠近；
     末行不计罚（末行本来就该短）。词数 ≤ 20，O(n³) 无压力。 */
  function wrapOpt(ctx, ws, face, size, track, maxW, maxRows) {
    const n = ws.length;
    if (!n) return [];
    const st0 = { face, track: track || 0, squeeze: 1 };
    const cache = new Map();
    const wOf = q => {
      const k = q.t;
      if (!cache.has(k)) cache.set(k, PV.glyphWidth(row(ctx, st0, [q], size).g));
      return cache.get(k);
    };
    /* 行宽直接量「整行画出来有多宽」，不累加单个词。
       逐词累加忽略了词与词之间的排布细节，累计误差能让 1267 的行画出 1450 ——
       于是上限判定形同虚设，折行怎么算都不对。 */
    const lwCache = new Map();
    const lineW = (i, j) => {
      const k = i + ':' + j;
      if (!lwCache.has(k)) lwCache.set(k, PV.glyphWidth(row(ctx, st0, ws.slice(i, j + 1), size).g));
      return lwCache.get(k);
    };
    const lim = Math.max(1, Math.min(maxRows || n, n));
    const INF = Infinity;
    const cost = [], prev = [];
    for (let r = 0; r <= lim; r++) { cost.push(new Array(n + 1).fill(INF)); prev.push(new Array(n + 1).fill(-1)); }
    cost[0][0] = 0;
    for (let r = 1; r <= lim; r++) {
      for (let j = r; j <= n; j++) {
        for (let i = r - 1; i < j; i++) {
          if (cost[r - 1][i] === INF) continue;
          const lw = lineW(i, j - 1);
          if (lw > maxW * 1.004) continue;
          const slack = maxW - lw;
          /* 末行同样计罚，只给 12% 的免罚额度（末行本来就该短一点）。
             完全不罚的话末行会变成垃圾桶：「Waking up I see / that everything is / ok」
             里那个两字符的 ok 就是这么来的 —— 前两行满宽、末行 354px，读作断错了。 */
          const sk = j === n ? Math.max(0, slack - maxW * 0.12) : slack;
          const c = cost[r - 1][i] + sk * sk;
          if (c < cost[r][j]) { cost[r][j] = c; prev[r][j] = i; }
        }
      }
    }
    let br = -1, bc = INF;
    for (let r = 1; r <= lim; r++) if (cost[r][n] < bc) { bc = cost[r][n]; br = r; }
    if (br < 0) return wrap(ctx, ws, face, size, track, maxW);
    const rows = [];
    let j = n, r = br;
    while (j > 0 && r > 0) { const i = prev[r][j]; if (i < 0) break; rows.unshift(ws.slice(i, j)); j = i; r--; }
    if (rows.length > 1 && ws.every(isCJKUnit)) kinsoku(rows);
    return rows.length ? rows : wrap(ctx, ws, face, size, track, maxW);
  }

  /* ---------------- 装饰标记（marks） ----------------
     坐标是版面绝对像素。engine 在字形之前画 under，之后画 over。
     `at`/`dur` 是镜头内相对秒：线从 0 长出来、标签晚 0.2s 淡入 —— 这是 JIZURA 的
     「信息逐条落下」的节奏，也是它不显得生硬的原因之一（没有东西是同时出现的）。 */
  const mk = (o) => Object.assign({ at: 0.06, dur: 0.55, a: 1 }, o);
  const rule = (x0, y0, x1, y1, col, o) => mk(Object.assign({ k: 'rule', x0, y0, x1, y1, col, w: HAIR() }, o || {}));
  const label = (x, y, text, size, col, o) => mk(Object.assign({ k: 'label', x, y, text, size, col }, o || {}));
  const plate = (x, y, w, h, col, o) => mk(Object.assign({ k: 'rect', x, y, w, h, col }, o || {}));
  const num = (x, y, n, size, col, o) => label(x, y, String(n).padStart(2, '0'), size, col, o);

  /* =====================================================================
     1  titleCard —— 标题卡（片头整段前奏）
     版面：左上注记 + 主标题顶左 + 一条 accent 短线 + 三行档案字 + 右侧巨大暗影
     ===================================================================== */
  L.titleCard = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.face || 'heavy';
    const title = String(st.text || st.line.text || '').toUpperCase();
    const m = M(), lab = LAB();
    const maxW = W * 0.82;
    const size = Math.round(fitW(ctx, title, face, maxW, H * 0.20, st.track == null ? -0.02 : st.track));
    const ws = [{ t: title, t0: st.line.t0, t1: st.line.t1, i: 0 }];
    const r = row(ctx, Object.assign({}, st, { face }), ws, size);
    const capH = size * 0.72;
    const baseY = H * 0.50;

    /* 右侧巨大暗影：整首曲名竖排、贴右缘被裁掉 —— JIZURA 的 cropGiant 在标题卡的用法。
       它给画面重量，但完全不抢主标题（dim 只比底色亮一档）。 */
    const marks = [];
    let giant = null;
    if (st.shadow !== false) {
      const gs2 = PV.buildGlyphs(ctx, { text: title, words: [{ text: title, t0: st.line.t0, t1: st.line.t1 }] },
        { text: title, face, size: Math.round(H * 0.30), track: 0.02, squeeze: 1 });
      const chars = gs2.filter(q => !q.isSpace);
      const lh = H * 0.30 * 1.02;
      const per = Math.max(1, Math.floor((H * 0.92) / lh));
      chars.forEach((q, k) => {
        q.x = W - M() * 0.55 - Math.floor(k / per) * H * 0.30 * 1.10;
        q.y = H * 0.06 + (k % per) * lh + H * 0.30 * 0.78;
      });
      for (const q of chars) { q.alpha = 1; }
      giant = { glyphs: chars, col: p.dim, alpha: st.shadowA == null ? 0.9 : st.shadowA };
    }

    shift(r.g, m, baseY);
    marks.push(rule(m, baseY + size * 0.34, m + W * 0.30, baseY + size * 0.34, p.accent, { dur: 0.75, at: 0.30, w: Math.max(3, HAIR() * 2) }));
    /* kicker 必须留出「标题 cap 顶」到它之间的空气：
       原来钉在 0.335H，而 0.20H 的标题 cap 顶正好落在 0.30H —— 两行叠在一起，
       读作「字压字」而不是「刊头 + 标题」。 */
    const kickY = Math.min(H * 0.30, baseY - size * 0.92);
    marks.push(label(m + lab * 1.5, kickY, (st.kicker || 'ORIGINAL SONG'), lab, p.sub, { track: 0.24, at: 0.18 }));
    marks.push(plate(m, kickY - lab * 0.60, lab * 0.42, lab * 0.42, p.accent, { at: 0.18, dur: 0.3 }));
    if (st.artist) marks.push(label(m, baseY + size * 0.34 + lab * 2.4, st.artist, lab * 1.55, p.fg,
      { at: 0.55, face: /[\u3400-\u9fff]/.test(String(st.artist)) ? 'cnSerifLt' : null }));
    if (st.meta) {
      marks.push(label(m, baseY + size * 0.34 + lab * 4.5, st.meta, lab * 0.94, p.sub, { track: 0.16, at: 0.75 }));
    }
    const bb = PV.bboxOf(r.g);
    return { glyphs: r.g, bbox: bb, size, marks, giant, bleed: true };
  };

  /* =====================================================================
     2  keySplit —— 大字挟み（一句话里挑一个词撑到 0.5H，其余缩到 0.26 倍）
     JIZURA tyKeySplit：大字居中，前后小字分列两侧，细线相连，下方 04 / 12 索引
     ===================================================================== */
  L.keySplit = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.face || 'heavy';
    const ws = words(st.line);
    if (ws.length < 2) return L.block(ctx, gs, st);
    let ki = st.key == null ? 0 : (st.key | 0);
    ki = Math.max(0, Math.min(ws.length - 1, ki));
    const bigT = ws[ki].t;
    const before = ws.slice(0, ki), after = ws.slice(ki + 1);

    let bigS = Math.round(Math.min(H * 0.56, fitW(ctx, bigT, face, W * 0.54, H, st.bigTrack == null ? -0.03 : st.bigTrack)));
    const big = row(ctx, { face, track: st.bigTrack == null ? -0.03 : st.bigTrack, squeeze: 1 }, [ws[ki]], bigS);
    const bw = big.w;
    const cx = W * (st.cx == null ? 0.5 : st.cx), baseY = H * 0.575;
    shift(big.g, cx - bw / 2, baseY);

    /* 小字：JIZURA 把前半句压在左、后半句压在右，各占大字两侧的空隙。
       尺寸固定 0.27 倍 —— 这个比例是「读得清但绝不抢」，改到 0.4 就变成两句话打架。 */
    const sS = Math.round(PV.clamp(bigS * 0.27, 26, H * 0.14));
    const gap = bigS * 0.16;
    const availL = cx - bw / 2 - gap - M();
    const availR = W - M() - (cx + bw / 2 + gap);
    const out = big.g;
    const marks = [];
    const topY = baseY - bigS * 0.40, botY = baseY + bigS * 0.18;

    const put = (grp, x, y, alignRight) => {
      if (!grp.length) return null;
      const t = grp.map(q => q.t).join(' ');
      const size = Math.round(Math.min(sS, fitW(ctx, t, st.body || 'sansMed', Math.max(availL, availR) * 1.6, sS, 0.03)));
      const rws = wrap(ctx, grp, st.body || 'sansMed', size, 0.03, Math.max(availL, availR));
      let yy = y;
      for (const rw of rws) {
        const r2 = row(ctx, { face: st.body || 'sansMed', track: 0.03, squeeze: 1 }, rw, size);
        shift(r2.g, alignRight ? x - r2.w : x, yy);
        for (const q of r2.g) { q.rowGap = size * 1.2; }
        out.push(...r2.g);
        yy += size * 1.24;
      }
      return { size, rows: rws.length, h: yy - y - size * 1.24 + size };
    };

    const Lb = before.length ? put(before, cx - bw / 2 - gap, topY, true) : null;
    const Ab = after.length ? put(after, cx + bw / 2 + gap, botY, false) : null;

    /* 细线把两块小字和大字连起来（JIZURA 的 rule 就是这个作用：
       不是装饰，是「这两块属于同一句」的句法标记） */
    /* 连接线要落在**小字块的中线**上，不是落在它的基线下面。
       落在下面就读作「下划线」，而 JIZURA 的这条线是句法标记（这两块属于同一句）。 */
    if (Lb) {
      const ly = topY + (Lb.rows - 1) * Lb.size * 1.24 + Lb.size * 0.34;
      marks.push(rule(cx - bw / 2 - gap - Math.min(bw * 0.30, W * 0.10), ly, cx - bw / 2 - gap * 0.4, ly, p.sub, { a: 0.5, dur: 0.6 }));
    }
    if (Ab) {
      const ly = botY + (Ab.rows - 1) * Ab.size * 1.24 + Ab.size * 0.34;
      marks.push(rule(cx + bw / 2 + gap * 0.4, ly, cx + bw / 2 + gap + Math.min(bw * 0.30, W * 0.10), ly, p.sub, { a: 0.5, dur: 0.6 }));
    }

    /* 索引：`04 / 12` —— 词的位置。JIZURA 每句都挂这个，它是「版面感」的一半 */
    marks.push(label(cx - bw / 2, baseY + bigS * 0.30, pad2(ki + 1) + ' / ' + pad2(ws.length), LAB() * 0.92, p.sub, { at: 0.30, track: 0.14 }));

    return { glyphs: out, bbox: PV.bboxOf(out), size: bigS, marks, bleed: true };
  };
  const pad2 = n => String(n).padStart(2, '0');

  /* =====================================================================
     3  cropGiant —— 見切れ大文字（巨型副本被画框切掉，清晰的一行让到另一侧）
     ===================================================================== */
  L.cropGiant = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.face || 'heavy';
    const txt = String(st.text || st.line.text || '').trim();
    const low = (st.edge || 'b') === 'b';
    /* 字号上限压到 0.44H：再大就只剩几个认不出的字母残片，抢掉可读块的主体地位。
       下限定 0.30H —— 小于这个尺寸就读作「背景纹理」，浪费一次版面。 */
    const G = Math.round(H * PV.clamp(st.giantK || 0.44, 0.30, 0.44));
    const marks = [];
    const out = [];

    /* 巨型副本：单行，越界就让它越界（这是设计，不是 bug）。
       颜色用 dim（只比底色亮一档）—— 用 fg 加透明度会得到「灰」，而 dim 是「压进纸里」。 */
    const gBig = row(ctx, { face, track: -0.02, squeeze: 1 }, [{ t: txt, t0: st.line.t0, t1: st.line.t1, i: 0 }], G);
    /* 横向：从左侧版心起排、向右溢出。居中的话两边同时被切，
       画面里只剩中间两三个字母 —— 读作噪点而不是「巨大的同一句话」。 */
    const gx = M() * 0.5 + (st.dx == null ? 0 : st.dx * W);
    /* 座位：巨影必须有一部分露在画外（这才是「見切れ」），但不能整块跑出画。
       字形实际占 baseline-0.78G ~ baseline+0.10G，于是：
         坐下缘 → baseline = H + 0.14G（脚下切掉约 0.24G）
         坐上缘 → baseline = 0.46G   （头上切掉约 0.32G）
       这里一度写成 ±0.02G：整块巨影落在画外，画面只剩一条淡淡的地平线。 */
    const gy = low ? H + G * 0.14 : G * 0.46;
    shift(gBig.g, gx, gy);
    /* 巨影一律用 dim（只比底色亮一档）实心画 —— 用 fg 加透明度得到的是「灰」，
       而 dim 是「压进纸里」。outline 只降一点 alpha，不要换成描边：
       drawGiant 走的是 fillText，描边分支从来没生效过。 */
    const giant = { glyphs: gBig.g, col: p.dim, alpha: st.mode === 'outline' ? 0.82 : 1 };

    /* 清晰的一行：让到巨型字的另一侧 */
    const maxLines = st.lines || 2;
    const availW = W * (st.availW || 0.56);
    let size = Math.round(Math.min(H * 0.196, fitW(ctx, txt, st.body || 'sansMed', availW, H)));
    let rws = wrap(ctx, words(st.line), st.body || 'sansMed', size, st.track || 0.02, availW);
    if (rws.length > maxLines) {
      size = Math.round(size * 0.82);
      rws = wrap(ctx, words(st.line), st.body || 'sansMed', size, st.track || 0.02, availW);
    }
    const lh = size * 1.20;
    const blockH = (rws.length - 1) * lh + size;
    const side = st.side || (low ? 't' : 'b');
    let y0 = side === 't' ? H * 0.20 : H * 0.80 - blockH;
    const x0 = st.x != null ? W * st.x : M();
    rws.forEach((rw, k) => {
      const r2 = row(ctx, { face: st.body || 'sansMed', track: st.track || 0.02, squeeze: 1 }, rw, size);
      shift(r2.g, x0, y0 + k * lh + size * 0.80);
      for (const q of r2.g) { q.rowGap = lh; q.row = k; }
      out.push(...r2.g);
    });

    /* 索引 + 一条从索引长出去的细线：JIZURA 的 No.07 就是这个位置 */
    const lab = LAB();
    const ly = (side === 't' ? y0 : y0 - lab * 1.5) - lab * 0.4;
    marks.push(label(x0, ly, (st.tag || 'No.') + pad2((st.no || 0) + 1), lab, p.accent, { at: 0.18, track: 0.12 }));
    marks.push(rule(x0 + lab * 4.4, ly - lab * 0.34, x0 + lab * 4.4 + availW * 0.42, ly - lab * 0.34, p.sub, { a: 0.7, at: 0.22, dur: 0.7 }));
    return { glyphs: out, bbox: PV.bboxOf(out), size, marks, giant, bleed: true };
  };

  /* =====================================================================
     4  lowerThird —— 下部テロップ（JIZURA layoutsA #1）
     一条从画面边缘伸进版心的 accent 条 + 方块 + 注记，歌词压在条上方
     ===================================================================== */
  L.lowerThird = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.face || 'heavy';
    const txt = String(st.text || st.line.text || '').trim();
    const left = (st.side || 'l') !== 'r';
    /* m 是「版心边距」，不是「起排点」。导演层给的 x 是空侧边距 ——
       人在左时 x=0.36，可横带这时要从**右缘**伸进来，右边界仍是 M()。
       直接拿 x 当右边界会让整块字往左退到人身上。 */
    const m = (left && st.x != null) ? W * st.x : M();
    const marks = [], out = [];
    const availW = W * (st.availW || 0.78);
    let size = Math.round(Math.min(H * 0.19, fitW(ctx, txt, face, availW, H)));
    let rws = wrap(ctx, words(st.line), face, size, st.track || 0.01, availW);
    /* 最多两行：第三行会把 blockTop 顶到 0.3H 以上，那时它就不再是「下部テロップ」，
       而是一块悬在画面中间的怪东西。宁可缩字号，也不许三行。 */
    for (let k = 0; k < 4 && rws.length > 2; k++) {
      size = Math.round(size * 0.80);
      rws = wrap(ctx, words(st.line), face, size, st.track || 0.01, availW);
    }
    const lh = size * 1.12;
    const barY = H * 0.845;
    const blockBot = barY - size * 0.30;
    const blockTop = blockBot - ((rws.length - 1) * lh + size * 0.76);
    rws.forEach((rw, k) => {
      const r2 = row(ctx, { face, track: st.track || 0.01, squeeze: 1 }, rw, size);
      const x = left ? m : W - m - r2.w;
      shift(r2.g, x, blockTop + k * lh + size * 0.76);
      for (const q of r2.g) { q.rowGap = lh; q.row = k; }
      out.push(...r2.g);
    });
    const widest = Math.max(...rws.map(rw => {
      const r3 = row(ctx, { face, track: st.track || 0.01, squeeze: 1 }, rw, size);
      return r3.w;
    }));
    const th = Math.max(4, size * 0.055);
    const reach = m + widest + size * 0.34;
    marks.push(plate(left ? 0 : W - reach, barY, reach, th, p.accent, { dur: 0.5, at: 0.10 }));
    const sq = th * 2.6, ex = left ? reach : W - reach;
    marks.push(plate(ex - sq / 2, barY + th / 2 - sq / 2, sq, sq, p.accent, { dur: 0.28, at: 0.34 }));
    const lab = LAB();
    const ly = blockTop - lab * 1.35;
    const tag = st.no != null ? pad2(st.no) : '';
    let cx = left ? m : W - m;
    if (st.label !== false) {
      marks.push(plate(left ? cx : cx - lab * 1.7, ly - lab * 0.62, lab * 1.7, lab * 1.7, p.inkBox, { a: 0.95, dur: 0.3, at: 0.30 }));
      marks.push(label(left ? cx + lab * 0.85 : cx - lab * 0.85, ly + lab * 0.10, tag, lab, PV.onCol(p, p.inkBox), { align: 'center', at: 0.34 }));
      cx += (lab * 2.4) * (left ? 1 : -1);
      marks.push(label(cx, ly + lab * 0.10, st.meta || '', lab, p.sub, { align: left ? 'left' : 'right', at: 0.44, track: 0.18 }));
    }
    return { glyphs: out, bbox: PV.bboxOf(out), size, marks, bleed: true };
  };

  /* =====================================================================
     5  cross —— 十字組（纵线 + 横线 + 圆，字压在轴心上）
     ===================================================================== */
  L.cross = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.face || 'heavy';
    const ws = words(st.line);
    const cx = W * (st.cx == null ? 0.5 : st.cx), cy = H * 0.52;
    const marks = [], out = [];
    const dark = st.dark !== false;

    /* 轴：细线 + 一个大圆。JIZURA 给的是 0.28 alpha 的十字 + 0.78 倍半径的圆 */
    /* 0.26 时横线正好穿过字的中宫，读作删除线；0.16 才退成「版面的轴」 */
    marks.push(rule(0, cy, W, cy, p.sub, { a: 0.16, dur: 0.9, at: 0.05 }));
    marks.push(rule(cx, 0, cx, H, p.sub, { a: 0.16, dur: 0.9, at: 0.05 }));
    marks.push(mk({ k: 'circle', cx, cy, r: H * 0.30, col: p.sub, a: 0.42, w: HAIR(), dur: 0.9, at: 0.12 }));

    /* 字：一句话沿水平轴排开，中心对齐轴心；第二行贴下方竖轴 */
    const availW = W * (st.availW || 0.80);
    let size = Math.round(Math.min(H * 0.22, fitW(ctx, ws.map(q => q.t).join(' '), face, availW, H)));
    let rws = wrap(ctx, ws, face, size, st.track || 0.02, availW);
    if (rws.length > 2) { size = Math.round(size * 0.72); rws = wrap(ctx, ws, face, size, st.track || 0.02, availW); }
    const lh = size * 1.10;
    rws.forEach((rw, k) => {
      const r2 = row(ctx, { face, track: st.track || 0.02, squeeze: 1 }, rw, size);
      const y = cy + (k - (rws.length - 1) / 2) * lh + size * 0.34;
      shift(r2.g, cx - r2.w / 2, y);
      for (const q of r2.g) { q.rowGap = lh; q.row = k; }
      out.push(...r2.g);
    });
    const lab = LAB();
    marks.push(label(cx + H * 0.30 * 0.72, cy - H * 0.30 * 0.72, pad2(st.no == null ? 1 : st.no) + '×' + pad2(ws.length), lab * 0.9, p.sub, { at: 0.40, track: 0.12 }));
    return { glyphs: out, bbox: PV.bboxOf(out), size, marks, bleed: true };
  };

  /* =====================================================================
     6  bandHide —— 帯隠れ（一条色带横过，把字「遮掉」一截）
     色带压在字上（over），并且从一侧滑入 —— 遮与露本身成为节奏
     ===================================================================== */
  L.bandHide = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.face || 'heavy';
    const ws = words(st.line);
    const txt = ws.map(q => q.t).join(' ');
    const cx = W / 2;
    const size = Math.round(Math.min(H * 0.26, fitW(ctx, txt, face, W * 0.88, H, st.track == null ? -0.02 : st.track)));
    let rws = wrap(ctx, ws, face, size, st.track == null ? -0.02 : st.track, W * 0.88);
    if (rws.length > 2) rws = [ws];
    const lh = size * 1.16;
    const out = [];
    const baseY = H * 0.52 - (rws.length - 1) * lh / 2;
    rws.forEach((rw, k) => {
      const r2 = row(ctx, { face, track: st.track == null ? -0.02 : st.track, squeeze: 1 }, rw, size);
      shift(r2.g, cx - r2.w / 2, baseY + k * lh + size * 0.34);
      for (const q of r2.g) { q.rowGap = lh; q.row = k; }
      out.push(...r2.g);
    });
    /* 帯隠れ：一条实色横带从一侧扫入。
       教训：第一版把带子压在**最后一行字上**（height 0.92×size），结果整词只剩顶部
       一条边，抽帧读作「字被切坏了」——JIZURA 的「隐」隐的是行间，不是把一个词吃掉。
       现在带子落在两行的行间（单行时落在基线下），高度 0.34×size：
       它把版面切成「上 / 下」两段，同时把标签带进来，字一个都不少。 */
    const marks = [];
    let bandCol = st.bandCol === 'fg' ? p.fg : (st.bandCol === 'ink' ? p.inkBox : p.accent);
    /* 带子的颜色不能和字同色。crimson/1 的 ink 就等于 fg（都是 #FF3D6E），
       同色带压在红字下缘，看起来像字被泡进底色里 —— 换成 accent2 才读得出「切开」。 */
    if (String(bandCol).toUpperCase() === String(p.fg).toUpperCase()) bandCol = p.accent2 || p.accent;
    const bandH = size * (st.bandK || 0.34);
    const lastBase = baseY + (rws.length - 1) * lh + size * 0.34;
    /* 单行时带的**顶边**贴在基线下 0.06em；原来写成 lastBase + 0.10size - bandH/2，
       减掉半个带高之后带顶跑到基线之上，把字底啃掉 27px。 */
    /* 间隙 0.16em：0.06em 会让 g / y / j 的尾巴被啃掉一截（带顶正好压在 descender 上）。 */
    const by = rws.length > 1 ? lastBase - lh * 0.52 - bandH / 2 : lastBase + size * 0.16;
    marks.push(mk({
      k: 'band', x: 0, y: by, w: W, h: bandH, col: bandCol,
      over: true, dir: st.dir == null ? 1 : st.dir, at: 0.16, dur: 0.52,
    }));
    /* 带上的反色注记：JIZURA 会在带上压一行反白小字，读作「标签」 */
    if (st.tag) {
      marks.push(mk({
        k: 'bandText', x: W * 0.075, y: by + bandH * 0.52, text: st.tag, size: LAB() * 0.86,
        col: PV.onCol(p, bandCol), over: true, at: 0.55, dur: 0.4,
      }));
    }
    return { glyphs: out, bbox: PV.bboxOf(out), size, marks, bleed: true };
  };

  /* =====================================================================
     7  indexTable —— 索引表（歌詞 + 右侧小小一张数据表）
     ===================================================================== */
  L.indexTable = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.face || 'mincho';
    const ws = words(st.line);
    const m = M();
    const marks = [], out = [];
    const right = (st.side || 'r') === 'r';
    const textW = W * 0.52;
    const size = Math.round(Math.min(H * 0.115, fitW(ctx, ws.map(q => q.t).join(' '), face, textW, H)));
    let rws = wrap(ctx, ws, face, size, st.track || 0.02, textW);
    if (rws.length > 4) rws = rws.slice(0, 4);
    const lh = size * 1.30;
    const y0 = H * 0.34;
    const x0 = right ? m : W - m - textW;
    rws.forEach((rw, k) => {
      const r2 = row(ctx, { face, track: st.track || 0.02, squeeze: 1 }, rw, size);
      shift(r2.g, x0, y0 + k * lh + size * 0.8);
      for (const q of r2.g) { q.rowGap = lh; q.row = k; }
      out.push(...r2.g);
    });
    /* 表：三行 KV + 细线。位置紧贴歌词块的另一侧 */
    const lab = LAB();
    /* 表宽取 9 个单位、行距 2.4 个单位：原来 8 个单位 / 行距 2.0 时，
       三行的细线几乎连成一片，读作「噪点」而不是「表」。 */
    const tw = lab * 9.0;
    const tx = right ? W - m - tw : m;
    const ty = H * 0.34;
    const rows = st.rows || [];
    rows.forEach((kv, k) => {
      const yy = ty + k * lab * 2.4;
      marks.push(label(tx, yy, kv[0], lab * 0.80, p.sub, { at: 0.30 + k * 0.1, track: 0.20 }));
      marks.push(label(tx + lab * 4.0, yy, kv[1], lab * 0.96, p.fg, { at: 0.36 + k * 0.1, track: 0.06 }));
      marks.push(rule(tx, yy + lab * 0.7, tx + tw, yy + lab * 0.7, p.sub, { a: 0.40, at: 0.34 + k * 0.1, dur: 0.4 }));
    });
    return { glyphs: out, bbox: PV.bboxOf(out), size, marks, bleed: true };
  };

  /* =====================================================================
     8  vRuler —— 目盛（竖轴刻度 + 字贴着刻度站）
     ===================================================================== */
  L.vRuler = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.face || 'heavy';
    const ws = words(st.line);
    const m = M();
    const left = (st.side || 'l') === 'l';
    const rx = left ? m * 0.52 : W - m * 0.52;
    const marks = [], out = [];
    const lab = LAB();
    /* 主刻度 */
    marks.push(rule(rx, H * 0.10, rx, H * 0.90, p.sub, { a: 0.55, dur: 0.8, at: 0.05 }));
    const n = st.n || 18;
    for (let k = 0; k <= n; k++) {
      const y = H * 0.10 + (H * 0.80) * k / n;
      const big = k % 3 === 0;
      const len = big ? lab * 0.9 : lab * 0.45;
      marks.push(rule(rx, y, rx + (left ? len : -len), y, p.sub, { a: big ? 0.75 : 0.45, at: 0.10 + k * 0.02, dur: 0.22 }));
      if (big && st.nums !== false) {
        marks.push(label(rx + (left ? len + lab * 0.5 : -len - lab * 0.5), y + lab * 0.30, pad2(k), lab * 0.78, p.sub, {
          align: left ? 'left' : 'right', at: 0.14 + k * 0.02, a: 0.92, track: 0.1,
        }));
      }
    }
    const textX = left ? rx + lab * 4.2 : rx - lab * 4.2;
    /* availW 必须服从导演层给的让位宽度，不能一路量到画布边。
       这里一度写成 W - textX - m（直接量到右缘），于是人物站在右边时
       整句压到她身上 145px —— i=44 「please don't go away」就是这样。 */
    const box = W * (st.availW || 0.62);
    const availW = Math.max(W * 0.24, left ? (m + box) - textX : box - lab * 4.2);
    const size = Math.round(Math.min(H * 0.13, fitW(ctx, ws.map(q => q.t).join(' '), face, availW, H)));
    let rws = wrap(ctx, ws, face, size, st.track || 0.01, availW);
    if (rws.length > 4) rws = rws.slice(0, 4);
    const lh = size * 1.18;
    const totalH = (rws.length - 1) * lh + size;
    rws.forEach((rw, k) => {
      const r2 = row(ctx, { face, track: st.track || 0.01, squeeze: 1 }, rw, size);
      shift(r2.g, left ? textX : textX - r2.w, H * 0.5 - totalH / 2 + k * lh + size * 0.78);
      for (const q of r2.g) { q.rowGap = lh; q.row = k; }
      out.push(...r2.g);
    });
    return { glyphs: out, bbox: PV.bboxOf(out), size, marks, bleed: true };
  };

  /* =====================================================================
     9  scaleSteps —— 階段（逐词递减字号，左缘对齐成阶梯）
     ===================================================================== */
  L.scaleSteps = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.face || 'heavy';
    const ws = words(st.line);
    const m = M();
    const out = [], marks = [];
    const k = ws.length;
    const up = st.dir === 'up';
    const r = k > 1 ? Math.max(0.80, Math.pow(0.5, 1 / (k - 1))) : 1;
    const bigK = st.bigK || 0.17;
    let size = H * bigK;
    /* 逐词宽度的和必须进画面：先按最宽的那个词收一次 */
    const widest = ws.reduce((a, q) => (q.t.length > a.length ? q.t : a), '');
    size = Math.min(size, fitW(ctx, widest, face, W * 0.72, H));
    const sizes = ws.map((_, i) => Math.round(size * Math.pow(r, up ? i : k - 1 - i)));
    const lh = sizes.map(s => s * 1.02);
    const totalH = lh.reduce((a, b) => a + b, 0);
    let y = H * 0.52 - totalH / 2;
    let indent = 0;
    ws.forEach((w, i) => {
      const g = row(ctx, { face, track: st.track || -0.01, squeeze: 1 }, [w], sizes[i]);
      const x = m + indent;
      shift(g.g, x, y + sizes[i] * 0.80);
      for (const q of g.g) { q.rowGap = lh[i]; q.row = i; }
      out.push(...g.g);
      /* 每一级下压一条极短的细线，坐实「台阶」 */
      /* 踏步线必须落在基线下 0.1em，不能落在行框底：落在行框底正好横穿下一行的字腰，
         抽帧看就像「画面被划了几道」。 */
      if (st.tread !== false) marks.push(rule(m, y + sizes[i] * 0.92, m + sizes[i] * 0.9, y + sizes[i] * 0.92, p.sub, { a: 0.30, at: 0.15 + i * 0.08, dur: 0.3 }));
      indent += sizes[i] * 0.18;
      y += lh[i];
    });
    return { glyphs: out, bbox: PV.bboxOf(out), size, marks, bleed: true };
  };

  /* =====================================================================
     10  marginNote —— 余白（小字压在版心一隅，留白占八成）
     ===================================================================== */
  L.marginNote = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.body || 'sansMed';
    const ws = words(st.line);
    const m = M();
    const out = [], marks = [];
    const lab = LAB();
    const bottom = (st.pos || 'b') === 'b';
    const availW = W * (st.availW || 0.56);
    const x0 = st.x != null ? W * st.x : m;
    const size = Math.round(Math.min(H * 0.072, fitW(ctx, ws.map(q => q.t).join(' '), face, availW, H)));
    let rws = wrap(ctx, ws, face, size, st.track || 0.03, availW);
    if (rws.length > 3) rws = rws.slice(0, 3);
    const lh = size * 1.46;
    const blockH = (rws.length - 1) * lh + size;
    const y0 = bottom ? H * 0.80 - blockH : H * 0.20;
    rws.forEach((rw, k) => {
      const r2 = row(ctx, { face, track: st.track || 0.03, squeeze: 1 }, rw, size);
      shift(r2.g, x0, y0 + k * lh + size * 0.80);
      for (const q of r2.g) { q.rowGap = lh; q.row = k; }
      out.push(...r2.g);
    });
    /* 一条长细线从版心左侧一直拉到右侧，把「空」切开 —— 空也不再是没设计 */
    const ry = bottom ? y0 - lab * 2.2 : y0 + blockH + lab * 2.2;
    marks.push(rule(x0, ry, x0 + availW, ry, p.sub, { a: 0.42, dur: 1.0, at: 0.06 }));
    marks.push(label(x0, ry - lab * 0.7, st.note || '', lab * 0.82, p.sub, { track: 0.22, at: 0.18 }));
    /* 编号挂在细线末端，不钉在画布右缘。
       钉右缘的话人物站右边时它会被画进圆窗里（t=26 的 59 就落在她脸上）。 */
    marks.push(label(x0 + availW, ry - lab * 0.7, st.no != null ? pad2(st.no) : '', lab * 0.82, p.accent, { align: 'right', track: 0.14, at: 0.22 }));
    return { glyphs: out, bbox: PV.bboxOf(out), size, marks, bleed: true };
  };

  /* =====================================================================
     11  fullTrack —— 全字距（一行摊满整幅，靠字距立住，不靠字号）
     ===================================================================== */
  L.fullTrack = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.face || 'mono';
    const txt = String(st.text || st.line.text || '').trim().toUpperCase();
    const m = M();
    const marks = [];
    /* 全字距要摊满版心，但摊不到人物身上：无人物时才允许 W-2m，有人物时收进 availW */
    const availW = Math.min(W - m * 2, W * (st.availW || 0.86));
    const n = [...txt].length;
    /* 反解字距：让 n 个字正好摊满版心 */
    const base = 100;
    const w100 = PV.measure(ctx, txt, face, base);
    let size = Math.round(Math.min(H * 0.14, availW / ((w100 / base) + 0.30 * Math.max(1, n - 1))));
    let track = 0.30;
    if (size >= H * 0.14) {
      size = Math.round(H * 0.14);
      track = Math.max(0.04, (availW / size - w100 / base) / Math.max(1, n - 1));
    }
    const r = row(ctx, { face, track, squeeze: 1 }, [{ t: txt, t0: st.line.t0, t1: st.line.t1, i: 0 }], size);
    const y = H * 0.52;
    shift(r.g, m + (availW - r.w) / 2, y);
    marks.push(rule(m, y - size * 0.92, W - m, y - size * 0.92, p.sub, { a: 0.5, dur: 0.9, at: 0.05 }));
    marks.push(rule(m, y + size * 0.26, W - m, y + size * 0.26, p.sub, { a: 0.5, dur: 0.9, at: 0.12 }));
    if (st.note) marks.push(label(m, y - size * 0.92 - LAB() * 0.8, st.note, LAB() * 0.8, p.sub, { track: 0.24, at: 0.3 }));
    return { glyphs: r.g, bbox: PV.bboxOf(r.g), size, marks, bleed: true };
  };

  /* =====================================================================
     12  block —— 左缘块（长句主力）：顶左、行距紧、左侧一条短竖线
     ===================================================================== */
  L.block = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.face || 'heavy';
    const ws = words(st.line);
    const m = M();
    const out = [], marks = [];
    const availW = W * (st.availW || 0.70);
    /* 字号优先，行数其次。
       原来先 fitW 把整句塞进一行，再检查行数 —— 长句因此被压到 69px 单行横贯 84% 的画面，
       读作「字幕条」而不是「版面」。JIZURA 的正文块宁愿折两行也不铺满。
       短句（≤4 词）反过来要撑大，那时候一行就该占满版心。 */
    const txt = ws.map(q => q.t).join(' ');
    const maxRows = st.maxRows || 3;
    let size = ws.length <= 4
      ? Math.round(Math.min(H * 0.185, fitW(ctx, txt, face, availW, H)))
      : Math.round(H * (st.hK || 0.135));
    let rws = wrapOpt(ctx, ws, face, size, st.track || 0.0, availW, maxRows);
    for (let guard = 0; guard < 5 && rws.length > maxRows; guard++) {
      size = Math.round(size * 0.86);
      rws = wrapOpt(ctx, ws, face, size, st.track || 0.0, availW, maxRows);
    }
    const lh = size * (st.lead || 1.16);
    const blockH = (rws.length - 1) * lh + size;
    const y0 = H * (st.cy == null ? 0.50 : st.cy) - blockH / 2;
    const x0 = st.x == null ? m : W * st.x;
    rws.forEach((rw, k) => {
      const r2 = row(ctx, { face, track: st.track || 0.0, squeeze: 1 }, rw, size);
      shift(r2.g, x0, y0 + k * lh + size * 0.78);
      for (const q of r2.g) { q.rowGap = lh; q.row = k; }
      out.push(...r2.g);
    });
    const widest = Math.max(...rws.map(rw => row(ctx, { face, track: st.track || 0.0, squeeze: 1 }, rw, size).w));
    marks.push(rule(x0 - LAB() * 1.1, y0 + size * 0.10, x0 - LAB() * 1.1, y0 + blockH - size * 0.16, p.accent, { a: 0.9, w: Math.max(3, HAIR() * 2), dur: 0.5 }));
    marks.push(rule(x0, y0 + blockH + size * 0.30, x0 + Math.min(widest, availW), y0 + blockH + size * 0.30, p.sub, { a: 0.45, dur: 0.8, at: 0.14 }));
    if (st.no != null) marks.push(label(x0, y0 - LAB() * 0.9, pad2(st.no), LAB() * 0.86, p.sub, { track: 0.16, at: 0.20 }));
    return { glyphs: out, bbox: PV.bboxOf(out), size, marks, bleed: true };
  };

  /* =====================================================================
     13  center —— 极简居中（只给间奏/无歌词镜，保持兼容）
     ===================================================================== */
  L.center = (ctx, gs, st) => {
    const p = st.pal || PV.DEFAULT_PAL, face = st.face || 'mincho';
    const txt = String(st.text || st.line.text || '').trim();
    const size = Math.round(fitW(ctx, txt, face, W * 0.72, H * 0.16, st.track || 0.06));
    const r = row(ctx, { face, track: st.track || 0.06, squeeze: 1 }, [{ t: txt, t0: st.line.t0, t1: st.line.t1, i: 0 }], size);
    shift(r.g, W / 2 - r.w / 2, H * 0.52);
    return { glyphs: r.g, bbox: PV.bboxOf(r.g), size, marks: [], bleed: true };
  };

  /* =====================================================================
     marks 渲染
     ===================================================================== */
  const ease = {
    outCubic: t => 1 - Math.pow(1 - t, 3),
    outExpo: t => (t >= 1 ? 1 : 1 - Math.pow(2, -10 * t)),
    inCubic: t => t * t * t,
  };
  PV.markEase = ease;

  function drawOne(ctx, m, lt, out) {
    const at = m.at || 0, dur = m.dur || 0;
    let p = dur > 0 ? PV.clamp((lt - at) / dur) : (lt >= at ? 1 : 0);
    if (p <= 0) return;
    const a = (m.a == null ? 1 : m.a) * (m.fadeIn === false ? 1 : Math.min(1, p * 2.2)) * out;
    if (a <= 0.003) return;
    const col = m.col || '#FFFFFF';

    if (m.k === 'rule') {
      const e = ease.outCubic(p);
      const x1 = m.x0 + (m.x1 - m.x0) * e, y1 = m.y0 + (m.y1 - m.y0) * e;
      PV.line(ctx, m.x0, m.y0, x1, y1, col, a, m.w || 1.5);
    } else if (m.k === 'rect') {
      PV.rect(ctx, m.x, m.y, m.w, m.h, col, a);
    } else if (m.k === 'band') {
      /* 色带：从一侧滑入，滑满后停住；遮住字的那一截就是「帯隠れ」 */
      const e = ease.outExpo(p);
      const dir = m.dir == null ? 1 : m.dir;
      const x = dir > 0 ? -m.w * (1 - e) : m.w * (1 - e);
      PV.rect(ctx, x, m.y, m.w, m.h, col, a);
    } else if (m.k === 'bandText') {
      ctx.save();
      ctx.globalAlpha = a;
      ctx.fillStyle = col;
      ctx.font = '500 ' + m.size + 'px "IBM Plex Mono", "Noto Sans SC", monospace';
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.fillText(m.text || '', m.x, m.y);
      ctx.restore();
    } else if (m.k === 'label') {
      ctx.save();
      ctx.globalAlpha = a;
      ctx.fillStyle = col;
      /* 字形回退是**逐字**的：日文黑体 Zen Kaku 有「己」但没有简体「岁」，
         少了 SC 兜底就会让「岁」静默滑到泛型 monospace（Windows 中文环境=宋体），
         同一个词里一半黑一半宋。Latin 走 IBM Plex Mono（档案行的 mono 质感），
         中文一律 Noto Sans SC，日文假名才轮到 Zen Kaku。见 RULES §83。 */
      /* m.face 可选：中文的「署名行」（片名卡上的原唱名）不该走 mono 的 0.14em 字距 ——
         三个汉字被拉开就是「网页表单」；宋体紧排才是「电影片尾」。拉丁/数字仍走 mono。 */
      const _sz = m.size || 24;
      ctx.font = m.face
        ? PV.cssFace(m.face, _sz)
        : '400 ' + _sz + 'px "IBM Plex Mono", "Noto Sans SC", "Zen Kaku Gothic New", monospace';
      ctx.textBaseline = 'alphabetic';
      const _cjkLab = /[\u3400-\u9fff]/.test(String(m.text || ''));
      const tr = (m.track == null ? (_cjkLab ? 0.02 : 0.14) : m.track) * _sz;
      const al = m.align || 'left';
      ctx.textAlign = 'left';
      const s = String(m.text || '');
      let x = m.x;
      if (al === 'center') x = m.x - (measureLabel(ctx, s, tr)) / 2;
      else if (al === 'right') x = m.x - measureLabel(ctx, s, tr);
      for (let i = 0; i < s.length; i++) {
        ctx.fillText(s[i], x, m.y);
        x += ctx.measureText(s[i]).width + tr;
      }
      ctx.restore();
    } else if (m.k === 'circle') {
      const e = ease.outCubic(p);
      ctx.save();
      ctx.globalAlpha = a;
      ctx.strokeStyle = col; ctx.lineWidth = m.w || 1.5;
      ctx.beginPath(); ctx.arc(m.cx, m.cy, m.r * (0.55 + 0.45 * e), 0, Math.PI * 2); ctx.stroke();
      ctx.restore();
    }
  }
  function measureLabel(ctx, s, tr) {
    let w = 0;
    for (const ch of s) w += ctx.measureText(ch).width + tr;
    return Math.max(0, w - tr);
  }

  /* cap：退场上界（engine 的 X_KEEP）。out = 1 - cap·q² 而不是 1 - q²，
     于是「字停在 55%」时线也停在 55%，切点上的画面与转场快照同形。 */
  PV.drawMarks = (ctx, marks, lt, outDur, dur, phase, cap) => {
    if (!marks || !marks.length) return;
    /* 退场：字在退，版面也必须跟着退，否则会出现「字没了、线还在」 */
    const q = outDur > 0 ? PV.clamp((lt - (dur - outDur)) / outDur) : 0;
    const out = 1 - q * q * (cap == null ? 1 : cap);
    for (const m of marks) {
      const isOver = !!m.over;
      if (phase === 'over' ? !isOver : isOver) continue;
      drawOne(ctx, m, lt, out);
    }
  };

  /* 巨型暗影（cropGiant / titleCard 用）：单独一层，画在 marks 之前、字之后？ 
     它必须**在字下面**，所以 engine 在 drawLyrics 开始处调用。 */
  PV.drawGiant = (ctx, giant, lt, outDur, dur, cap) => {
    if (!giant) return;
    const g = giant;
    const q = outDur > 0 ? PV.clamp((lt - (dur - outDur)) / outDur) : 0;
    const out = 1 - q * q * (cap == null ? 1 : cap);
    const pIn = PV.clamp(lt / 0.7);
    ctx.save();
    ctx.globalAlpha = g.alpha * out * (0.55 + 0.45 * ease.outCubic(pIn));
    const col = PV.hex(g.col);
    for (const q2 of g.glyphs) {
      if (q2.isSpace) continue;
      ctx.save();
      const cx = q2.x + q2.w / 2 + q2.dx, cy = q2.y - q2.size * 0.36 + q2.dy;
      ctx.translate(cx, cy);
      if (q2.rot) ctx.rotate(q2.rot);
      ctx.font = PV.cssFace(q2.face, q2.size);
      ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
      ctx.fillStyle = PV.rgba(col, 1);
      ctx.fillText(q2.c, -q2.w / 2, q2.size * 0.36);
      ctx.restore();
    }
    ctx.restore();
  };
})();
