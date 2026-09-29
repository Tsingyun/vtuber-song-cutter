# -*- coding: utf-8 -*-
"""v9（4K60 / CBR 18 Mbps）成片验收：
  1. ffprobe 规格逐项核对（分辨率/帧率/编码/级别/像素格式/色域/码率/音轨）
  2. 与 MP3 音源时长对齐核验
  3. 源帧（画布直出 PNG）vs 成片帧 逐像素 PSNR + 分区锐度
  4. v8 1080P 旧版 vs v9 4K 新版 1:1 对照图（右下角日期 / 左下角档案号）
用法: python _verify_v9.py <成片mp4> <源帧目录> [旧版1080P]
"""
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image

import os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from songcut import config as CFG      # noqa: E402

FFPROBE = CFG.ffprobe()
FFMPEG = CFG.ffmpeg()


def probe(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json",
                          "-show_streams", "-show_format", path],
                         capture_output=True, text=True, encoding="utf-8").stdout
    return json.loads(out)


def gray(a):
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


def sharp(g):
    gx = np.diff(g, axis=1); gy = np.diff(g, axis=0)
    return float((gx[:-1, :] ** 2).mean() + (gy[:, :-1] ** 2).mean())


def psnr(x, y):
    mse = float(np.mean((x.astype(np.float64) - y.astype(np.float64)) ** 2))
    return 99.0 if mse <= 1e-12 else 10 * np.log10(255.0 ** 2 / mse)


def main():
    mp4 = sys.argv[1]
    srcdir = sys.argv[2] if len(sys.argv) > 2 else None
    old = sys.argv[3] if len(sys.argv) > 3 else None

    info = probe(mp4)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), {})
    fmt = info["format"]
    dur = float(fmt["duration"])
    vsize = int(v.get("bit_rate") or 0)
    asize = int(a.get("bit_rate") or 0)
    if not vsize:
        vsize = int(max(0, int(fmt["size"]) - asize * dur / 8) * 8 / dur)
    num, den = v["avg_frame_rate"].split("/")

    print("=" * 74)
    print("v9 成片规格验收：%s" % os.path.basename(mp4))
    print("=" * 74)
    checks = [
        ("分辨率", "%sx%s" % (v["width"], v["height"]), v["width"] == 3840 and v["height"] == 2160),
        ("帧率", "%s/%s" % (num, den), num == "60" and den == "1"),
        ("视频编码", "%s / %s / level %s" % (v["codec_name"], v.get("profile"), v.get("level")),
         v["codec_name"] == "h264" and v.get("profile") == "High"),
        ("像素格式/位深", v.get("pix_fmt"), v.get("pix_fmt") == "yuv420p"),
        ("色域", "%s / %s / range=%s" % (v.get("color_space"), v.get("color_primaries"),
                                        v.get("color_range")),
         v.get("color_space") == "bt709" and v.get("color_range") == "tv"),
        ("视频码率", "%.2f Mbps (%d kbps)" % (vsize / 1e6, vsize / 1000),
         16_000_000 <= vsize <= 18_500_000),
        ("音轨", "%s %.0f kbps %sHz %sch" % (a.get("codec_name"), asize / 1000,
                                            a.get("sample_rate"), a.get("channels")),
         a.get("codec_name") == "aac" and 0 < asize <= 320_000),
        ("时长", "%.2f s" % dur, True),
        ("体积", "%.1f MB" % (int(fmt["size"]) / 1048576), True),
        ("轨道数", "%d 轨（B站要求单视频轨+单音轨）" % len(info["streams"]),
         len(info["streams"]) == 2),
    ]
    for name, val, ok in checks:
        print("  %-12s %-40s %s" % (name, val, "✓" if ok else "✗ 不符"))
    allok = all(c[2] for c in checks)

    mp3 = os.path.splitext(mp4)[0] + ".mp3"
    if os.path.exists(mp3):
        ai = probe(mp3)
        ad = float(ai["format"]["duration"])
        d = abs(dur - ad)
        print("  %-12s 音源 %.2fs，成片 %.2fs，偏差 %.3fs %s"
              % ("音画对齐", ad, dur, d, "✓" if d < 0.15 else "✗"))
        allok = allok and d < 0.15

    # ── 源帧 vs 成片帧 ──
    if srcdir and os.path.isdir(srcdir):
        frames = sorted(int(f[1:-4]) for f in os.listdir(srcdir)
                        if f.startswith("f") and f.endswith(".png"))
        print("\n" + "=" * 74)
        print("画质：画布直出 PNG（无编码损失） vs 成片解码帧，共 %d 帧" % len(frames))
        print("=" * 74)
        Z = {"右下角日期": (1700, 1042, 1840, 1070), "左下角档案号": (88, 1042, 500, 1070),
             "歌词区": (940, 130, 1830, 990), "标题区": (88, 150, 700, 300)}
        for f in frames:
            png = os.path.join(srcdir, "f%d.png" % f)
            tmp = os.path.join(srcdir, "_dec_%d.png" % f)
            subprocess.run([FFMPEG, "-y", "-v", "error", "-i", mp4,
                            "-vf", "select=eq(n\\,%d)" % f, "-vframes", "1", tmp],
                           capture_output=True)
            if not os.path.exists(tmp):
                print("  f%-6d 抽帧失败（成片可能不足该帧）" % f); continue
            s = np.asarray(Image.open(png).convert("RGB")).astype(np.float32)
            d = np.asarray(Image.open(tmp).convert("RGB")).astype(np.float32)
            print("  f%-6d 全画面 PSNR %5.2f dB" % (f, psnr(s, d)), end="")
            gs, gd = gray(s), gray(d)
            for n, (x0, y0, x1, y1) in Z.items():
                X0, Y0, X1, Y1 = x0 * 2, y0 * 2, x1 * 2, y1 * 2
                r = sharp(gd[Y0:Y1, X0:X1]) / max(sharp(gs[Y0:Y1, X0:X1]), 1e-9)
                print("  %s %3.0f%%" % (n, 100 * r), end="")
            print()
            os.remove(tmp)

    # ── 新旧对照 ──
    if old and os.path.exists(old):
        print("\n" + "=" * 74)
        print("新旧对照图（上=v8 1080P，下=v9 4K 同设计区域放大到同宽）")
        print("=" * 74)
        frames = [15000]
        for f in frames:
            tmp = os.path.join(os.path.dirname(mp4), "_old_%d.png" % f)
            subprocess.run([FFMPEG, "-y", "-v", "error", "-i", old,
                            "-vf", "select=eq(n\\,%d)" % f, "-vframes", "1", tmp],
                           capture_output=True)
            if not os.path.exists(tmp):
                continue
            from PIL import Image as I
            om = I.open(tmp).convert("RGB")
            nm = I.open(os.path.join(srcdir, "f%d.png" % f)).convert("RGB")
            for nmz, box in (("date", (1690, 1040, 1850, 1072)),
                             ("footer", (88, 1040, 560, 1072))):
                x0, y0, x1, y1 = box
                w, h = x1 - x0, y1 - y0
                a1 = om.crop(box).resize((w * 2, h * 2), I.LANCZOS)
                b1 = nm.crop((x0 * 2, y0 * 2, x1 * 2, y1 * 2)).resize((w * 2, h * 2), I.LANCZOS)
                cv = I.new("RGB", (w * 2, h * 4 + 6), (255, 0, 0))
                cv.paste(a1, (0, 0)); cv.paste(b1, (0, h * 2 + 6))
                p = os.path.join(os.path.dirname(mp4), "_v89_%s.png" % nmz)
                cv.save(p)
                print("  %s" % p)
            os.remove(tmp)

    print("\n结论：%s" % ("全部通过 ✓" if allok else "存在不符项 ✗"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
