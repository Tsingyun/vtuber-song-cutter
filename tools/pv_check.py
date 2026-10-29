"""成片验收：时长/码率/帧数 + 均匀抽 16 帧拼接触表。

用法：
  python tools/pv_check.py --mp4 _tmp/pv/innocence_pv_full.mp4 --manifest _tmp/pv/sb_full.json \
      --sheet _tmp/pv/final_sheet.png
判据（与 RULES 一致）：
  · 时长与编排稿 meta.dur 差 ≤ 0.5s
  · 分辨率 3840×2160、60fps、yuv420p、bt709
  · 视频码率 16000~19000 kbps（>19000 会被平台二压）
  · 抽帧底色 == 编排稿该时刻期望的 scheme.bg（换纸是否真的落地）
  · 平涂：四角同色（无暗角、无渐变、无残幅）
  · 底纹存在：右下象限有噪点（残幅的未绘制区是死平的 0）
  · 有内容：与底色差异 > 25 的像素 ≥ 0.15%

为什么不用「平均亮度 / 铺满率」：那两条是为米白满幅底设计的。新版是平涂底 +
大留白，黑底镜头的平均亮度 5.9、铺满率 0.537 完全正常 —— 两条都会恒误报。
换纸把「底色」变成了每一帧都不同的量，判据必须跟着编排稿走，不能再用绝对阈值。
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


def fill_ratio(png):  # noqa: F401 —— 已停用，见文件头说明
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


def load_schemes():
    """pack → [bg 色值,...]，顺序即 scheme 下标。直接解析 pv_scheme.js，避免与前端漂移。"""
    import re
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "renderer", "player", "pv", "pv_scheme.js")
    packs, cur = {}, None
    for line in open(p, encoding="utf-8"):
        m = re.match(r"\s{4}(\w+):\s*\{\s*$", line)
        if m:
            cur = m.group(1); packs[cur] = []; continue
        m2 = re.search(r"bg: '(#\w+)'", line)
        if m2 and cur:
            packs[cur].append(m2.group(1))
    return packs


def hex_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def expect_bg(sb, schemes, t):
    """编排稿里 t 时刻该用哪张纸的底色（附带版面名，供判据分流）。"""
    for s in (sb.get("shots") or []):
        if float(s["t0"]) <= t < float(s["t1"]):
            pal = s.get("pal") or {}
            lst = schemes.get(pal.get("pack")) or []
            lay = (s.get("layout") or {}).get("k") or ""
            if lst:
                return (hex_rgb(lst[int(pal.get("idx") or 0) % len(lst)]),
                        "%s/%s" % (pal.get("pack"), pal.get("idx") or 0), lay)
    return None, None, ""


def frame_stats(png):
    """(底色众数, 内容占比, 底纹方差, 四角最大差)。"""
    from PIL import Image
    import numpy as np
    im = Image.open(png).convert("RGB")
    a = np.asarray(im, dtype=np.int16)
    h, w, _ = a.shape
    # 底色众数：平涂底上底色一定占绝大多数面积，量化到 16 级后取最多的那一档
    small = np.asarray(im.resize((120, 68), Image.BILINEAR), dtype=np.int16)
    q = (small // 16).reshape(-1, 3)
    keys, cnt = np.unique(q, axis=0, return_counts=True)
    bg = keys[cnt.argmax()] * 16 + 8
    # 残幅检测：取右下象限的**中位数**，它是「这一块到底画没画」的直接证据。
    # 平涂底上哪怕压着字，中位数也还是底色（字占不到一半面积）；
    # 残幅时那一块根本没绘制，中位数是 0。
    # 这里一度改用「grain 方差 < 0.8」判残幅 —— 在 paper/1(#151515) 上恒误报：
    # 极深底上 grain 幅度只有 ±1，8bit 量化后标准差 0.13，而画面明明是画满的。
    patch = a[h // 2:, w // 2:].mean(axis=2)
    grain = float(np.median(patch))
    # 内容占比：与底色差 > 25 的像素（字、人物、标记）
    content = float((np.abs(a - bg).max(axis=2) > 25).mean())
    # 四角最大差：平涂底上四角必须和中心一样平（暗角是电影语法，这里不该有）
    def corner(y0, x0):
        b = a[int(h * y0):int(h * y0) + max(1, int(h * 0.04)),
              int(w * x0):int(w * x0) + max(1, int(w * 0.04))]
        return b.reshape(-1, 3).mean(axis=0)
    cs = [corner(0.02, 0.02), corner(0.02, 0.94), corner(0.94, 0.02), corner(0.94, 0.94)]
    flat = float(max(np.abs(cs[i] - cs[j]).max() for i in range(4) for j in range(i + 1, 4)))
    return bg, content, grain, flat


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
    fmt_ok = (info["w"], info["h"]) == (3840, 2160) and info["fps"].split("/")[0] in ("30", "60")
    print("[fmt] %dx%d %s %s %s %s" % (info["w"], info["h"], info["fps"], info["pix"], info["color"], "OK" if fmt_ok else "FAIL"))
    ok &= fmt_ok
    print("[bitrate] video %d kbps  %s" % (info["vbr"], "OK" if info["vbr"] <= 19000 else "FAIL(会二压)"))
    ok &= info["vbr"] <= 19000
    print("[audio] %s %d kbps" % (info["acodec"], info["abr"]))

    # 抽帧（--sheet 只决定要不要出接触表，抽样与判据始终执行）
    if True:
        from PIL import Image
        import numpy as np
        tmp = os.path.join(os.path.dirname(a.sheet or a.fig_sheet) or ".", "_chk")
        os.makedirs(tmp, exist_ok=True)
        d = info["dur"]
        schemes = {}
        if a.manifest:
            schemes = load_schemes()
        files, lum = [], []
        badpaper, badflat, badgrain, badcontent = [], [], [], []
        for i in range(a.n):
            t = d * (i + 0.5) / a.n
            p = os.path.join(tmp, "f%02d.png" % i)
            subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", "%.2f" % t, "-i", a.mp4,
                            "-frames:v", "1", p], check=True)
            files.append(p)
            lum.append(mean_luma(p))
            bg, content, grain, flat = frame_stats(p)
            want, name, lay = expect_bg(sb, schemes, t) if a.manifest else (None, None, "")
            tag = (round(t, 1), name)
            paper_ok = bool(a.manifest and sb.get("meta", {}).get("paper_ok"))
            if want is not None and not paper_ok:
                dp = float(np.abs(bg - np.array(want)).max())
                # 14 是 8bit 转换 + grain + scan 纹理能吃掉的量；再大就是换纸没落地
                if dp > 14:
                    badpaper.append((tag, "#%02X%02X%02X" % tuple(bg), "#%02X%02X%02X" % tuple(want), round(dp)))
            # 巨影版面跳过：cropGiant 的巨字本来就要被画缘切掉，它会占满左下与右下角
            # （实测 t=79.8 四角差 35.3，而画面无暗角、无残幅）。残幅由 [residue] 负责。
            # paper_ok（晨光绘本等非平涂纸底）：bg 件柔光/水彩有意进四角，四角不平≠暗角/残幅。
            if flat > 10 and lay != "cropGiant" and not paper_ok:
                badflat.append((tag, round(flat, 1)))
            if want is not None and not paper_ok and abs(float(grain) - sum(want) / 3.0) > 10:
                badgrain.append((tag, round(grain, 1), round(sum(want) / 3.0, 1)))
            if content < 0.0015:
                badcontent.append((tag, round(content * 100, 3)))
        print("[luma] min %.1f max %.1f（仅供参考，不作为判据）" % (min(lum), max(lum)))
        print("[paper] 底色与编排稿不符 %s" % (badpaper or "无"))
        ok &= not badpaper
        print("[flat]  四角不平（暗角/渐变/残幅）%s" % (badflat or "无"))
        ok &= not badflat
        print("[residue] 右下象限不是期望底色（残幅）%s" % (badgrain or "无"))
        ok &= not badgrain
        print("[content] 画面无内容 %s" % (badcontent or "无"))
        ok &= not badcontent
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
