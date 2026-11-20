#!/usr/bin/env python
"""lrc_offset_fit.py —— 歌词时间轴「整体偏移 + 轻微伸缩」估计。

用途：拿到一版歌词（LRC 或 _media_cache/_lrc_*.json 里的原文）和一条**实际音轨**，
估计 file_t = a * lrc_t + b，让每行歌词起点尽量落在人声起音上。

适用场景（2026-11-20《给我一个理由忘记》确立）：
  - 现场版比原曲快/慢 1% 左右 → 固定偏移补不了，全程漂移 1~2s；
  - CTC 强制对齐匹配率低于阈值（该曲 SenseVoice 漏字多，只得 51%）；
  - 源视频那段是「断歌 + 口播」，DTW 必然跑飞 → 只能拿外部音轨直接拟合。

做法：对数能量一阶正差分（起音通量）→ 对 (a, b) 二维网格搜索，
最大化 28 行歌词映射位置上 ±50ms 窗内的起音通量均值。

用法：
  python tools/lrc_offset_fit.py --audio 音轨.mp4|mp3 --lrc 歌词.lrc
  python tools/lrc_offset_fit.py --audio X.mp3 --cache _media_cache/_lrc_歌名.json
  ... --out segments/ov_歌名.lrc     # 写出映射后的歌词（cut 相对时间）
"""
import argparse
import io
import json
import os
import subprocess
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ffmpeg():
    try:
        from songcut import config as CFG
        return CFG.ffmpeg()
    except Exception:
        return "ffmpeg"


def load_audio(path, wav_out, sr=16000):
    subprocess.run([_ffmpeg(), "-y", "-v", "error", "-i", path, "-vn",
                    "-ac", "1", "-ar", str(sr), wav_out], check=True)
    w = wave.open(wav_out)
    x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.
    return x, w.getframerate()


def onset_flux(x, sr, smooth_ms=110):
    hop = int(0.01 * sr)
    n = len(x) // hop
    e = np.array([np.sqrt((x[i * hop:(i + 1) * hop] ** 2).mean()) for i in range(n)])
    db = 20 * np.log10(e + 1e-9)
    flux = np.maximum(np.diff(db), 0.0)
    k = max(1, int(smooth_ms / 10))
    flux = np.convolve(flux, np.ones(k) / k, "same")
    return (flux - flux.mean()) / (flux.std() + 1e-9)


def read_lrc_times(text):
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("[") and "]" in ln:
            body = ln[ln.index("]") + 1:]
            if not body.strip():
                continue
            head = ln[1:ln.index("]")]
            try:
                m, rest = head.split(":")
                out.append((int(m) * 60 + float(rest), body))
            except ValueError:
                continue
    return out


def fmt(t):
    t = max(0.0, t)
    m = int(t // 60)
    return "[%02d:%05.2f]" % (m, t - m * 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True, help="实际音轨（mp4/mp3/wav）")
    ap.add_argument("--lrc", default="", help="歌词文件")
    ap.add_argument("--cache", default="", help="_media_cache/_lrc_*.json（取其中的 lrc 字段）")
    ap.add_argument("--out", default="", help="写出映射后的歌词（cut 相对时间）")
    ap.add_argument("--a-range", default="0.980,1.020", help="伸缩搜索区间")
    ap.add_argument("--b-range", default="-4,4", help="偏移搜索区间（秒）")
    args = ap.parse_args()

    if args.cache:
        text = json.load(io.open(args.cache, encoding="utf-8"))["lrc"]
    elif args.lrc:
        text = io.open(args.lrc, encoding="utf-8").read()
    else:
        raise SystemExit("需要 --lrc 或 --cache")

    lines = read_lrc_times(text)
    if len(lines) < 8:
        raise SystemExit("歌词行数不足（%d），无法可靠拟合" % len(lines))

    wav = os.path.join(os.path.dirname(os.path.abspath(args.audio)), "_fit_audio.wav")
    x, sr = load_audio(args.audio, wav)
    try:
        os.remove(wav)
    except OSError:
        pass
    flux = onset_flux(x, sr)
    NF = len(flux)
    T = np.array([t for t, _ in lines])
    print("音轨 %.1fs / 歌词 %d 行（%.2f~%.2f）" % (len(x) / sr, len(T), T[0], T[-1]))

    def score(a, b):
        idx = ((a * T + b) / 0.01).astype(int)
        ok = (idx >= 6) & (idx < NF - 6)
        if ok.sum() < len(T) * 0.8:
            return -9.0
        return float(np.mean([flux[i - 5:i + 6].max() for i in idx[ok]]))

    a0, a1 = (float(v) for v in args.a_range.split(","))
    b0, b1 = (float(v) for v in args.b_range.split(","))
    best = (-9.0, 1.0, 0.0)
    for a in np.arange(a0, a1 + 1e-9, 0.002):
        for b in np.arange(b0, b1 + 1e-9, 0.05):
            s = score(a, b)
            if s > best[0]:
                best = (s, a, b)
    s, a, b = best
    for a2 in np.arange(a - 0.002, a + 0.0021, 0.0002):
        for b2 in np.arange(b - 0.05, b + 0.051, 0.005):
            s2 = score(a2, b2)
            if s2 > s:
                s, a, b = s2, a2, b2
    print("拟合：t = %.5f * lrc_t %+.3f（强度 %.3f）" % (a, b, s))
    print("  首行 %.2f → %.2f ；末行 %.2f → %.2f" % (T[0], a * T[0] + b, T[-1], a * T[-1] + b))

    resid = []
    for t in T:
        ft = a * t + b
        i = int(ft / 0.01)
        seg = flux[max(0, i - 60):i + 60]
        if len(seg) and flux[int(np.argmax(seg)) + max(0, i - 60)] > 0.8:
            j = int(np.argmax(seg)) + max(0, i - 60)
            resid.append(j * 0.01 - ft)
    if resid:
        r = np.array(resid)
        print("  %d 行找到明确起音：|残差| 中位 %.2fs / 最大 %.2fs"
              % (len(r), float(np.median(np.abs(r))), float(np.max(np.abs(r)))))

    if args.out:
        out = []
        for ln in text.splitlines():
            s2 = ln.strip()
            if s2.startswith("[") and "]" in s2:
                body = s2[s2.index("]") + 1:]
                head = s2[1:s2.index("]")]
                try:
                    m, rest = head.split(":")
                    t = int(m) * 60 + float(rest)
                except ValueError:
                    out.append(ln)
                    continue
                out.append(fmt(a * t + b) + body)
            else:
                out.append("")
        io.open(args.out, "w", encoding="utf-8", newline="").write("\n".join(out) + "\n")
        print("已写出：%s" % args.out)


if __name__ == "__main__":
    main()
