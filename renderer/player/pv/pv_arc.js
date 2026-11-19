/*! pv_arc.js —— 风格弧线（style arc）的运行时一侧。
 *
 *  这一层只做两件事：
 *    1. 把编排稿里的 `sb.arc.keys` 插值成 t 时刻的风格坐标（连续）；
 *    2. 按坐标做两件可见的事 —— 调色板加权混合、整幅极薄的色温/明暗/颗粒叠加。
 *
 *  为什么「换纸」不在这里做：纸底是**一次性烘焙**的（pv_engine.js 的 paperCanvas），
 *  逐帧换纸色会把 Chromium 的抖动相位带进来，同一时刻两次渲染差 ±1~2，
 *  确定性验收直接挂。所以换纸仍然发生在镜头边界（编排层把 scheme 写进 shot.pal），
 *  这一层负责的是「镜头之内也一直在缓慢变化」—— 也就是观众感觉到的「平滑」。
 *
 *  两者的分工可以一句话说清：
 *    编排层决定「用什么纸」，这一层决定「纸此刻有多暖、多亮、多糙」。
 */
(() => {
  'use strict';
  const PV = window.PV;

  const DIM = ['lum', 'warm', 'dense', 'motion', 'grain'];
  const NEUTRAL = { lum: 0.34, warm: 0.44, dense: 0.52, motion: 0.48, grain: 0.50 };

  let ARC = null;

  /* 载入编排稿的弧线。没有 arc 字段时返回 null —— 旧编排稿逐字节不变。 */
  PV.setArc = function (arc) {
    if (!arc || !arc.keys || !arc.keys.length) { ARC = null; return null; }
    ARC = {
      keys: arc.keys.slice().sort((a, b) => a.t - b.t),
      has: true,
    };
    return ARC;
  };
  PV.arcOn = () => !!ARC;

  /* 任意 t 的坐标。端点外保持常量（不外推）—— 外推会让片头/片尾跑到合法域之外。 */
  PV.arcAt = function (t) {
    const out = Object.assign({}, NEUTRAL);
    if (!ARC) return out;
    const k = ARC.keys;
    if (t <= k[0].t) { for (const d of DIM) out[d] = k[0][d]; return out; }
    if (t >= k[k.length - 1].t) { for (const d of DIM) out[d] = k[k.length - 1][d]; return out; }
    for (let i = 0; i < k.length - 1; i++) {
      const a = k[i], b = k[i + 1];
      if (a.t <= t && t < b.t) {
        const raw = (t - a.t) / Math.max(1e-6, b.t - a.t);
        /* smoothstep 与编排层 pv_arc.py 的 build() 完全一致。
           两边必须用同一条曲线：编排层算 pal 与换纸时刻，运行时算 tone，
           两处若要错开一个缓动族，就会出现「纸已经换了、色温还在路上」的错配。 */
        const q = raw * raw * (3 - 2 * raw);
        for (const d of DIM) out[d] = a[d] + (b[d] - a[d]) * q;
        return out;
      }
    }
    for (const d of DIM) out[d] = k[k.length - 1][d];
    return out;
  };

  /* ---------------- 调色板加权混合 ---------------- */
  const rgbHex = c => '#' + c.map(v => {
    const s = Math.max(0, Math.min(255, Math.round(v))).toString(16);
    return s.length < 2 ? '0' + s : s;
  }).join('');

  const lum = c => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];

  /* 九角色 + glow。混合时逐角色独立加权 —— 不是「先选一套再调一个色」，
     因为底色和字色必须一起动，只动一个会把对比度吃掉。 */
  const ROLES = ['bg', 'fg', 'sub', 'accent', 'accent2', 'ink', 'dim',
                 'ghostA', 'ghostB', 'glow'];

  /* mix = [[idx, w], ...]。只有一个元素时等价于 STYLE(pack, idx)。
   *
   * 跨深/浅保护：两套 bg 亮度差 > 0.30 时**不混**，直接取权重大的那套。
   * 这不是保险丝，是设计判断 —— 夜景与月白之间没有「中间色」，
   * 线性混出来的是灰，而灰底既不读作夜也不读作纸，且字与底的分离度同时下降。
   * 需要「夜 → 月白」这种大跳时，正确做法是让转场去承接，不是让颜色去调停。
   */
  PV.STYLE_MIX = function (pack, mix) {
    const p = PV.PACKS[pack] || PV.PACKS.noir;
    if (!mix || mix.length < 2) {
      const one = mix && mix.length === 1 ? (mix[0][0] | 0) : 0;
      return PV.STYLE(pack, one);
    }
    let tot = 0;
    for (const m of mix) tot += Math.max(0, m[1]);
    if (tot <= 0) return PV.STYLE(pack, mix[0][0] | 0);

    const picked = [];
    for (const m of mix) {
      const idx = ((m[0] | 0) % p.schemes.length + p.schemes.length) % p.schemes.length;
      picked.push({ s: p.schemes[idx], w: Math.max(0, m[1]) / tot });
    }
    /* 深浅保护：最大权重那套与任何一套的 bg 亮度差过大 → 整体退回纯色。
       返回 null 让调用方退回 STYLE(pack, 最大权重那套)。 */
    const lead = picked.reduce((a, b) => (b.w > a.w ? b : a), picked[0]);
    for (const q of picked) {
      if (q === lead) continue;
      const d = Math.abs(lum(PV.hex(q.s.bg)) - lum(PV.hex(lead.s.bg))) / 255;
      if (d > 0.30) (PV.__mixDrop = PV.__mixDrop || []).push(pack + ':' + d.toFixed(3));
      if (d > 0.30) return null;
    }

    const out = {};
    for (const role of ROLES) {
      let r = 0, g = 0, b = 0, wsum = 0;
      for (const q of picked) {
        const hx = q.s[role];
        if (!hx) continue;                       // 某套没写 glow → 不参与这一role的加权
        const c = PV.hex(hx);
        r += c[0] * q.w; g += c[1] * q.w; b += c[2] * q.w; wsum += q.w;
      }
      if (wsum > 0) out[role] = rgbHex([r / wsum, g / wsum, b / wsum]);
      else out[role] = lead.s[role];
    }
    /* 用混合后的 bg 重新解算一次（PV.STYLE 会顺手填 paper/ink/void 等旧字段，
       engine 其余部分还在读它们；只写 role 不够）。 */
    const base = PV.STYLE(pack, mix[0][0] | 0);
    for (const role of ROLES) if (out[role]) base[role] = out[role];
    base.paper = out.bg; base.ink = out.fg; base.void = out.dim; base.paper2 = out.dim;
    base.mixed = mix.map(m => m[0]);
    return base;
  };

  /* ---------------- 逐帧 tone ----------------
   * 幅度上限 0.075：这一层是「胶水」，不是「滤镜」。
   * 超过这个量，暖色叠加会开始吃掉字与底的分离度 —— 实测 0.10 时深底上的
   * 冷白字边缘出现一圈橙边，正是 RULES 里「空心字」那条判据的观感。
   *
   * 全部用 source-over + 纯色 + 固定 alpha：确定性由「纯色 + 常量」直接保证，
   * 不需要额外的抖动/噪声，也不会因为平台合成差异（soft-light / overlay
   * 在不同 GPU 上的实现有细微差别）而在两种机器上出不同的画面。
   */
  const WARM = [255, 168, 96];
  const COOL = [104, 146, 255];

  PV.drawArcTone = function (ctx, t, opt) {
    if (!ARC) return null;
    const A = PV.arcAt(t);
    ctx.save();
    ctx.globalCompositeOperation = 'source-over';
    /* 色温：以 0.5 为界，暖往赭推、冷往蓝推，两端各 0.075 上限 */
    const dw = (A.warm - 0.5) * 2;                 // -1..1
    if (Math.abs(dw) > 0.02) {
      const c = dw > 0 ? WARM : COOL;
      ctx.globalAlpha = Math.min(0.075, Math.abs(dw) * 0.075);
      ctx.fillStyle = PV.rgba(c, 1);
      ctx.fillRect(0, 0, PV.W, PV.H);
    }
    /* 明暗：lum 低就往黑里压，高就往白里提。两端不同幅度 ——
       提亮比压暗更容易「洗掉」暗部的细节，所以上半段给得更小。 */
    const dl = (A.lum - 0.5) * 2;
    if (Math.abs(dl) > 0.02) {
      if (dl < 0) {
        ctx.globalAlpha = Math.min(0.070, -dl * 0.070);
        ctx.fillStyle = 'rgba(0,0,0,1)';
      } else {
        ctx.globalAlpha = Math.min(0.045, dl * 0.045);
        ctx.fillStyle = 'rgba(255,255,255,1)';
      }
      ctx.fillRect(0, 0, PV.W, PV.H);
    }
    ctx.restore();
    return A;
  };

  /* 颗粒强度：drawPaper 之外的颗粒层由 renderAt 调用，这里只给数。
     基准 0.55（引擎原值），grain 维度在 0.35~0.80 之间摆动 ——
     幅度刻意小：颗粒是「纸的性格」，不是「每句都要换的滤镜」。 */
  PV.arcGrain = function (t, base) {
    if (!ARC) return base;
    const A = PV.arcAt(t);
    return base * (0.72 + 0.56 * A.grain);
  };
})();
