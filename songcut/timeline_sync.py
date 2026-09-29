# -*- coding: utf-8 -*-
"""timeline_sync.py —— 歌切主流程的「切入点检测 + 歌词时间轴精确同步」集成层

在 song_cutter 主链路（produce_one）中默认执行，任何一环失败都自动降级、
绝不上抛异常，保证主流程正常产出：

  ① detect_entry / refine_head   切入点检测（onset_v2 三段式：
     说话段 → 静音谷 → 能量回升起点）。修复 LLM 粗切点偏晚吞掉渐强前奏
     （wave_refine 只向回搜 12s 且切点不低于粗起点，粗点落在歌中间时无能为力）。

  ② analyze                      原曲匹配 + 全曲 DTW 分析：
     - slope（速度比）由全曲 DTW 稳健回归给出，钳制 [0.998, 1.002]（伴奏原速播放）；
     - 绝对锚点用能量法 onset（波形实证最可靠），DTW 截距仅作诊断
       （前奏周期性强时整体截距不可靠，曾偏差 2s）；
     - 局部 chroma 互相关（hop=256 ≈ 11.6ms）逐行校验，中位系统性偏移回写锚点；
     - song_end_abs = 校正锚点 + slope × 有声时长 → 尾部切点精确到乐句结束。

  ③ render_lrc                   时间轴映射：t_成品 = onset + (t_lrc − head) × slope
                                   − cut_start + head_pad（纯数学，零开销）。

降级链（produce_one 内实现）：精确同步(DTW) → ASR 对齐(lyric_align) → 原始时间轴。
触发降级的情形：网易云搜不到/下载失败（非免费歌且无 VIP cookie）、DTW 质量差
（互相关残差 RMS > 阈值）、校验点不足、歌词行数过少 —— 每种都落日志。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import onset_v2  # noqa: E402

# ---- 切入点 ----
HEAD_SLACK = 1.2        # 切点保底：伴奏起点前至少留这么多秒自然起手

# ---- 尾部 ----
TAIL_KEEP = 1.6         # 伴奏真实结束后的余韵（秒）

# ---- DTW ----
SLOPE_LO, SLOPE_HI = 0.998, 1.002

# ---- 局部互相关校验（step10 实证参数）----
LC_SR, LC_HOP = 22050, 256
SEG_PRE, SEG_LEN, SEARCH = 3.0, 12.0, 4.0
MAX_CHECK_LINES = 16    # 均匀抽样上限（控制耗时）
MIN_CHECK_N = 6         # 有效校验点下限
GATE_RMS = 0.35         # 残差 RMS（去中位）门槛
GATE_MED = 1.5          # 系统性偏移修正上限（秒）


# ================================================================
# ① 切入点检测
# ================================================================
def detect_entry(ffmpeg, src, rough_start, rough_end, tmp_dir=None, log=print):
    """切入点检测（onset_v2 三段式）。返回 dict(ok, onset, valley, best / reason)。"""
    try:
        r = onset_v2.detect_onset(ffmpeg, src, rough_start, rough_end=rough_end,
                                  window_back=60.0, window_fwd=8.0,
                                  tmp_dir=tmp_dir, log=log)
        r.pop("env", None)
        r.pop("t_axis", None)
        if r.get("ok"):
            valley = [float(x) for x in (r.get("quiet_seg") or [None, None])]
            return dict(ok=True, onset=float(r["onset"]), valley=valley,
                        best=r.get("best"))
        return dict(ok=False, reason="窗口内无合格静音谷候选")
    except Exception as e:                       # 任何异常 → 降级
        return dict(ok=False, reason="检测异常: %s" % (str(e) or repr(e))[:160])


def refine_head(cs, det):
    """用检测结果修正头部切点。返回 (new_cs, moved, note)。

    粗切点偏晚（onset < cs − 0.8s）时：切到静音谷起点（自然起手），
    但保底 onset − HEAD_SLACK（避免把说话尾巴整段带进来）。
    """
    if not det.get("ok"):
        return cs, False, ""
    onset = det.get("onset")
    if onset is None or onset >= cs - 0.8:
        return cs, False, ""
    valley_start = (det.get("valley") or [None])[0]
    new_cs = max(valley_start if valley_start is not None else 0.0, onset - HEAD_SLACK)
    new_cs = round(min(new_cs, onset), 3)
    if new_cs >= cs:
        return cs, False, ""
    return new_cs, True, "粗切点偏晚 %.2fs（前奏被吞）" % (cs - onset)


# ================================================================
# ② 原曲匹配 + 全曲分析
# ================================================================
def match_ref(title, artist, workdir, log=print):
    """搜索并下载原曲（网易云；免费歌直接下载，VIP 歌需 cookie，无则失败）。
    结果缓存 _media_cache/timeline/。返回 dict(ok, ref, sid / reason)。"""
    try:
        import lyrics_sync as LS
        nt = LS.Netease()
        cands = nt.search_top(title, artist or "", n=5)
        if not cands:
            return dict(ok=False, reason="网易云无搜索结果")
        cache = os.path.join(workdir, "_media_cache", "timeline")
        os.makedirs(cache, exist_ok=True)
        for c in cands[:3]:                      # 缓存优先
            p = os.path.join(cache, "ref_%s.mp3" % c.get("id"))
            if os.path.exists(p) and os.path.getsize(p) > 200000:
                return dict(ok=True, ref=p, sid=c.get("id"))
        for c in cands[:3]:                      # 逐个下载
            p = os.path.join(cache, "ref_%s.mp3" % c.get("id"))
            try:
                got = nt.download(c["id"], p, br=320000)
            except Exception:
                got = None
            if got and os.path.exists(p) and os.path.getsize(p) > 200000:
                log("  原曲下载: id=%s %s（320k）" % (c.get("id"), c.get("name", "")))
                return dict(ok=True, ref=p, sid=c.get("id"))
        return dict(ok=False, reason="原曲下载失败（前 3 候选均不可用/非免费且无 VIP cookie）")
    except Exception as e:
        return dict(ok=False, reason="匹配异常: %s" % (str(e) or repr(e))[:160])


def _ref_bounds(y, sr, thr=-60.0, blk=0.05):
    """原曲有声区 [head, end]（文件时间，秒）。阈值 -60dB：网易云文件常带
    1~2s 数字零前导，而 riser 弱起前奏只有 -60dB 左右，粗阈值会误判成静音。"""
    h = int(blk * sr)
    n = len(y) // h
    lv = np.array([20 * np.log10(max(np.sqrt(np.mean(y[i * h:(i + 1) * h] ** 2)), 1e-10))
                   for i in range(n)])
    on = np.where(lv > thr)[0]
    if on.size == 0:
        return 0.0, len(y) / sr
    return float(on[0] * blk), float(min((on[-1] + 1) * blk, len(y) / sr))


def _robust_slope(tr, tc):
    """IRLS 稳健线性拟合：剔除路径跑飞的离群点后取斜率。"""
    A0 = np.vstack([tr, np.ones(len(tr))]).T
    w = np.ones(len(tr))
    sl = ic = 0.0
    for _ in range(4):
        W = np.sqrt(w)[:, None]
        sl, ic = np.linalg.lstsq(A0 * W, tc * np.sqrt(w), rcond=None)[0]
        r = tc - (sl * tr + ic)
        s = 1.4826 * np.median(np.abs(r - np.median(r))) + 1e-6
        w = np.clip(1.0 - (np.abs(r) / (3.0 * s)) ** 2, 1e-3, 1.0)
    r = tc - (sl * tr + ic)
    return float(sl), float(ic), float(np.sqrt(np.mean(r ** 2)))


def _chroma(y, sr=LC_SR, hop=LC_HOP):
    import librosa
    C = librosa.feature.chroma_cens(y=y, sr=sr, hop_length=hop)
    C = C - np.median(C, axis=1, keepdims=True)
    C = C / (np.std(C, axis=1, keepdims=True) + 1e-9)
    C = C / (np.linalg.norm(C, axis=0, keepdims=True) + 1e-9)
    return np.ascontiguousarray(C.T.astype(np.float32))          # (T, 12)


def _local_delta(Y, X, t_ref, t_pred):
    """局部互相关：录播 t_pred 处实际对应的原曲时间 − t_ref（精度 ≈ 12ms）。
    X 以 cut_start 为 0 点，Y 以原曲文件 0 点为 0 点。"""
    a = t_pred - SEG_PRE
    if a < 0:
        return None, 0.0
    sr = LC_SR
    cs = X[int(a * sr): int((a + SEG_LEN) * sr)]
    if len(cs) < sr * 6:
        return None, 0.0
    Cc = _chroma(cs)
    n = Cc.shape[0]
    lo = max(0.0, t_ref - SEARCH)
    rp = Y[int(lo * sr): int(min(len(Y) / sr, t_ref + SEARCH + SEG_LEN) * sr)]
    Cr = _chroma(rp)
    if Cr.shape[0] < n + 4:
        return None, 0.0
    m = Cr.shape[0] - n + 1
    sims = np.empty(m, dtype=np.float32)
    for k in range(m):
        sims[k] = np.sum(Cc * Cr[k:k + n]) / n
    k0 = int(np.argmax(sims))
    m_time = lo + k0 * LC_HOP / sr
    return float((m_time + SEG_PRE) - t_ref), float(sims[k0])


def analyze(title, artist, ffmpeg, src, onset_abs, cut_start, rough_end, lrc_text,
            workdir, ref_info=None, log=print):
    """全曲分析：原曲下载 → DTW 速度比 → 锚点校正 → 伴奏真实结束点。

    返回 dict(ok, slope, head, song_len, song_end_abs, onset_abs(校正后),
              cut_start, rms, med, n, sim, corrected, reason)。
    绝不上抛异常。onset_abs=None（切入点检测失败）时直接放弃——没有锚点
    就没有时间轴，尾部也宁可保守（维持波形精修结果）。
    """
    try:
        if onset_abs is None:
            return dict(ok=False, reason="无切入点锚点（切入点检测未通过）")
        import lyrics_sync as LS
        if ref_info is None:
            ref_info = match_ref(title, artist, workdir, log=log)
        if not ref_info.get("ok"):
            return dict(ok=False, reason=ref_info.get("reason", "原曲匹配失败"))
        ref_path = ref_info["ref"]

        Y = LS.load_audio(ref_path, sr=LS.SR, duration=None)
        head, end = _ref_bounds(Y, LS.SR)
        song_len = end - head

        # X：直接从录播切（避开成品 MP3 的补静音/淡出污染对齐）
        import onset_v2 as OV
        wav = os.path.join(workdir, "_tmp", "tl_sync_x.wav")
        os.makedirs(os.path.dirname(wav), exist_ok=True)
        X = OV.extract_mono(ffmpeg, src, cut_start,
                            min(rough_end - cut_start, song_len + 12.0), wav)
        try:
            if os.path.exists(wav):
                os.remove(wav)
        except BaseException:
            pass
        X = X / (np.max(np.abs(X)) + 1e-9)

        # 全曲 DTW（先验偏移锚定，避免「翻唱窗口长于原曲」时粗对齐失效）
        prior = max(0.0, head - (onset_abs - cut_start))
        res = LS.align_audio(Y, X, verbose=False, prior=prior)
        slope_raw, _ic, _rms_fit = _robust_slope(res["t_ref"], res["t_cov"])
        # 斜率策略（step8 实证）：全曲 DTW 路径在直播音频上可能跑飞（曾给出 1.05~1.18），
        # 而伴奏是原速播放的。偏差 ≤0.5% 视为真实速度比；跑飞则直接取 1.0。
        # 质量把关交给下面的局部互相关校验。
        slope = slope_raw if abs(slope_raw - 1.0) <= 0.005 else 1.0

        info = dict(ok=True, slope=slope, slope_raw=round(slope_raw, 6),
                    head=head, song_len=song_len,
                    onset_abs=onset_abs, cut_start=cut_start,
                    rms=0.0, med=0.0, n=0, sim=0.0, corrected=False)

        # 局部互相关校验 + 系统性偏移校正
        # 注意：_local_delta 按 LC_SR(22050Hz) 索引，X/Y 需重采样到同速率
        if lrc_text:
            try:
                import librosa
                Yc = librosa.resample(np.asarray(Y, dtype=np.float32),
                                      orig_sr=LS.SR, target_sr=LC_SR)
                Xc = librosa.resample(np.asarray(X, dtype=np.float32),
                                      orig_sr=onset_v2.SR, target_sr=LC_SR)
                lines = LS.parse_lrc(lrc_text)
            except Exception:
                lines = []
            if len(lines) >= 4:
                idxs = np.linspace(0, len(lines) - 1,
                                   min(MAX_CHECK_LINES, len(lines))).astype(int)
                deltas, sims = [], []
                for i in idxs:
                    t_lrc = float(lines[i][0])
                    t_pred = onset_abs + (t_lrc - head) * slope - cut_start
                    if t_pred < SEG_PRE + 0.5 or t_pred > (rough_end - cut_start) - 2.0:
                        continue
                    d, sim = _local_delta(Yc, Xc, t_lrc, t_pred)
                    if d is not None:
                        deltas.append(d)
                        sims.append(sim)
                a = np.array(deltas)
                if a.size >= MIN_CHECK_N:
                    med = float(np.median(a))
                    rms_res = float(np.sqrt(np.mean((a - med) ** 2)))
                    info.update(n=int(a.size), med=med, rms=rms_res,
                                sim=float(np.mean(sims)) if sims else 0.0)
                    if rms_res > GATE_RMS or abs(med) > GATE_MED:
                        info["ok"] = False
                        info["reason"] = ("互相关校验未达标（rms=%.3fs med=%+.3fs n=%d）"
                                          % (rms_res, med, a.size))
                        return info
                    if abs(med) > 0.02:
                        info["onset_abs"] = onset_abs - med
                        info["corrected"] = True
                else:
                    info["ok"] = False
                    info["reason"] = "互相关校验点不足（%d/%d）" % (a.size, len(idxs))
                    return info

        info["song_end_abs"] = info["onset_abs"] + slope * song_len
        return info
    except Exception as e:
        return dict(ok=False, reason="分析异常: %s" % (str(e) or repr(e))[:160])


# ================================================================
# ③ 时间轴映射
# ================================================================
def _tag(t):
    t = max(0.0, float(t))
    return "[%02d:%05.2f]" % (int(t // 60), t % 60)


def render_lrc(sync, lrc_text, head_pad=0.0):
    """把 LRC（原曲时间轴）映射到成品相对 0 点。失败返回 None（调用方降级）。"""
    try:
        import lyrics_sync as LS
        lines = LS.parse_lrc(lrc_text)
        out = []
        for t, txt in lines:
            tf = sync["onset_abs"] + (float(t) - sync["head"]) * sync["slope"] \
                - sync["cut_start"] + head_pad
            out.append("%s%s" % (_tag(tf), txt))
        return "\n".join(out) + "\n" if out else None
    except Exception:
        return None
