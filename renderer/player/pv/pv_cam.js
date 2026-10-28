/*! pv_cam.js —— 相机层。整帧的运动，与文字样式正交。
 *
 *  参考 JIZURA（852wa/JIZURA，MIT）的 CAMERA 分组：它的每个 cut 都带一台「一直在动」的
 *  摄影机（默认慢推 1→1.03），这是文字 PV 不像 ppt 的最小单位。我们之前只有
 *  `cam.zoom` 一个**常量**，镜头内画面完全静止 —— 抽帧看不出来，连起来看就是一排
 *  静止的幻灯片，这才是「动效生硬」的主因。
 *
 *  纪律：
 *   · 纯函数：只吃 (lt, dur, W, H)，不做积分、不存状态 —— 同一 t 永远同一结果
 *     （确定性是硬要求，任何带状态的相机都会破坏「双渲字节一致」）。
 *   · 幅度 = 基础值 × A（镜头强度 0.85~1.15）× K（段落强度 0.6~1.2），
 *     所以最大位移不超过画面 3%，rot 不超过 2°。
 *   · s 永远 >= 1，且由位移反解兜底（见 camAt），绝不露画布边。
 */
(() => {
  'use strict';
  const PV = window.PV;
  const E = PV.ease;
  const TAU = 6.283185307;
  const RAD = Math.PI / 180;

  /* 平滑一维值噪声：相机的「手抖」必须连续，用 sin 拼会周期性地露出机械感 */
  const noise1 = (x, seed) => {
    const i = Math.floor(x), f = x - i, u = f * f * (3 - 2 * f);
    const r = (k) => {
      let h = Math.imul((seed | 0) ^ 0x9e3779b9, 0x85ebca6b) ^ Math.imul((i + k) | 0, 0xc2b2ae35);
      h = Math.imul(h ^ (h >>> 15), 0x2c1b3c6d);
      return ((h ^ (h >>> 13)) >>> 0) / 4294967296;
    };
    return PV.lerp(r(0), r(1), u) * 2 - 1;
  };
  PV.noise1 = noise1;

  const amp = (ev) => ev.A * ev.K;      // 统一幅度系数

  const C = (PV.CAM = {});

  /* 静止：只给出画面余量，供别的层缩放时不吃到边 */
  C.none = () => ({ s: 1.002 });

  /* 慢推：默认档。整镜缓慢推近，永不停 —— JIZURA 里最常见的「什么都不做但在动」。
     推进量随**时长**走而不是固定 3.8%：片头标题卡 8.2s 固定 3.8% 等于每帧 0.15px
     （实测帧差 p50 = 0.15，和静帧没区别）。改成 1.05%/s、总上限 5.5% 之后，
     长镜头也有恒定速度的推进，短镜头不会推得太狠。 */
  C.push = (ev) => {
    const q = PV.clamp(ev.lt / Math.max(0.3, ev.dur));
    const total = Math.min(0.055, 0.0105 * ev.dur);
    return { s: 1.002 + total * amp(ev) * E.outCubic(q) };
  };

  /* 拉远：从近处缓慢退开，用于「唱完一句松开」 */
  C.pull = (ev) => {
    const q = PV.clamp(ev.lt / Math.max(0.3, ev.dur));
    const total = Math.min(0.050, 0.0095 * ev.dur);
    return { s: 1.002 + total * amp(ev) * (1 - E.outCubic(q)) };
  };

  /* 浮游：值噪声驱动的三轴微动，最像「手持」，抒情段首选 */
  C.float = (ev) => {
    const t = ev.lt * 0.85, sd = ev.seed | 0, A = amp(ev);
    return {
      x: noise1(t * 0.55, sd) * ev.W * 0.018 * A,
      y: (noise1(t * 0.48 + 7, sd + 1) * 0.72 + 0.28 * Math.sin(t * 1.15)) * ev.H * 0.020 * A,
      rot: noise1(t * 0.33 + 3, sd + 2) * 1.0 * A,
      s: 1.028 + 0.008 * Math.sin(t * 0.7),
    };
  };

  /* 周回：绕画面中心极缓地画圆，带一点倾斜 —— 读作「镜头在呼吸」 */
  C.orbit = (ev) => {
    const sd = ev.seed | 0, A = amp(ev);
    const dir = (sd & 1) ? 1 : -1;
    const th = (sd % 628) / 100 + dir * ev.lt * 1.05;
    const g = E.outCubic(PV.clamp(ev.lt / 0.8));
    return {
      x: Math.cos(th) * ev.W * 0.014 * A * g,
      y: Math.sin(th) * ev.H * 0.016 * A * g,
      rot: Math.sin(th) * 0.7 * A * g * dir,
      s: 1.030,
    };
  };

  /* 振り子：画框像是吊在画面上方，旋转与横移天然耦合（只做横移会读作「飘」） */
  C.pendulum = (ev) => {
    const sd = ev.seed | 0, A = amp(ev);
    const damp = 0.72 + 0.28 * Math.exp(-ev.lt * 0.55);
    const L = ev.W * 0.55;
    const deg = (0.85 + (sd % 40) / 100) * A;
    const per = 2.2 + (sd % 90) / 100;
    const phi = deg * Math.cos(ev.lt / per * TAU) * damp * RAD;
    return { x: -L * Math.sin(phi), y: L * (1 - Math.cos(phi)) * 0.45, rot: phi / RAD * 0.55, s: 1.014 };
  };

  /* 从上落定：镜头从下方升上来，缓出后停住 —— 句子「落位」的手感 */
  C.tiltDown = (ev) => {
    const q = E.outCubic(PV.clamp(ev.lt / Math.max(0.5, ev.dur * 0.55)));
    const a = ev.H * 0.024 * amp(ev);
    return { y: a * (1.0 - 1.45 * q), s: 1.032 - 0.022 * q };
  };

  /* 聚焦：从「略大且漂」收到稳定，模拟对焦时的一次呼吸。
     注意末端**不许收敛成静止**：原来 0.62s 收完之后整镜只剩 0.008 的常数，
     6.8s 的镜头有 6 秒画面纹丝不动（实测 146.5s 那一镜 p50 帧差 0.07）。
     现在收完之后接一段极缓的继续推进，读作「对上了焦，还在慢慢靠近」。 */
  C.focusIn = (ev) => {
    const q = E.outCubic(PV.clamp(ev.lt / 0.62));
    const u = PV.clamp(ev.lt / Math.max(0.6, ev.dur));
    return {
      s: 1 + 0.040 * amp(ev) * (1 - q) + 0.008 * q + 0.020 * amp(ev) * u,
      x: noise1(ev.lt * 1.4, ev.seed | 0) * ev.W * 0.005 * (1 - q),
      y: noise1(ev.lt * 1.1 + 5, (ev.seed | 0) + 3) * ev.H * 0.005 * (1 - q),
    };
  };

  /* 甩镜：前半段定格，中段（0.16s）猛地甩到另一边再定格。只在有强重音时用 */
  C.snapPan = (ev) => {
    const sd = ev.seed | 0, A = amp(ev);
    const dir = (sd & 1) ? 1 : -1;
    const dist = ev.W * 0.024 * A * dir;
    const at = ev.snapAt == null ? 0.5 : ev.snapAt;
    if (ev.dur < 1.2) return { x: dist * (1 - 2 * E.inOutCubic(PV.clamp(ev.lt / Math.max(0.4, ev.dur)))), s: 1.03 };
    const dt = ev.lt - ev.dur * at, q = E.inOutCubic(PV.clamp(dt / 0.16));
    const bell = dt > 0 && dt < 0.16 ? Math.sin(Math.PI * dt / 0.16) : 0;
    return { x: dist * (1 - 2 * q), s: 1.03 + 0.010 * bell, skx: -dir * 3.6 * A * bell };
  };

  /* 弹性：落地时挤压再回弹（squash & stretch）。重拍/大字句专用 */
  C.jelly = (ev) => {
    const sd = ev.seed | 0, A = amp(ev);
    const a = 0.034 * A;
    const f = 19 + (sd % 6);
    const w = Math.exp(-ev.lt * 4.6) * Math.cos(ev.lt * f);
    return { s: 1.052, sx: 1 + a * w, sy: 1 - a * w * 0.9, y: ev.H * 0.005 * A * w };
  };

  /* 纵向漂移：整镜极缓地向上/下走，长镜头里最有「时间在过」的感觉 */
  C.drift = (ev) => {
    const sd = ev.seed | 0;
    const dir = (sd % 3 === 0) ? -1 : 1;
    const u = PV.clamp(ev.lt / Math.max(0.4, ev.dur));
    return { y: dir * (u - 0.5) * ev.H * 0.024 * amp(ev), s: 1.020 + 0.010 * u };
  };

  PV.CAM_ORDER = ['push', 'float', 'orbit', 'pendulum', 'tiltDown', 'focusIn', 'drift', 'pull', 'jelly', 'snapPan'];

  /* 统一入口：拿不到就退化成 push（永远有一台在动的机器） */
  PV.camAt = (shot, t) => {
    const c = shot.cam || {};
    const fn = C[c.k] || C.push;
    const en = shot._en == null ? 0.5 : shot._en;
    const ev = {
      lt: t - shot.t0,
      dur: Math.max(0.2, shot.t1 - shot.t0),
      W: PV.W, H: PV.H,
      A: c.amp == null ? 1 : c.amp,       // 镜头强度（导演层给 0.85~1.15）
      K: 0.6 + 0.6 * en,                  // 段落强度（energy）
      seed: (shot.i | 0) * 7919 + 13,
      snapAt: c.at,
    };
    const r = fn(ev) || {};
    /* 基线微动：**所有**相机都必须一直在动。
       push / focusIn 这类「有明确终点」的手法走到终点就完全静止 —— 8.2s 的片头
       标题卡在成片里 p50 帧差只有 0.08（等价于静帧），第一眼就是一张图。
       这里叠加一个与手法无关的慢漂移（±0.45%W / ±0.5%H / ±0.1°，周期 5~10s）：
       小到看不出「在动」，但整帧永远不是死的。仍然是纯函数，只依赖 lt 与 seed。 */
    const sd0 = ev.seed | 0;
    const bx = noise1(ev.lt * 0.52, sd0 + 11) * ev.W * 0.0075;
    const by = noise1(ev.lt * 0.45 + 9, sd0 + 12) * ev.H * 0.0080;
    const br = noise1(ev.lt * 0.34 + 17, sd0 + 13) * 0.13;
    const gx = (r.x || 0) + bx, gy = (r.y || 0) + by;
    /* 位移必须由缩放兜底：平移 |x| 而放大只有 s 时，露出的边距是
       |x| - W(1-1/s)/2，>0 就是一条纯色硬边。反解 s >= W/(W-2|x|)。 */
    const fx = Math.abs(gx), fy = Math.abs(gy);
    const needX = fx > 0 ? PV.W / Math.max(1, PV.W - 2 * fx) : 1;
    const needY = fy > 0 ? PV.H / Math.max(1, PV.H - 2 * fy) : 1;
    const s0 = r.s == null ? 1.002 : r.s;
    return {
      x: gx, y: gy,
      s: Math.max(s0, needX, needY, 1.0),
      sx: r.sx == null ? 1 : r.sx,
      sy: r.sy == null ? 1 : r.sy,
      rot: (r.rot || 0) + br, skx: r.skx || 0,
    };
  };
})();
