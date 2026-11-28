# -*- coding: utf-8 -*-
"""验收：片头补静音(head_pad)期间画面必须冻结，进正片后必须开始走。

判据（不依赖日志，直接量成片）：
  · pad 内两帧（0.05s / 0.80s）—— 面板区域像素几乎相同（冻结）；
  · 跨过 pad（0.80s / 1.30s）—— 明显不同（开始走帧）。
只要 pad 内不冻结，说明 pad 期间照样在拉解码帧，整片画面会比音频提前 head_pad 秒。
"""
import os
import subprocess
import sys

import numpy as np
from PIL import Image

MP4 = sys.argv[1]
FF = os.environ.get("FFMPEG_BIN") or "ffmpeg"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(ROOT, "_chk", "padcheck")
os.makedirs(TMP, exist_ok=True)
# 只取人物面板内部（避开底部档案行、右缘溶解区、背景颗粒）
CROP = "crop=1600:1200:200:300"


def frame(t):
    p = os.path.join(TMP, "pad_%05.2f.png" % t)
    subprocess.run([FF, "-v", "error", "-ss", "%.3f" % t, "-i", MP4, "-frames:v", "1",
                    "-vf", CROP, "-y", p], check=True)
    return np.asarray(Image.open(p).convert("L"), dtype=np.float32)


def main():
    a, b, c, d = frame(0.05), frame(0.80), frame(1.30), frame(2.40)
    def diff(x, y):
        return float(np.abs(x - y).mean())
    print("面板区域平均灰度差（0~255）：")
    print("  pad 内 0.05s ↔ 0.80s : %6.3f" % diff(a, b))
    print("  跨 pad 0.80s ↔ 1.30s : %6.3f" % diff(b, c))
    print("  正片内 1.30s ↔ 2.40s : %6.3f" % diff(c, d))
    ok1 = diff(a, b) < 1.0
    ok2 = diff(b, c) > 2.0
    print("\n结论：pad 内冻结 %s；进正片开始走帧 %s"
          % ("✓" if ok1 else "✗", "✓" if ok2 else "✗"))
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
