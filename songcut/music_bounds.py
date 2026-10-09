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
