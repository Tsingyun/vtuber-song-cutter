#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抽任意一段（通常副歌）做 GPU 渲染 demo，并混回该曲音轨。

用途：调歌词版式/动效时快速验证，不必整首重渲。走的是与正式出片
完全相同的 renderer/gpu_render.GpuRenderer，所以看到的就是成片效果。

    python tools/make_lyric_demo.py \
        --src "cuts/2026-11-19/【岁己SUI】唯一【20261119歌切】.mp4" \
        --lrc "cuts/2026-11-19/【岁己SUI】唯一【20261119歌切】.lrc" \
        --cover "_media_cache/_cover_唯一.img" \
        --title 唯一 --artist 告五人 --vocal 岁己SUI \
        --start 38 --sec 34 --res 2 --out _tmp/demo.mp4
"""
import argparse
import io
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="已出片的 mp4（只取音轨）")
    ap.add_argument("--lrc", required=True, help="对齐后的 .lrc（可含增强型逐字标签）")
    ap.add_argument("--cover", default="", help="封面图路径")
    ap.add_argument("--title", required=True)
    ap.add_argument("--artist", default="", help="原唱（画面 ORIGINAL）")
    ap.add_argument("--vocal", default="", help="演唱者（画面 VOCAL）")
    ap.add_argument("--track", default="", help="TRACK 标签，如 TRACK 03")
    ap.add_argument("--start", type=float, default=0.0, help="起始秒（全片时间轴）")
    ap.add_argument("--sec", type=float, default=30.0, help="时长秒")
    ap.add_argument("--res", type=int, default=2, help="1=1080p 2=4K")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ffmpeg", default=os.environ.get("FFMPEG_BIN") or "ffmpeg")
    a = ap.parse_args()

    from renderer import gpu_render as G

    lrc = io.open(a.lrc, encoding="utf-8").read()
    probe = a.ffmpeg.replace("ffmpeg", "ffprobe")
    if not os.path.exists(probe):
        probe = "ffprobe"
    dur = float(subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", a.src],
        capture_output=True, text=True).stdout.strip() or 0)

    out_dir = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(out_dir, exist_ok=True)
    tmp_v = os.path.join(out_dir, "_demo_v.mp4")

    job = {
        "title": a.title, "artist": a.artist, "vocal": a.vocal,
        "track": a.track, "lrc": lrc, "cover": a.cover,
        "duration": dur or (a.start + a.sec), "fps": 60,
        "bitrate": 18_000_000,
    }
    t0 = time.time()
    G.GpuRenderer(job, tmp_v, sec=a.sec, res=a.res, start=a.start).run()
    print("[demo] 画面 %.1fs" % (time.time() - t0))

    cmd = [a.ffmpeg, "-y", "-v", "error", "-i", tmp_v,
           "-ss", str(a.start), "-t", str(a.sec), "-i", a.src,
           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart", a.out]
    subprocess.run(cmd, check=True)
    os.remove(tmp_v)
    print("[demo] 完成 %s（%.1f MB）" % (a.out, os.path.getsize(a.out) / 1048576))


if __name__ == "__main__":
    main()
