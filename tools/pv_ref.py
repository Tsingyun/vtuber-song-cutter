# -*- coding: utf-8 -*-
"""参考视频 → 视觉风格档案（VSP, Visual Style Profile）

为什么要这个文件
----------------
现有 PV 引擎的「风格」是人手写的：一段一个 style pack、几档字号、一张意象表
（`docs/<歌>.json`）。想学一支日式手书 PV 的语汇，过去只能靠人眼看、手抄参数。

这里把「看片」变成可复现的测量：抽帧 → 逐帧量 → 按镜头聚合 → 落成
`styles/<id>.vsp.json`，再由 `pv_direct.py --style` 消费。

三条纪律
--------
1. **不许猜**：每个量都写明它是怎么算出来的（见 `METRIC_DOC`）。启发式估不准的
   （人物边界、字重、字距）必须带 `confidence`，低于阈值只作「建议」。
2. **不许随机**：KMeans 固定 `random_state`；不抽样、不用时间戳做种。同一视频
   跑两次必须得到同一份档案（`--verify` 自检）。
3. **不许把时间丢掉**：每帧都带 `t`，镜头边界、转场、节奏全部按秒记录；后续
   动画只能读这套时间，不允许「按歌词行数平均分」。

用法
----
    python tools/pv_ref.py fetch   <url|本地文件>            # 下载 + 元信息
    python tools/pv_ref.py analyze <url|id|mp4> [--verify]   # 出档案
    python tools/pv_ref.py all     <url>                     # fetch + analyze
    python tools/pv_ref.py report  styles/<id>.vsp.json      # 人读版摘要

产物
----
    _tmp/ref/<id>.mp4            下载的视频（_tmp 已 gitignore）
    styles/<id>.vsp.json         风格档案（入库，可复用）
    styles/<id>.md               人读版摘要（入库，便于 diff 与评审）
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile

import numpy as np

# 运行时路径只有一份：songcut/pvlink.runtime()（env → config.json → 内置探测）。
# 写死的 D:/ffmpeg/bin/... 换台机器就得全局替换，漏一处就是「找不到 ffmpeg」。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from songcut import pvlink as _PVRT  # noqa: E402
_RT = _PVRT.runtime()


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FFMPEG = _RT["ffmpeg"]
FFPROBE = _RT["ffprobe"]
STYLES = os.path.join(ROOT, "styles")
CACHE = os.path.join(ROOT, "_tmp", "ref")

# 分析分辨率。太高慢，太低字测不准；640x360 在 1080p 源上字高仍有 10~30px。
AW, AH = 640, 360

# 引擎实际有的 13 个转场（见 renderer/player/pv/pv_trans.js），按两族分。
# 参考视频的转场分类器只认得出「淡变 / 闪白 / 硬切」三态，认不出「这是 iris 还是 mask」；
# 所以风格层只把池子收窄到**族**，族内选谁仍交给语气与抽卡 —— 否则一支片看下来
# 每处转场都长得一样，那不是「学到了风格」，是「把变化锁死了」。
TRANS_SOFT = ["dissolve", "grad", "mask", "ink"]
TRANS_GEOM = ["wipe", "slice", "push", "blocks", "zoom", "clock", "blinds", "iris"]
ALL_TRANS = set(TRANS_SOFT) | set(TRANS_GEOM) | {"flash"}
# 渐变比例很高时不是自动等于「溶解」——擦除也是多帧渐变。靠 `spread` 分辨：
# 溶解时整幅同时变化（各块差异接近），擦除/推挤时差异集中在一条推进的边界上
# （各块差异悬殊）。实测参考片 spread≈0.35，肉眼抽帧确认是均匀叠化。


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------
def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, **kw)
    return p


def probe(path):
    p = run([FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path])
    if p.returncode != 0:
        raise RuntimeError("ffprobe 失败: " + p.stderr.decode("utf8", "ignore")[:400])
    d = json.loads(p.stdout.decode("utf8", "ignore"))
    v = next(s for s in d["streams"] if s.get("codec_type") == "video")
    num, den = (v.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den or 1)
    dur = float(d["format"].get("duration") or 0)
    return {
        "w": int(v["width"]), "h": int(v["height"]), "fps": round(fps, 3),
        "dur": round(dur, 3), "nb": int(v.get("nb_frames") or 0),
        "codec": v.get("codec_name"), "path": path,
    }


def slug(s):
    keep = []
    for ch in s.lower():
        if ch.isalnum():
            keep.append(ch)
        elif keep and keep[-1] != "-":
            keep.append("-")
    return "".join(keep).strip("-")[:48] or "ref"


# ----------------------------------------------------------------------------
# 1) 取片
# ----------------------------------------------------------------------------
def fetch(src, max_h=1080):
    """下载参考视频。只走 yt-dlp；失败时允许直接给本地文件。"""
    os.makedirs(CACHE, exist_ok=True)
    if os.path.exists(src):
        info = probe(src)
        rid = slug(os.path.splitext(os.path.basename(src))[0])
        # 本地路径没有标题/UP，档案里就只剩一条本机绝对路径 —— 换个目录就断了溯源。
        # 若同一个 id 之前是按链接拉的，把那次留下来的 meta 伴生文件接回来。
        source = {"kind": "file", "ref": src}
        side = os.path.join(CACHE, rid + ".meta.json")
        if os.path.exists(side):
            try:
                d = json.load(io.open(side, encoding="utf8"))
                d["kind"], d["ref"], d["sidecar"] = "file", src, True
                source = d
            except (ValueError, OSError):
                pass
        return {"id": rid, "path": src, "meta": info, "source": source}

    ytdlp = os.environ.get("SONGCUT_YTDLP") or _find_ytdlp()
    if not ytdlp:
        raise RuntimeError("找不到 yt-dlp（pip install -U yt-dlp），或改用本地文件路径")
    # 先拿元信息（不下载），用标题做 id，便于人认
    p = run([ytdlp, "--dump-json", "--no-download", src])
    if p.returncode != 0:
        raise RuntimeError("yt-dlp 元信息失败: " + p.stderr.decode("utf8", "ignore")[-500:])
    meta0 = json.loads(p.stdout.decode("utf8", "ignore").splitlines()[0])
    rid = slug(meta0.get("id") or meta0.get("title") or "ref")

    out = os.path.join(CACHE, rid + ".mp4")
    if not os.path.exists(out):
        fmt = "bv*[height<=%d]+ba/b[height<=%d]" % (max_h, max_h)
        p = run([ytdlp, "-f", fmt, "--merge-output-format", "mp4",
                 "-o", out, src])
        if p.returncode != 0:
            raise RuntimeError("yt-dlp 下载失败: " + p.stderr.decode("utf8", "ignore")[-800:])
    info = probe(out)
    source = {
        "kind": "url", "ref": src,
        "title": meta0.get("title"), "uploader": meta0.get("uploader"),
        "uploader_id": meta0.get("uploader_id"),
        "desc": (meta0.get("description") or "")[:400],
        "tags": (meta0.get("tags") or [])[:12],
    }
    # 留一份 meta 伴生文件：以后拿本地 mp4 重跑时还能认出「这是哪支片子」。
    try:
        with io.open(os.path.join(CACHE, rid + ".meta.json"), "w",
                     encoding="utf8", newline="\n") as f:
            json.dump(source, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return {"id": rid, "path": out, "meta": info, "source": source}


def _find_ytdlp():
    for c in ("yt-dlp", "yt-dlp.exe"):
        try:
            p = subprocess.run([c, "--version"], capture_output=True)
            if p.returncode == 0:
                return c
        except FileNotFoundError:
            pass
    guess = os.path.join(os.path.dirname(sys.executable), "Scripts", "yt-dlp.exe")
    return guess if os.path.exists(guess) else None


# ----------------------------------------------------------------------------
# 2) 镜头切分
# ----------------------------------------------------------------------------
def detect_shots(path, thr=0.28, dur=0.0):
    """用 ffmpeg 的 scene 分数找切点。

    scene 分数是「帧间直方图变化归一化」，对硬切很灵，对渐变（淡化/闪白）不灵 ——
    这是缺陷也是特征：我们先只拿它切硬点，渐变转场留给 `classify_trans()` 去分辨。
    阈值自适应：太稀（<8 个）就降到 0.18，太密（>240 个）就抬到 0.40。
    """
    for t in (thr, 0.18, 0.40):
        p = run([FFMPEG, "-hide_banner", "-nostats", "-i", path,
                 "-vf", "select='gt(scene,%s)',showinfo" % t, "-f", "null", os.devnull])
        txt = p.stderr.decode("utf8", "ignore")
        ts = []
        for line in txt.splitlines():
            i = line.find("pts_time:")
            if i > 0:
                try:
                    ts.append(float(line[i + 9:].split()[0]))
                except ValueError:
                    pass
        if 8 <= len(ts) <= 240:
            return sorted(ts), t
    return sorted(ts) if "ts" in dir() else [], thr


def build_shots(path, dur, thr=0.28):
    ts, used = detect_shots(path, thr, dur)
    cuts = [0.0] + [t for t in ts if 0.15 < t < dur - 0.15] + [dur]
    shots = []
    for i in range(len(cuts) - 1):
        a, b = cuts[i], cuts[i + 1]
        if b - a < 0.08:
            continue
        shots.append({"i": len(shots), "t0": round(a, 3), "t1": round(b, 3),
                      "dur": round(b - a, 3)})
    return shots, used


def crop_rect(path):
    """用 ffmpeg 自己的 cropdetect 求「有效画面矩形」。

    为什么不用自己数黑边：黑边与「深色 design 带」在像素上没有区别，启发式阈值
    必然在两者之间犯难（实测 6.1% vs 真值 9.3%）。cropdetect 是逐帧解出来的众数，
    又快又准，而且天然给出上下左右四边。
    """
    p = run([FFMPEG, "-hide_banner", "-nostats", "-i", path,
             "-vf", "cropdetect=24:2:0", "-f", "null", os.devnull])
    txt = p.stderr.decode("utf8", "ignore")
    from collections import Counter
    c = Counter()
    for mm in re.finditer(r"crop=(\d+):(\d+):(\d+):(\d+)", txt):
        c[tuple(int(x) for x in mm.groups())] += 1
    if not c:
        return None
    (w, h, x, y), _n = c.most_common(1)[0]
    return {"w": int(w), "h": int(h), "x": int(x), "y": int(y), "votes": int(_n),
            "total": int(sum(c.values()))}


# ----------------------------------------------------------------------------
# 3) 逐帧度量
# ----------------------------------------------------------------------------
METRIC_DOC = {
    "lum": "亮度均值 /255",
    "contrast": "亮度标准差 /255（同时用作「画面平不平」的判据）",
    "sat": "HSV 饱和度均值（max-min)/max",
    "edge": "Sobel 幅值 > p95 的像素占比（信息密度）",
    "text_h": "疑似文字连通域高度中位数 /画高（=字号代理量）",
    "text_lines": "疑似文字行数（按 y 重叠合并连通域）",
    "text_area": "疑似文字总面积 /画面积",
    "text_align": "文字包围盒中心相对画面的位置：left/center/right × top/mid/bottom",
    "letterbox": "纯黑行/列占画高的比例（上下黑边之和）",
    "grain": "高通残差在平坦区的标准差（胶片颗粒代理）",
    "halftone": "径向频谱 1/8~1/2 奈奎斯特频段的峰值比（网点/排线）",
    "scanline": "行均值序列的周期性峰（扫描线）",
    "rgb_split": "R 与 B 列均值互相关的水平错位（像素，0=无套色错位）",
    "bloom": "亮度 >240 的像素占比（光晕/泛光）",
    "subject": "与画面主色距离 > 阈值的最大连通域面积占比（主体代理量）",
    "subject_cx": "主体质心 x（0..1）",
    "motion": "相邻帧灰度相位相关的位移模长 /画宽（相机运动代理）",
    "sat_max": "饱和度 p95",
}


def _to_frame(buf):
    return np.frombuffer(buf, dtype=np.uint8).reshape(AH, AW, 3)


def _gray(rgb):
    g = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    return g


def _text_boxes(gray):
    """疑似文字连通域。不依赖 cv2，用 Sobel + 形态学膨胀 + 连通域标记。

    判据（在 640x360 上标定，对应 1080p 字高 30~90px）：
      高度 6~44px、宽高比 ≥1.1、填充率 0.12~0.72、宽度 ≤ 0.5W
    这些是「笔画块」的形状特征；纯色块填充率高、大图形太宽，都会被滤掉。
    """
    from scipy import ndimage as ndi
    gx = ndi.sobel(gray, axis=1)
    gy = ndi.sobel(gray, axis=0)
    mag = np.hypot(gx, gy)
    th = np.percentile(mag, 97)
    m = mag > max(th, 6.0)
    m = ndi.binary_dilation(m, np.ones((3, 3), bool), iterations=2)
    lab, n = ndi.label(m)
    if n == 0:
        return []
    objs = ndi.find_objects(lab)
    boxes = []
    for i, sl in enumerate(objs, 1):
        if sl is None:
            continue
        ys, xs = sl
        h = ys.stop - ys.start
        w = xs.stop - xs.start
        if not (6 <= h <= 44):
            continue
        if w < 4 or w > AW * 0.5:
            continue
        if w / max(1, h) < 1.1:
            continue
        area = (lab[sl] == i).sum()
        fill = area / float(max(1, w * h))
        if not (0.12 <= fill <= 0.72):
            continue
        boxes.append((xs.start, ys.start, w, h))
    return boxes


def _group_lines(boxes):
    """把笔画块按 y 重叠聚成行，返回 (bbox, n_boxes)。"""
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: b[1])
    lines = []
    for b in boxes:
        x, y, w, h = b
        cy = y + h / 2.0
        for ln in lines:
            if abs(cy - ln["cy"]) < max(h, ln["h"]) * 0.55:
                ln["x0"] = min(ln["x0"], x); ln["y0"] = min(ln["y0"], y)
                ln["x1"] = max(ln["x1"], x + w); ln["y1"] = max(ln["y1"], y + h)
                ln["n"] += 1
                ln["cy"] = (ln["y0"] + ln["y1"]) / 2.0
                ln["h"] = ln["y1"] - ln["y0"]
                break
        else:
            lines.append({"x0": x, "y0": y, "x1": x + w, "y1": y + h,
                          "cy": cy, "h": h, "n": 1})
    return [l for l in lines if l["n"] >= 2]


def _bands(rgb):
    """画幅内的「条」：上/下/左/右四条可能是黑边，也可能是 design 的深色带。

    判据必须**自适应**：某一行（列）的 99 分位亮度低于全画中位亮度的 40%、且连续
    长度 ≥1.5% 画高，才算一条。用绝对阈值会被「整片深色 PV」骗到 —— 参考片底色
    本身就接近黑，绝对阈值会把整幅判成黑边，进而让配色采样全变成 #020304。
    """
    g = _gray(rgb)
    med = float(np.median(g)) + 1e-6
    rowp = np.percentile(g, 99, axis=1)
    colp = np.percentile(g, 99, axis=0)
    thr = max(10.0, med * 0.40)

    def run(vec, n):
        k = 0
        while k < n and vec[k] < thr:
            k += 1
        return k

    top = run(rowp, AH); bot = run(rowp[::-1], AH)
    left = run(colp, AW); right = run(colp[::-1], AW)
    mh = max(2, int(0.015 * AH))
    mw = max(2, int(0.015 * AW))
    return {"top": (top if top >= mh else 0) / AH,
            "bottom": (bot if bot >= mh else 0) / AH,
            "left": (left if left >= mw else 0) / AW,
            "right": (right if right >= mw else 0) / AW}


def _grain(gray):
    from scipy import ndimage as ndi
    hp = gray - ndi.gaussian_filter(gray, 1.4)
    flat = ndi.gaussian_filter(np.abs(ndi.sobel(gray, 0)), 3) < 3.0
    if flat.sum() < 500:
        return 0.0
    return float(np.std(hp[flat]) / 12.0)


def _halftone(gray):
    """径向频谱：网点/排线在 1/8~1/2 奈奎斯特处有孤立峰。"""
    g = gray - gray.mean()
    win = np.outer(np.hanning(AH), np.hanning(AW))
    F = np.abs(np.fft.fftshift(np.fft.fft2(g * win)))
    yy, xx = np.mgrid[0:AH, 0:AW]
    r = np.hypot(yy - AH / 2.0, xx - AW / 2.0) / (min(AH, AW) / 2.0)
    band = (r > 0.25) & (r < 0.95)
    if not band.any():
        return 0.0
    v = F[band]
    if v.size == 0:
        return 0.0
    return float(np.percentile(v, 99.5) / (np.median(v) + 1e-6) / 60.0)


def _scanline(gray):
    """行周期纹理（扫描线 / CRT）。判据：行均值谱在周期 1.8~6.5px 处有显著孤立峰。

    旧版用「峰值占比 ×6」归一，任何片子都逼近 1.0（实测 0.88 假阳性）；
    现在改成看**周期是否落在扫描线的物理区间**、以及与谱中位数的**突出比**。
    """
    col = gray.mean(axis=1)
    col = col - col.mean()
    if np.allclose(col, 0):
        return 0.0
    sp = np.abs(np.fft.rfft(col))[1:]
    if sp.size < 4:
        return 0.0
    k = int(np.argmax(sp)) + 1
    per = AH / float(k)
    if not (1.8 <= per <= 6.5):
        return 0.0
    prom = float(sp[k - 1] / (np.median(sp) + 1e-6))
    return float(min(1.0, max(0.0, (prom - 4.0) / 20.0)))


def _rgb_split(rgb):
    """R 与 B 的列均值互相关，找 ±6px 内的水平错位（套色错位/RGB 色散）。"""
    r = rgb[:, :, 0].mean(axis=0); b = rgb[:, :, 2].mean(axis=0)
    r = r - r.mean(); b = b - b.mean()
    best, bestv = 0, -1
    for s in range(-6, 7):
        if s < 0:
            a, c = r[-s:], b[:AW + s]
        elif s > 0:
            a, c = r[:AW - s], b[s:]
        else:
            a, c = r, b
        v = float(np.dot(a, c) / (np.linalg.norm(a) * np.linalg.norm(c) + 1e-9))
        if v > bestv:
            bestv, best = v, s
    return abs(best)


def _saliency(rgb):
    """谱残差显著性（Hou & Zhang 2007）。纯 numpy/scipy，不依赖 cv2。

    为什么换掉「与主色距离」那版：参考片的背景本身就是大面积星芒渐变，
    用「离主色远」当日判据会把整幅判成主体 —— 实测 coverage 只有 0.015、
    出镜率 22%，与肉眼（几乎每个镜头都有角色）完全相反。
    谱残差只看「哪里意外」（对数幅度谱的局部均值残差），对角色/大字这类
    低频大块反而稳，且不需要任何语义模型。
    """
    from scipy import ndimage as ndi
    from PIL import Image
    g = _gray(rgb)
    s = 64
    small = np.asarray(Image.fromarray(g.astype(np.uint8)).resize((s, s), Image.BILINEAR),
                       dtype=np.float32)
    F = np.fft.fft2(small)
    logA = np.log(np.abs(F) + 1e-6)
    res = logA - ndi.uniform_filter(logA, 3)
    sal = np.abs(np.fft.ifft2(np.exp(res + 1j * np.angle(F)))) ** 2
    sal = ndi.gaussian_filter(sal, 2.0)
    if sal.max() <= 0:
        return 0.0, 0.5
    thr = max(float(np.percentile(sal, 90)), float(sal.max()) * 0.20)
    m = sal > thr
    lab, n = ndi.label(m)
    if n == 0:
        return 0.0, 0.5
    sizes = ndi.sum(m, lab, range(1, n + 1))
    k = int(np.argmax(sizes)) + 1
    area = float(sizes[k - 1]) / (s * s)
    cy, cx = ndi.center_of_mass(lab == k)
    return area, float(cx) / s


def frame_metrics(rgb, prev_gray=None):
    from scipy import ndimage as ndi
    g = _gray(rgb)
    out = {}
    b = _bands(rgb)
    out["band_top"] = b["top"]
    out["band_bottom"] = b["bottom"]
    out["band_lr"] = b["left"] + b["right"]
    out["lum"] = float(g.mean() / 255.0)
    out["contrast"] = float(g.std() / 255.0)
    mx = rgb.max(axis=2).astype(np.float32)
    mn = rgb.min(axis=2).astype(np.float32)
    # 近黑像素的 (mx-mn)/mx 会虚高到 1.0（分母趋 0），必须只在可见像素上算
    vis = mx > 40
    sat = np.zeros_like(mx)
    if vis.any():
        sat[vis] = (mx[vis] - mn[vis]) / mx[vis]
    out["sat"] = float(sat[vis].mean()) if vis.any() else 0.0
    out["sat_max"] = float(np.percentile(sat[vis], 95)) if vis.any() else 0.0

    mag = np.hypot(ndi.sobel(g, 1), ndi.sobel(g, 0))
    out["edge"] = float((mag > np.percentile(mag, 95)).mean())

    boxes = _text_boxes(g)
    lines = _group_lines(boxes)
    # 底部常驻 UI（credit 条 / 字幕条 / 水印）不是歌词：字小、贴下缘。
    # 不摘出去会把「有字帧占比」抬到 84%、把对齐判成 center-mid（首版实测就是这两条）。
    ui_idx = set(i for i, l in enumerate(lines)
                 if (l["y0"] + l["y1"]) / 2.0 > AH * 0.80 and (l["y1"] - l["y0"]) < AH * 0.045)
    out["has_credit"] = 1.0 if ui_idx else 0.0
    lines = [l for i, l in enumerate(lines) if i not in ui_idx]
    if lines:
        bh = [l["y1"] - l["y0"] for l in lines]
        x0 = min(l["x0"] for l in lines); x1 = max(l["x1"] for l in lines)
        y0 = min(l["y0"] for l in lines); y1 = max(l["y1"] for l in lines)
        cx, cy = (x0 + x1) / 2.0 / AW, (y0 + y1) / 2.0 / AH
        out["text_h"] = float(np.median(bh) / AH)
        out["text_lines"] = len(lines)
        out["text_area"] = float(sum((l["x1"] - l["x0"]) * (l["y1"] - l["y0"])
                                     for l in lines) / (AW * AH))
        out["text_align"] = ("left" if cx < 0.40 else "right" if cx > 0.60 else "center") + \
                            "-" + ("top" if cy < 0.36 else "bottom" if cy > 0.64 else "mid")
        out["_tb"] = (x0, y0, x1, y1)
    else:
        out["text_h"] = 0.0; out["text_lines"] = 0
        out["text_area"] = 0.0; out["text_align"] = "none"

    out["letterbox"] = b["top"] + b["bottom"]
    out["grain"] = _grain(g)
    out["halftone"] = _halftone(g)
    out["scanline"] = _scanline(g)
    out["rgb_split"] = _rgb_split(rgb)
    out["bloom"] = float((g > 240).mean())
    a, cx = _saliency(rgb)
    out["subject"] = a
    out["subject_cx"] = cx
    # 文字是否落在下条带里（credit 条 / 字幕带的判据，单帧）
    tb = out.get("_tb")
    out["text_in_bottom"] = (1.0 if (tb and b["bottom"] > 0.02
                                     and tb[3] > AH * (1 - b["bottom"] - 0.04)) else 0.0)
    # 变焦代理：边缘能量的加权半径。推近时变大、拉远时变小。
    # 相位相关只能测平移，纯推镜会被读成「零运动」——参考片 cam_speed 恒 0 就是这个原因。
    ys, xs = np.mgrid[0:AH, 0:AW]
    rr = np.hypot((ys - AH / 2.0) / (AH / 2.0), (xs - AW / 2.0) / (AW / 2.0))
    out["zoom_r"] = float((rr * mag).sum() / (mag.sum() + 1e-6))

    if prev_gray is None:
        out["motion"] = 0.0
        out["diff"] = 0.0
    else:
        # 画面变化率：与平移无关，捕捉「粒子/形变/逐帧抖动」这类非相机动态。
        # 参考片的 cam_speed 实测恒 0（不摇镜），但画面显然在动 —— 就是这个量在动。
        out["diff"] = float(np.abs(g - prev_gray).mean() / 32.0)
        # 相位相关：位移的整数峰（已验证：合成位移 3/5/8/15px 全部测回）
        f = np.fft.fft2(g * np.outer(np.hanning(AH), np.hanning(AW)))
        f0 = np.fft.fft2(prev_gray * np.outer(np.hanning(AH), np.hanning(AW)))
        R = f * np.conj(f0)
        R /= np.abs(R) + 1e-9
        c = np.abs(np.fft.ifft2(R))
        iy, ix = np.unravel_index(np.argmax(c), c.shape)
        dy = iy if iy <= AH // 2 else iy - AH
        dx = ix if ix <= AW // 2 else ix - AW
        out["motion"] = float(np.hypot(dx, dy) / AW)
    return out, g


# ----------------------------------------------------------------------------
# 3b) 文字检测（OCR 驱动）
# ----------------------------------------------------------------------------
# 为什么不用「Sobel + 连通域」那套纯启发式：实测在有角色的镜头上，它框的是
# 脸、头发、拍立得相框；而真正的歌词笔画细、对比低，一个都框不到 —— 于是
# 「有字帧 84%」「对齐 center-mid」全是假的。文字定位这件事没有捷径，必须有模型。
# 这里只借它的**检测框**（几何），不依赖识别结果对不对。
UI_WORDS = ("bilibili", "哔哩哔哩", "艾薇拉", "elvira", "my heart", "song for",
            "作词", "作曲", "编曲", "混音", "母带", "调教", "pv")

_OCR = {"eng": None}


def _ocr_engine():
    if _OCR["eng"] is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR["eng"] = RapidOCR()
    return _OCR["eng"]


def ocr_boxes(rgb):
    """返回归一化文字框列表（x0,y0,x1,y1 ∈ 0..1）与识别串。"""
    eng = _ocr_engine()
    h, w = rgb.shape[:2]
    res, _ = eng(rgb)
    out = []
    for item in (res or []):
        box, txt = item[0], item[1]
        sc = float(item[2]) if len(item) > 2 else 0.0
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        if x1 - x0 < 6 or y1 - y0 < 6:
            continue
        out.append({"x0": x0 / w, "y0": y0 / h, "x1": x1 / w, "y1": y1 / h,
                    "t": (txt or "").strip(), "s": sc})
    return out


def ocr_metrics(rgb):
    """把 OCR 框拆成「歌词」与「常驻 UI（水印/credit 条）」，产出与 frame_metrics 同名的键。

    UI 判据两条：贴在画面下缘（y0>0.80），或文本里含片头片尾固定字样。
    歌词再按 y 聚成行；若整体包围盒竖向且框数≥3，判为竖排。
    """
    boxes = ocr_boxes(rgb)
    ui_idx = set()
    for i, b in enumerate(boxes):
        t = b["t"].lower()
        if b["y0"] > 0.80 or any(k in t for k in UI_WORDS):
            ui_idx.add(i)
    lyr = [b for i, b in enumerate(boxes) if i not in ui_idx]
    ui = [b for i, b in enumerate(boxes) if i in ui_idx]
    m = {"has_credit": 1.0 if ui else 0.0, "ui_n": len(ui), "lyric_n": len(lyr),
         "text_h": 0.0, "text_lines": 0, "text_area": 0.0, "text_align": "none",
         "vertical": 0}
    if not lyr:
        return m

    x0 = min(b["x0"] for b in lyr); x1 = max(b["x1"] for b in lyr)
    y0 = min(b["y0"] for b in lyr); y1 = max(b["y1"] for b in lyr)
    hs = [b["y1"] - b["y0"] for b in lyr]
    m["text_h"] = float(np.median(hs))
    m["text_area"] = float(sum((b["x1"] - b["x0"]) * (b["y1"] - b["y0"]) for b in lyr))
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    m["text_align"] = ("left" if cx < 0.40 else "right" if cx > 0.60 else "center") + \
                      "-" + ("top" if cy < 0.36 else "bottom" if cy > 0.64 else "mid")

    # 行数：按 y 中心聚类（容差 = 中位字高的 0.7）
    tol = max(0.012, float(np.median(hs)) * 0.7)
    centers = sorted((b["y0"] + b["y1"]) / 2.0 for b in lyr)
    lines = 1
    for a, b2 in zip(centers, centers[1:]):
        if b2 - a > tol:
            lines += 1
    m["text_lines"] = int(lines)

    # 竖排：包围盒瘦高，且框沿 y 排成一列（x 展开小、y 展开大）
    bw, bh = x1 - x0, y1 - y0
    if len(lyr) >= 3 and bh > bw * 1.4:
        xs = [((b["x0"] + b["x1"]) / 2.0) for b in lyr]
        if (max(xs) - min(xs)) < bw * 0.6:
            m["vertical"] = 1

    m["_tb"] = (x0 * AW, y0 * AH, x1 * AW, y1 * AH)
    return m


def ocr_scan(path, dur, rect=None, fps=1.0):
    """按 fps 抽帧跑 OCR。

    排版量（字号/行数/对齐）在一句之内几乎不变，所以不需要每帧都跑 ——
    1fps 在 3 分钟的片子上是 179 次，约 2 分钟，换来的是**真**的排版参数。
    """
    ow, oh = 960, 540
    n = max(1, int(round(dur * fps)))
    out = []
    for i in range(n):
        t = (i + 0.5) * dur / n
        p = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % t,
                 "-i", path, "-frames:v", "1", "-vf", "scale=%d:%d" % (ow, oh),
                 "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
        if len(p.stdout) < ow * oh * 3:
            continue
        a = np.frombuffer(p.stdout[:ow * oh * 3], dtype=np.uint8).reshape(oh, ow, 3)
        m = ocr_metrics(a)
        m["t"] = round(t, 3)
        out.append(m)
    return out


# ----------------------------------------------------------------------------
# 4) 扫描整片（自适应抽帧密度）
# ----------------------------------------------------------------------------
def scan(path, shots, base_fps=2.0, text_fps=10.0, debug_dir=None):
    """两遍：
    ① 全片 base_fps（默认 2fps）拿粗统计，并判定哪些镜头是「文字密集/快切」；
    ② 对判定出的镜头按 text_fps 重扫，拿准字号/行数/对齐。
    这就是需求里「普通 1~2fps、文字 5~10fps」的落地方式。
    """
    per_shot = {s["i"]: [] for s in shots}

    def pass_cmd(fps, t0=None, dur=None):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]
        if t0 is not None:
            cmd += ["-ss", "%.3f" % t0]
        cmd += ["-i", path]
        if dur is not None:
            cmd += ["-t", "%.3f" % dur]
        cmd += ["-vf", "fps=%s,scale=%d:%d" % (fps, AW, AH),
                "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        return cmd

    # ① 全片
    p = subprocess.Popen(pass_cmd(base_fps), stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    n = AW * AH * 3
    prev = None
    k = 0
    si = 0
    while True:
        buf = p.stdout.read(n)
        if len(buf) < n:
            break
        t = k / base_fps
        k += 1
        while si + 1 < len(shots) and t >= shots[si]["t1"]:
            si += 1
        rgb = _to_frame(buf)
        m, g = frame_metrics(rgb, prev)
        prev = g
        if si < len(shots) and shots[si]["t0"] - 0.05 <= t <= shots[si]["t1"] + 0.05:
            per_shot[shots[si]["i"]].append((t, m))
        if debug_dir and k % 60 == 0:
            _dump_debug(debug_dir, k, rgb, m)
    p.stdout.close(); p.wait()

    # ② 自适应重扫
    dense = []
    for s in shots:
        fr = per_shot[s["i"]]
        if not fr:
            continue
        ea = float(np.median([f["edge"] for _, f in fr]))
        ta = float(np.median([f["text_area"] for _, f in fr]))
        mo = float(np.median([f["motion"] for _, f in fr]))
        if ta > 0.008 or ea > 0.14 or (mo > 0.02 and s["dur"] < 3.0):
            dense.append(s)

    for s in dense:
        cmd = pass_cmd(text_fps, s["t0"], s["dur"])
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        prev = None
        k = 0
        while True:
            buf = p.stdout.read(n)
            if len(buf) < n:
                break
            t = s["t0"] + k / text_fps
            k += 1
            rgb = _to_frame(buf)
            m, g = frame_metrics(rgb, prev)
            prev = g
            per_shot[s["i"]].append((t, m))
        p.stdout.close(); p.wait()

    return per_shot, len(dense)


def _dump_debug(d, k, rgb, m):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    os.makedirs(d, exist_ok=True)
    im = Image.fromarray(rgb)
    dr = ImageDraw.Draw(im)
    tb = m.get("_tb")
    if tb:
        dr.rectangle([tb[0], tb[1], tb[2], tb[3]], outline=(0, 255, 0))
    dr.text((4, 4), "t=%.2f h=%.2f" % (0, m["text_h"]), fill=(255, 0, 0))
    im.save(os.path.join(d, "f%04d.png" % k))


# ----------------------------------------------------------------------------
# 5) 聚合
# ----------------------------------------------------------------------------
def _kmeans_palette(pixels, k=6, seed=20261101):
    from sklearn.cluster import KMeans
    if len(pixels) < k:
        return []
    km = KMeans(n_clusters=k, random_state=seed, n_init=10).fit(pixels)
    cnt = np.bincount(km.labels_, minlength=k).astype(np.float64)
    w = cnt / cnt.sum()
    out = []
    for i in np.argsort(-w):
        c = km.cluster_centers_[i]
        out.append({"hex": "#%02x%02x%02x" % tuple(int(round(v)) for v in c[:3]),
                    "share": round(float(w[i]), 4)})
    return out


def sample_pixels(path, dur, n=24, rect=None):
    """均匀取 n 帧、每帧 1/16 降采样，用来算全片配色。

    必须**先裁掉黑边/深色条带**再取样：否则 letterbox 的黑色会吃掉最大的一簇，
    底色与强调色双双变成 #020304（首版实测就是这么错的）。
    """
    px = []
    ts = [dur * (i + 0.5) / n for i in range(n)]
    for t in ts:
        p = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % t,
                 "-i", path, "-frames:v", "1", "-vf", "scale=%d:%d" % (AW, AH),
                 "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
        if len(p.stdout) >= AW * AH * 3:
            a = np.frombuffer(p.stdout[:AW * AH * 3], dtype=np.uint8).reshape(AH, AW, 3)
            b = _bands(a)
            if rect:
                y0 = int(round(rect["y"] / 3.0)) + 1
                y1 = int(round((rect["y"] + rect["h"]) / 3.0)) - 1
                x0 = int(round(rect["x"] / 3.0)) + 1
                x1 = int(round((rect["x"] + rect["w"]) / 3.0)) - 1
            else:
                y0 = int(b["top"] * AH) + 1
                y1 = AH - int(b["bottom"] * AH) - 1
                x0 = int(b["left"] * AW) + 1
                x1 = AW - int(b["right"] * AW) - 1
            if y1 - y0 < 40 or x1 - x0 < 40:
                y0, y1, x0, x1 = 0, AH, 0, AW
            sub = a[y0:y1, x0:x1]
            px.append(sub[::4, ::4].reshape(-1, 3))
    if not px:
        return np.zeros((0, 3))
    return np.concatenate(px, axis=0).astype(np.float32)


def classify_trans(path, shots, fps, window=0.42):
    """判断每个切点是「硬切」还是「渐变(淡化/闪白)」。

    做法：取切点前后各 window 秒、逐帧算与前一帧的差（10fps 采样），
    硬切 = 单帧尖峰且前后都平；渐变 = 连续 ≥3 帧都高于基线。

    **能力边界（别高估这一项）**：它只能判「多帧变化 vs 单帧切换」，
    判不出「这是 dissolve 还是 wipe」。曾试着用「中间帧是否 ≈ 两端帧均值」
    来分辨均匀叠化与几何擦除，实测**失败**：本片过渡时整幅被重新调色，
    r_mid 普遍是 r_ab 的 1.4~3.2 倍（中间帧根本不在两端帧之间），
    该判据在有色偏的 PV 上不可用，已删除。所以形态由人工确认后写进档案：
    BV1jUxC6SEko 抽 t=14.85 帧看过，是两幅均匀叠化（无几何边界）。
    """
    res = []
    for s in shots[1:]:
        t = s["t0"]
        p = run([FFMPEG, "-hide_banner", "-loglevel", "error",
                 "-ss", "%.3f" % max(0, t - window), "-i", path,
                 "-t", "%.3f" % (window * 2), "-vf", "fps=10,scale=160:90",
                 "-pix_fmt", "gray", "-f", "rawvideo", "-"])
        fsz = 160 * 90
        n = len(p.stdout) // fsz
        if n < 4:
            continue
        fs = [np.frombuffer(p.stdout[i * fsz:(i + 1) * fsz], dtype=np.uint8)
              .astype(np.float32) for i in range(n)]
        d = [float(np.abs(fs[i] - fs[i - 1]).mean()) for i in range(1, n)]
        if not d:
            continue
        mx = max(d)
        hi = [i for i, v in enumerate(d) if v > mx * 0.35]
        span = (hi[-1] - hi[0] + 1) if hi else 1
        kind = "gradual" if span >= 3 else "cut"
        lum = [float(f.mean()) for f in fs]
        if kind == "gradual" and max(lum) > 200 and (max(lum) - min(lum)) > 40:
            kind = "flash"
        # 均匀叠化 vs 几何揭示：试过「中间帧 ≈ 两端帧均值」的自校准判据，
        # 在本片（过渡时整体重新调色）上失效 —— 保留字段但**不参与判定**，
        # 只作为诊断读数留在 list 里，供以后有干净样本时再标定。
        soft, r_mid, r_ab = None, 0.0, 0.0
        if kind == "gradual" and n >= 4:
            A, B = fs[0], fs[-1]
            j = int(np.argmax(d)) + 1
            if 0 < j < len(fs):
                M = fs[j]
                r_mid = float(np.abs(M - (A + B) / 2).mean())
                r_ab = float(np.abs(A - B).mean()) / 2.0
                if r_ab > 1e-3:
                    soft = bool(r_mid < 0.45 * r_ab)
        res.append({"t": round(t, 3), "kind": kind, "span_frames": span,
                    "peak": round(mx, 2), "soft": soft,
                    "r_mid": round(r_mid, 2), "r_ab": round(r_ab, 2)})
    return res


def aggregate(per_shot, shots, trans, pix, probe_info, src, dense_n, shots_thr,
              rect=None, ocr=None):
    def med(key, sub=None):
        v = []
        for s in (sub or shots):
            for _, m in per_shot.get(s["i"], []):
                v.append(m.get(key, 0.0))
        return float(np.median(v)) if v else 0.0

    def pct(key, q, sub=None):
        v = []
        for s in (sub or shots):
            for _, m in per_shot.get(s["i"], []):
                v.append(m.get(key, 0.0))
        return float(np.percentile(v, q)) if v else 0.0

    # 文字：有 OCR 就用 OCR（真框），没有才退回 Sobel 启发式（不可信，仅兜底）
    if ocr:
        tf = [m for m in ocr if m["lyric_n"] > 0]
        tf_all = ocr
    else:
        tf = [m for s in shots for _, m in per_shot.get(s["i"], []) if m["text_lines"] > 0]
        tf_all = [m for s in shots for _, m in per_shot.get(s["i"], [])]
    # 对齐要按「文字面积」加权投票：无字帧与只有一行小注记的帧不该和整块大字同权
    align = {}
    for m in tf:
        align[m["text_align"]] = align.get(m["text_align"], 0.0) + max(1e-4, m["text_area"])
    align_pick = max(align.items(), key=lambda kv: kv[1])[0] if align else "none"
    align_norm = {k: int(round(v / max(1e-9, max(align.values())) * 100)) for k, v in align.items()}

    # 常驻文字区（credit 条 / 水印）：在多帧同一位置出现的像素级累积
    per_shot_persist = None
    if tf:
        acc = np.zeros((AH, AW), np.float32)
        for s in shots:
            for _, m in per_shot.get(s["i"], []):
                if m.get("_tb"):
                    x0, y0, x1, y1 = m["_tb"]
                    acc[y0:y1, x0:x1] += 1
        acc /= max(1, len(tf))
        per_shot_persist = float((acc > 0.7).mean())

    durs = [s["dur"] for s in shots]
    pal = _kmeans_palette(pix, seed=20261101)
    lum_all = float(np.mean([p.mean() for p in pix])) / 255.0 if len(pix) else 0.5

    # 主色排序：最暗的当 bg，最亮的当 fg，饱和度最高的当 accent
    def hex2rgb(h):
        return np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], np.float32)
    pal_rgb = [(p, hex2rgb(p["hex"])) for p in pal]

    def lum_of(c):
        return float(c.mean() / 255.0)

    def chroma(c):
        return float((c.max() - c.min()) / max(1.0, float(c.max())))

    # 底：在「占比够大」的簇里取最暗的（避免被一小撮纯黑文字骗成底色）
    cand_dark = [t for t in pal_rgb if t[0]["share"] >= 0.10] or pal_rgb
    bg = min(cand_dark, key=lambda t: lum_of(t[1]))[0] if cand_dark else {"hex": "#101418"}
    # 字：取最亮的簇
    fg = max(pal_rgb, key=lambda t: lum_of(t[1]))[0] if pal_rgb else {"hex": "#f2eee6"}
    # 强调：必须是**看得见**的颜色（亮度 0.15~0.92），再按「彩度 × 占比」选
    cand_acc = [t for t in pal_rgb if 0.15 <= lum_of(t[1]) <= 0.92] or pal_rgb
    acc = max(cand_acc, key=lambda t: chroma(t[1]) * (0.4 + 2.0 * t[0]["share"]))[0] \
        if cand_acc else {"hex": "#e2604a"}
    dom = max(pal_rgb, key=lambda t: t[0]["share"])[0] if pal_rgb else bg

    # 变焦：逐镜对 zoom_r 做一次线性拟合，取斜率中位数（1/秒，正=推近）
    zslopes = []
    for s in shots:
        fr = per_shot.get(s["i"], [])
        if len(fr) >= 4:
            tt = np.array([t for t, _ in fr], np.float64)
            zz = np.array([m["zoom_r"] for _, m in fr], np.float64)
            if tt.std() > 1e-6:
                zslopes.append(float(np.polyfit(tt, zz, 1)[0]))
    zoom_med = float(np.median(zslopes)) if zslopes else 0.0

    # credit 条 / 常驻字带：下条带里长期有字
    credit = float(np.mean([m.get("has_credit", 0.0) for m in tf_all])) if tf_all else 0.0

    gradual = [t for t in trans if t["kind"] in ("gradual", "flash")]
    hard = [t for t in trans if t["kind"] == "cut"]
    # 分类器只认得出「淡变 / 闪白 / 硬切」三态，而引擎有 13 个转场名。
    # 所以这里记录的是**族**，不是名字：一个测量类别对不上一个引擎转场，
    # 硬塞会得到「全场只有 dissolve」——那不是参考风格，是把变化锁死了。
    spans = [max(1, t.get("span_frames", 1)) / 10.0 for t in gradual]   # 10fps 采样
    span_med = float(np.median(spans)) if spans else 0.0

    gr = len(gradual) / float(max(1, len(trans)))
    fl = float(np.mean([1.0 if t["kind"] == "flash" else 0.0 for t in trans])) if trans else 0.0
    # 只能判「多帧变化 vs 单帧切换」，判不出「溶解 vs 擦除」（原因见 classify_trans）。
    # 渐变为主时给**软溶解族**，依据是人工抽帧（t=14.85 是均匀叠化，无几何边界）；
    # 若某支参考片以擦除为主，请人工改档案的 trans_pool —— 这一维不做自动猜测，
    # 猜错会把「推测」写进档案，比留空更糟。
    if gr >= 0.55:
        trans_family, trans_pool = "soft-fade", list(TRANS_SOFT)
    elif gr <= 0.25:
        trans_family, trans_pool = "cut", list(TRANS_GEOM)
    else:
        trans_family = "mixed"
        trans_pool = list(TRANS_SOFT)[:3] + list(TRANS_GEOM)[:2]
    if fl >= 0.12:
        trans_pool.append("flash")

    if rect:
        H0, W0 = probe_info["h"], probe_info["w"]
        lb = (H0 - rect["h"]) / float(H0)
        lb_lr = (W0 - rect["w"]) / float(W0)
        asp = rect["w"] / float(rect["h"])
    else:
        lb = med("letterbox"); lb_lr = med("band_lr")
        asp = AW / max(1e-3, AH * (1 - lb))

    return {
        "id": None,
        "source": src,
        "probe": {"w": probe_info["w"], "h": probe_info["h"], "fps": probe_info["fps"],
                  "dur": probe_info["dur"], "nb": probe_info["nb"]},
        "analysis": {"shot_thr": shots_thr, "dense_shots": dense_n,
                     "dense_fps": 10, "base_fps": 2},
        "frame": {
            "letterbox": round(lb, 4),
            "letterbox_lr": round(lb_lr, 4),
            "crop": (dict(rect) if rect else None),
            "band_bottom": round(med("band_bottom"), 4),
            "aspect_visible": round(asp, 3),
            "persistent_text_area": round(per_shot_persist or 0.0, 4),
            "credit_band": round(credit, 3),
        },
        "palette": {
            "bg": bg["hex"], "fg": fg["hex"], "accent": acc["hex"],
            "dominant": dom["hex"],
            "colors": pal,
            "lum": round(lum_all, 4),
            "sat": round(med("sat"), 4),
            "sat_p95": round(pct("sat_max", 95), 4),
            "contrast": round(med("contrast"), 4),
        },
        "type": {
            "size_norm": round(float(np.median([m["text_h"] for m in tf])), 4) if tf else 0.0,
            "lines_med": int(np.median([m["text_lines"] for m in tf])) if tf else 0,
            "area_med": round(float(np.median([m["text_area"] for m in tf])), 4) if tf else 0.0,
            "align": align_pick,
            "text_frame_ratio": round(len(tf) / max(1, len(tf_all)), 3),
            "vertical_ratio": round(float(np.mean([m.get("vertical", 0) for m in tf])), 3) if tf else 0.0,
            "source": "ocr" if ocr else "sobel",
            "align_hist": align_norm,
        },
        "figure": {
            "coverage_med": round(float(np.median([m["subject"] for s in shots
                                                   for _, m in per_shot.get(s["i"], [])])), 4),
            "cx_med": round(float(np.median([m["subject_cx"] for s in shots
                                              for _, m in per_shot.get(s["i"], [])])), 3),
            "presence": round(float(np.mean([1.0 if m["subject"] > 0.020 else 0.0
                                             for s in shots for _, m in per_shot.get(s["i"], [])])), 3),
            "confidence": 0.55,
        },
        "texture": {
            "grain": round(med("grain"), 4),
            "halftone": round(med("halftone"), 4),
            "scanline": round(med("scanline"), 4),
            "rgb_split_px": round(med("rgb_split"), 3),
            "bloom": round(med("bloom"), 4),
        },
        "motion": {
            "shot_len_med": round(float(np.median(durs)), 3),
            "shot_len_p90": round(float(np.percentile(durs, 90)), 3),
            "cut_per_min": round(len(shots) / max(1e-6, probe_info["dur"] / 60.0), 2),
            "cam_speed": round(med("motion"), 5),
            "cam_speed_p90": round(pct("motion", 90), 5),
            "zoom_rate": round(zoom_med, 5),
            "churn": round(med("diff"), 4),
            "density": round(med("edge"), 4),
        },
        "trans": {
            "n": len(trans),
            "gradual_ratio": round(gr, 3),
            "flash_ratio": round(fl, 3),
            "span_s": round(span_med, 3),
            "family": trans_family,
            "pool": trans_pool,
            "list": trans[:40],
        },
        "shots": [{"i": s["i"], "t0": s["t0"], "t1": s["t1"], "dur": s["dur"],
                   "density": round(float(np.median([m["edge"] for _, m in per_shot[s["i"]]])), 3)
                   if per_shot.get(s["i"]) else 0.0,
                   "fig": round(float(np.median([m["subject"] for _, m in per_shot[s["i"]]])), 3)
                   if per_shot.get(s["i"]) else 0.0,
                   "txt": round(float(np.median([m["text_area"] for _, m in per_shot[s["i"]]])), 4)
                   if per_shot.get(s["i"]) else 0.0}
                  for s in shots],
        "metrics_doc": METRIC_DOC,
    }


# ----------------------------------------------------------------------------
# 6) 映射到我们引擎的旋钮
# ----------------------------------------------------------------------------
SCHEME_JS = os.path.join(ROOT, "renderer", "player", "pv", "pv_scheme.js")


def load_pack_colors(path=None):
    """从 pv_scheme.js 直接读每个 pack 的**真实配色**（不靠手填的 (亮度,饱和) 猜）。

    首版把 pack 判成 `crimson`（暗红），完全违背肉眼 —— 根因就是拿我手写的一对
    数字去比距离。这里改成解析源码里的 bg/fg/accent 六位色值，逐套取最小距离。
    """
    path = path or SCHEME_JS
    try:
        txt = io.open(path, encoding="utf8").read()
    except OSError:
        return {}
    i = txt.find("PV.PACKS")
    if i < 0:
        return {}
    body = txt[i:]
    packs = {}
    for mm in re.finditer(r"\n\s{2,8}([a-zA-Z][\w]*):\s*\{", body):
        name = mm.group(1)
        start = mm.end()
        nxt = re.search(r"\n\s{2,8}[a-zA-Z][\w]*:\s*\{", body[start:])
        block = body[start:start + (nxt.start() if nxt else len(body))]
        # 注意 `#` 必须写在捕获组里、**不占**那 6 位。写成 `([#0-9A-Fa-f]{6})`
        # 会把 `#060607` 读成 `#06060` 再要求紧跟引号，于是永远匹配不上。
        bgs = re.findall(r"bg:\s*'(#[0-9A-Fa-f]{6})'", block)
        fgs = re.findall(r"fg:\s*'(#[0-9A-Fa-f]{6})'", block)
        accs = re.findall(r"accent:\s*'(#[0-9A-Fa-f]{6})'", block)
        if bgs and fgs and accs:
            packs[name] = {"bg": bgs, "fg": fgs, "accent": accs}
    return packs


def _hex2rgb(h):
    return np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], np.float32)


def _mix(h1, h2, t):
    """两色按 t 混合，返回 `#RRGGBB`。用来把某个簇色「压」成可用的暗部色。"""
    a, b = _hex2rgb(h1), _hex2rgb(h2)
    c = np.clip(a * (1 - t) + b * t, 0, 255).astype(np.int32)
    return "#%02X%02X%02X" % (c[0], c[1], c[2])


def _cdist(a, b):
    """归一化 RGB 距离 → 0..100。"""
    return float(np.linalg.norm(a - b) / np.sqrt(3.0) / 255.0 * 100.0)


def match_pack(bg, fg, accent):
    """把量到的三色与现有 pack 比对，返回 (排序表, 最优 pack, 加权距离, 分量拆解)。

    权重：accent ×2。底色与字色在多数 pack 里都是「黑/白」二选一，区分度低；
    真正决定「这是哪套纸」的是强调色。所以**是否新建 pack** 不能只看加权总分，
    必须看 accent 自己的距离 —— 总分 62 分里可能有 53 分全是强调色不对，
    这时底色再像也不该硬塞进那个 pack。
    """
    packs = load_pack_colors()
    if not packs:
        return [], None, 0.0, {}
    mb, mf, ma = _hex2rgb(bg), _hex2rgb(fg), _hex2rgb(accent)
    scored = []
    for name, cols in packs.items():
        best = 9e9
        detail = None
        # 逐**套**比对，不做三色叉积：叉积会拼出「A 套的底 + B 套的字 + C 套的强调」
        # 这种并不存在的方案，把不相似的 pack 评得更像。三张表长度一致，可直接 zip。
        for b, f, a in zip(cols["bg"], cols["fg"], cols["accent"]):
            db = _cdist(_hex2rgb(b), mb)
            df = _cdist(_hex2rgb(f), mf)
            da = _cdist(_hex2rgb(a), ma)
            d = db + df + 2.0 * da
            if d < best:
                best, detail = d, {"bg": round(db, 1), "fg": round(df, 1),
                                   "accent": round(da, 1),
                                   "scheme": {"bg": b, "fg": f, "accent": a}}
        scored.append((round(best, 1), name, detail))
    scored.sort(key=lambda x: x[0])
    rank = [(s[0], s[1]) for s in scored]
    return rank, scored[0][1], scored[0][0], scored[0][2]


def to_engine(vsp):
    """把量测值翻译成引擎能吃的旋钮。原则：只给「取值范围与权重」，不给逐镜模板。"""
    p = vsp["palette"]; ty = vsp["type"]; tx = vsp["texture"]
    mo = vsp["motion"]; fg = vsp["figure"]; fr = vsp["frame"]

    rank, best, bd, pk = match_pack(p["bg"], p["fg"], p["accent"])
    accent_d = pk.get("accent", 0.0)
    # 两条独立触发条件，任一成立才建议新建 pack：
    #   a) 强调色单独差 ≥20/100 —— 底色再像也不是同一套纸；
    #   b) 加权总分 >70（≈ 三色各差 1/3 个量程）—— 整体都不像。
    propose = accent_d >= 20.0 or bd > 70.0

    # 版面权重：按「文字对齐 + 行数 + 覆盖率」推
    lw = {"block": 3, "cross": 2, "lowerThird": 2, "marginNote": 1, "titleCard": 1}
    al = ty["align"]
    if al.startswith("center") and ty["lines_med"] <= 2:
        lw = {"titleCard": 3, "center": 2, "scaleSteps": 2, "cross": 1, "block": 2}
    elif al.startswith("right"):
        lw = {"lowerThird": 3, "marginNote": 2, "vRuler": 2, "block": 2, "cross": 1}
    elif al.startswith("left"):
        lw = {"marginNote": 3, "lowerThird": 2, "indexTable": 2, "block": 2, "cross": 1}
    if fg["coverage_med"] > 0.25:
        # 人物占比大的片子，必须留「贴着人走」的版面
        lw.setdefault("cropGiant", 0)
        lw["cropGiant"] += 2

    # 转场池已在量测阶段按「族」定好，这里只做一次合法性过滤：
    # 老档案（没有 pool 字段）或手改加了不存在的名字时，不能把非法转场名传下去。
    trans_pool = [k for k in (vsp["trans"].get("pool") or []) if k in ALL_TRANS]
    if not trans_pool:
        trans_pool = list(TRANS_SOFT)

    # 转场时长：用实测的渐变跨度（秒）；这条比「按渐变比例估」更接近真相 ——
    # 比例只说「有多少是淡接」，跨度才说「每一次淡接多长」。
    span_s = vsp["trans"].get("span_s") or 0.0
    trans_dur = round(max(0.30, min(1.20, span_s)), 3) if span_s > 0 else \
        round(max(0.42, min(0.9, 0.42 + 0.35 * vsp["trans"]["gradual_ratio"])), 3)

    return {
        "pack": "violet" if propose else best,
        "pack_rank": [{"pack": n, "dist": d} for d, n in rank[:4]],
        "pack_proposal": ({
            "name": "violet",
            "scheme": {
                "bg": p["bg"], "fg": p["fg"], "accent": p["accent"],
                "sub": p["colors"][-1]["hex"] if p["colors"] else p["fg"],
                # dim 不能取「倒数第二个簇」——多数片子最暗的簇就是底本身，
                # 取出来 dim == bg，所有靠 dim 画的细线/框都会隐形。
                # 按底与字混合 18% 得到「比底亮一点、比字暗很多」的可用暗部色。
                "dim": _mix(p["bg"], p["fg"], 0.18),
            },
            "why": "最近 pack=%s：底 %.1f / 字 %.1f / 强调 %.1f（加权总分 %.1f）；%s"
                   % (best, pk.get("bg", 0.0), pk.get("fg", 0.0), accent_d, bd,
                      "强调色差 %.1f ≥ 20" % accent_d if accent_d >= 20.0
                      else "总分 %.1f > 70" % bd),
            "rank": [{"pack": n, "dist": d} for d, n in rank[:3]],
        } if propose else None),
        "layouts": lw,
        "trans_pool": trans_pool,
        "trans_dur": trans_dur,
        "type": {
            "size_ladder": round(1.0 + 2.2 * ty["size_norm"], 3),
            "tracking": round(0.02 + 0.28 * ty["size_norm"], 3),
            "leading": 1.35 if ty["lines_med"] <= 2 else 1.5,
            "accent_word_ratio": 0.12 if ty["area_med"] < 0.03 else 0.18,
            "serif": True if ty["size_norm"] < 0.06 else False,
        },
        "texture": {
            "grain": round(min(0.6, tx["grain"] * 0.8), 3),
            "halftone": round(min(0.6, tx["halftone"] * 0.8), 3),
            "scanline": round(min(0.5, tx["scanline"] * 0.6), 3),
            "rgb_split": round(min(0.5, tx["rgb_split_px"] / 12.0), 3),
            "bloom": round(min(0.8, tx["bloom"] * 12.0), 3),
        },
        "figure": {
            "presence": fg["presence"],
            # 显著性面积是 64x64 的占比，比真实人物覆盖率小一个量级（只框住脸/上半身），
            # 所以映射时按 0.24 + 3×area 放大，并夹在 [0.24, 0.62]。
            "coverage_target": round(min(0.62, max(0.26, 0.26 + fg["coverage_med"] * 4.0)), 3),
            "bias": "right" if fg["cx_med"] > 0.56 else "left" if fg["cx_med"] < 0.44 else "center",
            "frameless_hint": bool(fg["coverage_med"] > 0.020),
            "confidence": fg["confidence"],
        },
        "frame": {"letterbox": fr["letterbox"],
                  "credit_bar": bool(fr["credit_band"] > 0.45),
                  "aspect_visible": fr["aspect_visible"]},
        "motion": {
            # 平移与变焦合成「镜头有多动」；纯推镜的片子 cam_speed 恒为 0（相位相关只测平移）
            "cam_speed": round(min(1.0, mo["cam_speed"] * 60 + abs(mo["zoom_rate"]) * 2.2), 3),
            "zoom_rate": mo["zoom_rate"],
            "churn": mo.get("churn", 0.0),
            "shot_len_target": mo["shot_len_med"],
        },
    }


# ----------------------------------------------------------------------------
# 7) 落盘
# ----------------------------------------------------------------------------
def md_report(vsp):
    e = vsp["engine"]
    L = []
    a = L.append
    a("# 风格档案：%s\n" % vsp["id"])
    a("> 由 `tools/pv_ref.py` 自动生成，请勿手改；要改请改脚本或加 `--override`。\n")
    s = vsp["source"]
    a("| 项 | 值 |\n|---|---|")
    a("| 标题 | %s |" % (s.get("title") or "-"))
    a("| UP | %s |" % (s.get("uploader") or "-"))
    a("| 链接 | %s |" % (s.get("ref") or "-"))
    pr = vsp["probe"]
    a("| 规格 | %dx%d / %.2ffps / %.2fs / %d 帧 |" % (pr["w"], pr["h"], pr["fps"], pr["dur"], pr["nb"]))
    m = vsp["motion"]
    a("| 镜头 | %d 个，中位 %.2fs，p90 %.2fs，每分钟 %.1f 切 |" % (
        len(vsp["shots"]), m["shot_len_med"], m["shot_len_p90"], m["cut_per_min"]))
    a("")
    a("## 一、量测结论\n")
    a("| 维度 | 量 | 读数 |\n|---|---|---|")
    a("| 配色 | 底 / 字 / 强调 | `%s` / `%s` / `%s` |" % (
        vsp["palette"]["bg"], vsp["palette"]["fg"], vsp["palette"]["accent"]))
    a("| 配色 | 亮度 / 饱和p95 / 对比 | %.2f / %.2f / %.2f |" % (
        vsp["palette"]["lum"], vsp["palette"]["sat_p95"], vsp["palette"]["contrast"]))
    t = vsp["type"]
    a("| 排版 | 字高 / 行数 / 面积 | %.3f / %d / %.4f |" % (
        t["size_norm"], t["lines_med"], t["area_med"]))
    a("| 排版 | 对齐 | %s |" % t["align"])
    a("| 排版 | 有字帧占比 | %.0f%% |" % (t["text_frame_ratio"] * 100))
    f = vsp["figure"]
    a("| 人物 | 主体覆盖 / 水平偏置 / 出镜 | %.3f / %.2f / %.0f%% |" % (
        f["coverage_med"], f["cx_med"], f["presence"] * 100))
    x = vsp["texture"]
    a("| 质感 | 颗粒 / 网点 / 扫描线 / 色散 | %.3f / %.3f / %.3f / %.1fpx |" % (
        x["grain"], x["halftone"], x["scanline"], x["rgb_split_px"]))
    a("| 质感 | 泛光 / 画面变化率 | %.4f / %.3f |" % (x["bloom"], m.get("churn", 0.0)))
    tr = vsp["trans"]
    a("| 转场 | 渐变比例 / 池 | %.0f%% / %s |" % (tr["gradual_ratio"] * 100, ", ".join(tr["pool"])))
    a("| 画幅 | letterbox / 下条带 | %.1f%% / %.1f%% |" % (
        vsp["frame"]["letterbox"] * 100, vsp["frame"].get("band_bottom", 0.0) * 100))
    a("| 画幅 | 下条带长期有字（credit 条） | %.0f%% |" % (
        vsp["frame"].get("credit_band", 0.0) * 100))
    a("")
    a("## 二、映射到引擎（`pv_direct --style` 实际会用的值）\n")
    a("```json")
    a(json.dumps(e, ensure_ascii=False, indent=2))
    a("```")
    if e.get("pack_proposal"):
        a("\n> ⚠ 建议新增 pack `violet`：%s\n" % e["pack_proposal"]["why"])
    a("\n## 三、指标口径\n")
    a("| 量 | 怎么算的 |\n|---|---|")
    for k, v in vsp["metrics_doc"].items():
        a("| `%s` | %s |" % (k, v))
    if vsp["analysis"].get("dense_shots"):
        a("\n> 自适应抽帧：%d 个镜头被判为「文字密集/快切」，按 %dfps 重扫；其余 %dfps。\n" % (
            vsp["analysis"]["dense_shots"], vsp["analysis"]["dense_fps"],
            vsp["analysis"]["base_fps"]))
    return "\n".join(L)


def save(vsp, outdir=STYLES):
    os.makedirs(outdir, exist_ok=True)
    j = os.path.join(outdir, vsp["id"] + ".vsp.json")
    m = os.path.join(outdir, vsp["id"] + ".md")
    with io.open(j, "w", encoding="utf8", newline="\n") as f:
        json.dump(vsp, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    with io.open(m, "w", encoding="utf8", newline="\n") as f:
        f.write(md_report(vsp))
    return j, m


# ----------------------------------------------------------------------------
# 8) 命令
# ----------------------------------------------------------------------------
def do_analyze(src, rid=None, base_fps=2.0, text_fps=10.0, thr=0.28, debug=False,
               verify=False, ocr_fps=1.0):
    got = fetch(src)
    path = got["path"]
    info = got["meta"]
    shots, used_thr = build_shots(path, info["dur"], thr)
    if not shots:
        raise RuntimeError("没检出镜头，检查视频是否可解码")

    rect = crop_rect(path)
    if rect:
        print("[ref] 有效画面 %dx%d @ (%d,%d)  投票 %d/%d" % (
            rect["w"], rect["h"], rect["x"], rect["y"], rect["votes"], rect["total"]))

    tmpd = tempfile.mkdtemp(prefix="pvref_") if debug else None
    per_shot, dense_n = scan(path, shots, base_fps, text_fps, tmpd)
    trans = classify_trans(path, shots, info["fps"])
    pix = sample_pixels(path, info["dur"], rect=rect)
    ocr = ocr_scan(path, info["dur"], rect, fps=ocr_fps)
    print("[ref] OCR %d 帧，其中有歌词 %d 帧" % (
        len(ocr), sum(1 for m in ocr if m["lyric_n"] > 0)))

    vsp = aggregate(per_shot, shots, trans, pix, info, got["source"], dense_n, used_thr, rect, ocr)
    vsp["id"] = rid or got["id"]
    vsp["engine"] = to_engine(vsp)

    if verify:
        # 同一视频跑两次必须一致：比「去掉时间戳无关字段」的规范化 JSON
        def fingerprint(v):
            d = {k: v[k] for k in ("frame", "palette", "type", "figure", "texture",
                                   "motion", "trans", "engine")}
            return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False)
                                  .encode("utf8")).hexdigest()[:16]
        h1 = fingerprint(vsp)
        print("[verify] 指纹 %s（复跑中…）" % h1)
        shots2, _ = build_shots(path, info["dur"], thr)
        ps2, dn2 = scan(path, shots2, base_fps, text_fps)
        tr2 = classify_trans(path, shots2, info["fps"])
        px2 = sample_pixels(path, info["dur"], rect=rect)
        oc2 = ocr_scan(path, info["dur"], rect, fps=ocr_fps)
        v2 = aggregate(ps2, shots2, tr2, px2, info, got["source"], dn2, used_thr, rect, oc2)
        v2["id"] = vsp["id"]; v2["engine"] = to_engine(v2)
        h2 = fingerprint(v2)
        print("[verify] 二次指纹 %s → %s" % (h2, "一致 ✓" if h1 == h2 else "不一致 ✗"))
        if h1 != h2:
            raise SystemExit(3)

    j, m = save(vsp)
    print("[ref] %s" % j)
    print("[ref] %s" % m)
    print("[ref] 镜头 %d / 渐变转场 %d%% / pack=%s%s" % (
        len(shots), int(vsp["trans"]["gradual_ratio"] * 100), vsp["engine"]["pack"],
        " (建议新增)" if vsp["engine"]["pack_proposal"] else ""))
    if debug and tmpd:
        print("[ref] 调试图 %s" % tmpd)
    return vsp


def do_selftest():
    """自检：解析出的 pack 必须能「喂回自己」且距离为 0。

    这条正是 2026-11-01 踩过的坑 —— 色值正则把 `#` 也算进了那 6 位十六进制，
    于是 `load_pack_colors()` 静默返回空 dict、`match_pack()` 静默返回空表，
    pack 匹配整段失效却没有任何报错。所以这里把「可解析」和「可回灌」都钉成断言。
    """
    packs = load_pack_colors()
    order = None
    try:
        txt = io.open(SCHEME_JS, encoding="utf8").read()
        mm = re.search(r"PV\.PACK_ORDER\s*=\s*\[([^\]]*)\]", txt)
        if mm:
            order = re.findall(r"'(\w+)'", mm.group(1))
    except OSError:
        pass

    print("[selftest] 解析到 %d 个 pack：%s" % (len(packs), ", ".join(sorted(packs))))
    same_order = True
    if order:
        miss = [n for n in order if n not in packs]
        extra = [n for n in packs if n not in order]
        same_order = not miss and not extra
        print("[selftest] 与 PV.PACK_ORDER 对照：%s" % (
            "一致 ✓" if same_order else "缺 %s / 多 %s" % (miss, extra)))

    bad = total = 0
    for name in sorted(packs):
        c = packs[name]
        for i, (b, f, a) in enumerate(zip(c["bg"], c["fg"], c["accent"])):
            total += 1
            _rank, best, bd, _pk = match_pack(b, f, a)
            if not (best == name and bd == 0.0):
                bad += 1
                print("  ✗ %s[%d] %s/%s/%s → %s %.1f" % (name, i, b, f, a, best, bd))
    print("[selftest] 回灌 %d 套：%s" % (
        total, "全部命中自身 ✓" if bad == 0 else "%d 套没回到自身 ✗" % bad))
    return 0 if (packs and bad == 0 and same_order) else 1


def do_restyle(path, outdir=None):
    """只重算「量测 → 引擎旋钮」这一段，不重新量测。

    量测要跑 OCR（179 帧 × 0.7s，含复跑约 7 分钟）；而调映射（阈值、权重、
    取值范围）是高频操作。两者分开后，改一个阈值不必再等 7 分钟，
    也让「同一份量测可以重映射成不同旋钮」这件事变得可验证。
    """
    v = json.load(io.open(path, encoding="utf8"))
    old = (v.get("engine") or {}).get("pack")
    v["engine"] = to_engine(v)
    j, m = save(v, outdir or STYLES)
    print("[restyle] %s\n  pack %s → %s\n  写出 %s" % (
        os.path.basename(path), old, v["engine"]["pack"], j))
    if v["engine"].get("pack_proposal"):
        print("  ⚠ %s" % v["engine"]["pack_proposal"]["why"])
    return j


def main():
    ap = argparse.ArgumentParser(description="参考视频 → 视觉风格档案")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="只下载 + 读元信息")
    f.add_argument("src")
    f.add_argument("--max-h", type=int, default=1080)

    a = sub.add_parser("analyze", help="分析并出档案")
    a.add_argument("--ocr-fps", type=float, default=1.0, help="排版量测的抽帧率（OCR 贵，1fps 够）")
    a.add_argument("src")
    a.add_argument("--id", default="")
    a.add_argument("--base-fps", type=float, default=2.0)
    a.add_argument("--text-fps", type=float, default=10.0)
    a.add_argument("--thr", type=float, default=0.28)
    a.add_argument("--debug", action="store_true", help="落调试图（带文字框）")
    a.add_argument("--verify", action="store_true", help="复跑一次比指纹，验证确定性")

    b = sub.add_parser("all", help="fetch + analyze")
    b.add_argument("src")
    b.add_argument("--id", default="")
    b.add_argument("--verify", action="store_true")

    r = sub.add_parser("report", help="打印已有档案的人读版")
    r.add_argument("path")

    sub.add_parser("selftest", help="自检 pack 解析与回灌匹配")

    st = sub.add_parser("restyle", help="只重算映射，不重新量测（调阈值用）")
    st.add_argument("path")
    st.add_argument("--outdir", default="")

    ns = ap.parse_args()
    if ns.cmd == "fetch":
        got = fetch(ns.src, ns.max_h)
        print(json.dumps({"id": got["id"], "path": got["path"], "meta": got["meta"],
                          "title": (got["source"] or {}).get("title")},
                         ensure_ascii=False, indent=1))
    elif ns.cmd == "analyze":
        do_analyze(ns.src, ns.id or None, ns.base_fps, ns.text_fps, ns.thr,
                   ns.debug, ns.verify, ns.ocr_fps)
    elif ns.cmd == "all":
        do_analyze(ns.src, ns.id or None, verify=ns.verify)
    elif ns.cmd == "report":
        v = json.load(io.open(ns.path, encoding="utf8"))
        print(md_report(v))
    elif ns.cmd == "selftest":
        raise SystemExit(do_selftest())
    elif ns.cmd == "restyle":
        do_restyle(ns.path, ns.outdir or None)


if __name__ == "__main__":
    main()
