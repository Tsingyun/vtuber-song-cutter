/* ------------------------------------------------------------------
   Live Cue — 播放器前端
   数据：/api/index → track{ video, lrc, track, font, ... }
   · 视频：真实片段，按人物轨迹做 GPU 纹理平移裁切（不重绘、不搬运像素）
   · 歌词：现有 .lrc 轴（cut 相对时间），不重算任何时间
   · 进度：每一帧读 video.currentTime，与音频硬同步
   ------------------------------------------------------------------ */
(() => {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const stage = $('#stage'), vid = $('#vid'), portrait = $('#portrait');
  const trackEl = $('#lyricTrack'), viewEl = $('#lyricView');
  const pFill = $('#pFill'), pDot = $('#pDot'), progress = $('#progress');
  const tCur = $('#tCur'), tTot = $('#tTot');
  const lnNow = $('#lnNow'), lnAll = $('#lnAll');
  const mTitle = $('#mTitle'), mOrig = $('#mOrig'), mVocal = $('#mVocal'), mDate = $('#mDate');
  const gate = $('#gate'), loading = $('#loading'), errBox = $('#err');
  const amb = $('#ambient'), actx = amb.getContext('2d', { willReadFrequently: true });

  const Q = new URLSearchParams(location.search);
  const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);

  const S = {
    tracks: [], meta: null, lines: [], active: -1,
    dur: 0, srcW: 1920, srcH: 1080, cropH: 980, cropTop: 0, biasX: 56,
    panelW: 0, panelH: 0, cx: 960, cxTarget: 960,
    cxArr: null, trackFps: 4, ready: false,
    lastSample: 0, lastSec: -1, lastP: -1, raf: 0,
  };

  /* ------------------------------------------------ LRC：与 songcut 同口径 */
  const TS_RE = /\[(\d+):(\d+(?:[.:]\d+)?)\]/g;
  const WORD_RE = /<(\d+):(\d+(?:\.\d+)?)>/g;

  function parseLRC(text) {
    const out = [];
    const seen = new Set();
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.trim();
      if (!line || line[0] === '#') continue;
      const tags = [...line.matchAll(TS_RE)];
      if (!tags.length) continue;
      let body = line.replace(TS_RE, '').trim();
      let fill = null;
      const mEnd = body.match(/<(\d+):(\d+(?:\.\d+)?)>\s*$/);
      if (mEnd) { fill = +mEnd[1] * 60 + +mEnd[2]; body = body.slice(0, mEnd.index).trim(); }
      body = body.replace(WORD_RE, '').trim();
      if (!body) continue;
      for (const t of tags) {
        const sec = +t[1] * 60 + parseFloat(t[2].replace(':', '.'));
        const key = sec.toFixed(2) + '|' + body;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({ t: sec, text: body, fill });
      }
    }
    out.sort((a, b) => a.t - b.t);
    return out;
  }

  /* 逐字权重：假名/汉字按 1，拉丁字母与空格更短 */
  function charWeights(text) {
    const w = [];
    for (let ci = 0; ci < text.length; ci++) {
      const ch = text[ci];
      if (ch === ' ') w.push(0.30);
      else if (/[\u3040-\u30ff\u3400-\u9fff]/.test(ch)) w.push(1);
      else if (/[a-zA-Z0-9]/.test(ch)) w.push(0.58);
      else w.push(0.42);
    }
    const sum = w.reduce((a, b) => a + b, 0) || 1;
    let acc = 0;
    return w.map((x) => (acc += x) / sum);     // 累计归一 → 时刻比例
  }

  function measureLine(el) {
    const base = el.querySelector('.base');
    const node = base.firstChild;
    if (!node || !node.length) return [];
    const r = document.createRange();
    const box = el.getBoundingClientRect();          // 列宽（含 scale，比例不受影响）
    const origin = base.getBoundingClientRect().left;
    const offs = [];
    const len = node.length;
    for (let i = 0; i < len; i++) {
      r.setStart(node, i); r.setEnd(node, i + 1);
      const rect = r.getBoundingClientRect();
      offs.push((rect.left - origin + rect.width) / box.width);
    }
    return offs;
  }

  /* ------------------------------------------------ 构建歌词 */
  function buildLyrics(lines) {
    S.lines = lines;
    trackEl.innerHTML = '';
    const frag = document.createDocumentFragment();
    for (let i = 0; i < lines.length; i++) {
      const d = document.createElement('div');
      d.className = 'line future';
      d.dataset.i = i;
      const b = document.createElement('span');
      b.className = 'base';
      b.textContent = lines[i].text;
      const f = document.createElement('span');
      f.className = 'fill';
      f.dataset.text = lines[i].text;
      f.setAttribute('aria-hidden', 'true');
      d.append(b, f);
      frag.appendChild(d);
      lines[i].el = d;
      lines[i].w = charWeights(lines[i].text);
    }
    trackEl.appendChild(frag);
    lnAll.textContent = String(lines.length).padStart(3, '0');
    S.active = -1;
  }

  function layoutTrack() {
    // 还没到第一行时也把第 0 行摆到锚点，避免整列贴在顶部
    const idx = S.active < 0 ? 0 : S.active;
    if (!S.lines[idx]) return;
    const el = S.lines[idx].el;
    const anchor = viewEl.clientHeight * 0.46;
    const y = anchor - (el.offsetTop + el.offsetHeight * 0.5);
    trackEl.style.transform = `translate3d(0, ${y.toFixed(1)}px, 0)`;
  }

  /* 字号自适应：整列同字号，按最长一行收，保证活动行放大后仍不溢出 */
  function fitLyrics() {
    if (!S.lines.length) return;
    const col = viewEl.clientWidth;
    const stageW = stage.clientWidth || 1600;
    const ref = 100;
    trackEl.style.fontSize = ref + 'px';
    let maxW = 0;
    for (const ln of S.lines) {
      const b = ln.el.querySelector('.base');
      if (b) maxW = Math.max(maxW, b.getBoundingClientRect().width);
    }
    let fs = stageW * 0.0215;
    if (maxW > 0) fs = Math.min(fs, col * 0.985 / (maxW / ref) / 1.22);
    fs = Math.max(stageW * 0.0128, fs);
    trackEl.style.fontSize = fs.toFixed(2) + 'px';
    for (const ln of S.lines) ln.offs = null;
    layoutTrack();
  }

  function setActive(i) {
    if (i === S.active) return;
    S.active = i;
    for (let k = 0; k < S.lines.length; k++) {
      const ln = S.lines[k];
      const d = k - i;
      ln.el.className = 'line ' + (d === 0 ? 'active' : d < 0 ? 'past' : 'future');
      ln.el.style.opacity = d === 0 ? 1 : d === -1 ? .62 : d === -2 ? .4 : d === -3 ? .24 :
        d === 1 ? .9 : d === 2 ? .62 : d === 3 ? .34 : .14;
      if (d === 0) {
        ln.offs = measureLine(ln.el);
        // 活动行换行后列宽可能变化，其余行缓存作废（行数少，重建代价可忽略）
        for (let j = 0; j < S.lines.length; j++) if (j !== i) S.lines[j].offs = null;
      }
    }
    lnNow.textContent = String(i + 1).padStart(3, '0');
    layoutTrack();
  }

  /* ------------------------------------------------ 裁切跟随 */
  /* 取景：固定高度 + 顶部对齐（人物头顶永不被切），只做水平跟随 */
  function initCrop(tk) {
    S.cropH = (S.meta && S.meta.cropH) || 980;
    S.biasX = (S.meta && S.meta.cropBiasX) || 56;
    S.cropTop = 0;
  }

  function resizePanel() {
    const r = portrait.getBoundingClientRect();
    S.panelW = r.width; S.panelH = r.height;
    const s = S.panelH / S.cropH;
    vid.style.width = (S.srcW * s).toFixed(1) + 'px';
    vid.style.height = (S.srcH * s).toFixed(1) + 'px';
    layoutTrack();
  }

  function cxAt(t) {    const a = S.cxArr;
    if (!a || !a.length) return S.srcW / 2;
    const f = clamp(t, 0, (a.length - 1) / S.trackFps) * S.trackFps;
    const i = Math.floor(f), fr = f - i;
    const v0 = a[i], v1 = a[Math.min(i + 1, a.length - 1)];
    return v0 + (v1 - v0) * fr;
  }

  function applyCrop(t, dt) {
    if (!S.panelW) return;
    const target = cxAt(t);
    const k = 1 - Math.exp(-Math.max(dt, 0.008) / 0.20);
    S.cx += (target - S.cx) * k;
    if (Math.abs(target - S.cx) < 0.5) S.cx = target;
    const s = S.panelH / S.cropH;
    const cropW = S.cropH * (S.panelW / S.panelH);
    // 取景中心略向右偏：给人物右侧留呼吸位，同时把左侧直播 UI（弹幕）挤出画面
    const cx = clamp(S.cx + S.biasX, cropW / 2, S.srcW - cropW / 2);
    const tx = S.panelW / 2 - cx * s;
    const ty = S.panelH / 2 - (S.cropTop + S.cropH / 2) * s;
    if (S._tx === undefined || Math.abs(S._tx - tx) > 0.02 || Math.abs(S._ty - ty) > 0.02) {
      S._tx = tx; S._ty = ty;
      vid.style.transform = `translate3d(${tx.toFixed(2)}px, ${ty.toFixed(2)}px, 0)`;
    }
  }

  /* ------------------------------------------------ 环境取色 */
  function sampleAmbient() {
    if (vid.readyState < 2 || vid.paused) return;
    try { actx.drawImage(vid, 0, 0, 96, 54); } catch (e) { return; }
    let d;
    try { d = actx.getImageData(0, 0, 96, 54).data; } catch (e) { return; }
    const mean = (x0, x1) => {
      let r = 0, g = 0, b = 0, w = 0;
      for (let y = 0; y < 54; y++) {
        for (let x = x0; x < x1; x++) {
          const i = (y * 96 + x) * 4;
          const R = d[i], G = d[i + 1], B = d[i + 2];
          const mx = Math.max(R, G, B), mn = Math.min(R, G, B);
          const ww = ((mx - mn) / 255) * (mx / 255) * (mx / 255) + 0.02;
          r += R * ww; g += G * ww; b += B * ww; w += ww;
        }
      }
      return [r / w, g / w, b / w];
    };
    const c1 = mean(0, 48), c2 = mean(48, 96);
    const tune = (c, k) => c.map((v) => clamp(v * k, 0, 255) | 0);
    const A = tune(c1, 0.62), B = tune(c2, 0.52);
    actx.globalCompositeOperation = 'source-over';
    actx.fillStyle = '#060608';
    actx.fillRect(0, 0, 96, 54);
    actx.globalCompositeOperation = 'lighter';
    let g = actx.createRadialGradient(26, 20, 0, 26, 20, 52);
    g.addColorStop(0, `rgba(${A[0]},${A[1]},${A[2]},0.95)`);
    g.addColorStop(0.55, `rgba(${A[0]},${A[1]},${A[2]},0.32)`);
    g.addColorStop(1, 'rgba(0,0,0,0)');
    actx.fillStyle = g; actx.fillRect(0, 0, 96, 54);
    g = actx.createRadialGradient(76, 40, 0, 76, 40, 46);
    g.addColorStop(0, `rgba(${B[0]},${B[1]},${B[2]},0.8)`);
    g.addColorStop(1, 'rgba(0,0,0,0)');
    actx.fillStyle = g; actx.fillRect(0, 0, 96, 54);
  }

  /* ------------------------------------------------ 主循环 */
  function fmt(s) {
    s = Math.max(0, s | 0);
    const m = (s / 60) | 0;
    return String(m).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  }

  let prevT = 0;
  function loop(now) {
    S.raf = requestAnimationFrame(loop);
    const t = vid.currentTime;
    const dt = Math.min(0.1, Math.max(0.004, t - prevT));
    prevT = t;

    applyCrop(t, dt);
    if (now - S.lastSample > 170) { S.lastSample = now; sampleAmbient(); }

    if (!S.ready || !S.dur) return;
    const p = clamp(t / S.dur, 0, 1);
    if (Math.abs(p - S.lastP) > 0.0004) {
      S.lastP = p;
      pFill.style.width = (p * 100).toFixed(3) + '%';
      pDot.style.left = (p * 100).toFixed(3) + '%';
    }
    const sec = t | 0;
    if (sec !== S.lastSec) { S.lastSec = sec; tCur.textContent = fmt(t); }

    let i = -1;
    const L = S.lines;
    for (let k = L.length - 1; k >= 0; k--) { if (L[k].t <= t + 0.02) { i = k; break; } }
    if (i !== S.active) setActive(i);
    if (i < 0) return;

    // 逐字填充
    const ln = L[i];
    const next = L[i + 1];
    const t0 = ln.t;
    let t1 = next ? next.t : t0 + Math.max(2.2, ln.text.length * 0.42);
    if (ln.fill && ln.fill > t0) t1 = Math.min(t1, ln.fill);
    // 长间奏：逐字时间不能跟着行间隔走，按字数估一个上限
    const est = 0.42 * ln.text.length + 0.9;
    t1 = Math.min(t1, t0 + Math.max(est * 1.6, 1.2));
    const span = Math.max(0.35, t1 - t0);
    let pr = clamp((t - t0) / span, 0, 1);
    if (!ln.offs) ln.offs = measureLine(ln.el);
    if (ln.offs && ln.offs.length) {      const w = ln.w;
      let idx = 0;
      while (idx < w.length - 1 && w[idx] < pr) idx++;
      const prevW = idx > 0 ? w[idx - 1] : 0;
      const seg = Math.max(0.0001, w[idx] - prevW);
      const frac = clamp((pr - prevW) / seg, 0, 1);
      const x0 = idx > 0 ? ln.offs[idx - 1] : 0;
      const x1 = ln.offs[idx];
      const x = x0 + (x1 - x0) * frac;
      const f = ln.el.querySelector('.fill');
      if (f._x === undefined || Math.abs(f._x - x) > 0.0004) {
        f._x = x;
        f.style.clipPath = `inset(-30% ${((1 - x) * 100).toFixed(2)}% -30% 0)`;
      }
    }
  }

  /* ------------------------------------------------ 播放控制 */
  function play() {
    if (vid.ended || (S.dur && vid.currentTime >= S.dur - 0.06)) vid.currentTime = 0;
    vid.play().then(() => gate.classList.add('hidden')).catch(() => {
      gate.classList.remove('hidden');
    });
  }
  function toggle() {
    if (vid.paused) play(); else vid.pause();
  }

  gate.addEventListener('click', (e) => { e.stopPropagation(); play(); });
  stage.addEventListener('click', (e) => {
    if (gate.contains(e.target) || progress.contains(e.target) || $('#setlist').contains(e.target)) return;
    if (!S.ready) return;
    toggle();
  });
  document.addEventListener('keydown', (e) => {
    if (e.code === 'Space') { e.preventDefault(); toggle(); }
    else if (e.code === 'ArrowRight') { e.preventDefault(); vid.currentTime = clamp(vid.currentTime + 5, 0, S.dur - .1); }
    else if (e.code === 'ArrowLeft') { e.preventDefault(); vid.currentTime = clamp(vid.currentTime - 5, 0, S.dur - .1); }
  });

  let dragging = false;
  const seekTo = (clientX) => {
    const r = progress.getBoundingClientRect();
    const p = clamp((clientX - r.left) / r.width, 0, 1);
    vid.currentTime = p * S.dur;
  };
  progress.addEventListener('pointerdown', (e) => {
    dragging = true; progress.setPointerCapture(e.pointerId); seekTo(e.clientX);
  });
  progress.addEventListener('pointermove', (e) => { if (dragging) seekTo(e.clientX); });
  progress.addEventListener('pointerup', (e) => {
    dragging = false;
    try { progress.releasePointerCapture(e.pointerId); } catch (_) {}
  });

  vid.addEventListener('play', () => {
    gate.classList.add('hidden');
    gate.querySelector('.cap').textContent = 'play';
  });
  vid.addEventListener('error', () => {
    loading.classList.add('hidden');
    errBox.style.display = 'block';
    errBox.textContent = '视频加载失败：' + (vid.error ? 'code ' + vid.error.code : '未知错误');
  });
  vid.addEventListener('ended', () => {
    gate.classList.remove('hidden');
    gate.querySelector('.cap').textContent = 'replay';
  });

  /* ------------------------------------------------ 初始化 */
  async function loadFont(src) {
    if (!src) return;
    try {
      const ff = new FontFace('LC Serif', `url(${src})`, { weight: '100 900' });
      await ff.load();
      document.fonts.add(ff);
    } catch (e) { console.warn('font fallback', e); }
  }

  function bindTrack(meta) {
    S.meta = meta;
    S.dur = meta.duration;
    S.ready = false;
    S.active = -1;
    vid.src = meta.video;
    vid.load();
    mTitle.textContent = meta.title || '—';
    mOrig.textContent = meta.artist ? 'original · ' + meta.artist : 'original';
    mVocal.textContent = 'vocal ' + (meta.vocal || '—');
    mDate.textContent = (meta.date || '').replace(/-/g, '.');
    $('#spineText').textContent =
      `${(meta.date || '').replace(/-/g, '.')} — live cue — no.${String((meta.index || 0) + 1).padStart(2, '0')}`;
    document.title = `${meta.title} · Live Cue`;
    tTot.textContent = fmt(meta.duration);
    history.replaceState(null, '', `?i=${meta.index}`);

    const nav = $('#setlist');
    nav.innerHTML = '';
    if (S.tracks.length > 1) {
      S.tracks.forEach((tr) => {
        const r = document.createElement('span');
        r.className = 'row' + (tr.index === meta.index ? ' on' : '');
        r.innerHTML = `<span>${String(tr.index + 1).padStart(2, '0')}</span>${tr.title}`;
        r.onclick = () => switchTo(tr.index);
        nav.appendChild(r);
      });
    }
  }

  async function switchTo(i) {
    const meta = S.tracks.find((t) => t.index === i) || S.tracks[0];
    if (!meta) return;
    if (meta === S.meta) return;
    gate.classList.add('hidden');
    loading.classList.remove('hidden');
    const [lrcText, tk] = await Promise.all([
      fetch(meta.lrc, { cache: 'no-store' }).then((r) => r.text()),
      fetch(meta.track, { cache: 'no-store' }).then((r) => r.json()).catch(() => null),
    ]);
    bindTrack(meta);
    setTrackData(lrcText, tk);
    await new Promise((res) => {
      if (vid.readyState >= 1) return res();
      vid.addEventListener('loadedmetadata', res, { once: true });
    });
    S.srcW = vid.videoWidth || tk?.w || 1920;
    S.srcH = vid.videoHeight || tk?.h || 1080;
    S.cx = S.cxArr ? S.cxArr[0] : S.srcW / 2;
    resizePanel();
    S.ready = true;
    S.dur = vid.duration || meta.duration;
    tTot.textContent = fmt(S.dur);
    loading.classList.add('hidden');
    play();
  }

  function setTrackData(lrcText, tk) {
    buildLyrics(parseLRC(lrcText));
    S.cxArr = tk && tk.cx && tk.cx.length ? Float32Array.from(tk.cx) : null;
    S.trackFps = tk?.fps || 4;
    if (tk && tk.w) { S.srcW = tk.w; S.srcH = tk.h; }
    initCrop(tk);
    trackEl.style.transition = 'none';
    fitLyrics();
    requestAnimationFrame(() => {
      trackEl.style.transition = '';
      fitLyrics();
    });
  }

  async function boot() {
    // 噪点纹理
    const nc = document.createElement('canvas');
    nc.width = nc.height = 128;
    const nx = nc.getContext('2d');
    const id = nx.createImageData(128, 128);
    for (let i = 0; i < id.data.length; i += 4) {
      const v = 110 + (Math.random() * 90) | 0;
      id.data[i] = id.data[i + 1] = id.data[i + 2] = v;
      id.data[i + 3] = 255;
    }
    nx.putImageData(id, 0, 0);
    $('#grain').style.backgroundImage = `url(${nc.toDataURL()})`;

    try {
      const res = await fetch('/api/index', { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const j = await res.json();
      S.tracks = (j.tracks || []).slice();
    } catch (e) {
      loading.classList.add('hidden');
      errBox.style.display = 'block';
      errBox.textContent = '没有读到曲目索引。先运行：python tools/liveplayer.py prepare --date <日期> --index 0';
      return;
    }
    if (!S.tracks.length) {
      loading.classList.add('hidden');
      errBox.style.display = 'block';
      errBox.textContent = '索引为空：先 prepare 一首歌。';
      return;
    }

    const want = parseInt(Q.get('i') || '0', 10) || 0;
    const meta = S.tracks.find((t) => t.index === want) || S.tracks[0];
    await loadFont(meta.font);
    await document.fonts.ready;

    let lrcText = '', tk = null;
    try {
      const rs = await Promise.all([
        fetch(meta.lrc, { cache: 'no-store' }),
        fetch(meta.track, { cache: 'no-store' }).catch(() => null),
      ]);
      if (!rs[0].ok) throw new Error('lrc HTTP ' + rs[0].status);
      lrcText = await rs[0].text();
      tk = rs[1] && rs[1].ok ? await rs[1].json() : null;
    } catch (e) {
      loading.classList.add('hidden');
      errBox.style.display = 'block';
      errBox.textContent = '曲目素材读不到：' + e.message + '（重新 prepare 一次）';
      return;
    }
    bindTrack(meta);
    setTrackData(lrcText, tk);

    await new Promise((res) => {
      if (vid.readyState >= 1) return res();
      vid.addEventListener('loadedmetadata', res, { once: true });
      setTimeout(res, 8000);
    });
    S.srcW = vid.videoWidth || S.srcW;
    S.srcH = vid.videoHeight || S.srcH;
    S.cx = S.cxArr ? S.cxArr[0] : S.srcW / 2;
    S.cxTarget = S.cx;
    resizePanel();
    vid.classList.add('ready');
    S.dur = vid.duration || meta.duration;
    tTot.textContent = fmt(S.dur);
    S.ready = true;
    loading.classList.add('hidden');
    requestAnimationFrame(() => { S.lastP = -1; });

    // 定位到时间点（调试/截图用）
    const t = parseFloat(Q.get('t') || '0');
    if (t > 0) { vid.currentTime = Math.min(t, S.dur - 0.2); }
    if (Q.get('play') === '1') {
      if (Q.get('muted') === '1') vid.muted = true;
      play();
    }
  }

  let rzTimer = 0;
  const onResize = () => {
    resizePanel();
    clearTimeout(rzTimer);
    rzTimer = setTimeout(() => { fitLyrics(); resizePanel(); }, 120);
  };
  window.addEventListener('resize', onResize);
  new ResizeObserver(onResize).observe(portrait);

  S.raf = requestAnimationFrame(loop);
  window.__lc = S;                 // 调试/自检入口（只读快照，不参与逻辑）
  boot();
})();
