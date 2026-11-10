# -*- coding: utf-8 -*-
"""prepare.py -- stage one song into the self-contained official-engine project.

Steps (all deterministic, all local):
  1. copy the song's LRC into songs/<id>.lrc (self-contained source of truth)
  2. cut the exact audio slice out of the 11-09 recording  -> audio/song.wav
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
SESSION = json.loads((ROOT / 'songs' / '2026-11-09.json').read_text(encoding='utf-8'))
VIDEO = SESSION['source_video']
LRC_CACHE = {'faruxue': '_lrc_发如雪.json', 'huainvhai': '_lrc_坏女孩.json'}


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
    ap.add_argument('--song', required=True, choices=SESSION['whitelist'])
    ap.add_argument('--skip-live', action='store_true', help='skip frame extraction (audio/analysis only)')
    a = ap.parse_args()
    sid = a.song
    cfg = json.loads((ROOT / 'songs' / f'{sid}.json').read_text(encoding='utf-8'))
    meta = SESSION['songs'][sid]
    print(f'[prepare] {cfg["title"]}  {meta["abs_start"]} -> {meta["abs_end"]}  ({cfg["duration"]} s)')

    # ---- 1. self-contained LRC -------------------------------------------------
    lrc_path = ROOT / 'songs' / f'{sid}.lrc'
    if not lrc_path.exists():
        d = json.loads((CUTS / '_media_cache' / LRC_CACHE[sid]).read_text(encoding='utf-8'))
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
        f'[ti:{cfg["title"]}]\n[ar:{cfg["artist"]}]\n[by:岁己SUI / 2026-11-09 直播]\n' + body + '\n', encoding='utf-8')

    # ---- 2. exact audio slice --------------------------------------------------
    wav = ROOT / 'audio' / 'song.wav'
    run(['ffmpeg', '-hide_banner', '-v', 'error', '-y',
         '-ss', f'{meta["abs_start"]:.3f}', '-t', f'{cfg["duration"]:.3f}', '-i', VIDEO,
         '-vn', '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', str(wav)])
    print(f'[prepare] audio: {wav.stat().st_size/1e6:.1f} MB')

    # ---- 3. analysis/audio.json : beats are REAL lyric onsets + midpoints -----
    beats = []
    for i, (t, _) in enumerate(lines):
        beats.append(round(t, 3))
        if i + 1 < len(lines):
            mid = (t + lines[i + 1][0]) / 2
            if mid - t > 0.45: beats.append(round(mid, 3))
    beats = sorted(set(b for b in beats if 0 <= b <= cfg['duration']))
    sections = [{'index': i, 'start': ac['t0'], 'end': ac['t1'], 'look': ac['look'], 'id': ac['id']}
                for i, ac in enumerate(cfg['acts'])]
    audio = {
        'duration': cfg['duration'], 'bpm': cfg['bpm'], 'beats': beats,
        'bars': [{'index': i // 4, 'start': beats[i], 'beats': [i, i + 1, i + 2, i + 3]}
                 for i in range(0, len(beats) - 3, 4)],
        'sections': sections, 'stops': [], 'impacts': [ac['t0'] for ac in cfg['acts'] if ac['look'] in ('chorus', 'climax')],
        'downbeat_info': {'winning_phase': 0},
        'note': 'beat grid = official-LRC onsets + midpoints; cuts snap to real onsets, never n*60/bpm',
    }
    (ROOT / 'analysis' / 'audio.json').write_text(json.dumps(audio, ensure_ascii=False, indent=1), encoding='utf-8')

    # ---- 3b. 30 fps envelope from the actual PCM (Z.makeEnv) ------------------
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
    rms = np.clip(rms / mx, 0, 1)
    onset = np.clip(np.diff(rms, prepend=rms[0]) * 4, 0, 1)
    # cheap 3-band split: smoothing differs, enough for env.low / mid / high driven fx
    def smooth(v, k):
        ker = np.ones(k, np.float32) / k
        return np.convolve(v, ker, mode='same')
    low = smooth(rms, 21); high = np.clip(rms - smooth(rms, 5) + 0.2, 0, 1); mid = smooth(rms, 7)
    env = {'fps': 30, 'rms': rms.tolist(), 'low': np.clip(low, 0, 1).tolist(),
           'mid': np.clip(mid, 0, 1).tolist(), 'high': np.clip(high, 0, 1).tolist(),
           'onset': onset.tolist(), 'vocal': rms.tolist()}
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
    rng = np.random.default_rng({'faruxue': 164, 'huainvhai': 2011}.get(cfg['id'], 7))

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
    seed0 = {'faruxue': 164, 'huainvhai': 2011}.get(cfg['id'], 7)
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
