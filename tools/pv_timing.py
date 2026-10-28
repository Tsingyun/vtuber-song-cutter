# -*- coding: utf-8 -*-
"""LRC 行级时间 → 逐词 / 逐字时间轴（术力口文字 PV 的「逐字出现」靠它）。

为什么需要：
  LRC 只给每行起点，而文字 PV 的表现力几乎全在「每个词落在哪个音节上」。
  直接按字数均分在长句上会明显脱拍（实测 4s 的行能差出 0.5s 以上）。

做法（三步，全部可复核）：
  ① 人声频段「对数谱通量」求起音强度（250~4000Hz，100fps）——不取全带，
     否则底鼓和镲会把每小节切成四刀；
  ② 标定行起点：在 LRC 时间 ±窗口内找最強起音，统计全局偏移量（LRC 轴常有系统性偏差），
     偏移量取中位数后整体校正——而不是逐行乱吸（逐行乱吸会让行间距抖动）；
  ③ 行内按**音节权重**分配，再把每个边界向 ±0.18s 内的起音峰吸附，
     并强制单调 + 最小驻留，最后用「有声帧」裁掉行尾拖长。

输出带诊断：吸附位移分布、行起点偏移量、被吸附的边界比例 —— 数字不好看就说明
这条轴不可信，要回人工校正，而不是拿它出片。
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys

import numpy as np

SR = 24000
HOP = 240                      # 100 fps
V_LO, V_HI = 250.0, 4000.0     # 人声频段
TS = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")


# ---------------------------------------------------------------- 歌词解析
def parse_lrc(text):
    """→ [{t0, text}]。时间戳按 mm*60+ss 计算，允许 ss>=60（本场就有一条 [00:60.00]）。"""
    out = []
    for raw in text.replace("\r", "").split("\n"):
        s = raw.strip().lstrip("\ufeff")
        if not s:
            continue
        m = TS.match(s)
        if not m:
            continue
        t = int(m.group(1)) * 60.0 + float(m.group(2))
        body = m.group(3).strip()
        if not body:
            continue
        out.append({"t0": round(t, 3), "text": body})
    out.sort(key=lambda x: x["t0"])
    return out


SYL_RE = re.compile(r"[aeiouy]+")


def syllables(word):
    """英文音节粗估（元音组数，扣掉不发音的词尾 e）。"""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    n = len(SYL_RE.findall(w))
    if n > 1 and w.endswith("e") and not w.endswith(("le", "ee", "ye")):
        n -= 1
    return max(1, n)


CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def split_units(text):
    """切分单元：拉丁按空格成词，CJK 按字；标点并入前一单元，不单独占时间。"""
    units = []
    if len(CJK_RE.findall(text)) * 2 >= len(text.replace(" ", "")):
        for ch in text:
            if ch.isspace():
                continue
            if units and not CJK_RE.match(ch) and not ch.isalnum():
                units[-1]["text"] += ch       # 标点粘到前一个字
            else:
                units.append({"text": ch, "w": 1.0})
        return units
    # 拉丁：按空白切；单独成词的标点并到前一个词
    for tok in text.split():
        if units and not re.search(r"[0-9A-Za-z]", tok):
            units[-1]["text"] += " " + tok
        else:
            units.append({"text": tok, "w": float(max(1, syllables(tok)))})
    return units


# ---------------------------------------------------------------- 音频信号
def decode(audio, ffmpeg):
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", audio,
                          "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True).stdout
    if not raw:
        raise RuntimeError("ffmpeg 解码失败：" + audio)
    return np.frombuffer(raw, dtype="<f4").astype(np.float32)


def onset_env(y):
    """人声频段对数谱通量，归一化到 0..1，长度 = ceil(len/HOP)。

    用 log1p 而不是裸能量差：能量差会被「整段变响」误判成起音，
    对数域差只对「相对上升」敏感，是起音检测的常规做法。
    """
    import librosa
    n = 1024
    S = np.abs(librosa.stft(y, n_fft=n, hop_length=HOP, window="hann")) ** 2
    freqs = librosa.fft_frequencies(sr=SR, n_fft=n)
    m = (freqs >= V_LO) & (freqs <= V_HI)
    L = np.log1p(S[m] * 1e5)
    d = np.diff(L, axis=1)
    flux = np.maximum(0.0, d).sum(axis=0)
    flux = np.concatenate([[0.0], flux])
    # 轻微平滑（3 帧）+ 归一化
    k = np.array([0.25, 0.5, 0.25])
    flux = np.convolve(flux, k, mode="same")
    p95 = float(np.percentile(flux, 95)) or 1.0
    return np.clip(flux / p95, 0.0, 1.0)


def voiced_mask(ana, nfr, fps=60):
    """pro_analysis 的 voiced（0..1）→ 60fps 布尔掩码，重采样到 100fps 供查询。"""
    if not ana or "voiced_b64" not in ana:
        return None
    v = np.frombuffer(base64.b64decode(ana["voiced_b64"]), dtype=np.uint8).astype(np.float32) / 255.0
    # 60fps → 100fps
    idx = np.clip((np.arange(nfr) / 100.0 * fps).astype(int), 0, len(v) - 1)
    return v[idx] > 0.45


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lrc", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--analysis", default="", help="tools/analyze_music.py 的产物（取 voiced 掩码）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ffmpeg", default="D:/ffmpeg/bin/ffmpeg.exe")
    ap.add_argument("--lead", type=float, default=0.0, help="整体提前量（秒），正数=歌词更早出现")
    ap.add_argument("--snap", type=float, default=0.18, help="边界向起音吸附的最大位移")
    a = ap.parse_args()

    lines = parse_lrc(open(a.lrc, encoding="utf-8-sig").read())
    if len(lines) < 3:
        raise SystemExit("歌词行数过少（%d），检查 --lrc" % len(lines))
    y = decode(a.audio, a.ffmpeg)
    dur = len(y) / SR
    env = onset_env(y)
    fps_env = 100.0
    ana = None
    if a.analysis and os.path.exists(a.analysis):
        ana = json.load(open(a.analysis, encoding="utf-8"))
    vm = voiced_mask(ana, len(env), 60) if ana else None

    # ---------- ② 行起点标定
    deltas = []
    for i, ln in enumerate(lines):
        t0 = ln["t0"]
        nxt = lines[i + 1]["t0"] if i + 1 < len(lines) else dur
        lo = max(0, int((t0 - 0.30) * fps_env))
        hi = min(len(env), int((min(t0 + 0.70, nxt) * fps_env)))
        if hi - lo < 5:
            continue
        k = lo + int(np.argmax(env[lo:hi]))
        deltas.append(k / fps_env - t0)
    d = np.array(deltas) if deltas else np.zeros(1)
    med = float(np.median(d)) if len(d) else 0.0
    iqr = float(np.percentile(d, 75) - np.percentile(d, 25)) if len(d) else 0.0
    shift = med if (abs(med) > 0.06 and iqr < 0.45) else 0.0
    print("[calib] 行起点相对 LRC 偏移 中位 %+.3fs  IQR %.3fs  ⇒ 全局校正 %+.3fs%s"
          % (med, iqr, shift, "" if shift else "（分散过大，不校正）"))

    # ---------- ③ 逐行分配
    out_lines = []
    snap_hits = 0
    snaps = []
    for i, ln in enumerate(lines):
        t0 = ln["t0"] + shift - a.lead
        nxt = (lines[i + 1]["t0"] + shift - a.lead) if i + 1 < len(lines) else dur
        units = split_units(ln["text"])
        if not units:
            continue
        # 行尾：优先用「有声帧」找到真实收声，避免把词摊到间奏里
        lim = min(nxt - 0.06, t0 + 12.0, dur)
        end = lim
        if vm is not None:
            a0 = int(max(0, t0 * fps_env))
            b0 = int(min(len(vm), lim * fps_env))
            if b0 > a0 + 5:
                seg = vm[a0:b0]
                last = -1
                gap = 0
                for j in range(len(seg)):
                    if seg[j]:
                        last = j
                        gap = 0
                    else:
                        gap += 1
                        if gap > 45 and last > 0:      # 连续 0.45s 无声 ⇒ 收声
                            break
                if last > 0:
                    cand = a0 + last
                    end = min(lim, cand / fps_env + 0.12)
        if end < t0 + 0.35:
            end = min(lim, t0 + 0.35 + 0.28 * len(units))
        span = max(0.35, end - t0)

        W = np.array([u["w"] for u in units], dtype=np.float64)
        W = W / W.sum()
        # 比例分配（首个单元落在行首，不做 lead 内缩）
        edges = np.concatenate([[0.0], np.cumsum(W)]) * span
        # 吸附：只动内部边界，保留首尾
        for j in range(1, len(edges) - 1):
            t = t0 + edges[j]
            lo = int(max(0, (t - a.snap) * fps_env))
            hi = int(min(len(env), (t + a.snap) * fps_env))
            if hi - lo < 3:
                continue
            k = lo + int(np.argmax(env[lo:hi]))
            pk = env[k]
            if pk >= 0.16:                       # 太弱的峰不吸，否则被噪声拖走
                nt = k / fps_env
                snaps.append(nt - t)
                snap_hits += 1
                edges[j] = nt - t0
        # 强制单调 + 最小驻留 0.09s
        for j in range(1, len(edges)):
            edges[j] = max(edges[j], edges[j - 1] + 0.09)
        edges = np.minimum(edges, span)
        edges[-1] = span

        words = []
        for j, u in enumerate(units):
            words.append({
                "text": u["text"],
                "t0": round(t0 + float(edges[j]), 3),
                "t1": round(t0 + float(edges[j + 1]), 3),
            })
        # 逐字：词内按字符数均分（给「逐字弹出」用；英文单词再切会散架，所以只切到词）
        chars = []
        for w in words:
            chs = list(w["text"])
            if len(chs) <= 1:
                chars.append({"c": w["text"], "t0": w["t0"], "t1": w["t1"]})
                continue
            step = (w["t1"] - w["t0"]) / len(chs)
            for ci, c in enumerate(chs):
                chars.append({"c": c, "t0": round(w["t0"] + ci * step, 3),
                              "t1": round(w["t0"] + (ci + 1) * step, 3)})
        out_lines.append({
            "i": len(out_lines), "t0": round(t0, 3), "t1": round(end, 3),
            "text": ln["text"], "words": words, "chars": chars,
        })

    # ---------- 诊断
    snaps = np.array(snaps) if snaps else np.zeros(1)
    # 分母 = 可吸附的内部边界数 = 每行 (词数 - 1)
    total_b = max(1, sum(max(0, len(l["words"]) - 1) for l in out_lines))
    print("[snap] 被吸附边界 %d/%d (%.0f%%)  位移 |Δ| 中位 %.3fs  p90 %.3fs"
          % (snap_hits, total_b, 100.0 * snap_hits / total_b,
             float(np.median(np.abs(snaps))), float(np.percentile(np.abs(snaps), 90))))
    bad = [l for l in out_lines if l["t1"] - l["t0"] > 8.5]
    if bad:
        print("[warn] %d 行跨度 >8.5s（可能吞了间奏）：%s"
              % (len(bad), ", ".join("#%d %.1fs" % (l["i"], l["t1"] - l["t0"]) for l in bad[:6])))

    doc = {
        "version": 1, "sr": SR, "hop": HOP, "dur": round(dur, 3),
        "lrc_offset_applied": round(shift, 4),
        "calib": {"median": round(med, 4), "iqr": round(iqr, 4), "n": len(d)},
        "snap": {"hits": snap_hits, "total": total_b,
                 "abs_median": round(float(np.median(np.abs(snaps))), 4),
                 "abs_p90": round(float(np.percentile(np.abs(snaps), 90)), 4)},
        "lines": out_lines,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(doc, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("[out] %s  %d 行 %d 词  %.0fKB" %
          (a.out, len(out_lines), sum(len(l["words"]) for l in out_lines),
           os.path.getsize(a.out) / 1024.0))


if __name__ == "__main__":
    sys.exit(main())
