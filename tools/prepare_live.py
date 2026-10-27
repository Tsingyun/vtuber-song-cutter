#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把录播原始画面裁成播放器可用的图片序列（直播层素材）。

为什么要图片序列而不是 <video>：
  播放器是「逐帧离线渲染」（每帧 renderFrame(i) 后 toBlob），<video> 需要
  currentTime seek + 等 seeked 事件，异步且偶发丢帧；图片序列是同步可用的
  （Image 已解码即 drawImage），缺帧时有上一帧兜底，画面不会黑。

两个序列：
  head  —— 头部方形裁切，供圆窗使用（4K 下圆窗直径约 1040px，源 640px 放大 1.6×）
  wide  —— 整幅低分辨率，供失焦底衬使用（本身就很糊，15fps 足够）

用法：
  python tools/prepare_live.py --src <录播> --cut-start 4474.1 \
      --start 40 --dur 12 --out _tmp/live_seq --mode all
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

FFMPEG = "D:/ffmpeg/bin/ffmpeg.exe"


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        sys.stderr.write((p.stderr or "")[-2000:])
        raise SystemExit("ffmpeg 失败 (%d)" % p.returncode)
    return p


def probe_size(path):
    p = run([FFMPEG.replace("ffmpeg.exe", "ffprobe.exe"), "-v", "error",
             "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", path])
    w, h = p.stdout.strip().split("\n")[0].split(",")[:2]
    return int(w), int(h)


def extract(src, ss, dur, vf, outdir, pattern, q=3):
    if os.path.isdir(outdir):
        shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir, exist_ok=True)
    run([FFMPEG, "-y", "-v", "error", "-ss", "%.3f" % ss, "-t", "%.3f" % dur,
         "-i", src, "-vf", vf, "-q:v", str(q),
         "-start_number", "1", os.path.join(outdir, pattern)])
    files = sorted(f for f in os.listdir(outdir) if f.endswith(".jpg"))
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="录播源 mp4")
    ap.add_argument("--cut-start", type=float, required=True, help="成片 0s 对应的录播绝对时间")
    ap.add_argument("--start", type=float, default=0.0, help="片段在成片里的起点")
    ap.add_argument("--dur", type=float, default=12.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--head-fps", type=int, default=30)
    ap.add_argument("--wide-fps", type=int, default=15)
    # 头部裁切框（源分辨率 1920×1080 时的像素）。脸中心实测在源 (965, 372)，
    # 这里以它为裁切框几何中心 ⇒ 归一化圆心正好 (0.5, 0.5)，圆窗与源图同轴，
    # 放大倍数只由「圆窗半径」决定，不必再补偿偏心。
    ap.add_argument("--head-x", type=int, default=645)
    ap.add_argument("--head-y", type=int, default=52)
    ap.add_argument("--head-s", type=int, default=640)
    # 脸中心（源像素）：圆窗圆心据此归一化，--head-x/y 取「脸中心 - 半边长」即同轴
    ap.add_argument("--face-x", type=int, default=965)
    ap.add_argument("--face-y", type=int, default=372)
    ap.add_argument("--mode", default="all", choices=["all", "head", "wide", "body"])
    ap.add_argument("--body-x", type=int, default=675)
    ap.add_argument("--body-y", type=int, default=0)
    ap.add_argument("--body-w", type=int, default=580)
    ap.add_argument("--body-h", type=int, default=772)   # 580:772 = 3:4
    ap.add_argument("--jobs", default=None, help="输出 job 描述 json")
    a = ap.parse_args()

    ss = a.cut_start + a.start
    sw, sh = probe_size(a.src)
    print("[live] 源 %dx%d  seek %.3fs  dur %.1fs" % (sw, sh, ss, a.dur))

    # 源不是 1920 宽时按比例换算裁切框（录播一般是 1920×1080，这里只做兜底）
    sc = sw / 1920.0
    hx = int(round(a.head_x * sc))
    hy = int(round(a.head_y * sc))
    hs = int(round(a.head_s * sc))
    hs = min(hs, sh, sw - hx)

    os.makedirs(a.out, exist_ok=True)
    info = {"src": a.src, "cut_start": a.cut_start, "start": a.start,
            "dur": a.dur, "head": None, "wide": None, "body": None}

    if a.mode in ("all", "head"):
        d = os.path.join(a.out, "head")
        files = extract(a.src, ss, a.dur, "fps=%d,crop=%d:%d:%d:%d" % (a.head_fps, hs, hs, hx, hy), d, "h_%05d.jpg", q=2)
        info["head"] = {"dir": d.replace("\\", "/"), "n": len(files), "fps": a.head_fps,
                        "size": hs, "pattern": "h_%05d.jpg"}
        print("[live] head  %d 帧 %d×%d @%dfps  %.1f MB" %
              (len(files), hs, hs, a.head_fps, sum(os.path.getsize(os.path.join(d, f)) for f in files) / 1e6))

    if a.mode in ("all", "wide"):
        d = os.path.join(a.out, "wide")
        # 关键：整幅直播画面里有聊天列表（左）、粉丝榜（右）、顶部弹幕 ——
        # 直接铺成底衬会变成「一张截图」，一眼廉价。只取中间
        # 1152×648（源 1920 的 x 420~1572）＝ 主播 + 房间背景，UI 全在框外。
        cw, chh = int(round(1152 * sc)), int(round(648 * sc))
        cx0 = int(round(420 * sc))
        # 480 宽放大到 1920 是 4×，实测色块明显；960 是 2×，柔但仍是「失焦」不是「马赛克」
        ww, wh = 960, 540
        # 降饱和在 ffmpeg 侧烘死：canvas 的 ctx.filter 每帧重跑 saturate 在 4K 下很贵，
        # 而底衬本身就是「失焦的现场」，颜色要退到背景里去，不能和歌词/封面的彩色抢。
        files = extract(a.src, ss, a.dur,
                        "fps=%d,crop=%d:%d:%d:0,scale=%d:%d:flags=lanczos,hue=s=0.42"
                        % (a.wide_fps, cw, chh, cx0, ww, wh),
                        d, "w_%05d.jpg", q=5)
        info["wide"] = {"dir": d.replace("\\", "/"), "n": len(files), "fps": a.wide_fps,
                        "w": ww, "h": wh, "pattern": "w_%05d.jpg"}
        print("[live] wide  %d 帧 %d×%d @%dfps  %.1f MB" %
              (len(files), ww, wh, a.wide_fps, sum(os.path.getsize(os.path.join(d, f)) for f in files) / 1e6))

    if a.mode in ("all", "body"):
        # 半身竖幅（3:4）：左右都留出了聊天栏与粉丝榜，画面里只有主播本人和房间背景
        d = os.path.join(a.out, "body")
        bw, bh = int(round(a.body_w * sc)), int(round(a.body_h * sc))
        bx, by = int(round(a.body_x * sc)), int(round(a.body_y * sc))
        files = extract(a.src, ss, a.dur,
                        "fps=%d,crop=%d:%d:%d:%d" % (a.head_fps, bw, bh, bx, by),
                        d, "b_%05d.jpg", q=2)
        info["body"] = {"dir": d.replace("\\", "/"), "n": len(files), "fps": a.head_fps,
                        "w": bw, "h": bh, "pattern": "b_%05d.jpg"}
        print("[live] body  %d 帧 %d×%d @%dfps  %.1f MB" %
              (len(files), bw, bh, a.head_fps, sum(os.path.getsize(os.path.join(d, f)) for f in files) / 1e6))

    # 圆窗在「裁切图」里的圆心（归一化 0..1）：脸中心实测在源 (965, 372)
    cx = (a.face_x * sc - hx) / float(hs)
    cy = (a.face_y * sc - hy) / float(hs)
    info["face"] = {"cx": round(cx, 4), "cy": round(cy, 4)}

    jp = a.jobs or os.path.join(a.out, "live.json")
    # 只重跑一种序列时，保留另一种的既有条目 —— 否则 live.json 里的 head / wide
    # 会被置空，播放器静默不画（这个坑已经踩过一次）。
    if os.path.isfile(jp):
        try:
            with open(jp, encoding="utf-8") as f:
                old = json.load(f)
            for k in ("head", "wide", "body"):
                if not info.get(k) and old.get(k):
                    info[k] = old[k]
                    print("[live] 保留既有 %s（%d 帧）" % (k, old[k].get("n", 0)))
        except (ValueError, OSError):
            pass

    with open(jp, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    print("[live] face 圆心(裁切图归一化) %.3f, %.3f  → %s" % (cx, cy, jp))


if __name__ == "__main__":
    main()
