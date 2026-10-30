# -*- coding: utf-8 -*-
"""成片规格的**唯一真源**。

为什么要有这个文件
------------------
2026-10-30 复盘发现同一份「合格」在三处各写一遍，且已经互相矛盾：

  · `song_cutter.py` 的 `specs_ok` 里写死 `fps >= 59.9`，
    而手书 PV 走的是 4K30 —— 新方向的成片在主流程里被判「规格不达标」；
  · `tools/pv_check.py` 已经改成接受 30/60；
  · `songcut/qc.py` 的 S02 又用 `TH["fps_min"]`（60 起）。

三份判据、三个答案，等于没有判据。这里把「什么算合格」收敛成一套常量 + 判定函数，
其余模块只能引用，不许再写数字。

口径（实测，2026-10）
--------------------
帧率   30 或 60（4K60 是歌切默认；手书 PV 是 4K30 + koma12），容差 0.6fps。
分辨率 1920×1080 或 3840×2160（由 res 倍率决定，二者成对出现）。
像素格式 yuv420p / bt709 / tv range —— 少一个都会在平台转码时偏色。
视频码率 16000~18500 kbps 是「不触发二压」的安全区；>19000 必二压。
音频 AAC，≤320 kbps。
"""
from __future__ import annotations

FPS_ALLOWED = (30, 60)
FPS_TOL = 0.6                 # 容器里的平均帧率是浮点，30.000 会读成 29.97/30.03

RES_ALLOWED = ((1920, 1080), (3840, 2160))

PIX_FMT = "yuv420p"
COLOR = ("bt709", "bt709", "tv")      # space / transfer / range

VBR_SAFE_KBPS = (16000, 18500)        # 安全区
VBR_HARD_MAX_KBPS = 19000             # 超过必被平台二压
ABR_MAX_KBPS = 320


def fps_ok(fps: float) -> bool:
    """帧率是否合格。容忍容器平均帧率的浮点抖动。"""
    try:
        f = float(fps)
    except (TypeError, ValueError):
        return False
    return any(abs(f - a) <= FPS_TOL for a in FPS_ALLOWED)


def fps_nearest(fps: float) -> int:
    """把实测帧率吸附到允许值（用于报告与文件名，不做判定）。"""
    try:
        f = float(fps)
    except (TypeError, ValueError):
        return 0
    return min(FPS_ALLOWED, key=lambda a: abs(a - f))


def res_ok(w, h) -> bool:
    try:
        return (int(w), int(h)) in RES_ALLOWED
    except (TypeError, ValueError):
        return False


def vbr_ok(kbps) -> bool:
    """码率是否落在安全区（低于下限会被平台压糊，高于上限同样触发二压）。"""
    try:
        v = float(kbps)
    except (TypeError, ValueError):
        return False
    return VBR_SAFE_KBPS[0] <= v <= VBR_SAFE_KBPS[1]


def color_ok(pix_fmt, space, transfer, rng) -> bool:
    return (pix_fmt == PIX_FMT and space == COLOR[0]
            and transfer == COLOR[1] and rng == COLOR[2])


def check(specs: dict) -> tuple:
    """对 `verify_specs()` 的返回做一次总判。

    → (ok: bool, fails: list[str])。fails 里是**人能看懂的一句话**，
    直接进 manifest / 日志，不再让调用方自己拼条件表达式。
    """
    fails = []
    if not res_ok(specs.get("width"), specs.get("height")):
        fails.append("分辨率 %sx%s 不在允许集合" % (specs.get("width"), specs.get("height")))
    if not fps_ok(specs.get("fps")):
        fails.append("帧率 %s 不在 %s±%s" % (specs.get("fps"), FPS_ALLOWED, FPS_TOL))
    v = specs.get("video_bitrate_kbps")
    if v is not None and not vbr_ok(v):
        fails.append("视频码率 %s kbps 不在安全区 %s" % (v, VBR_SAFE_KBPS))
    return (not fails), fails


def describe() -> str:
    """一行摘要，写进日志让人一眼看到这次按什么规格验收。"""
    return ("规格：%s 分辨率 / %s fps / %s / 码率 %s kbps / 音频 AAC≤%s"
            % ("|".join("%dx%d" % r for r in RES_ALLOWED),
               "/".join(str(f) for f in FPS_ALLOWED), PIX_FMT,
               "%d~%d" % VBR_SAFE_KBPS, ABR_MAX_KBPS))
