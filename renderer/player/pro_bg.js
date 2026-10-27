/* pro_bg.js —— 歌切「专业向」背景可视化层（v10 深色版专用）
 *
 * 设计原则：
 *   ① 全部图形由真实音频分析驱动（频段能量 / 人声基频 / 谱通量 / 段落），不做假动画；
 *   ② 只做「背景」：网格 alpha ≤0.10、块体 ≤0.45、主线 ≤0.60 —— 歌词（逐字点亮）
 *      必须永远是第一层级；
 *   ③ 所有动效都是 t 的连续函数，离线渲染逐帧查表 ⇒ P=2 两段拼接处不会跳变；
 *   ④ 不改动任何计算链（歌词/卡拉OK/导出），只在 drawBackground 之后插一层。
 *
 * 为什么不用 globalCompositeOperation='lighter'：加法混合会把白网格 + 琥珀主线叠成
 * 一片惨白，实测整层「洗褪色」。本层所有元素都比近黑底更亮，source-over 不会把画面弄脏，
 * 颜色也保得住。
 *
 * 数据（tools/analyze_music.py 生成，song_cutter/render_song 经 job.proAnalysis 注入）：
 *   { fps, dur, nbands, nframes, bpm, bar_sec, bars[], sections[[t0,t1,label]],
 *     bands_b64(N×nb,uint8), rms_b64(N), flux_b64(N), voiced_b64(N), midi10_b64(N,int16) }
 *
 * 切换：URL ?bg=daw|spec|vocal|none（none = 不加层，保持现状）
 */
(function () {
  'use strict';

  const P = window.PROBG = { mode: 'none', ok: false, A: null };

  /* ---------------- 数据层 ---------------- */
  function b64u8(b64) {
    const s = atob(b64), n = s.length, a = new Uint8Array(n);
    for (let i = 0; i < n; i++) a[i] = s.charCodeAt(i);
    return a;
  }
  function b64i16(b64) {
    const s = atob(b64), n = s.length >> 1, u = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
    const dv = new DataView(u.buffer), a = new Int16Array(n);
    for (let i = 0; i < n; i++) a[i] = dv.getInt16(i * 2, true);
    return a;
  }
  function pct(arr, q) {
    if (!arr.length) return 0;
    const a = Array.prototype.slice.call(arr).sort((x, y) => x - y);
    return a[clamp(Math.round(q * (a.length - 1)), 0, a.length - 1)] / 255;
  }

  P.set = function (j) {
    if (!j || !j.bands_b64) return false;
    const n = j.nframes | 0, nb = j.nbands | 0;
    const A = P.A = {
      fps: j.fps || 60, dur: j.dur || 0, nb: nb, N: n,
      bpm: j.bpm || 120, barSec: j.bar_sec || 2,
      bands: b64u8(j.bands_b64), rms: b64u8(j.rms_b64 || ''),
      flux: b64u8(j.flux_b64 || ''), voiced: b64u8(j.voiced_b64 || ''),
      midi: b64i16(j.midi10_b64 || ''),
      bars: (j.bars || []).slice(), sections: (j.sections || []).slice(),
      bandHz: j.band_hz || [],
      off: j.off || 0,                       // 片段起点在整曲里的秒数（测试片段/预听用）
    };
    P.ok = A.N > 0 && A.bands.length >= A.N * nb;
    if (P.ok) {
      /* DYN 轨用「本曲自己的 p10~p95」做刻度：母带响度普遍挤在 -14~-10dB，
         用绝对刻度画出来是一条贴顶的厚带（实测就是一整块色板）。 */
      A.lo = pct(A.rms, 0.10); A.hi = Math.max(A.lo + 0.02, pct(A.rms, 0.95));
      /* 人声频段（band 18~34 ≈ 300Hz~3.5kHz）的 p10/p90 —— DYN 轨刻度。
         用绝对刻度时它长期贴顶（实测就是一整块实心色板），必须按曲自适应。 */
      const vals = [];
      for (let i = 0; i < A.N; i += 3) {
        let sum = 0;
        for (let b = 18; b <= 34; b++) sum += band(i, b);
        vals.push(sum / 17);
      }
      const sv = vals.sort((x, y) => x - y);
      /* 取 p3/p97 而不是 p8/p92：DYN 轨要的是「能看到换气与分句」，
         收得太紧会让整条曲线长时间贴着天花板（截顶）= 一块平板。 */
      A.voxLo = sv[Math.floor(sv.length * 0.03)] || 0;
      A.voxHi = Math.max(A.voxLo + 0.02, sv[Math.floor(sv.length * 0.97)] || 1);

      /* 人声音域（钢琴卷帘纵轴）：按全曲 F0 的 p8/p92 自动适配。
         写死 47~79（32 个半音）会把人声压在半个屏幕里 —— 线条看着像地震仪，
         而且低音区（贝斯/八度残渣）常年空着大半格。限 [44,80] 是为了剔掉
         低音锁频（本曲实测有一簇 MIDI 36 的 C2 假值）。 */
      const pv = [];
      for (let i = 0; i < A.N; i++) {
        const v = A.midi[i];
        if (v > -100) { const s = v / 10; if (s >= 44 && s <= 80) pv.push(s); }
      }
      if (pv.length > 200) {
        pv.sort((x, y) => x - y);
        let lo = Math.floor(pv[Math.floor(pv.length * 0.08)]) - 1;
        let hi = Math.ceil(pv[Math.floor(pv.length * 0.92)]) + 1;
        const MINSPAN = 14, MAXSPAN = 30;
        if (hi - lo < MINSPAN) { const c = (hi + lo) / 2; lo = Math.round(c - MINSPAN / 2); hi = lo + MINSPAN; }
        if (hi - lo > MAXSPAN) { hi = lo + MAXSPAN; }
        A.pchLo = lo; A.pchHi = hi;
      } else { A.pchLo = 48; A.pchHi = 78; }
    }
    return P.ok;
  };

  const clamp = (v, a, b) => v < a ? a : (v > b ? b : v);
  /* t 一律是「片段内时间」，查表时加上 A.off → 同一份全曲分析可服务任意片段 */
  const fi = (t) => clamp(Math.round((t + P.A.off) * P.A.fps), 0, P.A.N - 1);
  const band = (i, b) => P.A.bands[clamp(i, 0, P.A.N - 1) * P.A.nb + clamp(b | 0, 0, P.A.nb - 1)] / 255;
  function bandRange(i, b0, b1) {
    let s = 0, n = 0;
    for (let b = b0; b <= b1; b++) { s += band(i, b); n++; }
    return n ? s / n : 0;
  }
  const rmsAt = (i) => P.A.rms.length ? P.A.rms[clamp(i, 0, P.A.N - 1)] / 255 : 0;
  const fluxAt = (i) => P.A.flux.length ? P.A.flux[clamp(i, 0, P.A.N - 1)] / 255 : 0;
  const voicedAt = (i) => P.A.voiced.length ? P.A.voiced[clamp(i, 0, P.A.N - 1)] / 255 : 0;
  function dynAt(i) {                       // 0..1，本曲自适应刻度
    const A = P.A;
    return clamp((rmsAt(i) - A.lo) / (A.hi - A.lo), 0, 1);
  }

  /* 基频：两层清洗 —— ① ±3 帧中值砍掉孤立尖刺（八度残留/换气毛刺）
     ② 5 帧滑动均值（≈83ms）压颤音抖动。保留换音的台阶，不至于画成地震仪。 */
  const _pc = new Map();
  function pitchAt(i) {
    const A = P.A;
    i = clamp(i, 0, A.N - 1);
    if (_pc.has(i)) return _pc.get(i);
    const med = [];
    for (let k = -3; k <= 3; k++) {
      const v = A.midi[clamp(i + k, 0, A.N - 1)];
      if (v > -100) med.push(v / 10);
    }
    let out = NaN;
    if (med.length >= 3) {
      med.sort((a, b) => a - b);
      const mc = med[med.length >> 1];
      let s = 0, n = 0;
      for (let k = -2; k <= 2; k++) {
        const v = A.midi[clamp(i + k, 0, A.N - 1)];
        if (v > -100 && Math.abs(v / 10 - mc) <= 3.0) { s += v / 10; n++; }
      }
      out = n ? s / n : mc;
    }
    if (_pc.size > 60000) _pc.clear();
    _pc.set(i, out);
    return out;
  }
  /* 绘制用：与 ±15 帧（≈0.5s）中值相差 >6 半音的音点不画。
     八度误判有时能连成 10+ 帧（pyin 在长音上会锁错一整个八度），±3 帧中值杀不掉，
     画出来就是一根刺。数据本身不改，只在渲染时判为「不可信」跳过。 */
  function pitchDisp(i) {
    const v = pitchAt(i);
    if (!isFinite(v)) return NaN;
    const w = [];
    for (let k = -10; k <= 10; k++) {
      const q = pitchAt(clamp(i + k, 0, P.A.N - 1));
      if (isFinite(q)) w.push(q);
    }
    if (w.length < 8) return v;
    w.sort((a, b) => a - b);
    const ref = w[w.length >> 1];
    return Math.abs(v - ref) > 4.5 ? NaN : v;
  }

  /* 音符轨专用：先做 ±6 帧中值（≈0.2s，正好覆盖一个颤音周期）再量化到半音。
     直接量化 pitchDisp 会被 5~6Hz 的 vibrato 打断成一串小方块，读不出「音符」。 */
  function pitchNoteAt(i) {
    const w = [];
    for (let k = -6; k <= 6; k++) {
      const q = pitchDisp(clamp(i + k, 0, P.A.N - 1));
      if (isFinite(q)) w.push(q);
    }
    if (w.length < 5) return NaN;
    w.sort((a, b) => a - b);
    return w[w.length >> 1];
  }

  const NOTE = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
  function noteName(m) {
    const r = Math.round(m), o = Math.floor(r / 12) - 1;
    return NOTE[((r % 12) + 12) % 12] + o;
  }
  function sectionAt(t) {
    const S = P.A.sections;
    for (let i = 0; i < S.length; i++) if (t >= S[i][0] && t < S[i][1]) return S[i];
    return S.length ? S[S.length - 1] : [0, P.A.dur, ''];
  }
  function timeStr(s) {
    s = Math.max(0, s);
    const m = Math.floor(s / 60), r = s - m * 60;
    return m + ':' + (r < 10 ? '0' : '') + r.toFixed(1);
  }

  /* ---------------- 绘制基元 ---------------- */
  function Ln(ctx, x1, y1, x2, y2, col, a, w) {
    if (a <= 0.003) return;
    ctx.globalAlpha = a; ctx.strokeStyle = col; ctx.lineWidth = w || 1;
    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
  }
  function Rc(ctx, x, y, w, h, r, col, a) {
    if (a <= 0.003 || w <= 0 || h <= 0) return;
    ctx.globalAlpha = a; ctx.fillStyle = col;
    ctx.beginPath(); ctx.roundRect(x, y, Math.max(0.5, w), Math.max(0.5, h), r); ctx.fill();
  }
  function Tx(ctx, s, x, y, size, col, a, weight, align, spacing) {
    if (a <= 0.005) return;
    ctx.globalAlpha = a; ctx.fillStyle = col;
    ctx.textAlign = align || 'left'; ctx.textBaseline = 'middle';
    ctx.font = (weight || 500) + ' ' + size + 'px "HarmonyOS Sans SC",sans-serif';
    if (!spacing) { ctx.fillText(s, x, y); return; }
    const tot = Array.from(s).reduce((q, ch) => q + ctx.measureText(ch).width + spacing, 0);
    let cx = align === 'right' ? x - tot : (align === 'center' ? x - tot / 2 : x);
    for (const ch of s) { ctx.fillText(ch, cx, y); cx += ctx.measureText(ch).width + spacing; }
  }

  /* ================================================================
   * 方案 A：DAW 编排视图
   * ================================================================ */
  const LANES = [
    { n: 'VOX', k: 'acc', fn: (i) => voicedAt(i) * 0.75 + bandRange(i, 20, 34) * 0.45 },
    { n: 'PIANO', k: 'ice', fn: (i) => bandRange(i, 12, 26) * 1.05 },
    { n: 'DRUMS', k: 'wht', fn: (i) => fluxAt(i) * 0.95 },
    { n: 'BASS', k: 'acc', fn: (i) => bandRange(i, 0, 7) * 1.15 },
    { n: 'SYNTH', k: 'ice', fn: (i) => bandRange(i, 34, 43) * 1.25 },
    { n: 'FX', k: 'wht', fn: (i) => bandRange(i, 43, 47) * 1.55 },
  ];

  function drawDaw(o) {
    const { ctx, LY, COL } = o, A = P.A, t = o.t;
    const x0 = LY.x, y0 = LY.y, w = LY.w, h = LY.h;
    /* 左侧 72px 是「轨道名槽」（DAW 泳道的固定列），时间轴从 ax 起算。
       早期版本块从 x0+70 起、网格/播放头却从 x0 起 ⇒ 播放头与 clip 恒定错位 70px，
       肉眼看不出「错」，但播放头会稳定压在错误的小节上。 */
    const GUT = 72, ax = x0 + GUT, aw = w - GUT;
    const pps = aw / A.dur, barPx = A.barSec * pps, nBars = Math.ceil(A.dur / A.barSec);
    const tsong = t + A.off;                       // 全曲时间轴上的位置

    /* ① 幽灵网格（极低 alpha，只作质感） */
    for (let b = 0; b <= nBars; b++) {
      const x = ax + b * barPx; if (x > ax + aw) break;
      const four = b % 4 === 0;
      Ln(ctx, x, y0, x, y0 + h, COL.wht, four ? 0.070 : 0.026, 1);
    }
    for (let i = 0; i <= 6; i++) Ln(ctx, ax, y0 + h * i / 6, ax + aw, y0 + h * i / 6, COL.wht, 0.024, 1);

    /* ② 顶部小节尺（在视口上方，绝不压歌词） */
    const ry = y0 - 24;
    Ln(ctx, ax, ry + 7, ax + aw, ry + 7, COL.wht, 0.18, 1);
    for (let b = 0; b <= nBars; b += 4) {
      const x = ax + b * barPx; if (x > ax + aw) break;
      Ln(ctx, x, ry + 1, x, ry + 7, COL.wht, 0.22, 1);
      Tx(ctx, String(b / 4 + 1), x + 3, ry - 5, 9, COL.wht, 0.32, 500, 'left', 0.6);
    }
    /* ③ 播放头 */
    const phx = ax + clamp(tsong / A.dur, 0, 1) * aw;
    /* 时间读数（右端） */
    Tx(ctx, timeStr(tsong) + ' / ' + timeStr(A.dur), x0 + w, ry - 5, 9, COL.acc, 0.55, 600, 'right');

    /* ④ 编排概览：6 轨 × 段落块（块有无 = 该段该轨实际能量，不是随机装饰） */
    const laneH = 13, gap = 5, obH = LANES.length * (laneH + gap) - gap;
    const obY = y0 + h - obH - 58;
    Tx(ctx, 'ARRANGEMENT', x0, obY - 13, 9, COL.wht, 0.34, 600, 'left', 1.8);
    Tx(ctx, 'BPM ' + A.bpm.toFixed(1) + ' · 4/4', x0 + w, obY - 13, 9, COL.wht, 0.26, 500, 'right', 0.4);

    for (let li = 0; li < LANES.length; li++) {
      const ln = LANES[li], ly = obY + li * (laneH + gap);
      const col = ln.k === 'acc' ? COL.acc : (ln.k === 'ice' ? COL.ice : COL.wht);
      Rc(ctx, x0, ly + laneH / 2 - 2.5, 3, 5, 1, col, 0.55);            // 轨道色标
      Tx(ctx, ln.n, x0 + 62, ly + laneH / 2, 8.5, COL.wht, 0.34, 600, 'right', 1.0);
      for (const sec of A.sections) {
        const i0 = fi(sec[0]), i1 = Math.max(i0 + 1, fi(sec[1]));
        let sum = 0, cnt = 0;
        for (let i = i0; i < i1; i += 3) { sum += ln.fn(i); cnt++; }
        const act = cnt ? sum / cnt : 0;
        if (act < 0.15) continue;
        const bx = ax + sec[0] * pps;
        const bw = Math.max(4, (sec[1] - sec[0]) * pps - 2);
        const play = (tsong >= sec[0] && tsong < sec[1]);
        Rc(ctx, bx, ly, bw - 1.5, laneH, 2.5, col,
           clamp(0.14 + act * 0.34, 0, 0.50) * (play ? 1.3 : 0.8));
        /* 块顶 1px 高光 = clip 的「标题条」，段落边界因此一眼可辨 */
        Ln(ctx, bx + 1.5, ly + 1.2, bx + bw - 3.5, ly + 1.2, COL.wht, play ? 0.30 : 0.16, 1);
        /* clip 内部按「小节」画能量柱 —— 纯色条读不出编排，加了这层才像真 clip：
           全曲 134 小节摊在 844px 上 ≈ 6.3px/小节，正好是 DAW 缩小视图那种纹理密度。 */
        const bb0 = Math.floor(sec[0] / A.barSec), bb1 = Math.ceil(sec[1] / A.barSec);
        for (let bb = bb0; bb < bb1; bb++) {
          const t0 = bb * A.barSec, t1 = Math.min(sec[1], t0 + A.barSec);
          if (t1 - t0 < 0.2) continue;
          const j0 = fi(t0), j1 = Math.max(j0 + 1, fi(t1));
          let s2 = 0, c2 = 0;
          for (let j = j0; j < j1; j += 2) { s2 += ln.fn(j); c2++; }
          const av = c2 ? s2 / c2 : 0;
          const hh = clamp(av, 0, 1) * (laneH - 4.5);
          if (hh < 0.8) continue;
          Rc(ctx, bx + (t0 - sec[0]) * pps + 0.6, ly + laneH - 1.8 - hh,
             Math.max(0.9, (t1 - t0) * pps - 1.6), hh, 0.6, col,
             clamp(0.10 + av * 0.30, 0, 0.44) * (play ? 1.25 : 0.75));
        }
      }
    }
    /* 段落竖分隔（贯穿 6 条轨道） */
    for (const sec of A.sections) {
      const x = ax + sec[0] * pps;
      Ln(ctx, x, obY - 4, x, obY + obH + 4, COL.wht, 0.13, 1);
    }
    /* 段落字母 */
    for (const sec of A.sections) {
      const bx = ax + sec[0] * pps;
      const bw = Math.max(4, (sec[1] - sec[0]) * pps - 2);
      if (bw < 18) continue;
      Tx(ctx, sec[2], bx + 4, obY + obH + 9, 8, COL.wht, 0.34, 600, 'left', 0.9);
    }
    /* ③-b 播放头必须最后画：编排块是实心矩形，先画会被整片盖住（只剩上下两截白线） */
    Ln(ctx, phx, ry - 1, phx, y0 + h, COL.acc, 0.62, 1.8);
    ctx.globalAlpha = 0.90; ctx.fillStyle = COL.acc;
    ctx.beginPath(); ctx.moveTo(phx - 5, ry - 9); ctx.lineTo(phx + 5, ry - 9);
    ctx.lineTo(phx, ry - 1.5); ctx.closePath(); ctx.fill();

    /* ⑤ 主输出频谱（视口最底部） */
    const sy = y0 + h - 3, sh = 24;
    const bw2 = w / A.nb, i = fi(t);
    for (let b = 0; b < A.nb; b++) {
      const v = band(i, b), bh = Math.max(0.7, v * sh);
      const col = b < A.nb * 0.35 ? COL.acc : (b < A.nb * 0.7 ? COL.ice : COL.wht);
      Rc(ctx, x0 + b * bw2, sy - bh, Math.max(1.5, bw2 - 1.3), bh, 0.6, col, 0.24 + v * 0.36);
    }
  }

  /* ================================================================
   * 方案 B：频谱分析 + 旋律走向（钢琴卷帘 + F0 曲线 + 峰值保持）
   * ================================================================ */
  function bandIndexForHz(hz) {
    const A = P.A; if (!A.bandHz || A.bandHz.length < 2) return -1;
    for (let b = 0; b < A.bandHz.length - 1; b++)
      if (hz >= A.bandHz[b] && hz < A.bandHz[b + 1]) return b;
    return -1;
  }

  function drawSpec(o) {
    const { ctx, LY, COL } = o, A = P.A, t = o.t;
    const x0 = LY.x, w = LY.w;
    const back = 10, fwd = 3, span = back + fwd;
    const phx = x0 + w * (back / span);
    const xOf = (tt) => x0 + w * ((tt - (t - back)) / span);

    const m0 = A.pchLo, m1 = A.pchHi;          // 纵轴 = 本曲人声实际音域（自动适配）
    const y0 = LY.y + 30, y1 = LY.y + LY.h - 104;
    const yOf = (m) => y1 - (m - m0) / (m1 - m0) * (y1 - y0);

    /* ① 钢琴卷帘（半音线 + C 音加亮 + 音名） */
    for (let m = m0; m <= m1; m++) {
      const y = yOf(m), isC = (m % 12 === 0);
      Ln(ctx, x0, y, x0 + w, y, COL.wht, isC ? 0.105 : 0.030, isC ? 1.1 : 0.8);
      if (isC) Tx(ctx, noteName(m), x0 + w - 2, y - 5, 8.5, COL.wht, 0.30, 500, 'right');
    }
    /* ② 小节竖线：必须接在全曲小节网格上（b 是「全曲第几小节」），
       否则每 4 小节加亮的位置会随切换片段漂移，看起来像随机亮线。 */
    const b0 = Math.floor((t - back + A.off) / A.barSec) - 1;
    const b1 = Math.ceil((t + fwd + A.off) / A.barSec) + 1;
    for (let b = b0; b <= b1; b++) {
      const x = xOf(b * A.barSec - A.off);
      if (x < x0 - 2 || x > x0 + w + 2) continue;
      const four = ((b % 4) + 4) % 4 === 0;
      Ln(ctx, x, y0, x, y1, COL.wht, four ? 0.100 : 0.032, four ? 1.1 : 0.8);
    }

    /* ③ 旋律走向（真实 F0）：先画一层柔光，再画主线
       ⚠ 帧号必须叠加 A.off：iA/iB 是「全曲帧号」，xOf 收的是「片段内时间」。
       早期版本漏了 off ⇒ 取到全曲 0~11s 的纯前奏（pyin 无 F0）⇒ pts 全 null，
       旋律线整段消失，而网格/播放头/FFT 都正常 —— 这种「只少一条线」最难察觉。 */
    const iA = clamp(Math.floor((t - back + A.off) * A.fps), 0, A.N - 1);
    const iB = clamp(Math.ceil((t + fwd + A.off) * A.fps), 0, A.N - 1);
    const pts = [];
    for (let i = iA; i <= iB; i++) {
      const m = pitchDisp(i);
      if (isFinite(m)) pts.push([xOf(i / A.fps - A.off), yOf(m)]);
      else pts.push(null);
    }
    const stroke = (col, alpha, lw) => {
      ctx.globalAlpha = alpha; ctx.strokeStyle = col; ctx.lineWidth = lw;
      ctx.lineJoin = 'round'; ctx.lineCap = 'round';
      let open = false;
      ctx.beginPath();
      for (const p of pts) {
        if (!p) { open = false; continue; }
        if (!open) { ctx.moveTo(p[0], p[1]); open = true; }
        else ctx.lineTo(p[0], p[1]);
      }
      ctx.stroke();
      /* 暂停点圆角连接会留缝：补一遍圆点 */
      if (lw > 2.4) {
        ctx.globalAlpha = alpha; ctx.fillStyle = col;
        for (const p of pts) if (p) { ctx.beginPath(); ctx.arc(p[0], p[1], lw / 2, 0, 6.284); ctx.fill(); }
      }
    };
    stroke(COL.acc, 0.11, 7);      // 光晕
    stroke(COL.acc, 0.52, 2.4);    // 主线（0.62 时压过歌词字形，退到 0.52）

    /* 当前音读数放右上 HUD，不要在画布中央飘字（会压歌词）。
       现场调音的读法：音名 + 音分偏离，比只给音名更像专业表头。 */
    const curM = pitchDisp(fi(t));
    if (isFinite(curM)) {
      const cy = yOf(curM);
      ctx.globalAlpha = 0.85; ctx.fillStyle = COL.acc;
      ctx.beginPath(); ctx.arc(phx, cy, 3.2, 0, 6.284); ctx.fill();
      ctx.globalAlpha = 0.18; ctx.beginPath(); ctx.arc(phx, cy, 9, 0, 6.284); ctx.fill();
    }
    const cent = isFinite(curM) ? Math.round((curM - Math.round(curM)) * 100) : 0;
    /* 无音时不占位：左侧已经写了 SILENT，再挂一个「—— SILENT」纯属噪声 */
    if (isFinite(curM)) {
      Tx(ctx, noteName(curM) + ' ' + (cent >= 0 ? '+' : '') + cent + '¢',
         x0 + w, LY.y + 9, 11, COL.acc, 0.62, 600, 'right');
    }
    /* VOICED/SILENT 直接取「这一帧画得出音符吗」，不再看 pyin 的 voiced_flag：
       flag 与 F0 之间存在单帧错位，会出现「线明明画着、标签写着 SILENT」的矛盾。 */
    Tx(ctx, isFinite(curM) ? 'PITCH TRACK · VOICED' : 'PITCH TRACK · SILENT',
       x0, LY.y + 10, 9, COL.wht, 0.34, 600, 'left', 1.6);
    Tx(ctx, 'BPM ' + A.bpm.toFixed(1) + ' · ' + sectionAt(t + A.off)[2],
       x0 + w - 92, LY.y + 10, 9, COL.wht, 0.28, 600, 'right', 1.2);

    /* ④ 播放头 */
    Ln(ctx, phx, y0 - 12, phx, y1 + 8, COL.ice, 0.30, 1.2);

    /* ⑤ 底部 FFT + 峰值保持帽 + 频段标尺 */
    const sy = LY.y + LY.h - 8, sh = 62, nb = A.nb, bw = w / nb, i = fi(t);
    for (let b = 0; b < nb; b++) {
      const v = band(i, b), bh = Math.max(0.8, v * sh), cx = x0 + b * bw;
      const g = ctx.createLinearGradient(0, sy - sh, 0, sy);
      g.addColorStop(0, COL.ice); g.addColorStop(0.6, COL.iceDeep); g.addColorStop(1, COL.acc);
      Rc(ctx, cx, sy - bh, Math.max(1.7, bw - 1.5), bh, 0.8, g, 0.30 + v * 0.38);
      let pk = 0;
      for (let k = 0; k < 40; k++) { const ii = i - k; if (ii < 0) break; pk = Math.max(pk, band(ii, b)); }
      Rc(ctx, cx, sy - Math.max(1, pk * sh) - 3, Math.max(1.7, bw - 1.5), 1.6, 0.8, COL.wht, 0.34);
    }
    for (const hz of [100, 500, 1000, 4000, 10000]) {
      const bi = bandIndexForHz(hz);
      if (bi < 0) continue;
      const x = x0 + bi * bw;
      Ln(ctx, x, sy - 4, x, sy, COL.wht, 0.30, 1);
    }
  }

  /* ================================================================
   * 方案 C：虚拟歌姬调教台（音符块 + DYN 参数轨 + 声码器矩阵）
   * ================================================================ */
  function drawVocal(o) {
    const { ctx, LY, COL } = o, A = P.A, t = o.t;
    const x0 = LY.x, w = LY.w;
    const back = 9, fwd = 3, span = back + fwd;
    const phx = x0 + w * (back / span);
    const xOf = (tt) => x0 + w * ((tt - (t - back)) / span);

    const m0 = A.pchLo, m1 = A.pchHi;          // 同方案 B：纵轴跟随本曲音域
    const y0 = LY.y + 26, y1 = LY.y + LY.h - 172;
    const yOf = (m) => y1 - (m - m0) / (m1 - m0) * (y1 - y0);

    for (let m = m0; m <= m1; m++) {
      const y = yOf(m), isC = (m % 12 === 0);
      Ln(ctx, x0, y, x0 + w, y, COL.wht, isC ? 0.090 : 0.026, isC ? 1.1 : 0.8);
      if (isC) Tx(ctx, noteName(m), x0 + w - 2, y - 5, 8.5, COL.wht, 0.28, 500, 'right');
    }
    const b0 = Math.floor((t - back + A.off) / A.barSec) - 1;
    const b1 = Math.ceil((t + fwd + A.off) / A.barSec) + 1;
    for (let b = b0; b <= b1; b++) {
      const x = xOf(b * A.barSec - A.off);
      if (x < x0 - 2 || x > x0 + w + 2) continue;
      Ln(ctx, x, y0, x, y1, COL.wht, ((b % 4) + 4) % 4 === 0 ? 0.090 : 0.028, 1);
    }

    /* ① 音符块：F0 量化到半音 → 合并同音（容 4 帧断续）→ 圆角块 */
    const iA = clamp(Math.floor((t - back + A.off) * A.fps), 0, A.N - 1);
    const iB = clamp(Math.ceil((t + fwd + A.off) * A.fps), 0, A.N - 1);
    /* 带滞回的音符切分：新音高必须「连续 6 帧（100ms）稳定成立」才换音，
       否则视作颤音继续拖尾 —— 否则 5Hz 的 vibrato 会把每个音切成十几段虚线。
       断音（pitchDisp = NaN）超过 5 帧才真正结束当前音。 */
    let run = null, cand = null, candLen = 0, gaps = 0;
    const blocks = [];
    for (let i = iA; i <= iB; i++) {
      const m = pitchNoteAt(i);
      if (!isFinite(m)) {
        gaps++;
        if (run && gaps > 5) { if (run.t1 - run.t0 >= 3) blocks.push(run); run = null; cand = null; }
        continue;
      }
      gaps = 0;
      const mm = Math.round(m);
      if (!run) { run = { m: mm, t0: i, t1: i }; cand = null; continue; }
      if (mm === run.m) { run.t1 = i; cand = null; continue; }
      if (cand && cand.m === mm) candLen++;
      else { cand = { m: mm }; candLen = 1; }
      if (candLen >= 6) {                                   // 换音确认
        if (run.t1 - run.t0 >= 3) blocks.push(run);
        run = { m: mm, t0: i - 5, t1: i };
        cand = null;
      } else run.t1 = i;                                    // 未确认 → 继续拖尾
    }
    if (run && run.t1 - run.t0 >= 3) blocks.push(run);
    /* 先用一条细连线把相邻音符串起来（读作旋律轮廓；Vocaloid 里对应 portamento 段） */
    let lastPt = null;
    for (const b of blocks) {
      if (b.m < m0 - 1 || b.m > m1 + 1) { lastPt = null; continue; }
      const cx = xOf(((b.t0 + b.t1) / 2) / A.fps - A.off), cy = yOf(b.m);
      if (lastPt && Math.abs(cx - lastPt[0]) < w * 0.25)
        Ln(ctx, lastPt[0], lastPt[1], cx, cy, COL.acc, 0.22, 1.2);
      lastPt = [cx, cy];
    }
    for (const b of blocks) {
      if (b.m < m0 - 1 || b.m > m1 + 1) continue;          // 音域外的音（多为低频误判）不画
      const xa = xOf(b.t0 / A.fps - A.off), xb = xOf(b.t1 / A.fps - A.off);
      const bw = Math.max(5, xb - xa), y = yOf(b.m);
      const cur = (t >= b.t0 / A.fps - A.off && t < b.t1 / A.fps - A.off);
      Rc(ctx, xa, y - 9.5, bw, 19, 4, cur ? COL.acc : COL.ice, cur ? 0.62 : 0.34);
      Ln(ctx, xa + 1.5, y, xa + bw - 1.5, y, COL.wht, cur ? 0.55 : 0.28, 1.2);
      if (bw > 34) Tx(ctx, noteName(b.m), xa + 5, y, 8.5, COL.wht, 0.55, 600);
    }

    /* ② DYN 参数轨（真实响度，本曲自适应刻度） */
    const dy1 = LY.y + LY.h - 92, dh = 46, dy0 = dy1 - dh;
    Ln(ctx, x0, dy1, x0 + w, dy1, COL.wht, 0.16, 1);
    /* 轨道底纹 + 中线，读作「参数自动化 lane」而不是一块色板 */
    Rc(ctx, x0, dy0, w, dh, 2, COL.wht, 0.018);
    Ln(ctx, x0, dy0 + dh * 0.5, x0 + w, dy0 + dh * 0.5, COL.wht, 0.05, 1);
    /* 用「人声频段能量」而不是整轨 RMS：母带响度几乎恒定，RMS 画出来是一块贴顶平板
       （实测就是一条实心色带），而人声频段能量能清楚看到换气与分句。 */
    const voc = (tt) => {
      const i = fi(tt);
      return clamp((bandRange(i, 18, 34) - A.voxLo) / (A.voxHi - A.voxLo), 0, 1);
    };
    /* 填充用垂直渐变（线处亮、到基线衰减）：平铺单色在深底上会糊成一片脏色。 */
    const dg = ctx.createLinearGradient(0, dy0, 0, dy1);
    dg.addColorStop(0, 'rgba(255,176,103,0.30)');
    dg.addColorStop(0.55, 'rgba(255,176,103,0.13)');
    dg.addColorStop(1, 'rgba(255,176,103,0.02)');
    ctx.globalAlpha = 1; ctx.fillStyle = dg;
    ctx.beginPath(); ctx.moveTo(x0, dy1);
    for (let x = 0; x <= w; x += 3) ctx.lineTo(x0 + x, dy1 - voc(t - back + (x / w) * span) * dh);
    ctx.lineTo(x0 + w, dy1); ctx.closePath(); ctx.fill();
    ctx.globalAlpha = 0.58; ctx.strokeStyle = COL.acc; ctx.lineWidth = 1.4;
    ctx.beginPath();
    for (let x = 0; x <= w; x += 3) {
      const y = dy1 - voc(t - back + (x / w) * span) * dh;
      if (x === 0) ctx.moveTo(x0 + x, y); else ctx.lineTo(x0 + x, y);
    }
    ctx.stroke();
    for (let k = 0; k <= Math.round(span * 4); k++) {
      const tt = t - back + k * 0.25, x = xOf(tt);
      if (x < x0 || x > x0 + w) continue;
      ctx.globalAlpha = 0.40; ctx.fillStyle = COL.wht;
      ctx.beginPath(); ctx.arc(x, dy1 - voc(tt) * dh, 1.6, 0, 6.284); ctx.fill();
    }
    Tx(ctx, 'DYN', x0 - 10, dy0 + dh / 2, 8.5, COL.wht, 0.34, 600, 'right', 1.2);
    Tx(ctx, 'VOX 300~3.5k', x0 - 10, dy0 + dh / 2 + 12, 7.5, COL.wht, 0.22, 500, 'right', 0.3);
    /* 当前值读数（右对齐，避免压在曲线尖端上） */
    Tx(ctx, (voc(t) * 100).toFixed(0).padStart(3, ' ') + '%',
       x0 + w - 2, dy0 + 8, 8.5, COL.acc, 0.50, 600, 'right');

    /* ③ 声码器矩阵（12×8，亮度取对应频段） */
    const gw = 7, gh = 7, gx = x0 + w - 12 * gw - 2, gy = LY.y + 4, i = fi(t);
    for (let r = 0; r < 8; r++) {
      for (let c = 0; c < 12; c++) {
        const bi = Math.round((r * 12 + c) * (A.nb - 1) / 95);
        const v = band(i, bi);
        Rc(ctx, gx + c * gw, gy + r * gh, gw - 1.6, gh - 1.6, 1,
           v > 0.55 ? COL.acc : COL.ice, 0.07 + v * 0.42);
      }
    }
    Tx(ctx, 'VOCAL SYNTH · 96 CELL', gx + 12 * gw, gy + 8 * gh + 8, 8, COL.wht, 0.28, 600, 'right', 1.0);

    /* ④ 播放头 */
    Ln(ctx, phx, y0 - 6, phx, dy1 + 6, COL.acc, 0.48, 1.5);
  }

  /* ---------------- 入口 ---------------- */
  P.draw = function (o) {
    if (!P.ok || P.mode === 'none' || !o || !o.ctx) return;
    const C = {
      ctx: o.ctx, LY: o.LY, t: o.t,
      /* 固定语义色，**不跟随封面取色**：封面取色出来的 accent 是「主题色」，
         而这里需要的是「数据语义色」——人声永远琥珀、频谱永远冰蓝。
         实测用 PAL.accent 时，绿黑封面取到冷白 ⇒ 旋律线成了一条白线。 */
      COL: {
        acc: o.BRAND.rose,                                   // #FFB067 人声/旋律
        ice: 'rgba(' + o.BRAND.iceRGB.join(',') + ',1)',      // #8FE3FF 频谱
        iceDeep: 'rgba(' + o.BRAND.iceDeep.join(',') + ',1)',
        wht: 'rgba(214,226,240,1)',
      },
    };
    o.ctx.save();
    try {
      if (P.mode === 'daw') drawDaw(C);
      else if (P.mode === 'spec') drawSpec(C);
      else if (P.mode === 'vocal') drawVocal(C);
    } finally {
      o.ctx.globalAlpha = 1;
      o.ctx.restore();
    }
  };
})();
