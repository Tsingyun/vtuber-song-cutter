/* direction.js -- the DIFFERENTIATION CORE for song-cut PVs.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * Upstream README:  "All of them are a toolbox to adapt, not a look to reuse."
 * Upstream 02 §10:  "pick one option per row from the song's own material;
 *                    if you collide with the example on more than 3 rows, push further."
 * Upstream 02 §12:  lists "re-using the zanko motifs" as an anti-pattern.
 *
 * The previous driver obeyed none of that: every song ran the SAME 10-act skeleton with the
 * SAME intensity curve, selected scenes with `(i*5 + ai*3) % pool.length`, picked fx with
 * `act.fx[(i*3+k) % n]`, used the same 6 JIZURA plans and the same 7 Z.LOOKS. Two songs could
 * only differ in palette / glyphs / plates / lyrics -- one row out of twelve. That is exactly
 * the "templated" result the user rejected.
 *
 * This file replaces the fixed skeleton with a per-song DIRECTION:
 *   - the act structure (how many acts, how long, how intense) is DERIVED from the song's own
 *     lyric timings + a per-song role pattern, with a per-song seed;
 *   - every choice (scene, fx, transition, plan, lut, post) is a seeded weighted draw, not a
 *     modulo walk, so identical vocabularies still produce different orderings;
 *   - the 12-axis matrix is materialised as a signature so tools/audit_official.mjs can PROVE
 *     that two songs differ on >= 6 axes (the audit refuses to render otherwise).
 *
 * Determinism: only Z.rng / Z.hash are used. No Date.now, no Math.random.
 * Load order: after core/util.js, before songcut.timeline.js.
 */
(() => {
  'use strict';
  const Z = window.Z;

  // ============================================================== the 12-axis matrix (02 §10)
  // README summarises 11 rows; 02 §10 has 12 (it adds key/exposure). Cadence and type density
  // are free axes called out in the same section, so the effective spread is 14.
  const AXES = [
    'world', 'timeArc', 'key', 'palette', 'motif', 'material',
    'role', 'type', 'trans', 'cadence', 'density', 'ending',
  ];
  const FREE_AXES = ['cadence', 'density'];

  const ROLES = ['intro', 'verse', 'pre', 'chorus', 'bridge', 'climax', 'outro', 'refrain', 'tag'];

  // Defaults are deliberately NEUTRAL: a song that forgets to declare a direction must not
  // silently inherit the zanko grammar (no embers, no cracks, no barrier, no crimson).
  const DEFAULTS = {
    seed: null,                       // derived from cfg.id when null
    structure: {
      mode: 'manual',                 // 'manual' honours cfg.acts; 'auto' derives from lyrics
      pattern: ['intro', 'verse', 'pre', 'chorus', 'verse', 'pre', 'chorus', 'bridge', 'climax', 'outro'],
      weights: { verse: 1.0, pre: 0.55, chorus: 1.25, bridge: 0.8, climax: 0.9, refrain: 0.7, tag: 0.45 },
      base: { intro: 0.18, verse: 0.36, pre: 0.52, chorus: 0.74, bridge: 0.34, climax: 0.92, outro: 0.2, refrain: 0.62, tag: 0.28 },
      jitter: 0.06,
      density: 2,                     // lyric lines per cut in a verse (1 = every line)
      maxHold: 6.4,                   // 01 §4: no shot may hold still longer than ~7 s
      minShot: 0.62,
      tailGap: 2.0,                   // last inner act ends this long after the final lyric line
    },
    lexicon: {
      // role -> candidate scenes. A void list falls through to `any`.
      scenes: {},
      any: ['illust', 'jz', 'solid', 'converge'],
      fx: {}, any: ['dust', 'glow', 'rays'],
      trans: ['cut', 'xfade'],
      recipes: [],                    // names from recipes.js this song may draw
    },
    type: {
      styles: ['gold'],               // 27 available; see J.STYLES
      schemes: 'dark',                // 'dark' | 'light' | [indices]
      plans: 5,                       // how many JIZURA plans to build (>= 5)
      planStyles: null,               // optional explicit [style, style, ...] per plan
      centerDir: 'tb',
      zoneW: null,
      fx: { motion: 0.7, glitch: 0.3, chroma: 0.4, decor: 0.4, density: 0.5, texture: 0.5, flash: false, koma: 12, hud: 'off', bgSwitch: 0.3 },
      colors: null,                   // { accentOn, accent, ghostA, ghostB }
      extra: true, wa: false, horror: false, typo: false, kinetic: false,
      deny: {},                       // per-film deny-list overrides (06 §5: "adapt per film")
    },
    grade: {
      lutPool: ['night', 'dawn'],
      base: { boil: 1.0, bloom: 0.3, bloomThreshold: 0.8, grain: 0.055, vignette: 0.34, lutMix: 0.0 },
      looks: null,                    // optional Z.LOOKS replacement
      exposure: 0,                    // key: negative = low-key, positive = high-key
      bloomCurve: [0.18, 0.38],       // [min, max] across intensity
      grainCurve: [0.04, 0.075],
      vignetteCurve: [0.16, 0.46],
      lutMixCurve: [0.08, 0.26],
      caBoost: 1.1,
    },
    opening: 'typeCard',              // typeCard | titleRiff | converge | ink | solid
    ending: 'afterimage',             // afterimage | typeCard | solid | tunnel
    char: {},                         // { items: [...] } -- user illustrations, one window each
                                      // (or { img, shots } to recur one image in several forms)
    axes: {},                         // declarative axis answers, used by the divergence audit
  };

  // deep-merge that keeps arrays by replacement (a song's pattern must not concatenate)
  function merge(base, over) {
    if (Array.isArray(over) || over === null || typeof over !== 'object') return over === undefined ? base : over;
    // A `null` default (DEFAULTS.char, DEFAULTS.axes.*) used to throw on `k in base`: a null
    // base is now treated as "nothing to inherit", which is what those defaults mean.
    const objBase = (base && !Array.isArray(base) && typeof base === 'object') ? base : {};
    const out = Object.assign({}, objBase);
    for (const k of Object.keys(over)) out[k] = (k in objBase) ? merge(objBase[k], over[k]) : over[k];
    return out;
  }

  // ---------------------------------------------------------------- seeded helpers
  const norm = o => Array.isArray(o) ? o.map(n => ({ v: n, w: 1 }))
    : Object.keys(o).map(k => ({ v: k, w: +o[k] }));

  function pick(list, rng) {
    const it = norm(list);
    if (!it.length) return null;
    let sum = 0; for (const x of it) sum += Math.max(0, x.w);
    if (sum <= 0) return it[0].v;
    let r = rng() * sum;
    for (const x of it) { r -= Math.max(0, x.w); if (r <= 0) return x.v; }
    return it[it.length - 1].v;
  }

  // weighted draw WITHOUT replacement -- an act's scene pool must not be 4 copies of one name
  function pickN(list, n, rng) {
    const it = norm(list).filter(x => x.w > 0);
    const out = [];
    while (out.length < n && it.length) {
      let sum = 0; for (const x of it) sum += x.w;
      let r = rng() * sum, k = 0;
      for (; k < it.length; k++) { r -= it[k].w; if (r <= 0) break; }
      k = Math.min(k, it.length - 1);
      out.push(it[k].v);
      it.splice(k, 1);
    }
    return out;
  }

  const dedupe = a => [...new Set(a)];
  const clampI = (x, a, b) => Math.max(a, Math.min(b, x));

  // ---------------------------------------------------------------- structure derivation
  // The act skeleton is derived from WHERE THE LYRICS ACTUALLY ARE, not from an even split:
  // each role gets a share of the lyric interval proportional to its weight, then every
  // boundary snaps to the nearest real lyric onset so no cut ever lands mid-phrase.
  function buildActs(cfg, lines) {
    const d = cfg.direction, st = d.structure, rng = Z.rng(d.seed ^ 0x9e3779b9);
    const total = +cfg.duration;
    if (!lines || lines.length < 4) return cfg.acts || [];

    const firstT = lines[0].t;
    const lastT = lines[lines.length - 1].t;
    const inner = st.pattern.filter(r => r !== 'intro' && r !== 'outro');
    if (!inner.length) return cfg.acts || [];

    const wts = inner.map(r => Math.max(0.05, (st.weights && st.weights[r]) ?? 1));
    const sumW = wts.reduce((a, b) => a + b, 0);
    const span = Math.max(4, lastT - firstT);
    const tailGap = st.tailGap ?? 2.0;

    // raw boundaries over [firstT, lastT + tailGap]
    const raw = [firstT];
    let cum = 0;
    for (let i = 0; i < inner.length; i++) {
      cum += wts[i];
      if (i === inner.length - 1) { raw.push(Math.min(total, lastT + tailGap)); break; }
      raw.push(firstT + span * (cum / sumW));
    }

    // snap interior boundaries to the nearest lyric onset (keeps cuts on real phrase edges)
    const times = lines.map(l => l.t);
    const snap = (x, lo, hi) => {
      let best = x, bd = 1e9;
      for (const t of times) {
        const dd = Math.abs(t - x);
        if (dd < bd && t > lo + 0.9 && t < hi - 0.9) { bd = dd; best = t; }
      }
      return bd <= 3.2 ? +best.toFixed(3) : +x.toFixed(3);
    };
    const b = [0];
    let prev = 0;
    for (let i = 1; i < raw.length; i++) {
      const isLast = i === raw.length - 1;
      let t = isLast ? raw[i] : snap(raw[i], prev, isLast ? total : lastT + tailGap);
      if (t - prev < 3.0) t = Math.min(total, prev + 3.0);            // no 2-second "section"
      b.push(+t.toFixed(3)); prev = t;
    }
    b[b.length - 1] = Math.min(total, Math.max(b[b.length - 2] + 1.5, b[b.length - 1]));

    const acts = [];
    // intro: everything before the first lyric line. Only worth its own act when the head is
    // long enough to breathe; a song that starts singing at 0.4 s must not get a 2.5 s intro
    // overlapping its first line.
    const introEnd = firstT > 1.2 ? Math.min(firstT, total * 0.18) : 0;
    if (introEnd > 0.8) acts.push(makeAct('intro', 0, +introEnd.toFixed(3)));
    for (let i = 0; i < inner.length; i++) {
      const t0 = i === 0 ? introEnd : b[i];
      const t1 = b[i + 1];
      if (t1 - t0 < 0.8) continue;
      acts.push(makeAct(inner[i], +t0.toFixed(3), +t1.toFixed(3), i));
    }
    // outro: everything after the last inner act
    const lastEnd = acts.length ? acts[acts.length - 1].t1 : 0;
    if (total - lastEnd > 1.0) acts.push(makeAct('outro', lastEnd, +total.toFixed(3)));

    return acts;

    function makeAct(role, t0, t1, idx = 0) {
      const baseI = (st.base && st.base[role]) ?? 0.4;
      const jit = ((st.jitter ?? 0) * (rng() * 2 - 1));
      const intensity = +clampI(baseI + jit, 0.10, 1.0).toFixed(3);
      const look = role === 'refrain' ? 'chorus' : (role === 'tag' ? 'outro' : role);
      const vocab = d.lexicon.scenes || {};
      const scenePool = (vocab[role] && vocab[role].length) ? vocab[role]
        : (vocab.any && vocab.any.length ? vocab.any : DEFAULTS.lexicon.any);
      const fxVocab = (d.lexicon.fx && d.lexicon.fx[role] && d.lexicon.fx[role].length) ? d.lexicon.fx[role]
        : (d.lexicon.fx && d.lexicon.fx.any ? d.lexicon.fx.any : DEFAULTS.lexicon.fx.any);
      const nScene = clampI(3 + Math.round(intensity * 2), 3, 5);
      const nFx = clampI(2 + Math.round(intensity * 2), 2, 4);
      const luts = d.grade.lutPool && d.grade.lutPool.length ? d.grade.lutPool : DEFAULTS.grade.lutPool;
      return {
        id: role + (idx ? String(idx) : ''),
        role, t0, t1, look, intensity,
        lut: luts[Math.floor(rng() * luts.length) % luts.length],
        cadence: cadenceFor(look, st.density, rng),
        pool: dedupe(pickN(scenePool, nScene, rng)),
        fx: dedupe(pickN(fxVocab, nFx, rng)),
        trans: dedupe(pickN(d.lexicon.trans && d.lexicon.trans.length ? d.lexicon.trans : DEFAULTS.lexicon.trans, 3, rng)),
      };
    }
  }

  // cadence = lyric lines per cut. The free axis 02 §10 names next to the matrix.
  function cadenceFor(look, density, rng) {
    const base = (look === 'chorus' || look === 'climax') ? 1
      : (look === 'intro' || look === 'outro') ? 3 : 2;
    const j = rng() < 0.25 ? (rng() < 0.5 ? -1 : 1) : 0;   // occasional half-bar surprise
    return clampI(base + j, 1, 4);
  }

  // ---------------------------------------------------------------- normalise a song config
  function normalize(cfg, songId) {
    const src = cfg.direction || {};
    const d = merge(DEFAULTS, src);
    d.seed = (d.seed == null) ? (Z.hash(...String(cfg.id || songId).split('').map(c => c.charCodeAt(0))) >>> 0) : d.seed;
    // 02 §13: a high-key film must not inherit the low-key post look
    if (d.grade.exposure > 0.15) {
      d.grade.base = Object.assign({}, d.grade.base, {
        bloom: Math.max(d.grade.base.bloom, 0.42),
        bloomThreshold: Math.max(0.88, d.grade.base.bloomThreshold),
        vignette: Math.min(d.grade.base.vignette, 0.16),
        grain: Math.min(d.grade.base.grain, 0.04),
      });
    }
    // a lexicon of one name would make every act identical -> widen it from `any`
    for (const role of ROLES) {
      const v = d.lexicon.scenes && d.lexicon.scenes[role];
      if (v && v.length === 1 && d.lexicon.any && d.lexicon.any.length > 1) {
        d.lexicon.scenes[role] = dedupe([...v, ...d.lexicon.any.slice(0, 2)]);
      }
    }
    cfg.direction = d;
    return d;
  }

  // ---------------------------------------------------------------- 12-axis signature
  // Materialises what the film actually decided, so two songs can be compared mechanically.
  // Used by tools/audit_official.mjs -> window.__cutAudit.direction.signature.
  function signature(cfg, acts) {
    const d = cfg.direction, A = d.axes || {};
    const roles = acts.map(a => a.role);
    const counts = {};
    for (const r of roles) counts[r] = (counts[r] || 0) + 1;
    const intensities = acts.map(a => a.intensity);
    const scenes = dedupe(acts.flatMap(a => a.pool || []));
    return {
      world: A.world || (d.axes && d.axes.world) || null,
      timeArc: A.timeArc || null,
      key: A.key || (d.grade.exposure > 0.15 ? 'high' : 'low'),
      palette: A.palette || (cfg.palette ? Object.values(cfg.palette).slice(0, 4).join('') : null),
      motif: A.motif || (cfg.motifs || []).slice(0, 3).join('/'),
      material: A.material || null,
      role: A.role || null,
      type: `${(d.type.styles || []).join('+')}|${d.type.schemes}|${d.type.plans}p`,
      trans: dedupe(acts.flatMap(a => a.trans || [])).sort().join(','),
      cadence: acts.map(a => a.cadence).join(''),
      density: String(d.structure.density),
      ending: d.ending,
      // structural fingerprints (not one of the 12, but they are what "templated" looked like)
      actCount: acts.length,
      roleCounts: counts,
      intensityCurve: intensities.map(x => x.toFixed(2)).join(','),
      sceneVocab: scenes.sort().join(','),
    };
  }

  // how many of the 12 axes differ between two signatures
  function diff(a, b) {
    const rows = [], same = [];
    for (const ax of AXES) {
      const x = a[ax], y = b[ax];
      const eq = (x == null || y == null) ? (x === y) : String(x) === String(y);
      (eq ? same : rows).push(ax);
    }
    return { differing: rows, matching: same, count: rows.length };
  }

  window.DIR = { AXES, FREE_AXES, ROLES, DEFAULTS, merge, pick, pickN, buildActs, normalize, signature, diff, cadenceFor };
})();
