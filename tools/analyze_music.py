# -*- coding: utf-8 -*-
"""analyze_music.py —— 歌切背景可视化用的「真数据」分析器

产出（逐视频帧 60fps 对齐）：
  bands  48 段对数频段能量（40Hz~11kHz），带 attack/release 弹道，等价于专业分析器的柱状表
  rms    逐帧响度包络（DYN 参数轨）
  midi10 人声基频（librosa pyin，MIDI×10，未发声 = -1000）→ 旋律走向 / 钢琴卷帘音符块
  voiced 发声概率（VOX 轨道活动度）
  flux   谱通量（鼓组瞬态强度）
  bpm / bars / sections  小节网格与段落块（DAW 编排视图用）

用法:
  python tools/analyze_music.py --audio in.mp3 --out _tmp/pro_analysis.json [--fps 60] [--nb 48]

为什么在 Python 侧算：页面的 computeHaloTable 只有 127 个**线性** bin（间隔 sr/256≈187Hz），
低频段几乎无法分辨，且 JS 里跑 pyin 级别的基频提取要几十分钟。numpy+librosa 一次算完落盘，
播放器只做查表绘制 —— 与现有 HL.haloTable 的「离线确定性」思路一致。
"""
import argparse
import base64
import json
import os
import subprocess
import sys

import numpy as np


def decode(ffmpeg, path, sr):
    cmd = [ffmpeg, "-v", "error", "-i", path, "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise SystemExit("ffmpeg 解码失败：%s" % p.stderr.decode("utf-8", "ignore")[-400:])
    return np.frombuffer(p.stdout, dtype=np.float32).astype(np.float32)


def log_bands(nb, f_lo, f_hi):
    edges = np.geomspace(f_lo, f_hi, nb + 1)
    return edges


def ballistics(x, atk, rel):
    """一阶弹道平滑：上升用 atk，下降用 rel（专业分析器的「柱子上冲快、回落慢」）。"""
    out = np.empty_like(x)
    prev = 0.0
    for i in range(len(x)):
        v = x[i]
        a = atk if v > prev else rel
        prev = prev + (v - prev) * a
        out[i] = prev
    return out


def b64_u8(a):
    return base64.b64encode(np.ascontiguousarray(a.astype(np.uint8)).tobytes()).decode("ascii")


def b64_i16(a):
    return base64.b64encode(np.ascontiguousarray(a.astype(np.int16)).tobytes()).decode("ascii")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--nb", type=int, default=48, help="对数频段数量")
    ap.add_argument("--ffmpeg", default="D:/ffmpeg/bin/ffmpeg.exe")
    ap.add_argument("--bpm", type=float, default=0.0, help="人工指定 BPM（0=自动检测）")
    args = ap.parse_args()

    sr = 24000                      # 24000/60 = 400 整数 hop，帧点与视频帧严格对齐
    hop = sr // args.fps
    n_fft = 4096

    y = decode(args.ffmpeg, args.audio, sr)
    dur = len(y) / sr
    nframes = int(np.ceil(dur * args.fps))
    print("[decode] %.2fs / %d 帧 @%dfps  sr=%d hop=%d" % (dur, nframes, args.fps, sr, hop))

    import librosa
    # ⚠ librosa.stft 不做 1/N 归一化：满幅正弦的 |X[k]| ≈ N/4，必须在 dB 换算前除掉 N/2，
    # 否则 48 段全部落在 0 dB 附近、压完 -78~-12 的刻度后**整块饱和成 1.0**（实测过）。
    S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop, win_length=n_fft,
                            window="hann", center=True)) / (n_fft / 2.0)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)

    # ── 48 段对数频段能量 → dB → 0..1 ─────────────────────────────
    edges = log_bands(args.nb, 40.0, 11000.0)
    raw = np.zeros((args.nb, S.shape[1]), dtype=np.float32)
    for b in range(args.nb):
        m = (freqs >= edges[b]) & (freqs < edges[b + 1])
        if not m.any():
            m = np.zeros_like(freqs, dtype=bool)
            m[np.argmin(np.abs(freqs - (edges[b] + edges[b + 1]) / 2))] = True
        e = np.sqrt((S[m] ** 2).mean(axis=0))
        raw[b] = 20.0 * np.log10(e + 1e-9)
    db_lo, db_hi = -78.0, -12.0        # 固定参考：不同歌曲之间刻度一致（不是逐曲归一化）
    norm = np.clip((raw - db_lo) / (db_hi - db_lo), 0.0, 1.0)
    # 每段独立弹道（atk 0.55 / rel 0.10 ≈ 30ms 上升、150ms 回落）
    for b in range(args.nb):
        norm[b] = ballistics(norm[b], 0.55, 0.10)

    # ── 响度 / 谱通量 ─────────────────────────────────────────────
    rms = librosa.feature.rms(y=y, frame_length=n_fft, hop_length=hop, center=True)[0]
    rms_db = 20.0 * np.log10(rms + 1e-9)
    rms_n = np.clip((rms_db + 60.0) / 55.0, 0.0, 1.0)
    flux = np.maximum(0.0, np.diff(S, axis=1, prepend=S[:, :1])).sum(axis=0)
    flux = flux / (np.percentile(flux, 99) + 1e-9)
    flux_n = np.clip(flux, 0.0, 1.0)
    flux_n = ballistics(flux_n, 0.75, 0.25)

    # ── 人声基频（旋律走向）──────────────────────────────────────
    print("[pyin] 提取人声基频走向（C3~C6）…")
    f0, vflag, vprob = librosa.pyin(
        y, fmin=float(librosa.note_to_hz("C3")), fmax=float(librosa.note_to_hz("C6")),
        sr=sr, hop_length=hop, frame_length=2048, center=True, fill_na=np.nan)
    m = librosa.hz_to_midi(f0)                       # 未发声处为 nan
    finite = np.isfinite(m)

    # 八度误判纠正：pyin 最常见的错法是整段上下跳一个八度（表现为曲线突然掉到 C3 底噪）。
    # 对每个有声帧给三个候选 {m-12, m, m+12}，用 DP 选「发射代价 + 跳变代价」最小的序列：
    #   emission = |c-m|        （尽量贴近观测值）
    #   transition = 1.2*|c-prev|（强烈偏好连续，八度突跳要付 14.4 的代价才成立）
    # 这样孤立的八度跌落被抓回来，而真实的滑音走向完整保留 —— 比「差 >9 半音就丢帧」温和得多
    # （后者实测把可用帧从 84% 砍到 18%）。
    # ⚠ 回溯陷阱：`bk` 必须**每列都记录最优前驱**，不能只在「跳变更便宜」时才记。
    #   早期版本 bk 初始化为 0，当 cost（本列重启）优于转移时跳过赋值 ⇒ bk 留在 0，
    #   回溯就把「候选 0 = m-12」当成真实前驱，整条链被拽到低八度（实测 592/619 帧全偏低 12
    #   半音，而 dp 终值明明显示正确分支代价为 0）。判据：dp 终值 [12,0,12] 却选出分支 0。
    idx = np.where(finite)[0]
    if len(idx):
        cand = np.stack([m - 12.0, m, m + 12.0])     # 3 × N
        valid = (cand >= 40.0) & (cand <= 88.0)
        cost = np.where(valid, np.abs(cand - m), 1e9)
        dp = cost.copy()
        bk = np.full(cand.shape, -1, dtype=np.int8)  # -1 = 无前驱（乐句起点）
        for k in range(1, len(idx)):
            i, ip = idx[k], idx[k - 1]
            if i - ip > 4:                            # 断句：不惩罚跳变，保留 cost 作起点
                continue
            for c in range(3):
                tr = dp[:, k - 1] + 1.2 * np.abs(cand[c, i] - cand[:, ip])
                j = int(np.argmin(tr))
                bk[c, k] = j                          # 无条件记录，供回溯
                if tr[j] < dp[c, k]:
                    dp[c, k] = tr[j]
        stat = dp[:, len(idx) - 1]
        c = int(np.argmin(stat))
        pick = np.zeros(len(idx), dtype=int)
        for k in range(len(idx) - 1, -1, -1):
            pick[k] = c
            p = int(bk[c, k])
            if p >= 0:
                c = p
        m = np.full(len(m), np.nan, dtype=np.float32)
        m[idx] = cand[pick, idx]
        print("[pyin] 八度分支分布 m-12 / m / m+12 = %d / %d / %d"
              % tuple(int(np.sum(pick == q)) for q in range(3)))

    # 3 帧中值平滑（只平滑、不回填 nan，避免把刚纠正的帧又抹回去）
    m2 = m.copy()
    for i in range(len(m)):
        w = m[max(0, i - 1):i + 2]
        w = w[np.isfinite(w)]
        if np.isfinite(m[i]) and len(w):
            m2[i] = np.median(w)
    m = m2

    midi = np.full(len(f0), -1000.0, dtype=np.float32)
    ok = np.isfinite(m)
    midi[ok] = m[ok] * 10.0
    voiced = (vflag.astype(np.float32) if vflag is not None else ok.astype(np.float32))
    print("[pyin] 可用帧 %d / %d（%.0f%%）" % (ok.sum(), len(ok), 100.0 * ok.mean()))

    # ── 对齐到视频帧数 ────────────────────────────────────────────
    def fit2d(a):
        if a.shape[1] >= nframes:
            return a[:, :nframes]
        pad = np.zeros((a.shape[0], nframes - a.shape[1]), dtype=a.dtype)
        return np.concatenate([a, pad], axis=1)

    def fit1d(a, fill=0.0):
        a = np.asarray(a)
        if len(a) >= nframes:
            return a[:nframes]
        return np.concatenate([a, np.full(nframes - len(a), fill, dtype=a.dtype)])

    norm = fit2d(norm)
    rms_n = fit1d(rms_n)
    flux_n = fit1d(flux_n)
    midi = fit1d(midi, -1000.0)
    voiced = fit1d(voiced)

    # ── BPM / 小节网格 ────────────────────────────────────────────
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, hop_length=hop, units="time")
    tempo = float(tempo) if np.ndim(tempo) == 0 else float(np.atleast_1d(tempo)[0])
    if args.bpm > 0:
        tempo = args.bpm
    bar_sec = 4.0 * 60.0 / tempo                  # 4/4
    t0 = float(beats[0]) if len(beats) else 0.0
    bars = []
    t = t0
    while t < dur:
        bars.append(round(t, 3))
        t += bar_sec

    # ── 段落：小节特征向量 → KMeans 聚簇 → 按簇合并 → 按能量命名 ────
    # 单看响度跳变分不出 verse/chorus（两者常等响），必须叠加音色特征：
    # 谱质心、谱通量、低频占比、人声占比。同一簇的连续小节合并成一个段，
    # 就会出现真实的 verse/chorus 交替，而不是「每 N 小节一刀」。

    feat = []
    for i, bt in enumerate(bars):
        be = bars[i + 1] if i + 1 < len(bars) else dur
        i0, i1 = int(bt * args.fps), max(int(bt * args.fps) + 1, int(be * args.fps))
        i1 = min(i1, nframes)
        sl = slice(i0, i1)
        f = np.zeros(8, dtype=np.float64)
        if i1 > i0:
            f[0] = rms_n[sl].mean()
            f[1] = flux_n[sl].mean()
            f[2] = voiced[sl].mean()
            f[3] = norm[:12, sl].mean()          # 低频占比（bass/drum 体量）
            f[4] = norm[24:36, sl].mean()        # 中频（人声/钢琴基频区）
            f[5] = norm[36:, sl].mean()          # 高频（镲片/空气感）
            f[6] = norm[:, sl].mean()
            f[7] = norm[:12, sl].mean() / (norm[:, sl].mean() + 1e-6)
        feat.append(f)
    feat = np.array(feat)
    bar_rms = feat[:, 0]

    F = (feat - feat.mean(0)) / (feat.std(0) + 1e-9)
    n = len(bars)
    MIN_SIZE = 6                      # 最短段落 6 小节（≈10.4s @136BPM）
    cs  = np.vstack([np.zeros((1, F.shape[1])), np.cumsum(F, 0)])
    cs2 = np.vstack([np.zeros((1, F.shape[1])), np.cumsum(F * F, 0)])

    def seg_cost(a, b):               # 段内平方误差（8 维求和）
        s = cs[b] - cs[a]
        s2 = cs2[b] - cs2[a]
        m = s / (b - a)
        return float(np.sum(s2 - 2.0 * m * s + (b - a) * m * m))

    def partition(pen):
        INF = 1e18
        dp = np.full(n + 1, INF)
        arg = np.zeros(n + 1, dtype=int)
        dp[0] = 0.0
        for b in range(MIN_SIZE, n + 1):
            best, bi = INF, -1
            for a in range(0, b - MIN_SIZE + 1):
                if dp[a] >= INF:
                    continue
                v = dp[a] + seg_cost(a, b) + pen
                if v < best:
                    best, bi = v, a
            dp[b], arg[b] = best, bi
        cuts, b = [], n
        while b > 0:
            cuts.append((arg[b], b))
            b = arg[b]
        return list(reversed(cuts))

    if n >= MIN_SIZE * 2:
        lo, hi_pen = 0.0, 200.0
        for _ in range(24):            # 二分 pen，锁定 11~13 段
            mid = (lo + hi_pen) / 2
            kseg = len(partition(mid))
            if kseg > 13:
                lo = mid
            else:
                hi_pen = mid
        segs = partition(hi_pen)
        print("[section] DP 分段 pen=%.2f → %d 段（min_size=%d 小节）" % (hi_pen, len(segs), MIN_SIZE))
    else:
        segs = [(0, n)]
    secs = [(a, b, 0) for a, b in segs]

    # 命名：响度 0.6 + 人声密度 0.4 的复合分数，最高分=CHORUS
    score = np.array([(0.6 * float(bar_rms[a:b].mean()) + 0.4 * float(feat[a:b, 2].mean()))
                      if b > a else 0.0 for a, b, _ in secs])
    e = np.array([float(bar_rms[a:b].mean()) if b > a else 0.0 for a, b, _ in secs])
    hi = int(np.argmax(score)) if len(score) else 0
    names = []
    for i, (a, b, c) in enumerate(secs):
        sc = float(score[i]) / (float(score[hi]) + 1e-9)
        if i == 0 and len(secs) > 2 and sc < 0.80:
            names.append("INTRO")
        elif i == len(secs) - 1 and len(secs) > 2 and (b - a) <= 10:
            names.append("OUTRO")
        elif i == hi:
            names.append("CHORUS")
        elif 0 < i < len(secs) - 1 and sc < 0.62:
            names.append("BRIDGE")
        else:
            names.append("VERSE")
    # 对外只用编曲惯例的分段字母（A=高能量段 / B=中 / C=低），不臆断 verse/chorus：
    #   A1 B1 A2 … 一眼可读「同样的一段又回来了」，也不会在曲子结构判断错时丢可信度
    letters = []
    for i in range(len(secs)):
        sc = float(score[i]) / (float(score[hi]) + 1e-9)
        letters.append("A" if sc >= 0.95 else ("B" if sc >= 0.78 else "C"))
    cnt = {}
    for i, L in enumerate(letters):
        cnt[L] = cnt.get(L, 0) + 1
        names[i] = "%s%d" % (L, cnt[L])


    sections = []
    for i, (a, b, _c) in enumerate(secs):
        s0 = bars[a]
        s1 = bars[b] if b < len(bars) else dur
        sections.append([round(s0, 3), round(s1, 3), names[i]])

    # ── 落盘 ─────────────────────────────────────────────────────
    out = {
        "version": 1,
        "fps": args.fps,
        "sr": sr,
        "dur": round(dur, 3),
        "nframes": nframes,
        "nbands": args.nb,
        "band_hz": [round(float(x), 1) for x in edges],
        "bpm": round(tempo, 2),
        "bar_sec": round(bar_sec, 4),
        "bars": bars,
        "sections": sections,
        "bands_b64": b64_u8((norm.T * 255.0).round()),
        "rms_b64": b64_u8((rms_n * 255.0).round()),
        "flux_b64": b64_u8((flux_n * 255.0).round()),
        "voiced_b64": b64_u8((np.clip(voiced, 0, 1) * 255.0).round()),
        "midi10_b64": b64_i16(midi.round()),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("[out] %s  (%.1f KB)  bpm=%.1f bar=%.3fs  段落 %d 个" %
          (args.out, os.path.getsize(args.out) / 1024.0, tempo, bar_sec, len(sections)))
    for s in sections:
        print("      %7.2f → %7.2f  %s" % (s[0], s[1], s[2]))


if __name__ == "__main__":
    main()
