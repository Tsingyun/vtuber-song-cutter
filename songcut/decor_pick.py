# -*- coding: utf-8 -*-
"""
装饰方案自动匹配（歌切主路径）

用途：根据「歌曲意境」自动挑选最合适的标题装饰方案，而不是固定或随机使用。

输入信号（四个可解释维度，均为 0~1）：
  energy  节奏强度  —— 音频 onset 强度 / RMS 动态 / BPM
  valence 情绪明暗  —— 音频频谱质心（明亮度） + 歌词正负向情绪词
  warmth  冷暖色调  —— 封面主色色相 + 歌词冷暖意象词
  ornate  华丽程度  —— 封面饱和度 + 歌词古风/繁复意象词

决策：每套方案有一个「目标画像」（四维权重向量），取加权欧氏距离最小者；
      歌词关键词命中会给对应方案额外加权（可解释为「命中了哪些词」）。
      全部决策过程输出为可解释的 reason 文本与 scores 明细，落盘到 manifest。

手动覆盖：--scheme N（0~4）或 pick(..., force=N)，覆盖时仍记录自动选择结果供对比。
"""
import io
import os
import re
import sys
import math

# 与 renderer/player/index.html 的 TS_STYLES 保持同序号同语义
SCHEMES = [
    {"id": 0, "name": "流光渐变", "desc": "主色→高光→辅色缓慢流动，华丽温暖",
     "profile": {"energy": 0.55, "valence": 0.70, "warmth": 0.75, "ornate": 0.80}},
    {"id": 1, "name": "描边镂空", "desc": "冰白描边 + 冷调外光，清冷空灵",
     "profile": {"energy": 0.30, "valence": 0.42, "warmth": 0.22, "ornate": 0.18}},
    {"id": 2, "name": "霓虹柔光", "desc": "白字 + 主辅色呼吸光晕，都市律动",
     "profile": {"energy": 0.78, "valence": 0.55, "warmth": 0.45, "ornate": 0.55}},
    {"id": 3, "name": "色块高亮", "desc": "主→辅渐变色块衬底，明快流行",
     "profile": {"energy": 0.66, "valence": 0.82, "warmth": 0.66, "ornate": 0.45}},
    {"id": 4, "name": "双色错位", "desc": "主辅色轻微错位叠影，动感潮流",
     "profile": {"energy": 0.88, "valence": 0.60, "warmth": 0.38, "ornate": 0.62}},
]

# 关键词 → 方案加权（命中即给该方案减分，等价于加分）
KEYWORDS = {
    0: ("明月", "中秋", "月圆", "灯", "华", "锦", "繁花", "烟花", "锦绣", "团圆",
        "盛", "宴", "金", "辉煌", "璀璨", "流光", "溢彩"),
    1: ("月", "夜", "霜", "雪", "寒", "孤", "静", "空", "远", "清", "幽", "露",
        "影", "寂", "冷", "星", "云", "轻", "淡", "水墨", "秋风", "晓"),
    2: ("霓虹", "电", "光", "闪", "都市", "城市", "街", "节奏", "律动", "脉冲",
        "信号", "频率", "电子", "合成", "赛博", "未来", "spark", "glow"),
    3: ("甜", "糖", "恋", "喜欢", "心", "笑", "可爱", "元气", "happy", "love",
        "阳光", "晴", "暖", "春", "花", "拥抱", "陪"),
    4: ("燃", "冲", "战", "热", "飞", "奔", "狂", "爆", "炸", "燃焼", "燃烧",
        "极限", "速度", "逆", "破", "浪", "烈", "抖", "shake"),
}

# 情绪/意象词（影响 valence / warmth）
POS_WORDS = ("快乐", "欢喜", "笑", "阳光", "甜", "希望", "晴", "暖", "爱", "喜欢",
             "开心", "明亮", "灿烂", "拥抱", "梦", "愿", "春", "花")
NEG_WORDS = ("孤", "寂", "泪", "别", "离", "寒", "霜", "夜", "残", "空", "远",
             "愁", "思念", "旧", "沉默", "碎", "冷", "独")
WARM_WORDS = ("暖", "阳", "光", "火", "红", "金", "夏", "春", "灯", "烛", "热")
COOL_WORDS = ("雪", "霜", "冰", "寒", "夜", "月", "蓝", "海", "雨", "露", "静", "秋")


def _clamp01(v):
    return 0.0 if v < 0 else (1.0 if v > 1 else float(v))


# ---------------------------------------------------------------- 音频信号
def audio_signals(path, sr=22050, max_sec=120):
    """从音频提取 energy / valence(明亮度) / tempo。失败返回 None（降级）。"""
    try:
        import numpy as np
        import librosa
    except Exception:
        return None
    try:
        y, sr = librosa.load(path, sr=sr, mono=True, duration=max_sec)
    except Exception:
        return None
    if y.size < sr // 2:
        return None
    try:
        import numpy as np
        dur = y.size / float(sr)
        rms = float(np.mean(librosa.feature.rms(y=y)))
        onset = librosa.onset.onset_strength(y=y, sr=sr)
        on_mean = float(np.mean(onset))
        # 节奏强度：BPM 为主、onset 密度为辅。
        # 不用 onset 绝对强度——直播音频经过压缩，绝对值普遍虚高、失去区分度。
        tempo = 0.0
        try:
            t, _ = librosa.beat.beat_track(y=y, sr=sr)
            tempo = float(np.atleast_1d(t)[0])
        except Exception:
            tempo = 0.0
        # BPM 映射：60→0.12，180→0.95（超出钳制）
        if tempo <= 0:
            tempo_norm = 0.5
        else:
            tempo_norm = _clamp01(0.12 + (min(tempo, 190.0) - 60.0) / 120.0 * 0.83)
        try:
            peaks = librosa.onset.onset_detect(y=y, sr=sr, units="time")
            dens = len(peaks) / max(1.0, dur)          # 每秒 onset 数
        except Exception:
            dens = 1.5
        dens_norm = _clamp01((dens - 0.6) / 4.4)       # 0.6→0，5.0→1
        energy = _clamp01(tempo_norm * 0.62 + dens_norm * 0.38)
        # 明亮度：频谱质心相对奈奎斯特
        cen = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)))
        bright = _clamp01(cen / (sr * 0.22))
        return {"energy": round(energy, 3), "bright": round(bright, 3),
                "rms": round(rms, 4), "tempo": round(tempo, 1),
                "onset_density": round(dens, 2)}
    except Exception:
        return None


# ---------------------------------------------------------------- 封面信号
def cover_signals(path):
    """封面主色 → warmth / chroma。失败返回 None。"""
    try:
        from PIL import Image
        import numpy as np
    except Exception:
        return None
    try:
        im = Image.open(path).convert("RGBA")
        im.thumbnail((64, 64))
        a = np.asarray(im).astype(float)
        alpha = a[:, :, 3]
        rgb = a[:, :, :3][alpha > 125]
        if rgb.shape[0] < 32:
            return None
        # 中位切分（简化：按亮度分 6 簇，取权重×饱和度最高者）
        lum = rgb.mean(axis=1)
        order = np.argsort(lum)
        k = 6
        chunks = np.array_split(order, k)
        best, bs = rgb.mean(axis=0), -1.0
        for ch in chunks:
            if ch.size == 0:
                continue
            c = rgb[ch].mean(axis=0)
            mx, mn = c.max(), c.min()
            s = 0.0 if mx <= 0 else (mx - mn) / (mx + mn) if (mx + mn) < 255 else (mx - mn) / (510 - mx - mn)
            sc = (ch.size / rgb.shape[0]) * (0.35 + min(s, 0.9) * 0.9)
            if sc > bs:
                bs, best = sc, c
        r, g, b = [float(v) / 255 for v in best]
        mx, mn = max(r, g, b), min(r, g, b)
        l = (mx + mn) / 2
        s = 0.0 if mx == mn else ((mx - mn) / (mx + mn) if l < 0.5 else (mx - mn) / (2 - mx - mn))
        if mx == r:
            h = ((g - b) / (mx - mn) + (6 if g < b else 0)) / 6
        elif mx == g:
            h = ((b - r) / (mx - mn) + 2) / 6
        else:
            h = ((r - g) / (mx - mn) + 4) / 6
        # 暖色：色相靠近红/橙/黄（h≈0~0.17 或 0.9~1）
        warm_h = 1.0 - min(abs(h - 0.08), abs(h - 1.0) if h > 0.5 else abs(h + 0.02)) / 0.30
        return {"warmth": round(_clamp01(warm_h), 3), "chroma": round(_clamp01(s), 3),
                "light": round(_clamp01(l), 3), "hue": round(h, 3)}
    except Exception:
        return None


# ---------------------------------------------------------------- 文本信号
def text_signals(title="", artist="", lrc=""):
    """歌词/歌名 → 情绪明暗、冷暖意象、华丽程度、关键词命中。"""
    txt = "%s %s %s" % (title or "", artist or "", lrc or "")
    txt = txt[:20000]
    hits = {}
    for sid, words in KEYWORDS.items():
        got = [w for w in words if w and w in txt]
        if got:
            hits[sid] = got[:6]
    n_pos = sum(txt.count(w) for w in POS_WORDS)
    n_neg = sum(txt.count(w) for w in NEG_WORDS)
    n_warm = sum(txt.count(w) for w in WARM_WORDS)
    n_cool = sum(txt.count(w) for w in COOL_WORDS)
    denom = max(1, n_pos + n_neg)
    valence_t = 0.5 + 0.5 * (n_pos - n_neg) / denom          # 0=偏忧伤 1=偏明亮
    denom2 = max(1, n_warm + n_cool)
    warmth_t = 0.5 + 0.5 * (n_warm - n_cool) / denom2
    ornate_t = _clamp01(sum(len(v) for v in hits.values()) / 12.0)
    return {"valence": round(valence_t, 3), "warmth": round(warmth_t, 3),
            "ornate": round(ornate_t, 3), "hits": hits,
            "n_pos": n_pos, "n_neg": n_neg, "n_warm": n_warm, "n_cool": n_cool}


# ---------------------------------------------------------------- 决策
def pick(title="", artist="", lrc="", cover=None, audio=None, force=None):
    """返回 {scheme, name, reason, scores, signals, auto, forced}"""
    ts = text_signals(title, artist, lrc)
    au = audio_signals(audio) if audio and os.path.exists(audio) else None
    cv = cover_signals(cover) if cover and os.path.exists(cover) else None

    # 融合四维权重（缺信号时用文本兜底 + 中性默认）
    energy = au["energy"] if au else 0.5
    # valence：音频明亮度与歌词情绪各半
    valence = (au["bright"] * 0.5 + ts["valence"] * 0.5) if au else ts["valence"]
    # warmth：封面色相与歌词意象各半
    warmth = (cv["warmth"] * 0.5 + ts["warmth"] * 0.5) if cv else ts["warmth"]
    # ornate：封面饱和度与歌词华丽词各半
    ornate = (cv["chroma"] * 0.5 + ts["ornate"] * 0.5) if cv else ts["ornate"]
    sig = {"energy": round(_clamp01(energy), 3), "valence": round(_clamp01(valence), 3),
           "warmth": round(_clamp01(warmth), 3), "ornate": round(_clamp01(ornate), 3)}
    W = {"energy": 1.0, "valence": 1.0, "warmth": 0.85, "ornate": 0.9}

    scores = {}
    for sc in SCHEMES:
        p = sc["profile"]
        d = math.sqrt(sum(W[k] * (sig[k] - p[k]) ** 2 for k in W))
        bonus = 0.0
        got = ts["hits"].get(sc["id"])
        if got:
            # 双字及以上意象词更具指向性；单字词（如「月」「夜」）权重减半，避免误命中
            w = sum(0.10 if len(x) >= 2 else 0.045 for x in got)
            bonus = min(0.34, w)                # 命中关键词 → 距离减小（更易胜出）
        scores[sc["id"]] = round(d - bonus, 4)

    auto = min(scores, key=lambda k: scores[k])
    chosen = auto if force is None else int(force) % len(SCHEMES)
    ch = SCHEMES[chosen]

    bits = []
    bits.append("节奏%s(%.2f)/情绪%s(%.2f)/色调%s(%.2f)/华丽%s(%.2f)" % (
        "强" if sig["energy"] >= 0.6 else ("中" if sig["energy"] >= 0.4 else "弱"), sig["energy"],
        "明亮" if sig["valence"] >= 0.6 else ("平和" if sig["valence"] >= 0.4 else "幽婉"), sig["valence"],
        "暖" if sig["warmth"] >= 0.6 else ("中" if sig["warmth"] >= 0.4 else "冷"), sig["warmth"],
        "高" if sig["ornate"] >= 0.6 else ("中" if sig["ornate"] >= 0.35 else "低"), sig["ornate"]))
    src = []
    if au:
        src.append("音频(BPM≈%s)" % au.get("tempo"))
    if cv:
        src.append("封面取色")
    src.append("歌词意象")
    bits.append("信号源：" + "、".join(src))
    if ts["hits"].get(chosen):
        bits.append("命中意象词：" + "、".join(ts["hits"][chosen]))
    if not au and not cv:
        bits.append("（音频/封面缺失，仅凭歌词判断）")
    if force is not None and force != auto:
        bits.append("手动覆盖：自动判定为「%s」" % SCHEMES[auto]["name"])

    return {"scheme": chosen, "name": ch["name"], "desc": ch["desc"],
            "reason": "；".join(bits), "scores": scores, "signals": sig,
            "audio": au, "cover": cv, "text": {k: v for k, v in ts.items() if k != "hits"},
            "hits": ts["hits"], "auto": auto, "forced": force is not None}


def describe(res):
    lines = ["装饰方案：%d %s（%s）" % (res["scheme"], res["name"], res["desc"]),
             "  理由：" + res["reason"],
             "  各方案距离：" + "  ".join(
                 "%s=%.3f" % (SCHEMES[k]["name"], v) for k, v in sorted(res["scores"].items()))]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser(description="装饰方案自动匹配")
    ap.add_argument("--title", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--lrc-file", default="")
    ap.add_argument("--cover", default="")
    ap.add_argument("--audio", default="")
    ap.add_argument("--scheme", type=int, default=None, help="手动覆盖 0~4")
    args = ap.parse_args()
    lrc = ""
    if args.lrc_file and os.path.exists(args.lrc_file):
        lrc = io.open(args.lrc_file, encoding="utf-8", errors="ignore").read()
    r = pick(args.title, args.artist, lrc, args.cover or None, args.audio or None, args.scheme)
    print(describe(r))
    print(json.dumps({k: r[k] for k in ("scheme", "name", "signals", "auto")},
                     ensure_ascii=False))
