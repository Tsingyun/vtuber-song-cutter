# -*- coding: utf-8 -*-
"""prepare.py -- stage one song into the self-contained official-engine project.

Steps (all deterministic, all local):
  1. copy the song's LRC into songs/<id>.lrc (self-contained source of truth)
  2. cut the exact audio slice out of the session recording -> audio/song.wav
  3. build analysis/audio.json (real lyric onsets as the beat grid) and envelope_30fps.json
  4. write analysis/lyrics_mv.lrc (no BOM, no metadata rows -- 06 §3)
  5. paint procedural background plates (NO AI images, NO external art)  -> assets/bg/*.png
  6. extract ONLY the short live-insert windows as cropped frames -> assets/live/<id>/frames/
     Cropping is allowed by the no-warp rule; geometry is never stretched.

Usage:  python tools/prepare.py --song faruxue
"""
import argparse, json, math, pathlib, re, shutil, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CUTS = pathlib.Path(r'D:\岁己歌切')

# Session is chosen per run (--date, or the song's own `date` field).  Nothing
# here is pinned to 2026-11-09 any more: 11-10 and later sessions use the same
# code path with their own songs/<date>.json and source_video.
SEEDS = {'faruxue': 164, 'huainvhai': 2011,
         'natianxiayule': 20261110, 'yiyangdeyueguang': 20090529,
         'xiageluokoujian': 20091221}


def run(args, quiet=True):
    p = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if p.returncode != 0:
        print('FFMPEG FAILED:', ' '.join(map(str, args))[:300], file=sys.stderr)
        print(p.stderr[-1200:], file=sys.stderr)
        raise SystemExit(2)
    if not quiet:
        print(p.stdout[-500:])
    return p


def parse_lrc(text):
    out = []
    for line in text.replace('\ufeff', '').splitlines():
        m = re.match(r'^\[(\d+):(\d+(?:\.\d+)?)\](.*)$', line.strip())
        if m:
            out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default='', help='session date; defaults to the song config date')
    ap.add_argument('--song', required=True)
    ap.add_argument('--skip-live', action='store_true', help='skip frame extraction (audio/analysis only)')
    a = ap.parse_args()
    sid = a.song
    cfg = json.loads((ROOT / 'songs' / f'{sid}.json').read_text(encoding='utf-8'))
    date = a.date or cfg.get('date') or '2026-11-09'
    SESSION = json.loads((ROOT / 'songs' / f'{date}.json').read_text(encoding='utf-8'))
    if sid not in SESSION['whitelist']:
        raise SystemExit(f'song {sid} is not in the {date} whitelist')
    VIDEO = SESSION['source_video']
    meta = SESSION['songs'][sid]
    print(f'[prepare] {date} | {cfg["title"]}  {meta["abs_start"]} -> {meta["abs_end"]}  ({cfg["duration"]} s)')

    # ---- 1. self-contained LRC -------------------------------------------------
    lrc_path = ROOT / 'songs' / f'{sid}.lrc'
    if not lrc_path.exists():
        staged = ROOT / 'songs' / '_lrc_cache' / f'_lrc_{cfg["title"]}.json'
        if not staged.exists():
            raise SystemExit(f'no staged LRC for {sid}; run tools/fetch_lrc_1110.py first')
        d = json.loads(staged.read_text(encoding='utf-8'))
        lrc_path.write_text(d['lrc'], encoding='utf-8')
    lines = parse_lrc(lrc_path.read_text(encoding='utf-8'))
    assert lines and all(lines[i][0] <= lines[i + 1][0] for i in range(len(lines) - 1)), 'LRC not monotonic'
    # QA round 2: some live recordings carry a dead-air gap before the accompaniment starts
    # (11-09 《发如雪》: 1.5-14.25 s of digital silence, RMS ~0.001). We drop that head by
    # moving abs_start forward by the SAME amount and shifting the whole timeline -offset.
    # Audio / video / LRC / acts / live windows all shift together, so every lyric stays
    # locked to the exact same absolute instant of the original recording.
    offset = float(cfg.get('lrcOffset', 0) or 0)
    if offset:
        lines = [(t - offset, txt) for t, txt in lines if t - offset >= 0]
        assert lines and lines[0][0] >= 0, 'LRC offset removed every line'
        print(f'[prepare] LRC offset: -{offset:.2f}s -> {lines[0][0]:.2f} .. {lines[-1][0]:.2f}')
    print(f'[prepare] LRC: {len(lines)} lines, {lines[0][0]:.2f} .. {lines[-1][0]:.2f}')

    # JIZURA wants no metadata rows and no BOM (references/06 §3)
    body = '\n'.join(f'[{int(t)//60:02d}:{t%60:05.2f}]{txt}' for t, txt in lines if txt)
    (ROOT / 'analysis' / 'lyrics_mv.lrc').write_text(body + '\n', encoding='utf-8')
    # deliverable LRC with the ORIGINAL artist in the header (song-cut house rule)
    (ROOT / 'songs' / f'{sid}.deliver.lrc').write_text(
        f'[ti:{cfg["title"]}]\n[ar:{cfg["artist"]}]\n[by:岁己SUI / {date} 直播]\n' + body + '\n', encoding='utf-8')

    # ---- 2. exact audio slice --------------------------------------------------
    wav = ROOT / 'audio' / 'song.wav'
    run(['ffmpeg', '-hide_banner', '-v', 'error', '-y',
         '-ss', f'{meta["abs_start"]:.3f}', '-t', f'{cfg["duration"]:.3f}', '-i', VIDEO,
         '-vn', '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', str(wav)])
    print(f'[prepare] audio: {wav.stat().st_size/1e6:.1f} MB')

    # ---- 2b. bespoke palette for THIS film (02 §"build new palettes for every film") ----
    write_song_palette(ROOT, cfg)

    # ---- 3. beats = REAL lyric onsets + midpoints -------------------------------
    beats = []
    for i, (t, _) in enumerate(lines):
        beats.append(round(t, 3))
        if i + 1 < len(lines):
            mid = (t + lines[i + 1][0]) / 2
            if mid - t > 0.45: beats.append(round(mid, 3))
    beats = sorted(set(b for b in beats if 0 <= b <= cfg['duration']))
    sections = [{'index': i, 'start': ac['t0'], 'end': ac['t1'], 'look': ac['look'], 'id': ac['id']}
                for i, ac in enumerate(cfg.get('acts') or [])]

    # ---- 3b. PCM analysis: envelope + REAL downbeats + strong onsets -----------
    # `direction.structure.mode == 'auto'` derives the acts in the browser, so the sections
    # written here are advisory only. What the engine actually needs from the audio is the
    # beat grid and the DOWNBEATS: upstream `beats_in(T0,T1,"downbeats")` (and any Blender
    # shot later) hangs off them. The old build wrote `downbeat_info.winning_phase = 0`
    # unconditionally, i.e. an invented phase. Now the phase is measured from the PCM.
    import numpy as np, soundfile as sf
    x, sr = sf.read(str(wav), dtype='float32', always_2d=True)
    mono = x.mean(axis=1)
    n = int(math.ceil(cfg['duration'] * 30))
    hop = sr / 30

    def frame_rms(i):
        s = int(i * hop); e = min(len(mono), int((i + 1) * hop))
        return float(np.sqrt(np.mean(mono[s:e] ** 2))) if e > s else 0.0
    rms = np.array([frame_rms(i) for i in range(n)], dtype=np.float32)
    mx = float(rms.max()) or 1.0
    rmsn = np.clip(rms / mx, 0, 1)
    onset_raw = np.clip(np.diff(rmsn, prepend=rmsn[0]) * 4, 0, 1)

    bpm = float(cfg.get('bpm') or 0) or 120.0
    spb = 60.0 / bpm
    bar = 4.0 * spb
    nbars = max(1, int(cfg['duration'] / bar))
    best_ph, best_sc = 0.0, -1.0
    for ph in np.linspace(0, bar, 192, endpoint=False):
        idx = ((np.arange(nbars) * bar + ph) * 30).astype(int)
        idx = idx[(idx >= 0) & (idx < n)]
        sc = float(onset_raw[idx].sum()) if idx.size else 0.0
        if sc > best_sc:
            best_sc, best_ph = sc, float(ph)
    downbeats = [round(best_ph + k * bar, 3) for k in range(nbars) if best_ph + k * bar < cfg['duration'] - 0.3]
    # strong onsets: local maxima, 0.22 s apart
    peaks = []
    for i in range(1, n - 1):
        if onset_raw[i] >= onset_raw[i - 1] and onset_raw[i] >= onset_raw[i + 1] and onset_raw[i] > 0.16:
            t = i / 30.0
            if not peaks or t - peaks[-1][0] > 0.22:
                peaks.append((t, float(onset_raw[i])))
    peaks.sort(key=lambda p: -p[1])
    impacts = sorted(round(t, 3) for t, _ in peaks[:8])
    onsets_strong = sorted(round(t, 3) for t, v in peaks[:220])

    audio = {
        'duration': cfg['duration'], 'bpm': bpm, 'beats': beats, 'downbeats': downbeats,
        'bars': [{'index': k, 'start': t, 'beats': [k * 4 + j for j in range(4)]} for k, t in enumerate(downbeats)],
        'sections': sections, 'stops': [], 'impacts': impacts,
        'onsets_strong': onsets_strong,
        'downbeat_info': {'winning_phase': round(best_ph, 4), 'bpm': bpm,
                          'note': 'phase measured from the PCM onset envelope, not assumed'},
        'note': 'beat grid = official-LRC onsets + midpoints; downbeats from real onset energy',
    }
    (ROOT / 'analysis' / 'audio.json').write_text(json.dumps(audio, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'[prepare] downbeats: {len(downbeats)} (phase {best_ph:.3f}s @ {bpm:g} bpm, {bar:.2f}s/bar), strong onsets: {len(onsets_strong)}')

    # cheap 3-band split: smoothing differs, enough for env.low / mid / high driven fx
    def smooth(v, k):
        ker = np.ones(k, np.float32) / k
        return np.convolve(v, ker, mode='same')
    low = smooth(rmsn, 21); high = np.clip(rmsn - smooth(rmsn, 5) + 0.2, 0, 1); mid = smooth(rmsn, 7)
    env = {'fps': 30, 'rms': rmsn.tolist(), 'low': np.clip(low, 0, 1).tolist(),
           'mid': np.clip(mid, 0, 1).tolist(), 'high': np.clip(high, 0, 1).tolist(),
           'onset': onset_raw.tolist(), 'vocal': rmsn.tolist()}
    (ROOT / 'analysis' / 'envelope_30fps.json').write_text(json.dumps(env), encoding='utf-8')
    # ---- 3c. opening-audio guard (QA round 2) -----------------------------------------
    # The delivered cut must START with music, not 12 s of digital silence. Check the real
    # PCM we just cut, skipping the first 0.5 s so a transient click cannot mask dead air.
    head = mono[int(sr * 0.5):int(sr * 2.5)]
    head_rms = float(np.sqrt(np.mean(head ** 2))) if head.size else 0.0
    print(f'[prepare] opening RMS (0.5-2.5 s): {head_rms:.4f}')
    if head_rms < 0.008:
        raise SystemExit(f'OPENING AUDIO IS SILENT (RMS={head_rms:.5f}); check abs_start / lrcOffset')
    print(f'[prepare] analysis: {len(beats)} beats, {len(sections)} sections, {n} envelope frames')

    # ---- 4. procedural background plates (no AI art, no external images) ------
    try:
        from PIL import Image, ImageDraw, ImageFilter
        paint_plates(ROOT, cfg, Image, ImageDraw, ImageFilter)
        print('[prepare] procedural plates: 3')
    except ImportError:
        print('[prepare] Pillow missing -- illust falls back to its gradient branch (still official)')

    # ---- 4b. the (user-supplied) single illustration ---------------------------
    check_char_asset(ROOT, cfg)

    # ---- 5. live insert frames (short, cropped, 30 fps) -----------------------
    # Idempotent by construction: ffmpeg writes the FINAL frame numbers in place with
    # -start_number, so re-running never needs a bulk delete (the sandbox blocks it).
    if not a.skip_live:
        fdir = ROOT / 'assets' / 'live' / sid / 'frames'
        fdir.mkdir(parents=True, exist_ok=True)
        total = 0
        for wi, (w0, w1) in enumerate(cfg['live']):
            base = round(w0 * 30) + 1
            n = round((w1 - w0) * 30)
            # crop=640:1080:640:0 keeps the singer centred; this is a CROP, never a stretch
            run(['ffmpeg', '-hide_banner', '-v', 'error', '-y',
                 '-ss', f'{meta["abs_start"] + w0:.3f}', '-t', f'{w1-w0:.3f}', '-i', VIDEO,
                 '-an', '-vf', 'crop=640:1080:640:0,fps=30', '-frames:v', str(n),
                 '-start_number', str(base), '-q:v', '4',
                 str(fdir / 'f_%05d.jpg')])
            total += n
            print(f'[prepare] live window {wi+1}/{len(cfg["live"])}: {w0:.1f}-{w1:.1f} s -> {n} frames')
        print(f'[prepare] live frames: {total}  ({100*total/30/cfg["duration"]:.1f}% of runtime)')

    print('[prepare] DONE')


def _rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, k):
    A, B = _rgb(a), _rgb(b)
    return '#%02X%02X%02X' % tuple(round(A[i] + (B[i] - A[i]) * k) for i in range(3))


def write_song_palette(ROOT, cfg):
    """Write songs/<id>.palette.json -- the film's OWN gradient map.

    Upstream STYLE_RESEARCH / 02 §13: "build new palettes for every film, the shipped five are
    the zanko example". The engine (core/main.js) appends these rows to the LUT atlas as
    `S_<id>` / `S_<id>_duo`, so a song can be graded with its own ramp instead of borrowing
    P1..P5. Stops are declared as colours, no PNG asset needed.
    """
    P = cfg['palette']
    d = cfg.get('direction') or {}
    high = float((d.get('grade') or {}).get('exposure') or 0) > 0.15
    if high:
        # high-key: never crush the shadows, lift them toward the paper colour
        ramp = [(0.00, _mix(P['ink'], P['glow'], 0.28)),
                (0.30, _mix(P['mid'], P['accent'], 0.45)),
                (0.60, P['accent']),
                (0.85, _mix(P['accent'], P['glow'], 0.65)),
                (1.00, '#FFFFFF')]
    else:
        ramp = [(0.00, P['void']), (0.28, P['ink']), (0.54, P['mid']),
                (0.76, P['accent']), (0.92, P['glow']), (1.00, '#FFFFFF')]
    duo = [(0.0, P.get('cold') or P['mid']), (0.5, _mix(P['mid'], P['accent'], 0.5)), (1.0, P['glow'])]
    out = {'id': f"S_{cfg['id']}", 'key': 'high' if high else 'low',
           'ramp': [[k, c] for k, c in ramp], 'duo': [[k, c] for k, c in duo],
           'note': 'per-film gradient map; appended to the LUT atlas by core/main.js'}
    (ROOT / 'songs' / f"{cfg['id']}.palette.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f"[prepare] palette: {out['id']} ({out['key']}-key, {len(ramp)} stops)")


def check_char_asset(ROOT, cfg):
    """Validate the (user-supplied) single illustration before it is wired in.

    Upstream 09 lists "sprite straight edge" and "alpha 252-254" as recurring failure modes,
    and both are invisible until you watch the render. Catch them here instead.
    """
    p = ROOT / 'assets' / 'char' / f"{cfg['id']}.png"
    if not p.exists():
        print(f"[prepare] char: none at assets/char/{cfg['id']}.png -> character windows disabled for now")
        return
    try:
        from PIL import Image
    except ImportError:
        print('[prepare] char: found but Pillow missing, cannot validate')
        return
    im = Image.open(p)
    print(f"[prepare] char: {p.name}  {im.size[0]}x{im.size[1]}  {im.mode}")
    if im.mode not in ('RGBA', 'LA'):
        print('[prepare] char: WARNING no alpha channel -- export RGBA or run a matte step, '
              'otherwise the `sil` / `rim` forms will draw a rectangle')
    else:
        bb = im.getchannel('A').getbbox()
        w, h = im.size
        if bb and (bb[0] <= 1 or bb[1] <= 1 or bb[2] >= w - 1 or bb[3] >= h - 1):
            print(f'[prepare] char: WARNING subject touches the canvas edge {bb} -- official 09 '
                  f'"sprite straight edge"; the `close` form will reveal a cut line')
    if im.size[1] < 1600:
        print(f'[prepare] char: WARNING height {im.size[1]} < 1600 -- the close-up form draws at '
              f'h=2380 px, expect softness')


def paint_plates(ROOT, cfg, Image, ImageDraw, ImageFilter):
    """Three deterministic 1920x1080 plates so `illust` can use its official
    bg / bgFrom / bgTo / beatZoom camera branch (scenes.js:36-46) as well as the
    pure-gradient branch. Nothing here is AI-generated or downloaded."""
    import numpy as np
    W, H = 1920, 1080
    P = cfg['palette']
    out = ROOT / 'assets' / 'bg'
    out.mkdir(parents=True, exist_ok=True)
    yy = np.linspace(0, 1, H, dtype=np.float32)[:, None]
    xx = np.linspace(0, 1, W, dtype=np.float32)[None, :]
    rng = np.random.default_rng(SEEDS.get(cfg['id'], 7))

    def hx(c):
        c = c.lstrip('#'); return np.array([int(c[i:i+2], 16) for i in (0, 2, 4)], np.float32)

    def base(top, bot, seed):
        a, b = hx(top), hx(bot)
        g = a[None, None, :] * (1 - yy[:, :, None]) + b[None, None, :] * yy[:, :, None]
        noise = rng.normal(0, 5, (H, W, 1)).astype(np.float32)
        return np.clip(g + noise, 0, 255).astype(np.uint8)

    # Three DISTINCT motifs per song (QA round 1: the old three differed only in
    # gradient brightness, which read as one template repeated). Namespaced by song
    # id so preparing the second song cannot overwrite the first song's plates.
    grads = [(P['void'], P['mid']), (P['void'], P['ink']), (P['ink'], P['mid'])]
    seed0 = SEEDS.get(cfg['id'], 7)
    for idx, (suffix, (top, bot)) in enumerate(zip('abc', grads)):
        r2 = np.random.default_rng(seed0 * 31 + idx)
        img = Image.fromarray(base(top, bot, suffix), 'RGB')
        d = ImageDraw.Draw(img, 'RGBA')
        if cfg['id'] == 'faruxue':
            if idx == 0:
                # A - cold moon above a snowfield (the opening image)
                cx, cy, r = int(W*0.72), int(H*0.26), 168
                for k in range(9, 0, -1):
                    d.ellipse([cx-r*k, cy-r*k, cx+r*k, cy+r*k], fill=(232, 242, 248, 3))
                d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(232, 242, 248, 34))
                for _ in range(460):
                    x, y = int(r2.random()*W), int(r2.random()*H)
                    s = int(1+3*r2.random())
                    d.ellipse([x, y, x+s, y+s], fill=(232, 242, 248, int(25+80*r2.random())))
                hy = int(H*0.79)
                d.polygon([(0,hy+30),(int(W*0.2),hy-18),(int(W*0.45),hy+16),
                           (int(W*0.72),hy-26),(W,hy+8),(W,H),(0,H)], fill=(150,178,194,20))
                d.line([(0,hy),(W,hy)], fill=(210,230,240,26), width=2)
            elif idx == 1:
                # B - ink-wash mountains, three receding ridges, NO moon
                for layer, (yb, amp, col) in enumerate([(H*0.62,150,(12,26,36,120)),
                                                        (H*0.74,110,(20,40,52,120)),
                                                        (H*0.86,70,(30,54,68,120))]):
                    pts = [(0,H)]
                    for x in range(0, W+1, 60):
                        pts.append((x, int(yb - amp*(0.35+0.65*abs(np.sin(x/340.0+layer*1.7))))))
                    pts.append((W,H)); d.polygon(pts, fill=col)
                for _ in range(9):
                    x, y = int(r2.random()*W), int(r2.random()*H*0.6); rr = int(60+220*r2.random())
                    d.ellipse([x-rr,y-rr,x+rr,y+rr], fill=(8,18,26,10))
            else:
                # C - cinnabar lamplight, warm bloom + vertical brush strokes
                gx, gy = int(W*0.78), int(H*0.68)
                for k in range(10, 0, -1):
                    rr = 70*k
                    d.ellipse([gx-rr,gy-rr,gx+rr,gy+rr], fill=(200,86,60,4))
                for _ in range(22):
                    x = int(W*0.12+r2.random()*W*0.76); y0 = int(H*0.12+r2.random()*H*0.4)
                    ln = int(60+220*r2.random())
                    d.line([(x,y0),(x+int(8*r2.random()-4),y0+ln)],
                           fill=(200,86,60,int(18+34*r2.random())), width=int(2+6*r2.random()))
                for _ in range(90):
                    x, y = int(r2.random()*W), int(r2.random()*H); s = int(1+2*r2.random())
                    d.ellipse([x,y,x+s,y+s], fill=(217,178,106,int(20+55*r2.random())))
        elif cfg.get('theme') == 'rain':
            # 《那天下雨了》: windshield rain / rainbow / umbrella under a street lamp
            if idx == 0:
                for _ in range(520):
                    x, y = int(r2.random()*W), int(r2.random()*H)
                    ln = int(14+70*r2.random())
                    d.line([(x,y),(x-int(ln*0.28),y+ln)], fill=(143,199,216,int(12+42*r2.random())), width=1)
                for _ in range(130):
                    x, y = int(r2.random()*W), int(r2.random()*H); rr = int(2+7*r2.random())
                    d.ellipse([x-rr,y-rr,x+rr,y+rr], fill=(234,244,246,int(8+26*r2.random())))
                for k in range(6, 0, -1):
                    rr = 60*k
                    d.ellipse([W*0.74-rr,H*0.68-rr,W*0.74+rr,H*0.68+rr], fill=(232,163,76,3))
            elif idx == 1:
                cx, cy = int(W*0.5), int(H*0.95)
                cols = [(232,163,76,20),(143,199,216,20),(234,244,246,16),(159,110,196,18)]
                for k, col in enumerate(cols):
                    r = 250 + 62*k
                    d.arc([cx-r,cy-r,cx+r,cy+r], 180, 360, fill=col, width=16)
                for _ in range(220):
                    x, y = int(r2.random()*W), int(r2.random()*H*0.72)
                    d.ellipse([x,y,x+int(2+5*r2.random()),y+2], fill=(234,244,246,int(8+28*r2.random())))
                for _ in range(7):
                    y = int(H*(0.12+0.09*r2.random()))
                    x = int(r2.random()*W)
                    d.ellipse([x,y,x+420,y+90], fill=(30,58,72,12))
            else:
                ux, uy = int(W*0.62), int(H*0.46)
                d.arc([ux-330,uy-250,ux+330,uy+250], 180, 360, fill=(232,163,76,52), width=12)
                for k in range(-3, 4):
                    d.line([(ux,uy),(ux+int(k*115),uy+int(230-28*abs(k)))], fill=(232,163,76,26), width=3)
                d.line([(ux,uy),(ux,uy+430)], fill=(232,163,76,40), width=5)
                for k in range(11, 0, -1):
                    rr = 62*k
                    d.ellipse([W*0.18-rr,H*0.24-rr,W*0.18+rr,H*0.24+rr], fill=(255,205,130,4))
                for _ in range(180):
                    x, y = int(r2.random()*W), int(r2.random()*H)
                    d.line([(x,y),(x-4,y+int(12+40*r2.random()))], fill=(111,220,232,22), width=1)
        elif cfg.get('theme') == 'moon':
            # 《一样的月光》: moon through a window / long shadow / silver ripples
            if idx == 0:
                mx, my, mr = int(W*0.68), int(H*0.3), 210
                for k in range(12, 0, -1):
                    d.ellipse([mx-mr*k,my-mr*k,mx+mr*k,my+mr*k], fill=(238,242,255,3))
                d.ellipse([mx-mr,my-mr,mx+mr,my+mr], fill=(238,242,255,40))
                for gx in range(0, W, 320):
                    d.rectangle([gx,0,gx+12,H], fill=(8,14,30,90))
                d.rectangle([0,int(H*0.56),W,int(H*0.56)+12], fill=(8,14,30,90))
                for _ in range(140):
                    x, y = int(r2.random()*W), int(r2.random()*H)
                    d.ellipse([x,y,x+2,y+2], fill=(238,242,255,int(10+40*r2.random())))
            elif idx == 1:
                for k in range(9):
                    y = int(H*(0.62+0.045*k))
                    d.line([(0,y),(W,y)], fill=(157,180,232,int(8+3*k)), width=2)
                px, py = int(W*0.36), int(H*0.62)
                d.polygon([(px,py),(px+70,py),(px+300,H),(px-190,H)], fill=(5,10,22,90))
                d.ellipse([px-16,py-74,px+16,py-42], fill=(5,10,22,90))
                for _ in range(9):
                    x = int(r2.random()*W); y = int(H*(0.62+0.36*r2.random()))
                    d.ellipse([x-40,y-6,x+40,y+6], fill=(157,180,232,7))
            else:
                cx, cy = int(W*0.5), int(H*0.55)
                for k in range(1, 15):
                    r = 62*k
                    d.ellipse([cx-r,cy-int(r*0.62),cx+r,cy+int(r*0.62)], outline=(238,242,255,int(26-1*k)), width=2)
                for _ in range(260):
                    x, y = int(r2.random()*W), int(r2.random()*H)
                    s = int(1+3*r2.random())
                    d.ellipse([x,y,x+s,y+s], fill=(238,242,255,int(12+50*r2.random())))
        elif cfg.get('theme') == 'city':
            # 《下个，路口，见》: metro map / neon crossing / clock face
            if idx == 0:
                for k in range(7):
                    y = int(H*(0.12+0.13*k))
                    col = (240,111,168,70) if k % 2 == 0 else (99,201,222,70)
                    d.line([(0,y),(W,y+int(60*r2.random()-30))], fill=col, width=8)
                for k in range(6):
                    x = int(W*(0.08+0.17*k))
                    d.line([(x,0),(x+int(80*r2.random()-40),H)], fill=(232,196,138,44), width=6)
                for _ in range(34):
                    x, y = int(r2.random()*W), int(r2.random()*H); rr = 9
                    d.ellipse([x-rr,y-rr,x+rr,y+rr], fill=(255,240,246,110))
            elif idx == 1:
                for _ in range(20):
                    x, y = int(r2.random()*W), int(r2.random()*H*0.8)
                    w, h = int(60+300*r2.random()), int(14+70*r2.random())
                    col = (240,111,168,int(20+50*r2.random())) if r2.random() < .5 else (99,201,222,int(20+50*r2.random()))
                    d.rectangle([x,y,x+w,y+h], fill=col)
                for k in range(9):
                    x = int(W*0.08+k*W*0.105)
                    d.rectangle([x,int(H*0.82),x+int(W*0.048),H], fill=(255,240,246,16))
                for _ in range(180):
                    x, y = int(r2.random()*W), int(r2.random()*H)
                    d.line([(x,y),(x+int(30*r2.random()),y)], fill=(255,240,246,18), width=1)
            else:
                cx, cy, r = int(W*0.5), int(H*0.5), 330
                for k in range(3):
                    d.ellipse([cx-r+18*k,cy-r+18*k,cx+r-18*k,cy+r-18*k], outline=(232,196,138,34), width=3)
                for k in range(60):
                    ang = 2*np.pi*k/60
                    x0, y0 = cx+int((r-18)*np.cos(ang)), cy+int((r-18)*np.sin(ang))
                    x1, y1 = cx+int((r-38)*np.cos(ang)), cy+int((r-38)*np.sin(ang))
                    d.line([(x0,y0),(x1,y1)], fill=(255,240,246,70), width=3 if k % 5 == 0 else 1)
                d.line([(cx,cy),(cx+int(r*0.5),cy-int(r*0.35))], fill=(240,111,168,150), width=8)
                d.line([(cx,cy),(cx-int(r*0.2),cy-int(r*0.72))], fill=(99,201,222,150), width=6)
                d.ellipse([cx-12,cy-12,cx+12,cy+12], fill=(255,240,246,160))
        else:
            if idx == 0:
                # A - neon rain: magenta/cyan bars + wet diagonal streaks
                for _ in range(18):
                    x = int(r2.random()*W); y = int(r2.random()*H*0.78)
                    w = int(50+240*r2.random()); h = int(3+9*r2.random())
                    col = (232,85,127,95) if r2.random() < .5 else (111,220,232,95)
                    d.rectangle([x,y,x+w,y+h], fill=col)
                for _ in range(300):
                    x, y = int(r2.random()*W), int(r2.random()*H)
                    d.line([(x,y),(x-3,y+int(10+45*r2.random()))], fill=(111,220,232,36), width=1)
            elif idx == 1:
                # B - back-alley colour blocks: hard geometric slabs
                for _ in range(14):
                    x, y = int(r2.random()*W), int(r2.random()*H)
                    w, h = int(120+520*r2.random()), int(40+300*r2.random())
                    col = (232,85,127,26) if r2.random() < .5 else (111,220,232,24)
                    d.rectangle([x,y,x+w,y+h], fill=col)
                for _ in range(70):
                    x, y = int(r2.random()*W), int(r2.random()*H); rr = int(8+60*r2.random())
                    d.ellipse([x-rr,y-rr,x+rr,y+rr], fill=(240,240,255,7))
            else:
                # C - stage spotlights: warm cones + dust in the beams
                for k in range(4):
                    x0 = int(W*(0.18+0.21*k)); spread = int(180+120*r2.random())
                    d.polygon([(x0,-20),(x0+40,-20),(x0+spread,H),(x0-spread,H)], fill=(255,190,120,12))
                for _ in range(140):
                    x, y = int(r2.random()*W), int(r2.random()*H); s = int(1+3*r2.random())
                    d.ellipse([x,y,x+s,y+s], fill=(255,220,170,int(20+60*r2.random())))
        img = img.filter(ImageFilter.GaussianBlur(0.4))
        img.save(out / f'plate_{cfg["id"]}_{suffix}.png', optimize=True)


if __name__ == '__main__':
    main()
