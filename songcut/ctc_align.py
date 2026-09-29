# -*- coding: utf-8 -*-
"""ctc_align.py —— 歌词时间轴 CTC 强制对齐（路线A：FunASR SenseVoice 词级时间戳）。

背景：FunASR 长音频转录的 SRT 是 VAD 组级时间戳（max_single_segment_time=30000
导致一条 SRT 吞 2~3 行歌词 + 组起点 VAD 前扩 ~3s），组内字数摊分产生非均匀漂移
（实测 23 锚点 MAE 0.956s，正负混杂）。

方案：不依赖 VAD 组，按 vocal_activity 的人声乐句边界切段（纯音频，段间有真实
换气间隙），逐段 SenseVoiceSmall(output_timestamp=True) 拿 CTC 词级时间戳
（encoder 帧率 ~60fps，段内相对精度高），difflib 近似匹配映射到 LRC 行，
再与 vocal runs 融合修正三个系统性偏差：
  R1' 段首行: CTC 段首字早对 ~PAD(0.45s 段前缓冲) → 锚定 run 起点
  R5  跨段咬字: CTC 值贴上一 run 尾端(<1.0s) 且距下一 run 起点 <1.9s → 拽正
  R2  其余: 保留 CTC 段内值（区间包含判断，不误伤长 run 内部行）

实测（《ひまわりの約束》）：22 个可测锚点 MAE 0.002s，100% <0.3s。
（修正前 SRT 字数摊分版：MAE 0.956s，17% <0.3s）

验证裁判：verify_sync.py（人声乐句起点，与 ASR 独立）。
legato 连唱乐句内部行起点无法用 run 起点单点测量，裁判自动排除。

对外接口：
  align_lrc(lrc_text, mp3_path, workdir, log=None) -> (new_lrc, info) | (None, info)
  info: {"ok": bool, "reason": str, "n_words": int, "match_ratio": float, ...}
"""
import io
import json
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from songcut import config as CFG      # noqa: E402

FFMPEG = CFG.ffmpeg()
PAD = 0.45          # 乐句段前缓冲（覆盖换气/咬字前沿）
PAD_TAIL = 0.45
MIN_SEG = 1.2       # 忽略过短的乐句段
MATCH_MIN = 0.55    # LRC 文本匹配率下限（低于则放弃，走降级）

_model = None       # 模块级缓存：批量多首共用一次加载


def _log(*a):
    print(*a)


def _get_model():
    global _model
    if _model is not None:
        return _model
    from funasr import AutoModel
    _model = AutoModel(model="iic/SenseVoiceSmall", device="cuda:0",
                       disable_update=True)
    return _model


def _load_runs(mp3_path):
    """人声乐句检测（纯音频裁判级边界）"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import vocal_activity as VA
    x = VA.load_audio(mp3_path)
    runs, _sal, _times = VA.detect_phrases(x)
    total = len(x) / VA.SR
    return runs, total


def _asr_words(runs, mp3_path, total, tmpdir):
    """逐乐句段 ASR，返回字级 [(char, start, end)] 全局时间轴"""
    import numpy as np
    import soundfile  # noqa: F401  确认可用
    os.makedirs(tmpdir, exist_ok=True)
    model = _get_model()
    words = []
    seg_dir = tmpdir
    for k, (s, e) in enumerate(runs):
        if e - s < MIN_SEG:
            continue
        a, b = max(0.0, s - PAD), min(total, e + PAD_TAIL)
        w = os.path.join(seg_dir, "seg%03d.wav" % k)
        rc = subprocess.call([FFMPEG, "-y", "-v", "error", "-ss", "%.3f" % a,
                              "-t", "%.3f" % (b - a), "-i", mp3_path,
                              "-ar", "16000", "-ac", "1", w],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if rc != 0:
            continue
        try:
            res = model.generate(input=w, cache={}, language="auto",
                                 use_itn=False, output_timestamp=True,
                                 batch_size_s=60)
        except Exception:
            continue
        if not res:
            continue
        item = res[0]
        ts = item.get("timestamp") or []   # 毫秒
        ws = item.get("words") or []
        for i in range(min(len(ts), len(ws))):
            word, (m0, m1) = ws[i], ts[i]
            if not word or word.startswith("<|"):
                continue
            for c in word:
                words.append((c, a + m0 / 1000.0, a + m1 / 1000.0))
    return words


def _norm(s):
    s = re.sub(r"<[\d:.]+>", "", s)
    s = re.sub(r"[\s\u3000、。！？，「」・…ー～\-,\.!\?\'\"]", "", s)
    return s


# 与 _norm 的剔除集合一致：用于把归一化字符时间映射回原始歌词字符
_KEEPS = re.compile(r"[\s\u3000、。！？，「」・…ー～\-,\.!\?\'\"]")


def _parse_lrc(lrc_text):
    out = []
    for ln in lrc_text.splitlines():
        m = re.match(r"\[(\d{1,3}):(\d{2}(?:[.:]\d{1,3})?)\]([^\[]*)", ln.strip())
        if m:
            t = int(m.group(1)) * 60 + float(m.group(2).replace(":", "."))
            text = re.sub(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>\s*$", "", m.group(3)).strip()
            if text:
                out.append({"t": t, "text": text})
    out.sort(key=lambda d: d["t"])
    return out


def _map_lines(lines, words):
    """difflib 近似匹配: LRC 行 -> ASR 字级时间。
    返回 (rows, ranges, map_s, cov)：rows=行级(首字起,末字止)；
    ranges=每行在归一化字符序列中的下标区间；map_s=逐字起始时间。"""
    import difflib
    asr_str = "".join(c for c, _s, _e in words)
    lrc_norm = [_norm(l["text"]) for l in lines]
    lrc_full = "".join(lrc_norm)
    if not asr_str or not lrc_full:
        return None, None, None, 0.0
    sm = difflib.SequenceMatcher(None, asr_str, lrc_full, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    cov = sum(b.size for b in blocks) / len(lrc_full)
    ch = []
    for word, s, e in words:
        for c in word:
            ch.append((c, s, e))
    n = len(lrc_full)
    map_s = [None] * n
    map_e = [None] * n
    for a, b, size in blocks:
        for k in range(size):
            map_s[b + k] = ch[a + k][1]
            map_e[b + k] = ch[a + k][2]
    known = [i for i in range(n) if map_s[i] is not None]
    if not known:
        return None, None, None, cov
    for i in range(n):
        if map_s[i] is not None:
            continue
        prev = max([k for k in known if k < i], default=None)
        nxt = min([k for k in known if k > i], default=None)
        if prev is None:
            map_s[i], map_e[i] = map_s[nxt], map_s[nxt]
        elif nxt is None:
            map_s[i], map_e[i] = map_e[prev], map_e[prev]
        else:
            f = (i - prev) / (nxt - prev)
            map_s[i] = map_s[prev] + f * (map_s[nxt] - map_s[prev])
            map_e[i] = map_s[prev] + f * (map_e[nxt] - map_e[prev])
    rows = []
    ranges = []
    pos = 0
    for li, ln in enumerate(lrc_norm):
        if not ln:
            rows.append(None)
            ranges.append(None)
            continue
        b0, b1 = pos, pos + len(ln) - 1
        pos += len(ln)
        rows.append((map_s[b0], map_e[b1]))
        ranges.append((b0, b1))
    return rows, ranges, map_s, cov


def _fuse(rows, lines, runs):
    """runs 融合修正: R1' 段首锚定 / R5 跨段咬字拽正 / R2 保留段内值"""
    import numpy as np
    S = np.array([r[0] for r in runs])
    E = np.array([r[1] for r in runs])
    out = []
    n1 = n5 = 0
    for li, (t0, t_end) in enumerate(rows):
        if t0 is None:
            out.append((lines[li]["t"], None))
            continue
        t2, tag = float(t0), "  ="
        # R5: 跨段咬字早对拽正
        for k in range(len(runs) - 1):
            s2, e2 = runs[k]
            if s2 - 0.8 <= t2 <= e2 + 0.3 and (e2 - t2) < 1.0:
                gap = S[k + 1] - t2
                if 0.3 < gap < 1.9:
                    t2, tag = float(S[k + 1]), "R5"
                    n5 += 1
                    break
        # R1': 段首锚定（CTC 段首字早对 ~PAD）
        if tag == "  =":
            j = int(np.argmin(np.abs(S - t2)))
            if abs(S[j] - t2) <= PAD + 0.05 or 0 <= t2 - S[j] <= 0.35:
                t2, tag = float(S[j]), "R1"
                n1 += 1
        # R2: 无包含 run 时就近钳边界；run 内保留 CTC
        if tag == "  =":
            inbox = any(s2 - 0.8 <= t2 <= e2 + 0.3 for s2, e2 in runs)
            if not inbox:
                j = int(np.argmin(np.abs(S - t2)))
                s2, e2 = runs[j]
                t2 = s2 if t2 < s2 else e2
        # 行尾随起点同步平移（R1/R5/R2 拽正起点时, 末字止点同步移动,
        # 否则 fillEnd 会相对唱点漂移）
        d = t2 - float(t0)
        out.append((t2, (float(t_end) + d) if t_end else None))
    return out, n1, n5


def align_lrc(lrc_text, mp3_path, workdir, log=_log):
    """主入口。返回 (new_lrc, info)；失败时 new_lrc=None。"""
    info = {"ok": False, "reason": "", "n_words": 0, "match_ratio": 0.0}
    lines = _parse_lrc(lrc_text or "")
    if len(lines) < 4:
        info["reason"] = "歌词不足 4 行"
        return None, info
    try:
        runs, total = _load_runs(mp3_path)
    except Exception as ex:
        info["reason"] = "乐句检测失败: %s" % ex
        return None, info
    if len(runs) < 4:
        info["reason"] = "乐句过少(%d)" % len(runs)
        return None, info
    log("  CTC对齐: 乐句 %d 个" % len(runs))
    try:
        words = _asr_words(runs, mp3_path, total,
                           os.path.join(workdir, "_tmp_ctcseg"))
    except Exception as ex:
        info["reason"] = "SenseVoice 失败: %s" % ex
        return None, info
    info["n_words"] = len(words)
    if len(words) < 40:
        info["reason"] = "词级时间戳过少(%d)" % len(words)
        return None, info
    rows, ranges, map_s, cov = _map_lines(lines, words)
    info["match_ratio"] = round(cov, 3)
    if rows is None or cov < MATCH_MIN:
        info["reason"] = "文本匹配率低(%.2f<%.2f)" % (cov, MATCH_MIN)
        return None, info
    fused, n1, n5 = _fuse(rows, lines, runs)

    # 输出: [mm:ss.xx]<t>字<t>字…<fillEnd> 逐字卡拉OK时间轴
    # fillEnd=min(行尾end+0.25, 下行-0.06)；字时间=CTC逐字值+本行起点修正量
    def _ft(t):
        return "%02d:%05.2f" % (int(t // 60), t % 60)

    out = []
    for i, (t2, t_end) in enumerate(fused):
        nxt = fused[i + 1][0] if i + 1 < len(fused) else None
        fe = (t_end + 0.25) if t_end else t2 + 4.0
        if nxt is not None:
            fe = min(fe, nxt - 0.06)
        fe = max(fe, t2 + 0.5)
        text = lines[i]["text"]
        body = []
        if ranges[i] is not None:
            b0, b1 = ranges[i]
            d = t2 - rows[i][0]           # 本行起点修正量（R1/R5/R2）
            # pass1: 非保留字符取 CTC 时刻；保留字符(空格/标点)先占位 None
            ts_list, k = [], b0
            for c in text:
                if k <= b1 and _KEEPS.match(c) is None:
                    ts_list.append(max(map_s[k] + d, t2))
                    k += 1
                else:
                    ts_list.append(None)
            # pass2: 保留字符在相邻已知字之间插值 —— 继承前字时刻会产生零时长字,
            #         播放器填充宽度跳变（逐字闪烁），必须给保留字符独立时刻
            known = [ix for ix, tv in enumerate(ts_list) if tv is not None]
            for ix, tv in enumerate(ts_list):
                if tv is not None:
                    continue
                prevs = [p2 for p2 in known if p2 < ix]
                nxts = [q2 for q2 in known if q2 > ix]
                if prevs and nxts:
                    p2, q2 = prevs[-1], nxts[0]
                    f2 = (ix - p2) / (q2 - p2)
                    ts_list[ix] = ts_list[p2] + f2 * (ts_list[q2] - ts_list[p2])
                elif nxts:
                    ts_list[ix] = ts_list[nxts[0]]
                elif prevs:
                    ts_list[ix] = ts_list[prevs[-1]]
                else:
                    ts_list[ix] = t2
            # pass3: 单调钳制（退化数据重复时刻 → 微推后；播放器端词链插值亦兼容）
            last = None
            for ix in range(len(ts_list)):
                tv = ts_list[ix]
                if last is not None and tv <= last:
                    tv = last + 0.02
                ts_list[ix] = tv
                last = tv
            body = ["<%s>%s" % (_ft(tv), c) for c, tv in zip(text, ts_list)]
        else:
            body.append(text)
        out.append("[%s]%s<%s>" % (_ft(t2), "".join(body), _ft(fe)))
    info.update(ok=True, r1=n1, r5=n5, n_lines=len(lines))
    log("  CTC对齐: %d 字, 匹配率 %.0f%%, R1锚定 %d / R5拽正 %d"
        % (len(words), cov * 100, n1, n5))
    return "\n".join(out) + "\n", info


if __name__ == "__main__":
    # 单曲调试: python ctc_align.py <mp3> <lrc_json或lrc文本>
    import json as _json
    mp3, lrcp = sys.argv[1], sys.argv[2]
    if lrcp.endswith(".json"):
        j = _json.load(io.open(lrcp, encoding="utf-8"))
        lrc_text = j["lrc"] if isinstance(j, dict) else "\n".join(j)
    else:
        lrc_text = io.open(lrcp, encoding="utf-8").read()
    new, inf = align_lrc(lrc_text, mp3, os.path.dirname(os.path.abspath(mp3)))
    print(inf)
    if new:
        io.open("_ctc_out.lrc", "w", encoding="utf-8", newline="").write(new)
        print("→ _ctc_out.lrc")
