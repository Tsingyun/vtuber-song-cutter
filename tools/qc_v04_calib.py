# -*- coding: utf-8 -*-
"""V04「冻结窗口」判据的标定与双向验证。

判据（songcut/qc.py）：
    frac(MAD < freeze_mad) >= freeze_frac  →  该窗判冻结
    freeze_mad = 0.002, freeze_frac = 0.70, freeze_block_ratio = 0.34

为什么需要这个工具：V04 曾把「静止纸纹镜头」（帧间 MAD 中位 0.007）误判成
「卡死」（同帧复制，MAD 中位 0.0002）。两者**用最大差异分不开**
（真卡死量化跳变可达 0.0056 > 静止镜头最低 0.0033），只能靠分布形态。
任何一次阈值改动都必须在这里跑双向验证，否则等于给自己发免罪金牌。

用法：
    # ① 复现成片上某些窗口（帧号来自 QC 报错）
    python tools/qc_v04_calib.py --mp4 成片.mp4 --frames 726 7285 13716

    # ② 标定：从成片抽帧构造「同帧复制」样本，验证真卡死仍能检出
    python tools/qc_v04_calib.py --mp4 成片.mp4 --calib 11.8 121.1 228.3

    # ③ 传阈值做敏感性分析
    python tools/qc_v04_calib.py --mp4 成片.mp4 --frames 726 --mad 0.002 --frac 0.70
"""
import argparse
import os
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from songcut.qc import TH  # noqa: E402

FFMPEG = r"D:\ffmpeg\bin\ffmpeg.exe"
W, H, WIN = 192, 108, 10


def grab(path, ss, n=WIN):
    p = subprocess.run([FFMPEG, "-v", "error", "-ss", "%.3f" % ss, "-i", path,
                        "-frames:v", str(n), "-vf", "scale=%d:%d:flags=area" % (W, H),
                        "-pix_fmt", "gray", "-f", "rawvideo", "-"], capture_output=True)
    m = len(p.stdout) // (W * H)
    if m < 2:
        return None
    return np.frombuffer(p.stdout[: m * W * H], dtype=np.uint8).reshape(m, H, W).astype(np.float32)


def assess(arr, mad, frac):
    d = np.abs(np.diff(arr, axis=0)).mean(axis=(1, 2))
    f = float((d < mad).mean())
    return {"mad_vals": d, "frac": f, "median": float(np.median(d)), "frozen": f >= frac}


def make_freeze_sample(mp4, ss, crf=20, tmpdir=None):
    """抽一帧，复制成 60 帧，按成片同参数编码 → 已知「卡死」样本。"""
    tmpdir = tmpdir or tempfile.mkdtemp(prefix="v04calib_")
    png = os.path.join(tmpdir, "f_%s.png" % ss)
    out = os.path.join(tmpdir, "freeze_%s_crf%d.mp4" % (ss, crf))
    subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", "%.3f" % ss, "-i", mp4,
                    "-frames:v", "1", png], capture_output=True)
    if not os.path.exists(png):
        return None
    subprocess.run([FFMPEG, "-y", "-v", "error", "-loop", "1", "-i", png, "-t", "1", "-r", "60",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                    "-pix_fmt", "yuv420p", out], capture_output=True)
    return out if os.path.exists(out) else None


def main():
    ap = argparse.ArgumentParser(description="V04 冻结判据标定 / 双向验证")
    ap.add_argument("--mp4", required=True)
    ap.add_argument("--frames", nargs="*", type=int, help="按帧号复现窗口")
    ap.add_argument("--times", nargs="*", type=float, help="按秒数复现窗口")
    ap.add_argument("--calib", nargs="+", type=float, metavar="T", help="在这些秒处造卡死样本")
    ap.add_argument("--mad", type=float, default=TH["freeze_mad"])
    ap.add_argument("--frac", type=float, default=TH["freeze_frac"])
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    print("判据：frac(MAD < %.4f) >= %.2f  → 冻结   [freeze_block_ratio=%.2f]"
          % (a.mad, a.frac, TH["freeze_block_ratio"]))

    probes = [(f, f / 60.0) for f in (a.frames or [])]
    probes += [(None, t) for t in (a.times or [])]

    if probes:
        print("\n=== 成片窗口（期望：不判冻结）===")
        bad = 0
        for f, t in probes:
            arr = grab(a.mp4, max(0.0, t - 0.3))
            if arr is None:
                print("  f%-7s t=%.2fs  抽帧失败" % (f if f else "-", t))
                continue
            r = assess(arr, a.mad, a.frac)
            tag = "误报 ✗" if r["frozen"] else "正常 ✓"
            bad += int(r["frozen"])
            print("  f%-7s t=%7.2fs  frac=%.2f  med=%.4f  → %s"
                  % (f if f else "-", t, r["frac"], r["median"], tag))
            if a.verbose:
                print("            MAD: " + " ".join("%.4f" % v for v in r["mad_vals"]))
        print("  误报 %d/%d" % (bad, len(probes)))

    if a.calib:
        print("\n=== 构造卡死样本（期望：全部检出）===")
        hit = 0
        for t in a.calib:
            smp = make_freeze_sample(a.mp4, t, a.crf)
            if not smp:
                print("  t=%.2fs  构造失败" % t)
                continue
            arr = grab(smp, 0.0)
            r = assess(arr, a.mad, a.frac)
            ok = r["frozen"]
            hit += int(ok)
            print("  t=%7.2fs  frac=%.2f  med=%.4f  → %s"
                  % (t, r["frac"], r["median"], "检出 ✓" if ok else "漏检 ✗"))
        print("  检出 %d/%d" % (hit, len(a.calib)))
        if hit < len(a.calib):
            sys.exit(1)


if __name__ == "__main__":
    main()
