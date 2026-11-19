# -*- coding: utf-8 -*-
"""Derive 11-10 cut points from ASR anchors + real PCM, then write a report.

Pure read-only on the recording. Writes only official_pv/songs/_lrc_cache/.
Usage:  python tools/probe_cuts_1110.py
"""
import json, math, pathlib, re, subprocess, sys
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEST = ROOT / 'songs' / '_lrc_cache'
VIDEO = r'D:\录播\25788785-岁己SUI\2026-11-10\录制-25788785-20261110-201630-486-陪你这个猪过周二！.mp4'
SRT = pathlib.Path(r'D:\岁己直播自动总结\SRT\20261110-陪你这个猪过周二！ 2026年11月10日20点场.srt')

# (SRT absolute second, official-LRC second) -- all read off the 11-10 SRT above.
# Every pair is a real lyric onset; the median cancels ASR block-granularity noise.
ANCHORS = {
    'natianxiayule': [
        (5816.370, 28.73),   # 车子缓缓的开 你慢慢走来
        (5823.170, 35.35),   # 你尴尬 say 个 hi 没位坐下来
        (5836.360, 48.27),   # 远远的看著你点点头 车已开
        (5850.610, 64.60),   # 是你变美还是我变帅
        (5857.610, 71.12),   # 这里适合谈个恋爱
        (5882.810, 94.54),   # 黑黑的夜空 繁星变得耀眼
        (5946.590, 158.79),  # 原来多年前在那个书店
    ],
    'yiyangdeyueguang': [
        (6307.510, 15.91),   # 倔强的表情
        (6351.200, 59.43),   # 一样的月光 怎么看得我越来越心慌
        (6367.650, 75.70),   # 怎么照不亮未来的形状
    ],
    'xiageluokoujian': [
        (6726.280, 27.85),   # 你那边飞机碰巧也落地
        (6799.730, 101.15),  # 忙碌会议 你头脑转不停
        (6853.390, 154.74),  # 下个路口再见吧 (3rd chorus)
        (6872.980, 174.26),  # 下个路口再见吧 (last line)
    ],
}
OFFICIAL_DUR = {'natianxiayule': 223.0, 'yiyangdeyueguang': 240.386,
                'xiageluokoujian': 210.373}


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def pcm(ss, dur, rate=8000):
    """Mono float32 PCM via ffmpeg stdout (audio only, fast seek)."""
    cmd = ['ffmpeg', '-hide_banner', '-v', 'error', '-ss', f'{ss:.3f}',
           '-t', f'{dur:.3f}', '-i', VIDEO, '-vn', '-ac', '1', '-ar', str(rate),
           '-f', 'f32le', '-']
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        print(p.stderr.decode('utf-8', 'replace')[-800:], file=sys.stderr)
        raise SystemExit(2)
    return np.frombuffer(p.stdout, dtype='<f4').astype(np.float64)


def db_curve(x, rate=8000, win=0.05):
    n = int(rate * win)
    k = len(x) // n
    v = x[:k * n].reshape(k, n)
    rms = np.sqrt(np.mean(v * v, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-9))


def find_out(start, official, tail=45.0):
    """Scan +/- tail seconds around the predicted end; return the first
    sustained (<0.35 s) digital-silence valley, like the 11-09 method."""
    ss = start + official - tail
    x = pcm(ss, tail * 2 + 20)
    d = db_curve(x)
    # A -70 dB valley lasting >= 0.3 s; prefer the one CLOSEST to official end.
    quiet = d < -68
    runs, i = [], 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            if (j - i) * 0.05 >= 0.30:
                runs.append((i, j))
            i = j
        else:
            i += 1
    target = tail  # absolute second == official end
    cand = [r for r in runs if abs((r[0] * 0.05) - target) < 30]
    chosen = min(cand, key=lambda r: abs((r[0] * 0.05) - target)) if cand else None
    return {
        'scan_from': round(ss, 3),
        'official_end_pred': round(start + official, 3),
        'quiet_runs': [{'t0': round(r[0] * .05, 2), 't1': round(r[1] * .05, 2),
                        'abs': round(ss + r[0] * .05, 2)} for r in runs[:20]],
        'chosen': ({'abs': round(ss + chosen[0] * .05, 2),
                    'local_t': round(chosen[0] * .05, 2),
                    'len_s': round((chosen[1] - chosen[0]) * .05, 2)} if chosen else None),
    }


def main():
    if '--tail' in sys.argv:
        # Diagnostic: 0.5 s dB curve of the 60 s around each predicted end.
        for sid, s in [('natianxiayule', 5960.0),
                       ('yiyangdeyueguang', 6485.0),
                       ('xiageluokoujian', 6865.0)]:
            d = db_curve(pcm(s, 75), win=0.5)
            print(sid, 'base', s, ' '.join(f'{s+i*0.5:.1f}:{v:.0f}'
                                           for i, v in enumerate(d)))
        return
    if '--curve' in sys.argv:
        # Diagnostic: 0.5 s dB curve of the first 45 s of each cut.
        for sid, s in [('natianxiayule', 5787.8),
                       ('yiyangdeyueguang', 6291.77),
                       ('xiageluokoujian', 6698.61)]:
            d = db_curve(pcm(s, 45), win=0.5)
            print(sid, ' '.join(f'{i*0.5:.1f}:{v:.0f}' for i, v in enumerate(d)))
        return
    rep = {}
    for sid, anchors in ANCHORS.items():
        offs = [a - b for a, b in anchors]
        start = round(median(offs), 2)
        # opening guard: 0.5-2.5 s of the cut must contain real music
        head = pcm(start + 0.5, 2.0)
        head_rms = float(np.sqrt(np.mean(head ** 2)))
        out = find_out(start, OFFICIAL_DUR[sid])
        rep[sid] = {
            'abs_start': start,
            'anchor_offsets': [round(o, 2) for o in offs],
            'offset_spread': round(max(offs) - min(offs), 2),
            'official_duration': OFFICIAL_DUR[sid],
            'opening_rms_0_5_to_2_5s': round(head_rms, 5),
            'opening_ok': head_rms >= 0.008,
            'out_probe': out,
        }
        print(f"[{sid}] start={start}  spread={rep[sid]['offset_spread']}s  "
              f"headRMS={head_rms:.4f} {'OK' if rep[sid]['opening_ok'] else 'SILENT!'}")
        print(f"        predicted end={out['official_end_pred']}  "
              f"silence valley={out['chosen']}")
        for r in out['quiet_runs'][:8]:
            print(f"          quiet t={r['t0']}-{r['t1']} abs={r['abs']}")
    (DEST / 'cut_points.json').write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[done] -> {DEST / "cut_points.json"}')
    print('SRT exists:', SRT.exists(), '| video exists:', pathlib.Path(VIDEO).exists())


if __name__ == '__main__':
    main()
