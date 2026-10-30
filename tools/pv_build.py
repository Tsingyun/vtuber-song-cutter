# -*- coding: utf-8 -*-
"""文字 PV 一键出片：编排稿 → job → 逐帧渲染 → 合音轨。

为什么要这个脚本
----------------
`pv_direct.py` 只产编排稿，`render_song.cjs` 只吃 job 且**默认不封音轨**（`-an`，
正片由 song_cutter.py 负责 mux）。样片场景下音频是整首歌、画面只是一段，
直接套正片流程会把 232s 的音频贴到 66s 的画面上。这里把「取哪一段音频」
显式对齐到编排稿的 `meta.dur`，避免尾部多出一分半钟的静音黑屏。

用法：
  python tools/pv_build.py --timing _tmp/pv/timing.json --analysis _tmp/pro_analysis.json \\
      --live _tmp/live66/live.json --start 0 --dur 66 --audio _tmp/innoc_audio.mp3 \\
      --title "Innocence" --artist "Avril Lavigne" --vocal "岁己SUI" --date 2026.10.27 \\
      --out _tmp/pv/sample66.mp4 [--res 1] [--pages 1] [--reuse-sb]

中途改编排稿再出片：省略 `--reuse-sb` 会重跑导演层（随机量走 seed，结果可复现）。
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
NODE = os.environ.get("SONGCUT_NODE") or r"C:/Users/Tsing/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"
NODE_PATH = os.environ.get("SONGCUT_NODE_PATH") or r"C:/Users/Tsing/.workbuddy/binaries/node/workspace/node_modules"
FFMPEG = os.environ.get("SONGCUT_FFMPEG") or "D:/ffmpeg/bin/ffmpeg.exe"


def ap(**kw):
    a = argparse.ArgumentParser(**kw)
    a.add_argument("--timing", required=True, help="tools/pv_timing.py 产出的逐词时间轴")
    a.add_argument("--analysis", required=True, help="tools/analyze_music.py 产出的分析 json")
    a.add_argument("--live", default="", help="prepare_live.py 的 live.json（缺省=不用人物层）")
    a.add_argument("--start", type=float, default=0.0)
    a.add_argument("--dur", type=float, default=0.0, help="0=整首")
    a.add_argument("--audio", required=True, help="整首成片音频（mp3）")
    a.add_argument("--out", required=True)
    a.add_argument("--sb", default="", help="编排稿路径（缺省 _tmp/pv/storyboard.json）")
    a.add_argument("--seed", type=int, default=20261027)
    a.add_argument("--title", default="")
    a.add_argument("--artist", default="")
    a.add_argument("--vocal", default="")
    a.add_argument("--date", default="")
    a.add_argument("--mark", default="")
    a.add_argument("--res", type=int, default=1, help="1=1080p 2=4K")
    a.add_argument("--fps", type=int, default=30,
                   help="成片帧率。必须显式写进 job：不给的话 render_song 会退到 "
                        "storyboard.meta.fps（导演层默认 60），于是 koma=12 变成每张画 5 帧，"
                        "和按 30fps 量的时间轴全部错位")
    a.add_argument("--pages", type=int, default=0,
                   help="ffmpeg 两阶段并行实例数；0=按分辨率自动（4K→3，1080p→1）")
    a.add_argument("--bitrate", type=int, default=0, help="0 = res>=2 ? 18M : 12M")
    a.add_argument("--jq", type=float, default=0.92,
                   help="中间 JPEG 质量（0.92=190KB/帧；0.98 仅用于逐字节取证）")
    a.add_argument("--reuse-sb", action="store_true", help="不重跑导演层，直接用现有编排稿")
    a.add_argument("--no-mux", action="store_true", help="只出视频，不封音轨")
    return a.parse_args()


def run(cmd, **kw):
    print("[cmd] " + " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        raise SystemExit("命令失败 rc=%d" % r.returncode)


def main():
    a = ap()
    sb_path = a.sb or os.path.join(ROOT, "_tmp/pv/storyboard.json")

    if not (a.reuse_sb and os.path.exists(sb_path)):
        cmd = [PY, os.path.join(ROOT, "tools/pv_direct.py"),
               "--timing", a.timing, "--analysis", a.analysis, "--out", sb_path,
               "--start", str(a.start), "--dur", str(a.dur), "--seed", str(a.seed),
               "--fps", str(a.fps),
               "--title", a.title, "--artist", a.artist, "--vocal", a.vocal, "--date", a.date]
        if a.live:
            cmd += ["--live", a.live]
        run(cmd)

    sb = json.load(open(sb_path, encoding="utf-8"))
    dur = float(sb["meta"]["dur"])
    live = None
    if a.live:
        lj = json.load(open(os.path.join(ROOT, a.live), encoding="utf-8"))
        live = {}
        for k, _v in lj.items():
            # 序列名由 live.json 决定（s1..s6），写死三档会一条都装不进去
            if k in ("face", "cut_start") or not isinstance(_v, dict) or not _v.get("dir"):
                continue
            if lj.get(k):
                d = lj[k]["dir"].replace("\\", "/")
                if not os.path.isabs(d):
                    d = os.path.abspath(os.path.join(ROOT, d)).replace("\\", "/")
                live[k] = dict(lj[k], dir=d)
        if lj.get("face"):
            live["face"] = lj["face"]

    out = os.path.abspath(a.out).replace("\\", "/")
    silent = out.replace(".mp4", ".video.mp4") if not a.no_mux else out
    pages = a.pages or (3 if a.res >= 2 else 1)
    job = {
        "playerDir": os.path.join(ROOT, "renderer/player").replace("\\", "/"),
        "page": "index_pv.html",
        "audio": os.path.abspath(a.audio).replace("\\", "/"),
        "title": a.title, "artist": a.artist, "vocal": a.vocal,
        "storyboard": sb, "live": live, "pvDur": dur,
        "res": a.res,
        "fps": a.fps,
        "bitrate": a.bitrate or (18000000 if a.res >= 2 else 12000000),
        "encoder": "ffmpeg",
        "pages": pages,
        "out": silent,
    }
    job_path = os.path.join(ROOT, "_tmp/pv/job_%s.json" % os.path.basename(out).replace(".mp4", ""))
    os.makedirs(os.path.dirname(job_path), exist_ok=True)
    json.dump(job, open(job_path, "w", encoding="utf-8"), ensure_ascii=False)
    print("[job] %s  dur=%.2fs  res=%s  pages=%d  fps=%d" % (job_path, dur, a.res, pages, a.fps))

    env = dict(os.environ, NODE_PATH=NODE_PATH)
    run([NODE, os.path.join(ROOT, "renderer/render_song.cjs"),
         "--job", job_path, "--encoder", "ffmpeg", "--jq", str(a.jq)], env=env)

    if a.no_mux:
        print("[out] %s（未封音轨）" % out)
        return

    # 音轨：从整首音频里切出编排稿对应的那一段，长度与画面严格一致
    start = float(sb["meta"].get("start") or 0.0)
    run([FFMPEG, "-y", "-hide_banner", "-loglevel", "warning",
         "-ss", "%.3f" % start, "-t", "%.3f" % dur, "-i", job["audio"],
         "-i", silent,
         "-map", "0:a:0", "-map", "1:v:0",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
         "-shortest", "-movflags", "+faststart", out])
    if os.path.abspath(silent) != os.path.abspath(out):
        try:
            os.remove(silent)
        except OSError:
            pass
    sz = os.path.getsize(out) / 1048576
    print("[out] %s  %.1f MB  %.2fs" % (out, sz, dur))


if __name__ == "__main__":
    main()
