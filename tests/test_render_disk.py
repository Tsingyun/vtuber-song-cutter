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
print()
print("=" * 72)
print("⑧ CLI 优先级：显式 --pages > --auto-pages > 默认 P=2")
print("=" * 72)
ck("  都不给 → 默认 P=2 且不开自动", SC.resolve_pages(None, False), (2, False))
ck("  --pages 1 → (1, False)", SC.resolve_pages(1, False), (1, False))
ck("  --pages 2 → (2, False)", SC.resolve_pages(2, False), (2, False))
ck("  --auto-pages → (2, True)", SC.resolve_pages(None, True), (2, True))
ck("  --pages 1 + --auto-pages → 服从 --pages", SC.resolve_pages(1, True), (1, False))

print()
print("=" * 72)
print("⑨ P=1 / P=2 空间估算（P=1 帧不落盘，只有两份视频流）")
print("=" * 72)
p2 = SC.estimate_render_bytes(280, 18e6, 2)
p1 = SC.estimate_render_bytes(280, 18e6, 1)
bd2 = SC.estimate_render_breakdown(280, 18e6, 2)
bd1 = SC.estimate_render_breakdown(280, 18e6, 1)
print("   280s/4K60/18M：P=2 需 %.1f GB（mjpeg %.1f + 视频 %.1f + 余量 %.1f）"
      % (p2 / GB, bd2["mjpeg"] / GB, bd2["video"] / GB, bd2["margin"] / GB))
print("   280s/4K60/18M：P=1 需 %.1f GB（mjpeg %.1f + 视频 %.1f + 余量 %.1f）"
      % (p1 / GB, bd1["mjpeg"] / GB, bd1["video"] / GB, bd1["margin"] / GB))
ck("  P=2 估在 30~45 GB 区间", p2 / GB, 38.0, 7.0, "GB")
ck("  P=1 估在 1~3 GB 区间（实测约 1.8 GB）", p1 / GB, 1.8, 1.0, "GB")
ck("  P=1 相较高峰省 >88%%", (1 - p1 / p2) * 100, 95.0, 7.0, "%")
ck("  P=1 的 mjpeg 项为 0（流式不落盘）", bd1["mjpeg"], 0)
ck("  P=2 的 mjpeg 占 >85%%", bd2["mjpeg"] / bd2["total"] * 100, 95.0, 10.0, "%")
ck("  单帧取值 ≥ 实测 2.03MB（不得退回抽帧的 0.57MB）", SC.MJPEG_FRAME_MB >= 2.03, True)

print()
print("=" * 72)
print("⑩ --auto-pages 按空间自动选档")
print("=" * 72)
r = SC.choose_render_pages(int(200 * GB), 280, 18e6, prefer=2)
ck("  可用 200 GB → P=2（速度优先）", r["pages"], 2)
ck("  未触发降级", r["fallback"], False)
r = SC.choose_render_pages(int(39 * GB), 280, 18e6, prefer=2)
ck("  可用 39 GB（够 P=2 的 %.1f）→ 仍 P=2" % (r["need_p2"] / GB), r["pages"], 2)
r = SC.choose_render_pages(int(5 * GB), 280, 18e6, prefer=2)
ck("  可用 5 GB（不够 P=2）→ 降级 P=1", r["pages"], 1)
ck("  fallback 标记为真", r["fallback"], True)
ck("  降级后 need 取 P=1 的量", r["need"], r["need_p1"])

print()
print("=" * 72)
print("⑪ P=1 也不足 → 必须在渲染开始前抛错（不启动长任务）")
print("=" * 72)
try:
    SC.choose_render_pages(int(0.5 * GB), 280, 18e6, prefer=2)
    ck("  抛 RuntimeError", False, True)
except RuntimeError as e:
    ck("  抛 RuntimeError", True, True)
    msg = str(e)
    ck("  信息含 P=2 需求量", "P=2" in msg and "GB" in msg, True)
    ck("  信息含 P=1 需求量", "P=1" in msg, True)
    ck("  信息含当前可用量", "当前可用" in msg, True)
    print("   实际报错：%s" % msg)

print()
print("=" * 72)
print("⑫ _render_tmp 与成片隔离 + 完成后不留大型残留")
print("=" * 72)
tmpd = tempfile.mkdtemp(prefix="tmproot_")
try:
    d = SC.render_tmp_dir(tmpd, "2026-10-08")
    norm = os.path.abspath(d).replace("\\", "/")
    ck("  目录是 _render_tmp/<date>", norm.endswith("_render_tmp/2026-10-08"), True)
    ck("  不在 cuts/ 下（与成片物理隔离）", "/cuts/" in norm, False)
    ck("  目录已创建", os.path.isdir(d), True)

    fake_out = os.path.join(d, "【x】y【z】.mp4.render.mp4")
    for suf in (".p0.mjpeg", ".p1.mjpeg", ".p0.mp4", ".p1.mp4", ".concat.txt"):
        with open(fake_out + suf, "wb") as fh:
            fh.write(b"\x00" * MB)
    io.open(fake_out, "wb").write(b"\x00" * MB)          # .render.mp4 本体
    ck("  渲染中 temp 统计含全部产物", SC._render_tmp_size(fake_out) >= 6 * MB, True)
    SC._cleanup_render_tmp(fake_out)
    ck("  清理后只剩 .render.mp4（混流还要用，不能提前删）",
       sorted(os.listdir(d)), ["【x】y【z】.mp4.render.mp4"])
    ck("  mjpeg 全清", any(f.endswith(".mjpeg") for f in os.listdir(d)), False)
    _rm = SC._rm_retry(fake_out)
    SC._prune_empty_tmp_dir(tmpd, "2026-10-08")
    ck("  收尾后临时目录被删掉", os.path.exists(d), False)
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

print()
print("=" * 72)
print("⑬ 历史残留自愈：_render_tmp 下超过 6h 的残留会被下次启动清掉")
print("=" * 72)
tmpd = tempfile.mkdtemp(prefix="sweep2_")
try:
    d2 = os.path.join(tmpd, "_render_tmp", "2026-10-07")
    os.makedirs(d2)
    stale = os.path.join(d2, "old.render.mp4.p0.mjpeg")
    with open(stale, "wb") as fh:
        fh.write(b"\x00" * (5 * MB))
    freed = SC._sweep_stale_render_tmp(tmpd, older_than_h=-1.0, log=lambda s: None)
    ck("  回收字节数", freed, 5 * MB)
    ck("  残留已消失", os.path.exists(stale), False)

    fresh = os.path.join(d2, "new.render.mp4.p1.mjpeg")
    with open(fresh, "wb") as fh:
        fh.write(b"\x00" * (5 * MB))
    ck("  刚写入的被视为正在跑，跳过",
       SC._sweep_stale_render_tmp(tmpd, older_than_h=6.0, log=lambda s: None), 0)
    ck("  正在跑的文件仍在", os.path.exists(fresh), True)
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

print()
print("=" * 72)
print("⑭ 测试内公式复刻与实现不得漂移（防止两边各改各的）")
print("=" * 72)
for d in (229, 280, 600):
    ck("  %4ds 复刻 == 实现（差 <1KB）" % d,
       abs(need_new(d) - SC.estimate_render_bytes(d, 18e6, 2)) < 1024, True)

print()
print("=" * 72)
print("⑮ 渲染中磁盘监控常量自洽")
print("=" * 72)
ck("  监控间隔 30s", SC.DISK_MONITOR_INTERVAL_S, 30.0)
ck("  危险区阈值 15%%", SC.DISK_DANGER_RATIO, 0.15)
ck("  陈旧门槛 6h", SC.STALE_TMP_HOURS, 6.0)
ck("  安全余量 0.5GB", SC.DISK_SAFETY_MARGIN, int(0.5 * GB))

print()
print("=" * 72)
print("⑯ 渲染中危险区阈值：15% 占比必须有绝对上限（否则大容量盘会误杀）")
print("=" * 72)
# 2026-10-08 实测事故：931.5 GB 盘、剩 139.2 GB（14.9%）被判危险区，渲染被掐断，
# 而当时本次渲染只需 24.8 GB —— 15% × 总容量 = 140 GB 是需求的 4 倍。
th = SC.disk_danger_threshold(int(931.5 * GB), int(25 * GB))
ck("  931GB盘/还需25GB → 阈值 27GB（需求侧生效）", th / GB, 27.0, 0.2, "GB")
ck("  剩 139.2GB 时不该中止", 139.2 * GB < th, False)
ck("  剩 100GB 时不该中止", 100 * GB < th, False)
ck("  剩 26GB 时应中止", 26 * GB < th, True)
th2 = SC.disk_danger_threshold(int(100 * GB), int(1 * GB))
ck("  100GB盘/还需1GB → 阈值 15GB（15% 占比生效）", th2 / GB, 15.0, 0.2, "GB")
th3 = SC.disk_danger_threshold(int(931.5 * GB), int(0.5 * GB))
ck("  收尾阶段仍守 20GB 绝对底线（保护机器）", th3 / GB, 20.0, 0.2, "GB")
ck("  剩 1GB 时应中止", 1 * GB < th3, True)
ck("  剩 25GB 时不该中止", 25 * GB < th3, False)
# 总容量 40GB 的小盘：15% = 6GB < 20GB 上限，占比规则原样生效
th4 = SC.disk_danger_threshold(int(40 * GB), int(0.5 * GB))
ck("  40GB小盘/还需0.5GB → 阈值 6GB（15%×40）", th4 / GB, 6.0, 0.2, "GB")
ck("  15% 规则绝对上限 = 20GB", SC.DISK_DANGER_MAX_GATE, int(20 * GB))

print("=" * 72)
if _fails:
    print("✗ %d 项未通过：%s" % (len(_fails), _fails))
    sys.exit(1)
print("✓ 全部通过")
