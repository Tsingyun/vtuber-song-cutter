# -*- coding: utf-8 -*-
"""《小夜子》歌词时间轴重建（2026-10-08 后半段音画不同步）。

根因：现场伴奏比官方录音**慢约 3.9%**，而旧时间轴是「官方轴 + 常数偏移 5.49s」，
常数偏移无法吸收速度差 —— 开头晚、结尾早，越到后面越明显（末行差 5.06s，
正好落在用户指出的「2:10 之后」区间）。

解法：拿现场演唱的实际起唱时刻当锚点，拟合 live_u = a × official_t + b 后整体重标定。

⚠⚠ 锚点取值的两条纪律（本次踩坑总结，改别首歌也要遵守）：
1. **不能用 SRT 段首直接当锚点**。FunASR 的 VAD 会被进场伴奏/呼吸提前触发，
   实测提前量 0.3~2.4s 且不均匀（L15 提前 1.5s）。
2. **也不能一律用包络判读**。连唱段里没有静音谷，包络只能看到「一直在响」，
   此时应回到 SRT 的**语义分段**（段首文本 == 该行开头）。
   → 逐锚点判断：包络能分辨（有静音谷 / 有台阶）用包络；分辨不出用 SRT。
3. 判读时要核对「这一句到底是哪一行」——本次 L30 曾误把 L29「キヅカナイヨ」
   的能量回升当成 L30 起唱，导致残差从 0.39s 恶化到 0.74s。

结果：a=1.0394 b=+2.14（伴奏慢 3.94%），11 个锚点残差 max 0.84s / RMS 0.39s
      （上一版用 SRT 段首当锚点时 RMS 0.64s）。
"""
import io
import os
import re
import statistics

SRC_LRC = r"D:\岁己歌切\cuts\2026-10-06\【岁己SUI】小夜子【20261006歌切】.src.lrc"
OUT = r"D:\岁己歌切\_tmp\koyako_fixed2.lrc"
HEAD_PAD = 0.933          # 仅用于日志提示；管线已自动加，本文件写 cut 相对时间

official = []
for ln in io.open(SRC_LRC, encoding="utf-8").read().splitlines():
    m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", ln)
    if m:
        official.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))

# ── 锚点：(官方 t, 实测起唱 u[cut 相对], 依据) ──────────────────────────────
# u 全部换算到「cut 相对时间」= 成片时刻 − HEAD_PAD
A = [
    (22.85,  26.00, 'env'),   # L1  包络台阶 26.93（成片）；SRT 段首 25.08 被伴奏提前
    (52.27,  56.22, 'env'),   # L5  包络 57.15（成片），前有 −58dB 静音谷
    (66.58,  71.35, 'env'),   # L8  包络 72.28（成片），L7 收尾后换气
    (91.97,  97.37, 'env'),   # L11 包络 98.30（成片）
    (105.78, 112.25, 'env'),  # L13 包络 113.18（成片），前有 −31dB 谷
    (120.78, 128.06, 'env'),  # L15 包络 128.99（成片）；SRT 段首 126.56 提前 1.5s
    (144.97, 152.69, 'env'),  # L20 包络 153.62（成片）
    (183.82, 193.08, 'srt'),  # L26 连唱无谷，取 SRT 语义段首（E11）
    (189.32, 199.76, 'srt'),  # L27 同上（E12）
    (200.88, 211.06, 'srt'),  # L30 同上（E13）；⚠ 别误判成 209.3 ——那是 L29 在唱
    (216.47, 226.40, 'env'),  # L32 包络 227.33（成片）
]

n = len(A)
mt = sum(t for t, _, _ in A) / n
mu = sum(u for _, u, _ in A) / n
sxx = sum((t - mt) ** 2 for t, _, _ in A)
sxy = sum((t - mt) * (u - mu) for t, u, _ in A)
a = sxy / sxx
b = mu - a * mt
slopes = [(A[j][1] - A[i][1]) / (A[j][0] - A[i][0])
          for i in range(n) for j in range(i + 1, n)
          if abs(A[j][0] - A[i][0]) > 8]
a_ts = statistics.median(slopes)
b_ts = statistics.median([u - a_ts * t for t, u, _ in A])
res = [u - (a * t + b) for t, u, _ in A]

print("官方歌词 %d 行，首行 %.2fs，末行 %.2fs" % (len(official), official[0][0], official[-1][0]))
print("\n锚点 %d 个：" % n)
for (t, u, src) in A:
    print("   t=%7.2f → u=%7.2f  残差 %+6.2fs  [%s]" % (t, u, u - (a * t + b), src))
print("\n最小二乘 : a=%.4f b=%+.2f  （伴奏慢 %.2f%%）" % (a, b, (a - 1) * 100))
print("Theil-Sen: a=%.4f b=%+.2f  （%d 个两两斜率中位数）" % (a_ts, b_ts, len(slopes)))
print("残差     : max %.2fs  RMS %.2fs"
      % (max(abs(r) for r in res), (sum(r * r for r in res) / n) ** 0.5))
if abs(a - a_ts) > 0.004:
    print("⚠ OLS 与 Theil-Sen 斜率不一致（差 %.4f），请人工复核锚点" % abs(a - a_ts))

print("\n=== 新旧轴对照（cut 相对，成片 = 该值 + %.3f）===" % HEAD_PAD)
print("  %-34s %8s %8s %8s" % ("歌词", "旧轴", "新轴", "变化"))
for i, (t, txt) in enumerate(official, 1):
    old = t + 5.49
    new = a * t + b
    if i <= 3 or i >= 32 or abs(new - old) > 4:
        print("  L%-2d %-31s %8.2f %8.2f %+8.2f" % (i, txt[:31], old, new, new - old))


def fmt(sec):
    sec = max(0.0, sec)
    return "[%02d:%05.2f]" % (int(sec // 60), sec % 60)


os.makedirs(os.path.dirname(OUT), exist_ok=True)
with io.open(OUT, "w", encoding="utf-8", newline="") as fh:
    for t, txt in official:
        fh.write("%s%s\n" % (fmt(a * t + b), txt))
print("\n已写入 %s" % OUT)
print("成片首行 %.2fs（旧 %.2fs），末行 %.2fs（旧 %.2fs）——末行后移 %.2fs"
      % (a * official[0][0] + b + HEAD_PAD, official[0][0] + 5.49 + HEAD_PAD,
         a * official[-1][0] + b + HEAD_PAD, official[-1][0] + 5.49 + HEAD_PAD,
         (a * official[-1][0] + b) - (official[-1][0] + 5.49)))
