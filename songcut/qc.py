# -*- coding: utf-8 -*-
"""songcut.qc —— 成片自检（Quality Control），面向「全自动切片 → 全自动生成正确视频」闭环。

设计原则
--------
1. **判定化**：每一项只输出 PASS / WARN / BLOCK，并附**可定位**证据
   （成片路径、帧号、时间码、歌词行号、实测值 vs 期望值 vs 阈值）。
   禁止出现「看起来没问题」这类无法复核的结论——结论必须由数字支撑。
2. **低成本**：只解码成片，不重启浏览器、不重新渲染、不联网。
   - 一次音频解码（22050 Hz 单声道 PCM）支撑全部音频检查 + 歌词对齐判分；
   - N 次**定点 seek** 抽帧（每次 5 帧灰度小图）支撑全部画面检查，
     不做全片顺序解码（4K 全片软解需数十秒，与「轻量」冲突）；
   - 歌词检查复用 manifest / .lrc 中间产物与已有 vocal_activity 纯音频判据。
3. **抽样诚实**：画面检查是**窗口抽样**，报告会显式标注抽样窗口数与覆盖帧数，
   绝不声称「全片无问题」。

层级
----
- BLOCK：必须修，自动化流程应中止或标记该片段不合格。
- WARN ：可疑/降级判定，不阻断，但必须列出（例如参照缺失导致某项无法判定）。

退出码：0 = 无 BLOCK（WARN 允许）；1 = 存在 BLOCK；2 = 用法/运行错误。

命令行
------
    python -m songcut.qc --mp4 成片.mp4 [--lrc 成片.lrc] [--ref-lrc 源歌词.lrc]
                         [--manifest cuts/2026-09-29/manifest.json] [--json qc.json]
                         [--windows 12] [--win 5] [--seed 20260930] [--src-audio 录播.mp4 --src-ss 5814]
    python -m songcut.qc --date 2026-09-29 --workdir .     # 批量：跑 manifest 内全部成片
"""
import argparse
import difflib
import io
import json
import os
import random
import re
import subprocess
import sys
import time

import numpy as np

from songcut import config as CFG

try:                                    # 繁转简降级链（与 lyrics_fetch 一致）
    from songcut.lyrics_fetch import _t2s as t2s
except Exception:                       # pragma: no cover
    def t2s(s):
        return s

BLOCK, WARN, PASS = "BLOCK", "WARN", "PASS"

# ── 判定阈值（集中放置，便于按机型/平台调整）─────────────────────────────
TH = {
    # 规格
    "vbr_min_kbps": 16_000,             # B站二压防护下限
    "vbr_max_kbps": 18_500,             # 上限（超出必被二压）
    "fps_min": 59.9,
    # 音频
    "dur_tol_s": 0.5,                   # 成片时长 vs 期望时长
    "av_dur_tol_s": 0.25,               # 视频流 vs 音轨 时长差
    "av_start_tol_s": 0.08,             # 视频流 vs 音轨 起始时间差
    "av_xcorr_tol_s": 0.15,             # 与源片段互相关偏移容差
    "sil_db": -45.0,                    # 静音判定（dBFS，滑窗 RMS）
    "sil_block_s": 4.0,                 # 连续静音超此值 → BLOCK
    "sil_warn_s": 2.5,
    "sil_head_allow_s": 2.5,            # 片头允许静音区（head_pad 之外再放宽）
    "sil_tail_allow_s": 2.5,            # 片尾允许静音区（淡出）
    "clip_amp": 0.995,                  # 满量程比例（削波）
    "clip_block_n": 200,                # 削波样本数 BLOCK 阈值
    "clip_warn_n": 1,
    "click_step": 0.95,                 # 相邻样本跳变占满量程比例（音乐瞬态常见 0.6，爆音接近 1.0）
    "click_block_n": 2000,
    "click_warn_n": 200,
    # 画面
    "black_block": 6.0,                 # 帧平均亮度（0~255）
    "black_warn": 14.0,
    "freeze_mad": 0.01,                 # 重复帧判据：灰度小图平均绝对差（实测正常片 0.002~0.84）
    "freeze_block_ratio": 0.34,         # 重复帧窗口占比 ≥ 1/3 → BLOCK（正常片间奏实测 0%~17%）
    "flicker_abs": 0.8,                 # 孤立跳变绝对下限（灰度小图 MAD，实测片头动态约 0.6~0.84）
    "flicker_med_mult": 4.0,            # 且 > 本窗中位数的 N 倍（自适应，避免半静态段误报）
    "flicker_global_mult": 20.0,        # 整窗剧烈抖动：窗口帧差中位 > 全片基准的 N 倍（仅作参考，不判 BLOCK）
    "flicker_alt": 0.80,                # 双稳态闪烁：亮度一阶差分「符号交替率」≥ 此值
    "flicker_amp": 8.0,                 # 且平均亮度跳变幅度 ≥ 此值（灰度 0~255）
    "flicker_alt_warn": 0.60,
    "flicker_amp_warn": 4.0,
    "flicker_nb_mult": 2.0,             # 且相邻帧也在跳（"跳出去又跳回来"才是渲染 bug）
    "flicker_block_n": 3,               # 单窗内脉冲数
    "flicker_warn_n": 1,
    # 歌词
    "line_dur_min": 0.8, "line_dur_max": 20.0,
    "line_gap_sing_ratio": 0.35,  # 过慢间隔内「人声乐句覆盖率」低于此值 → 实况间歇而非缺行
    "line_dur_block_min": 0.4, "line_dur_block_max": 25.0,
    "line_gap_warn": 12.0, "line_gap_block": 20.0,
    "cov_block": 0.70, "cov_warn": 0.80,
    "first_line_max": 15.0,             # 首行起点上限（片头 padding 后）
    "anchor_min": 4,                    # 可验证锚点最少个数
    "anchor_mae_pass": 0.35, "anchor_mae_warn": 0.60,
    "anchor_ratio_pass": 0.80,          # |偏差| < 0.6s 的行占比
}

TS = r"(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?"
RE_LINE_HEAD = re.compile(r"^\s*\[" + TS + r"\]\s*(.*)$")
RE_MARK = re.compile(r"<" + TS + r">")


def _sec(m):
    a, b, c = m.group(1), m.group(2), m.group(3)
    return int(a) * 60 + int(b) + (float("0." + c) if c else 0.0)


# ── 结果模型 ────────────────────────────────────────────────────────────
class Finding(dict):
    def __init__(self, cid, group, name, sev, ok, detail, where=None, fix=""):
        super().__init__(id=cid, group=group, name=name, sev=sev, ok=bool(ok),
                         detail=detail, where=where or {}, fix=fix)


def _f(cid, group, name, detail, ok, sev_fail, where=None, fix=""):
    """ok=True → PASS；否则按 sev_fail 分级。"""
    return Finding(cid, group, name, PASS if ok else sev_fail, ok, detail, where, fix)


# ── 基础工具 ────────────────────────────────────────────────────────────
def ffprobe_json(path):
    out = subprocess.run([CFG.ffprobe(), "-v", "error", "-print_format", "json",
                          "-show_streams", "-show_format", path],
                         capture_output=True, text=True, encoding="utf-8").stdout
    return json.loads(out or "{}")


def decode_audio(ffmpeg, path, sr=22050):
    """解码为单声道 PCM（float32，-1~1）。供全部音频 + 歌词对齐判分复用。"""
    p = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-vn",
                        "-ac", "1", "-ar", str(sr), "-f", "s16le", "-c:a", "pcm_s16le", "-"],
                       capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("音频解码失败: %s" % (p.stderr or b"")[-300:])
    return np.frombuffer(p.stdout, dtype="<i2").astype(np.float32) / 32768.0, sr


def grab_windows(ffmpeg, path, starts, win, w=192, h=108):
    """定点 seek 抽连续窗口。返回 {start: np.ndarray(win,h,w) uint8}。

    用 `-ss`（输入侧 seek）+ 只解 win 帧，避免 4K 全片顺序解码。
    """
    out = {}
    for s in starts:
        t = max(0.0, s / 60.0 - 0.001)
        p = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", "%.3f" % t, "-i", path,
             "-frames:v", str(win), "-vf", "scale=%d:%d:flags=area" % (w, h),
             "-pix_fmt", "gray", "-f", "rawvideo", "-"],
            capture_output=True)
        buf = p.stdout
        n = len(buf) // (w * h)
        if n == 0:
            out[s] = None
            continue
        arr = np.frombuffer(buf[:n * w * h], dtype=np.uint8).reshape(n, h, w)
        out[s] = arr
    return out


def parse_lrc(text):
    """→ [{t, text, marks:[...], end}]。兼容行内逐字 <mm:ss.xx> 标签。"""
    rows = []
    for raw in (text or "").splitlines():
        m = RE_LINE_HEAD.match(raw)
        if not m:
            continue
        t = _sec(m)
        body = m.group(4)
        marks = [_sec(x) for x in RE_MARK.finditer(body)]
        plain = RE_MARK.sub("", body).strip()
        if not plain:
            continue
        end = max(marks) if marks else t
        rows.append({"t": t, "text": plain, "marks": marks, "end": end})
    rows.sort(key=lambda r: r["t"])
    return rows


def norm_line(s):
    """文本比对规范化：繁转简 + 去空白。不改标点（标点差异也要能发现）。"""
    return t2s(re.sub(r"[\s\u3000]", "", s or ""))


def fmt_t(s):
    s = max(0.0, float(s))
    return "%02d:%06.3f" % (int(s // 60), s - 60 * int(s // 60))


# ── 检查项：容器 / 规格 ─────────────────────────────────────────────────
def check_container(info, want_w, want_h):
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = info.get("format", {})
    dur = float(fmt.get("duration", 0) or 0)
    size = float(fmt.get("size", 0) or 0)
    vbr = int(v.get("bit_rate") or 0)
    abr = int(a.get("bit_rate") or 0)
    if not vbr and dur > 0:
        vbr = int(max(0.0, size - abr * dur / 8) * 8 / dur)
    num, den = (v.get("avg_frame_rate") or "0/1").split("/")
    fps = (float(num) / float(den)) if float(den or 0) else 0.0
    nframes = int(v.get("nb_frames") or 0)
    ctx = {"duration": round(dur, 3), "vbr_kbps": int(vbr / 1000), "fps": round(fps, 3),
           "size_mb": round(size / 1048576, 1), "nframes": nframes,
           "w": v.get("width"), "h": v.get("height"), "pix_fmt": v.get("pix_fmt"),
           "vstart": float(v.get("start_time") or 0), "astart": float(a.get("start_time") or 0),
           "vdur": float(v.get("duration") or 0), "adur": float(a.get("duration") or 0)}

    out = []
    out.append(_f("S01", "规格", "分辨率",
                  "%sx%s" % (v.get("width"), v.get("height")),
                  v.get("width") == want_w and v.get("height") == want_h, BLOCK,
                  {"期望": "%dx%d" % (want_w, want_h)}, "检查 OUT_RES / render_song.cjs 的 res 参数"))
    out.append(_f("S02", "规格", "帧率", "%.3f fps" % fps, fps >= TH["fps_min"], BLOCK,
                  {"阈值": TH["fps_min"]}, "渲染端 -r 60 未生效或编码器丢帧，重渲该片段"))
    out.append(_f("S03", "规格", "像素格式/色域",
                  "%s / %s / %s / range=%s" % (v.get("pix_fmt"), v.get("color_space"),
                                               v.get("color_transfer"), v.get("color_range")),
                  v.get("pix_fmt") == "yuv420p" and v.get("color_space") == "bt709"
                  and v.get("color_range") == "tv" and v.get("color_transfer") == "bt709", BLOCK,
                  {}, "补 -vf scale=...:out_color_matrix=bt709 与 -color* 系列；NVENC 需 h264_metadata bsf"))
    out.append(_f("S04", "规格", "视频码率", "%d kbps" % (vbr // 1000),
                  TH["vbr_min_kbps"] <= vbr / 1000 <= TH["vbr_max_kbps"], BLOCK,
                  {"安全区": "%d~%d kbps" % (TH["vbr_min_kbps"], TH["vbr_max_kbps"])},
                  "低于下限会被平台二压（糊）；高于上限同样触发二压。调 OUT_BITRATE"))
    out.append(_f("S05", "规格", "轨道数", "%d 轨" % len(info.get("streams", [])),
                  len(info.get("streams", [])) == 2, BLOCK,
                  {}, "混流 -map 需只保留 0:v:0 与 1:a:0"))
    return out, ctx, bool(a)


# ── 检查项：音频 ────────────────────────────────────────────────────────
def check_audio(x, sr, ctx, want_dur=None, head_pad=0.0, tag=""):
    out = []
    n = len(x)
    dur = n / float(sr)

    # A01 音轨存在（由调用方保证已进入本函数）

    # A02 时长
    if want_dur is None:
        out.append(Finding("A02", "音频", "时长符合预期", WARN, False,
                           "无期望时长（manifest 缺 cut_start/cut_end），跳过判定",
                           {"实测": round(dur, 3)},
                           "用 --manifest 传入片段条目，或确保 produce_one 写入 manifest"))
    else:
        d = abs(dur - want_dur)
        out.append(_f("A02", "音频", "时长符合预期",
                      "%.3fs（期望 %.3fs，偏差 %+.3fs）" % (dur, want_dur, dur - want_dur),
                      d <= TH["dur_tol_s"], BLOCK,
                      {"容差": TH["dur_tol_s"], "期望": round(want_dur, 3)},
                      "切点（cut_start/cut_end）或 head_pad 与成片不一致，核对混流 -shortest"))

    # A03 音画结构性同步
    dv = abs(ctx["vdur"] - ctx["adur"])
    ds = abs(ctx["vstart"] - ctx["astart"])
    out.append(_f("A03", "音频", "音画同步(结构)",
                  "时长差 %.3fs / 起始差 %.3fs" % (dv, ds),
                  dv <= TH["av_dur_tol_s"] and ds <= TH["av_start_tol_s"], BLOCK,
                  {"时长容差": TH["av_dur_tol_s"], "起始容差": TH["av_start_tol_s"]},
                  "混流时音画起点/长度不一致；检查 -shortest 与音轨 -ss"))

    # A04 静音段
    wl, hop = int(0.5 * sr), int(0.25 * sr)
    if n >= wl:
        # O(n) 滑窗 RMS（cumsum 差分）。⚠ 勿改回 np.convolve：O(n·wl) 在 160s 素材上要跑数分钟
        c = np.concatenate(([0.0], np.cumsum(x * x, dtype=np.float64)))
        idx = np.arange(0, n - wl + 1, hop)
        rms = np.sqrt(np.maximum(c[idx + wl] - c[idx], 0.0) / wl + 1e-12)
        db = 20 * np.log10(rms + 1e-9)
        silent = db < TH["sil_db"]
        segs, i = [], 0
        while i < len(silent):
            if silent[i]:
                j = i
                while j + 1 < len(silent) and silent[j + 1]:
                    j += 1
                t0, t1 = i * hop / float(sr), (j * hop + wl) / float(sr)
                segs.append((t0, t1))
                i = j + 1
            else:
                i += 1
        head_ok = head_pad + TH["sil_head_allow_s"]
        bad_b = [s for s in segs if (s[1] - s[0]) > TH["sil_block_s"]
                 and s[0] > head_ok and s[1] < dur - TH["sil_tail_allow_s"]]
        bad_w = [s for s in segs if TH["sil_warn_s"] < (s[1] - s[0]) <= TH["sil_block_s"]
                 and s[0] > head_ok and s[1] < dur - TH["sil_tail_allow_s"]]
        det = "静音段 %d 段（>%s dBFS，阈值 %.1fs）" % (len(segs), TH["sil_db"], TH["sil_warn_s"])
        if bad_b:
            det += "；超限: " + ", ".join("%s~%s" % (fmt_t(a), fmt_t(b)) for a, b in bad_b[:6])
            sev, ok = BLOCK, False
        elif bad_w:
            det += "；可疑: " + ", ".join("%s~%s" % (fmt_t(a), fmt_t(b)) for a, b in bad_w[:6])
            sev, ok = WARN, True
        else:
            sev, ok = PASS, True
        out.append(Finding("A04", "音频", "无异常静音", sev, ok, det,
                           {"阈值": TH["sil_block_s"], "片头豁免": round(head_ok, 2),
                            "片尾豁免": TH["sil_tail_allow_s"]},
                           "片段中间出现长静音 → 切点落在说话段/伴奏间隙，回调 cut_start/cut_end"))

    # A05 爆音 / 削波
    amp = np.abs(x)
    nclip = int((amp >= TH["clip_amp"]).sum())
    nclick = int((np.abs(np.diff(x)) >= TH["click_step"]).sum())
    if nclip >= TH["clip_block_n"] or nclick >= TH["click_block_n"]:
        sev, ok = BLOCK, False
    elif nclip >= TH["clip_warn_n"] or nclick >= TH["click_warn_n"]:
        sev, ok = WARN, True
    else:
        sev, ok = PASS, True
    out.append(Finding("A05", "音频", "无爆音/削波", sev, ok,
                       "削波样本 %d，突变样本 %d（总 %d）" % (nclip, nclick, n),
                       {"削波阈值": TH["clip_block_n"], "突变阈值": TH["click_block_n"],
                        "峰值": round(float(amp.max()), 4)},
                       "加 afade 淡入淡出 / limiter；检查切点是否切在掌声或爆音上"))
    return out


def check_av_xcorr(ffmpeg, mp4, src, ss, x, sr, ctx):
    """可选：与源片段做互相关，给出音画内容级偏移（--src-audio 时启用）。"""
    need = int(ctx["duration"] * sr) + sr
    p = subprocess.run([ffmpeg, "-v", "error", "-ss", "%.3f" % max(0.0, ss), "-i", src,
                        "-vn", "-ac", "1", "-ar", str(sr), "-t", "%.3f" % (ctx["duration"] + 2),
                        "-f", "s16le", "-c:a", "pcm_s16le", "-"], capture_output=True)
    if p.returncode != 0 or len(p.stdout) < sr:
        return Finding("A06", "音频", "音画同步(内容)", WARN, True,
                       "源片段解码失败，跳过内容级同步判定", {"src": src}, "")
    y = np.frombuffer(p.stdout, dtype="<i2").astype(np.float32) / 32768.0
    m = min(len(x), len(y), need)
    if m < sr:
        return Finding("A06", "音频", "音画同步(内容)", WARN, True, "素材过短，跳过", {}, "")
    a, b = x[:m] - x[:m].mean(), y[:m] - y[:m].mean()
    nfft = 1 << (int(np.ceil(np.log2(2 * m))))
    cc = np.fft.irfft(np.fft.rfft(a, nfft) * np.conj(np.fft.rfft(b, nfft)), nfft)
    k = int(np.argmax(np.abs(cc[: nfft // 2]))) if True else 0
    arr = cc[: nfft // 2]
    k = int(np.argmax(np.abs(arr)))
    if k > nfft // 4:
        k -= nfft // 2
    off = k / float(sr)
    return _f("A06", "音频", "音画同步(内容)",
              "相对源片段偏移 %+.3fs（互相关）" % off,
              abs(off) <= TH["av_xcorr_tol_s"], BLOCK,
              {"容差": TH["av_xcorr_tol_s"], "src": os.path.basename(src), "ss": round(ss, 3)},
              "成片音轨相对源有整体偏移 → 混流 -ss / -shortest 或切片起点有误")


# ── 检查项：画面 ────────────────────────────────────────────────────────
def check_video(ffmpeg, mp4, nframes, windows=12, win=10, seed=20260930, deep=False):
    out = []
    if nframes <= 0:
        return [Finding("V01", "画面", "帧数", BLOCK, False, "无法读取帧数", {}, "")], 0

    # V01 帧数
    exp = int(round(float(nframes)))
    out.append(Finding("V01", "画面", "帧数", PASS, True,
                       "%d 帧（%.2fs @60）" % (exp, exp / 60.0), {"nframes": exp}, ""))

    # 抽样：首 1 窗 + 尾 1 窗 + 随机（固定种子可复现）
    rnd = random.Random(seed)
    starts = [0, max(0, exp - win)]
    for _ in range(max(0, windows - 2)):
        starts.append(rnd.randrange(0, max(1, exp - win)))
    starts = sorted(set(starts))
    frames = grab_windows(ffmpeg, mp4, starts, win)
    got = sum(0 if v is None else len(v) for v in frames.values())
    miss = [s for s, v in frames.items() if v is None]
    if miss:
        out.append(Finding("V02", "画面", "抽帧可用", BLOCK, False,
                           "窗口 %s 抽帧失败（视频短于预期或被截断）" % miss[:5],
                           {"期望窗口": len(starts)}, "成片可能被 -shortest 截断，核对时长"))

    black, freeze_runs, flick, violent, per_win = [], [], [], [], []
    for s, arr in frames.items():
        if arr is None or len(arr) < 2:
            continue
        g = arr.astype(np.float32)
        mean = g.mean(axis=(1, 2))
        for k, mv in enumerate(mean):
            if mv < TH["black_block"]:
                black.append((s + k, float(mv), "黑帧"))
            elif mv < TH["black_warn"]:
                black.append((s + k, float(mv), "偏暗"))
        d = np.abs(np.diff(g, axis=0)).mean(axis=(1, 2))
        # 卡顿：整个窗口每一对都「几乎不动」才算静止（间奏/静态背景的短时静止属正常）
        if len(d) and bool((d < TH["freeze_mad"]).all()):
            freeze_runs.append(s)
        per_win.append((s, d, mean))

    # 闪烁基准必须取「跨窗口全局中位」而非窗内中位：整窗持续跳变时窗内中位会被自身抬高，
    # 导致真正的闪烁反而判不出来（实测注入闪烁可漏检为 0 处）。
    meds = [float(np.median(d)) for _s, d, _m in per_win if len(d)]
    base = float(np.median(meds)) if meds else 0.0
    stab, stab_w = [], []
    for s, d, mean in per_win:
        m = float(np.median(d))
        if m > max(TH["flicker_abs"], TH["flicker_global_mult"] * base):
            violent.append((s, round(m, 3)))          # 整窗帧间变化远超全片基准（仅参考，不单独判 BLOCK）
        # 双稳态闪烁：亮度一阶差分的「符号交替率」接近 1 = 每帧反向 = 来回跳；
        # 正常动画是渐变（符号少变），粒子随机游走交替率约 0.5。此判据不依赖幅度基准，
        # 因此不会出现「闪烁占半片 → 基准被自身抬高 → 漏检」。
        if len(mean) >= 4:
            dd = np.diff(mean)
            sgn = np.sign(dd)
            sgn = sgn[sgn != 0]
            if len(sgn) >= 3:
                alt = float((np.diff(sgn) != 0).mean())
                amp = float(np.abs(dd).mean())
                if alt >= TH["flicker_alt"] and amp >= TH["flicker_amp"]:
                    stab.append((s, round(alt, 2), round(amp, 1)))
                elif alt >= TH["flicker_alt_warn"] and amp >= TH["flicker_amp_warn"]:
                    stab_w.append((s, round(alt, 2), round(amp, 1)))
        # 帧级孤立脉冲：本帧跳变显著高于本窗基准，且邻帧也在跳（"跳出去又跳回来"）
        for k in range(len(d)):
            nb = max(d[k - 1] if k > 0 else 0.0, d[k + 1] if k + 1 < len(d) else 0.0)
            if (d[k] > max(TH["flicker_abs"], TH["flicker_med_mult"] * m)
                    and nb > TH["flicker_nb_mult"] * m):
                flick.append((s + k, float(d[k]), float(nb), round(m, 4)))

    nb_block = [b for b in black if b[2] == "黑帧"]
    if nb_block:
        sev, ok = BLOCK, False
    elif black:
        sev, ok = WARN, True
    else:
        sev, ok = PASS, True
    det = "抽样 %d 帧：黑帧 %d，偏暗 %d" % (got, len(nb_block), len(black) - len(nb_block))
    if black:
        det += "；例：" + ", ".join("f%d(亮度%.1f)" % (f, m) for f, m, _ in black[:5])
    out.append(Finding("V03", "画面", "无黑帧", sev, ok, det,
                       {"黑帧阈值": TH["black_block"], "偏暗阈值": TH["black_warn"],
                        "抽样帧": got},
                       "黑帧 → 渲染首帧未预热/素材未加载；定位到帧号后用 shot_frame.cjs 复现"))

    nw = len([1 for v in frames.values() if v is not None and len(v) >= 2])
    ratio = (len(freeze_runs) / nw) if nw else 0.0
    if ratio >= TH["freeze_block_ratio"]:
        sev, ok = BLOCK, False
    elif freeze_runs:
        sev, ok = WARN, True
    else:
        sev, ok = PASS, True
    out.append(Finding("V04", "画面", "无重复帧(卡顿)", sev, ok,
                       "整窗逐像素重复 %d/%d 处（%.0f%%）%s"
                       % (len(freeze_runs), nw, ratio * 100,
                          ("；起点帧 %s" % freeze_runs[:6] if freeze_runs else "")),
                       {"重复帧判据MAD": TH["freeze_mad"], "BLOCK占比": TH["freeze_block_ratio"],
                        "重复窗口起点帧": freeze_runs[:8],
                        "说明": "半静态画面（间奏/尾奏）本就近似不变，此处只判「逐像素重复」"},
                       "占比过半 → 渲染卡死/素材未加载，画面被同一帧复制填充；单窗多为片尾，需人工确认"))

    if stab or len(flick) >= TH["flicker_block_n"]:
        sev, ok = BLOCK, False
    elif stab_w or flick or violent:
        sev, ok = WARN, True
    else:
        sev, ok = PASS, True
    det = "双稳态闪烁窗口 %d；疑似 %d；孤立脉冲 %d（全片帧差基准 %.3f，剧烈窗口 %d）" % (
        len(stab), len(stab_w), len(flick), base, len(violent))
    if stab:
        det += "；闪烁: " + ", ".join("f%d(交替率%.2f 幅度%.0f)" % (a, b, c) for a, b, c in stab[:5])
    if flick:
        det += "；脉冲: " + ", ".join("f%d(%.2f)" % (a, b) for a, b, _c, _d in flick[:5])
    out.append(Finding("V05", "画面", "无闪烁/孤立跳变", sev, ok, det,
                       {"全片帧差基准": round(base, 4), "交替率阈值": TH["flicker_alt"],
                        "幅度阈值": TH["flicker_amp"], "闪烁窗口": stab[:8],
                        "疑似窗口": stab_w[:8], "脉冲帧": [f[0] for f in flick[:8]],
                        "判据": "亮度一阶差分符号交替率（闪烁=来回跳≈1.0；渐变动画≈0；粒子随机游走≈0.5）"},
                       "帧间不连续 → 卡拉OK填充/模糊档位未插值，检查 karaokeWidth/drawBlurred"))
    # V06 帧节奏（深度项）：全片 packet 时间戳扫描。实测本机 ffprobe 扫 160s 4K 素材需 60s+
    # 且 pkt_duration 输出为空 ⇒ 默认关闭，只由 --deep 触发；关闭时必须显式标注"未执行"。
    if not deep:
        out.append(Finding("V06", "画面", "帧节奏均匀", WARN, True,
                           "未执行（需 --deep；全片 packet 扫描实测约 60s/160s 素材，与「轻量」冲突）",
                           {}, "需要逐帧节奏铁证时加 --deep；日常由 V01 帧数 + V04 重复帧覆盖"))
    else:
        try:
            p = subprocess.run([CFG.ffprobe(), "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "frame=pkt_duration_time,pkt_pts_time",
                                "-of", "csv=p=0", mp4], capture_output=True, text=True, timeout=300)
            rows = [ln.split(",") for ln in p.stdout.splitlines() if ln.strip()]
            vals = np.array([float(r[0]) for r in rows if r and r[0] not in ("N/A", "")],
                            dtype=np.float64)
            if len(vals):
                nom = 1.0 / 60.0
                bad = int((np.abs(vals - nom) > 0.002).sum())
                out.append(Finding("V06", "画面", "帧节奏均匀", BLOCK if bad > 3 else PASS, bad <= 3,
                                   "异常帧间隔 %d/%d（标称 %.4fs，容差 0.002s）" % (bad, len(vals), nom),
                                   {"异常帧数": bad, "总帧数": int(len(vals)),
                                    "间隔范围": [round(float(vals.min()), 5), round(float(vals.max()), 5)]},
                                   "帧间隔不均 → 渲染丢帧或拼接处时间戳错位，检查两阶段 concat 与 -r 60"))
            else:
                out.append(Finding("V06", "画面", "帧节奏均匀", WARN, True,
                                   "ffprobe 未返回帧时间戳（字段为空），无法判定", {}, ""))
        except Exception as e:                  # pragma: no cover
            out.append(Finding("V06", "画面", "帧节奏均匀", WARN, True,
                               "帧节奏检查失败：%s" % str(e)[:80], {}, ""))
    return out, got


# ── 检查项：歌词 ────────────────────────────────────────────────────────
def check_lyrics(rows, raw_text, ref_rows, dur, x=None, sr=22050):
    out = []
    if not rows:
        return [Finding("L01", "歌词", "歌词存在", BLOCK, False, "未找到歌词（无 .lrc 或空）", {},
                        "produce_one 未落盘 .lrc，或 lyrics_fetch 整首未命中")]

    # L01 存在
    out.append(Finding("L01", "歌词", "歌词存在", PASS, True,
                       "%d 行，末行 %s，片段 %.1fs" % (len(rows), fmt_t(rows[-1]["t"]), dur),
                       {"n": len(rows)}, ""))

    # L02 文本与源逐字一致
    if ref_rows is None:
        out.append(Finding("L02", "歌词", "文本与源一致", WARN, False,
                           "未提供参照歌词（--ref-lrc / manifest.lrc_src_path），无法判定",
                           {}, "传入源歌词以启用：拦截漏行、翻唱版、繁简错乱"))
    else:
        a = [norm_line(r["text"]) for r in rows]
        b = [norm_line(r["text"]) for r in ref_rows]
        sm = difflib.SequenceMatcher(None, b, a, autojunk=False)
        miss = [i for tag, i1, i2, j1, j2 in sm.get_opcodes()
                if tag in ("delete", "replace") for i in range(i1, i2)]
        extra = [j for tag, i1, i2, j1, j2 in sm.get_opcodes()
                 if tag in ("insert", "replace") for j in range(j1, j2)]
        # 现场加唱（重复副歌）产生的多余行：文本与源已有行逐字相同 → 实况重唱，不是抓词错误
        src_texts = set(b)
        dup_extra = [j for j in extra if norm_line(rows[j]["text"]) in src_texts]
        real_extra = [j for j in extra if j not in set(dup_extra)]
        if miss or real_extra:
            sev = BLOCK if len(miss) + len(real_extra) > max(2, 0.05 * len(b)) else WARN
            det = "源 %d 行 / 成片 %d 行；缺 %d 行，多 %d 行（另 %d 行为现场重唱）" % (
                len(b), len(a), len(miss), len(real_extra), len(dup_extra))
            if miss:
                det += "；缺: " + ", ".join("L%d「%s」" % (i + 1, ref_rows[i]["text"][:12])
                                           for i in miss[:6])
            if extra:
                det += "；多: " + ", ".join("「%s」" % rows[j]["text"][:12] for j in extra[:4])
            out.append(Finding("L02", "歌词", "文本与源一致", sev, False, det,
                               {"缺行索引": miss[:12], "多行索引": extra[:12],
                                "源行数": len(b), "成片行数": len(a)},
                               "clean_lrc 剪行阈值过紧会吞掉 outro/重复副歌；或命中翻唱版歌词"))
        else:
            out.append(Finding("L02", "歌词", "文本与源一致", PASS, True,
                               "%d 行逐字一致（繁简/空白已归一化）" % len(a), {}, ""))

    # L03 编码/乱码
    bad = []
    for i, r in enumerate(rows):
        s = r["text"]
        if "\ufffd" in s or any(ord(c) < 32 and c not in "\t" for c in s):
            bad.append((i, "替换符/控制字符"))
        elif t2s(s) != s:
            bad.append((i, "疑似繁体残留"))
    if bad:
        out.append(Finding("L03", "歌词", "无乱码/繁简正确",
                           BLOCK if any(k == "替换符/控制字符" for _, k in bad) else WARN, False,
                           "%d 行异常：%s" % (len(bad), "; ".join("L%d %s「%s」"
                                                              % (i + 1, k, rows[i]["text"][:10])
                                                              for i, k in bad[:5])),
                           {"行号": [i + 1 for i, _ in bad[:12]]},
                           "opencc/zhconv 未生效时补装 opencc-python-reimplemented；检查 _T2S_OK"))
    else:
        out.append(Finding("L03", "歌词", "无乱码/繁简正确", PASS, True, "%d 行无异常字符" % len(rows), {}, ""))

    # L04 时间戳单调
    bad = [(i, rows[i]["t"], rows[i + 1]["t"])
           for i in range(len(rows) - 1) if rows[i + 1]["t"] < rows[i]["t"] - 1e-6]
    out.append(Finding("L04", "歌词", "时间轴单调", BLOCK if bad else PASS, not bad,
                       "逆序 %d 处%s" % (len(bad), ("；例 L%d %s→%s" %
                                                  (bad[0][0] + 1, fmt_t(bad[0][1]), fmt_t(bad[0][2]))
                                        if bad else "")),
                       {"逆序行": bad[:8]}, "render_lrc/ctc_align 输出未排序，检查时间轴生成分支"))

    # L05 单行时长
    too_fast, too_slow, warn_r, long_gap = [], [], [], []
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["t"] if i + 1 < len(rows) else min(dur, r["end"] + 1.0)
        d = nxt - r["t"]
        if d < TH["line_dur_block_min"]:
            too_fast.append((i, d))
        elif d > TH["line_dur_block_max"]:
            # 超长间隔：实况说话/间奏的音频无法与唱歌确定性区分（说话也被算进人声
            # 乐句，实测闲聊段覆盖 78%），在此判「缺行」会假 BLOCK → 移交 L06 人工确认
            long_gap.append((i, d))
        elif d < TH["line_dur_min"] or d > TH["line_dur_max"]:
            warn_r.append((i, d))

    # 过慢细分：用「人声乐句覆盖率」判定间隔内是否真的在唱。
    #   演唱 = 连续乐句（vocal_activity 的 run 长）；说话 = 碎片短句。
    #   ⚠ 不能用「电平高低」判定：现场伴奏常全程在响，主播停下聊天时电平与全片
    #     均值几乎相同（本场实测 -17dB vs -17.1dB），会把真实间歇误判成缺行。
    runs_x = None
    if x is not None and len(x):
        try:
            from songcut import vocal_activity as VA
            runs_x, _sal, _ts = VA.detect_phrases(x, sr)
        except Exception:
            runs_x = None

    def _sing_ratio(i, d):
        """间隔 [t0, t0+d] 被人声乐句覆盖的比例。"""
        if not runs_x:
            return None
        t0, t1 = float(rows[i]["t"]), float(rows[i]["t"]) + float(d)
        cov = 0.0
        for (a, b) in runs_x:
            cov += max(0.0, min(float(b), t1) - max(float(a), t0))
        return cov / max(1e-6, float(d))

    slow_active, slow_gap = [], []
    for i, d in too_slow:
        r = _sing_ratio(i, d)
        if r is None:                            # 无音频/检测失败 → 保守按 BLOCK
            slow_active.append((i, d, None))
        elif r >= TH["line_gap_sing_ratio"]:
            slow_active.append((i, d, round(r, 2)))    # 期间在唱却没分到行 = 缺行
        else:
            slow_gap.append((i, d, round(r, 2)))       # 期间没在唱 = 实况间歇

    if too_fast or slow_active:
        sev, ok = BLOCK, False
    elif too_slow or warn_r or long_gap:
        sev, ok = WARN, True
    else:
        sev, ok = PASS, True
    det = "过快 %d，过慢 %d（其中疑似缺行 %d / 实况间歇 %d），可疑 %d，超长间隔 %d" % (
        len(too_fast), len(too_slow), len(slow_active), len(slow_gap), len(warn_r), len(long_gap))
    for lab, lst in (("过快", too_fast), ("可疑", warn_r)):
        if lst:
            det += "；%s: %s" % (lab, ", ".join("L%d(%.1fs)" % (i + 1, d) for i, d in lst[:5]))
    if slow_active:
        det += "；疑似缺行(间隔内仍在唱): " + ", ".join(
            "L%d(%.1fs%s)" % (i + 1, d, "" if r is None else " 演唱占比%.0f%%" % (r * 100))
            for i, d, r in slow_active[:5])
    if slow_gap:
        det += "；实况间歇(需人工确认): " + ", ".join(
            "L%d(%.1fs 演唱占比%.0f%%)" % (i + 1, d, r * 100) for i, d, r in slow_gap[:5])
    if long_gap:
        det += "；超长间隔(移交L06人工确认): " + ", ".join(
            "L%d(%.1fs)" % (i + 1, d) for i, d in long_gap[:5])
    out.append(Finding("L05", "歌词", "单行时长合理", sev, ok, det,
                       {"合理区间": [TH["line_dur_min"], TH["line_dur_max"]],
                        "BLOCK区间外": [TH["line_dur_block_min"], TH["line_dur_block_max"]],
                        "间歇判据": "间隔内人声乐句覆盖率 < %.0f%% 视为实况间歇（WARN），"
                                   "否则为疑似缺行（BLOCK）" % (TH["line_gap_sing_ratio"] * 100)},
                       "过快 → 时间轴被压缩（速度比 slope 跑飞）；疑似缺行 → 检查是否漏 outro/重复副歌；"
                       "实况间歇 → 主播中途聊天/重启伴奏，需用 FunASR 转写确认该段确未在演唱"))

    # L06 行间隔
    gaps = []
    for i, r in enumerate(rows[:-1]):
        g = rows[i + 1]["t"] - r["end"]
        if g > TH["line_gap_warn"]:
            gaps.append((i, g))
    # 超长间隔不再 BLOCK：间奏/实况说话/缺行纯音频不可判别（vocal_activity 把说话
    # 也算人声乐句），正常实况歌切几乎必有主播闲聊间隔 → WARN 提示人工确认，
    # 缺行由 L05(≤25s 覆盖率判据) 与 L07(尾部覆盖) 兜底。
    if gaps:
        sev, ok = WARN, True
    else:
        sev, ok = PASS, True
    out.append(Finding("L06", "歌词", "行间隔合理", sev, ok,
                       "超 %ss 间隔 %d 处%s" % (TH["line_gap_warn"], len(gaps),
                                             ("；例 L%d→L%d 空 %.1fs" % (gaps[0][0] + 1,
                                                                      gaps[0][0] + 2, gaps[0][1])
                                              if gaps else "")),
                       {"WARN": TH["line_gap_warn"], "原BLOCK阈值(仅提示)": TH["line_gap_block"],
                        "位置": [[i + 1, round(g, 2)] for i, g in gaps[:8]]},
                       "大间隔多半是中间掉了几行歌词（间奏除外，需听音频确认）"))

    # L07 覆盖率
    cov = (rows[-1]["end"] / dur) if dur > 1 else 1.0
    if cov < TH["cov_block"]:
        sev, ok = BLOCK, False
    elif cov < TH["cov_warn"]:
        sev, ok = WARN, True
    else:
        sev, ok = PASS, True
    out.append(Finding("L07", "歌词", "尾部覆盖", sev, ok,
                       "末行 %s / 片段 %.1fs = %.0f%%" % (fmt_t(rows[-1]["end"]), dur, cov * 100),
                       {"BLOCK<": TH["cov_block"], "WARN<": TH["cov_warn"]},
                       "末行远早于片段结束 → 尾部歌词（重复副歌/outro）缺失，需联网复核完整版"))

    # L08 首行位置
    t0 = rows[0]["t"]
    out.append(_f("L08", "歌词", "首行位置合理",
                  "首行 %s" % fmt_t(t0), t0 <= TH["first_line_max"], WARN,
                  {"上限": TH["first_line_max"]},
                  "首行过晚 → head_pad 未计入或时间轴整体后移，检查 _shift_lrc"))

    # L09 与音频对齐（复用 vocal_activity 纯音频判据，独立于任何对齐算法）
    if x is None:
        out.append(Finding("L09", "歌词", "歌词与音频对齐", WARN, False,
                           "无音频样本，跳过（需解码成片音轨）", {}, ""))
        return out
    try:
        from songcut import vocal_activity as VA
        from songcut.verify_sync import match_anchors
        runs, _sal, _ts = VA.detect_phrases(x, sr)
        res = match_anchors(rows, runs)
        devs = []
        for (i, j, d, anchor) in res:
            if d is None or not anchor or abs(d) > 2.5:
                continue
            t = rows[i]["t"]
            if any(s + 0.35 < t <= e + 0.3 for (s, e) in runs):
                continue                        # legato 内部行，单点不可测
            devs.append((i, d))
        if len(devs) < TH["anchor_min"]:
            out.append(Finding("L09", "歌词", "歌词与音频对齐", WARN, False,
                               "可验证锚点仅 %d 个（<%d），样本不足无法判定"
                               % (len(devs), TH["anchor_min"]),
                               {"锚点数": len(devs)},
                               "多为连唱（legato）曲目时属正常；可改用 CTC 对齐后复检"))
        else:
            arr = np.array([d for _, d in devs])
            mae = float(np.abs(arr).mean())
            ratio = float((np.abs(arr) < 0.6).mean())
            if mae > TH["anchor_mae_warn"] or ratio < TH["anchor_ratio_pass"]:
                sev, ok = BLOCK, False
            elif mae > TH["anchor_mae_pass"]:
                sev, ok = WARN, True
            else:
                sev, ok = PASS, True
            worst = sorted(devs, key=lambda kv: abs(kv[1]))[-3:]
            out.append(Finding("L09", "歌词", "歌词与音频对齐", sev, ok,
                               "锚点 %d 个：MAE %.3fs，中位 %+.3fs，|偏差|<0.6s 占 %.0f%%"
                               % (len(devs), mae, float(np.median(arr)), ratio * 100),
                               {"最差行": [[i + 1, round(d, 2)] for i, d in worst],
                                "MAE_PASS": TH["anchor_mae_pass"], "MAE_WARN": TH["anchor_mae_warn"]},
                               "整体偏移 → DTW 错锚/斜率跑飞，改走 CTC；单行偏差 → 检查该行时间戳"))
    except Exception as e:                      # pragma: no cover
        out.append(Finding("L09", "歌词", "歌词与音频对齐", WARN, False,
                           "判分异常：%s" % str(e)[:120], {}, "检查 vocal_activity/verify_sync 依赖"))
    return out


# ── 汇总 ────────────────────────────────────────────────────────────────
def run_qc(mp4, *, entry=None, lrc_text=None, ref_lrc_text=None, ffmpeg=None,
           windows=12, win=10, seed=20260930, src_audio=None, src_ss=0.0, deep=False,
           want_w=None, want_h=None, tmp_dir=None):
    """对单个成片执行全部自检。返回结构化 report（dict）。"""
    ffmpeg = ffmpeg or CFG.ffmpeg()
    t0 = time.time()
    if not os.path.exists(mp4):
        raise FileNotFoundError(mp4)

    entry = entry or {}
    info = ffprobe_json(mp4)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    W = want_w or int(v.get("width") or 0)
    H = want_h or int(v.get("height") or 0)

    findings = []
    findings.append(_f("A01", "音频", "音轨存在",
                       "%s %sHz %sch" % (a.get("codec_name"), a.get("sample_rate"),
                                         a.get("channels")),
                       bool(a) and a.get("codec_name") == "aac", BLOCK, {},
                       "混流未 -map 音轨或音轨编码失败，成片会无声"))

    spec_f, ctx, has_audio = check_container(info, W, H)
    findings += spec_f

    # 歌词来源：显式 > manifest 路径 > 成片同名 .lrc
    if lrc_text is None:
        for key in ("lrc_path", "lrc"):
            p = entry.get(key)
            if p and os.path.exists(p):
                lrc_text = io.open(p, encoding="utf-8").read()
                break
        if lrc_text is None:
            cand = os.path.splitext(mp4)[0] + ".lrc"
            if os.path.exists(cand):
                lrc_text = io.open(cand, encoding="utf-8").read()
    if ref_lrc_text is None:
        for key in ("lrc_src_path", "ref_lrc_path"):
            p = entry.get(key)
            if p and os.path.exists(p):
                ref_lrc_text = io.open(p, encoding="utf-8").read()
                break
        if ref_lrc_text is None:
            cand = os.path.splitext(mp4)[0] + ".src.lrc"
            if os.path.exists(cand):
                ref_lrc_text = io.open(cand, encoding="utf-8").read()

    rows = parse_lrc(lrc_text or "")
    ref_rows = parse_lrc(ref_lrc_text) if ref_lrc_text else None

    # 期望时长
    want_dur = None
    if entry.get("cut_end") and entry.get("cut_start"):
        want_dur = float(entry["cut_end"]) - float(entry["cut_start"]) + float(entry.get("head_pad", 0) or 0)
    head_pad = float(entry.get("head_pad", 0) or 0)

    x = None
    if has_audio:
        try:
            x, sr = decode_audio(ffmpeg, mp4)
            findings += check_audio(x, sr, ctx, want_dur, head_pad)
            if src_audio and os.path.exists(src_audio):
                findings.append(check_av_xcorr(ffmpeg, mp4, src_audio, src_ss, x, sr, ctx))
        except Exception as e:
            findings.append(Finding("A02", "音频", "音频可解码", BLOCK, False,
                                    "解码失败：%s" % str(e)[:160], {}, "成片音轨损坏，重新混流"))

    vf, ngrab = check_video(ffmpeg, mp4, int(ctx["nframes"] or 0), windows, win, seed, deep)
    findings += vf
    findings += check_lyrics(rows, lrc_text, ref_rows, ctx["duration"],
                             x, sr if x is not None else 22050)

    blockers = [f for f in findings if f["sev"] == BLOCK]
    warns = [f for f in findings if f["sev"] == WARN]
    return {
        "target": mp4,
        "title": entry.get("title") or os.path.basename(mp4),
        "passed": not blockers,
        "elapsed_s": round(time.time() - t0, 1),
        "cost": {"decoded_frames": ngrab, "windows": windows, "win": win, "seed": seed,
                 "re_render": False},
        "ctx": ctx,
        "findings": findings,
        "blockers": blockers,
        "warnings": warns,
        "n_pass": sum(1 for f in findings if f["sev"] == PASS),
    }


def render_report(rep, verbose=True):
    L = []
    L.append("=" * 78)
    L.append("QC %s  %s" % ("通过 ✓" if rep["passed"] else "失败 ✗", rep["title"]))
    L.append("  成片: %s" % rep["target"])
    L.append("  耗时 %.1fs | 解码 %d 帧（%d 窗口×%d，seed=%s）| 未重新渲染"
             % (rep["elapsed_s"], rep["cost"]["decoded_frames"], rep["cost"]["windows"],
                rep["cost"]["win"], rep["cost"]["seed"]))
    L.append("=" * 78)
    grp = {}
    for f in rep["findings"]:
        grp.setdefault(f["group"], []).append(f)
    for g in ("规格", "音频", "画面", "歌词"):
        if g not in grp:
            continue
        L.append("[%s]" % g)
        for f in grp[g]:
            mark = {"PASS": "✓", "WARN": "⚠", "BLOCK": "✗"}[f["sev"]]
            L.append("  %s %-4s %-14s %s" % (mark, f["id"], f["name"], f["detail"]))
            if f["sev"] != PASS:
                if f["where"]:
                    L.append("        证据: %s" % json.dumps(f["where"], ensure_ascii=False)[:300])
                if f["fix"]:
                    L.append("        修复: %s" % f["fix"])
    L.append("-" * 78)
    L.append("汇总: PASS %d / WARN %d / BLOCK %d → %s"
             % (rep["n_pass"], len(rep["warnings"]), len(rep["blockers"]),
                "通过" if rep["passed"] else "不通过"))
    if rep["blockers"]:
        L.append("失败项: " + ", ".join("%s %s" % (f["id"], f["name"]) for f in rep["blockers"]))
    return "\n".join(L)


# ── CLI ─────────────────────────────────────────────────────────────────
def _pick_entry(manifest, mp4):
    for e in manifest.get("songs", []):
        if os.path.abspath(e.get("mp4", "")) == os.path.abspath(mp4):
            return e
    return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description="成片自检（判定化 / 轻量 / 可复用）")
    ap.add_argument("--mp4", help="单个成片路径")
    ap.add_argument("--date", help="批量：cuts/<date> 下 manifest.json 内全部成片")
    ap.add_argument("--workdir", default=CFG.get("workdir") or CFG.ROOT)
    ap.add_argument("--manifest", help="manifest.json 路径（提供则取 cut 点/歌词路径）")
    ap.add_argument("--lrc", help="成片所用歌词（默认取 <mp4>.lrc）")
    ap.add_argument("--ref-lrc", help="源歌词参照（默认取 <mp4>.src.lrc）")
    ap.add_argument("--src-audio", help="源录播（启用内容级音画同步判定）")
    ap.add_argument("--src-ss", type=float, default=0.0, help="源录播中该片段起点秒")
    ap.add_argument("--windows", type=int, default=12, help="抽帧窗口数（含首尾）")
    ap.add_argument("--win", type=int, default=10, help="每窗口帧数")
    ap.add_argument("--seed", type=int, default=20260930, help="随机抽样种子（固定=可复现）")
    ap.add_argument("--deep", action="store_true",
                    help="启用深度项（V06 全片帧节奏扫描，约 +60s/160s 素材）")
    ap.add_argument("--json", dest="json_out", help="报告 JSON 输出路径")
    ap.add_argument("--quiet", action="store_true", help="只打印汇总行")
    args = ap.parse_args(argv)

    if not args.mp4 and not args.date:
        ap.error("需指定 --mp4 或 --date")

    targets = []
    manifest = {}
    if args.date:
        mdir = os.path.join(args.workdir, "cuts", args.date)
        mf = args.manifest or os.path.join(mdir, "manifest.json")
        if not os.path.exists(mf):
            print("找不到 manifest: %s" % mf)
            return 2
        manifest = json.load(io.open(mf, encoding="utf-8"))
        for e in manifest.get("songs", []):
            if e.get("mp4") and os.path.exists(e["mp4"]):
                targets.append((e["mp4"], e))
    else:
        entry = {}
        if args.manifest and os.path.exists(args.manifest):
            manifest = json.load(io.open(args.manifest, encoding="utf-8"))
            entry = _pick_entry(manifest, args.mp4)
        targets.append((args.mp4, entry))

    lrc_text = io.open(args.lrc, encoding="utf-8").read() if args.lrc else None
    ref_text = io.open(args.ref_lrc, encoding="utf-8").read() if args.ref_lrc else None

    reps, bad = [], 0
    for mp4, entry in targets:
        try:
            rep = run_qc(mp4, entry=entry, lrc_text=lrc_text, ref_lrc_text=ref_text,
                         windows=args.windows, win=args.win, seed=args.seed, deep=args.deep,
                         src_audio=args.src_audio, src_ss=args.src_ss)
        except Exception as e:
            print("[qc] %s 运行失败: %s" % (mp4, e))
            bad += 1
            continue
        reps.append(rep)
        if not rep["passed"]:
            bad += 1
        print(render_report(rep) if not args.quiet else
              "%s %s  BLOCK=%d WARN=%d" % ("✓" if rep["passed"] else "✗", rep["title"],
                                           len(rep["blockers"]), len(rep["warnings"])))
        print()

    if args.json_out:
        io.open(args.json_out, "w", encoding="utf-8", newline="").write(
            json.dumps(reps, ensure_ascii=False, indent=1, default=str))
        print("→ %s" % args.json_out)
    if len(reps) > 1:
        print("批量汇总: %d 个成片，不通过 %d 个" % (len(reps), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
