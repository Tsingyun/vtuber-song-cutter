# -*- coding: utf-8 -*-
"""「假切入」剔除回归（2026-10-10《晚婚》+《昨日青空》双案例）。

事故形态
--------
主播常「先放几秒伴奏 / 先试唱一句，说一句话，再重新正式起伴奏」。伪起点与
真前奏在音频特征上**同源**（都低平坦度 + 有节拍，`score = beat - 0.5*flat`
都过 `SCORE_T = 0.15`），而 `find_head()` 按设计取窗口内**最早**的合格音乐帧
（那是为了救回被说话打断的连续前奏），于是必然切进伪起点：

  · 晚婚    ：find_head → 6877.25，实际 6892~6908 是「试唱 + 说话」，
              6907.25~6908.95（-92.7dB）+ 6909.35~6910.30（-80.6dB）两段硬静音
              之后，6910.30 伴奏才正式重入（前奏 18.8s ≈ 官方 17.95s）。
              → 片头多吞 33.0s 闲聊。
  · 昨日青空：find_head → 7575.02，实际 7580.4~7588.1 是说话，
              7588.55~7589.40（-87.2dB）硬静音之后 7589.40 伴奏重来。
              → 片头多吞 14.4s。

判据
----
真起点前必有一段 **≤ -55dB 的深静音谷**（播放器暂停 / 切轨留下的空档，
实测比本场本底噪声 -77dB 还低），且该谷紧邻 `detect_entry` 的回升点。
阈值 -55dB 安全：实测最弱的轻声前奏也有 -46dB，不会误伤。

本测试用**合成音频**复现「说话 → 硬静音 → 伪伴奏 → 说话 → 硬静音 → 真伴奏」
结构，断言 `head_after_valley()` 把起点从伪伴奏挪到真伴奏，并覆盖三条降级路径。
"""
import os
import shutil
import struct
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from songcut import config as CFG  # noqa: E402
from songcut import music_bounds as MB  # noqa: E402

FAILS = []
SR = 16000


def check(name, got, want, tol=0.0):
    """tol=0 → 精确比对；tol>0 → 数值带容差。"""
    if tol and isinstance(got, (int, float)) and isinstance(want, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if ok:
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s -> got %r, want %r%s" % (name, got, want, " ±%s" % tol if tol else ""))


def _noise(n, amp, seed):
    rng = np.random.default_rng(seed)
    return (rng.random(n) * 2.0 - 1.0) * amp


def _music(n, amp, seed):
    """低平坦度 + 有节拍 → 会被判为「音乐」。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / float(SR)
    x = 0.55 * np.sin(2 * np.pi * 220.0 * t) + 0.30 * np.sin(2 * np.pi * 440.0 * t)
    x = x * (0.55 + 0.45 * np.sin(2 * np.pi * 2.0 * t))          # 2Hz 节拍包络
    x = x + 0.05 * (rng.random(n) * 2.0 - 1.0)
    return x * (amp / max(1e-9, np.abs(x).max()))


def build_wav(path):
    """0-4 说话 / 4-5.5 硬静音 / 5.5-7.5 伪伴奏 / 7.5-10 说话 /
    10-11.5 硬静音 / 11.5-14.5 真伴奏 / 14.5-16.5 轻声(≈-42dB) / 16.5-20 说话"""
    segs = [
        (4.0, _noise(int(4.0 * SR), 0.12, 1)),
        (1.5, np.zeros(int(1.5 * SR))),                 # 谷 A
        (2.0, _music(int(2.0 * SR), 0.12, 2)),          # 伪起点
        (2.5, _noise(int(2.5 * SR), 0.12, 3)),
        (1.5, np.zeros(int(1.5 * SR))),                 # 谷 B（紧邻真起点）
        (3.0, _music(int(3.0 * SR), 0.12, 4)),          # 真伴奏
        (2.0, _music(int(2.0 * SR), 0.008, 5)),         # 轻声段（-42dB，不是谷）
        (3.5, _noise(int(3.5 * SR), 0.12, 6)),
    ]
    x = np.concatenate([s for _, s in segs])
    pcm = np.clip(x * 32767.0, -32768, 32767).astype("<i2")
    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
    w.close()
    return path


FF = CFG.ffmpeg()
if not (shutil.which(FF) or os.path.exists(FF)):
    print("ffmpeg 不可用（%r）→ 跳过本测试" % FF)
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="fv_")
try:
    wav = build_wav(os.path.join(tmp, "synth.wav"))
    print("合成音频: %s（%.1fs）" % (wav, 20.0))

    # ---------- ① deep_valleys 只认硬静音，不把轻声段当谷 ----------
    print("[1] deep_valleys 静音谷识别")
    vs = MB.deep_valleys(FF, wav, 0.0, 20.0, tmp_dir=tmp)
    print("     检出: %s" % (vs,))
    check("谷数量（轻声段不算谷）", len(vs), 2)
    if len(vs) == 2:
        check("谷 A 起点", vs[0][0], 4.0, 0.15)
        check("谷 A 终点", vs[0][1], 5.5, 0.15)
        check("谷 B 起点", vs[1][0], 10.0, 0.15)
        check("谷 B 终点", vs[1][1], 11.5, 0.15)
        check("谷底 ≤ -55dB", all(v[2] <= MB.DEEP_SIL_DB for v in vs), True)
        check("谷后立刻有伴奏 → post_sil 低", all(v[3] < MB.VALLEY_POST_MAX for v in vs), True)

    # ---------- ② 假切入剔除：从伪伴奏挪到真伴奏 ----------
    print("[2] head_after_valley 剔除伪起点")
    new, note = MB.head_after_valley(FF, wav, 5.6, 11.5, 11.5, tmp_dir=tmp)
    print("     cand=5.6 onset=11.5 → %r（%s）" % (new, note))
    check("伪起点 5.6 → 真起点 11.75", new, 11.75, 0.15)

    new2, _ = MB.head_after_valley(FF, wav, 2.0, 11.5, 11.5, tmp_dir=tmp)
    check("cand=2.0（更早的伪段）同样落到 11.75", new2, 11.75, 0.15)

    cm = MB.last_deep_valley(FF, wav, 0.0, 20.0, tmp_dir=tmp)
    check("兼容壳 last_deep_valley → 11.5", cm, 11.5, 0.15)

    # ---------- ③ 三条降级路径 ----------
    print("[3] 降级路径")
    n3, r3 = MB.head_after_valley(FF, wav, 11.0, 11.5, 11.5, tmp_dir=tmp)
    check("搜索空间不足 → None", n3, None)

    n4, r4 = MB.head_after_valley(FF, wav, 6.0, 8.0, 8.0, tmp_dir=tmp)
    check("区间内无静音谷 → None", n4, None)

    n5, r5 = MB.head_after_valley(FF, wav, 2.0, None, 5.6, tmp_dir=tmp)
    print("     onset 缺失退回 t_first=5.6 → %r（%s）" % (n5, r5))
    check("onset 缺失时用 t_first 兜底", n5, 5.75, 0.15)

    # ---------- ④ 阈值纪律：-55dB 不能抬高 ----------
    print("[4] 阈值纪律")
    check("DEEP_SIL_DB == -55.0（抬高会误伤轻声前奏）", MB.DEEP_SIL_DB, -55.0)
    check("VALLEY_NEAR == 3.0", MB.VALLEY_NEAR, 3.0)
    check("VALLEY_POST_MAX == 0.5", MB.VALLEY_POST_MAX, 0.5)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILS:
    print("失败 %d 项：%s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("全部通过 ✓")
