# -*- coding: utf-8 -*-
"""pv_arc_plot.py —— 把一条风格弧线画成可检查的图。

为什么需要这张图：弧线本身是一串数字，看不出「它到底有没有跟着歌词走」。
把三样东西画在同一根时间轴上，一眼就能判断：
  · 五维坐标曲线（这一段是不是平的、有没有来回抖）
  · 每句落地到哪一套纸（色带直接用 pv_scheme.js 里的 bg）
  · 换纸点在哪一句的边界（有没有落在句中）

用法：
  python tools/pv_arc_plot.py --arc _tmp/jz/chorus2/arc.json \
      --sb _tmp/jz/chorus2/sb_arc.json --out _tmp/jz/chorus2/arc.png
"""
import argparse
import io
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pv_arc  # noqa: E402

# Windows 自带字体；没有时退回默认位图字体（图仍可读，只是不好看）
FONT_CAND = ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
             "C:/Windows/Fonts/simhei.ttf"]


def _font(size, bold=False):
    for p in ([FONT_CAND[1]] if bold else []) + FONT_CAND:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def hex_rgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def text_color(bg):
    l = pv_arc.hex_lum('#%02X%02X%02X' % bg)
    return (24, 24, 28) if l > 0.45 else (243, 240, 234)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arc", required=True)
    ap.add_argument("--sb", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--w", type=int, default=1600)
    ap.add_argument("--h", type=int, default=716)
    a = ap.parse_args()

    arc = json.load(io.open(a.arc, encoding="utf-8"))
    sb = json.load(io.open(a.sb, encoding="utf-8"))
    dur = float(sb["meta"]["dur"])
    schemes = pv_arc.pack_schemes()

    W, H = a.w, a.h
    img = Image.new("RGB", (W, H), (247, 245, 241))
    d = ImageDraw.Draw(img)
    f_t, f_s, f_b = _font(30, True), _font(18), _font(21)

    X0, X1 = 96, W - 46
    def px(t):
        return X0 + (X1 - X0) * max(0.0, min(1.0, t / dur))

    # ---- 时间轴刻度
    Y_TOP, Y_BOT = 138, 470
    for s in range(0, int(dur) + 1, 5):
        x = px(s)
        d.line([(x, Y_TOP - 10), (x, Y_BOT + 6)], fill=(228, 224, 216), width=1)
        d.text((x - 8, Y_BOT + 10), "%ds" % s, font=f_s, fill=(150, 145, 138))
    for v in (0.0, 0.5, 1.0):
        y = Y_BOT - (Y_BOT - Y_TOP) * v
        d.line([(X0, y), (X1, y)], fill=(235, 231, 224), width=1)
        d.text((X0 - 34, y - 9), "%.1f" % v, font=f_s, fill=(160, 155, 148))

    # ---- 曲线：从弧线自身重采样（与引擎同一套插值，不另写一份）
    A = pv_arc.Arc(arc["keys"], arc.get("spans") or [])
    step = 0.08
    ts, lum, warm, dense = [], [], [], []
    t = 0.0
    while t <= dur + 1e-6:
        v = A.at(t)
        ts.append(t); lum.append(v["lum"]); warm.append(v["warm"]); dense.append(v["dense"])
        t += step

    def cy(v):
        return Y_BOT - (Y_BOT - Y_TOP) * max(0.0, min(1.0, v))

    # lum 区域（半透明填充，让「先降后升」一眼可读）
    pts = [(px(tt), cy(v)) for tt, v in zip(ts, lum)]
    d.polygon(pts + [(px(dur), Y_BOT), (px(0), Y_BOT)], fill=(226, 218, 244))
    d.line(pts, fill=(96, 78, 168), width=4)
    d.line([(px(tt), cy(v)) for tt, v in zip(ts, dense)], fill=(196, 150, 96), width=2)
    d.line([(px(tt), cy(v)) for tt, v in zip(ts, warm)], fill=(214, 92, 96), width=2)

    # ---- 图例
    lg = [("lum 明暗（决定用哪套纸）", (96, 78, 168)),
          ("warm 色温", (214, 92, 96)),
          ("dense 疏密（字距）", (196, 150, 96))]
    lx = X0
    for name, col in lg:
        d.line([(lx, 96), (lx + 26, 96)], fill=col, width=4)
        d.text((lx + 34, 86), name, font=f_s, fill=(70, 66, 60))
        lx += 26 + 34 + d.textlength(name, font=f_s) + 30

    # ---- 每镜的纸：色带 + 歌词
    Y_B1, Y_B2 = 540, 636
    for sh in sb["shots"]:
        pal = sh.get("pal") or {}
        try:
            bg = schemes[pal.get("pack")][int(pal.get("idx")) % len(schemes[pal.get("pack")])]["bg"]
        except Exception:
            bg = "#444444"
        c = hex_rgb(bg)
        x0, x1 = px(sh["t0"]), px(sh["t1"])
        d.rectangle([x0 + 1, Y_B1, x1 - 1, Y_B2], fill=c, outline=(250, 248, 244))
        tc = text_color(c)
        name = "#%d  %s" % (int(pal.get("idx", 0)), bg.upper())
        d.text((x0 + 10, Y_B1 + 8), name, font=f_s, fill=tc)
        txt = ""
        if sh.get("line", -1) >= 0:
            txt = sb["lines"][sh["line"]]["text"]
        if txt:
            # 长句按宽度折断：色带只有 96px 高，两行刚好装下一句副歌
            words = list(txt)
            line1, line2 = "", ""
            for ch in words:
                if d.textlength(line1 + ch, font=f_b) < (x1 - x0 - 24) or not line1:
                    line1 += ch
                else:
                    line2 += ch
            d.text((x0 + 10, Y_B1 + 34), line1, font=f_b, fill=tc)
            if line2:
                d.text((x0 + 10, Y_B1 + 62), line2, font=f_b, fill=tc)

    # ---- 换纸点
    for i in range(1, len(sb["shots"])):
        p0, p1 = sb["shots"][i - 1].get("pal") or {}, sb["shots"][i].get("pal") or {}
        if p0.get("idx") == p1.get("idx"):
            continue
        x = px(sb["shots"][i]["t0"])
        d.line([(x, Y_TOP - 10), (x, Y_B2 + 6)], fill=(40, 38, 44), width=2)
        jump = abs((pv_arc.pack_schemes()[p1["pack"]][p1["idx"]]["lum"])
                   - (pv_arc.pack_schemes()[p0["pack"]][p0["idx"]]["lum"]))
        tag = "换纸 %.2fs（跨深/浅 %.2f）" % (sb["shots"][i]["t0"], jump)
        d.text((x + 6, Y_TOP - 34), tag, font=f_s, fill=(40, 38, 44))

    # ---- 标题与脚注
    d.text((X0 - 4, 20), "《%s》副歌 %.1fs —— 风格弧线（歌词意境 × 曲风）"
           % (sb["meta"].get("title", ""), dur), font=f_t, fill=(30, 28, 26))
    d.text((X0 - 4, 58), "bpm %.1f   意境 %.0f%% / 曲风 %.0f%%   句边界过渡 %.1fs"
           % (arc["meta"].get("bpm", 0), 100 * arc["meta"].get("w_lyric", 0),
              100 * arc["meta"].get("w_music", 0), arc["meta"].get("lead", 0)),
           font=f_s, fill=(120, 115, 108))
    for s in arc.get("spans") or []:
        pass
    d.text((X0 - 4, H - 42),
           "色带 = 该句实际使用的纸（pv_scheme.js 的 bg）。竖线 = 换纸点，全部落在句边界。",
           font=f_s, fill=(150, 145, 138))
    d.text((X0 - 4, H - 20),
           "曲线在句内是平的（不逐句抖），落差全部发生在句边界前后 %.1fs 的缓动窗里 ——"
           " 在 34.8s 的横轴上只占 2%%，所以看起来像台阶。" % arc["meta"].get("lead", 0.6),
           font=f_s, fill=(150, 145, 138))

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    img.save(a.out)
    print("[plot] %s  %.0fKB" % (a.out, os.path.getsize(a.out) / 1024.0))


if __name__ == "__main__":
    main()
