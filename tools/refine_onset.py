# -*- coding: utf-8 -*-
"""逐行精修起唱时刻：短窗（0.7s）扫描，定位「行首字」第一次被听见的位置。

为什么不用现成的两个量法
--------------------------------------------------------------------------------
· slide_verify：dev = (首个命中窗起点 + win/2) − 行起点。它隐含假设「窗中心 = 行首」。
  但一行 4~5s、十几个字，1.5s 的窗只要 score≥0.55 就命中，那个窗必然覆盖的是**行的
  中后段**（窗从行首开始只能覆盖 4~5 字，达不到 0.55）。于是所有长行都被判「偏晚
  0.5~2s」——这是度量本身的偏差，不是轴的问题。拿它当中位偏移去改轴，会把对的东西
  改错，而且越长的行改得越多。
· 全局 LCS / 逐字戳：轻唱、嘶吼时覆盖率只有 18~21%，匹不到就插值，等于猜。

这里改成：窗 0.7s（窗内最多 2~3 个字）、步长 0.1s，只在「行首 ±3s」内扫。窗短到
装不下行的中段，所以「第一次听见行首字」的窗起点就是起唱时刻（±0.15s），没有
窗中心假设。判据只看行首 3 个字命中了几个，不做整行匹配。

用法
    python tools/refine_onset.py --media X.mp4 --lrc 轴.lrc [--pad 1.0] [--write 新轴.lrc]
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")


def cjk(s):
    return "".join(CJK.findall(s or ""))


def parse_lrc(text):
    rows = []
    for line in (text or "").splitlines():
        m = re.match(r"\[(\d{1,3}):(\d{2}(?:\.\d+)?)\](.*)", line)
        if not m:
            continue
        body = re.sub(r"<[^>]*>", "", m.group(3)).strip()
        if len(cjk(body)) < 2:
            continue
        rows.append([int(m.group(1)) * 60 + float(m.group(2)), body])
    return rows


def ffmpeg():
    from songcut import config as CFG
    p = getattr(CFG, "FFMPEG", "ffmpeg")
    if os.path.exists(p):
        return p
    for c in ("D:/ffmpeg/bin/ffmpeg.exe", "ffmpeg"):
        if os.path.exists(c) or c == "ffmpeg":
            return c
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--media", required=True)
    ap.add_argument("--lrc", required=True)
    ap.add_argument("--pad", type=float, default=0.0, help="lrc 口径不含补静音时填成片补静音秒数")
    ap.add_argument("--span", type=float, default=3.0, help="在行时间前后各搜多少秒")
    ap.add_argument("--win", type=float, default=0.7)
    ap.add_argument("--hop", type=float, default=0.1)
    ap.add_argument("--write", default="")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    from songcut import ctc_align as CTC
    rows = parse_lrc(open(a.lrc, encoding="utf-8").read())
    if not rows:
        raise SystemExit("lrc 无可用行")
    ff = ffmpeg()
    st = os.stat(a.media)
    tag = re.sub(r"[^\w.-]+", "_", os.path.basename(a.media))[:40]
    d = os.path.join(ROOT, "_tmp_refine", "%s_%d_%d" % (tag, st.st_size, int(st.st_mtime)))
    os.makedirs(d, exist_ok=True)

    # 先把每行需要的窗切出来（去重），再一次性批量识别
    wins, need = [], {}
    for li, (t_row, _body) in enumerate(rows):
        for k in range(int(-a.span / a.hop), int(a.span / a.hop) + 1):
            t = max(0.0, t_row + k * a.hop + a.pad)
            key = int(round(t / a.hop))
            if key not in need:
                need[key] = len(wins)
                w = os.path.join(d, "w%06d.mp3" % key)
                if not os.path.exists(w):
                    subprocess.call([ff, "-y", "-v", "error", "-ss", "%.2f" % t,
                                     "-t", "%.2f" % a.win, "-i", a.media,
                                     "-ar", "16000", "-ac", "1", w],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                wins.append((t, w))
    wins.sort(key=lambda x: x[0])
    idx = {int(round(t / a.hop)): i for i, (t, _w) in enumerate(wins)}

    cache = os.path.join(d, "texts.json")
    if os.path.exists(cache):
        texts = json.load(open(cache, encoding="utf-8"))
        if len(texts) != len(wins):
            texts = None
    else:
        texts = None
    if texts is None:
        model = CTC._get_model()
        texts = []
        for k in range(0, len(wins), 8):
            batch = wins[k:k + 8]
            try:
                res = model.generate(input=[b[1] for b in batch], language="zh",
                                     use_itn=False, batch_size_s=120)
            except Exception:
                res = []
            for j in range(len(batch)):
                s = re.sub(r"<\|[^|]*\|>", "", res[j].get("text") or "").strip() if j < len(res) else ""
                texts.append(s)
        json.dump(texts, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    cj = [cjk(s) for s in texts]

    print("媒体 %.1fs / %d 行 / %d 窗（win %.1f hop %.1f）"
          % (os.path.getsize(a.media) and 0 or 0, len(rows), len(wins), a.win, a.hop))
    print("\n 行  轴时间   实测起唱  偏差   行首命中   文本")
    fixed, devs = [], []
    for li, (t_row, body) in enumerate(rows):
        head = cjk(body)[:3]
        hits = []
        for k in range(int(-a.span / a.hop), int(a.span / a.hop) + 1):
            t = max(0.0, t_row + k * a.hop + a.pad)
            i = idx.get(int(round(t / a.hop)))
            if i is None:
                continue
            s = cj[i]
            # 行首 3 字里至少命中 2 个（允许 ASR 错一个字）
            n = sum(1 for ch in head if ch in s)
            if n >= min(2, len(head)):
                hits.append((t, n, s))
        if hits:
            hits.sort()
            t_first, n_first, s_first = hits[0]
            # 同一位置连续命中的第一个窗起点就是起唱（窗短，不存在中段误吸）
            dev = t_first - (t_row + a.pad)
            devs.append(dev)
            fixed.append(round(t_first - a.pad, 2))
            print("%3d %7.2f %9.2f %+6.2f   %d/%d      %s"
                  % (li, t_row + a.pad, t_first, dev, n_first, len(head), body[:16]))
        else:
            fixed.append(round(t_row, 2))
            print("%3d %7.2f %9s %6s   -        %s  ⚠未检出"
                  % (li, t_row + a.pad, "--", "--", body[:16]))
    if devs:
        srt = sorted(devs)
        med = srt[len(srt) // 2]
        print("\n检出 %d/%d 行：中位偏差 %+.2fs，P10 %+.2f，P90 %+.2f"
              % (len(devs), len(rows), med, srt[int(len(srt) * .1)], srt[int(len(srt) * .9) - 1 if len(srt) > 1 else 0]))
    if a.report:
        json.dump([[i, r[0], fixed[i], r[1]] for i, r in enumerate(rows)],
                  open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("→ %s" % a.report)
    if a.write:
        out = []
        for (t, body) in zip(fixed, [r[1] for r in rows]):
            t = max(0.02, t)
            out.append("[%02d:%05.2f]%s" % (int(t // 60), t - int(t // 60) * 60, body))
        open(a.write, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
        print("→ %s" % a.write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
