#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""主播人物跟踪：给新播放器（renderer/player/live）产出裁切中心轨迹。

为什么不用人脸检测：
  · 本机 cv2 5.0 已移除 CascadeClassifier；FaceDetectorYN 需要额外 onnx 模型，
    且训练集是真人人脸，对本项目 Live2D 立绘（二次元）召回很差（实测漏检）。
  · 直播画面里立绘是**白发高亮 + 深色背景**的强对比目标，用「HSV 亮块连通域」
    取头部质心 + 首帧头部做模板匹配（多尺度、限制在预测位置附近搜索）双源融合，
    既稳又准，而且完全不依赖外部模型权重。

输出（播放器直接消费）：
  {"fps": 4, "t0": 0.0, "dur": 336.4, "w": 1920, "h": 1080,
   "cx": [...], "cy": [...], "conf": [...], "src": "..."}
  cx/cy 为**源视频像素坐标**，索引 i 对应时刻 i/fps（相对片段起点）。

用法：
  python tools/track_host.py --src <录播.mp4> --start 2352.4 --end 2688.0 \
      --out _media_cache/live/2026-11-27/0.track.json [--overlay _chk/track]

  ⚠ 中文路径：cv2.imread/imwrite 在 Windows 上会失败，本文件一律用
    np.fromfile + cv2.imdecode / cv2.imencode。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import cv2
import numpy as np

PROC_W = 480          # 分析分辨率（源 16:9 → 480x270）
X_BAND = (0.20, 0.80)  # 只看中间带：避开左侧弹幕、右侧礼物墙
V_MIN = 0.66          # 亮度阈值（白发/皮肤）
S_MAX = 0.62


def _ffmpeg() -> str:
    return _which("ffmpeg")


def _ffprobe() -> str:
    return _which("ffprobe")


def _which(name: str) -> str:
    for c in (os.environ.get(name.upper()), name, "D:/ffmpeg/bin/%s.exe" % name):
        if not c:
            continue
        try:
            subprocess.run([c, "-version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True)
            return c
        except Exception:
            continue
    raise SystemExit("找不到 %s" % name)


def src_size(src: str):
    out = subprocess.run([_ffprobe(), "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height",
                          "-of", "csv=p=0:s=x", src],
                         capture_output=True, text=True).stdout.strip()
    try:
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except Exception:
        return 1920, 1080


def read_frames(src: str, start: float, dur: float, fps: float):
    """按固定帧率解码片段，逐帧 yield BGR ndarray（宽 480，高按源比例）。"""
    sw, sh = src_size(src)
    ph = int(round(PROC_W * sh / float(sw) / 2.0)) * 2
    fsz = PROC_W * ph * 3
    cmd = [_ffmpeg(), "-v", "error", "-ss", "%.3f" % start, "-t", "%.3f" % dur,
           "-i", src, "-an", "-vf", "fps=%g,scale=%d:%d" % (fps, PROC_W, ph),
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    while True:
        buf = p.stdout.read(fsz)
        if len(buf) < fsz:
            break
        yield np.frombuffer(buf, np.uint8).reshape(ph, PROC_W, 3)
    p.stdout.close()
    err = p.stderr.read().decode("utf-8", "ignore")
    p.stderr.close()
    p.wait()
    if err.strip():
        sys.stderr.write("[track] ffmpeg: %s\n" % err.strip()[-300:])


def head_blob(frame):
    """返回 (cx, cy, w, h, area) —— 亮块中「头部」部分的质心（480p 坐标）。"""
    h, w = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2].astype(np.float32) / 255.0
    s = hsv[:, :, 1].astype(np.float32) / 255.0
    mask = ((v >= V_MIN) & (s <= S_MAX)).astype(np.uint8)
    mask[:, : int(w * X_BAND[0])] = 0
    mask[:, int(w * X_BAND[1]):] = 0
    mask[: int(h * 0.02)] = 0
    mask[int(h * 0.99):] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, lab, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    if n <= 1:
        return None
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    area = int(stats[idx, cv2.CC_STAT_AREA])
    if area < 900:                      # 480p 下约 0.7% 画面
        return None
    x, y, bw, bh = (int(stats[idx, cv2.CC_STAT_LEFT]), int(stats[idx, cv2.CC_STAT_TOP]),
                    int(stats[idx, cv2.CC_STAT_WIDTH]), int(stats[idx, cv2.CC_STAT_HEIGHT]))
    # 头部 = 连通域上 45% 行；白发对称包住脸，质心≈脸中心
    rows = lab[y:y + max(6, int(bh * 0.45)), x:x + bw]
    ys, xs = np.nonzero(rows == idx)
    if len(xs) < 50:
        return None
    return (x + float(xs.mean()), y + float(ys.mean()), bw, bh, area)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=float, default=4.0)
    ap.add_argument("--overlay", default=None, help="目录：导出核对图（每 2s 一张）")
    a = ap.parse_args()

    dur = a.end - a.start
    sw, sh = src_size(a.src)
    sx = sw / float(PROC_W)
    sy = sh / float(int(round(PROC_W * sh / float(sw) / 2.0)) * 2)
    tpl = None
    ref_w = None
    cx_list, cy_list, conf_list = [], [], []
    prev = None
    frames_vis = []

    for i, fr in enumerate(read_frames(a.src, a.start, dur, a.fps)):
        blob = head_blob(fr)
        cx = cy = None
        conf = 0.0
        # ① 模板匹配：在上一次位置附近搜索（限制范围 = 抗弹窗/抗串位）
        if tpl is not None and prev is not None:
            t = float(i) / a.fps
            # 每 90s 用当前画面刷新模板，抗 Live2D 表情/服装变化
            best = None
            for sc in (0.94, 0.97, 1.0, 1.03, 1.06):
                tt = cv2.resize(tpl, None, fx=sc, fy=sc)
                th, tw = tt.shape[:2]
                px, py = int(prev[0]), int(prev[1])
                rad = int(fr.shape[1] * 0.14)
                x0 = max(0, px - tw // 2 - rad); x1 = min(fr.shape[1], px + tw // 2 + rad)
                y0 = max(0, py - th // 2 - int(fr.shape[0] * 0.10))
                y1 = min(fr.shape[0], py + th // 2 + int(fr.shape[0] * 0.10))
                roi = fr[y0:y1, x0:x1]
                if roi.shape[0] <= th or roi.shape[1] <= tw:
                    continue
                r = cv2.matchTemplate(roi, tt, cv2.TM_CCOEFF_NORMED)
                _, mx, _, loc = cv2.minMaxLoc(r)
                if best is None or mx > best[0]:
                    best = (mx, x0 + loc[0] + tw / 2.0, y0 + loc[1] + th / 2.0, sc)
            if best and best[0] >= 0.52:
                cx, cy, conf = best[1], best[2], float(best[0])
                if best[0] >= 0.72:
                    # 重新取模板（以当前匹配框为准，保持尺寸一致）
                    th, tw = tpl.shape[:2]
                    nx, ny = int(cx), int(cy)
                    box = fr[max(0, ny - th // 2):ny + th // 2,
                             max(0, nx - tw // 2):nx + tw // 2]
                    if box.shape[0] == th and box.shape[1] == tw:
                        tpl = box.copy()
        # ② 亮块兜底/校验
        if blob is not None:
            bx, by, bw, bh, area = blob
            if cx is None:
                cx, cy, conf = bx, by, 0.30
                # 首次（或丢失后）用亮块头部做模板
                th, tw = int(bh * 0.62), int(bw * 0.92)
                tx, ty = int(bx - tw / 2), int(by - th * 0.42)
                tx = max(0, min(fr.shape[1] - tw - 1, tx))
                ty = max(0, min(fr.shape[0] - th - 1, ty))
                cand = fr[ty:ty + th, tx:tx + tw]
                if cand.size and cand.shape[0] > 20 and cand.shape[1] > 20:
                    tpl = cand.copy()
                    ref_w = bw
            else:
                if abs(bx - cx) > fr.shape[1] * 0.10:
                    # 两源分歧大：亮块更新（人真的动了），信任亮块
                    cx, cy, conf = bx, by, 0.45
        if cx is None:
            cx, cy = prev if prev else (fr.shape[1] / 2.0, fr.shape[0] * 0.28)
            conf = 0.0
        # ③ 跳变抑制：单帧位移 > 8% 画面宽度时按 6 成收敛，避免抽风
        if prev is not None:
            dx = cx - prev[0]
            lim = fr.shape[1] * 0.08
            if abs(dx) > lim:
                cx = prev[0] + lim * (1 if dx > 0 else -1)
        prev = (cx, cy)
        cx_list.append(cx)
        cy_list.append(cy)
        conf_list.append(conf)
        if a.overlay and i % int(a.fps * 2) == 0:
            frames_vis.append((i, fr.copy(), cx, cy))

    if not cx_list:
        raise SystemExit("没有解到帧，检查 --src/--start/--end")

    # ④ 中值滤波压抖动（窗口 5 ≈ 1.25s），再接一阶低通
    cx_arr = np.array(cx_list, np.float32)
    cy_arr = np.array(cy_list, np.float32)
    k = 5
    pad = np.pad(cx_arr, (k // 2, k // 2), mode="edge")
    cx_s = np.array([np.median(pad[j:j + k]) for j in range(len(cx_arr))], np.float32)
    pad = np.pad(cy_arr, (k // 2, k // 2), mode="edge")
    cy_s = np.array([np.median(pad[j:j + k]) for j in range(len(cy_arr))], np.float32)
    alpha = 0.35
    for j in range(1, len(cx_s)):
        cx_s[j] = alpha * cx_s[j] + (1 - alpha) * cx_s[j - 1]
        cy_s[j] = alpha * cy_s[j] + (1 - alpha) * cy_s[j - 1]

    sx = sw / float(PROC_W)
    out = {
        "fps": a.fps,
        "dur": round(dur, 3),
        "w": sw, "h": sh,
        "cx": [round(float(v) * sx, 2) for v in cx_s],
        "cy": [round(float(v) * sy, 2) for v in cy_s],
        "conf": [round(float(c), 3) for c in conf_list],
        "src": os.path.basename(a.src),
        "start": round(a.start, 3),
        "end": round(a.end, 3),
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    xs = np.array(out["cx"])
    print("[track] %d 帧  cx %.0f~%.0f (中位 %.0f, 抖动 σ=%.1fpx)  低置信 %d/%d"
          % (len(xs), xs.min(), xs.max(), np.median(xs), xs.std(),
             sum(1 for c in conf_list if c < 0.35), len(conf_list)))

    if a.overlay:
        os.makedirs(a.overlay, exist_ok=True)
        # 裁切框：高度满幅 1020，宽 = 1020*1.10
        ch, cw = 1020, int(1020 * 1.10)
        for i, fr, cx, cy in frames_vis:
            cv_fr = cv2.resize(fr, (960, 540), interpolation=cv2.INTER_AREA)
            x = int(cx * sx / 2 - cw / 4)
            y = int((1080 - ch) / 2 / 2)
            cv2.rectangle(cv_fr, (max(0, x), max(0, y)),
                          (min(959, x + cw // 2), min(539, y + ch // 2)), (60, 220, 120), 2)
            cv2.circle(cv_fr, (int(cx * sx / 2), int(cy * sy / 2)), 5, (60, 120, 255), -1)
            cv2.putText(cv_fr, "%.1fs" % (i / a.fps), (12, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            p = os.path.join(a.overlay, "t_%05.1f.jpg" % (i / a.fps))
            ok, buf = cv2.imencode(".jpg", cv_fr)
            if ok:
                buf.tofile(p)
        print("[track] 核对图 → %s" % a.overlay)
    return 0


if __name__ == "__main__":
    sys.exit(main())
