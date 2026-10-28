/*! pv_piece.js —— 字形连通域拆解 + piece 级绘制
 *
 *  JIZURA 最能一眼认出的东西：字不是一个整体，而是**一堆可以各自飞散的零件**。
 *  `02_fonts.js` 的做法是把字符栅格化后做连通域（connected component）标记，
 *  于是「語」被拆成「言 + 吾」、「漂」被拆成「氵 + 票」，每个部件能独立位移/旋转/淡出。
 *  本文件把这套机制移植到我们的 face / 坐标系上。
 *
 *  为什么值得移植、不能用别的东西替代：
 *    - 用 clip 均匀切四刀只能得到「整齐的四块」，读作「字被切了」而不是「字碎了」；
 *      连通域得到的是笔画本来的结构，碎法跟着字形走。
 *    - 位移的量级完全不同：部件可以从 3 倍字宽外飞回原位，整字位移到那么远
 *      会读作「画面在晃」而不是「字在重组」。
 *
 *  坐标系约定（与 pv_type.js 的 drawGlyphs 对齐）：
 *    进入绘制前 ctx 已 translate 到**字形视觉中心**，故 piece.cx/cy（单位 em、
 *    相对该中心）直接乘 g.size 即为像素偏移。
 *  栅格化用 textBaseline='middle'（与 JIZURA 相同），与 drawGlyphs 的 alphabetic
 *  基线在原点是等价的 —— 两者都以「视觉中心」为原点。
 */
(() => {
  'use strict';
  const PV = window.PV;
  const DEG = Math.PI / 180;
  /* 384 而不是 512：**染色副本是按配色各存一份的**（见 sprite），一个零件遇上 5~7 套
     scheme 就会有 5~7 张同尺寸 canvas。512 时整套缓存实测把 4K 渲染的 renderer 堆
     顶到 `ERR_BLOB_OUT_OF_MEMORY`（成片渲到 10200/13931 帧崩，`toBlob` 返回 null）。
     384 在 4K 下对 190px 以下的设计字号仍然足够锐，超过这个尺寸的字本来就少。 */
  const MAX_RES = 384;
  const MAX_ENTRIES = 500;     // 字符×字体×分桶的组合上限
  const MAX_TINT = 200;        // 染色副本上限：它只是缓存便利，重染的代价很小

  /* ---------------- piece cache ---------------- */
  const _pm = new Map();          // key -> {ch,res,pieces}
  const _tint = new Map();        // pieceId -> Map(color -> canvas)
  let _pid = 0;

  const bucket = px => { let r = 64; while (r < px && r < MAX_RES) r *= 2; return r; };

  /* 把一个字符拆成若干连通块。返回 [{id,cv,res,cx,cy,w,h,area}]（cx/cy/w/h 单位 em） */
  function decompose(faceKey, ch, res) {
    const S = Math.ceil(res * 1.45), half = S / 2;
    const cv = document.createElement('canvas');
    cv.width = S; cv.height = S;
    const x = cv.getContext('2d', { willReadFrequently: true });
    x.font = PV.cssFace(faceKey, res);
    x.textAlign = 'center';
    x.textBaseline = 'middle';
    x.fillStyle = '#fff';
    x.fillText(ch, half, half);

    const img = x.getImageData(0, 0, S, S).data;
    const N = S * S;
    const A = new Uint8Array(N);
    for (let i = 0; i < N; i++) A[i] = img[i * 4 + 3];

    /* ---- connected components（8 邻域，显式栈） ---- */
    const TH = 60;
    const L = new Int32Array(N);
    const stack = new Int32Array(N);
    const boxes = [];
    let nl = 0;
    for (let i = 0; i < N; i++) {
      if (A[i] < TH || L[i]) continue;
      nl++;
      let sp = 0;
      stack[sp++] = i;
      L[i] = nl;
      let x0 = S, y0 = S, x1 = 0, y1 = 0, area = 0;
      while (sp) {
        const p = stack[--sp], px = p % S, py = (p / S) | 0;
        area++;
        if (px < x0) x0 = px;
        if (px > x1) x1 = px;
        if (py < y0) y0 = py;
        if (py > y1) y1 = py;
        for (let dy = -1; dy <= 1; dy++) {
          const yy = py + dy;
          if (yy < 0 || yy >= S) continue;
          for (let dx = -1; dx <= 1; dx++) {
            const xx = px + dx;
            if (xx < 0 || xx >= S) continue;
            const q = yy * S + xx;
            if (!L[q] && A[q] >= TH) { L[q] = nl; stack[sp++] = q; }
          }
        }
      }
      boxes[nl] = { x0, y0, x1, y1, area };
    }
    if (!nl) return { ch, res, pieces: [] };

    /* 抗锯齿边缘不属于任何 label：两轮膨胀把它并到邻居身上，
       否则每个字周围会留一圈半透明毛边（点钩类小粒度尤其明显）。 */
    for (let pass = 0; pass < 2; pass++) {
      const L2 = L.slice();
      for (let p = 0; p < N; p++) {
        if (L[p] || !A[p]) continue;
        const px = p % S, py = (p / S) | 0;
        let lab = 0;
        if (px > 0 && L[p - 1]) lab = L[p - 1];
        else if (px < S - 1 && L[p + 1]) lab = L[p + 1];
        else if (py > 0 && L[p - S]) lab = L[p - S];
        else if (py < S - 1 && L[p + S]) lab = L[p + S];
        if (lab) {
          L2[p] = lab;
          const b = boxes[lab];
          if (px < b.x0) b.x0 = px;
          if (px > b.x1) b.x1 = px;
          if (py < b.y0) b.y0 = py;
          if (py > b.y1) b.y1 = py;
        }
      }
      L.set(L2);
    }

    /* 小渣滓并进最近的块：点/钩独立飞出去读作「噪点」而不是「碎了」 */
    const minA = res * res * 0.0012;
    const remap = new Int32Array(nl + 1);
    for (let l = 1; l <= nl; l++) remap[l] = l;
    for (let l = 1; l <= nl; l++) {
      const b = boxes[l];
      if (b.area >= minA) continue;
      let best = 0, bd = 1e9;
      const cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
      for (let m = 1; m <= nl; m++) {
        if (m === l || boxes[m].area < minA) continue;
        const o = boxes[m];
        const dx = Math.max(o.x0 - cx, 0, cx - o.x1);
        const dy = Math.max(o.y0 - cy, 0, cy - o.y1);
        const d = dx * dx + dy * dy;
        if (d < bd) { bd = d; best = m; }
      }
      if (best) {
        remap[l] = best;
        const o = boxes[best];
        o.x0 = Math.min(o.x0, b.x0); o.y0 = Math.min(o.y0, b.y0);
        o.x1 = Math.max(o.x1, b.x1); o.y1 = Math.max(o.y1, b.y1);
      }
    }

    const pieces = [];
    for (let l = 1; l <= nl; l++) {
      if (remap[l] !== l) continue;
      const b = boxes[l];
      const w = b.x1 - b.x0 + 1, h = b.y1 - b.y0 + 1;
      const pc = document.createElement('canvas');
      pc.width = w; pc.height = h;
      const px2 = pc.getContext('2d');
      const id = px2.createImageData(w, h);
      const d = id.data;
      for (let yy = 0; yy < h; yy++) {
        for (let xx = 0; xx < w; xx++) {
          const p = (b.y0 + yy) * S + (b.x0 + xx);
          if (remap[L[p]] === l && L[p]) {
            const o = (yy * w + xx) * 4;
            d[o] = d[o + 1] = d[o + 2] = 255;
            d[o + 3] = A[p];
          }
        }
      }
      px2.putImageData(id, 0, 0);
      pieces.push({
        id: ++_pid, cv: pc, res,
        cx: ((b.x0 + b.x1 + 1) / 2 - half) / res,
        cy: ((b.y0 + b.y1 + 1) / 2 - half) / res,
        w: w / res, h: h / res,
        area: b.area / (res * res),
      });
    }
    pieces.sort((a, b) => b.area - a.area);     // 大块在前：视觉权重大的先归位
    return { ch, res, pieces };
  }

  PV.glyphPieces = (faceKey, ch, px) => {
    const res = bucket(px);
    const key = faceKey + '|' + ch + '|' + res;
    let g = _pm.get(key);
    if (!g) {
      g = decompose(faceKey, ch, res);
      if (_pm.size > MAX_ENTRIES) { _pm.clear(); _tint.clear(); }
      _pm.set(key, g);
    }
    return g;
  };

  /* ---------------- 把单块再切碎（JIZURA `fragments`）----------------
     为什么必须有这一层：**拉丁字母几乎清一色是单一连通块**。
     日本字「語」能拆成「言+吾」，但 "H" "e" "l" 各只有一块 ——
     只做连通域的话，英文歌词上 assemble/explode 会完全失效（实测 "I hope"
     三个字纹丝不动，因为每块零件就是整颗字母）。JIZURA 对此的办法是
     按块的尺寸再补 1~2 刀，切成凸多边形碎片：这才有得碎。
     注意 JIZURA 的语义分工：assemble 用 pieces，explode/fall 才碎 ——
     所以这一层只在 g.shatter 时启用。 */
  function clipHalf(poly, nx, ny, d0, sgn) {
    const out = [];
    for (let i = 0; i < poly.length; i++) {
      const a = poly[i], b = poly[(i + 1) % poly.length];
      const da = sgn * (nx * a[0] + ny * a[1] - d0);
      const db = sgn * (nx * b[0] + ny * b[1] - d0);
      if (da >= 0) out.push(a);
      if ((da >= 0) !== (db >= 0)) {
        const t = da / (da - db);
        out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
      }
    }
    return out;
  }

  function fragmentize(pc, seed) {
    if (pc.frags) return pc.frags;
    const w = pc.w, h = pc.h;
    let polys = [[[-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]]];
    const cuts = Math.max(w, h) > 0.42 ? 2 : Math.max(w, h) > 0.2 ? 1 : 0;
    for (let c = 0; c < cuts; c++) {
      const next = [];
      for (const poly of polys) {
        const ang = (w > h ? Math.PI / 2 : 0) + PV.rng(seed, pc.id, c)() * 0.5;
        const nx = Math.cos(ang), ny = Math.sin(ang);
        const cx = poly.reduce((s, p) => s + p[0], 0) / poly.length;
        const cy = poly.reduce((s, p) => s + p[1], 0) / poly.length;
        const off = PV.rng(seed, pc.id, c, 7)() * 0.12 * Math.max(w, h);
        const d0 = nx * cx + ny * cy + off;
        next.push(clipHalf(poly, nx, ny, d0, 1), clipHalf(poly, nx, ny, d0, -1));
      }
      polys = next.filter(p => p.length >= 3);
    }
    pc.frags = polys.map(p => {
      const fx = p.reduce((s, q) => s + q[0], 0) / p.length;
      const fy = p.reduce((s, q) => s + q[1], 0) / p.length;
      return { poly: p, fx, fy };
    });
    return pc.frags;
  }

  /* 展开成可绘制的碎片表（每片带上父级 sprite 的引用与裁剪多边形） */
  function fragFor(gph, seed) {
    if (gph._frag && gph._frag.seed === seed) return gph._frag.list;
    const out = [];
    for (const p of gph.pieces) {
      const fr = fragmentize(p, seed);
      if (fr.length <= 1) { out.push(p); continue; }
      for (let i = 0; i < fr.length; i++) {
        const f = fr[i];
        out.push({
          src: p, poly: f.poly, fx: f.fx, fy: f.fy,
          cx: p.cx + f.fx, cy: p.cy + f.fy,
          w: p.w, h: p.h, res: p.res, id: p.id * 8 + i,
          area: p.area / fr.length,
        });
      }
    }
    gph._frag = { seed, list: out };
    return out;
  }

  /* 白 sprite 的染色副本（source-in）；白/未染色直接返回原图 */
  function sprite(p, color) {
    if (!color || color === '#ffffff' || color === '#fff' || /rgba\(255,255,255/.test(color)) return p.cv;
    /* 染色副本是**按配色各存一份**的：一个零件遇上多套 scheme 就会有多张同尺寸 canvas，
       这是整套缓存里最容易失控的一项。它只是省两次 canvas 操作，不值得占内存 ——
       超上限就整表清掉，下次用到重新染。 */
    if (_tint.size > MAX_TINT) _tint.clear();
    let m = _tint.get(p.id);
    if (!m) { m = new Map(); _tint.set(p.id, m); }
    let c = m.get(color);
    if (!c) {
      c = document.createElement('canvas');
      c.width = p.cv.width; c.height = p.cv.height;
      const x = c.getContext('2d');
      x.drawImage(p.cv, 0, 0);
      x.globalCompositeOperation = 'source-in';
      x.fillStyle = color;
      x.fillRect(0, 0, c.width, c.height);
      m.set(color, c);
    }
    return c;
  }
  PV.pieceSprite = sprite;

  /* 一个 piece 的变换，语义对齐 JIZURA 的 J.PT：
     dx dy(px) rot(deg) s(等比缩放) st(单轴拉伸, 沿 sdir) a(alpha)
     st 用来表达「高速飞行时被拉长」，是让 piece 动画不死板的关键。 */
  PV.PT = (dx, dy, rot, s, st, sdir, a) =>
    ({ dx: dx || 0, dy: dy || 0, rot: rot || 0, s: s == null ? 1 : s, st: st == null ? 1 : st, sdir: sdir || 0, a: a == null ? 1 : a });
  PV.PID = PV.PT(0, 0, 0, 1, 1, 0, 1);

  /* ---------------- 多个来源的合成 ----------------
     入场和退场可能都要驱动零件（沿 JIZURA 的 combinePiece）：
       - 任一环节返回 null  ⇒ 该零件整体消失，立即短路
       - 返回 PID          ⇒ 这一层没有意见，跳过（但不能因此认定「静止」）
       - 其余：位移/旋转累加，缩放与 alpha 相乘，单轴拉伸取较大者 */
  PV.combinePiece = (fns) => {
    if (!fns || !fns.length) return null;
    if (fns.length === 1) return fns[0];
    return (j, p, n) => {
      let o = null;
      for (let i = 0; i < fns.length; i++) {
        const r = fns[i](j, p, n);
        if (r === null) return null;
        if (r === PV.PID) continue;
        if (!o) o = PV.PT();
        o.dx += r.dx || 0;
        o.dy += r.dy || 0;
        o.rot += r.rot || 0;
        o.s *= (r.s == null ? 1 : r.s);
        o.a *= (r.a == null ? 1 : r.a);
        if (Math.abs(r.st - 1) < 1e-6) { /* no-op */ }
        else if (r.st > o.st) { o.st = r.st; o.sdir = r.sdir; }
      }
      return o;
    };
  };

  /* ---------------- 绘制 ----------------
     g.pfns 是若干驱动源；合成后若所有零件都在原位（PID），
     说明这一帧零件动画已经结束 —— 退回整字绘制，既省一次 drawImage，
     也保住 TREAT 的笔画处理（渐层/空心不受影响）。 */
  PV.drawPieces = (ctx, g, pieces, color, alpha, fn) => {
    const size = g.size;
    /* g.shatter：单字母也要能碎 —— 先把它切成凸多边形碎片（见fragile_for）。
       退场只有这一步才谈得上「碎」，否则整颗字母飞走 = 整字退场。 */
    const gph = g._gph;
    const list = (g.shatter && gph) ? fragFor(gph, g.psd || 1) : pieces;
    let moved = false;
    for (let j = 0; j < list.length; j++) {
      const p = list[j];
      const t = fn(j, p, list.length);
      if (t === null) { moved = true; continue; }               // 已碎掉 / 未出现：什么都不画
      if (t === PV.PID) continue;                               // 这一层没意见
      moved = true;
      if (t.a != null && t.a <= 0.004) continue;
      const spr = sprite(p.src || p, color);
      ctx.save();
      ctx.translate(p.cx * size + (t.dx || 0), p.cy * size + (t.dy || 0));
      if (t.st != null && t.st !== 1) {
        const d = (t.sdir || 0) * DEG;
        ctx.rotate(d);
        ctx.scale(t.st, 1 / Math.sqrt(t.st));     // 保面积：拉长时不一起变胖
        ctx.rotate(-d);
      }
      if (t.rot) ctx.rotate(t.rot * DEG);
      const k = (size / p.res) * (t.s == null ? 1 : t.s);
      ctx.scale(k, k);
      ctx.globalAlpha = alpha * (t.a == null ? 1 : t.a);
      if (p.poly) {
        /* 碎片：裁剪到自己的多边形上，再画父级 sprite。
           多边形顶点是「相对父级中心」的 em 坐标，这里已经 translate 到碎片中心，
           故要减掉碎片自身的质心偏移；sprite 同理放在父级中心。 */
        const src = p.src, res = p.res;
        ctx.beginPath();
        for (let qi = 0; qi < p.poly.length; qi++) {
          const q = p.poly[qi];
          const X = (q[0] - p.fx) * res, Y = (q[1] - p.fy) * res;
          if (qi) ctx.lineTo(X, Y); else ctx.moveTo(X, Y);
        }
        ctx.closePath();
        ctx.clip();
        ctx.drawImage(spr, -p.fx * res - src.w * res / 2, -p.fy * res - src.h * res / 2);
      } else {
        ctx.drawImage(spr, -spr.width / 2, -spr.height / 2);
      }
      ctx.restore();
    }
    return moved;
  };

  /* 该字在这个尺寸下能拆出几块。只有真的拆开了（>=2 块）才值得走 pfn 分支：
     只拆出 1 块时等于用 sprite 画了一个整字 —— 多一次 drawImage 且没有任何收益，
     还会和 TREAT 的笔画处理（渐层/空心）打架。 */
  PV.pieceCount = (faceKey, ch, px) => PV.glyphPieces(faceKey, ch, px).pieces.length;

  PV.pieceCacheSize = () => _pm.size;
})();
