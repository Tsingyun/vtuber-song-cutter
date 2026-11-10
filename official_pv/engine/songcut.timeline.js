/* songcut.timeline.js -- generic song-cut driver for the OFFICIAL vocaloid-style-mv engine.
 *
 * Design contract (all of it from the upstream kit, commit 1b13be4):
 *   - 05-engine.md §3   : shot = { id, t0, t1, scene, args, lyric, plan, post, trans, text, hud }
 *   - 05-engine.md §8   : adapt the 24 registered scenes first; write new ones only for signature moments
 *   - 01-workflow.md §4 : every shot gets a plan, post params and a motivated transition
 *   - 02 §6 / 09 §3     : escalate intensity per SECTION (Z.LOOKS), never delete a whole effect class
 *   - 06 §3             : several JIZURA plans, differentiated by style/seed/fx/centerFree
 *   - 06 §6             : lyric:'none' ONLY on shots whose scene IS the typography
 *   - effect-cookbook §0: custom fx factories are small deterministic {type:'custom',fn} recipes
 *   - 09 §2             : cuts on lyric onsets, holds, colour script, motivated transitions
 *
 * The scene name on every shot is looked up in Z.SCENES, which is populated by the 8 official
 * scene files loaded by index.html. tools/audit_official.mjs verifies this AT RUNTIME.
 */
(() => {
  'use strict';
  const Z = window.Z, D = Z.draw, E = Z.ease;
  const W = 1920, H = 1080;
  const SONG = new URLSearchParams(location.search).get('song') || 'faruxue';
  const NO_IMAGE = (() => Z.canvas(1, 1).toDataURL('image/png'))();   // 1x1 transparent PNG (official demo trick)

  // ================================================================ deterministic custom FX factories
  // Official demo (engine/timeline.js:50-128) defines 5 small factories: stars / ridge / starTrails /
  // firstLight / swarmGlyph. We do the same, with motifs taken from THIS song's lyrics.
  // Every factory is a pure function of S.t / S.p / S.clock; no Date.now, no unseeded random.
  function factories(cfg) {
    const P = cfg.palette, r = (i, s) => Z.rnd(i, s);
    const A = (c, a) => Z.rgba(c, a);
    const spec = fn => ({ type: 'custom', fn });

    return {
      // 雪 / snow: closed-form falling flakes, 12 fps hand-drawn timing
      snow: () => spec((ctx, S) => {
        const tq = Z.quant(S.t, 12), n = 190;
        ctx.save(); ctx.fillStyle = A(P.glow, 0.72);
        for (let i = 0; i < n; i++) {
          const sp = 26 + 90 * r(i, 11), x = (r(i, 12) * W + tq * 14 * (0.3 + r(i, 13))) % W;
          const y = (r(i, 14) * H + tq * sp) % (H + 40) - 20;
          const rad = 0.8 + 2.6 * Math.pow(r(i, 15), 3);
          ctx.globalAlpha = 0.20 + 0.62 * r(i, 16) * (0.55 + 0.45 * Math.sin(tq * 1.7 + i));
          ctx.beginPath(); ctx.arc(x, y, rad, 0, Z.TAU); ctx.fill();
        }
        ctx.restore();
      }),
      // 月 / moon halo: a slow-breathing cold disc that never blows out (clamped alpha)
      moon: () => spec((ctx, S) => {
        const x = W * (0.72 + 0.02 * Z.fbm1(S.t * 0.06, 5)), y = H * 0.27;
        const rad = H * (0.20 + 0.02 * S.clock.pulse(S.t, 5));
        const g = ctx.createRadialGradient(x, y, rad * 0.10, x, y, rad * 2.6);
        g.addColorStop(0, A(P.glow, 0.30)); g.addColorStop(0.22, A(P.glow, 0.11));
        g.addColorStop(0.55, A(P.accent, 0.045)); g.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.save(); ctx.globalCompositeOperation = 'screen'; ctx.fillStyle = g;
        ctx.fillRect(x - rad * 2.6, y - rad * 2.6, rad * 5.2, rad * 5.2); ctx.restore();
      }),
      // 墨 / ink wash: soft blooms spreading from the lyric's position
      ink: () => spec((ctx, S) => {
        const k = Z.clamp((S.t - S.shot.t0) / Math.max(0.5, S.dur));
        ctx.save(); ctx.globalCompositeOperation = 'multiply';
        for (let i = 0; i < 6; i++) {
          const x = W * (0.12 + 0.76 * r(i, 31)), y = H * (0.18 + 0.7 * r(i, 32));
          const rad = H * (0.08 + 0.42 * k) * (0.5 + r(i, 33));
          const g = ctx.createRadialGradient(x, y, 0, x, y, rad);
          g.addColorStop(0, A(P.void, 0.34 * (1 - k * 0.5))); g.addColorStop(1, 'rgba(0,0,0,0)');
          ctx.fillStyle = g; ctx.fillRect(x - rad, y - rad, rad * 2, rad * 2);
        }
        ctx.restore();
      }),
      // 朱砂 / one warm accent: vertical brush strokes, only where the lyric asks for it
      cinnabar: (color) => spec((ctx, S) => {
        const k = Z.clamp((S.t - S.shot.t0) / Math.max(0.4, S.dur));
        ctx.save(); ctx.globalAlpha = 0.55;
        for (let i = 0; i < 5; i++) {
          const x = W * (0.30 + 0.40 * r(i, 41)) + 5 * Math.sin(S.t * 1.3 + i);
          const y0 = H * (0.18 + 0.12 * r(i, 42)), len = H * (0.10 + 0.28 * r(i, 43)) * (0.35 + 0.65 * k);
          ctx.strokeStyle = A(color || P.warm, 0.55); ctx.lineWidth = 2 + 5 * r(i, 44);
          ctx.beginPath(); ctx.moveTo(x, y0); ctx.bezierCurveTo(x + 14, y0 + len * 0.4, x - 12, y0 + len * 0.7, x + 4, y0 + len); ctx.stroke();
        }
        ctx.restore();
      }),
      // 霓虹 / neon bars: small elements only (photosensitivity: < 10% of frame, no full-frame flicker)
      neon: (color) => spec((ctx, S) => {
        const tq = Z.quant(S.t, 12);
        ctx.save(); ctx.globalCompositeOperation = 'screen';
        for (let i = 0; i < 7; i++) {
          const x = W * (0.06 + 0.88 * r(i, 51)), y = H * (0.08 + 0.80 * r(i, 52));
          const w = 40 + 150 * r(i, 53), h = 3 + 5 * r(i, 54);
          const on = 0.35 + 0.45 * Math.abs(Math.sin(tq * (0.7 + r(i, 55)) + i));
          ctx.fillStyle = A(i % 2 ? (color || P.magenta) : P.accent, 0.30 * on);
          ctx.fillRect(x, y, w, h);
        }
        ctx.restore();
      }),
      // 拨号音 / dial rings: one ring per beat, radius = age (cookbook G07)
      dial: () => spec((ctx, S) => {
        const c = S.clock, t = S.t;
        ctx.save(); ctx.lineWidth = 2.4;
        for (let k = 0; k < 4; k++) {
          const age = (t - (c.nearest(t) - k * c.spb));
          if (age < 0 || age > 1.8) continue;
          const rad = age * 420, a = 1 - age / 1.8;
          ctx.strokeStyle = A(P.accent, 0.36 * a);
          ctx.beginPath(); ctx.arc(W * 0.5, H * 0.52, rad, 0, Z.TAU); ctx.stroke();
        }
        ctx.restore();
      }),
      // 雨玻璃 / rain on glass: short streaks + a few distorted highlights (cookbook G11)
      rainGlass: () => spec((ctx, S) => {
        const tq = Z.quant(S.t, 12);
        ctx.save();
        for (let i = 0; i < 130; i++) {
          const x = (r(i, 61) * W + tq * (18 + 30 * r(i, 62))) % W;
          const y = (r(i, 63) * H + tq * (190 + 260 * r(i, 64))) % H;
          const len = 14 + 46 * r(i, 65);
          ctx.strokeStyle = A(P.accent, 0.05 + 0.15 * r(i, 66)); ctx.lineWidth = 0.7 + 1.1 * r(i, 67);
          ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - 2, y + len); ctx.stroke();
        }
        ctx.restore();
      }),
      // 光尘 / drifting specks in a light shaft (cookbook G03)
      motes: (color) => spec((ctx, S) => {
        ctx.save(); ctx.fillStyle = A(color || P.gold, 0.5);
        for (let i = 0; i < 90; i++) {
          const x = W * (0.5 + 0.42 * Math.sin(S.t * (0.10 + 0.2 * r(i, 71)) + i));
          const y = ((r(i, 72) * H - S.t * (7 + 16 * r(i, 73))) % H + H) % H;
          ctx.globalAlpha = 0.10 + 0.35 * r(i, 74);
          ctx.beginPath(); ctx.arc(x, y, 0.7 + 1.8 * r(i, 75), 0, Z.TAU); ctx.fill();
        }
        ctx.restore();
      }),
    };
  }

  // ================================================================ built-in fx specs (scenes.js:97-116)
  function builtin(type, cfg, act, i) {
    const P = cfg.palette, q = 0.55 + 0.45 * act.intensity;
    switch (type) {
      case 'embers':    return { type: 'embers', n: Math.round(40 + 120 * act.intensity), color: P.warm || P.gold, rise: 90, size: 2.6, area: [0, 280, W, H + 80], wind: 0.12 };
      case 'fireflies': return { type: 'fireflies', n: Math.round(12 + 34 * act.intensity), area: [120, H * 0.35, W - 120, H], alpha: 0.6 * q, color: P.glow };
      case 'rain':      return { type: 'rain', n: Math.round(90 + 150 * act.intensity), angle: 0.18, speed: 260, len: 26, color: P.accent };
      case 'dust':      return { type: 'dust', n: Math.round(18 + 36 * act.intensity), color: Z.rgba(P.glow, 0.16) };
      case 'feathers':  return { type: 'feathers', n: Math.round(10 + 26 * act.intensity), size: 16, color: P.glow, line: false };
      case 'rays':      return { type: 'rays', x: W * 0.5, y: H * 0.16, n: 7, len: 1500, color: P.accent, a: 0.15 * q, seed: 3 };
      case 'glow':      return { type: 'glow', x: W * (0.3 + 0.4 * Z.rnd(i, 81)), y: H * 0.42, r: 260, color: P.accent, alpha: 0.20 * q };
      case 'cracks':    return { type: 'cracks', at: 0.15, dur: 0.9, color: P.warm || P.magenta };
      case 'fill':      return { type: 'fill', color: P.void, a: 0.24 * act.intensity, comp: 'multiply' };
      default:          return null;
    }
  }

  // ================================================================ setup
  Z.setup = async (audio) => {
    const cfg = await (await fetch(Z.ROOT + `songs/${SONG}.json`)).json();
    const rawLrc = (await (await fetch(Z.ROOT + cfg.lrc)).text()).replace(/^\uFEFF/, '');
    const lrc = rawLrc.split(/\r?\n/).filter(l => l.trim() && !/^\[(ti|ar|al|by|offset|length):/i.test(l.trim())).join('\n');
    const lines = [];
    for (const l of lrc.split(/\r?\n/)) {
      const m = l.match(/^\[(\d+):(\d+(?:\.\d+)?)\](.*)$/);
      if (m) lines.push({ t: +m[1] * 60 + +m[2], text: m[3].trim() });
    }

    Z.TITLE = cfg.title; Z.TITLE_SUB = cfg.titleSub; Z.ARTIST = cfg.artist; Z.CREDIT = cfg.credit;
    Z.LIVE_DIR = `assets/live/${SONG}/frames/`;
    Z.LIVE_LAST = Math.round(cfg.duration * 30) + 2;
    // prepare.py extracts exactly [round(a*30)+1 .. round(b*30)] per live window.
    // livewin.js uses this to snap edge probes onto a real frame instead of 404-ing.
    Z.LIVE_WINDOWS = cfg.live.map(([a, b]) => [Math.round(a * 30) + 1, Math.round(b * 30)]);
    // Palette defaults: an official scene or fx factory may reference any colour key.
    // 《坏女孩》 omits `gold`/`cold`, which crashed Z.hex(undefined) in the motes
    // factory at t=60 (QA round 1). Normalise once, here, so every consumer is safe.
    const PAL_DEFAULT = { void: '#05090E', ink: '#0B141C', mid: '#1B2E3C', glow: '#E8F2F8',
      accent: '#9FC6D8', warm: '#C8563C', gold: '#D9B26A', cold: '#7FA9BE',
      violet: '#8E7CC3', magenta: '#E8557F' };
    cfg.palette = Object.assign({}, PAL_DEFAULT, cfg.palette);
    const P = cfg.palette, F = factories(cfg);
    const acts = cfg.acts;
    const actAt = t => acts.find(a => t >= a.t0 && t < a.t1) || acts[acts.length - 1];

    // ------------------------------------------------------------ JIZURA: 5 curated plans (06 §3/§5)
    // Five plans, differentiated on style + seed + fx + centerFree + schemes (NOT just `mood`,
    // which J.plan ignores). Deny-list is the upstream template's, copied verbatim.
    if (window.J && lrc.trim()) {
      JZ.setAudio({ duration: audio.duration, beats: audio.beats, energy: null, energyRate: 0 });
      const DENY = {
        layout: 'scatter ring wave labels pill knSeesaw knGearWords knTumble knPadGrid knPathRide zigzag arcTop gridCells bubble ticker searchBar chat notification ticket hanging wordCloud bounceLine elastic stickerBomb keycaps bubbles slotMachine flipBoard dotMatrix equalizer tape contents numbered poster ema ransom newspaper vinyl cassette bookSpine stampSheet calendar chochin routeMap stationSign noren tanzaku omikuji kakejiku shoji clapper warningLabel priceTag nameTag stickyNotes karuta cube flipCards accordion pile blocks balloons magnets tiles bulbs ledScroll billboard crowdBubbles crossword wordSearch puzzle dominoes burst fisheye origami zipper mosaicTiles stencil tyIndexTable tyStatCount typeSpecimen dictionary proofread magazine headlineDeck footnote ribbon flag pendulum knCollide knSlamStack halftoneBig tySquare tyRotBlock subtitleBar tile kaleido polaroid postcard letterPaper genkou panels splitScreen splitHalves wall cylinder cube',
        fx: 'panelWipe irisTrans doors blindsTrans splitSlide gridRepeat whiteFrame blackFrame strobe colorBars mirrorFlash starGlint focusLines speedLines halftone duotone kaleido bulge squash snapshot loopScroll crtOff tvStatic zoomStutter negativeRing edgeDetect hueShift posterize ditherBit rotateSnap invert bandInvert flash bloomFlash',
        decor: 'dots arrows shapes qrBlock triangleSpin checkerStrip confetti petals heartsStars kamon seigaiha asanoha hanabi chochin shimenawa sensu momiji namiGashira memphis zigzagRibbon polkaPatch stripeCircle starburst cursorClick windowChrome progressBar toggleSwitch notifBell likeCounter mediaControls volumeBars musicNotes bubbles dandelion vines cloudPuffs tapePieces highlightMark staple paperClip indexTabs punchHoles swatches spinner tally qrBlock bracketsJP loopArrows halfCircles decoCorners tyColophon dateStamp tyTypeScale tyTextRule',
        cam: 'knCardFlip jelly bounce knJumpCut knTiltKick knReadPan knShearKick knRushIn',
        treat: 'boxed marker knWordPlate circled sticker splitColor rainbow chrome',
        enter: 'bubbles stickerPeel crumple loadingBar tokoroten',
        exit: 'balloonOff crumpleOut peelOff',
        bg: 'argyle tartan chevron houndstooth herringbone polka checker retroGrid eqBars kaleidoscope fireworks sunburst bigStripes isoCubes seigaiha asanoha tvBars splitDiag spiralArms squareTunnel moire',
        trans: 'knCornerSwing knStripSlam checker cubeTurn spinOut doorsOpen pixelate',
      };
      DENY.decor += ' dataColumns timecodeBar rainStreaks';
      // QA round 1: `huge` scales a 6-character line past the frame (t=45 unreadable).
      // JIZURA's enabled-map is the official way to remove one layout without dropping a whole class.
      DENY.layout += ' shadowPlay glitchGrid condensed huge';
      DENY.trans += ' knStutterCut';
      const enabled = {};
      for (const g of J.GROUP_KEYS) {
        enabled[g] = {}; const deny = new Set((DENY[g] || '').split(/\s+/).filter(Boolean));
        for (const k of J.order(g)) {
          const d = J.registry(g)[k] || {};
          let ok = !deny.has(k) && !/^hr[A-Z]/.test(k) && d.set !== 'horror';
          if (k === 'none') ok = true;
          enabled[g][k] = ok;
        }
      }
      const common = {
        enabled, lyrics: lrc, fps: 30, res: 1080, aspect: '16:9', seed: 128,
        unify: true, typeset: true, extra: true, wa: false, horror: false,
        colors: { accentOn: true, accent: P.accent, ghostA: P.cold || P.violet, ghostB: P.warm || P.magenta },
        schemes: 'dark',
        fx: { motion: 0.7, glitch: 0.32, chroma: 0.42, decor: 0.4, density: 0.5, texture: 0.5, flash: false, koma: 12, hud: 'off', bgSwitch: 0.3 },
      };
      await JZ.addPlan('main',  Object.assign({}, common, { style: 'gold',    seed: 128 }));
      // `cf` = FULL-FRAME typography. It must NOT set centerFree: that flag is JIZURA's
      // 「中央を空ける」 side-band mode (jizura_engine.js:2944 J.sideZones -> :3998 X.clip()).
      // It reserves the middle 40% of the frame for an on-screen character and CLIPS every
      // lyric/decoration to the left/right 30% bands. Applied to ordinary shots it cut giant
      // glyphs in half (《发如雪》「惹尘埃是非」) and left the centre of every animation empty
      // (《坏女孩》t=7). Only `oncam`, used EXCLUSIVELY by livewin shots, may open that centre.
      await JZ.addPlan('cf',    Object.assign({}, common, { style: 'gold',    seed: 131 }));
      await JZ.addPlan('oncam', Object.assign({}, common, { style: 'gold',    seed: 131, centerFree: true }));
      await JZ.addPlan('noir',  Object.assign({}, common, { style: 'noir',    seed: 129, fx: Object.assign({}, common.fx, { glitch: 0.52, chroma: 0.62 }) }));
      await JZ.addPlan('mono',  Object.assign({}, common, { style: 'mono',    seed: 130, fx: Object.assign({}, common.fx, { motion: 0.85, chroma: 0.70 }) }));
      await JZ.addPlan('slam',  Object.assign({}, common, { style: 'crimson', seed: 133, fx: Object.assign({}, common.fx, { motion: 0.92, glitch: 0.20, chroma: 0.30, density: 0.62 }) }));
      for (const nm of ['main', 'cf', 'noir', 'mono', 'slam', 'oncam']) {
        const pl = JZ.plans[nm] && JZ.plans[nm].plan;
        if (pl) for (const c of pl.cuts) if (c.companion) c.companion.scheme = c.scheme;
      }
    }

    // ------------------------------------------------------------ post looks, escalation (02 §6 / 09 §3)
    Z.LOOK_DEFAULT = cfg.lookDefault;
    Z.LOOKS = {
      intro:   { boil: 0.7, bloom: 0.18, grain: 0.04, beatCA: 0 },
      verse:   { boil: 0.9, grain: 0.055, beatCA: 0.8 },
      pre:     { boil: 1.0, beatCA: 1.4 },
      bridge:  { boil: 0.8, bloom: 0.22, beatCA: 0.6 },
      chorus:  { boil: 1.2, bloom: 0.44, beatBloom: 0.14, beatCA: 2.0 },
      climax:  { boil: 1.3, bloom: 0.50, beatBloom: 0.18, beatCA: 2.6 },
      outro:   { boil: 0.5, bloom: 0.16, grain: 0.04, beatCA: 0 },
    };
    Z.sectionAt = t => ({ look: actAt(t).look });
    const FLASH = acts.filter(a => ['chorus', 'climax'].includes(a.look)).map(a => a.t0);
    Z.globalFX = (Q, t) => {
      for (const at of FLASH) { const d = t - at; if (d >= 0 && d < 0.10) Q.flash = Math.max(Q.flash || 0, 0.55 * (1 - d / 0.10)); }
      const a = actAt(t);
      if ((a.look === 'climax' || a.look === 'chorus') && SONG === 'huainvhai') {
        Q.shake = [2.2 * Math.sin(t * 31), 1.2 * Math.cos(t * 27)];
      }
    };
    // credits: the ORIGINAL artist must be on the picture (song-cut house rule)
    Z.hud = (ctx, S) => {
      ctx.save();
      ctx.font = '400 20px "Noto Sans SC", sans-serif';
      ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
      ctx.fillStyle = Z.rgba(P.glow, 0.34);
      ctx.fillText(`${cfg.title}   /   ${cfg.artist}`, 56, H - 46);
      ctx.textAlign = 'right';
      ctx.font = '400 15px "Cormorant Garamond", serif';
      ctx.fillStyle = Z.rgba(P.glow, 0.22);
      ctx.fillText(cfg.titleSub, W - 56, H - 46);
      ctx.restore();
    };

    // ------------------------------------------------------------ shot grid: cuts on lyric onsets (09 §2)
    const bounds = [0];
    for (let ai = 0; ai < acts.length; ai++) {
      const a = acts[ai];
      const ls = lines.filter(l => l.t >= a.t0 && l.t < a.t1);
      const step = (a.look === 'chorus' || a.look === 'climax') ? 1 : (a.look === 'intro' || a.look === 'outro' ? 3 : 2);
      for (let k = 0; k < ls.length; k += step) { const t = ls[k].t; if (t > a.t0 + 0.35 && t < a.t1 - 0.35) bounds.push(t); }
      bounds.push(a.t1);
    }
    // live insert windows are hard boundaries too: the footage gets exactly its own short shots
    for (const [a, b] of cfg.live) { bounds.push(a, b); }
    let B = [...new Set(bounds.map(t => +t.toFixed(3)))].sort((x, y) => x - y)
      .filter((t, i, arr) => t <= cfg.duration && (i === 0 || t - arr[i - 1] >= 0.62));
    if (B[B.length - 1] < cfg.duration) B.push(cfg.duration);
    // 01-workflow §4 / 09 §2: no shot may hold still longer than 7 s. Intro/outro have
    // almost no lyric onsets, so without this the 22 s intro is ONE frozen title card.
    // Split long spans on motivated interior beats; never split inside a live window.
    const MAX_HOLD = 7.0;
    const inLive = t => cfg.live.some(([a, b]) => t > a + 0.05 && t < b - 0.05);
    const split = [];
    for (let i = 0; i < B.length - 1; i++) {
      const a = B[i], b = B[i + 1], span = b - a;
      split.push(a);
      if (span <= MAX_HOLD) continue;
      const n = Math.ceil(span / MAX_HOLD);
      for (let k = 1; k < n; k++) {
        const t = +(a + span * k / n).toFixed(3);
        if (!inLive(t) && t - a >= 0.62 && b - t >= 0.62) split.push(t);
      }
    }
    split.push(B[B.length - 1]);
    B = [...new Set(split)].sort((x, y) => x - y);

    const TYPO = new Set(['typeCard', 'kanjiSlam', 'tunnel', 'afterimage', 'jz']);
    const S = [];
    const stats = { scenes: {}, fx: {}, trans: {}, plans: {}, custom: {}, liveShots: 0, liveSec: 0 };
    const bump = (o, k) => { o[k] = (o[k] || 0) + 1; };
    let prev = ['', ''];
    const liveAt = t => cfg.live.findIndex(([a, b]) => t >= a && t < b);

    for (let i = 0; i < B.length - 1; i++) {
      const t0 = B[i], t1 = B[i + 1], mid = (t0 + t1) / 2;
      const act = actAt(mid), ai = acts.indexOf(act);
      const lw = liveAt(mid);
      const id = 's' + String(i + 1).padStart(3, '0');
      let scene;

      if (lw >= 0) {
        scene = 'livewin';
        const w = 486 + 26 * (lw % 3), h = 872 + 22 * (i % 3);
        S.push({
          id, t0, t1, scene,
          args: {
            mode: ['soft', 'seam', 'iris', 'bloom'][(lw + i) % 4],
            w, h, x: W * 0.5 - w / 2 + (lw % 2 ? 44 : -44), y: (H - h) / 2 + 10,
            zoom: 1.0, push: 0, beatGrow: 0, glow: 0.62,
            pal: { faceA: 0.10, scrA: 0.10, mulA: 0.34 },
            bg: cfg.grad, edge: P.accent, bgA: 0,
            fadeX: 74, fadeY: 62, rad: 0.47, hard: 0.50, maskAspect: 1.30,
            phase: i * 0.7, swayX: 0, swayY: 0,
            fx: [{ type: 'dust', n: 16, color: Z.rgba(P.glow, 0.14) }],
          },
          plan: 'oncam', lyric: 'front',   // centre reserved ONLY here: the singer is really on screen
          post: { bloom: 0.13, bloomThreshold: 0.88, beatBloom: 0, exposure: -0.015, lutA: act.lut, lutMix: 0.10, boil: 0.6 },
          hud: true,
        });
        bump(stats.scenes, 'livewin'); bump(stats.plans, 'oncam');
        stats.liveShots++; stats.liveSec += t1 - t0;
        prev = [prev[1], 'livewin'];
        continue;
      }

      // pick an OFFICIAL scene from the act's pool; never 3 identical scenes in a row (02 §12)
      scene = act.pool[(i * 5 + ai * 3) % act.pool.length];
      if (scene === prev[0] && scene === prev[1]) scene = act.pool[(act.pool.indexOf(scene) + 2) % act.pool.length];
      prev = [prev[1], scene];

      const intensity = act.intensity;
      const fxList = [];
      const chosenFx = [act.fx[(i * 3) % act.fx.length], act.fx[(i * 3 + 1) % act.fx.length], act.fx[(i * 3 + 2) % act.fx.length]];
      for (const f of new Set(chosenFx)) {
        if (f === 'custom') {
          const names = SONG === 'faruxue' ? ['snow', 'moon', 'ink', 'cinnabar', 'motes']
                                          : ['neon', 'rainGlass', 'dial', 'motes', 'ink'];
          const nm = names[(i + ai) % names.length];
          fxList.push((nm === 'cinnabar' ? F.cinnabar(P.warm) : nm === 'neon' ? F.neon(P.magenta) : F[nm]()));
          bump(stats.custom, nm);
        } else {
          const s = builtin(f, cfg, act, i);
          if (s) { fxList.push(s); bump(stats.fx, f); }
        }
      }

      const glyphs = cfg.glyphs;
      let args, post, lyric = 'front', plan;

      switch (scene) {
        case 'illust':
          // every third shot uses the official `illust` CAMERA branch (bg + bgFrom/bgTo +
          // beatZoom + shake), the others use its pure-gradient branch. Both are official code
          // paths in scenes.js:36-46; the plates are procedural, never AI-generated.
          args = Object.assign(
            (i % 3 === 0) ? { bg: `assets/bg/plate_${SONG}_${['a', 'b', 'c'][((i / 3) | 0) % 3]}.png` } : { grad: cfg.grad },
            {
            bgFrom: { zoom: 1.04, x: 0, y: 0 }, bgTo: { zoom: 1.11, x: 0, y: 0 }, bgEase: 'inOutSine',
            beatZoom: 0.010 * intensity, shake: intensity > 0.8 ? 2 : 0,
            tint: [{ color: P.mid, a: 0.16 * intensity, comp: 'soft-light' }],
            fxBack: fxList.slice(0, 1), fx: fxList.slice(1),
            vignette: 0.22 + 0.28 * intensity,
            });
          break;
        case 'solid':
          args = { grad: cfg.grad, embers: { n: Math.round(30 + 70 * intensity), color: P.warm || P.accent }, fireflies: { n: 12, color: P.glow, area: [0, H * 0.4, W, H] } };
          break;
        case 'jz':
          args = { plan: ['main', 'cf', 'noir', 'mono', 'slam'][i % 5] }; lyric = 'none';
          break;
        case 'converge':
          args = { seed: 7 + i };
          post = S2 => Object.assign(Z.INTRO.post.converge(S2), { lutA: act.lut, lutMix: 0.12 });
          break;
        case 'afterimage':
          args = { text: cfg.title, sub: cfg.titleSub.toLowerCase(), credit: cfg.credit, color: P.warm || P.violet, char: NO_IMAGE };
          post = S2 => Object.assign(Z.INTRO.post.afterimage(S2), { boil: 0 });
          lyric = 'none';
          break;
        case 'kanjiSlam':
          args = {
            glyphs: [glyphs[i % glyphs.length], glyphs[(i + 2) % glyphs.length], glyphs[(i + 4) % glyphs.length], glyphs[(i + 6) % glyphs.length]],
            // Rotate the official SLAM variants; reserve the white `day` sky for the climax
            // so the same bright burst does not recur in every section (QA round 1).
            variant: ['night', 'default', 'hand'][i % 3], char: null, bg: null,
            sky: act.look === 'climax' ? 'day' : 'night',
            color: act.look === 'climax' ? (P.warm || P.magenta) : P.glow,
            fire: act.look === 'climax',
          };
          post = S2 => Object.assign(Z.typo.post.kanjiSlam(S2), { lutA: act.lut, lutMix: 0.10 });
          lyric = 'none';
          break;
        case 'typeCard':
          args = { lines: i % 3 === 0 ? [cfg.title] : [cfg.typeLines[i % cfg.typeLines.length]], size: 96, fam: 'minchoHeavy', weight: 700, paper: P.void, ruleColor: P.accent, sub: cfg.credit, lead: 0.25 };
          post = { boil: 0, bloom: 0.22, grain: 0.035 };
          lyric = 'none';
          break;
        case 'tunnel':
          args = { words: cfg.tunnelWords, toward: 'dawn' };
          post = S2 => Object.assign(Z.typo.post.tunnel(S2), { lutA: act.lut, lutMix: 0.10 });
          lyric = 'none';
          break;
        case 'ember':
          // Z.INTRO.black() (intro.js:1094) reads `stops` as ABSOLUTE song-time blackout
          // windows. Passing the whole shot span blacked out the entire shot (t=160 QA).
          // Keep the official 溜め as a 2-frame breath only.
          args = { mode: 'breathe', seed: 5 + i, stops: [[t0 + 0.10, t0 + 0.18]] };
          post = S2 => Object.assign(Z.INTRO.post.ember(S2), { lutA: act.lut, lutMix: 0.10 });
          break;
        case 'road':
          args = { mode: 'tear', impact: 0.35, horizon: 0.55, open: 0.4 };
          break;
        case 'ref':
          args = { mode: 'melt', meltAt: 0.3, meltEnd: 0.9, cols: 18, depth: 0.4, seed: 11 + i };
          break;
        default:
          args = { grad: cfg.grad, fx: fxList };
      }

      // lyric plans: 5 genuinely different plans, picked per act + shot (06 §3)
      plan = (act.look === 'chorus' || act.look === 'climax') ? (i % 3 === 0 ? 'mono' : 'slam')
           : (act.look === 'intro' || act.look === 'outro') ? (i % 2 ? 'cf' : 'main')
           : (act.look === 'pre') ? 'noir'
           : (i % 4 === 0 ? 'cf' : 'main');
      if (lyric !== 'none') bump(stats.plans, plan);

      // per-shot post (01 §4): every shot carries its own params; section scaling comes from Z.LOOKS
      if (!post) post = {
        bloom: +(0.18 + 0.38 * intensity + 0.05 * Math.sin(i * 1.7)).toFixed(3),
        bloomThreshold: +(0.92 - 0.22 * intensity).toFixed(3),
        grain: +(0.04 + 0.035 * intensity).toFixed(3),
        vignette: +(0.16 + 0.30 * intensity).toFixed(3),
        lutA: act.lut, lutMix: +(0.08 + 0.18 * intensity).toFixed(3),
        boil: TYPO.has(scene) ? 0 : 1.0,
        ca: intensity > 0.7 ? 1.1 : 0,
      };
      // motivated transition (09 §2): from this act's grammar, never decorative noise
      const tt = act.trans[(i + ai) % act.trans.length];
      const trans = tt === 'cut' ? undefined : {
        type: tt, dur: tt === 'flash' ? 0.14 : (tt === 'black' ? 0.24 : 0.34),
        ...(tt === 'iris' || tt === 'irisClose' || tt === 'shatter' ? { cx: W * 0.5, cy: H * 0.46 } : {}),
        ...(tt === 'flash' ? { color: P.glow } : {}),
      };
      if (trans) bump(stats.trans, tt);

      S.push({ id, t0, t1, scene, args, lyric, plan, post, trans, hud: !TYPO.has(scene) });
      bump(stats.scenes, scene);
    }

    // signature moments: force the title card / final afterimage to actually land
    const first = S[0], last = S[S.length - 1];
    if (first) { first.scene = 'typeCard'; first.args = { lines: [cfg.title], size: 104, fam: 'minchoHeavy', weight: 700, paper: P.void, ruleColor: P.accent, sub: cfg.credit, lead: 0.25 }; first.lyric = 'none'; first.post = { boil: 0, bloom: 0.20, grain: 0.035 }; first.hud = false; }
    if (last)  { last.scene = 'afterimage'; last.args = { text: cfg.title, sub: cfg.titleSub.toLowerCase(), credit: cfg.credit, color: P.warm || P.violet, char: NO_IMAGE }; last.lyric = 'none'; last.post = S2 => Object.assign(Z.INTRO.post.afterimage(S2), { boil: 0 }); last.hud = false; }

    // ---- HARD GUARD (QA round 2) -------------------------------------------------------
    // A centre-reserving JIZURA plan is legal ONLY on a shot whose scene is `livewin`.
    // Sanitise any accidental use so the middle of an ordinary animation can never be
    // clipped away, and expose the count so the audit can prove it is zero.
    const CF = new Set(window.JZ ? Object.keys(JZ.plans).filter(n => JZ.plans[n].plan.centerFree) : []);
    let centreReserveOutsideLive = 0;
    for (const sh of S) {
      const used = [sh.plan, sh.args && sh.args.plan].filter(Boolean);
      if (sh.scene !== 'livewin' && used.some(p => CF.has(p))) {
        if (sh.plan && CF.has(sh.plan)) sh.plan = 'cf';
        if (sh.args && CF.has(sh.args.plan)) sh.args.plan = 'cf';
        centreReserveOutsideLive++;
      }
    }
    Z.SHOTS = S;
    window.__zankoMeta.duration = cfg.duration;
    window.__zankoMeta.fps = 30;

    // runtime evidence for tools/audit_official.mjs — this is what proves the official kit is in use
    window.__cutAudit = {
      song: SONG, officialScenesRegistered: Object.keys(Z.SCENES).sort(),
      officialScenesUsed: Object.keys(stats.scenes).filter(k => k !== 'livewin').sort(),
      scenes: stats.scenes, fxTypes: stats.fx, customFactories: stats.custom,
      transitions: stats.trans, plans: stats.plans,
      jizuraPlans: window.JZ ? Object.keys(JZ.plans) : [],
      centreFreePlans: [...CF], centreReserveOutsideLive,
      oncamShots: S.filter(s => s.scene === 'livewin').length,
      liveShots: stats.liveShots, liveSec: +stats.liveSec.toFixed(2),
      livePct: +(100 * stats.liveSec / cfg.duration).toFixed(1),
      shots: S.length, duration: cfg.duration,
      shotsWithPost: S.filter(s => s.post).length,
      shotsWithTrans: S.filter(s => s.trans).length,
      lyricNone: S.filter(s => s.lyric === 'none').length,
      lutNames: [...new Set(acts.map(a => a.lut))],
    };
  };
})();
