# -*- coding: utf-8 -*-
"""量《如果有来生》头尾：0.1s 分辨率 RMS，找静音谷与伴奏收束点。"""
import subprocess, numpy as np

SRC = r"D:/录播/25788785-岁己SUI/录制-25788785-20261121-201653-985-陪你这个猪过周六！【新衣活动中_merged.mp4"
FF = r"D:/ffmpeg/bin/ffmpeg.exe"
A, B = 4860.0, 5160.0   # 01:21:00 ~ 01:26:00


def hms(t):
    return "%02d:%02d:%05.2f" % (int(t // 3600), int(t % 3600 // 60), t % 60)


cmd = [FF, "-v", "error", "-ss", str(A), "-t", str(B - A), "-i", SRC,
       "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
x = np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, dtype=np.int16).astype(np.float32) / 32768.
hop = 1600  # 0.1s
n = len(x) // hop
rms = np.array([np.sqrt(np.mean(x[i * hop:(i + 1) * hop] ** 2)) for i in range(n)])
db = 20 * np.log10(np.maximum(rms, 1e-10))

for i in range(n):
    t = A + i * 0.1
    print("%8.1f %s  %6.1f dB" % (t, hms(t), db[i]))
