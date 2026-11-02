# -*- coding: utf-8 -*-
"""pv_figcomp.py —— 把人物画框逐帧合成进「整帧文字序列」，产出可直接编码的全片帧序列。

这条工具解决的是**内存**问题，不是审美问题：
  pv_engine 的序列预载按镜头滑窗全解析（窗口 = 镜头时长 + 2.5s）。4K 一张解码位图
  33MB，30fps 下 5s 就是 150 张 ≈ 5GB，一条 20s 空档直接爆 —— 这就是「Blob OOM：
  渲染崩在 ~73%、主日志无错」的结构性成因。逐帧流式之后内存恒定在几十 MB。

输入：
  --spec   fig_spec.json：每段 {i, t0, t1, mode, side, win:[起点秒, 时长], seqs:{head,body}}
  --jz     整帧序列目录（j_%05d.jpg）+ jz.json（读 n / fps）
输出：
  --over   合成过的帧（只有十几秒 × 16 段，约 1500 张）
  --final  全片序列：有人物的帧链接 over/，其余链接原始序列 —— **全程不删文件**

窗形与几何见 GEO；人像一律双色调（去饱和 + 墨/纸两端上色）+ 硬边墨线，
读作「印刷品里切好的一块」（RULES §115 / §118）。

用法：
  python tools/pv_figcomp.py --spec _tmp/x/fig/fig_spec.json --jz _tmp/x/jz_full
      --over _tmp/x/mix_over --final _tmp/x/jz_final
  python tools/pv_figcomp.py ... --test      # 每段 3 张预览，不写全片
"""
import argparse
import io
import json
import os
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INK = (26, 26, 26)
W, H = 1920, 1080
S = 2                     # 设计坐标 → 4K 的倍率（1080 设计稿 ×2）
FPS = 30

GEO = {
    # w/h 是设计像素；x0 = 距安全边；split 走出血，单独算
    "disc":  dict(w=400, h=400, x0=76, cy=0.42),
    "arch":  dict(w=384, h=620, x0=76, cy=0.50),
    "band":  dict(w=451, h=600, x0=76, cy=0.30),
    "split": dict(w=576, h=1145, bleed=10),
}
def geo_of(mode, side):
    """返回 4K 像素 (x, y, w, h)。"""
    g = GEO[mode]
    w, h = g["w"] * S, g["h"] * S
    if mode == "split":
        y = -int(g["bleed"] * S)
        x = -int(g["bleed"] * S) if side == "l" else (W * S - w + int(g["bleed"] * S))
        return x, y, w, h
    y = int(round(g["cy"] * H * S - h / 2))
    x = int(round(g["x0"] * S)) if side == "l" else int(round((W - g["x0"]) * S - w))
    return x, y, w, h


def mask_of(mode, w, h):
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    if mode == "disc":
        d.ellipse((0, 0, w - 1, h - 1), fill=255)
    elif mode == "arch":
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=w // 2,
                            corners=(True, True, False, False), fill=255)
    else:
        d.rectangle((0, 0, w - 1, h - 1), fill=255)
    return m


def outline_of(mode, w, h, lw):
    """墨线 = 遮罩减去内核腐蚀一圈。不用 rounded_rectangle(width=)：
    拱窗的半径正好等于半宽，PIL 那条路径会因为 x1<x0 直接抛错。"""
    from PIL import ImageFilter
    m = mask_of(mode, w, h)
    k = max(3, lw * 2 + 1)
    inner = m.filter(ImageFilter.MinFilter(k))
    return ImageChops.subtract(m, inner)


def cover(im, w, h, zoom=1.0, fy=0.5):
    iw, ih = im.size
    k = max(w / float(iw), h / float(ih)) * zoom
    sw, sh = max(1, int(round(iw * k))), max(1, int(round(ih * k)))
    im = im.resize((sw, sh), Image.LANCZOS)
    x = (sw - w) // 2
    y = int((sh - h) * fy)
    return im.crop((x, y, x + w, y + h))


def duotone(im):
    """印在纸上的照片：去饱和 → 墨(34,31,29) 到纸(214,212,205) 两端上色 → 微提对比。"""
    g = im.convert("L")
    # 轻微提对比（幂 0.88）：直播截图本身打光很平，不去压暗部就会读成「一张灰照片」
    g = g.point(lambda v: max(0, min(255, int((v / 255.0) ** 0.88 * 238 + 4))))
    return ImageOps.colorize(g, black=(26, 24, 22), white=(212, 210, 203))


def seq_frame(sq, t_rel):
    """取序列里 t_rel 秒那张图（缺帧回落 ±0.25s，与引擎同义）。"""
    n = int(sq["n"])
    i = max(0, min(n - 1, int(round(t_rel * float(sq["fps"])))))
    p = os.path.join(ROOT, sq["dir"], sq["pattern"] % (i + 1))
    if os.path.isfile(p):
        return Image.open(p).convert("RGB")
    for d in range(1, max(3, int(float(sq["fps"]) * 0.25)) + 1):
        for j in (i - d, i + d):
            if 0 <= j < n:
                q = os.path.join(ROOT, sq["dir"], sq["pattern"] % (j + 1))
                if os.path.isfile(q):
                    return Image.open(q).convert("RGB")
    raise IOError("序列缺帧：%s" % p)


def sm(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def paint(base, spec, t):
    """把一段人物画进 base（RGB，原地改动并返回）。"""
    t0, t1 = spec["t0"], spec["t1"]
    a = sm((t - t0) / 0.55) * sm((t1 - t) / 0.55)
    if a <= 0.004:
        return base
    mode, side = spec["mode"], spec["side"]
    sq = spec["seqs"]["head" if mode == "disc" else "body"]
    x, y, w, h = geo_of(mode, side)

    im = seq_frame(sq, t - spec["win"][0])
    zoom = 1.06 + 0.05 * ((t - t0) / max(0.1, t1 - t0))
    if mode == "split":
        # 纵向分屏的宽高比是 1:2，而素材是 3:4 的脸部特写：硬铺（cover）会把脸放大一倍、
        # 下巴被框边切掉，读作「手机壁纸」而不是「版面里的一扇窗」。
        # 改成「纸上的照片」：分屏先铺纸色，肖像按 3:4 贴到底部，上面留出气口。
        tile = Image.new("RGB", (w, h), (198, 197, 192))
        pw = w - int(w * 0.055)
        ph = int(pw * 1.34)
        tile.paste(cover(duotone(im), pw, ph, zoom=zoom, fy=0.30),
                   ((w - pw) // 2, h - ph))
    else:
        tile = cover(duotone(im), w, h, zoom=zoom,
                     fy=0.40 if mode == "band" else 0.46)
    mask = mask_of(mode, w, h)

    # 圆窗/拱窗：前 0.6s 遮罩从 0.88 长到 1.0（不是硬贴上去）
    if t - t0 < 0.6 and mode in ("disc", "arch"):
        k = 0.88 + 0.12 * sm((t - t0) / 0.6)
        tw, th = int(w * k), int(h * k)
        tile = tile.resize((tw, th), Image.LANCZOS)
        mask = mask.resize((tw, th), Image.LANCZOS)
        x += (w - tw) // 2
        y += (h - th) // 2
        w, h = tw, th

    # 分屏/竖幅：从画面外滑进来（贴着它该在的那条边）
    dx = 0
    if mode in ("split", "band") and t - t0 < 0.55:
        q = sm((t - t0) / 0.55)
        dx = int((1 - q) * w * 0.16) * (-1 if side == "l" else 1)

    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    layer.paste(tile, (0, 0))
    lw = max(2, int(1.6 * S))
    edge = Image.new("RGBA", (w, h), INK + (255,))
    edge.putalpha(outline_of(mode, w, h, lw))
    layer.alpha_composite(edge)

    # 整体 alpha（入场/退场）× 形状遮罩，两个都要乘，不能用 composite 二选一
    alpha = ImageChops.multiply(layer.getchannel("A"), mask)
    if a < 0.999:
        alpha = alpha.point(lambda v: int(v * a))
    layer.putalpha(alpha)
    base.paste(layer, (x + dx, y), layer)
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="fig_spec.json")
    ap.add_argument("--jz", required=True, help="整帧序列目录（内含 jz.json）")
    ap.add_argument("--over", default="", help="合成帧输出目录（缺省 <jz>/../mix_over）")
    ap.add_argument("--final", default="", help="全片序列目录（缺省 <jz>/../jz_final）")
    ap.add_argument("--prev", default="", help="预览输出目录（缺省 <jz>/../prev）")
    ap.add_argument("--test", action="store_true", help="只出预览")
    a = ap.parse_args()

    jz = os.path.abspath(a.jz)
    over = os.path.abspath(a.over) if a.over else os.path.join(os.path.dirname(jz), "mix_over")
    final = os.path.abspath(a.final) if a.final else os.path.join(os.path.dirname(jz), "jz_final")
    prev = os.path.abspath(a.prev) if a.prev else os.path.join(os.path.dirname(jz), "prev")

    spec = json.load(io.open(a.spec, encoding="utf-8"))
    jm = json.load(io.open(os.path.join(jz, "jz.json"), encoding="utf-8"))
    n = int(jm["n"])
    print("[jz] %d 帧 @%gfps  %dx%d" % (n, jm["fps"], jm["w"], jm["h"]))

    if a.test:
        os.makedirs(prev, exist_ok=True)
        for sp in spec:
            t0, t1 = sp["t0"], sp["t1"]
            for k, t in enumerate([t0 + 0.4, (t0 + t1) / 2, t1 - 0.5]):
                f = int(round(t * FPS))
                base = Image.open(os.path.join(jz, "j_%05d.jpg" % (f + 1))).convert("RGB")
                paint(base, sp, t)
                base.save(os.path.join(prev, "f%02d_%d.jpg" % (sp["i"], k)), quality=92)
            print("[prev] f%02d %-5s %s" % (sp["i"], sp["mode"], sp["side"]), flush=True)
        print("→ %s" % prev)
        return

    os.makedirs(over, exist_ok=True)
    done = 0
    for sp in spec:
        f0 = max(0, int(round(sp["t0"] * FPS)))
        f1 = min(n - 1, int(round(sp["t1"] * FPS)))
        for i in range(f0, f1 + 1):
            base = Image.open(os.path.join(jz, "j_%05d.jpg" % (i + 1))).convert("RGB")
            paint(base, sp, i / float(FPS))
            base.save(os.path.join(over, "j_%05d.jpg" % (i + 1)), quality=95, subsampling=1)
            done += 1
        print("[mix] f%02d %-5s %s  %d 帧（%.2f~%.2fs）"
              % (sp["i"], sp["mode"], sp["side"], f1 - f0 + 1, sp["t0"], sp["t1"]), flush=True)

    # 拼装：有人物的帧取 over/，其余链接原始序列。
    # **全程不删文件**：沙箱对「一轮内删除 ≥50 个文件」是硬拦截（safe-delete），
    # 先铺链接再 os.remove 覆盖的写法跑到第 50 帧就被拦断，而脚本正常退出（RULES §118.4）。
    os.makedirs(final, exist_ok=True)
    linked = 0
    for i in range(1, n + 1):
        dst = os.path.join(final, "j_%05d.jpg" % i)
        if os.path.exists(dst):
            continue
        src = os.path.join(over, "j_%05d.jpg" % i)
        if not os.path.exists(src):
            src = os.path.join(jz, "j_%05d.jpg" % i)
        os.link(src, dst)
        linked += 1
    print("[mix] 合成 %d 帧 / 全片 %d 帧；final 新链接 %d 帧（共 %d）"
          % (done, n, linked, len(os.listdir(final))))


if __name__ == "__main__":
    main()
