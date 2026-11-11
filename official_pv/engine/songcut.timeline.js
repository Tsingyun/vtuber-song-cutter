/* songcut.timeline.js -- DIRECTION-DRIVEN song-cut driver for the OFFICIAL vocaloid-style-mv engine.
 *
 * WHAT CHANGED FROM THE PREVIOUS DRIVER
 * -------------------------------------
 * The old driver was a fixed skeleton: 10 acts, one intensity curve, `(i*5+ai*3) % pool.length`
 * scene dispatch, `act.fx[(i*3+k) % n]` fx dispatch, 6 hardcoded JIZURA plans, 7 hardcoded
 * Z.LOOKS, `SONG === 'huainvhai'` / `SONG === 'faruxue'` special cases, and no `grad`/`fill` fx.
 * That produced one look with three palettes -- the "templated" result the user rejected.
 *
 * This driver takes NO decisions of its own. Every decision comes from `songs/<id>.json`:
 *   direction.structure   how many acts, how long, how intense, how dense (auto-derived
 *                         from the song's own lyric timings + a per-song seed)
 *   direction.lexicon     which scenes / fx / recipes / transitions this film may use
 *   direction.type        JIZURA styles, schemes, plan count, per-film deny-list
 *   direction.grade       LUT pool, post curves, key (exposure)
 *   direction.opening/ending, direction.char (the single AI illustration + how it recurs)
 * `engine/direction.js` turns that into acts + a 12-axis signature; this file turns the acts
 * into shots. `tools/audit_official.mjs` refuses to render if two songs collide on > 3 axes.
 *
 * Official contracts honoured (unchanged):
 *   05 §3   shot = { id, t0, t1, scene, args, lyric, plan, post, trans, text, hud }
 *   05 §8   adapt the 24 upstream scenes; only `livewin` is ours
 *   06 §3   several JIZURA plans differentiated on style/seed/fx/centerFree
 *   06 §6   lyric:'none' ONLY where the scene IS the typography
 *   09 §2   cuts on lyric onsets, no shot longer than 7 s, motivated transitions
 */
(() => {
  'use strict';
  const Z = window.Z, D = Z.draw, E = Z.ease;
  const W = 1920, H = 1080;
  const SONG = new URLSearchParams(location.search).get('song') || 'faruxue';
  const NO_IMAGE = (() => Z.canvas(1, 1).toDataURL('image/png'))();

  const clamp = Z.clamp, lerp = Z.lerp;

  // ================================================================ built-in fx (scenes.js:97-116)
  // All 11 official spec types are produced here. The previous build silently dropped any name
  // it did not know (`grad` was written in three song configs and never rendered) -- now every
  // name resolves or is reported in __cutAudit.fxDropped.
  function builtinFx(type, C, act) {
    const P = C.P, inten = act.intensity, q = 0.55 + 0.45 * inten;
    const v = Z.rnd(C.i, 81);
    switch (type) {
      case 'embers':    return { type: 'embers', n: Math.round(40 + 120 * inten), color: P.warm || P.gold, rise: 90, size: 2.6, area: [0, 280, W, H + 80], wind: 0.12 };
      case 'fireflies': return { type: 'fireflies', n: Math.round(12 + 34 * inten), area: [120, H * 0.35, W - 120, H], alpha: 0.6 * q, color: P.glow };
      case 'rain':      return { type: 'rain', n: Math.round(90 + 150 * inten), angle: 0.18, speed: 260, len: 26, color: P.accent };
      case 'dust':      return { type: 'dust', n: Math.round(18 + 36 * inten), color: Z.rgba(P.glow, 0.16) };
      case 'feathers':  return { type: 'feathers', n: Math.round(10 + 26 * inten), size: 16, color: P.glow, line: false };
      case 'rays':      return { type: 'rays', x: W * (0.35 + 0.3 * v), y: H * 0.16, n: 7, len: 1500, color: P.accent, a: 0.15 * q, seed: 3 + C.i };
      case 'glow':      return { type: 'glow', x: W * (0.3 + 0.4 * v), y: H * 0.42, r: 260, color: P.accent, a: 0.20 * q };
      case 'cracks':    return { type: 'cracks', at: 0.15, dur: 0.9, n: 18, reach: 900, color: P.warm || P.magenta, seed: 1 + C.i };
      case 'grad':      return { type: 'grad', stops: C.cfg.grad, a: 0.22 * inten, comp: 'source-over' };
      case 'fill':      return { type: 'fill', color: P.void, a: 0.24 * inten, comp: 'multiply' };
      case 'custom':    return null;                       // resolved by the recipe layer
      default:          return null;
    }
  }

  // ================================================================ single-illustration forms
  // One AI illustration, drawn through the official `illust` character branch (scenes.js:70-95).
  // Six forms so the SAME png never reads as the same picture twice.
  function charParams(form, C, idx) {
    const P = C.P, rng = C.rng;
    const base = { img: C.charImg, anchor: [0.5, 1], sway: 0.010, breathe: 1, wave: { amp: 9, from: 0.3, q: 12 }, phase: idx * 1.7 };
    const side = rng() < 0.5 ? -1 : 1;
    switch (form) {
      case 'bust':   // half body, lyric band beside
        return Object.assign(base, { x: W * (0.5 + 0.16 * side), y: H + 40, h: 1620, h2: 1664, ease: 'inOutSine',
          in: { dur: 0.55, dy: 60, ds: -0.04 }, out: { at: C.dur - 0.45, dur: 0.4, dy: 40 } });
      case 'close':  // face crop; the type layer passes in front
        return Object.assign(base, { x: W * (0.5 + 0.1 * side), y: H + 180, h: 2380, h2: 2440, ease: 'inOutSine',
          in: { dur: 0.7, ds: 0.06 }, out: { at: C.dur - 0.5, dur: 0.45, ds: 0.03 }, front: true, q: 15 });
      case 'sil':    // full silhouette: prints the pose away, keeps the outline
        return Object.assign(base, { x: W * (0.5 + 0.14 * side), y: H * 0.98, h: 1180, h2: 1120, ease: 'outSine',
          sil: P.ink, alpha: 0.92, in: { dur: 0.6, dy: 30 }, out: { at: C.dur - 0.4, dur: 0.35, dy: 20 } });
      case 'rim':    // rim light only (the low-key workhorse)
        return Object.assign(base, { x: W * (0.5 + 0.12 * side), y: H * 0.99, h: 1140, h2: 1160,
          rim: { color: P.warm || P.gold, off: [7, -4] }, in: { dur: 0.8, ds: 0.04 }, out: { at: C.dur - 0.5, dur: 0.45 } });
      case 'shadow': // cast shadow + tiny figure: distance, memory
        return Object.assign(base, { x: W * (0.5 - 0.2 * side), y: H * 0.94, h: 620, h2: 600,
          shadow: { dx: 210 * side, dy: -10, color: P.void, a: 0.52 }, sway: 0.006,
          in: { dur: 0.7, dx: 40 * side }, out: { at: C.dur - 0.4, dur: 0.4, dx: 30 * side } });
      default:       // 'full'
        return Object.assign(base, { x: W * (0.5 + 0.1 * side), y: H + 20, h: 1010, h2: 960, ease: 'inOutSine',
          in: { dur: 0.75, dy: 70, ds: -0.05 }, out: { at: C.dur - 0.5, dur: 0.45, dy: 50 } });
    }
  }

  // ================================================================ scene argument registry
  // Every scene registered by the 8 upstream files is reachable here (25 total incl. livewin).
  // Anything a song's lexicon does not name simply never runs -- capability, not obligation.
  function sceneArgs(name, C) {
    const P = C.P, act = C.act, inten = act.intensity, fx = C.fxList, i = C.i, rng = C.rng;
    const ch = C.charEntry;

    switch (name) {
      // ---------------------------------------------------------------- scenes.js
      case 'illust': {
        const usePlate = rng() < 0.55;
        const plate = `assets/bg/plate_${C.cfg.id}_${['a', 'b', 'c'][C.plateIdx % 3]}.png`;
        const a = Object.assign(
          usePlate ? { bg: plate } : { grad: C.cfg.grad },
          {
            bgFrom: { zoom: 1.03 + 0.03 * rng(), x: 0, y: 0 },
            bgTo: { zoom: 1.09 + 0.05 * rng(), x: 0, y: 0 },
            bgEase: 'inOutSine',
            beatZoom: 0.012 * inten,
            shake: inten > 0.82 ? 2 : 0,
            tint: [{ color: P.mid, a: 0.14 * inten, comp: 'soft-light' }],
            fxBack: fx.slice(0, 1), fx: fx.slice(1),
            vignette: 0.20 + 0.26 * inten,
          });
        if (ch) a.chars = [ch];
        return { args: a };
      }
      case 'solid':
        return { args: { grad: C.cfg.grad, embers: { n: Math.round(30 + 70 * inten), color: P.warm || P.accent }, fireflies: { n: 12, color: P.glow, area: [0, H * 0.4, W, H] }, chars: ch ? [ch] : undefined } };
      case 'jz':
        return { args: { plan: C.pickPlan(), bg: null, chars: ch ? [ch] : undefined }, lyric: 'none' };

      // ---------------------------------------------------------------- intro.js
      case 'converge':
        return { args: { seed: 7 + i }, post: S2 => Object.assign(Z.INTRO.post.converge(S2), { lutA: act.lut, lutMix: C.lutMix }) };
      case 'afterimage':
        return { args: { text: C.cfg.title, sub: C.cfg.titleSub.toLowerCase(), credit: C.cfg.credit, color: P.warm || P.violet, char: C.charImg || NO_IMAGE },
          post: S2 => Object.assign(Z.INTRO.post.afterimage(S2), { boil: 0 }), lyric: 'none' };
      case 'ember':
        return { args: { mode: C.mode('ember', ['breathe', 'hold', 'rise']), seed: 5 + i, stops: [[C.t0 + 0.10, C.t0 + 0.18]],
            char: C.charImg || NO_IMAGE, sky: C.plate('c'), bg: C.plate('b') },
          post: S2 => Object.assign(Z.INTRO.post.ember(S2), { lutA: act.lut, lutMix: C.lutMix }) };
      case 'titleRiff':
        // upstream titlePreload() wants a character AND a sky plate (it falls back to
        // assets/char/REF_master.png + assets/bg/bg_sky.png, neither of which exists here).
        return { args: { seed: 11 + i, whiteAt: 0.5, grain: 0.5, variant: C.mode('titleVar', ['intro']),
            char: C.charImg || NO_IMAGE, sky: C.plate('a') }, lyric: 'none' };

      // ---------------------------------------------------------------- typo.js
      case 'kanjiSlam':
        return {
          args: {
            glyphs: C.glyphs4(),
            variant: C.mode('slamVariant', ['night', 'default', 'hand']),
            char: null, bg: null,
            sky: C.look === 'climax' ? 'day' : 'night',
            color: C.look === 'climax' ? (P.warm || P.magenta) : P.glow,
            fire: C.look === 'climax' && C.cfg.direction.grade.fire !== false,
          },
          post: S2 => Object.assign(Z.typo.post.kanjiSlam(S2), { lutA: act.lut, lutMix: C.lutMix }),
          lyric: 'none',
        };
      case 'typeCard':
        return {
          args: { lines: i % 3 === 0 ? [C.cfg.title] : [C.cfg.typeLines[i % C.cfg.typeLines.length]], size: 92 + 8 * (i % 3), fam: 'minchoHeavy', weight: 700, paper: P.void, ruleColor: P.accent, sub: C.cfg.credit, lead: 0.25 },
          post: { boil: 0, bloom: 0.22, grain: 0.035 }, lyric: 'none',
        };
      case 'tunnel':
        return { args: { words: C.cfg.tunnelWords, toward: 'dawn' },
          post: S2 => Object.assign(Z.typo.post.tunnel(S2), { lutA: act.lut, lutMix: C.lutMix }), lyric: 'none' };

      // ---------------------------------------------------------------- glass.js
      case 'heart':
        return { args: { seed: 21 + i, mode: C.mode('heart', ['crack', 'hold', 'release']), impact: inten, night: C.key === 'high' ? 0.2 : 0.9, char: C.charImg || NO_IMAGE, charH: 900, charX: W * 0.5, bg: null, releaseBeat: true } };
      case 'skyShatter':
        return { args: { bg: null, char: C.charImg || NO_IMAGE } };

      // ---------------------------------------------------------------- dark.js
      case 'thread':
        return { args: { hand: null, light: { x: W * (0.7 + 0.1 * rng()), y: H * 0.3 }, stops: [], arriveAt: 0.6, kickAt: 0.9 } };
      case 'fall':
        return { args: { img: C.charImg || NO_IMAGE, char: C.charImg || NO_IMAGE, speed: 1, h: 820, freeze: true, vp: [W * 0.5, H * 0.52] } };
      case 'flame':
        return { args: { img: C.charImg || NO_IMAGE, face: null, mode: C.mode('flame', ['under', 'front', 'side']) } };
      case 'ink':
        return { args: { char: C.charImg || NO_IMAGE, seed: 31 + i, x: W * (0.3 + 0.4 * rng()), y: H * 0.55, h: 1000 } };

      // ---------------------------------------------------------------- night.js
      case 'wetFloor':
        return { args: { bg: null, chars: ch ? [ch] : [], mode: C.mode('wetFloor', ['still', 'walk', 'wait']), hero: true, heroX: 0.5, from: 0, to: 1, taps: [], cars: [], shutter: true, freeze: false } };
      case 'train':
        return { args: { bg: null, chars: ch ? [ch] : [], freeze: false } };

      // ---------------------------------------------------------------- memory.js
      case 'memories':
        return { args: { char: C.charImg || NO_IMAGE, photos: [], horizon: 0.58, open: 0.4, impact: inten, dolly: true } };
      case 'road':
        return { args: { char: C.charImg || NO_IMAGE, mode: C.mode('road', ['tear', 'run', 'stop']) } };

      // ---------------------------------------------------------------- meta.js
      case 'panels':
        return { args: { panels: 3 + (i % 3), every: 2, speed: 0.8, roll: 0.2, rim: P.accent, grade: 0.2, hud: false, flash: false, lead: 0.15 } };
      case 'burst':
        return { args: { seed: 41 + i, at: C.t0 + 0.2, hold: 0.1, rate: 2, desat: 0.2, push: 0.05, jitter: 0.3, drip: false, cuts: [], shot: null, span: 1 } };
      case 'ref':
        return { args: { mode: C.mode('ref', ['melt', 'tear', 'burn']), meltAt: 0.3, meltEnd: 0.9, cols: 14 + (i % 3) * 4, depth: 0.4, seed: 11 + i } };

      default:
        return { args: { grad: C.cfg.grad, fx } };
    }
  }

  // transition specs (core/trans.js) with per-type params
  function transSpec(type, C) {
    if (!type || type === 'cut') return undefined;
    const P = C.P, rng = C.rng;
    const base = { type, dur: type === 'flash' ? 0.14 : type === 'black' ? 0.24 : 0.34 };
    switch (type) {
      case 'flash':     return Object.assign(base, { color: P.glow });
      case 'iris':      return Object.assign(base, { cx: W * (0.3 + 0.4 * rng()), cy: H * 0.46, color: P.warm || P.accent });
      case 'irisClose': return Object.assign(base, { cx: W * (0.3 + 0.4 * rng()), cy: H * 0.46 });
      case 'barrier':   return Object.assign(base, { px: -40, py: H * (0.5 + 0.2 * rng()) });
      case 'wipe':      return Object.assign(base, { angle: -0.35 + 0.7 * rng(), color: P.warm || P.magenta });
      case 'slices':    return Object.assign(base, { n: 7 + Math.round(6 * rng()) });
      case 'push':      return Object.assign(base, { dx: rng() < 0.5 ? -1 : 1, dy: rng() < 0.3 ? 0.35 : 0 });
      case 'ink':       return Object.assign(base, { seed: 7 + C.i });
      case 'shatter':   return Object.assign(base, { seed: 3 + C.i, cx: W * (0.4 + 0.2 * rng()), cy: H * 0.46 });
      default:          return base;
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

    const PAL_DEFAULT = { void: '#05090E', ink: '#0B141C', mid: '#1B2E3C', glow: '#E8F2F8',
      accent: '#9FC6D8', warm: '#C8563C', gold: '#D9B26A', cold: '#7FA9BE',
      violet: '#8E7CC3', magenta: '#E8557F' };
    cfg.palette = Object.assign({}, PAL_DEFAULT, cfg.palette);
    const P = cfg.palette;

    // ------------------------------------------------------------ direction (engine/direction.js)
    const DIR = window.DIR;
    const dir = DIR.normalize(cfg, SONG);
    const acts = dir.structure.mode === 'auto' ? DIR.buildActs(cfg, lines) : (cfg.acts && cfg.acts.length ? cfg.acts : DIR.buildActs(cfg, lines));
    cfg.acts = acts;
    const R = window.RECIPES.build(cfg);
    const BASE_SEED = dir.seed >>> 0;
    const rng = Z.rng(BASE_SEED ^ 0x51ed270b);
    const key = dir.grade.exposure > 0.15 ? 'high' : 'low';

    Z.TITLE = cfg.title; Z.TITLE_SUB = cfg.titleSub; Z.ARTIST = cfg.artist; Z.CREDIT = cfg.credit;
    Z.LIVE_DIR = `assets/live/${SONG}/frames/`;
    Z.LIVE_LAST = Math.round(cfg.duration * 30) + 2;
    Z.LIVE_WINDOWS = cfg.live.map(([a, b]) => [Math.round(a * 30) + 1, Math.round(b * 30)]);

    const actAt = t => acts.find(a => t >= a.t0 && t < a.t1) || acts[acts.length - 1];

    // ------------------------------------------------------------ JIZURA plans from direction.type
    const planNames = [];
    if (window.J && lrc.trim()) {
      JZ.setAudio({ duration: audio.duration, beats: audio.beats, energy: null, energyRate: 0 });
      // Upstream's template deny-list is the STARTING point; 06 §5 says "adapt per film".
      const DENY = {
        layout: 'scatter ring wave labels pill knSeesaw knGearWords knTumble knPadGrid knPathRide zigzag arcTop gridCells bubble ticker searchBar chat notification ticket hanging wordCloud bounceLine elastic stickerBomb keycaps bubbles slotMachine flipBoard dotMatrix equalizer tape contents numbered poster ema ransom newspaper vinyl cassette bookSpine stampSheet calendar chochin routeMap stationSign noren tanzaku omikuji kakejiku shoji clapper warningLabel priceTag nameTag stickyNotes karuta cube flipCards accordion pile blocks balloons magnets tiles bulbs ledScroll billboard crowdBubbles crossword wordSearch puzzle dominoes burst fisheye origami zipper mosaicTiles stencil tyIndexTable tyStatCount typeSpecimen dictionary proofread magazine headlineDeck footnote ribbon flag pendulum knCollide knSlamStack halftoneBig tySquare tyRotBlock subtitleBar tile kaleido polaroid postcard letterPaper genkou panels splitScreen splitHalves wall cylinder cube shadowPlay glitchGrid condensed huge',
        fx: 'panelWipe irisTrans doors blindsTrans splitSlide gridRepeat whiteFrame blackFrame strobe colorBars mirrorFlash starGlint focusLines speedLines halftone duotone kaleido bulge squash snapshot loopScroll crtOff tvStatic zoomStutter negativeRing edgeDetect hueShift posterize ditherBit rotateSnap invert bandInvert flash bloomFlash',
        decor: 'dots arrows shapes qrBlock triangleSpin checkerStrip confetti petals heartsStars kamon seigaiha asanoha hanabi chochin shimenawa sensu momiji namiGashira memphis zigzagRibbon polkaPatch stripeCircle starburst cursorClick windowChrome progressBar toggleSwitch notifBell likeCounter mediaControls volumeBars musicNotes bubbles dandelion vines cloudPuffs tapePieces highlightMark staple paperClip indexTabs punchHoles swatches spinner tally bracketsJP loopArrows halfCircles decoCorners tyColophon dateStamp tyTypeScale tyTextRule dataColumns timecodeBar rainStreaks',
        cam: 'knCardFlip jelly bounce knJumpCut knTiltKick knReadPan knShearKick knRushIn',
        treat: 'boxed marker knWordPlate circled sticker splitColor rainbow chrome',
        enter: 'bubbles stickerPeel crumple loadingBar tokoroten',
        exit: 'balloonOff crumpleOut peelOff',
        bg: 'argyle tartan chevron houndstooth herringbone polka checker retroGrid eqBars kaleidoscope fireworks sunburst bigStripes isoCubes seigaiha asanoha tvBars splitDiag spiralArms squareTunnel moire',
        trans: 'knCornerSwing knStripSlam checker cubeTurn spinOut doorsOpen pixelate knStutterCut',
      };
      // per-film additions / removals (arrays in the song config)
      const denyCfg = dir.type.deny || {};
      for (const g of Object.keys(denyCfg)) {
        const set = new Set((DENY[g] || '').split(/\s+/).filter(Boolean));
        for (const k of (denyCfg[g].remove || [])) set.delete(k);
        for (const k of (denyCfg[g].add || [])) set.add(k);
        DENY[g] = [...set].join(' ');
      }
      const enabled = {};
      for (const g of J.GROUP_KEYS) {
        enabled[g] = {}; const deny = new Set((DENY[g] || '').split(/\s+/).filter(Boolean));
        for (const k of J.order(g)) {
          const d = J.registry(g)[k] || {};
          let ok = !deny.has(k) && !/^hr[A-Z]/.test(k) && d.set !== 'horror';
          if (!dir.type.extra && d.extra) ok = false;
          if (k === 'none') ok = true;
          enabled[g][k] = ok;
        }
      }
      const colors = dir.type.colors || { accentOn: true, accent: P.accent, ghostA: P.cold || P.violet, ghostB: P.warm || P.magenta };
      const styles = (dir.type.styles && dir.type.styles.length) ? dir.type.styles : ['gold'];
      const common = {
        enabled, lyrics: lrc, fps: 30, res: 1080, aspect: '16:9',
        seed: BASE_SEED % 9973,
        unify: true, typeset: true, extra: dir.type.extra, wa: dir.type.wa, horror: dir.type.horror,
        typo: dir.type.typo, kinetic: dir.type.kinetic,
        colors,
        schemes: dir.type.schemes,
        centerDir: dir.type.centerDir || 'tb',
        fx: Object.assign({}, dir.type.fx),
      };
      if (dir.type.zoneW) common.zoneW = dir.type.zoneW;
      const mk = async (name, styleIdx, seedOff, extra) => {
        const st = styles[styleIdx % styles.length];
        const r = await JZ.addPlan(name, Object.assign({}, common, { style: st, seed: (common.seed + seedOff) % 9973 }, extra || {}));
        planNames.push(name);
        return r;
      };
      await mk('main', 0, 0);
      await mk('cf', 0, 3);
      await mk('oncam', 0, 3, { centerFree: true });      // the ONLY centre-free plan (livewin)
      const extraN = Math.max(0, (dir.type.plans || 6) - 3);
      for (let k = 0; k < extraN; k++) await mk('v' + (k + 1), 1 + (k % Math.max(1, styles.length - 1)), 5 + k * 4);
      // propagate each cut's scheme to its companion (upstream addPlan contract)
      for (const nm of planNames) {
        const pl = JZ.plans[nm] && JZ.plans[nm].plan;
        if (pl) for (const c of pl.cuts) if (c.companion) c.companion.scheme = c.scheme;
      }
    }
    const planPool = planNames.filter(n => n !== 'oncam');
    const pickPlan = () => planPool.length ? DIR.pick(planPool.map(n => n), rng) : 'main';

    // ------------------------------------------------------------ post looks from direction.grade
    const g = dir.grade;
    Z.LOOK_DEFAULT = Object.assign({}, g.base, cfg.lookDefault && typeof cfg.lookDefault === 'object' ? cfg.lookDefault : {});
    const LOOKS = g.looks || {
      intro:   { boil: 0.7, bloom: 0.18, grain: 0.04, beatCA: 0 },
      verse:   { boil: 0.9, grain: 0.055, beatCA: 0.8 },
      pre:     { boil: 1.0, beatCA: 1.4 },
      chorus:  { boil: 1.2, bloom: 0.44, beatBloom: 0.14, beatCA: 2.0 },
      bridge:  { boil: 0.8, bloom: 0.22, beatCA: 0.6 },
      climax:  { boil: 1.3, bloom: 0.50, beatBloom: 0.18, beatCA: 2.6 },
      outro:   { boil: 0.5, bloom: 0.16, grain: 0.04, beatCA: 0 },
    };
    Z.LOOKS = LOOKS;
    Z.sectionAt = t => ({ look: actAt(t).look });

    // section-start flash is OPT-IN per film (the old build flashed every chorus for every song)
    const flashAt = g.flashOnSection ? acts.filter(a => ['chorus', 'climax'].includes(a.look)).map(a => a.t0) : [];
    Z.globalFX = (Q, t) => {
      for (const at of flashAt) { const d = t - at; if (d >= 0 && d < 0.10) Q.flash = Math.max(Q.flash || 0, 0.55 * (1 - d / 0.10)); }
      const a = actAt(t);
      if (g.shakeOnClimax && a.look === 'climax') { Q.shake = [2.2 * Math.sin(t * 31), 1.2 * Math.cos(t * 27)]; }
    };

    // ------------------------------------------------------------ HUD
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

    // ------------------------------------------------------------ single-illustration windows
    // One PNG, several FORMS. Windows are placed at real lyric onsets inside acts the song
    // actually wants a face in, and never overlap a live insert.
    // The illustration is USER-SUPPLIED, so it may not exist yet: probe it once and degrade to
    // "no character shots" instead of failing the whole build on a missing asset.
    const charCfg = dir.char || null;
    let charImg = null, charMissing = null;
    if (charCfg && charCfg.img) {
      try {
        const r = await fetch(Z.ROOT + charCfg.img, { method: 'HEAD' });
        if (r.ok) charImg = charCfg.img; else charMissing = `${charCfg.img} (HTTP ${r.status})`;
      } catch (e) { charMissing = `${charCfg.img} (${String(e).slice(0, 60)})`; }
    }
    const charShots = [];
    if (charImg) {
      const inLiveW = (t, pad = 0.4) => cfg.live.some(([a, b]) => t > a - pad && t < b + pad);
      const wanted = charCfg.shots && charCfg.shots.length ? charCfg.shots : (() => {
        const out = [];
        const forms = charCfg.forms || ['full', 'bust', 'close', 'sil', 'rim', 'shadow'];
        const n = Math.max(2, charCfg.count || 4);
        const prefer = charCfg.roles || ['verse', 'chorus', 'climax', 'bridge'];
        const cand = acts.filter(a => prefer.includes(a.role));
        const pool = cand.length ? cand : acts;
        for (let k = 0; k < n; k++) {
          const a = pool[Math.floor(rng() * pool.length) % pool.length];
          const ls = lines.filter(l => l.t > a.t0 + 1.0 && l.t < a.t1 - 2.2 && !inLiveW(l.t));
          const t0 = ls.length ? ls[Math.floor(rng() * ls.length) % ls.length].t : a.t0 + (a.t1 - a.t0) * 0.4;
          const len = lerp(charCfg.lenRange?.[0] ?? 2.8, charCfg.lenRange?.[1] ?? 4.6, rng());
          out.push({ t0: +t0.toFixed(3), t1: +(t0 + len).toFixed(3), form: forms[k % forms.length] });
        }
        // resolve overlaps: keep the earlier, drop the later
        out.sort((x, y) => x.t0 - y.t0);
        const keep = [];
        for (const w of out) if (!keep.some(k => w.t0 < k.t1 + 0.35 && w.t1 > k.t0 - 0.35)) keep.push(w);
        return keep;
      })();
      for (const w of wanted) if (!inLiveW((w.t0 + w.t1) / 2)) charShots.push(w);
    }
    const charAt = t => charShots.find(w => t >= w.t0 && t < w.t1) || null;

    // ------------------------------------------------------------ shot grid: cuts on lyric onsets
    const st = dir.structure;
    const minShot = st.minShot ?? 0.62;
    const bounds = [0];
    for (let ai = 0; ai < acts.length; ai++) {
      const a = acts[ai];
      const ls = lines.filter(l => l.t >= a.t0 && l.t < a.t1);
      const step = a.cadence || (a.look === 'chorus' || a.look === 'climax' ? 1 : 2);
      for (let k = 0; k < ls.length; k += step) { const t = ls[k].t; if (t > a.t0 + 0.35 && t < a.t1 - 0.35) bounds.push(t); }
      bounds.push(a.t1);
    }
    for (const [a, b] of cfg.live) bounds.push(a, b);
    for (const w of charShots) bounds.push(w.t0, w.t1);          // a form change is always a cut
    let B = [...new Set(bounds.map(t => +t.toFixed(3)))].sort((x, y) => x - y)
      .filter((t, i, arr) => t <= cfg.duration && (i === 0 || t - arr[i - 1] >= minShot));
    if (B[B.length - 1] < cfg.duration) B.push(cfg.duration);

    const maxHold = st.maxHold ?? 6.4;
    const inLive = t => cfg.live.some(([a, b]) => t > a + 0.05 && t < b - 0.05);
    const split = [];
    for (let i = 0; i < B.length - 1; i++) {
      const a = B[i], b = B[i + 1], span = b - a;
      split.push(a);
      if (span <= maxHold) continue;
      const n = Math.ceil(span / maxHold);
      for (let k = 1; k < n; k++) {
        const t = +(a + span * k / n).toFixed(3);
        if (!inLive(t) && t - a >= minShot && b - t >= minShot) split.push(t);
      }
    }
    split.push(B[B.length - 1]);
    B = [...new Set(split)].sort((x, y) => x - y);
    // a char window must not be split by the max-hold pass
    const charEdge = t => charShots.some(w => Math.abs(t - w.t0) < 1e-3 || Math.abs(t - w.t1) < 1e-3);
    B = B.filter((t, i) => i === 0 || i === B.length - 1 || charEdge(t) || !charShots.some(w => t > w.t0 + 0.01 && t < w.t1 - 0.01));

    const TYPO = new Set(['typeCard', 'kanjiSlam', 'tunnel', 'afterimage', 'jz', 'titleRiff']);
    const S = [];
    const stats = { scenes: {}, fx: {}, trans: {}, plans: {}, recipes: {}, forms: {}, liveShots: 0, liveSec: 0 };
    const fxDropped = new Set();
    const fxDeclared = new Set();
    const bump = (o, k) => { o[k] = (o[k] || 0) + 1; };
    const lastScenes = [];
    const liveAt = t => cfg.live.findIndex(([a, b]) => t >= a && t < b);
    let plateIdx = 0;

    const glyphs4 = () => {
      const gl = cfg.glyphs;
      const s = Math.floor(rng() * gl.length) % gl.length;
      return [gl[s], gl[(s + 2) % gl.length], gl[(s + 4) % gl.length], gl[(s + 6) % gl.length]];
    };

    for (let i = 0; i < B.length - 1; i++) {
      const t0 = B[i], t1 = B[i + 1], mid = (t0 + t1) / 2, dur = t1 - t0;
      const act = actAt(mid), ai = acts.indexOf(act);
      const lw = liveAt(mid);
      const id = 's' + String(i + 1).padStart(3, '0');
      if (dur < 0.30) continue;

      // ---------------- live insert: unchanged (zero-warp, oncam plan, <=15% runtime)
      if (lw >= 0) {
        const w = 486 + 26 * (lw % 3), h = 872 + 22 * (i % 3);
        S.push({
          id, t0, t1, scene: 'livewin',
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
          plan: 'oncam', lyric: 'front',
          post: { bloom: 0.13, bloomThreshold: 0.88, beatBloom: 0, exposure: -0.015, lutA: act.lut, lutMix: 0.10, boil: 0.6 },
          hud: true,
        });
        bump(stats.scenes, 'livewin'); bump(stats.plans, 'oncam');
        stats.liveShots++; stats.liveSec += dur;
        lastScenes.push('livewin');
        continue;
      }

      // ---------------- scene choice: seeded weighted draw, never a modulo walk
      const cw = charAt(mid);
      let scene;
      if (cw) {
        scene = 'illust';                                   // the illustration window wins
        bump(stats.forms, cw.form);
      } else {
        const pool = act.pool && act.pool.length ? act.pool : (dir.lexicon.any || ['illust']);
        const ban = new Set(lastScenes.slice(-2));
        const filtered = pool.filter(s => !ban.has(s));
        scene = DIR.pick((filtered.length ? filtered : pool).map(s => s), rng);
        if (!scene) scene = pool[0];
      }
      lastScenes.push(scene);
      if (lastScenes.length > 4) lastScenes.shift();

      // ---------------- fx: built-ins + recipes, seeded draw, no silent drops
      const fxList = [];
      const fxVocab = (act.fx && act.fx.length ? act.fx : dir.lexicon.anyFx || ['dust']);
      const nFx = clamp(2 + Math.round(act.intensity * 2), 2, 4);
      const chosen = DIR.pickN(fxVocab, nFx, rng);
      for (const f of chosen) {
        if (f === 'custom') {
          const names = (dir.lexicon.recipes && dir.lexicon.recipes.length) ? dir.lexicon.recipes : Object.keys(R);
          const nm = names[Math.floor(rng() * names.length) % names.length];
          if (R[nm]) { fxList.push(R[nm](rng() < 0.5 ? P.glow : P.accent)); bump(stats.recipes, nm); }
          continue;
        }
        fxDeclared.add(f);
        const spec = builtinFx(f, { P, i, cfg, look: act.look }, act);
        if (spec) { fxList.push(spec); bump(stats.fx, f); } else fxDropped.add(f);
      }

      const C = {
        cfg, P, act, i, t0, t1, dur, intensity: act.intensity, look: act.look, key,
        fxList, glyphs4, rng, plateIdx: plateIdx++,
        plate: k => `assets/bg/plate_${cfg.id}_${k}.png`,
        lutMix: +lerp(g.lutMixCurve[0], g.lutMixCurve[1], act.intensity).toFixed(3),
        pickPlan,
        mode: (slot, list) => list[Math.floor(rng() * list.length) % list.length],
        charImg,
        charEntry: cw ? charParams(cw.form, { P, dur, rng, charImg }, i) : null,
      };
      const built = sceneArgs(scene, C);
      let { args, post, lyric = 'front', plan } = built;

      // ---------------- plan: seeded draw from THIS film's plan pool
      if (lyric !== 'none') { plan = pickPlan(); bump(stats.plans, plan); }

      // ---------------- post: every shot carries its own params (01 §4)
      if (!post) {
        const inten = act.intensity;
        post = {
          bloom: +(lerp(g.bloomCurve[0], g.bloomCurve[1], inten) + 0.04 * Math.sin(i * 1.7)).toFixed(3),
          bloomThreshold: +(0.92 - 0.22 * inten).toFixed(3),
          grain: +lerp(g.grainCurve[0], g.grainCurve[1], inten).toFixed(3),
          vignette: +lerp(g.vignetteCurve[0], g.vignetteCurve[1], inten).toFixed(3),
          lutA: act.lut,
          lutMix: +lerp(g.lutMixCurve[0], g.lutMixCurve[1], inten).toFixed(3),
          boil: TYPO.has(scene) ? 0 : 1.0,
          ca: inten > 0.7 ? g.caBoost : 0,
          exposure: g.exposure || 0,
        };
      }

      // ---------------- transition: from this act's grammar
      const tt = (act.trans && act.trans.length) ? act.trans[Math.floor(rng() * act.trans.length) % act.trans.length] : 'cut';
      const trans = transSpec(tt, C);
      // `cut` is a real choice of grammar, not the absence of one -- count it
      if (trans) bump(stats.trans, tt); else bump(stats.trans, 'cut');

      S.push({ id, t0, t1, scene, args, lyric, plan, post, trans, hud: !TYPO.has(scene) });
      bump(stats.scenes, scene);
    }

    // signature moments: the opening / closing are a DIRECTION decision, not a constant
    const first = S[0], last = S[S.length - 1];
    if (first) {
      const openScene = dir.opening || 'typeCard';
      if (first.scene !== openScene) {
        const C0 = { cfg, P, act: actAt(first.t0), i: 0, t0: first.t0, t1: first.t1, dur: first.t1 - first.t0, intensity: 0.2, look: 'intro', key, fxList: [], glyphs4, rng, plateIdx: 0, plate: k => `assets/bg/plate_${cfg.id}_${k}.png`, lutMix: 0.1, pickPlan, mode: (s, l) => l[0], charImg, charEntry: null };
        const b0 = sceneArgs(openScene, C0);
        first.scene = openScene; first.args = b0.args; first.lyric = b0.lyric || 'none';
        first.plan = pickPlan(); first.post = b0.post || { boil: 0, bloom: 0.20, grain: 0.035 }; first.hud = false;
        bump(stats.scenes, openScene);
      }
    }
    if (last) {
      const endScene = dir.ending || 'afterimage';
      if (last.scene !== endScene) {
        const C1 = { cfg, P, act: actAt(last.t0), i: S.length, t0: last.t0, t1: last.t1, dur: last.t1 - last.t0, intensity: 0.2, look: 'outro', key, fxList: [], glyphs4, rng, plateIdx: 0, plate: k => `assets/bg/plate_${cfg.id}_${k}.png`, lutMix: 0.1, pickPlan, mode: (s, l) => l[0], charImg, charEntry: null };
        const b1 = sceneArgs(endScene, C1);
        last.scene = endScene; last.args = b1.args; last.lyric = b1.lyric || 'none';
        last.plan = pickPlan(); last.post = b1.post || { boil: 0, bloom: 0.18, grain: 0.03 }; last.hud = false;
        bump(stats.scenes, endScene);
      }
    }

    // ---- HARD GUARD: a centre-reserving JIZURA plan is legal ONLY on livewin ---------------
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

    // ---- runtime evidence for tools/audit_official.mjs ------------------------------------
    const signature = DIR.signature(cfg, acts);
    window.__cutAudit = {
      song: SONG, seed: BASE_SEED, directionMode: dir.structure.mode,
      officialScenesRegistered: Object.keys(Z.SCENES).sort(),
      officialScenesUsed: Object.keys(stats.scenes).filter(k => k !== 'livewin').sort(),
      scenes: stats.scenes, fxTypes: stats.fx, customFactories: stats.recipes,
      transitions: stats.trans, plans: stats.plans,
      jizuraPlans: planNames, jizuraStyles: (dir.type.styles || []), schemes: dir.type.schemes,
      centreFreePlans: [...CF], centreReserveOutsideLive,
      oncamShots: S.filter(s => s.scene === 'livewin').length,
      liveShots: stats.liveShots, liveSec: +stats.liveSec.toFixed(2),
      livePct: +(100 * stats.liveSec / cfg.duration).toFixed(1),
      shots: S.length, duration: cfg.duration,
      shotsWithPost: S.filter(s => s.post).length,
      shotsWithTrans: S.filter(s => s.trans).length,
      lyricNone: S.filter(s => s.lyric === 'none').length,
      lutNames: [...new Set(acts.map(a => a.lut))],
      // new: proof that nothing was silently discarded, and what the film actually decided
      fxDeclared: [...fxDeclared].sort(), fxDropped: [...fxDropped].sort(),
      charShots: charShots.length, charForms: stats.forms, charImg: charImg || null,
      charDeclared: charCfg ? charCfg.img : null, charMissing,
      actCount: acts.length, actRoles: acts.map(a => a.role),
      intensityCurve: acts.map(a => +a.intensity.toFixed(2)),
      cadence: acts.map(a => a.cadence),
      direction: { seed: BASE_SEED, mode: dir.structure.mode, signature, axes: dir.axes || {} },
    };
  };
})();
