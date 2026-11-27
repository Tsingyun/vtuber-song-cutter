# -*- coding: utf-8 -*-
"""成片歌词轴体检：把「成片音频」和「成片实际渲染用的 lrc」逐行比，量真实偏差。

为什么必须单独有这么一个工具
--------------------------------------------------------------------------------
仓库里已有的验收全是「人工轴 vs 同一段音频」的自证：
  · L02 比的是 src_lrc 与 lrc 的行数/文本（同源，比不出时间）
  · L09 用的是 verify_sync，锚点来自同一份 lrc，只能查「局部对不上」
  · slide_verify 用当前 lrc 做查询串，轴整体平移时命中率不一定掉
一旦轴是**整体常数偏移**或**缓慢漂移**，上面三个都会绿 —— 用户看到的就是
「前奏没了 / 第一秒多出人声 / 后半首全部晚半秒」。这个工具不看 lrc 的内部
一致性，只用「窗口 ASR 文本 ↔ 歌词字符序列」的全局 LCS 反推每行真实起唱时刻，
再把 (实测 − 轴) 对时间做回归，直接输出：

    · 常数偏移（整体早/晚多少）
    · 漂移斜率（每秒差多少 → 半首差多少）
    · 逐行偏差表（哪一段开始崩）

判定（默认阈值，可用参数覆盖）
    |中位偏差| > 0.50s            → 整体错位      rc=2
    |斜率| > 0.02 s/s（半首 >2s） → 缩放漂移      rc=3
    单行残差 > 1.5s 且 ≥3 行      → 局部段错位    rc=4
    rc=0 = 通过

用法
    python tools/lrc_drift_check.py --media "成片.mp4" --lrc "成片.lrc"
    python tools/lrc_drift_check.py --media X.mp3 --lrc segments/ov_X.lrc --pad 1.0
      --pad: lrc 是「不含片头补静音」口径时，指定成片里补了多少秒，工具会先减掉
             再比较（只影响报告口径，不影响判定方向）
"""
import argparse
import array
import json
import math
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _cjk(s):
    return [c for c in (s or "") if "\u4e00" <= c <= "\u9fff" or "\u3040" <= c <= "\u30ff"]


def parse_lrc(text):
    """只取行级时间 + 正文；剥掉所有 <..> 内嵌标签。"""
    rows = []
    for line in (text or "").splitlines():
        ms = re.findall(r"\[(\d{1,3}):(\d{2}(?:\.\d+)?)\]", line)
        if not ms:
            continue
        body = re.sub(r"\[\d{1,3}:\d{2}(?:\.\d+)?\]", "", line)
        body = re.sub(r"<\d{1,3}:\d{2}(?:\.\d+)?>", "", body).strip()
        if not body:
            continue
        if not _cjk(body):
            continue
        t = int(ms[0][0]) * 60 + float(ms[0][1])
        rows.append((t, body))
    rows.sort(key=lambda x: x[0])
    return rows


def duration_of(media):
    out = subprocess.run(
        [os.path.join(os.path.dirname(_ffmpeg()), "ffprobe.exe") if os.name == "nt" else "ffprobe",
         "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", media],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise SystemExit("读不出时长：%s" % media)


def _ffmpeg():
    from songcut import config as CFG
    p = getattr(CFG, "FFMPEG", None) or "ffmpeg"
    if os.path.exists(p):
        return p
    for c in ("D:/ffmpeg/bin/ffmpeg.exe", "/usr/bin/ffmpeg"):
        if os.path.exists(c):
            return c
    return p


def slice_windows(media, hop, win, total):
    ff = _ffmpeg()
    st = os.stat(media)
    tag = re.sub(r"[^\w.-]+", "_", os.path.basename(media))[:40]
    d = os.path.join(ROOT, "_tmp_drift", "%s_%d_%d_%d" % (tag, st.st_size, int(st.st_mtime), int(hop * 100)))
    os.makedirs(d, exist_ok=True)
    wins, t, i = [], 0.0, 0
    while t + win <= total:
        w = os.path.join(d, "s%04d.mp3" % i)
        if not os.path.exists(w):
            subprocess.call([ff, "-y", "-v", "error", "-ss", "%.2f" % t, "-t", "%.2f" % win,
                             "-i", media, "-ar", "16000", "-ac", "1", w],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wins.append((round(t, 2), w))
        t += hop
        i += 1
    return wins, d


def transcribe(wins, cachedir):
    from songcut import ctc_align as CTC
    cache = os.path.join(cachedir, "text.json")
    if os.path.exists(cache):
        txt = json.load(open(cache, encoding="utf-8"))
        if len(txt) == len(wins):
            return txt
    model = CTC._get_model()
    out = []
    for k in range(0, len(wins), 8):
        batch = wins[k:k + 8]
        try:
            res = model.generate(input=[b[1] for b in batch], language="zh",
                                 use_itn=False, batch_size_s=120)
        except Exception:
            res = []
        for j in range(len(batch)):
            s = re.sub(r"<\|[^|]*\|>", "", res[j].get("text") or "").strip() if j < len(res) else ""
            out.append(s)
    json.dump(out, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    return out


def lcs_match(wins, texts, rows, win):
    """窗口文本序列 ↔ 歌词字符序列 全局 LCS；返回 行号 -> 实测起唱时刻。"""
    S, owner, pos_in, wlen = [], [], [], []
    for wi, s in enumerate(texts):
        cc = _cjk(s)
        for k, ch in enumerate(cc):
            S.append(ch)
            owner.append(wi)
            pos_in.append(k)
            wlen.append(len(cc))
    T, line_of = [], []
    for li, (_t, txt) in enumerate(rows):
        for ch in _cjk(txt):
            T.append(ch)
            line_of.append(li)
    n, m = len(S) + 1, len(T) + 1
    if n < 2 or m < 2:
        return {}, 0, len(S)
    dp = [array.array("H", [0]) * m]
    for i in range(1, n):
        si = S[i - 1]
        prow = dp[-1]
        cur = array.array("H", [0]) * m
        for j in range(1, m):
            if si == T[j - 1]:
                cur[j] = prow[j - 1] + 1
            else:
                a, b = cur[j - 1], prow[j]
                cur[j] = a if a >= b else b
        dp.append(cur)
    i, j = n - 1, m - 1
    match = {}
    score = dp[n - 1][m - 1]
    while i > 0 and j > 0:
        if S[i - 1] == T[j - 1]:
            frac = (pos_in[i - 1] + 0.5) / max(1, wlen[i - 1])
            match[line_of[j - 1]] = wins[owner[i - 1]][0] + win * frac
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return match, score, len(S)


def linreg(xs, ys):
    k = len(xs)
    if k < 3:
        return 0.0, 0.0
    mx = sum(xs) / k
    my = sum(ys) / k
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return 0.0, 0.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return slope, my - slope * mx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--media", required=True, help="成片 mp4/mp3")
    ap.add_argument("--lrc", required=True, help="成片实际渲染用的 lrc")
    ap.add_argument("--hop", type=float, default=0.5)
    ap.add_argument("--win", type=float, default=1.5)
    ap.add_argument("--pad", type=float, default=0.0,
                    help="成片片头补静音秒数；lrc 为「不含补静音」口径时填它")
    ap.add_argument("--bias", type=float, default=0.0,
                    help="已知的 ASR/测量系统偏差（会先从实测值里减掉），默认 0")
    ap.add_argument("--median-tol", type=float, default=0.50)
    ap.add_argument("--slope-tol", type=float, default=0.02)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.media):
        raise SystemExit("找不到成片：%s" % a.media)
    if not os.path.exists(a.lrc):
        raise SystemExit("找不到 lrc：%s" % a.lrc)

    rows = parse_lrc(open(a.lrc, encoding="utf-8").read())
    if not rows:
        raise SystemExit("lrc 里没有可用歌词行")
    total = duration_of(a.media)
    wins, cdir = slice_windows(a.media, a.hop, a.win, total)
    texts = transcribe(wins, cdir)
    match, score, nchar = lcs_match(wins, texts, rows, a.win)

    print("媒体 %.2fs / %d 窗（hop %.2fs win %.2fs）/ 歌词 %d 行"
          % (total, len(wins), a.hop, a.win, len(rows)))
    if nchar:
        print("LCS 覆盖率 %.0f%%（%d/%d 字）" % (100.0 * score / nchar, score, nchar))
    if len(match) < max(3, 0.35 * len(rows)):
        print("⚠ 命中行数太少（%d/%d），ASR 认不出这段人声，本结果不可作判据"
              % (len(match), len(rows)))
        return 5

    # 实测时刻 → 换成 lrc 口径（减 pad），再去掉已知系统偏差
    pts = []
    for li in sorted(match):
        t_axis = rows[li][0]
        t_true = match[li] - a.pad - a.bias
        pts.append((li, rows[li][0], t_true, t_true - t_axis, rows[li][1]))

    devs = [p[3] for p in pts]
    med = sorted(devs)[len(devs) // 2]
    slope, _ = linreg([p[1] for p in pts], devs)
    resid = [d - (med + slope * (p[1] - sum(x[1] for x in pts) / len(pts))) for d, p in zip(devs, pts)]
    absres = sorted(abs(r) for r in resid)
    p90 = absres[int(len(absres) * 0.9) - 1] if len(absres) > 1 else absres[-1]
    worst = max(resid, key=abs)

    print("\n 行  轴时间   实测     偏差   残差   文本")
    for (li, ta, tt, d, txt), r in zip(pts, resid):
        if a.verbose or abs(r) > 0.8 or abs(d) > 0.8:
            print("%3d %7.2f %7.2f %+6.2f %+6.2f  %s" % (li, ta, tt, d, r, txt[:20]))
    print("\n中位偏差 %+.2fs（轴比人声晚为正）  漂移 %+.4f s/s（半首 %+.2fs）"
          % (med, slope, slope * total / 2))
    print("残差 P90 %.2fs  最大 %+.2fs" % (p90, worst))

    fail = []
    if abs(med) > a.median_tol:
        fail.append("整体错位 %+.2fs（容差 %.2f）" % (med, a.median_tol))
    if abs(slope) > a.slope_tol:
        fail.append("缩放漂移 %+.4f s/s（容差 %.3f）" % (slope, a.slope_tol))
    big = [p for p, r in zip(pts, resid) if abs(r) > 1.5]
    if len(big) >= 3:
        fail.append("%d 行局部错位 >1.5s（首处 %.2fs 行%d）"
                    % (len(big), big[0][1], big[0][0]))
    if fail:
        print("\n✗ " + "；".join(fail))
        rc = 2 if abs(med) > a.median_tol else 3
        return rc if "缩放" not in fail[0] else 3
    print("\n✓ 时间轴通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
