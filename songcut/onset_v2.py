# -*- coding: utf-8 -*-
"""onset_v2.py —— 以音频波形为主的「开唱切入点」检测（v2.1）

设计动机
--------
v1（wave_refine.py）用「绝对/半绝对能量阈值」二值化后找连续发声块左沿：
    th = max(p5(env) * 2.5, 0.04)
两个致命弱点：
  1) 歌曲入场常是 riser / pad / fade-in 这类**低能量渐强**段（寄明月实例：
     -54dB → -33dB，比说话段低 20~40dB），会被这道门槛整体判成静音，
     onset 被推迟到鼓点真正进入处（本例会晚 ~4.2s）；
  2) 只在粗切点前 12s 搜索，LLM 粗切点常落在歌中间（寄明月偏晚 28s），
     窗口内全是歌，onset 只能取窗口左沿 → 偏差可达 15s 以上。

v2.1 思路：三段式模式匹配，全部**局部相对**判定
------------------------------------------------
    [说话段]  →  [静音谷]  →  [能量持续回升 = 伴奏/演唱起点]

1. 多阈值候选静音谷（解决“流间隙数字零污染底噪”问题）：
   阈值集合 {noise+6, speech-45, speech-35, speech-25}，各自二值化，
   形态学闭/开运算去毛刺，汇总去重。谷的合法性由后续局部验证决定，
   不依赖单一全局阈值。

2. 每个候选谷的局部验证（全部相对量）：
   - v_level   = 谷内 p20 能量
   - 对比度     = 谷前 2s p80 − v_level ≥ 15 dB（谷前必须是活动段）
   - 回升       = 谷后 3s 内 max − v_level ≥ 8 dB
   - 回升持久性 = cross 后 0.8s 中位 ≥ v_level+8（排除换气/假起音后回落）
   - 持续发声   = 谷后 22s 内，以局部活动线论最长连续活动 ≥ 18s
                 （歌是连续发声；说话碎片化、必被 0.3s+ 停顿切断）
   - onset 精修 = 从首次越过 v_level+8 处**向前回溯**到 v_level+2 ——
                 抓渐强离开底噪的物理起点，而不是等它涨到某个响度

3. 选优：持续发声占比 × 回升幅度 × 距粗切点距离 综合评分。

用法
----
    from onset_v2 import detect_onset
    r = detect_onset(ffmpeg, src, rough_start, rough_end=None, window_back=60)
    r["onset"]      # 开唱切入点（录播绝对秒）
    r["quiet_seg"]  # 静音谷 [起, 止]
    r["best"]       # 判定依据明细
"""
import os, subprocess, wave
import numpy as np

SR = 16000
HOP = 0.01          # 包络/特征步长（秒）→ 10ms 分辨率
WIN = 0.05          # RMS 窗长
NFFT = 1024

# —— 阈值（全部相对）——
NOISE_MARGIN = 6.0      # 静音阈值① = 底噪 + 6 dB
SPEECH_DROPS = (45.0, 35.0, 25.0)   # 静音阈值②③④ = 说话能量 − N dB
MIN_QUIET = 0.35        # 静音谷最短时长（秒）
RISE_DB = 8.0           # 谷后需抬升（相对谷底）
RISE_WITHIN = 3.0       # 抬升观察窗（秒）
HOLD_DB = 2.0           # 回溯：能量最后一次低于 谷底+2dB 处 = 渐强起点
CONTRAST_DB = 15.0      # 谷前活动段与谷底的最小对比度
SUSTAIN = 22.0          # 持续发声验证窗（秒）
SUSTAIN_LEN = 18.0      # 该窗口内最长连续活动需 ≥ 此值（秒）
SUSTAIN_RATIO = 0.80    # 兜底：活动占比门槛
ACT_MARGIN = 12.0       # 活动线 = 谷底 + 12 dB（与局部 p20 取高者）
FLUX_Z = 1.5            # 频谱变化 z-score 抬升门槛（旁证）
MED_K = 5               # 包络中值滤波窗（去抖动）
MA_K = 7                # 包络均值滤波窗


# ---------------- I/O ----------------
def extract_mono(ffmpeg, src, start, dur, out_wav, timeout=900):
    cmd = [ffmpeg, "-y", "-v", "error", "-ss", "%.3f" % max(0.0, start), "-i", src,
           "-t", "%.3f" % dur, "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", out_wav]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=0x08000000 if os.name == "nt" else 0)
    if p.returncode != 0:
        raise RuntimeError("音频提取失败：%s" % (p.stderr or "")[-300:])
    w = wave.open(out_wav, "rb")
    x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64) / 32768.0
    w.close()
    return x


# ---------------- 特征 ----------------
def rms_db(x):
    n_win, n_hop = int(WIN * SR), int(HOP * SR)
    n = (len(x) - n_win) // n_hop
    fr = np.lib.stride_tricks.sliding_window_view(x, n_win)[::n_hop][:n]
    r = np.sqrt(np.mean(fr ** 2, axis=1))
    return 20 * np.log10(np.maximum(r, 1e-10))


def smooth_db(e):
    from scipy.ndimage import median_filter, uniform_filter1d
    return uniform_filter1d(median_filter(e, size=MED_K), size=MA_K)


def spectral_flux(x):
    n_hop = int(HOP * SR)
    win = np.hanning(NFFT)
    n = max(2, (len(x) - NFFT) // n_hop)
    fr = np.lib.stride_tricks.sliding_window_view(x, NFFT)[::n_hop][:n]
    S = np.abs(np.fft.rfft(fr * win, axis=1))
    L = np.log1p(S)
    f = np.maximum(np.diff(L, axis=0), 0).sum(axis=1)
    f = np.concatenate([f[:1], f])
    return f


def align_len(a, b):
    m = min(len(a), len(b))
    return a[:m], b[:m]


def _runs(mask):
    if mask.size == 0:
        return []
    d = np.diff(mask.astype(np.int8))
    starts = list(np.where(d == 1)[0] + 1)
    ends = list(np.where(d == -1)[0] + 1)
    if mask[0]:
        starts = [0] + starts
    if mask[-1]:
        ends = ends + [mask.size]
    return list(zip(starts, ends))


# ---------------- 主检测 ----------------
def detect_onset(ffmpeg, src, rough_start, rough_end=None,
                 window_back=60.0, window_fwd=8.0, tmp_dir=None, log=print):
    from scipy.ndimage import binary_closing, binary_opening
    tmp_dir = tmp_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmpwav")
    os.makedirs(tmp_dir, exist_ok=True)
    a = max(0.0, rough_start - window_back)
    b = (rough_end if rough_end else rough_start) + window_fwd
    dur = max(2.0, b - a)
    wav = os.path.join(tmp_dir, "onsetv2_%d.wav" % (abs(hash((src, round(rough_start, 2)))) % 10**8))
    try:
        x = extract_mono(ffmpeg, src, a, dur, wav)
    finally:
        if os.path.exists(wav):
            os.remove(wav)

    e = smooth_db(rms_db(x))
    flux, _ = align_len(spectral_flux(x), e)
    flux = flux[:e.size]

    from scipy.ndimage import median_filter as _mf
    wmed = int(0.8 / HOP) | 1
    noise = float(np.percentile(_mf(e, size=wmed), 1))   # 底噪：最安静 0.8s 的中位分位
    speech = float(np.percentile(e, 80))           # 说话/演唱参考能量
    info = dict(a=a, b=b, dur=dur, noise=noise, speech=speech, hop=HOP)

    def t2i(t): return int(round((t - a) / HOP))
    def i2t(i): return a + i * HOP

    # ---- 1) 多阈值候选静音谷 ----
    thr_list = sorted({noise + NOISE_MARGIN, *(speech - d for d in SPEECH_DROPS)})
    seen, cands = set(), []
    for q in thr_list:
        m = e < q
        m = binary_closing(m, structure=np.ones(int(0.15 / HOP)))       # 填毛刺
        m = binary_opening(m, structure=np.ones(int(MIN_QUIET / HOP)))  # 丢过短段
        for s, en in _runs(m):
            if i2t(en) > b - 1.0:                   # 贴窗口右沿不可靠
                continue
            key = (s // 5, en // 5)
            if key in seen:
                continue
            seen.add(key)
            cands.append((s, en))
    info["thr_list"] = [round(v, 1) for v in thr_list]
    info["n_quiet_runs"] = len(cands)

    # ---- 2) 局部验证 ----
    scored = []
    n3 = int(RISE_WITHIN / HOP)
    nq3 = int(0.3 / HOP)
    nhold = int(0.8 / HOP)
    nsus = int(SUSTAIN / HOP)
    for s, en in cands:
        seg = e[s:en]
        v_level = float(np.percentile(seg, 20)) if seg.size else 0.0
        # 对比度：谷前 2s 必须是活动段
        pre = e[max(0, s - int(2.0 / HOP)):s]
        if pre.size < 5:
            continue
        a_pre = float(np.percentile(pre, 80))
        if a_pre - v_level < CONTRAST_DB:
            continue
        # 回升
        post = e[en:min(e.size, en + n3)]
        if post.size < 5:
            continue
        rise = float(np.max(post) - v_level)
        if rise < RISE_DB:
            continue
        cross = en + int(np.argmax(post > v_level + RISE_DB))
        # 回升持久性：cross 后 0.8s 中位仍高于 v+8（排除假起音）
        hold = e[cross:min(e.size, cross + nhold)]
        if hold.size < 3 or float(np.median(hold)) < v_level + RISE_DB:
            continue
        # onset 回溯：从 cross 回退到 v+2（渐强离开底噪处）
        back = cross
        lim = max(0, s - int(0.5 / HOP))
        while back > lim and e[back] > v_level + HOLD_DB:
            back -= 1
        onset_i = back + 1
        # 持续发声：从 cross 起、以「谷底+12 / 活动参考−45」中较高者为活动线，
        # 连续活动（容忍 ≤0.3s 毛刺）需 ≥ SUSTAIN_LEN —— 歌开始后不会停，
        # 说话 2s 内必被停顿切断（这是区分「歌的起点谷」与「说话换气谷」的关键）
        sus = e[en:min(e.size, en + nsus)]
        if sus.size < nsus // 2:
            continue
        act_line = max(v_level + ACT_MARGIN, speech - 45.0)
        actm = binary_closing(sus > act_line, structure=np.ones(int(0.3 / HOP)))
        k = cross - en
        if k >= actm.size:
            continue
        if not actm[k]:
            j = k
            while j < actm.size and not actm[j]:
                j += 1
            k = j
            if k >= actm.size:
                continue
        end_k = k
        while end_k < actm.size and actm[end_k]:
            end_k += 1
        sustain = (end_k - k) * HOP
        ratio = float(np.mean(actm))
        if sustain < SUSTAIN_LEN:
            continue
        # 频谱旁证
        f_pre = flux[max(0, s - int(1.0 / HOP)):s]
        f_post = flux[en:min(flux.size, en + int(1.0 / HOP))]
        z = 0.0
        if f_pre.size > 3 and f_post.size > 3:
            sd = float(np.std(f_pre)) or 1e-9
            z = float((np.mean(f_post) - np.mean(f_pre)) / sd)
        scored.append(dict(qs=i2t(s), qe=i2t(en), valley=round(v_level, 1),
                           rise=round(rise, 1), contrast=round(a_pre - v_level, 1),
                           ratio=round(ratio, 3), sustain=round(sustain, 1), flux_z=round(z, 2),
                           onset=i2t(onset_i), cross=i2t(cross),
                           dur_quiet=round(i2t(en) - i2t(s), 2)))
    info["candidates"] = scored

    if not scored:
        info["ok"] = False
        info["onset"] = None
        return info

    # ---- 3) 嵌套/重叠谷去重：同一处静音被不同阈值切成多个候选时，
    #         保留谷底最深（最"纯"静音）的那个 —— 宽谷会吞掉渐强段，
    #         其 onset 回溯点偏晚，不能让它参赛 ----
    def _overlap(c1, c2):
        ov = min(c1["qe"], c2["qe"]) - max(c1["qs"], c2["qs"])
        return ov > 0.5 * min(c1["dur_quiet"], c2["dur_quiet"])

    kept = []
    for c in sorted(scored, key=lambda z: z["valley"]):     # 谷底最深优先
        if not any(_overlap(c, k) for k in kept):
            kept.append(c)

    # ---- 4) 选优 ----
    for c in scored:
        c["score"] = (min(c["sustain"], SUSTAIN) / SUSTAIN * 2.0
                      + min(c["rise"], 30.0) / 30.0
                      - abs(c["onset"] - rough_start) / 60.0)
    best = max(kept, key=lambda c: c["score"])
    info["ok"] = True
    info["onset"] = round(best["onset"], 3)
    info["best"] = best
    info["quiet_seg"] = [round(best["qs"], 3), round(best["qe"], 3)]
    info["env"] = e
    info["t_axis"] = a + np.arange(e.size) * HOP
    return info


if __name__ == "__main__":
    import sys, json
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from songcut import config as CFG
    if len(sys.argv) < 3:
        raise SystemExit("用法：python songcut/onset_v2.py <录播音视频> <粗切点秒>")
    FF = CFG.ffmpeg()
    SRC = sys.argv[1]
    RS = float(sys.argv[2])
    r = detect_onset(FF, SRC, RS, rough_end=RS + 225.0, window_back=60.0, window_fwd=8.0)
    r.pop("env", None); r.pop("t_axis", None)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=float))
