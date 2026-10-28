/*! jz_adapter.js —— 把 JIZURA 字面引擎（MIT, (c) 2026 hakoniwa, github.com/852wa）当作「歌词动效层」驱动。
 *
 *  本文件是我们自己的代码（不是上游）：上游只提供 J.plan / J.Renderer 两个入口，
 *  这里把它们包成「时间 t 的纯函数」，好让我们的 PV 导演层在任意帧号取图。
 *
 *  加载顺序（缺一不可）：
 *    1. <script> 播下确定性 Math.random（mulberry32）—— 必须在引擎之前，
 *       因为 J.Renderer 构造时会用 Math.random 生成 grain/paper 纸纹贴图，
 *       不同随机序列 ⇒ 不同纸纹 ⇒ 同一帧两次渲染不一致。
 *    2. jizura_engine.js
 *    3. 本文件
 *
 *  API：
 *    JZ.setAudio({ duration, beats:[s…], bpm })     喂我们自己的 librosa 拍点，不用 JIZURA 自带检测
 *    await JZ.addPlan(name, project)               建一个 plan（= 一个 JIZURA 工程），返回摘要
 *    JZ.drawPlan(name, ctx, t, mode)               在 ctx 上画第 t 秒，mode: front|back|transparent|normal
 *    JZ.cutsOf(name)                               规划出的分镜表（给导演层做时间轴用）
 *    JZ.linesOf(name)                              歌词行表（带起止时间）
 */
(() => {
  'use strict';

  const HUD_CHARS = '0123456789:./-_()【】・No.LYRICRECUNTITLEDXYlinebpminterlude—─／ ';
  const S = { audio: null, plans: {}, ready: false, version: (typeof J !== 'undefined' && J.VERSION) || '?' };

  function mergeProject(p) {
    const base = J.defaultProject();
    const out = Object.assign({}, base, p || {});
    out.fx = Object.assign({}, base.fx, (p && p.fx) || {});
    out.timing = Object.assign({}, base.timing, (p && p.timing) || {});
    if (p && p.enabled) {
      // 深合并：只关掉我们点名的部分，其余保持默认全开
      out.enabled = JSON.parse(JSON.stringify(base.enabled));
      for (const [g, m] of Object.entries(p.enabled)) {
        Object.assign(out.enabled[g] || (out.enabled[g] = {}), m);
      }
    }
    return out;
  }

  const optsFor = (mode, scale) => {
    if (mode === 'back' || mode === 'front') return { scale, transparent: true, layer: mode };
    if (mode === 'transparent') return { scale, transparent: true };
    return { scale };
  };

  const JZ = {
    state: S,
    get version() { return S.version; },

    setAudio(a) {
      S.audio = a ? {
        duration: a.duration,
        beats: Array.from(a.beats || []),
        energy: a.energy ? Float32Array.from(a.energy) : null,
        energyRate: a.energyRate || 0,
        bpm: a.bpm || 0,
      } : null;
      return !!S.audio;
    },

    async addPlan(name, p) {
      const project = mergeProject(p);
      const plan = J.plan(project, S.audio);

      /* schemes: 'dark' | 'light' | [索引…]
         深色片要「浅色字」的配色，浅色片要「深色字」的配色；JIZURA 每个 style 自带 2~4 套配色，
         分镜会轮着用，这里把每一镜的 scheme 重映射到子集里，避免深底上出现深墨字（实测看不见）。 */
      if (p.schemes === 'dark' || p.schemes === 'light' || Array.isArray(p.schemes)) {
        const pick = test => plan.style.schemes.map((s, i) => (test(J.lum(s.bg)) ? i : -1)).filter(i => i >= 0);
        const ok = Array.isArray(p.schemes) ? p.schemes
          : p.schemes === 'dark' ? pick(l => l < 0.25) : pick(l => l > 0.6);
        if (ok.length) for (const c of plan.cuts) c.scheme = ok[(c.scheme | 0) % ok.length];
      }

      const R = new J.Renderer();
      const text = String(project.lyrics || '') + (project.title || '') + (project.artist || '') + HUD_CHARS;
      // 字体必须先就位，否则第一帧用系统 fallback、后面变字形 ⇒ 同一帧两次渲染不一致
      await J.ensureFonts(text, J.fontsOfPlan(plan));
      J.glyphs.maxRes = 768;

      S.plans[name] = { project, plan, R, canvas: null, ctx: null };
      S.ready = true;
      return {
        name, style: plan.styleKey, lang: plan.lang,
        lines: plan.lines.length, cuts: plan.cuts.length, beats: plan.beats.length,
        duration: plan.duration, W: plan.W, H: plan.H, fps: plan.fps,
        fonts: J.fontsOfPlan(plan),
        missingFonts: J.fontsOfPlan(plan).filter(k => J.FONTS[k] && !document.fonts.check(J.fontCSS(k, 32), '字')),
      };
    },

    drawPlan(name, ctx2, t, mode = 'front') {
      const P = S.plans[name];
      if (!P) throw new Error('no such JIZURA plan: ' + name);
      P.R.frame(ctx2, P.plan, t, optsFor(mode, ctx2.canvas.width / P.plan.W));
    },

    /* 建一块离屏 canvas 并渲染，返回 canvas（探针/预览用；正式渲染走 drawPlan 直画主画布） */
    renderTo(name, t, mode = 'front') {
      const P = S.plans[name];
      if (!P) throw new Error('no such JIZURA plan: ' + name);
      const [w, h] = J.outputSize(P.project);
      if (!P.canvas) { P.canvas = document.createElement('canvas'); P.ctx = P.canvas.getContext('2d'); }
      if (P.canvas.width !== w || P.canvas.height !== h) { P.canvas.width = w; P.canvas.height = h; }
      P.ctx.setTransform(1, 0, 0, 1, 0, 0);
      P.ctx.clearRect(0, 0, w, h);
      P.R.frame(P.ctx, P.plan, t, optsFor(mode, 1));
      return P.canvas;
    },

    cutsOf(name) {
      const P = S.plans[name];
      return P.plan.cuts.map(c => ({
        i: c.index, line: c.line,
        start: +c.start.toFixed(3), end: +c.end.toFixed(3), text: c.text,
        layout: c.layout, enter: c.enter, hold: c.hold, exit: c.exit,
        bg: c.bg, cam: c.cam, trans: c.trans || null, scheme: c.scheme,
        decor: (c.decor || []).map(d => (d && (d.id || d.type)) || d),
      }));
    },

    linesOf(name) {
      const P = S.plans[name];
      return P.plan.lines.map(l => ({
        text: l.text, start: +l.start.toFixed(3), end: +l.end.toFixed(3), kind: l.kind || '',
      }));
    },

    /* 部件目录：给导演层做「可用部件白名单」用 */
    parts() {
      const out = {};
      for (const g of J.GROUP_KEYS) out[g] = J.order(g).slice();
      return out;
    },
  };

  window.JZ = JZ;
})();
