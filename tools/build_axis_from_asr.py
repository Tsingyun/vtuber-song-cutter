# -*- coding: utf-8 -*-
"""用字级 ASR 重建歌词轴（cut 相对时间），不依赖官方 LRC 的时间戳。

背景（RULES #13 / #25）：
· 官方 LRC 的**时间戳**在实况里未必成立（弹的伴奏可能是 Remix / 变速 / 剪短版），
  但**歌词文本与顺序**是可靠的。
· CTC 全曲对齐在日语上匹配率低（同音字多）；而「字级 ASR → 文本序列对齐」不依赖
  音素模型，天然吸收速度漂移与漏唱。

做法：
① 在 [cut_start, cut_end] 内按 ≤12s 窗口跑 SenseVoice 字级 ASR（>20s 时间戳会塌陷）；
② 把 ASR 字序列与「LRC 各行文本拼接」做 difflib 序列对齐；
③ 每行的起点 = 该行第一个字在 ASR 里对应的时刻（cut 相对）；
④ 未被匹配的行显式报警，不静默吞掉。

用法：
  python tools/build_axis_from_asr.py <video> <cut_start> <cut_end> <lrc_in> <lrc_out> [--trim-credits]
"""
import io
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
from songcut import ctc_align as CA

FF = (os.environ.get("SONGCUT_FFMPEG")
      or os.environ.get("FFMPEG")
      or r"D:/ffmpeg/bin/ffmpeg.exe")
WIN = 12.0
HOP = 11.0        # 1s 重叠，避免边界切字


def parse_lrc(path):
    rows = []
    for line in io.open(path, encoding="utf-8").read().splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", line.strip())
        if not m:
            continue
        t = int(m.group(1)) * 60 + float(m.group(2))
        txt = m.group(3).strip()
        if not txt:
            continue
        rows.append([t, txt])
    return rows


def norm(s):
    """只留歌词主体：去罗马字注音括号、去标点空白、去制作名单行。"""
    s = re.sub(r"[（(][^）)]*[）)]", "", s)          # 去掉 丸(まる) 这类注音
    s = re.sub(r"^\s*(作词|作曲|编曲|歌|演唱|原唱|翻唱)[:：].*$", "", s)
    s = re.sub(r"[^\u3040-\u30ff\u4e00-\u9fffA-Za-z0-9]", "", s)
    return s


def asr_words(video, t0, t1, tmp):
    wav_all = os.path.join(tmp, "axis_all.wav")
    if not os.path.exists(wav_all):
        subprocess.run([FF, "-v", "error", "-y", "-ss", "%.3f" % t0, "-t", "%.3f" % (t1 - t0),
                        "-i", video, "-map", "0:a:0", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", wav_all], check=True)
    total = t1 - t0
    words = []
    t = 0.0
    while t < total - 0.4:
        d = min(WIN, total - t)
        w = os.path.join(tmp, "axis_%05d.wav" % int(t * 10))
        subprocess.run([FF, "-v", "error", "-y", "-ss", "%.3f" % t, "-t", "%.3f" % d,
                        "-i", wav_all, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", w], check=True)
        try:
            ws = CA._asr_words([(0.0, d)], w, d, tmp)
        finally:
            try:
                os.remove(w)
            except OSError:
                pass
        for c, s, e in ws:
            a = t + s
            if a < t - 0.05:      # 越过窗口边界
                continue
            words.append((c, a, t + e))
        t += HOP
    # 去重（重叠区重复识别）：同一字、时间接近则保留靠前
    out = []
    for c, s, e in words:
        if out and out[-1][0] == c and abs(out[-1][1] - s) < 0.6:
            continue
        out.append((c, s, e))
    return out, total


def main():
    video, cs, ce, lrc_in, lrc_out = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4], sys.argv[5]
    trim = "--trim-credits" in sys.argv
    tmp = r"D:/岁己歌切/_tmp/axisbuild"
    os.makedirs(tmp, exist_ok=True)

    rows = parse_lrc(lrc_in)
    if trim:
        rows = [r for r in rows if norm(r[1])]
    texts = [norm(r[1]) for r in rows]
    if sum(len(x) for x in texts) < 10:
        print("!! LRC 归一化后几乎为空，检查输入")
        return 1

    print("窗口 %.0fs / 步长 %.0fs，歌曲区间 %.2f~%.2f（%.1fs）" % (WIN, HOP, cs, ce, ce - cs))
    words, total = asr_words(video, cs, ce, tmp)
    print("ASR 字级：%d 个" % len(words))
    asr_chars = "".join(w[0] for w in words)
    lrc_chars = "".join(texts)
    print("ASR 文本 %d 字 | LRC 文本 %d 字" % (len(asr_chars), len(lrc_chars)))

    import difflib
    sm = difflib.SequenceMatcher(None, lrc_chars, asr_chars, autojunk=False)
    l2a = {}
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            l2a[a + k] = b + k
    ratio = 2.0 * sum(b.size for b in sm.get_matching_blocks()) / max(1, len(lrc_chars) + len(asr_chars))
    print("总体匹配率 %.3f" % ratio)

    # 每行起点
    pos = 0
    out_lines, report = [], []
    prev_t = 0.0
    for i, (t0, raw) in enumerate(rows):
        n = len(texts[i])
        if n == 0:
            out_lines.append(None)
            pos += n
            continue
        idx = [l2a.get(pos + k) for k in range(n)]
        hits = [x for x in idx if x is not None]
        if hits:
            t_live = words[hits[0]][1]
            cov = len(hits) / float(n)
        else:
            t_live, cov = None, 0.0
        if t_live is None or cov < 0.34:
            report.append((i + 1, t0, t_live, cov, raw, "FAIL"))
            out_lines.append(None)
        else:
            if t_live < prev_t - 0.2:
                report.append((i + 1, t0, t_live, cov, raw, "NONMONO"))
            prev_t = max(prev_t, t_live)
            out_lines.append((t_live, raw))
        pos += n

    # 填充失败行：取前后成功行的线性插值
    ok = [i for i, x in enumerate(out_lines) if x is not None]
    for i, x in enumerate(out_lines):
        if x is not None:
            continue
        prev = max([j for j in ok if j < i], default=None)
        nxt = min([j for j in ok if j > i], default=None)
        if prev is None and nxt is None:
            continue
        if prev is None:
            out_lines[i] = (out_lines[nxt][0], rows[i][1])
        elif nxt is None:
            out_lines[i] = (out_lines[prev][0] + 4.0, rows[i][1])
        else:
            a, b = out_lines[prev][0], out_lines[nxt][0]
            out_lines[i] = (a + (b - a) * (i - prev) / float(nxt - prev), rows[i][1])

    lines = []
    for x in out_lines:
        if x is None:
            continue
        t = max(0.0, x[0])
        lines.append("[%02d:%05.2f]%s" % (int(t // 60), t - int(t // 60) * 60, x[1]))
    io.open(lrc_out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n写入 %s：%d 行" % (lrc_out, len(lines)))
    for l in lines:
        print("  ", l)
    if report:
        print("\n⚠ 需人工复核的行：")
        for r in report:
            print("  L%-3d 官方%7.2f  live=%s 覆盖%.2f  %s  [%s]"
                  % (r[0], r[1], ("%.2f" % r[2]) if r[2] is not None else "-", r[3], r[4][:28], r[5]))
    else:
        print("\n全部行均匹配成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
