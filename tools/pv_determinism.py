# -*- coding: utf-8 -*-
"""成片级「重复渲染一致性」验收。

单帧确定性（同一 t 三连渲取 md5）只能证明 canvas 逻辑可复现，证明不了整条
流水线（逐帧循环 / 编码器 / 分片并行）可复现。用户验收项要求「重复渲染结果
一致」，所以这里直接比对两个成片的：

  1. 视频流字节 md5      —— 最强判据，一致即完全确定
  2. 逐帧 md5 序列       —— 字节不同时，用来看是「全片都不同」还是「只在某个
                            片段不同」（编码器噪声 vs 画面真的变了）
  3. 逐帧 PSNR           —— 量化差异幅度；>45dB 说明是编码器级别噪声

用法：
  python tools/pv_determinism.py --a _tmp/pv/x.mp4 --b _tmp/pv/x_b.mp4
  python tools/pv_determinism.py --job _tmp/pv/job_x.json --a x.mp4   # 自动再渲一次

退出码：0 = 字节一致（PASS）；2 = 仅编码噪声（WARN）；3 = 画面确实不同（FAIL）
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = os.environ.get("SONGCUT_NODE") or r"C:/Users/Tsing/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"
NODE_PATH = os.environ.get("SONGCUT_NODE_PATH") or r"C:/Users/Tsing/.workbuddy/binaries/node/workspace/node_modules"
FFMPEG = os.environ.get("SONGCUT_FFMPEG") or "D:/ffmpeg/bin/ffmpeg.exe"


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def stream_md5(path):
    r = sh([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path,
            "-map", "0:v:0", "-c", "copy", "-f", "md5", "-"])
    return (r.stdout or "").strip()


def frame_md5(path):
    """逐帧解码后取 md5（不含编码器元信息，只反映解码出的像素）。"""
    r = sh([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path,
            "-map", "0:v:0", "-f", "framemd5", "-"])
    out = []
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        p = [x.strip() for x in ln.split(",")]
        if len(p) >= 6:
            out.append((int(p[2]) if p[2].isdigit() else 0, p[-1]))
    return out


def psnr_seq(a, b, fps=60):
    """逐秒 PSNR（整帧比对太慢，按 1s 抽帧更实用）。返回 [(t, psnr|None)]。"""
    filt = "fps=1,format=yuv420p"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "info", "-i", a, "-i", b,
           "-filter_complex", "[0:v]%s[x];[1:v]%s[y];[x][y]psnr=stats_file=-" % (filt, filt),
           "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    res = []
    for ln in (r.stderr or "").splitlines():
        m = re.search(r"n:(\d+).*?psnr_avg:([\d.]+|inf)", ln)
        if m:
            res.append((int(m.group(1)), None if m.group(2) == "inf" else float(m.group(2))))
    return res


def render_again(job_path, out_path):
    job = json.load(open(job_path, encoding="utf-8"))
    job["out"] = out_path.replace("\\", "/")
    job2 = job_path.replace(".json", "_det.json")
    json.dump(job, open(job2, "w", encoding="utf-8"), ensure_ascii=False)
    env = dict(os.environ, NODE_PATH=NODE_PATH)
    print("[det] 再渲一次 -> %s" % out_path)
    r = subprocess.run([NODE, os.path.join(ROOT, "renderer/render_song.cjs"),
                        "--job", job2, "--encoder", "ffmpeg"], env=env)
    if r.returncode != 0:
        raise SystemExit("重渲失败 rc=%d" % r.returncode)
    return job2


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--a", required=True, help="第一次成片")
    a.add_argument("--b", default="", help="第二次成片；缺省时用 --job 现场再渲")
    a.add_argument("--job", default="", help="render_song.cjs 的 job json（用于自动再渲）")
    a.add_argument("--keep", action="store_true", help="保留第二次成片")
    args = a.parse_args()

    pa = os.path.abspath(args.a)
    if not os.path.exists(pa):
        raise SystemExit("找不到 %s" % pa)
    pb = os.path.abspath(args.b) if args.b else ""
    if not pb:
        if not args.job:
            raise SystemExit("需要 --b 或 --job 之一")
        pb = pa.replace(".mp4", "_det.mp4")
        render_again(os.path.abspath(args.job), pb)

    ma, mb = stream_md5(pa), stream_md5(pb)
    print("\n[1] 视频流 md5")
    print("    A = %s" % ma)
    print("    B = %s" % mb)
    if ma and ma == mb:
        print("    ✅ PASS：两次渲染视频流**字节完全一致**")
        if not args.keep and pb != pa:
            try:
                os.remove(pb)
            except OSError:
                pass
        return 0

    print("    ⚠ 字节不一致，继续定位差异范围")

    fa, fb = frame_md5(pa), frame_md5(pb)
    print("\n[2] 逐帧 md5 序列  A=%d 帧  B=%d 帧" % (len(fa), len(fb)))
    n = min(len(fa), len(fb))
    diff = [i for i in range(n) if fa[i][1] != fb[i][1]]
    print("    相同 %d / 不同 %d" % (n - len(diff), len(diff)))
    if diff:
        seg = []
        s = p = diff[0]
        for i in diff[1:]:
            if i == p + 1:
                p = i
            else:
                seg.append((s, p))
                s = p = i
        seg.append((s, p))
        for s0, s1 in seg[:12]:
            print("    差异帧 %d~%d  (%.2fs~%.2fs)" % (s0, s1, s0 / 60.0, s1 / 60.0))
        if len(seg) > 12:
            print("    …共 %d 段" % len(seg))

    pr = psnr_seq(pa, pb)
    if pr:
        vals = [v for _, v in pr if v is not None]
        print("\n[3] 逐秒 PSNR  n=%d" % len(pr))
        if vals:
            vals_sorted = sorted(vals)
            print("    min=%.2f  p05=%.2f  median=%.2f  max=%.2f"
                  % (vals_sorted[0], vals_sorted[int(len(vals_sorted) * .05)],
                     vals_sorted[len(vals_sorted) // 2], vals_sorted[-1]))
            worst = sorted(pr, key=lambda x: (x[1] is None, x[1] or 0))[:6]
            for t, v in worst:
                print("    t=%ds  psnr=%s" % (t, "inf" if v is None else "%.2f" % v))
            if vals_sorted[0] >= 45:
                print("    ⚠ WARN：差异仅为编码器噪声（min PSNR ≥ 45dB），画面实质一致")
                if not args.keep:
                    try:
                        os.remove(pb)
                    except OSError:
                        pass
                return 2
    print("    ❌ FAIL：画面确实不同，按 RULES 51 顺序排查")
    return 3


if __name__ == "__main__":
    sys.exit(main())
