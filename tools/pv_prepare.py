# -*- coding: utf-8 -*-
"""文字 PV 的前置编排：音频 → 分析 → 逐词轴 → 编排稿（一条命令）。

为什么单独抽一层
----------------
主链 `song_cutter.py` 已经有音频、歌词、时间轴，但没有「文字 PV」需要的两样东西：
① `analyze_music.py` 的段落/能量曲线（导演层拿它定档位）；
② 逐词时间轴（LRC 只有行级，逐字出现需要更细）。
把这三步（分析 → 轴 → 导演）封装在这里，主链只负责「调它一次 + 把结果塞进 job」，
不把 400 行编排逻辑塞进 song_cutter。

用法（主链内部调用，也可单独跑）：
  python tools/pv_prepare.py --audio _tmp/a.mp3 --lrc _tmp/a.lrc --outdir _tmp/pv/prep \\
      --title "Innocence" --artist "Avril Lavigne" --vocal "岁己SUI" --date 2026.10.27 \\
      [--live _tmp/live/live.json] [--dur 0] [--seed 20261027] [--reuse]

输出：<outdir>/analysis.json、<outdir>/timing.json、<outdir>/storyboard.json
退出码 0 = 成功；非 0 时打印原因（主链据此回退到默认播放器页面，而不是整首废掉）。
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time

# 运行时路径只有一份：songcut/pvlink.runtime()（env → config.json → 内置探测）。
# 写死的 D:/ffmpeg/bin/... 换台机器就得全局替换，漏一处就是「找不到 ffmpeg」。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from songcut import pvlink as _PVRT  # noqa: E402
_RT = _PVRT.runtime()


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def run(cmd, what):
    t = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = r.returncode == 0
    print("[pv] %-8s %s  %.1fs" % (what, "ok" if ok else "FAIL", time.time() - t))
    if not ok:
        tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-6:]
        for ln in tail:
            print("      " + ln)
    return ok, r


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--audio", required=True)
    a.add_argument("--lrc", required=True, help="对齐后的 LRC 文件（成片实际使用的那份）")
    a.add_argument("--outdir", required=True)
    a.add_argument("--dur", type=float, default=0.0, help="0=按音频实际时长")
    a.add_argument("--start", type=float, default=0.0)
    a.add_argument("--seed", type=int, default=20261027)
    a.add_argument("--title", default="")
    a.add_argument("--artist", default="")
    a.add_argument("--vocal", default="")
    a.add_argument("--date", default="")
    a.add_argument("--source", default="")
    a.add_argument("--live", default="", help="prepare_live.py 的 live.json（缺省=不用人物层）")
    a.add_argument("--fig-spans", default="",
                   help="人物挂点表（tools/pv_spans.py 从编排稿生成）；"
                        "分段序列必须给，否则随机挂图会取到错位画面")
    a.add_argument("--jz", default="", help="tools/pv_jz.cjs 渲出的 jz.json（缺省=不用 JIZURA 层）")
    a.add_argument("--arc", action="store_true",
                   help="风格弧线（按歌词意境 + 曲风能量逐句调制）；缺省关闭，关闭时编排稿与旧版逐字段相同")
    a.add_argument("--reuse", action="store_true", help="复用已有 analysis/timing/storyboard（只补缺的）")
    # ---- 图层语义（可选；不传时编排稿与旧版逐字段相同）
    a.add_argument("--bg3d", default="", help="三维场景层：图片序列目录（Blender/DCC 产物）")
    a.add_argument("--bg3d-fps", type=float, default=24.0)
    a.add_argument("--bg3d-shots", default="all", help="all 或逗号分隔镜头号")
    a.add_argument("--bg3d-rect", default="0,0,1,1", help="摆放 x,y,w,h（画面占比）")
    a.add_argument("--bg3d-alpha", type=float, default=1.0)
    a.add_argument("--grade-bg", default="", help='背景层组后期，如 "blur(6px) saturate(1.15)"')
    a.add_argument("--grade-fig", default="", help="人物层滤镜（缺省不处理）")
    a.add_argument("--grade-shots", default="all")
    # 立绘（2026-11-03 改向）：人物穿插改用素材库的透明 PNG
    a.add_argument("--fig-art", default="",
                   help="立绘：auto / #序号 / 文件名 / 子串（songcut/figart.py）")
    a.add_argument("--fig-art-shots", default="split")
    a.add_argument("--fig-art-h", type=float, default=0.0)
    a.add_argument("--fig-art-x", type=float, default=-2.0)
    a.add_argument("--fig-art-shadow", type=float, default=-1.0)
    a.add_argument("--ffmpeg", default=_RT["ffmpeg"],
                   help="缺省走 pvlink.runtime（env → config → 内置探测）")
    args = a.parse_args()

    out = os.path.abspath(args.outdir)
    os.makedirs(out, exist_ok=True)
    ana = os.path.join(out, "analysis.json")
    tim = os.path.join(out, "timing.json")
    sb = os.path.join(out, "storyboard.json")
    for p in (args.audio, args.lrc):
        if not os.path.exists(p):
            print("[pv] 缺少输入：%s" % p)
            return 2

    dur = args.dur
    if dur <= 0:
        r = subprocess.run([_RT["ffprobe"],
                            "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", args.audio],
                           capture_output=True, text=True)
        try:
            dur = float((r.stdout or "").strip())
        except ValueError:
            print("[pv] 读不到音频时长")
            return 2

    steps = []
    if not (args.reuse and os.path.exists(ana)):
        steps.append(([PY, os.path.join(ROOT, "tools/analyze_music.py"),
                       "--audio", args.audio, "--out", ana, "--ffmpeg", args.ffmpeg], "analysis"))
    if not (args.reuse and os.path.exists(tim)):
        steps.append(([PY, os.path.join(ROOT, "tools/pv_timing.py"),
                       "--audio", args.audio, "--lrc", args.lrc, "--out", tim,
                       "--analysis", ana, "--ffmpeg", args.ffmpeg], "timing"))
    if not (args.reuse and os.path.exists(sb)):
        cmd = [PY, os.path.join(ROOT, "tools/pv_direct.py"),
               "--timing", tim, "--analysis", ana, "--out", sb,
               "--start", str(args.start), "--dur", str(dur), "--seed", str(args.seed),
               "--title", args.title, "--artist", args.artist, "--vocal", args.vocal,
               "--date", args.date, "--source", args.source, "--lrc", args.lrc]
        if args.live:
            cmd += ["--live", args.live]
        if args.fig_spans:
            cmd += ["--fig-spans", args.fig_spans]
        if args.jz:
            cmd += ["--jz", args.jz]
        if args.arc:
            cmd += ["--arc"]
        # 图层语义透传（不传就不加，编排稿与旧版逐字段相同）
        if args.bg3d:
            cmd += ["--bg3d", args.bg3d, "--bg3d-fps", str(args.bg3d_fps),
                    "--bg3d-shots", args.bg3d_shots, "--bg3d-rect", args.bg3d_rect,
                    "--bg3d-alpha", str(args.bg3d_alpha)]
        if args.grade_bg:
            cmd += ["--grade-bg", args.grade_bg]
        if args.grade_fig:
            cmd += ["--grade-fig", args.grade_fig]
        if args.grade_bg or args.grade_fig:
            cmd += ["--grade-shots", args.grade_shots]
        if args.fig_art:
            cmd += ["--fig-art", args.fig_art, "--fig-art-shots", args.fig_art_shots]
            if args.fig_art_h > 0:
                cmd += ["--fig-art-h", str(args.fig_art_h)]
            if args.fig_art_x > -2:
                cmd += ["--fig-art-x", str(args.fig_art_x)]
            if args.fig_art_shadow >= 0:
                cmd += ["--fig-art-shadow", str(args.fig_art_shadow)]
        steps.append((cmd, "direct"))

    for cmd, what in steps:
        ok, _ = run(cmd, what)
        if not ok:
            return 3

    # 交付前自检：编排稿必须覆盖整段，且行数不能比输入 LRC 少太多
    try:
        s = json.load(io.open(sb, encoding="utf-8"))
        m = s.get("meta") or {}
        n = len(s.get("shots") or [])
        cov = float(m.get("dur") or 0)
        print("[pv] storyboard  shots=%d  dur=%.2fs  seed=%s  start=%.2f"
              % (n, cov, m.get("seed"), float(m.get("start") or 0)))
        if n < 3:
            print("[pv] ⚠ 镜头数过少，编排稿可疑")
            return 4
    except Exception as e:
        print("[pv] 编排稿读不出：%s" % e)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
