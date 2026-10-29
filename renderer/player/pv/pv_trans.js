/*! pv_trans.js —— 切间转场（JIZURA 的 TRANS 分组）。
 *
 *  契约与 JIZURA 不同的一点：JIZURA 是「A/B 两帧都在手上，合成它们」；
 *  我们是逐帧顺序渲染，画布上**已经是新镜头（B）**，手上只有上一镜的
 *  快照（A）。所以每个转场的语义统一为「把 A 按某种几何盖在 B 上面，
 *  随 p→1 逐步让位」。能表达的转场和 JIZURA 完全等价（wipe/blinds/iris/
 *  blocks/push/zoom/slice/ink/flash/clock 十种），只是合成方向反过来了。
 *
 *  纪律：
 *   · 纯函数：同一 (A, p, I) 永远同一输出，参数里的随机一律走 I.seed；
 *   · p<=0 全盖（A 满屏），p>=1 全让（什么都不画，由调用方短路）；
 *   · 前沿那道「光」是转场读起来不像贴图滑动的关键，宁可少一种转场，
 *     也不要没有前沿的纯几何擦除。
 */
(() => {
  'use strict';
  const PV = window.PV;
  const { clamp, ease, rgba } = PV;
  const TAU = 6.283185307;

  const TR = (PV.TRANS = {});

  /* 前沿光条：擦到哪里亮到哪里。bell(p) 中段最亮、两端收掉。 */
  const bell = p => Math.sin(Math.PI * clamp(p, 0, 1));

  /* 转场加速色：优先用本镜 accent，退而求其次用 ink。 */
  function acc(I) {
    return I.accent || (I.S && I.S.accent) || '#FFFFFF';
  }

  /* ---------- 1. wipe：方向性边缘擦除 + 亮边 ---------- */
  TR.wipe = {
    name: '边缘擦除', dur: 0.30,
    draw(ctx, A, p, I) {
      const W = I.W, H = I.H, e = ease.inOutCubic(p);
      const dir = I.dir || 'L';
      const lw = Math.max(3, Math.min(W, H) * 0.006);
      ctx.save();
      ctx.beginPath();
      let bar;
      if (dir === 'R') { const x = W * e; ctx.rect(0, 0, x, H); bar = [x - lw / 2, 0, lw, H, -1]; }
      else if (dir === 'U') { const y = H * (1 - e); ctx.rect(0, y, W, H - y); bar = [0, y - lw / 2, W, lw, 1]; }
      else if (dir === 'D') { const y = H * e; ctx.rect(0, 0, W, y); bar = [0, y - lw / 2, W, lw, -1]; }
      else { const x = W * (1 - e); ctx.rect(x, 0, W - x, H); bar = [x - lw / 2, 0, lw, H, 1]; }
      ctx.clip();
      ctx.drawImage(A, 0, 0, I.W, I.H);
      ctx.restore();
      /* 前沿亮条 + 一根拖尾细线：没有它们，擦除读作「方块滑过」 */
      const a = Math.pow(bell(p), 0.6);
      ctx.save();
      ctx.globalAlpha = a; ctx.fillStyle = acc(I);
      ctx.fillRect(bar[0], bar[1], bar[2], bar[3]);
      const off = lw * 3.2 * bar[4];
      ctx.globalAlpha = a * 0.5;
      if (bar[2] === lw) ctx.fillRect(bar[0] + off, 0, Math.max(1, lw * 0.35), H);
      else ctx.fillRect(0, bar[1] + off, W, Math.max(1, lw * 0.35));
      ctx.restore();
    },
  };

  /* ---------- 2. blinds：百叶窗（横/竖条各自收窄） ---------- */
  TR.blinds = {
    name: '百叶窗', dur: 0.34,
    draw(ctx, A, p, I) {
      const W = I.W, H = I.H, e = ease.inOutCubic(p);
      const n = I.n || 10;
      const vert = I.dir === 'V';
      const L = vert ? W : H;
      const sl = L / n;
      ctx.save();
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const c = (i + 0.5) * sl;
        const half = sl * 0.5 * (1 - e);
        if (vert) ctx.rect(c - half, 0, half * 2, H);
        else ctx.rect(0, c - half, W, half * 2);
      }
      ctx.clip();
      ctx.drawImage(A, 0, 0, I.W, I.H);
      ctx.restore();
      /* 条与条之间的亮缝 */
      const a = Math.pow(bell(p), 0.7) * 0.55;
      if (a > 0.01) {
        ctx.save();
        ctx.globalAlpha = a; ctx.fillStyle = acc(I);
        const lw = Math.max(1.5, Math.min(W, H) * 0.0022);
        for (let i = 0; i <= n; i++) {
          const c = i * sl;
          if (vert) ctx.fillRect(c - lw / 2, 0, lw, H);
          else ctx.fillRect(0, c - lw / 2, W, lw);
        }
        ctx.restore();
      }
    },
  };

  /* ---------- 3. clockWipe：钟表扫（径向扇形收） ---------- */
  TR.clock = {
    name: '钟表扫', dur: 0.38,
    draw(ctx, A, p, I) {
      const W = I.W, H = I.H, e = ease.inOutCubic(p);
      const cx = W * (I.x == null ? 0.5 : I.x), cy = H * (I.y == null ? 0.5 : I.y);
      const R = Math.hypot(Math.max(cx, W - cx), Math.max(cy, H - cy)) + 4;
      const a0 = (I.a0 == null ? -90 : I.a0) * Math.PI / 180;
      const dir = I.spin == null ? 1 : I.spin;
      /* A 还占的扇形 = 从 a0 反方向扫过的剩余角 */
      const a1 = a0 + (1 - e) * TAU * dir;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, R, a0, a1, dir < 0);
      ctx.closePath();
      ctx.clip();
      ctx.drawImage(A, 0, 0, I.W, I.H);
      ctx.restore();
      const a = Math.pow(bell(p), 0.5), lw = Math.max(3, Math.min(W, H) * 0.005);
      ctx.save();
      ctx.globalAlpha = a; ctx.strokeStyle = acc(I); ctx.lineWidth = lw; ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(a1) * R, cy + Math.sin(a1) * R);
      ctx.stroke();
      ctx.fillStyle = acc(I);
      ctx.beginPath(); ctx.arc(cx, cy, lw * 2.2, 0, TAU); ctx.fill();
      ctx.restore();
    },
  };

  /* ---------- 4. iris：圆孔打开（A 只剩圆外） ---------- */
  TR.iris = {
    name: '光圈开', dur: 0.34,
    draw(ctx, A, p, I) {
      const W = I.W, H = I.H, e = ease.inOutCubic(p);
      const cx = W * (I.x == null ? 0.5 : I.x), cy = H * (I.y == null ? 0.5 : I.y);
      const R = Math.hypot(Math.max(cx, W - cx), Math.max(cy, H - cy)) + 4;
      const r = R * e;
      if (r > 0.5) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(0, 0, W, H);
        ctx.arc(cx, cy, r, 0, TAU, true);   // even-odd：圆外保留
        ctx.clip('evenodd');
        ctx.drawImage(A, 0, 0, I.W, I.H);
        ctx.restore();
      } else {
        ctx.drawImage(A, 0, 0, I.W, I.H);
      }
      const a = Math.pow(bell(p), 0.6), lw = Math.max(3, Math.min(W, H) * 0.008);
      ctx.save();
      ctx.globalAlpha = a; ctx.strokeStyle = acc(I); ctx.lineWidth = lw;
      ctx.beginPath(); ctx.arc(cx, cy, r + lw / 2, 0, TAU); ctx.stroke();
      ctx.globalAlpha = a * 0.6; ctx.lineWidth = lw * 0.4;
      ctx.beginPath(); ctx.arc(cx, cy, r * 1.06 + lw * 2.5, 0, TAU); ctx.stroke();
      ctx.restore();
    },
  };

  /* ---------- 5. blocks：方格按种子顺序消解 ---------- */
  TR.blocks = {
    name: '方格消解', dur: 0.36,
    draw(ctx, A, p, I) {
      const W = I.W, H = I.H;
      const nx = I.nx || 8, ny = I.ny || 5;
      const cw = W / nx, ch = H / ny;
      const seed = (I.seed | 0) || 7;
      const order = [];
      for (let y = 0; y < ny; y++) for (let x = 0; x < nx; x++) {
        const h = ((x * 73856093) ^ (y * 19349663) ^ seed) >>> 0;
        order.push([x, y, h % 4096]);
      }
      order.sort((a, b) => a[2] - b[2]);
      const n = order.length;
      ctx.save();
      for (let i = 0; i < n; i++) {
        const thr = i / n;
        const q = clamp((p - thr * 0.85) / 0.15, 0, 1);
        if (q >= 1) continue;              // 该格已让位
        const [x, y] = order[i];
        /* 让位时向中心缩一点 + 淡出，读作「崩掉」而不是「关灯」 */
        const k = 1 - ease.inCubic(q);
        const bx = x * cw, by = y * ch;
        const mx = bx + cw / 2, my = by + ch / 2;
        const hw = cw * 0.5 * (0.35 + 0.65 * k), hh = ch * 0.5 * (0.35 + 0.65 * k);
        ctx.save();
        ctx.beginPath();
        ctx.rect(mx - hw, my - hh, hw * 2, hh * 2);
        ctx.clip();
        ctx.globalAlpha = Math.min(1, k * 1.6);
        ctx.drawImage(A, 0, 0, I.W, I.H);
        ctx.restore();
      }
      ctx.restore();
    },
  };

  /* ---------- 6. flash：闪白交叉（A → 白 → 露 B） ---------- */
  TR.flash = {
    name: '闪白', dur: 0.26,
    draw(ctx, A, p, I) {
      const W = I.W, H = I.H;
      /* 前 55% A 还在（亮度抬高），后 45% 已经让位，只留白闪 */
      const a = p < 0.55 ? 1 - ease.inQuad(p / 0.55) : 0;
      if (a > 0.004) {
        ctx.save();
        ctx.globalAlpha = a;
        ctx.drawImage(A, 0, 0, I.W, I.H);
        /* A 自身也过曝一下，读作「光把上一镜吃掉」 */
        ctx.globalCompositeOperation = 'lighter';
        ctx.globalAlpha = a * 0.55 * bell(p / 0.55);
        ctx.fillStyle = '#FFFFFF';
        ctx.fillRect(0, 0, W, H);
        ctx.restore();
      }
      /* 中点一次纯白爆闪 */
      const fl = bell(p);
      if (fl > 0.01) {
        ctx.save();
        ctx.globalAlpha = Math.pow(fl, 1.6) * 0.92;
        ctx.fillStyle = I.flashCol || '#FFFFFF';
        ctx.fillRect(0, 0, W, H);
        ctx.restore();
      }
    },
  };

  /* ---------- 7. push：A 整幅推走（B 在底下被露出来） ---------- */
  TR.push = {
    name: '推走', dur: 0.30,
    draw(ctx, A, p, I) {
      const W = I.W, H = I.H, e = ease.inOutCubic(p);
      const dir = I.dir || 'L';
      const h = dir === 'L' || dir === 'R';
      const L = h ? W : H;
      const sg = (dir === 'L' || dir === 'U') ? -1 : 1;
      const off = Math.round(e * L) * sg;
      ctx.save();
      if (h) ctx.drawImage(A, off, 0, I.W, I.H);
      else ctx.drawImage(A, 0, off, I.W, I.H);
      /* 推走时上一镜压暗一点，避免两屏内容同时抢注意力 */
      ctx.fillStyle = '#000000';
      ctx.globalAlpha = 0.28 * e;
      ctx.fillRect(0, 0, W, H);
      ctx.restore();
      /* 接缝亮条 */
      const lw = Math.max(3, Math.min(W, H) * 0.005);
      const q = h ? (sg < 0 ? W + off : off) : (sg < 0 ? H + off : off);
      ctx.save();
      ctx.globalAlpha = Math.pow(bell(p), 0.6) * 0.85;
      ctx.fillStyle = acc(I);
      if (h) ctx.fillRect(q - lw / 2, 0, lw, H);
      else ctx.fillRect(0, q - lw / 2, W, lw);
      ctx.restore();
    },
  };

  /* ---------- 8. zoom：A 放大冲出画面（透视穿越） ---------- */
  TR.zoom = {
    name: '穿越放大', dur: 0.30,
    draw(ctx, A, p, I) {
      const W = I.W, H = I.H, e = ease.inCubic(p);
      const s = 1 + e * 1.6;
      const a = 1 - ease.inQuad(p);
      if (a <= 0.004) return;
      ctx.save();
      ctx.globalAlpha = a;
      ctx.translate(W / 2, H / 2);
      ctx.scale(s, s);
      ctx.translate(-W / 2, -H / 2);
      ctx.drawImage(A, 0, 0, I.W, I.H);
      ctx.restore();
      /* 边缘极淡的径向亮晕：强化「往里穿」的方向感 */
      const g = ctx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.12,
        W / 2, H / 2, Math.max(W, H) * 0.62);
      g.addColorStop(0, rgba([255, 255, 255], 0));
      g.addColorStop(1, rgba([255, 255, 255], 0.16 * bell(p)));
      ctx.save();
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
      ctx.restore();
    },
  };

  /* ---------- 9. slice：短册错位（横条交替平移后淡出） ---------- */
  TR.slice = {
    name: '短册错位', dur: 0.32,
    draw(ctx, A, p, I) {
      const W = I.W, H = I.H, e = ease.inOutCubic(p);
      const n = I.n || 9;
      const sh = H / n;
      const amp = W * 0.16;
      const seed = (I.seed | 0) || 3;
      const a0 = 1 - ease.inQuad(p);
      if (a0 <= 0.004) return;
      for (let i = 0; i < n; i++) {
        /* 奇偶列反向，中间快两头慢 —— 读作「一叠纸被推开」 */
        const phase = ((seed >> (i % 8)) & 1) ? 1 : -1;
        const q = clamp((p - (i / n) * 0.25) / 0.75, 0, 1);
        const dx = phase * amp * ease.inCubic(q);
        ctx.save();
        ctx.beginPath();
        ctx.rect(0, i * sh, W, sh + 1);
        ctx.clip();
        ctx.globalAlpha = a0 * (1 - q * 0.35);
        ctx.drawImage(A, dx, 0, I.W, I.H);
        ctx.restore();
      }
    },
  };

  /* ---------- 10. ink：墨点晕开（B 从几个有机斑点长满） ---------- */
  TR.ink = {
    name: '墨晕', dur: 0.42,
    draw(ctx, A, p, I) {
      const W = I.W, H = I.H, e = ease.outCubic(p);
      const seed = (I.seed | 0) || 11;
      const r = (j, k) => PV.rng(seed, j, k)();
      const N = 6;
      const R0 = Math.hypot(W, H) * 0.52;
      ctx.save();
      /* 圆外保留 A：把 N 个「已经长起来的墨斑」从 A 上挖掉 */
      ctx.beginPath();
      ctx.rect(0, 0, W, H);
      for (let i = 0; i < N; i++) {
        const cx = W * (0.12 + 0.76 * r(i, 1));
        const cy = H * (0.12 + 0.76 * r(i, 2));
        /* 每个斑点有自己的起跑线，读作「墨在纸上洇开」而不是「圆在长大」 */
        const q = clamp((e - r(i, 3) * 0.35) / 0.65, 0, 1);
        const rad = R0 * (0.22 + 0.42 * r(i, 4)) * q;
        if (rad <= 1) continue;
        /* 用 8 段折线拟合一个略不圆的斑 */
        ctx.moveTo(cx + rad, cy);
        for (let k = 1; k <= 8; k++) {
          const ang = k / 8 * TAU;
          const rr = rad * (0.86 + 0.28 * r(i, 10 + k));
          ctx.lineTo(cx + Math.cos(ang) * rr, cy + Math.sin(ang) * rr);
        }
        ctx.closePath();
      }
      ctx.clip('evenodd');
      ctx.drawImage(A, 0, 0, I.W, I.H);
      ctx.restore();
    },
  };

  /* ---------- 11. dissolve：叠化（A 单纯褪掉，B 一直在下面） ---------- */
  /* 手书最常用的一种「过渡」其实不是过渡，是**两张画同时存在**——
     所以这里不加任何前沿光、不加位移：加一点东西就变成特效了。 */
  TR.dissolve = {
    name: '叠化', dur: 0.60,
    draw(ctx, A, p, I) {
      const e = ease.inOutCubic(p);
      const a = 1 - e;
      if (a <= 0.004) return;
      ctx.save();
      ctx.globalAlpha = a;
      ctx.drawImage(A, 0, 0, I.W, I.H);
      ctx.restore();
    },
  };

  /* ---------- 12. grad：渐变擦除（软边前沿，化开而不是推走） ----------
     wipe 的前沿是一条硬边 + 一道亮线；grad 的前沿是一条 34% 画幅的软坡。
     两者画面差别很大：硬边读作「换镜」，软坡读作「化开」——手书里后者才是常态。 */
  TR.grad = {
    name: '渐变擦除', dur: 0.52,
    draw(ctx, A, p, I) {
      const W = I.W, H = I.H, e = ease.inOutCubic(p);
      const aw = A.width || W, ah = A.height || H;
      const o = PV.offscreen('transGrad', aw, ah, 1);
      o.ctx.drawImage(A, 0, 0);
      const band = 0.34;
      const f = e * (1 + band) - band;            // 前沿位置（0..1，负值 = 还在画外）
      /* 方向语义与 wipe 一致：L/R 沿横轴、U/D 沿纵轴；反向时渐变起点对调 */
      const horiz = I.dir !== 'U' && I.dir !== 'D';
      const revDir = I.dir === 'R' || I.dir === 'U';
      const gx0 = horiz ? (revDir ? aw : 0) : 0;
      const gx1 = horiz ? (revDir ? 0 : aw) : 0;
      const gy0 = horiz ? 0 : (revDir ? ah : 0);
      const gy1 = horiz ? 0 : (revDir ? 0 : ah);
      const g = o.ctx.createLinearGradient(gx0, gy0, gx1, gy1);
      /* destination-in：源 alpha 为 0 的地方被挖掉 ⇒ 落后于前沿的一侧先空出来 */
      const s0 = clamp(f, 0, 0.998);
      const s1 = Math.max(s0 + 0.002, clamp(f + band, 0.002, 1));
      g.addColorStop(s0, 'rgba(0,0,0,0)');
      g.addColorStop(s1, 'rgba(0,0,0,1)');
      o.ctx.globalCompositeOperation = 'destination-in';
      o.ctx.fillStyle = g;
      o.ctx.fillRect(0, 0, aw, ah);
      o.ctx.globalCompositeOperation = 'source-over';
      ctx.drawImage(o.c, 0, 0, W, H);
    },
  };

  /* ---------- 13. mask：圆角矩遮罩扩张（A 中间被一块圆角咬开） ---------- */
  /* iris 是圆形从中心张开；mask 是**画框形状**从中心张开 ——
     和片子里的拱窗/分屏同形，转场于是属于同一个视觉语汇，而不是外来特效。 */
  TR.mask = {
    name: '遮罩扩张', dur: 0.46,
    draw(ctx, A, p, I) {
      const W = I.W, H = I.H, e = ease.outCubic(p);
      const cx = W * (I.x == null ? 0.5 : I.x), cy = H * (I.y == null ? 0.5 : I.y);
      const k = 1 + e * 1.35;                     // 咬开的口子随 p 长到盖过整幅
      const rw = W * 0.30 * k, rh = H * 0.34 * k;
      const rad = Math.min(rw, rh) * (I.rad == null ? 0.18 : I.rad);
      const x0 = cx - rw / 2, y0 = cy - rh / 2;
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, W, H);
      const rr = Math.max(1, Math.min(rad, Math.min(rw, rh) / 2));
      ctx.moveTo(x0 + rr, y0);
      ctx.lineTo(x0 + rw - rr, y0);
      ctx.arcTo(x0 + rw, y0, x0 + rw, y0 + rr, rr);
      ctx.lineTo(x0 + rw, y0 + rh - rr);
      ctx.arcTo(x0 + rw, y0 + rh, x0 + rw - rr, y0 + rh, rr);
      ctx.lineTo(x0 + rr, y0 + rh);
      ctx.arcTo(x0, y0 + rh, x0, y0 + rh - rr, rr);
      ctx.lineTo(x0, y0 + rr);
      ctx.arcTo(x0, y0, x0 + rr, y0, rr);
      ctx.closePath();
      ctx.clip('evenodd');
      ctx.drawImage(A, 0, 0, W, H);
      ctx.restore();
      /* 咬开的边缘一道极淡的亮线：没有它，扩张读作「画面里有块黑」 */
      const a = Math.pow(bell(p), 0.8) * 0.5;
      if (a > 0.01) {
        ctx.save();
        ctx.globalAlpha = a;
        ctx.strokeStyle = acc(I);
        ctx.lineWidth = Math.max(2, Math.min(W, H) * 0.0032);
        ctx.beginPath();
        ctx.moveTo(x0 + rr, y0);
        ctx.lineTo(x0 + rw - rr, y0);
        ctx.arcTo(x0 + rw, y0, x0 + rw, y0 + rr, rr);
        ctx.lineTo(x0 + rw, y0 + rh - rr);
        ctx.arcTo(x0 + rw, y0 + rh, x0 + rw - rr, y0 + rh, rr);
        ctx.lineTo(x0 + rr, y0 + rh);
        ctx.arcTo(x0, y0 + rh, x0, y0 + rh - rr, rr);
        ctx.lineTo(x0, y0 + rr);
        ctx.arcTo(x0, y0, x0 + rr, y0, rr);
        ctx.closePath();
        ctx.stroke();
        ctx.restore();
      }
    },
  };

  /* ---------- 编排辅助 ----------
     导演层不写参数时，引擎按镜头号确定性推一套默认参数。
     同一镜头永远同一参数（seed 只吃 shot.i），双渲字节一致不受影响。 */
  TR.DIRS = ['L', 'R', 'U', 'D'];
  TR.derive = (k, shot) => {
    const seed = ((shot.i | 0) * 2654435761) >>> 0;
    const r = (a, b) => PV.rng(seed, a, b)();
    const d = { k, seed };
    if (k === 'wipe' || k === 'push') {
      const dirs = ['L', 'L', 'R', 'R', 'U', 'D'];   // 横向为主：竖切会把整行字拦腰截断
      d.dir = dirs[Math.floor(r(1, 1) * dirs.length) % dirs.length];
    } else if (k === 'grad') {
      const dirs = ['L', 'L', 'R', 'D', 'U'];
      d.dir = dirs[Math.floor(r(7, 1) * dirs.length) % dirs.length];
    } else if (k === 'mask') {
      d.x = 0.5 + (r(8, 1) - 0.5) * 0.20;
      d.y = 0.5 + (r(8, 2) - 0.5) * 0.16;
    } else if (k === 'blinds') {
      d.dir = r(2, 1) < 0.72 ? 'H' : 'V';
      d.n = 8 + Math.floor(r(2, 2) * 6);
    } else if (k === 'clock') {
      d.spin = r(3, 1) < 0.7 ? 1 : -1;
      d.a0 = [-90, -90, 0, 180][Math.floor(r(3, 2) * 4) % 4];
    } else if (k === 'iris') {
      d.x = 0.5 + (r(4, 1) - 0.5) * 0.24;
      d.y = 0.5 + (r(4, 2) - 0.5) * 0.20;
    } else if (k === 'blocks') {
      d.nx = 6 + Math.floor(r(5, 1) * 5);
      d.ny = 4 + Math.floor(r(5, 2) * 4);
    } else if (k === 'slice') {
      d.n = 7 + Math.floor(r(6, 1) * 5);
    }
    return d;
  };

  /* 全部转场名（导演层从这里取池子） */
  TR.ORDER = ['wipe', 'blinds', 'clock', 'iris', 'blocks', 'flash', 'push', 'zoom', 'slice', 'ink',
    'dissolve', 'grad', 'mask'];
})();
