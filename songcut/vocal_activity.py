# -*- coding: utf-8 -*-
"""人声显著度 + 乐句起点检测（歌切歌词同步的「独立声学裁判」）。

前提（本项目实测成立）：主播清唱时**人声显著大于伴奏**，
因此「带通谐波能量相对局部基线抬升」可以作为可靠的人声存在证据，
从而把纯伴奏的前奏/间奏与人声段分开——这是 pyin 单独做不到的事
（乐器也有基频，前奏会被误判成人声）。

流程：
  带通谐波能量(dB)  →  减去局部滚动百分位基线(纯伴奏底噪)
                    →  抬升阈值分段  →  形态学合并句内换气  →  乐句起点

输出 [(起, 止), ...]（秒，相对于给定音频文件的起点）。

用法：
    python vocal_activity.py <audio> [--out json] [--th 6.0] [--show]
"""
import argparse
import io
import json
import os
import subprocess
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from songcut import config as CFG      # noqa: E402

FF = CFG.ffmpeg()
SR = 22050
HOP = 256                      # ≈11.6ms

DEFAULT = dict(
    fmin=130.0,       # 人声基频下限（Hz）：滤掉鼓/贝斯
    fmax=4200.0,      # 人声上限（Hz）：滤掉镲片
    base_sec=18.0,    # 滚动基线窗口长度（秒）：须大于最长纯伴奏段
    base_pct=20.0,    # 基线取该窗口的第 N 百分位（≈纯伴奏底噪）
    th_db=6.0,        # 抬升阈值（dB）：本项目人声比伴奏响 ⇒ 6dB 稳定可分
    merge_gap=0.30,   # 句内换气合并上限（秒）
    min_run=0.45,     # 最短乐句（秒）：滤掉爆音/辅音碎片
    harm_margin=2.0,  # HPSS 边距
)


def load_audio(path, sr=SR, ss=0.0, t=None):
    cmd = [FF, "-v", "error"]
    if ss:
        cmd += ["-ss", "%.3f" % ss]
    cmd += ["-i", path]
    if t:
        cmd += ["-t", "%.3f" % t]
    cmd += ["-vn", "-ar", str(sr), "-ac", "1", "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def salience_curve(x, sr=SR, **kw):
    """返回 (sal_db, base_db, times)；sal 为相对于局部基线的抬升量（dB）。"""
    import librosa
    from scipy.ndimage import percentile_filter
    p = dict(DEFAULT)
    p.update(kw)

    S = np.abs(librosa.stft(x, n_fft=2048, hop_length=HOP)) + 1e-9
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    band = (freqs >= p["fmin"]) & (freqs <= p["fmax"])

    # 只保留谐波成分（去掉鼓/打击乐 ⇒ 更接近人声 + 旋律乐器）
    H, _P = librosa.decompose.hpss(S, margin=p["harm_margin"])
    band = band[: H.shape[0]]

    e = np.sqrt((H[band] ** 2).sum(axis=0))          # 每帧带通谐波能量
    edb = 20 * np.log10(e + 1e-12)

    w = max(3, int(round(p["base_sec"] * sr / HOP)))
    if w % 2 == 0:
        w += 1
    base = percentile_filter(edb, percentile=p["base_pct"], size=w, mode="nearest")
    sal = edb - base
    times = np.arange(len(sal)) * HOP / sr
    return sal, base, times


def detect_phrases(x, sr=SR, **kw):
    """→ (runs, sal, times)；runs = [(start, end), ...] 人声乐句。"""
    from scipy.ndimage import binary_closing, binary_opening, label
    sal, base, times = salience_curve(x, sr, **kw)
    p = dict(DEFAULT)
    p.update(kw)

    m = sal > p["th_db"]
    hop_t = HOP / sr
    # 闭：合并句内换气；开：去掉碎片
    k = max(1, int(round(p["merge_gap"] / hop_t)))
    m = binary_closing(m, np.ones(1 + 2 * k))
    m = binary_opening(m, np.ones(1 + 2 * max(1, k // 2)))

    lbl, n = label(m)
    runs = []
    for i in range(1, n + 1):
        idx = np.flatnonzero(lbl == i)
        a, b = idx[0] * hop_t, (idx[-1] + 1) * hop_t
        if b - a >= p["min_run"]:
            runs.append((round(a, 3), round(b, 3)))
    return runs, sal, times


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--ss", type=float, default=0.0)
    ap.add_argument("--t", type=float, default=None)
    ap.add_argument("--th", type=float, default=DEFAULT["th_db"])
    ap.add_argument("--base-sec", type=float, default=DEFAULT["base_sec"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    x = load_audio(args.audio, ss=args.ss, t=args.t)
    print("音频 %.1fs" % (len(x) / SR))
    runs, sal, times = detect_phrases(x, th_db=args.th, base_sec=args.base_sec)
    print("检出人声乐句 %d 个（阈值 %.1f dB）" % (len(runs), args.th))
    for i, (a, b) in enumerate(runs):
        print("  %3d %8.2f - %8.2f  (%.2fs)" % (i, a, b, b - a))
    if args.out:
        io.open(args.out, "w", encoding="utf-8", newline="").write(
            json.dumps({"runs": [list(r) for r in runs],
                        "th_db": args.th, "audio": args.audio},
                       ensure_ascii=False, indent=1))
        print("→", args.out)


if __name__ == "__main__":
    main()
