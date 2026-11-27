# -*- coding: utf-8 -*-
"""片头体检：切点有没有吃到「上一句说话 / 上一首的尾音」。

背景（2026-11-27《五块钱的伞》事故）：
    cut_start=1202.0 看着像「音乐从这里进」，实际 1202.0~1202.87 是她说完话的
    混响尾音（-33 → -66 dB，200~2k 占 98%），鼓点 1202.91 才进。
    成片第一秒因此多出一段人声残响。
    只看 0.1s 总电平是看不出「人声 vs 鼓」的，必须看**频段构成**：
    底鼓/贝斯 98% 能量 <200Hz，说话 95%+ 落在 200~2000Hz。

用法：
    # 1) 切之前体检源码切点（推荐）
    python tools/head_onset_check.py --src "D:/录播/x.mp4" --cut-start 1202.0

    # 2) 交付前体检成片片头（含 1s 补静音也能自动跳过）
    python tools/head_onset_check.py --mp4 "cuts/2026-11-26/xxx.mp4"

判据：
    · 乐器 onset = 切点之后连续 3 帧（75ms）low(<200Hz) 占比 ≥0.45 且 ≥-46dB 的起点；
      若始终没有（弦乐/pad 前奏）则退化为「第一个 ≥-40dB 的帧」并提示。
    · onset 之前若存在 ≥0.10s 的 mid(200~2000Hz) 占比 ≥0.60 且 ≥-46dB 的帧
      → 判「人声/残响残留」，退出码 2（切点偏早）。
退出码：0 干净 / 2 有残留 / 3 找不到 onset。
"""
import argparse
import math
import os
import struct
import subprocess
import sys
import tempfile
import wave

FFMPEG = os.environ.get("FFMPEG_BIN", "D:/ffmpeg/bin/ffmpeg.exe")
SR = 16000
FRAME = 0.025
LOW_CUT, MID_HI = 200.0, 2000.0
ONSET_DB, ONSET_LOW, ONSET_RUN = -46.0, 0.45, 3
SPEECH_DB, SPEECH_MID = -46.0, 0.60


def _extract(src, start, dur, out):
    cmd = [FFMPEG, "-v", "error", "-y"]
    if start is not None:
        cmd += ["-ss", "%.3f" % start]
    cmd += ["-t", "%.3f" % dur, "-i", src, "-ac", "1", "-ar", str(SR), "-f", "wav", out]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise SystemExit("ffmpeg 抽取失败：%s" % r.stderr.decode("utf-8", "replace")[-300:])


def _load(path):
    import numpy as np
    w = wave.open(path)
    n = w.getnframes()
    a = np.array(struct.unpack("<%dh" % n, w.readframes(n)), dtype=np.float32) / 32768.0
    w.close()
    return a


def analyse(x, offset=0.0):
    import numpy as np
    win = int(FRAME * SR)
    rows = []
    for i in range(0, len(x) - win, win):
        seg = x[i:i + win]
        sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
        f = np.fft.rfftfreq(len(seg), 1.0 / SR)
        tot = float(sp.sum()) + 1e-20
        low = float(sp[f < LOW_CUT].sum()) / tot
        mid = float(sp[(f >= LOW_CUT) & (f < MID_HI)].sum()) / tot
        rms = float((seg ** 2).mean()) ** 0.5
        db = 20.0 * math.log10(rms) if rms > 0 else -200.0
        rows.append((offset + i / SR, db, low, mid))
    return rows


def find_onset(rows, start_t):
    run = 0
    for r in rows:
        if r[0] < start_t - 1e-6:
            continue
        if r[1] >= ONSET_DB and r[2] >= ONSET_LOW:
            run += 1
            if run >= ONSET_RUN:
                return r[0] - (ONSET_RUN - 1) * FRAME, "percussive"
        else:
            run = 0
    for r in rows:
        if r[0] >= start_t - 1e-6 and r[1] >= -40.0:
            return r[0], "fallback"
    return None, "none"


def report(rows, label, start_t):
    onset, how = find_onset(rows, start_t)
    print("== %s ==" % label)
    if onset is None:
        print("  前 %.1fs 内没有任何 ≥-40dB 的内容 → 切点可能是空的" % (rows[-1][0] if rows else 0))
        return 3
    if how == "fallback":
        print("  未检出低频打击 onset（前奏可能是弦乐/pad），退化取第一个 ≥-40dB 帧：%.2fs" % onset)
    else:
        row = min(rows, key=lambda r: abs(r[0] - onset))
        print("  乐器 onset：%.2fs（low %.2f / %.1f dB）" % (onset, row[2], row[1]))
    print("  onset 距切点 %.2fs" % (onset - start_t))
    bad = [r for r in rows
           if start_t - 1e-6 <= r[0] < onset - 1e-6 and r[1] >= SPEECH_DB and r[3] >= SPEECH_MID]
    if len(bad) * FRAME >= 0.10:
        print("  x 切点后 %.2fs 是人声/残响残留（mid 占比高、电平不低）：" % (len(bad) * FRAME))
        head, tail = bad[:6], (bad[-4:] if len(bad) > 10 else [])
        for r in head:
            print("      %8.2fs  %6.1f dB  low %.2f  mid %.2f" % r)
        if tail:
            print("      ...")
            for r in tail:
                print("      %8.2fs  %6.1f dB  low %.2f  mid %.2f" % r)
        print("  -> 切点后移到 %.2fs（onset 前 0.03s）" % (onset - 0.03))
        return 2
    print("  ok onset 之前无人声，片头干净")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="")
    ap.add_argument("--cut-start", type=float, default=None)
    ap.add_argument("--mp4", default="")
    ap.add_argument("--lead", type=float, default=2.0, help="src 模式回看秒数（默认 2s）")
    ap.add_argument("--span", type=float, default=8.0, help="切点之后分析秒数")
    a = ap.parse_args()

    tmp = os.path.join(tempfile.gettempdir(), "_head_onset_check.wav")
    if a.mp4:
        _extract(a.mp4, None, a.span, tmp)
        rows = analyse(_load(tmp), 0.0)
        rc = report(rows, "成片 %s" % os.path.basename(a.mp4), 0.0)
    elif a.src and a.cut_start is not None:
        start = max(0.0, a.cut_start - a.lead)
        _extract(a.src, start, a.lead + a.span, tmp)
        rows = analyse(_load(tmp), start)
        rc = report(rows, "%s @ cut_start=%.2f" % (os.path.basename(a.src), a.cut_start), a.cut_start)
    else:
        raise SystemExit("要么给 --mp4，要么给 --src 与 --cut-start")
    try:
        os.remove(tmp)
    except OSError:
        pass
    sys.exit(rc)


if __name__ == "__main__":
    main()
