#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pv_theme.py —— 曲风 / 音频特征 → JIZURA theme 的映射表与判据器。

为什么需要它
------------
JIZURA 官方只有 6 个 theme（lyricpv / kinetic / wa / horror / pop / ballad），
没有 genre 标签（RULES §117.1）。而 `J.omakase(p, rnd, themeId)` 一换 theme，
版式家族就整体换一套（§117.2）。所以「按曲风自动出气质」这件事，
缺的只是一张能从曲风落到 theme 的桥。

两张表
------
1. GENRE_THEME：人工标注的曲风 → theme 先验（音乐学常识 + 官方 theme 语义）
2. THEME_RULES：音频客观量 → theme 的打分规则（阈值用实测校准）

两张表都要能复核：命中哪条、读数多少，全部打印。

用法
----
    python tools/pv_theme.py --analysis a.json                  # 纯音频推断
    python tools/pv_theme.py --analysis a.json --genre 爵士      # 给曲风先验
    python tools/pv_theme.py --analysis a.json --genre 爵士 --json out.json

退出码：0 正常；2 参数错。
"""
import argparse
import base64
import json
import sys

import numpy as np

# ── 表 1：曲风 → theme 先验 ────────────────────────────────────────────
# 一个曲风可以落多个 theme，第一个是首选。曲风名做成同义词表，
# 因为歌单里可能写「抒情」「慢歌」「ballad」任意一种。
GENRE_THEME = {
    "爵士":   (["lyricpv", "pop"],       "戏剧性、字重排版、切分节奏 → 版式感优先"),
    "戏剧":   (["lyricpv", "pop"],       "同上：舞台/叙事→编辑排版气质"),
    "音乐剧": (["lyricpv", "pop"],       "同上"),
    "抒情":   (["ballad", "lyricpv"],    "慢速留白 → 钟摆式大字最稳"),
    "慢歌":   (["ballad", "lyricpv"],    "同上"),
    "民谣":   (["wa", "ballad"],         "原声、留白、五声倾向 → 和风/纸感"),
    "古风":   (["wa", "ballad"],         "五声、丝竹、墨色 → 和风"),
    "和风":   (["wa", "ballad"],         "同上"),
    "流行":   (["pop", "kinetic"],       "中快、明亮、饱满 → 波普"),
    "摇滚":   (["kinetic", "pop"],       "快、密、低频强 → 动力学"),
    "电子":   (["kinetic", "pop"],       "同上"),
    "舞曲":   (["kinetic", "pop"],       "同上"),
    "金属":   (["kinetic", "horror"],    "极快 + 低频轰 + 高对比"),
    "硬核":   (["kinetic", "horror"],    "同上"),
    "说唱":   (["lyricpv", "kinetic"],   "短句密集、字本身是节奏 → 版式"),
    "叙事":   (["lyricpv", "ballad"],    "念白感、句子短促"),
    "暗黑":   (["horror", "lyricpv"],    "小调、低频、低高频"),
    "悬疑":   (["horror", "editorial"],  "同上"),
    "恐怖":   (["horror", "lyricpv"],    "同上"),
    "童趣":   (["pop"],                  "明亮、圆润、高饱和"),
    "治愈":   (["ballad", "wa"],         "慢、软、留白"),
}
# 同义词归一
GENRE_ALIAS = {
    "ballad": "抒情", "抒情歌": "抒情", "slow": "慢歌", "慢": "慢歌",
    "folk": "民谣", "gufeng": "古风", "trad": "古风", "和风": "和风",
    "pop": "流行", "rock": "摇滚", "edm": "电子", "dance": "舞曲",
    "metal": "金属", "rap": "说唱", "hiphop": "说唱", "hip-hop": "说唱",
    "dark": "暗黑", "horror": "恐怖", "jazz": "爵士", "jazzpop": "爵士",
    "theatrical": "戏剧", "musical": "音乐剧", "kids": "童趣", "healing": "治愈",
}

# ── 表 2：音频客观量 → theme 打分 ─────────────────────────────────────
# 每条 = (说明, 判据函数, 权重)。权重不是概率，是"这条判据有多能代表该 theme"。
# 阈值来源：音乐学常识区间 + 《小丑的品格》(bpm120/jazz)、《画心》(bpm138/pop) 实测校准。
THEME_RULES = {
    "kinetic": [
        ("bpm>=132",          lambda f: f["bpm"] >= 132,                    0.34),
        ("flux>=0.42",        lambda f: f["flux_mean"] >= 0.42,             0.26),
        ("低频>=0.38",        lambda f: f["low"] >= 0.38,                   0.20),
        ("高频>=0.20",        lambda f: f["high"] >= 0.20,                  0.12),
        ("人声句长<=0.55s",   lambda f: 0 < f["voiced_med_run"] <= 0.55,    0.08),
    ],
    "pop": [
        ("bpm 104~140",       lambda f: 104 <= f["bpm"] <= 140,             0.28),
        ("中频>=0.37",        lambda f: f["mid"] >= 0.37,                   0.22),
        ("大调倾向>=0.45",    lambda f: (1 - f["minor_tilt"]) >= 0.45,      0.20),
        ("动态<=0.30(压缩)",  lambda f: f["rms_dyn"] <= 0.30,               0.16),
        ("高频>=0.19",        lambda f: f["high"] >= 0.19,                  0.14),
    ],
    "ballad": [
        ("bpm<=100",          lambda f: f["bpm"] <= 100,                    0.34),
        ("flux<=0.34",        lambda f: f["flux_mean"] <= 0.34,             0.24),
        ("动态<=0.28(平稳)",  lambda f: f["rms_dyn"] <= 0.28,               0.22),
        ("人声句长>=0.70s",   lambda f: f["voiced_med_run"] >= 0.70,        0.20),
    ],
    "lyricpv": [
        ("人声句长<=0.60s",   lambda f: 0 < f["voiced_med_run"] <= 0.60,    0.30),
        ("断续>=1.00/s",      lambda f: f["voiced_edges_per_s"] >= 1.00,    0.26),
        ("bpm 104~136",       lambda f: 104 <= f["bpm"] <= 136,             0.18),
        ("flux 0.30~0.48",    lambda f: 0.30 <= f["flux_mean"] <= 0.48,     0.16),
        ("五声性<=0.92",      lambda f: f["penta"] <= 0.92,                 0.10),
    ],
    "wa": [
        ("五声性>=0.96",      lambda f: f["penta"] >= 0.96,                 0.40),
        ("bpm<=108",          lambda f: f["bpm"] <= 108,                    0.20),
        ("高频 0.18~0.30",    lambda f: 0.18 <= f["high"] <= 0.30,          0.22),
        ("低频<=0.40",        lambda f: f["low"] <= 0.40,                   0.18),
    ],
    "horror": [
        ("小调倾向>=0.58",    lambda f: f["minor_tilt"] >= 0.58,            0.34),
        ("低频>=0.42",        lambda f: f["low"] >= 0.42,                   0.24),
        ("高频<=0.19",        lambda f: f["high"] <= 0.19,                  0.26),
        ("响度<=0.78",        lambda f: f["rms_mean"] <= 0.78,              0.16),
    ],
}

THEME_NOTE = {
    "lyricpv": "文字PV：编辑排版感、注记、数字、字重对比",
    "kinetic": "キネティック：快切、彩条、撞字、高饱和",
    "wa":      "和风：纸感、墨、竖排、留白",
    "horror":  "恐怖：暗底、噪点、监视感、红白",
    "pop":     "波普：明亮、圆润、高饱和、贴纸感",
    "ballad":  "バラード：慢、留白、钟摆、柔和",
}


def _u8(s):
    return np.frombuffer(base64.b64decode(s), dtype=np.uint8).astype(np.float32) / 255.0


def features(path):
    """从 analysis.json（tools/analyze_music.py 产物）提取可判别特征。"""
    d = json.load(open(path, encoding="utf-8"))
    n, nb, fps = d["nframes"], d["nbands"], d["fps"]
    bands = np.frombuffer(base64.b64decode(d["bands_b64"]), dtype=np.uint8)
    if bands.size != n * nb:
        raise SystemExit("bands 长度与 nframes*nbands 不符：%d vs %d" % (bands.size, n * nb))
    bands = bands.reshape(n, nb).astype(np.float32) / 255.0
    rms, flux, voiced = _u8(d["rms_b64"])[:n], _u8(d["flux_b64"])[:n], _u8(d["voiced_b64"])[:n]
    midi = np.frombuffer(base64.b64decode(d["midi10_b64"]), dtype=np.int16).astype(np.float32)[:n]

    hz = np.array(d["band_hz"], dtype=float)
    masks = {"low": hz[:-1] < 250, "mid": (hz[:-1] >= 250) & (hz[:-1] < 2200), "high": hz[:-1] >= 2200}
    share = {k: float((bands[:, m].mean(1) / (bands.mean(1) + 1e-9)).mean()) for k, m in masks.items()}
    tot = sum(share.values()) + 1e-9
    share = {k: v / tot for k, v in share.items()}

    v = (voiced > 0.5).astype(np.int8)
    runs, cur = [], 0
    for x in v:
        if x:
            cur += 1
        elif cur:
            runs.append(cur); cur = 0
    if cur:
        runs.append(cur)

    f = dict(
        dur=round(float(d["dur"]), 2), bpm=float(d["bpm"]),
        rms_mean=float(rms.mean()), rms_dyn=float(np.percentile(rms, 90) - np.percentile(rms, 10)),
        flux_mean=float(flux.mean()),
        voiced_ratio=float(v.mean()),
        voiced_edges_per_s=float(np.abs(np.diff(v)).sum() / 2 / (n / fps)),
        voiced_med_run=float(np.median(runs) / fps) if runs else 0.0,
        low=share["low"], mid=share["mid"], high=share["high"],
        penta=0.0, minor_tilt=0.5,
    )

    pv = midi[midi > -100]
    if pv.size > 50:
        pc = np.round(pv / 10.0).astype(int) % 12
        h = np.bincount(pc, minlength=12).astype(float)
        h /= h.sum() + 1e-9
        acc, k = 0.0, 0
        for i in np.argsort(-h):
            acc += h[i]; k += 1
            if acc >= 0.70:
                break
        f["penta"] = 5.0 / k                      # k=5→1.0（纯五声） k=7→0.71 k=12→0.42
        tonic = int(np.argmax(h))
        maj = h[(tonic + 4) % 12] + h[(tonic + 7) % 12]
        mino = h[(tonic + 3) % 12] + h[(tonic + 7) % 12]
        f["minor_tilt"] = float(mino / (maj + mino + 1e-9))
        f["tonic"] = tonic
    return f


def score(f):
    """返回 {theme: (原始权重和, 归一化得分, 命中明细)}。

    归一化 = 权重和 / 该 theme 的权重总和。原始和不能直接比：
    horror 只有 4 条、pop 有 5 条，条数不同会让"多写几条规则"的 theme 天然占优。
    """
    out = {}
    for th, rules in THEME_RULES.items():
        hits, s, wtot = [], 0.0, 0.0
        for name, fn, w in rules:
            ok = bool(fn(f))
            hits.append((name, ok, w))
            wtot += w
            if ok:
                s += w
        out[th] = (round(s, 3), round(s / (wtot + 1e-9), 3), hits)
    return out


def axes(f):
    """三个气质轴（0~1），用来解释"为什么是它"，比单个主题分更可信。

    tempo  快慢：bpm 60→0，160→1
    bright 明暗：高频占比 0.12→0，0.30→1，再减去小调倾向
    dense  疏密：通量 0.20→0，0.60→1，叠加人声断续密度
    """
    clamp = lambda x: max(0.0, min(1.0, x))
    tempo = clamp((f["bpm"] - 60) / 100.0)
    bright = clamp((f["high"] - 0.12) / 0.18) * 0.7 + clamp(1 - f["minor_tilt"]) * 0.3
    dense = clamp((f["flux_mean"] - 0.20) / 0.40) * 0.7 + clamp(f["voiced_edges_per_s"] / 1.6) * 0.3
    return {"tempo": round(tempo, 2), "bright": round(bright, 2), "dense": round(dense, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--analysis", required=True)
    ap.add_argument("--genre", default="", help="人工曲风（如 爵士 / 流行 / 古风）")
    ap.add_argument("--json", default="", help="把结果写成 json")
    args = ap.parse_args()

    f = features(args.analysis)
    sc = score(f)

    print("== 音频特征  %s" % args.analysis)
    print("   时长 %.1fs   bpm %.1f   段落 %d" % (f["dur"], f["bpm"], len(json.load(
        open(args.analysis, encoding="utf-8")).get("sections", []))))
    print("   响度 %.3f  动态 %.3f  通量 %.3f" % (f["rms_mean"], f["rms_dyn"], f["flux_mean"]))
    print("   人声 %.2f  句长 %.2fs  断续 %.2f/s" % (f["voiced_ratio"], f["voiced_med_run"], f["voiced_edges_per_s"]))
    print("   频段 低%.3f 中%.3f 高%.3f   五声性 %.2f   小调倾向 %.2f" % (
        f["low"], f["mid"], f["high"], f["penta"], f["minor_tilt"]))

    order = sorted(sc.items(), key=lambda kv: -kv[1][1])
    print("\n== theme 得分（归一化 0~1，括号内为命中条数）")
    for th, (raw, nrm, hits) in order:
        mark = "".join("●" if ok else "○" for _, ok, _ in hits)
        nm = sum(1 for _, ok, _ in hits if ok)
        print("   %-9s %.2f (%d/%d)  %s  %s" % (th, nrm, nm, len(hits), mark, THEME_NOTE[th]))

    ax = axes(f)
    print("\n== 气质轴")
    print("   快慢 %.2f   明暗 %.2f   疏密 %.2f" % (ax["tempo"], ax["bright"], ax["dense"]))

    top, second = order[0], order[1]
    tie = abs(top[1][1] - second[1][1]) < 1e-6
    rec, reason = top[0], "音频归一化得分最高 %.2f" % top[1][1]
    if tie:
        reason += "（与 %s 并列 —— 音频层分不开，需曲风先验拍板）" % second[0]

    if args.genre:
        g = GENRE_ALIAS.get(args.genre.strip().lower(), args.genre.strip())
        if g in GENRE_THEME:
            cand, why = GENRE_THEME[g]
            # 曲风先验拍板：候选内取得分最高；若并列取表里排第一的那个
            best = max(cand, key=lambda t: (sc[t][1], -cand.index(t)))
            rec = best
            reason = "曲风「%s」→ 候选 %s（%s）；候选内最高 = %s(%.2f)" % (
                args.genre, "/".join(cand), why, best, sc[best][1])
        else:
            reason = "曲风「%s」不在表内，退回音频特征" % args.genre

    others = [t for t, _ in order if t != rec][:2]
    print("\n== 结论")
    print("   推荐 theme: %s" % rec)
    print("   理由: %s" % reason)
    print("   备选: %s" % ", ".join("%s(%.2f)" % (t, sc[t][1]) for t in others))

    if args.json:
        json.dump(dict(theme=rec, reason=reason, features=f, axes=ax,
                       scores={k: {"raw": v[0], "norm": v[1]} for k, v in sc.items()},
                       hits={k: [(n, o) for n, o, _ in v[2]] for k, v in sc.items()}),
                  open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("   已写 %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
