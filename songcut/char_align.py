# -*- coding: utf-8 -*-
"""char_align.py —— 字符级强制对齐（Paraformer 逐字时间戳 → 每行真实起唱时刻）。

为什么需要它（2026-11-24 事故）：
《小跳蛙》《快乐的扑满》两首清唱，走「VAD 乐句段 + 段内按原曲节奏铺开」的分段
对齐。该通路有一条隐含假设——**段起点 = 段内第一行的起唱时刻**。这次两首的
第一个 VAD 段都吞进了 8~16 秒的无声/噪声前导（实测《小跳蛙》段 0 = 0.92~15.68s），
于是第 1、2 行被钉在 1.0s，而真实起唱在 9.5s —— 用户看到的就是「还没开始唱，
歌词已经滚到第二句」。整首的其余行也跟着这套锚点整体偏移。

本模块不再假设任何「段起点」，改为取模型自己的逐字时间：
  · Paraformer（关掉内置 VAD，直接喂短段）→ text + 每字 [start_ms, end_ms]
  · 歌词字串 vs ASR 字串做 LCS（difflib），得到每个命中字的绝对时刻
  · 每行取「该行第一个命中字」的时刻（行首字误识时按字距回推）
  · 锚点过离群检测（与邻域趋势偏差 > OUTLIER_S 视为重复段的误匹配）
  · 剩余行在锚点间按**原曲相对位置**插值

实测（11/22《小跳蛙》）：32 行命中 20 行，锚点相对原曲时间的偏移稳定在
−4.93~−5.14s（散布 0.2s），而旧通路第 1 行偏 −8.5s、第 2 行 −5.3s。
"""
import difflib
import io
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from songcut import config as CFG      # noqa: E402

FFMPEG = CFG.ffmpeg()
PAD = 0.45          # 段前缓冲（与 ctc_align 一致，覆盖咬字前沿）
PAD_TAIL = 0.45
MIN_SEG = 1.2       # 忽略过短乐句
CHAR_SEC = 0.22     # 行首字未命中时，按平均字距回推
OUTLIER_S = 4.0     # 与邻域趋势偏差超过此值 → 判为重复段误匹配

_pf = None


def _log(*a):
    print(*a)


def _get_paraformer():
    """Paraformer 关掉内置 VAD/标点：必须保留被 SenseVoice 丢掉的清唱段。"""
    global _pf
    if _pf is not None:
        return _pf
    from funasr import AutoModel
    _pf = AutoModel(model="paraformer-zh", vad_model=None, punc_model=None,
                    device="cuda:0", disable_update=True,
                    disable_pbar=True, disable_log=True)
    return _pf


def timed_chars(mp3_path, workdir, merge_gap=0.18, min_run=0.6, log=_log):
    """成片音频 → [(time_s, ch)]，时间相对 mp3 起点；按 VAD 细乐句分段识别。

    返回 None 表示模型/音频不可用（上游应降级到别的通路）。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import vocal_activity as VA

    if not os.path.exists(mp3_path):
        return None
    x = VA.load_audio(mp3_path)
    if x is None or len(x) == 0:
        return None
    total = len(x) / VA.SR
    try:
        runs, _sal, _t = VA.detect_phrases(x, merge_gap=merge_gap, min_run=min_run)
    except Exception as e:
        log("  ⚠ 乐句检测失败（%s）" % e)
        return None
    runs = [(s, e) for (s, e) in runs if e - s >= MIN_SEG]
    if not runs:
        return None
    try:
        model = _get_paraformer()
    except Exception as e:
        log("  ⚠ Paraformer 不可用（%s）" % e)
        return None

    # ⚠ 目录必须按音频隔离：workdir 在同一场次的所有歌之间是共享的，段文件若按
    #   c000.wav 命名并「存在即复用」，第二首会直接读到第一首的切片 —— 识别出的是
    #   别首歌的文本，LCS 只匹配得上寥寥几行（11/22《快乐的扑满》实测只剩 5/38）。
    _tag = re.sub(r"[^\w.-]+", "_", os.path.basename(mp3_path))[:60]
    segdir = os.path.join(workdir, "_tmp_char", _tag)
    os.makedirs(segdir, exist_ok=True)
    chars, nseg, nfail = [], 0, 0
    for k, (s, e) in enumerate(runs):
        a, b = max(0.0, s - PAD), min(total, e + PAD_TAIL)
        w = os.path.join(segdir, "c%03d.wav" % k)
        if not os.path.exists(w):
            rc = subprocess.call([FFMPEG, "-y", "-v", "error", "-ss", "%.3f" % a,
                                  "-t", "%.3f" % (b - a), "-i", mp3_path,
                                  "-ar", "16000", "-ac", "1", w],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            if rc != 0:
                nfail += 1
                continue
        try:
            r = model.generate(input=w, output_timestamp=True, batch_size_s=60)
        except Exception:
            nfail += 1
            continue
        if not r:
            nfail += 1
            continue
        nseg += 1
        txt = (r[0].get("text") or "").replace(" ", "")
        ts = r[0].get("timestamp") or []
        for ch, pair in zip(txt, ts):
            try:
                chars.append((a + float(pair[0]) / 1000.0, ch))
            except (IndexError, TypeError, ValueError):
                break
    log("  字符级识别: %d 段（失败 %d）→ %d 字，覆盖 %.1f~%.1fs"
        % (nseg, nfail, len(chars),
           chars[0][0] if chars else -1, chars[-1][0] if chars else -1))
    return chars or None


def _parse_lrc(lrc):
    """按行顺序返回 [(原时间, 文本)]。**保留空文本行**：渲染时每一条时间标签都
    占一行，若这里丢掉「只有时间、没有文字」的行，_rewrite 的行号就会和原文错位，
    后面的歌词会整体串行。"""
    rows = []
    for line in (lrc or "").splitlines():
        m = re.search(r"\[(\d{1,3}):(\d{2}(?:[.:]\d{1,3})?)\]", line)
        if not m:
            continue
        t = int(m.group(1)) * 60 + float(m.group(2).replace(":", "."))
        txt = re.sub(r"<[^>]*>", "", re.sub(r"\[[^\]]*\]", "", line)).strip()
        rows.append((t, txt))
    return rows


def _cjk(t):
    return "".join(ch for ch in str(t) if "\u4e00" <= ch <= "\u9fff")


def _lcs(a, b):
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b):
            cur.append(prev[j] + 1 if ca == cb else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def _score(a, b):
    a, b = _cjk(a), _cjk(b)
    if len(a) < 2 or len(b) < 1:
        return 0.0
    return _lcs(a, b) / float(min(len(a), len(b)))


def _hit_anchors(rows, chars):
    """LCS 对齐 → 每行锚点 {行号: (时刻, 置信字数)}。chars 为 cut 相对时间。"""
    L, idx_of = [], []
    for i, (_t, txt) in enumerate(rows):
        for ch in txt:
            if "\u4e00" <= ch <= "\u9fff":
                L.append(ch)
                idx_of.append(i)
    A = [c for _t, c in chars]
    if not L or not A:
        return {}
    sm = difflib.SequenceMatcher(None, "".join(L), "".join(A), autojunk=False)
    per_line = {}
    for blk in sm.get_matching_blocks():
        for j in range(blk.size):
            li = idx_of[blk.a + j]
            per_line.setdefault(li, []).append((blk.a + j, chars[blk.b + j][0]))
    anchors = {}
    for li, hs in per_line.items():
        hs.sort()
        off, t = hs[0]
        nth = sum(1 for q in idx_of[:off] if q == li)     # 行内第几个字
        anchors[li] = (t - CHAR_SEC * nth, len(hs))
    return anchors


def _drop_outliers(anchors, rows):
    """重复副歌会让 LCS 把某行匹配到别的实例（实测《快乐的扑满》偏 +20s）。
    用邻域（前后各 2 个锚点）的线性预测做残差检验，超阈值即剔除。"""
    keys = sorted(anchors)
    if len(keys) < 5:
        return anchors
    bad = []
    for k, li in enumerate(keys):
        t_abs, _n = anchors[li]
        # 用前后最近的锚点做线性预测（跨过本行，避免自证）
        prv = keys[max(0, k - 3):k]
        nxt = keys[k + 1:k + 4]
        if not prv or not nxt:
            continue
        t0, a0 = rows[prv[-1]][0], anchors[prv[-1]][0]
        t1, a1 = rows[nxt[0]][0], anchors[nxt[0]][0]
        if t1 <= t0:
            continue
        pred = a0 + (rows[li][0] - t0) / (t1 - t0) * (a1 - a0)
        if abs(t_abs - pred) > OUTLIER_S:
            bad.append(li)
    for li in bad:
        del anchors[li]
    return anchors


def _interp(rows, anchors, cut_start, cut_end):
    """锚点 → 每行 cut 相对时间（锚点间按原曲相对位置插值 + 单调 + 片尾兜底）。"""
    pts = sorted((rows[li][0], t) for li, (t, _n) in anchors.items())
    clean = []
    for t_old, t_new in pts:                     # 两侧严格单调
        if clean and (t_new <= clean[-1][1] or t_old <= clean[-1][0]):
            continue
        clean.append((t_old, t_new))
    pts = clean
    dur = cut_end - cut_start

    def _slope(a, b):
        return min(2.0, max(0.5, (b[1] - a[1]) / max(1e-6, b[0] - a[0])))

    def to_cut(t_old):
        if not pts:
            return None
        if t_old <= pts[0][0]:
            k = _slope(pts[0], pts[1]) if len(pts) > 1 else 1.0
            return max(0.0, pts[0][1] - (pts[0][0] - t_old) * k)
        if t_old >= pts[-1][0]:
            k = _slope(pts[-2], pts[-1]) if len(pts) > 1 else 1.0
            return pts[-1][1] + (t_old - pts[-1][0]) * k
        for i in range(len(pts) - 1):
            (t0, a0), (t1, a1) = pts[i], pts[i + 1]
            if t0 <= t_old <= t1:
                return a0 + (t_old - t0) / max(1e-6, t1 - t0) * (a1 - a0)
        return pts[-1][1]

    out = [to_cut(t) for t, _ in rows]
    # 最小行距保护：锚点过密时两行会被压成 0.0x 秒（QC L05 的 BLOCK 线是 0.4s），
    # 而「短行」判据会因此把整条时间轴判为不可用 —— 实测《快乐的扑满》就卡在这里。
    min_gap = 0.45
    for i in range(1, len(out)):
        if out[i] is not None and out[i - 1] is not None \
                and out[i] - out[i - 1] < min_gap:
            out[i] = out[i - 1] + min_gap
    # 片尾兜底：越界的行在「上一行~片尾前 0.3s」之间均分，每行至少 0.55s
    limit = max(0.05, dur - 0.30)
    over = [k for k, v in enumerate(out) if v is not None and v > limit]
    if over:
        p = over[0]
        base = out[p - 1] if p > 0 else 0.0
        n = len(out) - p
        limit = min(max(0.05, dur - 0.25), max(base + 0.55 * n, dur - 1.0))
        if limit <= base:
            limit = min(max(0.05, dur - 0.10), base + 0.55 * n)
        for k in range(p, len(out)):
            out[k] = base + (limit - base) * ((k - p + 1) / float(n))
    return out


def _rewrite(lrc, rows, new_t):
    """按行顺序把时间戳写回原文（保留 <mm:ss.xx> 逐字标签的相对偏移）。"""
    out, k, last = [], 0, -1.0
    for line in (lrc or "").splitlines():
        m = re.search(r"\[(\d{1,3}):(\d{2}(?:[.:]\d{1,3})?)\]", line)
        if not m:
            if not re.search(r"<\d{1,3}:\d{2}", line):
                out.append("")
            continue
        if k >= len(new_t) or new_t[k] is None:
            out.append(line)                 # 无新时间 → 原样保留，不丢行
            k += 1
            continue
        t_old = rows[k][0]
        t_new = max(last + 0.02, new_t[k])
        last = t_new
        d = t_new - t_old

        def sub(mm, _d=d):
            s = int(mm.group(1)) * 60 + float(mm.group(2).replace(":", ".")) + _d
            s = max(0.0, s)
            return "[%02d:%05.2f]" % (int(s // 60), s % 60)
        out.append(re.sub(r"\[(\d{1,3}):(\d{2}(?:[.:]\d{1,3})?)\]", sub, line,
                          count=1))
        k += 1
    return "\n".join(out).strip()


def slide_verify(lrc_text, mp3_path, workdir, win=1.5, hop=0.75, log=_log):
    """时间轴独立验收：滑窗 ASR 逐窗文本 → 每行「首次出现」时刻 → 与 LRC 比。

    为什么不用 audit_timeline：它只接受 <=6s 的 SRT 短句，两首歌各只有 11 个
    可判样本，且长合并句（正是出错最狠的地方）全被跳过 —— 于是 8 秒的整体错位
    也能拿到「命中率 100%」。这里改成固定网格滑窗（不同模型、不同粒度、不依赖
    VAD），样本覆盖整片。

    返回 {judged, hit_rate, median_dev, p90_dev, fails}；median_dev 是本片
    「歌词比实际演唱早/晚多少」的中位数，绝对值 > 1.0s 即判整体错位。
    """
    import subprocess
    rows = _parse_lrc(lrc_text)
    info = {"judged": 0, "hit_rate": None, "median_dev": None,
            "p90_dev": None, "fails": []}
    if len(rows) < 4 or not os.path.exists(mp3_path):
        return info
    total = 0.0
    try:
        r = subprocess.run([CFG.ffprobe(), "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", mp3_path],
                           capture_output=True, text=True)
        total = float(r.stdout.strip() or 0)
    except Exception:
        total = 0.0
    if total <= 0:
        return info

    # 同样按音频隔离（理由见 timed_chars）：滑窗是固定网格，跨首复用会直接串音。
    _tag = re.sub(r"[^\w.-]+", "_", os.path.basename(mp3_path))[:60]
    d = os.path.join(workdir, "_tmp_slide", _tag)
    os.makedirs(d, exist_ok=True)
    wins, t, i = [], 0.0, 0
    while t + win <= total + 0.01:
        w = os.path.join(d, "s%04d.mp3" % i)
        if not os.path.exists(w):
            rc = subprocess.call([FFMPEG, "-y", "-v", "error", "-ss", "%.2f" % t,
                                  "-t", "%.2f" % win, "-i", mp3_path,
                                  "-ar", "16000", "-ac", "1", w],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            if rc != 0:
                break
        wins.append((t, w))
        t += hop
        i += 1
    if not wins:
        return info
    try:
        from songcut import ctc_align as CA
        model = CA._get_model()
    except Exception as e:
        log("  ⚠ 验收模型不可用（%s）" % e)
        return info
    texts = []
    for k in range(0, len(wins), 8):
        batch = wins[k:k + 8]
        try:
            res = model.generate(input=[b[1] for b in batch], language="auto",
                                 use_itn=False, batch_size_s=120)
        except Exception:
            res = []
        for j in range(len(batch)):
            s = ""
            if j < len(res):
                s = re.sub(r"<\|[^|]*\|>", "", res[j].get("text") or "").strip()
            texts.append(s)

    devs = []
    prev_hit = -1e9
    prev_txt = ""
    for li, (t_row, txt) in enumerate(rows):
        tgt = _cjk(txt)
        if len(tgt) < 3:
            continue
        # 逐行推进下界：歌词里「重复副歌」很常见（《小跳蛙》连唱两遍同一句），
        # 不设下界就会把前一遍当成这一遍，凭空造出 −4s 的假偏差。
        same = (txt.strip() == prev_txt.strip())
        lo = max(t_row - 6.0, prev_hit + (2.5 if same else 0.3))
        first = None
        for (ws, _p), s in zip(wins, texts):
            if ws > t_row + 6.0:
                break
            if ws + win < lo:
                continue
            if _score(txt, s) >= 0.55:
                first = ws
                break
        if first is None:
            info["fails"].append((round(t_row, 1), None, txt[:12]))
            prev_txt = txt
            continue
        dev = (first + win / 2.0) - t_row
        devs.append(dev)
        info["judged"] += 1
        prev_hit = first
        prev_txt = txt
        if abs(dev) > 1.5:
            info["fails"].append((round(t_row, 1), round(dev, 2), txt[:12]))
    if not devs:
        return info
    devs.sort()
    info["median_dev"] = round(devs[len(devs) // 2], 2)
    info["p90_dev"] = round(devs[int(len(devs) * 0.9)], 2)
    n_all = info["judged"] + len([f for f in info["fails"] if f[1] is None])
    info["hit_rate"] = round(
        sum(1 for d in devs if abs(d) <= 1.5) / float(max(1, n_all)), 3)
    info["cover"] = round(len(devs) / float(max(1, len(rows))), 3)
    info["summary"] = ("滑窗验收 %d/%d 行：中位偏差 %+.2fs，P90 %+.2fs，"
                       "通过率 %.0f%%"
                       % (info["judged"], len(rows), info["median_dev"],
                          info["p90_dev"], info["hit_rate"] * 100))
    return info


def align_lrc_chars(lrc_text, chars, cut_start, cut_end, log=_log):
    """主入口：chars 为 **cut 相对时间** 的 [(t, ch)]。返回 (lrc, info)。"""
    info = {}
    rows = _parse_lrc(lrc_text)
    if len(rows) < 4 or not chars:
        info["summary"] = "歌词/字符不足（%d 行 / %d 字）" % (len(rows), len(chars))
        return None, info
    anchors = _hit_anchors(rows, chars)
    if len(anchors) < 3:
        info["summary"] = "字符锚点仅 %d 个" % len(anchors)
        return None, info
    raw_n = len(anchors)
    anchors = _drop_outliers(anchors, rows)
    if len(anchors) < 3:
        info["summary"] = "剔除离群后锚点仅 %d 个" % len(anchors)
        return None, info
    new_t = _interp(rows, anchors, cut_start, cut_end)
    gaps = []
    keys = sorted(anchors)
    for a, b in zip(keys, keys[1:]):
        gaps.append((anchors[b][0] - anchors[a][0], rows[a][0]))
    info["anchors"] = len(anchors)
    info["anchors_raw"] = raw_n
    info["rows"] = len(rows)
    if gaps:
        g, at = max(gaps)
        info["max_gap_s"] = round(g, 1)
        info["max_gap_at"] = round(at, 1)
    info["summary"] = ("字符级对齐 %d 行（命中 %d 行 / 剔除离群 %d）"
                       % (len(rows), len(anchors), raw_n - len(anchors)))
    return _rewrite(lrc_text, rows, new_t), info
