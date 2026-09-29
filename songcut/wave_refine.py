# -*- coding: utf-8 -*-
"""wave_refine.py —— 音频波形边界精修（歌切切点以波形为准）

输入：源录播文件 + ASR/LLM 给出的粗切点（rough_start / rough_end，秒）
输出：精确的 cut_start / cut_end（秒）

策略：
  起点伴奏定位：在粗起点前 [-12, +3] 窗口内，寻找「持续发声块」的左沿——
    伴奏/演唱的能量连续（间隙 < GAP_TOL 秒），说话通常间隙更碎、且会在歌前停顿；
  结点果断切分：从粗终点向后找「静音持续 ≥ QUIET_HOLD 秒」的转折点 t_sil；
    尾部缓冲 = t_sil + POST_PAD(2s)；若缓冲内检测到新发声（如开始说话），
    提前截断到发声前 0.05s；若 [t_sil, t_sil+POST_PAD] 全静音则保留整段缓冲。
  起点缓冲 = 伴奏起点 - PRE_PAD(1s)；若缓冲内有残余活动（说话未停），
    截断到活动块结束后、伴奏起点前留至少 0.12s。
"""
import io, os, subprocess
import numpy as np

SR = 16000          # 分析用重采样率
HOP = 0.02          # 包络步长（秒）
WIN = 0.04          # 包络窗口（秒）
GAP_TOL = 1.1       # 判定"连续发声"允许的最大间隙（秒）
QUIET_HOLD = 1.5    # 判定"静音开始"需要持续安静的时长（秒）
PRE_PAD = 1.0       # 头部静音缓冲
POST_PAD = 2.0      # 尾部静音缓冲
SEARCH_BACK = 12.0  # 伴奏起点向前搜索范围
SEARCH_END = 18.0   # 结尾向后搜索范围


def _extract_mono_wav(ffmpeg, src, start, end, tmp_wav, timeout=600):
    dur = max(1.0, end - start)
    cmd = [ffmpeg, "-y", "-v", "error", "-ss", "%.3f" % max(0, start), "-i", src,
           "-t", "%.3f" % dur, "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", tmp_wav]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=0x08000000 if os.name == "nt" else 0)
    if p.returncode != 0:
        raise RuntimeError("波形提取失败：%s" % (p.stderr or "")[-200:])


def _envelope(x):
    n_hop = int(HOP * SR)
    n_win = int(WIN * SR)
    n = (len(x) - n_win) // n_hop
    if n <= 0:
        return np.zeros(1)
    idx = np.arange(n) * n_hop
    # 分窗 RMS（向量化）
    frames = np.lib.stride_tricks.sliding_window_view(x, n_win)[::n_hop][:n]
    return np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))


def _runs(mask):
    """布尔序列 → [(start_idx, end_idx_exclusive)]"""
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


def _merge_runs(runs, gap_idx):
    """按最大间隙合并相邻 run。gap_idx: 允许合并的最大间隙（采样 idx 数）"""
    if not runs:
        return []
    merged = [list(runs[0])]
    for s, e in runs[1:]:
        if s - merged[-1][1] <= gap_idx:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return merged


def refine_segment(ffmpeg, src, rough_start, rough_end, tmp_dir=None, log=print):
    """返回 dict(cut_start, cut_end, onset, silence_at, notes)"""
    import tempfile
    tmp_dir = tmp_dir or tempfile.gettempdir()
    os.makedirs(tmp_dir, exist_ok=True)
    base = os.path.join(tmp_dir, "wr_%d" % (abs(hash((src, round(rough_start, 2)))) % 10**8))

    a = max(0.0, rough_start - SEARCH_BACK)
    b = rough_end + SEARCH_END
    wav = base + ".wav"
    try:
        _extract_mono_wav(ffmpeg, src, a, b, wav)
        with io.open(wav, "rb") as f:
            import wave as wavmod
        # 用 wave 模块读取
        import wave as wavmod
        w = wavmod.open(wav, "rb")
        raw = w.readframes(w.getnframes())
        w.close()
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    finally:
        # 临时 wav 仅为本函数创建，删除失败不阻断主流程
        try:
            if os.path.exists(wav):
                os.remove(wav)
        except BaseException:
            pass

    env = _envelope(x)
    hop_n = env.size
    if hop_n < 10:
        return {"cut_start": rough_start, "cut_end": rough_end,
                "onset": None, "silence_at": None, "notes": ["包络过短，回退粗切点"]}

    def t2i(t): return int(round((t - a) / HOP))
    def i2t(i): return a + i * HOP

    floor = float(np.percentile(env, 5))
    peak = float(np.percentile(env, 99))
    # 直播音频经过压缩处理动态小：p5 通常即代表底噪/静音档
    th = max(floor * 2.5, 0.04)                  # 发声阈值
    active = env > th
    notes = ["floor=%.4f peak=%.4f th=%.4f" % (floor, peak, th)]

    gap_idx = int(GAP_TOL / HOP)
    blocks = _merge_runs(_runs(active), gap_idx)          # 连续发声块（间隙<1.1s 合并）
    S_i, E_i = t2i(rough_start), t2i(rough_end)

    # ---- 起点伴奏定位：粗起点附近（<=S+1.5s）最靠前的、覆盖粗起点的连续块 ----
    onset_i = None
    covering = [bl for bl in blocks if bl[0] <= S_i + int(1.5 / HOP) and bl[1] >= S_i]
    if covering:
        bl = min(covering, key=lambda z: z[0])
        onset_i = bl[0]
        notes.append("伴奏起点=块左沿 %.2fs（粗起点 %.2f）" % (i2t(onset_i), rough_start))
    else:
        # 没有覆盖块：粗起点附近可能歌声很轻，退而求其次——最后一个在 S 前的活动沿
        pre = [bl for bl in blocks if bl[1] <= S_i + int(1.0 / HOP)]
        if pre:
            onset_i = pre[-1][0]
            notes.append("⚠ 未找到覆盖块，取粗起点前最后活动沿 %.2fs" % i2t(onset_i))
        else:
            onset_i = S_i
            notes.append("⚠ 窗口内无活动块，伴奏起点=粗起点")

    onset_t = i2t(onset_i)

    # ---- 头部缓冲：伴奏起点前 1s；若其中仍有活动（说话残留）→ 截到静音处 ----
    cut_start = onset_t - PRE_PAD
    buf_s, buf_e = t2i(cut_start), onset_i
    if buf_e > buf_s:
        seg_act = active[buf_s:buf_e]
        runs_in = _runs(seg_act)
        # 活动块若触及缓冲末端（与伴奏块几乎相连）视为说话未停 → 截断
        near = [r for r in runs_in if r[1] >= buf_e - int(0.35 / HOP)]
        if runs_in and near:
            # 说话到缓冲末尾：只留 0.12s，且缓冲起点从该活动块起点后算起
            first_act = near[0][0]
            cut_start = max(cut_start, i2t(buf_s + first_act) - 0.0)
            # 真正可用的静音从活动块结束后开始
            sil_end = buf_s + near[0][1]
            cut_start = max(onset_t - min(PRE_PAD, max(0.12, (onset_t - i2t(sil_end)))), onset_t - 0.12)
            notes.append("头部缓冲检测到讲话 → 截断至 %.2fs" % cut_start)
        else:
            notes.append("头部缓冲全静音，保留 %.1fs" % PRE_PAD)
    cut_start = max(0.0, cut_start)

    # ---- 结点：粗终点后第一个「静音持续≥QUIET_HOLD」的转折 ----
    quiet_idx = int(QUIET_HOLD / HOP)
    silence_at = None
    from_s = max(E_i, onset_i)
    inactive_after = _runs(~active[from_s:])
    for s, e in inactive_after:
        if e - s >= quiet_idx:
            silence_at = i2t(from_s + s)
            break
    if silence_at is None:
        cut_end = rough_end
        notes.append("⚠ 搜索范围内未找到持续静音，终点=粗终点")
    else:
        cand = silence_at + POST_PAD
        # 缓冲内新发声（开始说话）→ 提前截断
        bs, be = t2i(silence_at), t2i(cand)
        trunc = None
        if be > bs:
            for s, e in _runs(active[bs:min(be, active.size)]):
                st_t = i2t(bs + s)
                if st_t - silence_at > 0.45:            # 刚结束的歌尾不算（留给淡出）
                    trunc = st_t
                    break
        if trunc is not None:
            cut_end = trunc - 0.05
            notes.append("尾部缓冲 %.2fs 处检测到发声 → 提前截断至 %.2f" % (trunc, cut_end))
        else:
            cut_end = cand
            notes.append("静音起点 %.2fs，保留 %.1fs 缓冲 → %.2f" % (silence_at, POST_PAD, cut_end))

    # 兜底约束
    cut_end = max(cut_end, rough_end - 1.0)
    cut_start = min(cut_start, rough_start)
    dur = cut_end - cut_start
    if not (40 <= dur <= 900):
        notes.append("⚠ 精修后时长 %.1fs 越界，回退粗切点" % dur)
        return {"cut_start": rough_start, "cut_end": rough_end,
                "onset": onset_t, "silence_at": silence_at, "notes": notes}

    return {"cut_start": round(cut_start, 3), "cut_end": round(cut_end, 3),
            "onset": round(onset_t, 3), "silence_at": round(silence_at, 3) if silence_at else None,
            "notes": notes}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 4:
        FF = r"D:/ffmpeg/bin/ffmpeg.exe"
        r = refine_segment(FF, sys.argv[1], float(sys.argv[2]), float(sys.argv[3]))
        for k, v in r.items():
            print(k, "=", v)
