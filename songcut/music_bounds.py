# -*- coding: utf-8 -*-
"""music_bounds.py —— 在**录播音频自身**上定位歌曲真实边界（伴奏起点 / 终点）

为什么需要它（2026-10-10 事故复盘）
------------------------------------------------------------------
旧链路用 `detect_entry`（说话段 → 静音谷 → 能量回升）定起点，用波形静音
定终点。这是**人声启发式**，与「歌曲边界」系统性错位：

  ① 前奏是纯伴奏、音量很轻（实测昨日青空前奏 RMS ≈ −32dB，比说话低 11dB）
     → 被当成「静音谷」的延续，直到人声进来才判定回升 ⇒ **前奏整段被切掉**。
  ② 说话本身也是能量 ⇒ 切点落在说话中间 ⇒ **片头塞进一段闲聊**。

而「拿原曲做音频匹配」在实况里也不成立：主播用全民K歌伴奏，编曲/和声与
网易云官方版差异过大。实测（2026-10-08 场）：
   · 全曲 DTW：slope = 1.503（跑飞），残差 4.77s
   · chroma 指纹多片段投票：5 段互相矛盾，score 仅 0.15~0.23
两者都不可用，故改为在录播自身信号上做**音乐活动检测**。

判据（实测标定）
------------------------------------------------------------------
  beat  = onset 包络自相关在 0.30~1.20s（50~200BPM）区间的峰值 → 伴奏节拍
  flat  = 频谱平坦度 → 说话/噪声高（0.4~0.7），乐音低（0.05~0.20）
  score = beat − 0.5 × flat
    纯伴奏前奏：+0.17 ~ +0.56      说话：−0.21 ~ +0.19
单一帧噪声大（歌唱时人声会扰乱 beat），因此用 **5 帧向前 min 窗口** 判定：
只有「接下来 5 秒都像音乐」才算音乐，避免把说话尾巴判成前奏。
允许演唱中途的说话被打断（覆盖率 ≥ 55% 即视为同一段）。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import onset_v2  # noqa: E402

HOP = 1.0            # 特征帧步长（秒）
WIN = 3.0            # 每帧分析窗口（秒）
BEAT_LO, BEAT_HI = 0.30, 1.20     # 节拍周期搜索范围（秒）
SCORE_T = 0.15       # 音乐判定阈值
MIN_WIN = 5          # 向前 min 窗口帧数（=5 秒）
COVER_MIN = 0.55     # （保留兼容）起点到第一句歌词之间「像音乐」的覆盖率下限
GAP_SIL = 10.0       # 起点到第一句歌词之间允许的最长连续静音（秒）
MAX_HEAD_PRE = 35.0  # 前奏长度上限：伴奏起点距第一句歌词不超过这么多秒
MAX_GAP_NONMUSIC = 15.0   # 起点到第一句歌词之间允许的最长连续「不像音乐」段（秒）
HEAD_PAD = 0.25      # 切点在伴奏起点前留的自然起手（秒）
TAIL_KEEP = 1.8      # 伴奏结束后的余韵（秒）
QUIET_DB = -45.0     # 尾部：跌到这个电平即视为歌曲已结束（dBFS）
DEEP_SIL_DB = -55.0  # 「静音谷」判定：远低于轻声前奏（实测前奏 −34~−46dB，谷 −62~−112dB）
VALLEY_MIN = 0.5     # 静音谷最短时长（秒）
VALLEY_HOP = 0.05    # 静音谷细扫步长（秒）
VALLEY_NEAR = 3.0    # 静音谷结束点距 onset 多远之内才认作「假切入分界」（秒）
VALLEY_POST_MAX = 0.5   # 谷后 post 秒内仍静音的时间占比上限（排除歌间空档/长静场）
SIL_MARGIN = 20.0    # 尾部回退：相对演唱段 RMS 的衰减量（dB）
SIL_HOLD = 1.5       # 尾部回退：需持续这么久才算静音（秒）


def profile(ffmpeg, src, t0, t1, tmp_dir=None, log=print):
    """提取 [t0, t1) 的音频并逐秒给出 (t_abs, rms_db, flat, beat)。"""
    t0 = max(0.0, float(t0))
    dur = max(1.0, float(t1) - t0)
    tmp_dir = tmp_dir or os.path.join(os.getcwd(), "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    wav = os.path.join(tmp_dir, "mb_%d.wav" % (int(t0 * 100) % 1000000007))
    try:
        x = onset_v2.extract_mono(ffmpeg, src, t0, dur, wav)
    finally:
        try:
            if os.path.exists(wav):
                os.remove(wav)
        except BaseException:
            pass
    return profile_array(x, onset_v2.SR, t0)


def profile_array(x, sr, t0=0.0):
    """对已提取的单声道数组逐秒计算特征。"""
    import librosa
    y = np.asarray(x, dtype=np.float32)
    total = len(y) / float(sr)
    out = []
    n = int((total - WIN) / HOP) + 1
    for i in range(max(0, n)):
        a = int(i * HOP * sr)
        seg = y[a: a + int(WIN * sr)]
        if len(seg) < sr:
            break
        rms = 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-10)
        S = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) + 1e-10
        flat = float(np.exp(np.mean(np.log(S))) / np.mean(S))
        oe = librosa.onset.onset_strength(y=seg, sr=sr, hop_length=256)
        oe = oe - np.mean(oe)
        if np.std(oe) < 1e-9:
            beat = 0.0
        else:
            ac = np.correlate(oe, oe, mode="full")[len(oe) - 1:]
            ac = ac / (ac[0] + 1e-9)
            fps = float(sr) / 256
            lo, hi = int(BEAT_LO * fps), min(int(BEAT_HI * fps), len(ac) - 1)
            beat = float(np.max(ac[lo:hi])) if hi > lo else 0.0
        out.append((t0 + i * HOP, rms, flat, beat))
    return out


def _music_flags(prof):
    """score 序列 → 布尔「像音乐」序列（中心 5 帧窗口中位数）。

    用中位数而非最小值：前奏里偶有弱拍/换气帧 score 掉到 0 附近（晚婚
    6883s 实测 −0.07），min 窗口会把整段前奏判成非音乐；中位数抗单点噪声。
    用中心窗口而非向前窗口：向前看会把后面的高分带进来，让起点提前 1~2s。
    """
    sc = [b - 0.5 * f for (_t, _r, f, b) in prof]
    n = len(sc)
    half = MIN_WIN // 2
    flags = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        flags.append(float(np.median(sc[lo:hi])) > SCORE_T)
    return flags


def find_head(prof, t_first, back=48.0):
    """从「第一句歌词」向前找伴奏真正起来的点。返回 (abs_time, note)。

    取窗口内**最早**的合格伴奏帧；合格后不再要求全程连续——实况里主播常在
    前奏里插话（实测昨日青空前奏 26s，中间说话 12s），说话盖过轻声伴奏时
    单帧判不出音乐是正常现象，不能用覆盖率把它一刀切断。

    合格条件：
      ① 自身像音乐（5 帧 min 窗口）
      ② 非孤立帧：其后 3 帧内至少还有 2 帧像音乐
      ③ 到第一句歌词之间没有长静音（连续 > GAP_SIL 秒低于 −50dB），
         否则说明中间隔着另一段内容（别的歌 / 长时间静场），不是同一首。
    """
    if not prof:
        return None, "无特征数据"
    flags = _music_flags(prof)
    ts = [p[0] for p in prof]
    rms = [p[1] for p in prof]
    i1 = min(range(len(ts)), key=lambda i: abs(ts[i] - t_first))
    lo_i = 0
    for i in range(len(ts)):
        if ts[i] < t_first - back:
            lo_i = i
    best = None
    for i in range(lo_i, i1):
        if not flags[i]:
            continue
        if sum(1 for f in flags[i: min(len(flags), i + 3)] if f) < 2:
            continue                                   # 孤立帧，跳过
        if t_first - ts[i] > MAX_HEAD_PRE:
            continue                                   # 离第一句太远 → 是别的段落，不是前奏
        # 到第一句歌词之间：不能有长静音，也不能有长段「不像音乐」
        g_sil = run_s = 0
        g_non = run_n = 0
        for k in range(i, i1 + 1):
            run_s = run_s + 1 if rms[k] < -50.0 else 0
            g_sil = max(g_sil, run_s)
            run_n = run_n + 1 if not flags[k] else 0
            g_non = max(g_non, run_n)
        if g_sil > GAP_SIL or g_non > MAX_GAP_NONMUSIC:
            continue
        best = i
        break                                          # 取最早的一个
    if best is None:
        return None, "窗口内未检出伴奏起点（%.0fs 内无合格音乐帧）" % back
    return round(float(ts[best]) - HEAD_PAD, 3), "伴奏起点 %.2f（音乐帧，前留 %.2fs）" % (
        ts[best], HEAD_PAD)


def last_deep_valley(ffmpeg, src, t0, t1, tmp_dir=None):
    """兼容壳：返回 (t0,t1) 内最后一段深静音谷的结束时刻（无则 None）。"""
    vs = deep_valleys(ffmpeg, src, t0, t1, tmp_dir=tmp_dir)
    return vs[-1][1] if vs else None


def deep_valleys(ffmpeg, src, t0, t1, tmp_dir=None, post=20.0):
    """(t0, t1) 内的深静音谷列表 [(start, end, min_db, post_sil_ratio)]，时间升序。

    post_sil_ratio = 谷结束后 post 秒内「仍低于 DEEP_SIL_DB」的时间占比：
      · 真起点前的谷 → 后面立刻进前奏，占比 ≈ 0
      · 歌间空档/长时间静场 → 后面还是静音，占比高
    调用方用它把「假切入分界」和「别的空档」区分开。

    为什么必须补这一条（2026-10-10 二次复盘）
    ---------------------------------------------------------------
    主播的「假切入」实测形态（两首同时命中；下列数字为 0.05s 窗 RMS 独立取证）：

      昨日青空：7574.60~7576.75 −79dB 静音 → 7576.8 起一小段伴奏 → 7580~7588 说话
                → 7588.55~7589.40 −87dB 静音 → **7589.40 伴奏重来**（−37.8dB 渐强）
                → 7602.4 起唱
      晚婚    ：6892~6908「试唱一句 + 全部说话」→ 6907.25~6908.95 −93dB → 短暂一句
                人声 → 6909.35~6910.30 −81dB → **6910.30 伴奏正式起**（前奏 18.8s
                ≈ 官方 17.95s）→ 6929.08 起唱

    伪起点（一小段伴奏 / 一句试唱）与真前奏在音频特征上**同源**——都是低平坦度
    + 有节拍，`score = beat − 0.5×flat` 都过 SCORE_T。`find_head` 按设计取窗口内
    **最早**的合格帧（为的是救回被说话打断的连续前奏），于是必然切进伪起点：
    实测昨日青空给出 7575.02（吞进 7580~7588 的说话 + 两段静音）、晚婚给出
    6877.25（吞进试唱与说话，成片片头多出 30s 闲聊）。

    两例的唯一稳定共同点是：**真起点与伪起点之间隔着一段深静音谷**（−79~−93dB，
    比轻声前奏还低 20dB 以上，是播放器暂停/切轨留下的空档）。阈值 −55dB 安全：
    实测前奏最弱也有 −46dB，不会误伤。用法见 head_after_valley()。
    """
    t0 = max(0.0, float(t0))
    dur = float(t1) - t0
    if dur <= VALLEY_MIN:
        return []
    tmp_dir = tmp_dir or os.path.join(os.getcwd(), "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    wav = os.path.join(tmp_dir, "mbv_%d.wav" % (int(t0 * 100) % 1000000007))
    try:
        x = onset_v2.extract_mono(ffmpeg, src, t0, dur, wav)
    finally:
        try:
            if os.path.exists(wav):
                os.remove(wav)
        except BaseException:
            pass
    sr = onset_v2.SR
    y = np.asarray(x, dtype=np.float32)
    if not len(y):
        return []
    w = max(1, int(VALLEY_MIN * sr))
    hop = max(1, int(VALLEY_HOP * sr))
    if len(y) < w:
        return []
    c = np.concatenate(([0.0], np.cumsum(y * y, dtype=np.float64)))
    n_win = (len(y) - w) // hop + 1
    idx = np.arange(n_win) * hop
    rms = 20 * np.log10(np.sqrt(np.maximum(c[idx + w] - c[idx], 0.0) / w) + 1e-10)
    sil = rms < DEEP_SIL_DB
    npw = max(1, int(round(post / VALLEY_HOP)))
    out, i = [], 0
    while i < len(sil):
        if not sil[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(sil) and sil[j + 1]:
            j += 1
        s = t0 + i * hop / float(sr)
        e = t0 + (j * hop + w) / float(sr)
        nxt = sil[j + 1: j + 1 + npw]
        ratio = float(nxt.mean()) if len(nxt) else 0.0
        out.append((round(s, 3), round(e, 3), round(float(rms[i:j + 1].min()), 1), round(ratio, 3)))
        i = j + 1
    return out


def head_after_valley(ffmpeg, src, cand, onset, t_first, tmp_dir=None):
    """「假切入」剔除：候选起点之后若还隔着深静音谷，返回谷后的真伴奏起点。

    返回 ``(new_head, note)``；不适用时 ``(None, reason)``，调用方沿用原候选值。

    为什么不能只靠 find_head（2026-10-10 二次复盘，实测两首）
    ---------------------------------------------------------------
      · 昨日青空：find_head → 7575.02，但 7574.60~7576.75 实测是 −79dB 静音，
        真正的伴奏 7576.75 才起；其后 7580~7588 是说话，7588.55~7589.40 又一
        段 −87dB 静音，7589.40 伴奏**重来**并接到 7602.4 的起唱。
      · 晚婚：find_head → 6877.25，而 6892~6908 是「试唱一句 + 说话」，
        6907.25~6908.95（−93dB）+ 6909.35~6910.30（−81dB）两段深谷之后，
        6910.3 伴奏才正式起（前奏 18.8s ≈ 官方 17.95s）。

    两例的共同点：**真起点前必有一段 ≤ −55dB 的深静音谷**（播放器暂停/切轨），
    比最弱的轻声前奏（−34~−46dB）低 20dB 以上，故 −55dB 不会误伤。
    伪起点与真前奏在音频特征上同源（低平坦度 + 有节拍，score 都过 SCORE_T），
    find_head 又按设计取**最早**合格帧，因此必然切进伪起点。

    上界取 ``onset``（detect_entry 的「伴奏回升点」）：语义就是「不要早于紧邻
    回升点的那段静音谷」。``onset`` 缺失时退回 ``t_first``（第一句歌词时刻），
    此时晚婚这类「锚点被说话行带偏」的情况会搜不到谷 → 不改动，安全降级。
    """
    cand = float(cand)
    up = onset if (onset is not None and onset > cand) else float(t_first)
    if up - cand <= 1.0:
        return None, "候选起点距上界仅 %.2fs，无搜索空间" % (up - cand)
    vs = [v for v in deep_valleys(ffmpeg, src, cand, up, tmp_dir=tmp_dir)
          if v[3] < VALLEY_POST_MAX and abs(up - v[1]) <= VALLEY_NEAR]
    if not vs:
        return None, "未检出紧邻起点的静音谷"
    v = min(vs, key=lambda z: abs(up - z[1]))
    new = round(v[1] + HEAD_PAD, 3)
    return new, ("静音谷 %.2f~%.2f（谷底 %.0fdB）后伴奏起点 %.2f"
                 % (v[0], v[1], v[2], new))


def find_tail(prof, t_last, fwd=60.0):
    """从「末句歌词」向后找伴奏结束点。返回 (abs_time, note)。

    优先用音乐性判定；判不出（人声扰乱 beat）则回退到能量衰减判定。
    """
    if not prof:
        return None, "无特征数据"
    flags = _music_flags(prof)
    ts = [p[0] for p in prof]
    rms = [p[1] for p in prof]
    i2 = min(range(len(ts)), key=lambda i: abs(ts[i] - t_last))
    hi_i = min(len(ts) - 1,
               min(range(len(ts)), key=lambda i: abs(ts[i] - (t_last + fwd))))
    # ① 音乐性：末句之后最后一个「像音乐」的帧。
    #    扫描期间允许出现非音乐帧（正在唱时人声会压过 beat，前奏/尾奏的弱拍
    #    也常判不出），但只要**还有声音**（rms > QUIET_DB）就仍在同一首歌里；
    #    一旦跌进静音就停止——否则会把歌后说话、甚至下一首歌的伴奏当成尾奏
    #    （晚婚实测：末句 7167s，7191s 起是说话，7204s 是下一首的伴奏）。
    last_music = None
    for i in range(i2, hi_i + 1):
        if rms[i] <= QUIET_DB:
            break
        if flags[i]:
            last_music = i
    if last_music is not None:
        t = ts[last_music] + TAIL_KEEP
        return round(float(t), 3), "伴奏结束 %.2f（音乐性判定）+ %.1fs 余韵" % (
            ts[last_music], TAIL_KEEP)
    # ② 回退：能量相对演唱段衰减 SIL_MARGIN dB 并持续 SIL_HOLD 秒
    ref = np.median([p[1] for p in prof[max(0, i2 - 8): i2 + 1]]) if i2 else -20.0
    thr = ref - SIL_MARGIN
    hold = max(1, int(round(SIL_HOLD / HOP)))
    for i in range(i2, hi_i - hold + 1):
        if all(prof[i + k][1] < thr for k in range(hold)):
            t = ts[i] + TAIL_KEEP
            return round(float(t), 3), "静音起点 %.2f（%.0fdB 衰减）+ %.1fs 余韵" % (
                ts[i], SIL_MARGIN, TAIL_KEEP)
    return None, "尾部未检出伴奏结束点"
