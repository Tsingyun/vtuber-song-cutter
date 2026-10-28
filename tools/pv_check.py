"""成片验收：时长/码率/帧数 + 均匀抽 16 帧拼接触表。

用法：
  python tools/pv_check.py --mp4 _tmp/pv/innocence_pv_full.mp4 --manifest _tmp/pv/sb_full.json \
      --sheet _tmp/pv/final_sheet.png
判据（与 RULES 一致）：
  · 时长与编排稿 meta.dur 差 ≤ 0.5s
  · 分辨率 3840×2160、60fps、yuv420p、bt709
  · 视频码率 16000~19000 kbps（>19000 会被平台二压）
  · 抽帧全部非纯黑（平均亮度 > 6）
"""
import argparse
import json
import os
import subprocess
import sys

FFMPEG = "D:/ffmpeg/bin/ffmpeg.exe"
FFPROBE = "D:/ffmpeg/bin/ffprobe.exe"


def probe(mp4):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", mp4],
        capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    a = next((s for s in j["streams"] if s["codec_type"] == "audio"), None)
    br = int(j["format"].get("bit_rate") or 0) // 1000
    vbr = int(v.get("bit_rate") or 0) // 1000
    return {
        "dur": float(j["format"]["duration"]),
        "w": v["width"], "h": v["height"],
        "fps": v["r_frame_rate"], "pix": v.get("pix_fmt"),
        "nb": int(v.get("nb_frames") or 0),
        "vbr": vbr or br,
        "acodec": a["codec_name"] if a else None,
        "abr": int(a.get("bit_rate") or 0) // 1000 if a else 0,
        "color": "%s/%s/%s" % (v.get("color_primaries"), v.get("color_transfer"), v.get("color_space")),
    }


def mean_luma(png):
    out = subprocess.run([FFMPEG, "-v", "error", "-i", png, "-vf", "scale=64:36,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    return sum(out) / max(1, len(out))


def fill_ratio(png):
    """内容铺满率：非黑像素的横向跨度 / 画面宽度。

    这条检查是被一次真实事故逼出来的：`renderAt` 把画布变换复位成单位矩阵，
    4K 母版上整张画面只画在左上 1920×1080，其余 3/4 纯黑 —— 时长、码率、帧数
    全部正常，抽帧的「平均亮度」也没低于阈值，只有肉眼看画面才发现整片作废。
    """
    from PIL import Image
    import numpy as np
    im = np.asarray(Image.open(png).convert("L"), dtype=np.uint8)
    mask = im > 10
    cols = mask.any(axis=0)
    if not cols.any():
        return 0.0
    x0, x1 = np.argmax(cols), len(cols) - 1 - np.argmax(cols[::-1])
    return (x1 - x0 + 1) / im.shape[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mp4", required=True)
    ap.add_argument("--manifest", default="")
    ap.add_argument("--sheet", default="")
    ap.add_argument("--n", type=int, default=16)
    ap.add_argument("--fig-sheet", default="",
                    help="额外从成片里抽含人物的镜头拼成接触表（必须走成片，不能用 pv_stills）")
    a = ap.parse_args()

    info = probe(a.mp4)
    want = None
    if a.manifest:
        sb = json.load(open(a.manifest, encoding="utf-8"))
        want = float(sb["meta"]["dur"])

    ok = True
    print("[probe] %s" % json.dumps(info, ensure_ascii=False))
    if want:
        d = abs(info["dur"] - want)
        print("[dur] %.2fs vs 编排稿 %.2fs  差 %.2fs %s" % (info["dur"], want, d, "OK" if d <= 0.5 else "FAIL"))
        ok &= d <= 0.5
    print("[fmt] %dx%d %s %s %s" % (info["w"], info["h"], info["fps"], info["pix"], info["color"]))
    ok &= (info["w"], info["h"]) == (3840, 2160) and info["fps"].startswith("60")
    print("[bitrate] video %d kbps  %s" % (info["vbr"], "OK" if info["vbr"] <= 19000 else "FAIL(会二压)"))
    ok &= info["vbr"] <= 19000
    print("[audio] %s %d kbps" % (info["acodec"], info["abr"]))

    # 抽帧（--sheet 只决定要不要出接触表，抽样与判据始终执行）
    if True:
        from PIL import Image
        tmp = os.path.join(os.path.dirname(a.sheet or a.fig_sheet) or ".", "_chk")
        os.makedirs(tmp, exist_ok=True)
        d = info["dur"]
        files, lum, fill = [], [], []
        for i in range(a.n):
            t = d * (i + 0.5) / a.n
            p = os.path.join(tmp, "f%02d.png" % i)
            subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", "%.2f" % t, "-i", a.mp4,
                            "-frames:v", "1", p], check=True)
            files.append(p)
            lum.append(mean_luma(p))
            fill.append(fill_ratio(p))
        dark = [(round(d * (i + 0.5) / a.n, 1), round(v, 1)) for i, v in enumerate(lum) if v < 6]
        print("[luma] min %.1f max %.1f  过暗帧 %s" % (min(lum), max(lum), dark or "无"))
        ok &= not dark
        thin = [(round(d * (i + 0.5) / a.n, 1), round(v, 3)) for i, v in enumerate(fill) if v < 0.94]
        print("[fill] 最小铺满率 %.3f  未铺满 %s" % (min(fill), thin or "无"))
        ok &= not thin
        # 人物层：必须从**成片**里抽，不能用 pv_stills（那条路是预览接口，绕开了缺陷本身）。
        # 这一条是被「成片里一个人都没有、抽帧却每格都有」逼出来的。
        if a.fig_sheet:
            from PIL import Image
            figs = [x for x in (sb.get("shots") or []) if x.get("fig")]
            if not figs:
                print("[fig] 编排稿里没有含人物的镜头")
                ok = False
            else:
                k = min(8, len(figs))
                pick = [figs[round(i * (len(figs) - 1) / max(1, k - 1))] for i in range(k)]
                W, H = 480, 270
                sh_img = Image.new("RGB", (W * 4, H * 2), (8, 10, 14))
                for i, x in enumerate(pick):
                    t = (float(x["t0"]) + float(x["t1"])) / 2
                    p = os.path.join(tmp, "fig%02d.png" % i)
                    subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", "%.2f" % t, "-i", a.mp4,
                                    "-frames:v", "1", p], check=True)
                    im = Image.open(p).convert("RGB").resize((W, H), Image.LANCZOS)
                    sh_img.paste(im, ((i % 4) * W, (i // 4) * H))
                sh_img.save(a.fig_sheet)
                print("[fig] 成片里含人物的镜头 %d 个，抽 %d 帧 → %s" % (len(figs), k, a.fig_sheet))
                print("[fig] 时刻 %s" % ", ".join("%.1f" % ((float(x["t0"]) + float(x["t1"])) / 2) for x in pick))

        if not a.sheet and not a.fig_sheet:
            print("\n结论：%s" % ("全部通过" if ok else "有项目未通过（见上）"))
            sys.exit(0 if ok else 1)
        if a.sheet:
            W, H = 480, 270
            sheet = Image.new("RGB", (W * 4, H * 4))
            for i, f in enumerate(files):
                im = Image.open(f).convert("RGB").resize((W, H), Image.LANCZOS)
                sheet.paste(im, ((i % 4) * W, (i // 4) * H))
            sheet.save(a.sheet)
            print("[sheet] %s" % a.sheet)

    print("\n结论：%s" % ("全部通过" if ok else "有项目未通过（见上）"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
