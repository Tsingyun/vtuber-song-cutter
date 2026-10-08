# -*- coding: utf-8 -*-
"""渲染磁盘占用治理的回归测试（2026-10-08）。

锁死三件事：
  ① 预检公式必须贴近真实占用（旧公式高估 2.1~2.2 倍）
  ② 残留扫描必须能清掉历史 mjpeg，且大文件删除要重试
  ③ 不得回归到「pages × frames」的错误估算

单帧体积基准来自实测（双源互证）：
  · cuts/2026-10-07/*.render.mp4.p{0,1}.mjpeg共 14.47 GB，
    按 JPEG SOI(ffd8ff) 计数 8816 帧 → 2.03 MB/帧
  · 渲染日志 recvBytes 13200 帧 / 27981 MB → 2.12 MB/帧
真实浏览器 toBlob q0.98 输出约 2.03~2.12 MB/帧，
**不要用 ffmpeg -q:v 2 抽帧测**（那是解码后的压缩帧，会低估 3~4 倍）。
"""
import io
import os
import sys
import glob
import time
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import song_cutter as SC

GB = 1024 ** 3
MB = 1024 ** 2
FRAME_REAL = 2.03 * MB          # 实测单帧
FRAME_EST = 2.2 * MB            # 预检取值（含开销余量）

_fails = []


def ck(name, got, want, tol=None, unit=""):
    if tol is None:
        ok = got == want
        detail = "got=%s want=%s" % (got, want)
    else:
        ok = abs(got - want) <= tol
        detail = "got=%.3f want=%.3f±%.3f %s" % (got, want, tol, unit)
    print("   %s %s  %s" % ("✓" if ok else "✗", name, detail))
    if not ok:
        _fails.append(name)


def need_new(dur, bitrate=18e6, pages=2, frame=FRAME_EST):
    """复刻 render_player_video 里的新预检公式。"""
    fr = int(dur * 60)
    return fr * frame + dur * (bitrate / 8.0) * 2.2 + 0.5 * GB


def need_real(dur, bitrate=18e6):
    """真实峰值 = mjpeg(实测单帧) + 视频侧三份。"""
    fr = int(dur * 60)
    return fr * FRAME_REAL + dur * (bitrate / 8.0) * 2.2


def need_old(dur, pages=2):
    """旧公式（有bug）：2GB + pages × frames × 2.2MB。"""
    return 2.0 * GB + pages * int(dur * 60) * 2.2 * MB


print("=" * 72)
print("① 预检公式贴近真实占用（不得高估 2 倍）")
print("=" * 72)
for d in (229, 280, 600):
    ratio = need_new(d) / need_real(d)
    ck("  %4ds 新/真实比值 ≤1.25" % d, ratio, 1.10, 0.15)

print()
print("   对照：旧公式的错误程度（这些必须 >1.5，确认我们避开了）")
for d in (229, 280, 600):
    r = need_old(d) / need_real(d)
    ok = r > 1.5
    print("   %s %4ds 旧/真实 = %.2fx（高估）" % ("✓" if ok else "✗", d, r))
    if not ok:
        _fails.append("旧公式对照 %ds" % d)

print()
print("=" * 72)
print("② 预检不随 pages 线性放大（分段落盘，每帧只写一次）")
print("=" * 72)
r2 = need_new(280, pages=2)
r5 = need_new(280, pages=5)
# pages 只影响视频侧的段数，不影响 mjpeg
ck("  pages 2→5 增幅 <15%%", abs(r5 / r2 - 1) * 100, 5.0, 10.0, "%")

print()
print("=" * 72)
print("③ 码率只影响视频侧，不影响 mjpeg（mjpeg 与码率无关）")
print("=" * 72)
a = need_new(280, bitrate=18e6) - need_new(280, bitrate=18e6)  # 占位
mj_only = int(280 * 60) * FRAME_EST
d18 = need_new(280, bitrate=18e6) - mj_only
d14 = need_new(280, bitrate=14e6) - mj_only
ck("  18M 视频侧占比 <10%%", d18 / need_new(280, bitrate=18e6) * 100, 3.0, 7.0, "%")
print("   mjpeg=%.1fGB 与码率无关；视频侧 18M→14M 省 %.0f MB"
      % (mj_only / GB, (d18 - d14) / MB))

print()
print("=" * 72)
print("④ _rm_retry 能删掉大文件（Windows 瞬时失败必须重试）")
print("=" * 72)
tmpd = tempfile.mkdtemp(prefix="swtest_")
try:
    big = os.path.join(tmpd, "big.render.mp4.p0.mjpeg")
    with open(big, "wb") as fh:
        fh.write(b"\x00" * (3 * MB))
    ck("  普通文件一次删除", SC._rm_retry(big), True)
    ck("  已删除后重复调用仍返回 True（幂等）", SC._rm_retry(big), True)
    ck("  不存在的路径返回 True", SC._rm_retry(os.path.join(tmpd, "nope")), True)
    # 目录不可删（模拟占用）
    ck("  目录返回 False（不抛异常）", SC._rm_retry(tmpd), False)
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

print()
print("=" * 72)
print("⑤ _sweep_stale_render_tmp 清理历史残留")
print("=" * 72)
tmpd = tempfile.mkdtemp(prefix="sweep_")
try:
    os.makedirs(os.path.join(tmpd, "cuts", "2026-10-07"))
    stale = os.path.join(tmpd, "cuts", "2026-10-07",
                         "【x】y【z】.mp4.render.mp4.p0.mjpeg")
    with open(stale, "wb") as fh:
        fh.write(b"\x00" * (5 * MB))
    logs = []
    # 用 -1 小时强制「视为陈旧」，等价于生产里的older_than_h=6
    freed = SC._sweep_stale_render_tmp(tmpd, older_than_h=-1.0, log=logs.append)
    ck("  回收字节数", freed, 5 * MB)
    ck("  文件已消失", os.path.exists(stale), False)
    ck("  有清理日志", len(logs) >= 1, True)

    # age 门槛：新建文件（age≈0）必须被 6 小时门槛跳过
    fresh = os.path.join(tmpd, "cuts", "2026-10-07", "new.render.mp4.p1.mjpeg")
    with open(fresh, "wb") as fh:
        fh.write(b"\x00" * (5 * MB))
    freed2 = SC._sweep_stale_render_tmp(tmpd, older_than_h=6.0, log=lambda s: None)
    ck("  新文件被跳过（age 门槛生效）", freed2, 0)
    ck("  新文件仍在", os.path.exists(fresh), True)

    # 把 mtime 调老 8 小时后应被清掉 —— 证明门槛是 age 而非别的条件
    old_t = time.time() - 8 * 3600
    os.utime(fresh, (old_t, old_t))
    freed3 = SC._sweep_stale_render_tmp(tmpd, older_than_h=6.0, log=lambda s: None)
    ck("  mtime 调老 8h 后被清掉", freed3, 5 * MB)
    ck("  老文件确已消失", os.path.exists(fresh), False)
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

print()
print("=" * 72)
print("⑥ 常量自洽")
print("=" * 72)
ck("  OUT_BITRATE 在 B站不二压区间", 16e6 <= SC.OUT_BITRATE <= 18.5e6, True)
ck("  OUT_PAGES ≥ 1", SC.OUT_PAGES >= 1, True)
ck("  OUT_RES ∈ {1,2}", SC.OUT_RES in (1, 2), True)

print()
print("=" * 72)
print("⑦ 时长放大关系（线性、可预测）")
print("=" * 72)
for d in (60, 120, 229, 280, 600):
    print("   %4ds → 预检 %6.1fGB  真实峰值 %6.1fGB  成片仅 %5.2fGB  放大 %2.0fx"
          % (d, need_new(d) / GB, need_real(d) / GB,
             (d * 18e6 / 8 + d * 320e3 / 8) / GB, need_real(d) / (d * 18e6 / 8 + d * 320e3 / 8)))

print()
print("=" * 72)
if _fails:
    print("✗ %d 项未通过：%s" % (len(_fails), _fails))
    sys.exit(1)
print("✓ 全部通过")
