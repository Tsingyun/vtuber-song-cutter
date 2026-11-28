# -*- coding: utf-8 -*-
"""面部跟踪：为歌切 MP4 输出计算「面部视觉中心」轨迹。

不是「画面中心」，也不是「头顶/头发中心」——锚点取**双眼中点**：
  1. 白发亮块（S 低 V 高）定出头部范围，只用于**限定搜索区**，不参与锚点；
  2. 在头部范围内用高饱和红瞳掩膜找两个连通域，几何约束（水平并列、面积相近、
     间距在脸宽的合理区间）筛掉腮红/嘴唇/发饰；
  3. 两瞳质心的中点即面部视觉中心；
  4. 本帧失败 → 上一帧位置（限时回溯，超过 2s 标记 hold），绝不在失败帧跳回中间。

输出 4Hz 轨迹 JSON，供渲染器逐帧线性插值 + 低通。

用法：
    python tools/face_track.py --src <录播.mp4> --start 2352.4 --end 2688.0 \
        --out _media_cache/live/2026-11-27/0.track.json [--overlay 目录]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FFMPEG = os.environ.get("FFMPEG_BIN") or "ffmpeg"

# 跟踪用的工作分辨率（宽）。1280 下眼距约 130px，足够稳定，且解码只有 1080p 的 2/3
PROC_W = 1280
TRACK_FPS = 4.0


def _ffmpeg() -> str:
    """按「环境变量 → PATH → 本机常见安装位置」找 ffmpeg。

    ⚠ 不要用「路径里含 os.path.sep 才算绝对路径」这种判断：Windows 上
    `D:/ffmpeg/bin/ffmpeg.exe` 用的是正斜杠，判断会失手退到裸 "ffmpeg"，
    结果 CreateProcess 直接 PermissionError，跟踪静默降级成「画面居中」。
    """
    for c in (FFMPEG, "ffmpeg", "D:/ffmpeg/bin/ffmpeg.exe"):
        if not c:
            continue
        if c == "ffmpeg":
            from shutil import which
            if which(c):
                return c
            continue
        if os.path.exists(c):
            return c
    return "ffmpeg"


def head_box(bgr: np.ndarray) -> tuple[int, int, int, int] | None:
    """白发亮块的外接框。只用来限定搜索范围。"""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, w = bgr.shape[:2]
    m = ((hsv[:, :, 2] >= 165) & (hsv[:, :, 1] <= 80)).astype(np.uint8) * 255
    m[:, : int(w * 0.18)] = 0          # 左侧弹幕栏
    m[:, int(w * 0.82):] = 0           # 右侧挂件栏
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(m, 8)
    if n < 2:
        return None
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[i, cv2.CC_STAT_AREA] < 3000:
        return None
    # 太小的框不可信（多半是画面里的白色 UI）
    if stats[i, cv2.CC_STAT_WIDTH] < w * 0.08 or stats[i, cv2.CC_STAT_HEIGHT] < h * 0.10:
        return None
    return tuple(int(v) for v in stats[i, :4])      # type: ignore[return-value]


def eye_pair(bgr: np.ndarray, hb: tuple[int, int, int, int],
             hue_span: int = 8) -> tuple[float, float, float, tuple, tuple] | None:
    """头部框内找双眼。返回 (cx, cy, 眼距, 左瞳, 右瞳)，坐标是 PROC 图坐标。"""
    x, y, w, h = hb
    H, W = bgr.shape[:2]
    x0, x1 = max(0, x), min(W, x + w)
    y0, y1 = max(0, y), min(H, y + h)
    roi = bgr[y0:y1, x0:x1]
    if roi.shape[0] < 40 or roi.shape[1] < 40:
        return None
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    hh, ww = roi.shape[:2]
    m = (((hsv[:, :, 0] <= hue_span) | (hsv[:, :, 0] >= 180 - hue_span))
         & (hsv[:, :, 1] >= 110) & (hsv[:, :, 2] >= 90)).astype(np.uint8) * 255
    # 眼睛不会长在下半张脸以下：砍掉下 38%（下巴/衣领/手）
    m[int(hh * 0.62):, :] = 0
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
    blobs = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        bw, bh = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
        if a < ww * hh * 0.0015 or a > ww * hh * 0.12:
            continue
        if bh == 0 or bw / bh < 0.55 or bw / bh > 3.2:
            continue
        blobs.append((a, float(cent[i][0]), float(cent[i][1])))
    if len(blobs) < 2:
        return None
    blobs.sort(key=lambda b: -b[0])
    best = None
    for i in range(min(6, len(blobs))):
        for j in range(i + 1, min(6, len(blobs))):
            a1, cx1, cy1 = blobs[i]
            a2, cx2, cy2 = blobs[j]
            dx, dy = abs(cx1 - cx2), abs(cy1 - cy2)
            if dx < ww * 0.06 or dx > ww * 0.55:
                continue
            if dy > dx * 0.42:                      # 必须大致水平并列
                continue
            if min(a1, a2) / max(a1, a2) < 0.30:    # 面积相近（同一对眼睛）
                continue
            score = (a1 + a2) - dy * 30 - abs(dx - ww * 0.26) * 4
            if best is None or score > best[0]:
                best = (score, (cx1, cy1, a1), (cx2, cy2, a2))
    if not best:
        return None
    _, p1, p2 = best
    c1 = (x0 + p1[0], y0 + p1[1])
    c2 = (x0 + p2[0], y0 + p2[1])
    d = float(np.hypot(c1[0] - c2[0], c1[1] - c2[1]))
    mx, my = (c1[0] + c2[0]) / 2.0, (c1[1] + c2[1]) / 2.0
    return mx, my, d, c1, c2


def _decode(src: str, start: float, dur: float, w: int = PROC_W):
    """NVDEC 解码 + 抽 4Hz + 缩到工作宽，逐帧 yield BGR。"""
    ff = _ffmpeg()
    h = int(round(w * 9 / 16 / 2)) * 2
    cmd = [ff, "-v", "error", "-ss", "%.3f" % start, "-t", "%.3f" % dur,
           "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", src,
           "-vf", "scale_cuda=%d:%d,fps=%g,hwdownload,format=nv12" % (w, h, TRACK_FPS),
           "-pix_fmt", "nv12", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    ysz, csz = w * h, (w // 2) * (h // 2) * 2
    try:
        while True:
            y = p.stdout.read(ysz)
            c = p.stdout.read(csz)
            if len(y) < ysz or len(c) < csz:
                break
            nv = np.frombuffer(y + c, np.uint8)
            # nv12 单平面布局：Y 全幅 + UV 交织
            yuv = nv.reshape((h * 3 // 2, w))
            yield cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
    finally:
        try:
            p.stdout.close()
        except Exception:
            pass
        p.wait()


def track(src: str, start: float, end: float, overlay: str | None = None) -> dict:
    dur = max(0.5, end - start)
    head_tpl = None
    prev = None          # 上一有效眼中心
    prev_head = None     # 上一有效头部框
    hold_since = None
    rows = []
    stats = {"eyes": 0, "tpl": 0, "hold": 0, "total": 0}
    os.makedirs(overlay, exist_ok=True) if overlay else None

    for i, bgr in enumerate(_decode(src, start, dur)):
        t = i / TRACK_FPS
        stats["total"] += 1
        hb = head_box(bgr)
        got = None
        src_kind = "hold"
        if hb:
            got = eye_pair(bgr, hb)
            if got:
                src_kind = "eyes"
        if got is None and head_tpl is not None and prev_head is not None:
            # 眼部判别失败（闭眼/手挡/表情）：在上一帧头部位置附近做模板匹配
            px, py, pw, ph = prev_head
            pad = int(pw * 0.35)
            sx0, sy0 = max(0, px - pad), max(0, py - pad)
            sx1 = min(bgr.shape[1], px + pw + pad)
            sy1 = min(bgr.shape[0], py + ph + pad)
            sub = bgr[sy0:sy1, sx0:sx1]
            th, tw = head_tpl.shape[:2]
            if sub.shape[0] >= th and sub.shape[1] >= tw:
                r = cv2.matchTemplate(sub, head_tpl, cv2.TM_CCOEFF_NORMED)
                _, mxv, _, mloc = cv2.minMaxLoc(r)
                if mxv >= 0.55:
                    # 模板左上 → 头部框左上 → 眼部落在头部框的固定相对位置
                    nx, ny = sx0 + mloc[0], sy0 + mloc[1]
                    rx, ry = (prev[0] - px) / max(1, pw), (prev[1] - py) / max(1, ph)
                    got = (nx + rx * tw, ny + ry * th, prev[2] if prev else 0, None, None)
                    hb = (nx, ny, tw, th)
                    src_kind = "tpl"
        if got is not None:
            mx, my, d = got[0], got[1], got[2]
            if prev is not None and d > 0:
                # 单帧位移上限：超过 12% 工作宽视为误检（换行/画面切换）
                if abs(mx - prev[0]) > bgr.shape[1] * 0.12:
                    got = None
                    src_kind = "hold"
        if got is None:
            stats["hold"] += 1
            if hold_since is None:
                hold_since = t
            if prev is not None:
                mx, my = prev[0], prev[1]
                d = prev[2]
            else:
                # 从未检出：给画面中位（宁可偏中间，也不许乱指）
                mx, my, d = bgr.shape[1] * 0.5, bgr.shape[0] * 0.38, 0.0
            hb_use = prev_head
        else:
            stats[src_kind] = stats.get(src_kind, 0) + 1
            hold_since = None
            hb_use = hb
            if src_kind == "eyes":
                # 首帧/换形象时更新模板（只在眼眶判别成功时更新，避免污染）
                if head_tpl is None or i % 60 == 0:
                    x, y, w, h = hb
                    head_tpl = bgr[y:y + h, x:x + w].copy()
            prev = (mx, my, d)
            prev_head = hb_use

        rows.append({"t": round(t, 3), "x": round(float(mx), 2), "y": round(float(my), 2),
                     "d": round(float(d), 2), "src": src_kind,
                     "head": list(hb_use) if hb_use else None})

        if overlay and i % 20 == 0:
            vis = bgr.copy()
            if hb_use:
                cv2.rectangle(vis, (hb_use[0], hb_use[1]),
                              (hb_use[0] + hb_use[2], hb_use[1] + hb_use[3]), (0, 255, 255), 1)
            cv2.circle(vis, (int(mx), int(my)), 5, (0, 255, 0), -1)
            cv2.putText(vis, "%s t=%.1f" % (src_kind, t), (10, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.imencode(".png", vis)[1].tofile(os.path.join(overlay, "tr_%04d.png" % i))

    xs = np.array([r["x"] for r in rows], float)
    ys = np.array([r["y"] for r in rows], float)
    ds = np.array([r["d"] for r in rows], float)
    # 「从未检出」（d<=0）的样本先按最近的有效样本线性回填，再平滑。
    # 否则片头那几帧会停在画面中位，进正片才跳到真实位置 —— 开头白白横移一段。
    ok = ds > 0
    if ok.any() and not ok.all():
        gi = np.where(ok)[0]
        ar = np.arange(xs.size)
        xs = np.interp(ar, gi, xs[gi])
        ys = np.interp(ar, gi, ys[gi])
    # 平滑：5 点中值去孤点 → 一阶低通（α=0.35，4Hz 下 τ≈0.7s）
    sm_x, sm_y = _smooth(xs), _smooth(ys)
    for r, x, y in zip(rows, sm_x, sm_y):
        r["sx"] = round(float(x), 2)
        r["sy"] = round(float(y), 2)

    valid_d = ds[ds > 0]
    return {
        "version": 1,
        "anchor": "eye_midpoint",
        "proc_w": PROC_W,
        "fps": TRACK_FPS,
        "start": round(start, 3),
        "end": round(end, 3),
        "frames": rows,
        "stats": {
            "total": stats["total"],
            "eyes": stats["eyes"],
            "tpl": stats.get("tpl", 0),
            "hold": stats["hold"],
            "eye_dist_median": round(float(np.median(valid_d)), 1) if valid_d.size else 0.0,
            "x_std": round(float(sm_x.std()), 2),
            "x_min": round(float(sm_x.min()), 1),
            "x_max": round(float(sm_x.max()), 1),
        },
    }


def _smooth(a: np.ndarray, k: int = 5, alpha: float = 0.35) -> np.ndarray:
    if a.size == 0:
        return a
    med = a.copy()
    h = k // 2
    for i in range(a.size):
        lo, hi = max(0, i - h), min(a.size, i + h + 1)
        med[i] = np.median(a[lo:hi])
    out = med.copy()
    for i in range(1, med.size):
        out[i] = out[i - 1] + alpha * (med[i] - out[i - 1])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--overlay", default=None)
    a = ap.parse_args()

    res = track(a.src, a.start, a.end, a.overlay)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    s = res["stats"]
    print("[face] %d 帧：眼 %d / 模板 %d / 保持 %d（%.1f%% 检出）｜眼距中位 %.0f ｜x 标准差 %.1f"
          % (s["total"], s["eyes"], s["tpl"], s["hold"],
             100.0 * (s["eyes"] + s["tpl"]) / max(1, s["total"]),
             s["eye_dist_median"], s["x_std"]))
    print("[face] → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
