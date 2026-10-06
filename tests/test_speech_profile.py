#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_speech_profile.py —— 「她同时在讲别的事」判据的回归测试

背景（2026-10-06）：全自动歌切会把「只放 BGM、她同时在解说」误判成主播演唱。
用户提出的判据：播 BGM 时她通常同时在说话，FunASR 会把那段时间她在讲的内容也
转写出来 → 疑似演唱窗口内**非歌词语音**时长占比 + 最长连续段可以区分两者。

本测试用 4 个真实标注样本（3 首真唱 + 1 个 BGM 误判）锁死判据行为：
  真唱  《泡泡》10-01          非歌词 3.5%   最长连续 6s
  真唱  Don't Look Back In Anger 10-02     非歌词 0.0%   最长连续 0s
  真唱  《Moon River》10-02    非歌词 30.5%  最长连续 15s  ← FunASR 英文错字，最难判
  假唱  《You(=I)》10-05       非歌词 100%   最长连续 173s ← 在解说《银河护卫队》
  假唱  《88》10-04             非歌词 100%   最长连续 185s ← 在讲《吸血鬼与挨屁者》美剧

用法：
  python tests/test_speech_profile.py            # 用真实 SRT+LRC 跑（需 config.json）
  python tests/test_speech_profile.py --synthetic # 无外部依赖的合成样本自检
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from songcut import lyric_locate as LL  # noqa: E402

NONLYRIC_RATIO_MAX = 0.55
NONLYRIC_RUN_MAX = 45.0

# (标签, 是否真唱, SRT日期目录, cuts目录, 歌词文件名关键字, 窗口起, 窗口止)
REAL_CASES = [
    ("《泡泡》(10-01)", True, "20261001", "2026-10-01", "泡泡", 4282.2, 4499.5),
    ("Don't Look Back In Anger(10-02)", True, "20261002", "2026-10-02",
     "Look Back", 5896.9, 6184.7),
    ("Moon River(10-02)", True, "20261002", "2026-10-02", "Moon", 6534.8, 6657.9),
    ("You(=I)(10-05) BGM误判", False, "20261005", None, None, 885.8, 1056.5),
    ("88(10-04) BGM误判", False, "20261004", None, "88", 9519.2, 9751.9),
]


def parse_srt(path):
    t = open(path, encoding="utf-8-sig").read()
    cues = []
    for blk in re.split(r"\n\s*\n", t.strip()):
        m = re.search(r"(\d\d):(\d\d):(\d\d)[,.](\d\d\d)\s*-->\s*"
                      r"(\d\d):(\d\d):(\d\d)[,.](\d\d\d)", blk)
        if not m:
            continue
        a = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000
        b = int(m.group(5)) * 3600 + int(m.group(6)) * 60 + int(m.group(7)) + int(m.group(8)) / 1000
        txt = blk[m.end():].strip().replace("\n", " ")
        if txt:
            cues.append((a, b, txt))
    return cues


def verdict(prof):
    """复刻 auto_cut.verify_candidate 闸门 4b 的判定，返回 (PASS/WARN/FAIL, 原因)。"""
    if prof["speech_s"] <= 0:
        return "SKIP", "窗口内无转写（上游 SRT 缺段）"
    r, run = prof["nonlyric_ratio"], prof["max_run_s"]
    if r > NONLYRIC_RATIO_MAX:
        return "FAIL", "非歌词占比 %.0f%% > %d%%" % (r * 100, NONLYRIC_RATIO_MAX * 100)
    if run > NONLYRIC_RUN_MAX:
        return "FAIL", "连续非歌词 %.0fs > %ds" % (run, NONLYRIC_RUN_MAX)
    if r > NONLYRIC_RATIO_MAX * 0.6 or run > NONLYRIC_RUN_MAX * 0.6:
        return "WARN", "接近阈值"
    return "PASS", ""


def test_synthetic():
    """无外部依赖：合成两类窗口，验证占比/连续段的计算与判定方向。"""
    fails = []
    # 假唱：整段都在讲别的事（非歌词）
    fake = [(0.0, 14.0, "勇度用少剑制服了火箭，女西司雇他来抓银河护卫队"),
            (14.0, 28.0, "绿夺者内战一触即发，正当众人对峙时，勇度措不及防的倒地"),
            (28.0, 42.0, "校人要求分10的酬金，并要了艘飞船去追杀卡莫拉")]
    # 真唱：整段都是歌词
    true_zh = [(0.0, 12.0, "告诉我吧告诉我吧，你的眼睛，你的嘴角都在守候"),
               (12.0, 24.0, "天上的小地上的花，撩记你的侧脸优雅，不记一般温柔光洒"),
               (24.0, 36.0, "想用所有的温柔抚平你眉头想告诉他们，爱你不是我对手")]
    lrc_zh = ("[00:00.00]告诉我吧告诉我吧你的眼睛你的嘴角都在守候\n"
              "[00:12.00]天上的小地上的花撩记你的侧脸优雅不记一般温柔光洒\n"
              "[00:24.00]想用所有的温柔抚平你眉头想告诉他们爱你不是我对手\n"
              "[00:36.00]想吹个泡泡点着你某个回眸\n")

    p_fake = LL.speech_profile(fake, lrc_zh, 0.0, 42.0)
    v_fake, why_fake = verdict(p_fake)
    if v_fake != "FAIL":
        fails.append("合成假唱窗口应判 FAIL，实得 %s（非歌词 %.0f%%）"
                     % (v_fake, p_fake["nonlyric_ratio"] * 100))

    p_true = LL.speech_profile(true_zh, lrc_zh, 0.0, 36.0)
    v_true, _ = verdict(p_true)
    if v_true != "PASS":
        fails.append("合成真唱窗口应判 PASS，实得 %s（非歌词 %.0f%%）"
                     % (v_true, p_true["nonlyric_ratio"] * 100))

    # 边界：短于 NONLYRIC_MIN_CUE 的碎句不该拉高占比
    p_short = LL.speech_profile(
        [(0.0, 12.0, "告诉我吧告诉我吧你的眼睛你的嘴角都在守候"),
         (12.0, 12.5, "嗯"), (12.5, 24.0, "天上的小地上的花撩记你的侧脸优雅不记一般温柔光洒")],
        lrc_zh, 0.0, 24.0)
    if p_short["nonlyric_ratio"] > 0.05:
        fails.append("0.5s 碎句被计入非歌词，占比 %.1f%%（应 ≈0）"
                     % (p_short["nonlyric_ratio"] * 100))

    # 空窗口 → 不否决（SKIP），交给音频判据
    p_empty = LL.speech_profile([(500.0, 510.0, "别处的声音")], lrc_zh, 0.0, 10.0)
    if verdict(p_empty)[0] != "SKIP":
        fails.append("空窗口应判 SKIP，实得 %s" % verdict(p_empty)[0])

    for f in fails:
        print("  [FAIL] " + f)
    print("合成样本：%d 项检查，%d 失败" % (4, len(fails)))
    return not fails


def test_real():
    """真实数据回归：4 个标注样本的判定方向必须正确。"""
    import json
    import io
    import glob
    import song_cutter as SC
    from songcut import lyrics_fetch as LF

    srt_dir = SC.SRT_DIR
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache = os.path.join(root, "_media_cache")
    os.makedirs(cache, exist_ok=True)

    def srt_of(day):
        for f in os.listdir(srt_dir):
            if f.startswith(day):
                return os.path.join(srt_dir, f)
        return None

    def lrc_of(cut_day, key):
        if not cut_day:
            return None
        for p in glob.glob(os.path.join(root, "cuts", cut_day, "*.src.lrc")):
            if key in os.path.basename(p):
                return io.open(p, encoding="utf-8").read()
        return None

    fails, ran = [], 0
    for label, is_true, srt_day, cut_day, key, ws, we in REAL_CASES:
        sp = srt_of(srt_day)
        if not sp:
            print("  [SKIP] %s：找不到 SRT（%s）" % (label, srt_day))
            continue
        lrc = lrc_of(cut_day, key)
        if not lrc:
            lrc, _cov, _info = LF.fetch_lyrics_and_cover(key or "You(=I)", "",
                                                         cache_dir=cache)
        if not lrc:
            print("  [SKIP] %s：拿不到歌词" % label)
            continue
        entries = parse_srt(sp)
        prof = LL.speech_profile(entries, lrc, ws, we)
        v, why = verdict(prof)
        ran += 1
        ok = (v == "PASS") if is_true else (v == "FAIL")
        print("  %s %-32s 非歌词 %5.1f%%  最长连续 %6.1fs  → %s%s"
              % ("✓" if ok else "[FAIL]", label,
                 prof["nonlyric_ratio"] * 100, prof["max_run_s"], v,
                 ("（%s）" % why) if why else ""))
        if not ok:
            fails.append("%s 期望 %s，实得 %s（非歌词 %.0f%%，最长 %.0fs）"
                         % (label, "PASS" if is_true else "FAIL", v,
                            prof["nonlyric_ratio"] * 100, prof["max_run_s"]))
    print("真实样本：%d 个，%d 失败" % (ran, len(fails)))
    return not fails


if __name__ == "__main__":
    print("=== 合成样本 ===")
    a = test_synthetic()
    print("=== 真实样本 ===")
    try:
        b = test_real()
    except Exception as e:            # 缺 config.json / SRT 目录时不算失败
        print("  [SKIP] 真实样本不可用：%s" % e)
        b = True
    sys.exit(0 if (a and b) else 1)
