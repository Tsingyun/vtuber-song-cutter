# -*- coding: utf-8 -*-
"""风格弧线（style arc）—— 让「纸」跟着歌词走，而不是跟着段落跳。

问题
================================
现有机制是「段落 → 一个 pack + 一套 scheme」：段落之间**硬切**换纸。
结构上没错（观众靠换纸感知换段），但它只认**曲式**，不认**内容**：
同一段副歌里，「爱着你」与「画不出你的骨骼」在情绪上是两件事，纸上却一模一样 ——
观众读到的不是「这首歌的情绪在走」，而是「这一段一直是这个颜色」。

做法
================================
把风格拆成五个**连续**维度，而不是从五套预设里挑一套：

    lum     0 夜 ↔ 1 纸    底色明暗（决定用哪一套 scheme）
    warm    0 冷 ↔ 1 暖    色温（逐帧 tone 与 scheme 的取舍）
    dense   0 疏 ↔ 1 密    字距 / 字号 / 版心密度
    motion  0 静 ↔ 1 动    转场族与转场时长
    grain   0 净 ↔ 1 纸    颗粒强度

两个来源合成每句的目标坐标：

    意境（歌词词典）× 曲风（能量包络 + BPM）

然后**只在句边界**上用 smoothstep 过渡（lead 0.6s）。于是：
  · 句中恒定 —— 不会出现「字读到一半换了个色」；
  · 句间渐变 —— 过渡时长与转场时长同量级，肉眼读作「翻页」而不是「闪」；
  · 换纸有闸门 —— 加了滞回（margin）与最小停留（min_hold），避免两句之间来回横跳。

为什么不做逐帧全参数插值
================================
纸底是**一次性烘焙**的（见 pv_engine.js 的 paperCanvas 注释）：逐帧换纸色会把
Chromium 的抖动相位带进来，同一时刻两次渲染差 ±1~2，确定性验收直接挂。
所以分层处理：
  · 大切换（换 scheme）只发生在句边界，由转场承接；
  · 逐帧连续的只剩一层极薄的 tone（暖冷 / 明暗 / 颗粒），成本可忽略。

这也是「平滑」的正确含义：不是把两套配色糊成灰，而是**让切换发生在观众
预期它发生的位置**（句读处、音乐重音处），再给它一个过渡时长。
"""
from __future__ import unicode_literals

import base64
import io
import json
import os
import re

DIM = ("lum", "warm", "dense", "motion", "grain")

# 中性基准。**不要**取 0.5 全中：0.5 的 lum 落在深/浅底的分界上，
# 任何一点扰动都会引起换纸（滞回再大也压不住）。所以基准偏暗一档，
# 让「默认」明确站在夜这一侧，亮底只能由内容把 lum 推上去。
BASE = {"lum": 0.34, "warm": 0.44, "dense": 0.52, "motion": 0.48, "grain": 0.50}

# ----------------------------------------------------------------- 意境词典
# 每个词族给一组**相对基准**的偏移（可正可负），值不是「分数」而是「往哪边推多少」。
# 只收两类词：情绪（爱/痛/冷/誓/辽阔）与**身体感**（触摸/骨/眼/喉）。
# 身体感是文字 PV 最好用的一层 —— 「骨骼」「脸色」这类词天然要求画面更硬、更暗，
# 而「歌」「唱不完」天然要求留白。这不是文学修辞，是版面密度。
LEX = {
    "tender": ({"lum": +0.16, "warm": +0.20, "dense": -0.10, "motion": -0.12},
               ["爱", "恋", "喜欢", "心跳", "温柔", "暖", "拥抱", "想", "念",
                "吻", "甜", "微笑", "轻", "靠近", "手"]),
    "ache":   ({"lum": -0.16, "warm": -0.04, "dense": +0.18, "motion": +0.16},
               ["痛", "疼", "骨", "血", "碎", "泪", "哭", "断", "伤", "裂",
                "疤", "冷眼", "撕", "陷", "沉"]),
    "cold":   ({"lum": -0.12, "warm": -0.20, "dense": -0.04, "motion": -0.08},
               ["月", "夜", "冷", "霜", "雪", "影", "风", "灯", "寒", "孤",
                "寂", "空", "灰", "雾", "黑"]),
    "vow":    ({"lum": -0.04, "warm": +0.06, "dense": +0.14, "motion": -0.14},
               ["等", "记", "守", "执着", "永远", "一直", "约", "信", "刻",
                "画", "念着", "不忘", "承诺", "执着"]),
    "vast":   ({"lum": +0.18, "warm": +0.02, "dense": -0.16, "motion": -0.06},
               ["歌", "天", "海", "远方", "唱", "一生", "世", "长", "尽头",
                "辽阔", "飞", "云"]),
}

# 中文没有词边界，用「子串命中」而不是分词：命中的词越长权重越高（长词更具体）。
# 「执着」命中 2 次（词表里出现两次是有意的：它在《画心》副歌里是关键词，
# 重复一次等于给它双倍权重，比再写一个 weight 字段更直观）。


def _lex_score(text):
    """一句话 → {dim: 偏移, "_hits": [(族, 词, 权重)]}

    权重 = 命中词长度 / 2（2 字词 = 1.0，3 字 = 1.5，4 字 = 2.0），上限 2。
    为什么要按长度加权：中文里「爱」是万能字（爱着你/可爱/爱好），
    而「心跳」「执着」是具体词。等权会把整首歌推成同一个方向。
    """
    out = {d: 0.0 for d in DIM}
    hits = []
    for fam, (delta, words) in LEX.items():
        for w in words:
            if w and w in text:
                k = min(2.0, max(1.0, len(w) / 2.0))
                hits.append((fam, w, k))
                for d, v in delta.items():
                    out[d] += v * k
    return out, hits


# ----------------------------------------------------------------- 曲风
def _music_energy(ana, t0, t1, w0):
    """窗口内 [t0,t1] 的平均能量（0~1），从 analysis 的 bands 解出来。

    为什么不用「BPM 高就等于动」：BPM 是整首一个数，副歌和主歌一样快。
    真正区分「这段要不要压上去」的是**局部能量**（鼓组密度 + 高频量）。
    bands 是 48 频段的短时能量，正好是这件事的现成代理。

    取不到 bands 时返回 None，调用方按「无曲风项」处理 —— 不是塞一个 0.5 冒充。
    """
    b64 = ana.get("bands_b64")
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    nb = int(ana.get("nbands") or 48)
    nf = int(ana.get("nframes") or 0)
    fps = float(ana.get("fps") or 30)
    if nb <= 0 or nf <= 0 or len(raw) < nb * nf:
        return None
    i0 = max(0, int((t0 + w0) * fps))
    i1 = min(nf, int((t1 + w0) * fps))
    if i1 <= i0:
        return None
    s, n = 0, 0
    for i in range(i0, i1, 2):          # 隔帧采样：能量包络不需要 30fps 精度
        base = i * nb
        s += sum(raw[base:base + nb])
        n += nb
    if not n:
        return None
    return s / float(n) / 255.0


def _norm_energy(v, lo=0.10, hi=0.45):
    """把原始 band 均值压到 0~1。

    阈值来自《画心》全曲实测分位（p5=0.096 / p95=0.444，中位 0.335；
    副歌段 p50=0.395）。写成常数而不是按本曲 min/max 归一化：
    后者会让**同一首歌的不同窗口**各自拉伸到满量程，
    于是「拿副歌切片」与「拿整首」得到两条不同的曲线，
    同一段落在两个入口下渲染结果不一致。

    注意一个反直觉的实测结论：单看副歌这 35 秒，四句的能量是 0.414 / 0.409 /
    0.408 / 0.366 —— 几乎是平的。也就是说**在这一段里曲风项基本不起作用**，
    风格变化必须由歌词意境承担。这不是缺陷：曲风项的价值在主歌↔副歌之间
    （整首渲染时 p50 0.26 → 0.40 是一个实打实的台阶），把它当成
    「每一句都会提供信息」才会写出抖动不止的曲线。
    """
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


# ----------------------------------------------------------------- scheme 坐标
_CACHE = {}


def pack_schemes(path=None):
    """解析 pv_scheme.js → {pack: [{bg, fg, accent, lum}, ...]}

    为什么从 JS 读而不是在 Python 里复制一份色表：色值只有一处真源。
    复制一份之后，改 pv_scheme.js 忘了同步 Python，弧线会按**旧颜色**算 lum，
    选出来的 scheme 与画面实际明暗对不上 —— 而且不报错，只是「怎么调都不对」。
    """
    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(here), "renderer", "player", "pv",
                            "pv_scheme.js")
    if path in _CACHE:
        return _CACHE[path]
    packs, cur = {}, None
    # scheme 一律写成 `{ bg: '#xxxxxx', fg: '#xxxxxx', ... }` 的单行。
    pat = re.compile(r"\{\s*bg:\s*'(#[0-9A-Fa-f]{6})',\s*fg:\s*'(#[0-9A-Fa-f]{6})',"
                     r".*?accent:\s*'(#[0-9A-Fa-f]{6})'")
    for line in open(path, encoding="utf-8"):
        m = re.match(r"\s{4}(\w+):\s*\{\s*$", line)
        if m:
            cur = m.group(1)
            packs[cur] = []
            continue
        if cur is not None:
            mm = pat.search(line)
            if mm:
                bg, fg, ac = mm.groups()
                packs[cur].append({"bg": bg, "fg": fg, "accent": ac,
                                   "lum": hex_lum(bg), "flum": hex_lum(fg),
                                   "sat": hex_sat(bg)})
    _CACHE[path] = packs
    return packs


def hex_lum(h):
    """sRGB 相对亮度（0~1）。用 gamma 还原，不做「灰度加权」那种近似 ——
    我们要判断的是「这底色算深底还是浅底」，近似公式在 #7F7F7F 附近会翻边。"""
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (1, 3, 5))

    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def hex_sat(h):
    """HSV 彩度（0~1）。第二维坐标 —— 只有亮度是不够的。

    实测依据：violet 这个 pack 的五套 scheme，除「月白」外，底色亮度分别是
    0.002 / 0.003 / 0.006 / 0.014 —— 按亮度根本分不开，四套会被判成同一套。
    但它们在观感上是四张不同的纸：夜巡（近黑）、余烬（灰紫）、靛（蓝紫）、
    紫晕（中紫）。差异全在**彩度与色相**上，不在明度上。
    所以坐标必须是二维的：亮度决定「夜还是纸」，彩度决定「哪一种夜」。
    """
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    mx, mn = max(r, g, b), min(r, g, b)
    return 0.0 if mx <= 0 else (mx - mn) / mx


# ----------------------------------------------------------------- 主体
class Arc(object):
    """一条时间轴上的风格坐标曲线。"""

    def __init__(self, keys, spans, meta=None):
        self.keys = sorted(keys, key=lambda k: k["t"])
        self.spans = spans or []
        self.meta = meta or {}

    # ---- 取值
    def at(self, t):
        ks = self.keys
        if not ks:
            return dict(BASE)
        if t <= ks[0]["t"]:
            return {d: ks[0][d] for d in DIM}
        if t >= ks[-1]["t"]:
            return {d: ks[-1][d] for d in DIM}
        for i in range(len(ks) - 1):
            a, b = ks[i], ks[i + 1]
            if a["t"] <= t < b["t"]:
                q = (t - a["t"]) / max(1e-6, b["t"] - a["t"])
                # smoothstep：两端导数为 0。线性插值在句边界会有「折角」，
                # 画面读作「色温突然开始变」，而 smoothstep 读作「慢慢过去」。
                q = q * q * (3 - 2 * q)
                return {d: round(a[d] + (b[d] - a[d]) * q, 4) for d in DIM}
        return {d: ks[-1][d] for d in DIM}

    # ---- 换纸（二维坐标 + 滞回 + 最小停留）
    def pick(self, pack, t, schemes, cur=None, margin=0.040, min_hold=6.0,
             last_switch=None):
        """按 (亮度, 彩度) 选一套 scheme，返回 (idx, mix, why)。

        为什么必须是二维：violet 的四套深底亮度是 0.002 / 0.003 / 0.006 / 0.014，
        只按亮度选，四句会被压到同一套纸 ——「按内容换风格」就等于没发生。
        它们真正的差别在彩度（近黑 / 灰紫 / 蓝紫 / 中紫）。

        mix 恒为 None：这一版不做调色板混合。不是偷懒 —— 混合会把两套纸的差异
        抹平，而「换纸」正是观众用来感知段落的东西；抹平之后得到的是「一直在
        微妙地变色」，不是「翻过一页」。平滑改由另外三件事承担：句边界过渡曲线、
        转场族与时长、逐帧色温层（pv_arc.js 的 drawArcTone）。

        滞回 + 最小停留：没有这两条，句间坐标的微差会让纸来回横跳。
        """
        if not schemes:
            return 0, None, "no-scheme"
        v = self.at(t)
        le, we = float(v["lum"]), float(v["warm"])

        def dist(sc):
            # 亮度满量程按 0.90 折算（纸白 0.92 ↔ 夜黑 0.00），彩度按 1.0。
            # 权重 0.72 / 0.28：亮度是主判据（「夜」与「纸」是两种东西），
            # 彩度是次级（同一种夜里挑哪一张）。彩度权重再高，浅底与深底
            # 就会被判成近邻，跨侧大跳变成小碎步。
            return 0.72 * min(1.0, abs(sc["lum"] - le) / 0.90) + 0.28 * abs(sc["sat"] - we)

        best = min(range(len(schemes)), key=lambda i: dist(schemes[i]))
        if cur is not None and best != cur and 0 <= cur < len(schemes):
            dc, dn = dist(schemes[cur]), dist(schemes[best])
            if dc - dn < margin:
                best = cur
            elif last_switch is not None and (t - last_switch) < min_hold and dc < 0.45:
                best = cur
        return best, None, "lum %.2f sat %.2f → #%d" % (le, we, best)


def build(lines, ana, w0, dur, w_lyric=0.62, w_music=0.38, lead=0.60):
    """歌词 + 曲风 → Arc。

    w_lyric / w_music 是两者的权重。给 6:4 而不是 5:5：
    意境是「这句在说什么」，是可解释的；能量只是「这里响不响」。
    能量占太多会把整条曲线拉成一条跟音量包络一样的起伏，所有歌都长一个样。
    """
    spans, keys = [], []
    prev = dict(BASE)
    bpm = float(ana.get("bpm") or 0)
    # BPM 基准：120 为中性。快歌整体更「动」，但只推 ±0.10 ——
    # 让「同一首歌里哪句更动」仍然是歌词说了算。
    bpm_bias = max(-0.10, min(0.10, (bpm - 120.0) / 200.0)) if bpm else 0.0

    for l in lines:
        t0, t1 = float(l["t0"]), float(l["t1"])
        text = l.get("text", "")
        sc, hits = _lex_score(text)
        e = _music_energy(ana, t0, t1, w0)
        ev = _norm_energy(e) if e is not None else None

        tgt = {}
        for d in DIM:
            lv = sc[d]
            # 意境偏移最多 ±0.30：词典是「倾向」，不是「指令」。
            lv = max(-0.30, min(0.30, lv))
            v = BASE[d] + lv
            if ev is not None:
                if d == "motion":
                    v += (ev - 0.45) * 0.22 * w_music / 0.38
                elif d == "dense":
                    v += (ev - 0.45) * 0.12 * w_music / 0.38
                elif d == "lum":
                    v += (ev - 0.45) * 0.05
            if d == "motion":
                v += bpm_bias
            tgt[d] = round(max(0.0, min(1.0, v)), 4)

        # 关键帧：句边界前 lead 秒开始过渡，句首到达目标。
        # lead 与转场时长（0.4~0.6s）同量级 —— 观众看到的是「翻页时色温已经在变了」，
        # 而不是「翻完页色温才开始变」。
        t0k = max(0.0, t0 - lead)
        if not keys:
            if t0k > 0.02:
                # 第一句前面还有前奏：先钉基准，再在 t0k 起过渡。
                keys.append(dict(t=0.0, **BASE, why="基准"))
            else:
                # 第一句就顶着 0.0（副歌切片、或整首第一句）：直接以目标开局。
                # 此时再加一条 BASE 会造成两个 t=0 的关键帧 —— at() 会取到基准那条，
                # 于是**选纸用的是中性值**，第一句永远落到中间那套纸，而且不报错。
                keys.append(dict(t=0.0, **tgt, why=_why(hits, ev)))
                prev = tgt
                spans.append(_span(l, tgt, hits, ev, t0, t1))
                continue
        # 上一句的值先钉在过渡起点，再在新句起点钉目标值
        if t0k > keys[-1]["t"] + 0.02:
            keys.append(dict(t=round(t0k, 3), **prev, why="保持"))
        keys.append(dict(t=round(t0, 3), **tgt, why=_why(hits, ev)))
        prev = tgt

        spans.append(_span(l, tgt, hits, ev, t0, t1))

    # 收尾：最后一句保持到片尾，不再漂（片尾漂移会被读成「渲染坏了」）
    if keys:
        keys.append(dict(t=round(dur, 3), **{d: keys[-1][d] for d in DIM},
                         why="收尾"))
    return Arc(keys, spans, {
        "bpm": bpm, "w_lyric": w_lyric, "w_music": w_music, "lead": lead,
        "base": dict(BASE),
    })


def _span(l, tgt, hits, ev, t0, t1):
    return {
        "line": l.get("i"), "t0": round(t0, 3), "t1": round(t1, 3),
        "text": l.get("text", ""), "target": tgt,
        "hits": [{"fam": f, "w": w, "k": k} for f, w, k in hits],
        "energy": None if ev is None else round(ev, 3),
        "why": _why(hits, ev),
    }


def _why(hits, ev):
    if hits:
        fams = {}
        for f, _w, k in hits:
            fams[f] = fams.get(f, 0) + k
        s = "/".join("%s×%.1f" % (f, v) for f, v in
                     sorted(fams.items(), key=lambda kv: -kv[1]))
    else:
        s = "无词族命中"
    if ev is not None:
        s += "  e=%.2f" % ev
    return s


# ----------------------------------------------------------------- 参数映射
def type_params(v):
    """dense → 字距。0 疏 ↔ 1 密 对应 0.005~0.075em。

    上限 0.075 而不是更宽：中文正文超过 0.08em 会读成「字被拆开」，
    那是「间距没调好」，不是「密」。下限 0.005 而不是负数：
    负字距（挤）在粗黑中文字族上会压住笔画，只有标题级的大字才受得住。
    """
    return {"track": round(0.005 + 0.070 * float(v["dense"]), 4)}


def trans_params(v):
    """motion → 转场时长与偏好族。

    静的时候用 0.55s 的柔过渡（dissolve/grad），动的时候 0.30s 的硬过渡。
    时长差近一倍，但**都保留过渡**：用户要的是「自然平滑」，
    再强的情绪也不该在这一版里回到硬切（硬切是段落级的手法，不是句级的）。
    """
    m = float(v["motion"])
    return {"dur": round(0.55 - 0.25 * m, 3),
            "soft": m < 0.42, "hard": m > 0.66}


def tone_params(v):
    """逐帧 tone 的幅度（引擎侧用）。全部乘在一个小系数上 —— 这一层是「胶水」，
    不是「滤镜」。超过 0.10 的叠加就会开始吃掉字与底的分离度。"""
    return {"warm": round((float(v["warm"]) - 0.5) * 2.0, 3),   # -1 冷 ↔ +1 暖
            "lum": round((float(v["lum"]) - 0.5) * 2.0, 3),     # -1 暗 ↔ +1 亮
            "grain": round(float(v["grain"]), 3)}


def dump(arc, path):
    d = {"version": 1, "dim": list(DIM), "meta": arc.meta,
         "keys": arc.keys, "spans": arc.spans}
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    return path


def describe(arc, schemes_by_pack=None):
    """给人看的表：每句的意境命中 → 坐标 → 落地到哪套纸。"""
    out = []
    for s in arc.spans:
        t = s["target"]
        row = "  %-22s lum %.2f warm %.2f dense %.2f motion %.2f  %s" % (
            (s["text"][:20] + "…") if len(s["text"]) > 21 else s["text"],
            t["lum"], t["warm"], t["dense"], t["motion"], s["why"])
        out.append(row)
    return "\n".join(out)
