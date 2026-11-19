# -*- coding: utf-8 -*-
"""风格层：把一份 VSP（视觉风格档案）读成编排层能用的**约束**。

设计边界（很重要，别越界）
------------------------------------------------
VSP 是「约束层」，不是「模板」。它只允许表达三件事：

1. **取值范围**（字号阶梯、字距、行距、人物占比目标、镜头速度）
2. **候选池与权重**（版面权重、转场族、纸包排序）
3. **布尔倾向**（无框人物、衬线）

它**不允许**逐镜指定「第 12 镜用 keySplit、字放在 (x,y)」。一旦允许，同一份档案
重渲两次会得到同一支片 —— 那不是「学会了风格」，那是「把一支片复刻了一遍」，
而且下次换歌就完全不适用。

因此本模块只做四件事：读、校验、裁剪到合法域、归一化成「权重」。
怎么用这些值，由 `pv_direct.py` 的抽卡机制决定；同一份 VSP 重渲仍然出新构图。

为什么不用 pydantic / jsonschema
------------------------------------------------
只有一个消费方、字段不到 20 个，多引一个依赖就多一处版本风险（项目规范：不随意
增加技术栈）。而且这里需要的不是「结构校验」，是「取值范围裁剪 + 明确告警」——
schema 库做不了后者，还是会自己写一遍。
"""
from __future__ import unicode_literals

import io
import json
import os

# 每个数值旋钮的 (下限, 上限, 说明)。越界不会拒收，而是夹紧并告警：
# 手改档案是正常用法，硬失败只会让人放弃手改。
RANGES = {
    "trans_dur": (0.20, 1.60, "转场时长（秒）"),
    "type.size_ladder": (0.70, 2.00, "字号阶梯倍率"),
    "type.tracking": (0.00, 0.40, "字距（em）"),
    "type.leading": (1.05, 2.20, "行距倍率"),
    "type.accent_word_ratio": (0.00, 0.60, "强调词占比"),
    "texture.grain": (0.00, 1.00, "颗粒"),
    "texture.halftone": (0.00, 1.00, "网点"),
    "texture.scanline": (0.00, 1.00, "扫描线"),
    "texture.rgb_split": (0.00, 1.00, "色散"),
    "texture.bloom": (0.00, 1.00, "泛光"),
    "figure.presence": (0.00, 1.00, "人物出镜率"),
    "figure.coverage_target": (0.05, 0.85, "人物占比目标"),
    "frame.letterbox": (0.00, 0.45, "上下黑边占比"),
    "motion.cam_speed": (0.00, 1.00, "镜头运动强度"),
    "motion.churn": (0.00, 1.00, "画面变化率"),
}

BIAS = ("left", "center", "right")


def _get(d, dotted, default=None):
    cur = d
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _num(d, dotted, warn):
    """取数值并夹到合法域。缺字段返回 None（不填占位值）。"""
    v = _get(d, dotted)
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        warn("%s 不是数字（%r），忽略" % (dotted, v))
        return None
    lo_hi = RANGES.get(dotted)
    if not lo_hi:
        # 没登记边界 = 不需要夹（例如「目标镜长」，它只是参考值，超了也不会有害）。
        # 这里不做静默兜底警告：RANGES 是「有意义的边界清单」，不是字段白名单。
        return round(v, 4)
    lo, hi, desc = lo_hi
    if v < lo or v > hi:
        c = min(hi, max(lo, v))
        warn("%s=%.3f 超出 [%.2f, %.2f]（%s），已夹到 %.3f" % (dotted, v, lo, hi, desc, c))
        v = c
    return round(v, 4)


def load(path, valid_layouts=None, valid_trans=None, valid_packs=None, strict=False):
    """读一份 VSP，返回规范化后的约束字典。读不到就抛，不要静默返回空。"""
    if not os.path.exists(path):
        raise SystemExit("[style] 找不到风格档案：%s" % path)
    try:
        raw = json.load(io.open(path, encoding="utf8"))
    except ValueError as e:
        raise SystemExit("[style] %s 不是合法 JSON：%s" % (path, e))
    eng = raw.get("engine")
    if not isinstance(eng, dict):
        raise SystemExit("[style] %s 里没有 engine 段 —— 这不是给编排层用的档案"
                         "（旧版档案请先跑 tools/pv_ref.py restyle）" % path)

    warn_list = []

    def warn(msg):
        warn_list.append(msg)

    known = {"pack", "pack_rank", "pack_proposal", "layouts", "trans_pool", "trans_dur",
             "type", "texture", "figure", "frame", "motion"}
    unknown = sorted(set(eng) - known)
    if unknown:
        warn("engine 里有本版不认识的键：%s（忽略）" % ", ".join(unknown))
    if strict and unknown:
        raise SystemExit("[style] --style-strict：不认识的键 %s" % ", ".join(unknown))

    # ---- 纸包：主包 + 备选（按距离排序）。备选只用于「段落之间换纸」，
    #      不用于「同一段里乱跳」—— 一段一纸是这个项目已经验证过的节奏。
    primary = eng.get("pack") or ""
    rank = []
    for it in (eng.get("pack_rank") or []):
        if isinstance(it, dict) and it.get("pack"):
            rank.append(it["pack"])
    if primary and primary not in rank:
        rank.insert(0, primary)
    if valid_packs:
        bad = [p for p in rank if p not in valid_packs]
        if bad:
            # 最常见的真实情况：档案建议新建 violet，但 pv_scheme.js 里还没写。
            # 这时**必须明确报错**，不能静默落到 noir —— 否则片子颜色会悄悄错掉。
            raise SystemExit(
                "[style] 档案要求的纸包 %s 不在 pv_scheme.js 里（现有：%s）。\n"
                "        两个选择：① 把该包写进 renderer/player/pv/pv_scheme.js；\n"
                "        ② 用 --style-pack 指定一个现有包。"
                % (", ".join(bad), ", ".join(valid_packs)))
    packs = rank[:3] if rank else []

    # ---- 版面权重：只保留本引擎真有的版面名。不存在的一律丢弃并告警
    #      （档案里的 "center"/"titleCard" 是量测口径，不一定进了抽卡池）。
    lw = {}
    for k, v in (eng.get("layouts") or {}).items():
        if valid_layouts and k not in valid_layouts:
            warn("版面 `%s` 不在引擎的版面表里，已丢弃" % k)
            continue
        try:
            w = float(v)
        except (TypeError, ValueError):
            warn("版面 %s 权重不是数字（%r），已丢弃" % (k, v))
            continue
        if w <= 0:
            continue
        lw[k] = round(min(3.0, max(0.3, w)), 3)

    # ---- 转场族：非法名字丢弃
    tp = []
    for k in (eng.get("trans_pool") or []):
        if valid_trans and k not in valid_trans:
            warn("转场 `%s` 不存在，已丢弃" % k)
            continue
        tp.append(k)

    out = {
        "path": os.path.abspath(path),
        "id": raw.get("id") or os.path.splitext(os.path.basename(path))[0],
        "packs": packs,
        "layouts": lw,
        "trans_pool": tp,
        "trans_dur": _num(eng, "trans_dur", warn),
        "type": {k: _num(eng, "type." + k, warn)
                 for k in ("size_ladder", "tracking", "leading", "accent_word_ratio")},
        "texture": {k: _num(eng, "texture." + k, warn)
                    for k in ("grain", "halftone", "scanline", "rgb_split", "bloom")},
        "figure": {
            "presence": _num(eng, "figure.presence", warn),
            "coverage_target": _num(eng, "figure.coverage_target", warn),
            "bias": _get(eng, "figure.bias"),
            "frameless_hint": bool(_get(eng, "figure.frameless_hint", False)),
            "confidence": _get(eng, "figure.confidence"),
        },
        "frame": {"letterbox": _num(eng, "frame.letterbox", warn),
                  "credit_bar": bool(_get(eng, "frame.credit_bar", False))},
        "motion": {k: _num(eng, "motion." + k, warn)
                   for k in ("cam_speed", "churn", "shot_len_target")},
        "warnings": warn_list,
    }
    if out["figure"]["bias"] not in BIAS:
        if out["figure"]["bias"] is not None:
            warn("figure.bias=%r 不合法（应为 %s），忽略" % (out["figure"]["bias"], "/".join(BIAS)))
        out["figure"]["bias"] = None
    if not out["packs"]:
        raise SystemExit("[style] %s 的 engine 段没有 pack 信息" % path)
    # 缺项一律留 None：下游用「有没有值」决定要不要接管，而不是拿 0 当有效值。
    return out


def describe(st):
    """一行摘要，给命令行打印用。"""
    if not st:
        return "（未启用）"
    ty = st["type"]
    fg = st["figure"]
    bits = ["pack=%s" % "/".join(st["packs"]),
            "转场=%s(%.2fs)" % (",".join(st["trans_pool"][:3]) or "-", st["trans_dur"] or 0),
            "字号×%.2f" % (ty["size_ladder"] or 1.0)]
    if ty.get("tracking") is not None:
        bits.append("字距%.3f" % ty["tracking"])
    if fg.get("presence") is not None:
        bits.append("人物%.0f%%" % (fg["presence"] * 100))
    if fg.get("frameless_hint"):
        bits.append("无框")
    return " · ".join(bits)
