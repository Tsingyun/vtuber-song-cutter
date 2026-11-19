#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pv_z_render.py —— 用上游引擎出片（分段单进程 + 拼接 + 混音）。

    为什么不用上游自带的 `--workers N`：
    这台机器上 `--workers 2/3` 会在开头一百多帧后整段停住（两个 worker 同时停在同一时刻），
    `--workers 1` 则稳定在 2.6~2.9 fps。原因没有继续深挖（疑似 ANGLE/D3D11 共享上下文与
    NVENC 并发会话），但结论是可用的：**每个进程只开一个 worker，用多个进程并行分段**。

    本脚本因此把 [start, end) 切成 N 段，每段起一个独立的 `render_frames.mjs --workers 1`，
    全部成功后再用我们自己的 ffmpeg 拼接并混入音轨（上游的 --audio 只在单段时可用）。

用法：
    python tools/pv_z_render.py --dst _tmp/mvg/zrun --out 40s.mp4 --start 0 --end 40 --chunks 2
    python tools/pv_z_render.py --dst _tmp/mvg/zrun --out full.mp4 --chunks 3
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from songcut import pvlink as PL  # noqa: E402

RT = PL.runtime()


def run_chunk(dst, i, t0, t1, out_dir, log_dir):
    log = os.path.join(log_dir, "chunk_%02d.log" % i)
    cmd = [RT["node"], os.path.join(dst, "tools", "render_frames.mjs"),
           "--root", dst,
           "--start", "%.3f" % t0, "--end", "%.3f" % t1,
           "--fps", "30", "--workers", "1",
           "--out", os.path.join(out_dir, "chunk_%02d.mp4" % i)]
    f = open(log, "wb")
    p = subprocess.Popen(cmd, cwd=dst, stdout=f, stderr=subprocess.STDOUT)
    return p, log, f, cmd


def concat(parts, out, audio):
    """ffmpeg concat demuxer + 音轨：直接用文件列表，避免中间 mp4 的容器差异。"""
    lst = os.path.join(os.path.dirname(out), "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '%s'\n" % p.replace("\\", "/"))
    cmd = [RT["ffmpeg"], "-y", "-f", "concat", "-safe", "0", "-i", lst]
    if audio:
        cmd += ["-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "320k", "-shortest"]
    cmd += ["-c:v", "copy", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main(argv=None):
    ap = argparse.ArgumentParser(description="上游引擎分段出片")
    ap.add_argument("--dst", required=True, help="上游工程根（含 tools/render_frames.mjs）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=0.0, help="不给就取 timeline 的时长")
    ap.add_argument("--chunks", type=int, default=2)
    ap.add_argument("--audio", default="", help="音轨；默认用 <dst>/audio/song.wav")
    ap.add_argument("--no-audio", action="store_true")
    a = ap.parse_args(argv)

    dst = os.path.abspath(a.dst)
    dur = a.end
    if not dur:
        rep = json.load(open(os.path.join(dst, "analysis", "z_link.json"), encoding="utf-8"))
        dur = float(rep["dur"])
    t0, t1 = float(a.start), float(dur)
    out = os.path.abspath(a.out)
    out_dir = os.path.join(os.path.dirname(out), "_parts")
    log_dir = os.path.join(os.path.dirname(out), "_logs")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        os.remove(os.path.join(out_dir, f))

    n = max(1, a.chunks)
    step = (t1 - t0) / n
    jobs = []
    for i in range(n):
        jobs.append((i, t0 + i * step, t0 + (i + 1) * step))
    print("[z-render] %d 段，t=%.2f..%.2f，逐段串行（同机并发浏览器会卡死，见 docs/z-runtime.md §5）" % (n, t0, t1))
    t_start = time.time()
    bad = []
    for i, s, e in jobs:
        dstmp = os.path.join(out_dir, "chunk_%02d.mp4" % i)
        if os.path.exists(dstmp) and os.path.getsize(dstmp) > 65536:
            print("  chunk %02d  %.2f-%.2f s  已存在，跳过" % (i, s, e))
            continue
        p, log, fh, cmd = run_chunk(dst, i, s, e, out_dir, log_dir)
        print("  chunk %02d  %.2f-%.2f s  pid=%d" % (i, s, e, p.pid), flush=True)
        rc = p.wait()
        fh.close()
        if rc != 0:
            bad.append((i, rc, log))
            print("  chunk %02d  失败 rc=%d  日志 %s" % (i, rc, log), flush=True)
            break
        print("  chunk %02d  完成  %.0fs" % (i, time.time() - t_start), flush=True)
    if bad:
        print("[z-render] 第 %d 段失败，先看日志：%s" % (bad[0][0], bad[0][2]))
        return 1

    parts = [os.path.join(out_dir, "chunk_%02d.mp4" % i) for i, _, _ in jobs]
    miss = [p for p in parts if not os.path.exists(p)]
    if miss:
        print("[z-render] 缺段：%s" % miss)
        return 1
    audio = "" if a.no_audio else (a.audio or os.path.join(dst, "audio", "song.wav"))
    if audio and not os.path.exists(audio):
        print("[z-render] 音轨不存在：%s" % audio)
        return 1
    concat(parts, out, audio)
    sz = os.path.getsize(out) / 1048576.0
    print("[z-render] %s  %.1f MB  耗时 %.0fs" % (out, sz, time.time() - t_start))
    return 0


if __name__ == "__main__":
    sys.exit(main())
