#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pv_to_z.py —— 把我们的切点翻译成 Vocaloid-Style MV Pipeline 的编排稿。

    和上一版的区别：**不再自己写 JS**。上一版用一份简化模板重写了 setup（配色 / LOOKS /
    post / HUD 全是手写），结果上游八个场景只用上三个、自定义 fx 一个没用、JIZURA 只挂两个
    plan —— 等于借它的引擎跑我们的片子。

    这一版直接拿上游 template 的 `engine/timeline.js` 当母版：
      保留   helpers（stars / ridge / starTrails / firstLight / swarmGlyph）、DENY 策展、
             JIZURA 四个 plan（main / cf / noir / mono）、LOOKS、globalFX、HUD、竖排配置
      替换   只有两处：镜头表（shots）与片名 / 配色脚本（film identity / colour script）
    所以画面上除了「切在哪、唱什么、谁站在哪」之外，没有一样是我们定的。

用法：
    python tools/pv_to_z.py --job _tmp/mvg/art_out/_work --dst _tmp/mvg/zrun
    python tools/pv_to_z.py --job ... --dst ... --shots 0:40      # 只翻译一个区间
    python tools/pv_to_z.py --job ... --dst ... --art 100@repost   # 换立绘（逗号分隔）

产出（全部落在 --dst）：
    engine/timeline.js        编排稿（上游母版 + 我们的镜头表）
    analysis/lyrics_mv.lrc    唱词（JIZURA 的时间轴来源）
    assets/char/*.png         立绘（从 songcut.figart 素材库复制，只读不改原图）
    analysis/z_link.json      映射报告（镜数、场景分布、立绘镜数）

纪律：只写 --dst 里的四个东西，不碰 _ref/ 下任何文件（上游 clone 保持只读）。
"""
import argparse
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

REF_TL = os.path.join(ROOT, "_ref", "vocaloid-style-mv-pipeline", "skills",
                      "vocaloid-style-mv", "template", "engine", "timeline.js")

# ------------------------------------------------------------------ 配色脚本
# 上游 timeline.js 里这段叫 "colour script of the demo (one palette, kept everywhere)"：
# 一部片子一份，字段名（void / ink / shadow / dusk / verm / sunset / glow / hi / core）是场景与
# JIZURA 共用的接口。这里只给这九个值：小丑的红与金，底子是深夜的紫黑。
# 配色怎么用、铺在哪、跟哪个 plan 走，全在上游手里。
FF = {
    "void": "#05030A", "ink": "#120A14", "shadow": "#2A1220", "dusk": "#5A1A28",
    "verm": "#C8373A", "sunset": "#FF6B5A", "glow": "#FFB067", "hi": "#FFE8C8",
    "core": "#FFFFFF",
}
NIGHT = "[[0, '#05030a'], [0.5, '#0e0716'], [0.85, '#1d0d1e'], [1, '#2a1226']]"

# ------------------------------------------------------------------ 立绘选角
# 用户指定的三张（同一次投喂的立绘组，760x1000 半身像）。按段落情绪分工：
#   calm  100  平静注视     —— 主歌 / 安静段，字压在她胸口最稳
#   smile 102  开口笑       —— 导句，抬气氛
#   cool  105  抱臂歪头     —— 副歌，姿态收得紧，压字最狠
ART_CAST = [
    ("calm", "1211081868701597696_100@repost.png"),
    ("smile", "1211081868701597696_102@repost.png"),
    ("cool", "1211081868701597696_105@repost.png"),
]
MOOD = {"verse": "calm", "intro": "calm", "pre-chorus": "smile", "chorus": "cool"}


# ------------------------------------------------------------------ JS 序列化
class Raw(str):
    """原样写进 JS 的片段：函数、调用、上游常量名（stars(...) / NIGHT / CHAR.calm）。"""


def js(v, ind=0):
    """dict/list/str/num/bool/None → JS 字面量。dict 按插入顺序，所以读起来像手写的。"""
    pad = "  " * ind
    if isinstance(v, Raw):
        return str(v)
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return ("%.3f" % v).rstrip("0").rstrip(".")
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        if not v:
            return "[]"
        inner = ", ".join(js(x, ind + 1) for x in v)
        if len(inner) + len(pad) < 100:
            return "[" + inner + "]"
        return "[\n" + "".join("  " * (ind + 1) + js(x, ind + 1) + ",\n" for x in v) + pad + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        parts = []
        for k, x in v.items():
            key = k if re.match(r"^[A-Za-z_$][\w$]*$", str(k)) else json.dumps(k, ensure_ascii=False)
            parts.append("%s: %s" % (key, js(x, ind + 1)))
        inner = ", ".join(parts)
        if len(inner) + len(pad) < 118:
            return "{" + inner + "}"
        return "{\n" + "".join("  " * (ind + 1) + p + ",\n" for p in parts) + pad + "}"
    raise TypeError("不能序列化：%r" % (v,))


# ------------------------------------------------------------------ 上游母版
def load_base():
    """读上游 timeline.js，切成 head / tail 两段（中间的 demo 镜头表整段丢弃）。"""
    if not os.path.exists(REF_TL):
        raise SystemExit("[z] 找不到上游母版：%s\n"
                         "    _ref/ 是上游 clone（RULES「_ref 只读」），删了要重新 clone。" % REF_TL)
    with open(REF_TL, encoding="utf-8") as f:
        txt = f.read()
    # 从「shots 段的分节标题」开始整段丢弃：母版在这里已经声明过 const S / shot，
    # 只截到 INTRO 会留下它们，和我们生成的镜头表撞成 "Identifier 'S' has already been declared"。
    i = txt.index("    // ================================================================== shots")
    j = txt.index("    Z.SHOTS = S;")
    head = _HEADER + txt[txt.index("(() => {"):i]
    tail = txt[j:]
    return head, tail


def sub1(pattern, repl, text, what, flags=re.M):
    """替换且必须命中恰好 1 次 —— 上游改版后静默失配比报错难查得多。"""
    new, n = re.subn(pattern, repl, text, count=0, flags=flags)
    if n != 1:
        raise SystemExit("[z] 母版补丁「%s」命中 %d 次（应为 1）。上游 timeline.js 可能改版了。"
                         % (what, n))
    return new


_HEADER = """/* engine/timeline.js —— 由 tools/pv_to_z.py 生成，请勿手改。
 *
 *  母版：_ref/vocaloid-style-mv-pipeline/skills/vocaloid-style-mv/template/engine/timeline.js
 *  （NikusonP -- vocaloid-style-mv-pipeline, MIT licence）。除下面两处，母版原样保留：
 *    1. film identity / colour script —— 片名、演员、九个色值
 *    2. shots —— 本片切点表（由 storyboard.json 翻译）
 *  helpers（stars / ridge / starTrails / firstLight / swarmGlyph）、DENY 策展、JIZURA 四个
 *  plan、LOOKS、globalFX、HUD、竖排配置全部照抄上游，未做修改。
 */
"""


# ------------------------------------------------------------------ 数据
def parse_lrc(path):
    """[mm:ss.xx]文本 → [(t, text)]；空行与 [ti:]/[ar:] 剔掉（JIZURA 会当空句画出来）。"""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8-sig") as f:
        for ln in f:
            m = re.match(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$", ln.strip())
            if not m:
                continue
            body = m.group(3).strip()
            if body:
                rows.append((int(m.group(1)) * 60 + float(m.group(2)), body))
    rows.sort(key=lambda r: r[0])
    return rows


def energy_at(audio, t):
    """按时间查上游 analysis 的段落能量；没有 audio.json 时返回 None（全片按 verse 处理）。"""
    hit = None
    for s in (audio or {}).get("sections") or []:
        st, en = float(s.get("start") or 0), float(s.get("end") or 0)
        if st <= t + 1e-6 and (en <= 0 or t < en):
            hit = s
    return float(hit.get("energy") or 0) if hit else None


def look_of(e):
    """能量 → 上游 LOOKS 的词汇。analysis 的 label 恒为 'section'，直接用会让 LOOKS 全套落空；
    这里只把段落归到上游已有的四个档，不新增档位。"""
    if e is None:
        return "verse"
    if e >= 0.78:
        return "chorus"
    if e >= 0.62:
        return "pre-chorus"
    if e >= 0.30:
        return "verse"
    return "intro"


def lyric_at(lrc, t):
    cur = ""
    for tt, tx in lrc:
        if tt <= t + 0.05:
            cur = tx
        else:
            break
    return cur


def glyphs_of(text, n=2):
    body = re.sub(r"[，。！？、—…·「」『』（）()\s]", "", text or "")
    return list(body[:n])


def blocks_of(real):
    """按 section 名切连续块：[(name, [镜下标...])]。同名但不相邻的算两块。"""
    out = []
    for k, sh in enumerate(real):
        nm = str(sh.get("section") or "-")
        if not out or out[-1][0] != nm:
            out.append([nm, []])
        out[-1][1].append(k)
    return [(nm, ks) for nm, ks in out]


# ------------------------------------------------------------------ 立绘
def resolve_pool(specs=None):
    """[(tag, abs_path, name)]。specs=None 用 ART_CAST；给了就用它们（测试注入单张图）。"""
    from songcut import figart

    wanted = [(tag, name) for tag, name in ART_CAST] if specs is None \
        else [("art%d" % i, str(s)) for i, s in enumerate(specs)]

    pool = []
    for tag, spec in wanted:
        p = spec if (os.path.isabs(spec) and os.path.exists(spec)) else None
        if p is None:
            try:
                p = figart.resolve(spec)
            except SystemExit as e:
                print("[warn] 立绘 %s 取不到：%s" % (spec, e))
                continue
        if not p or not os.path.exists(p):
            print("[warn] 立绘不存在，跳过：%s" % spec)
            continue
        pool.append((tag, p, os.path.basename(p)))
    return pool


def copy_art(pool, char_dir):
    """只复制，不改原图；同名同大小就跳过。"""
    os.makedirs(char_dir, exist_ok=True)
    for _, src, name in pool:
        tgt = os.path.join(char_dir, name)
        if not (os.path.exists(tgt) and os.path.getsize(tgt) == os.path.getsize(src)):
            shutil.copy2(src, tgt)
    return len(pool)


# ------------------------------------------------------------------ 编排
def scene_plan(real, audio, limit):
    """每镜的场景。只用上游 demo 的镜头语言：

        typeCard   开场标题
        illust     主力（渐变 + fxBack 星野山脊 + fx 萤火余烬）
        kanjiSlam  每个副歌块的第一镜砸巨字
        converge   每个导句块的最后一镜收白
        afterimage 尾卡（残影 + credit）

    副歌 / 导句的判定只用上游 analysis 的段落能量，不看我们自己的 tier。
    """
    n = len(real)
    scene = ["illust"] * n
    look = [look_of(energy_at(audio, (real[k]["t0"] + real[k]["t1"]) / 2)) for k in range(n)]

    if real and real[0].get("kind") == "title":
        scene[0] = "typeCard"

    slam, conv = [], []
    for nm, ks in blocks_of(real):
        blk = [k for k in ks if scene[k] != "typeCard"]
        if not blk:
            continue
        lk = look[blk[0]]

        def dur(i):
            return real[i]["t1"] - real[i]["t0"]

        def energy(i):
            return energy_at(audio, (real[i]["t0"] + real[i]["t1"]) / 2) or 0

        if lk == "chorus" and dur(blk[0]) >= 2.4:
            slam.append((energy(blk[0]), blk[0]))
        elif lk == "pre-chorus" and dur(blk[-1]) >= 2.4:
            conv.append((energy(blk[-1]), blk[-1]))
    # 两种「重镜」都要限量：demo 一部片只砸 2 次巨字、收白 1 次。按段落能量从高到低取。
    slam.sort(reverse=True)
    conv.sort(reverse=True)
    for _, k in slam[:6]:
        scene[k] = "kanjiSlam"
    for _, k in conv[:5]:
        scene[k] = "converge"
    return scene, look


def cast_plan(real, scene, look, pool, density=0.62):
    """{镜号: (tag, path, name)}。每个 section 块的第一镜必挂、块内隔一镜挂；
    巨字镜 / 收白镜不挂（她让位给字与漩涡）。三张按段落情绪分工。"""
    cast = {}
    if not pool:
        return cast
    by_tag = {t: (t, p, nm) for t, p, nm in pool}
    first = pool[0]

    for nm, ks in blocks_of(real):
        blk = [k for k in ks if scene[k] == "illust"]
        if not blk:
            continue
        picks = blk if density >= 0.85 else [k for j, k in enumerate(blk) if j == 0 or j % 2 == 1]
        want = MOOD.get(look[blk[0]], "calm")
        who = by_tag.get(want) or first
        for k in picks:
            cast[k] = who
    return cast


def shot_row(k, sh, scene, look, char, lrc):
    """一镜的 args / extra。illust 吃 grad / fxBack / fx / vignette —— 全是上游 demo 的写法。"""
    t0, t1 = float(sh["t0"]), float(sh["t1"])
    row = {"id": "s%02d" % int(sh.get("i", k)), "t0": round(t0, 3), "t1": round(t1, 3),
           "scene": scene, "look": look, "char": bool(char)}

    if scene == "typeCard":
        row["args"] = {
            "lines": [Raw("Z.TITLE")], "size": 112, "fam": "minchoHeavy", "weight": 700,
            "ruleColor": Raw("FF.glow"),
            "sub": Raw("[Z.TITLE_SUB, Z.ARTIST].filter(Boolean).join('  ·  ')"), "lead": 0.25,
        }
        row["extra"] = {"lyric": "none", "hud": False, "post": {"boil": 0, "bloom": 0.4, "grain": 0.04}}
        return row

    if scene == "converge":
        row["args"] = {"seed": 7 + (k % 5)}
        row["extra"] = {"plan": "cf",
                        "post": Raw("S => Object.assign(Z.INTRO.post.converge(S), { lutA: 'P4', lutMix: 0.12 })")}
        return row

    if scene == "kanjiSlam":
        row["args"] = {"glyphs": glyphs_of(lyric_at(lrc, t0 + 0.6), 2) or ["岁", "己"],
                       "variant": "night", "char": None, "bg": None,
                       "color": Raw("FF.glow"), "slots": Raw("SLAM")}
        row["extra"] = {"lyric": "none", "hud": False,
                        "post": Raw("S => Object.assign(Z.typo.post.kanjiSlam(S), { lutA: 'night', lutMix: 0.12 })")}
        return row

    seed = 3 + (k * 7) % 17
    fx_back, fx = [], []
    if look == "intro":
        fx_back.append(Raw("stars(70, %d, 0.55, '#CFE0FF', 0.55)" % seed))
        fx_back.append(Raw("ridge(930, 120, '#04060c', { drift: 5, seed: %d })" % (seed + 1)))
        fx.append({"type": "dust", "n": 26, "color": "rgba(190,205,240,0.26)"})
        vignette = 0.58
    elif look == "verse":
        fx_back.append(Raw("stars(150, %d, 0.72)" % seed))
        fx_back.append(Raw("ridge(860, 140, '#070a14', { drift: 10, seed: %d, scale: 480 })" % (seed + 1)))
        fx_back.append(Raw("ridge(950, 110, '#03050a', { drift: 22, seed: %d, scale: 360 })" % (seed + 2)))
        fx.append({"type": "fireflies", "n": 14, "area": [0, 640, 1920, 440],
                   "alpha": 0.7, "color": Raw("FF.glow")})
        vignette = 0.50
    elif look == "pre-chorus":
        fx_back.append(Raw("starTrails(%d, 190, { n: 300, seed: %d })" % (1340 + (k % 3) * 120, seed)))
        fx_back.append({"type": "glow", "x": 1400, "y": 200, "r": 300, "color": "#BFD4FF", "a": 0.20})
        fx.append(Raw("ridge(940, 120, '#03060c', { drift: 4, seed: %d, rim: 'rgba(255,176,103,0.20)' })" % seed))
        vignette = 0.46
    else:
        fx_back.append(Raw("stars(120, %d, 0.55)" % seed))
        fx_back.append({"type": "glow", "x": 960, "y": 1080, "r": 900,
                        "color": Raw("FF.sunset"), "a": 0.34, "beat": 0.5})
        fx_back.append({"type": "rays", "x": 960, "y": 1150, "n": 22, "len": 1800,
                        "color": Raw("FF.glow"), "a": 0.07})
        fx.append(Raw("ridge(960, 90, '#02040a', { drift: 30, seed: %d })" % seed))
        fx.append({"type": "embers", "n": 120, "color": Raw("FF.glow"), "rise": 150,
                   "size": 3.2, "area": [0, 300, 1920, 1160], "wind": 0.15})
        fx.append({"type": "fireflies", "n": 36, "alpha": 0.8, "color": Raw("FF.glow")})
        vignette = 0.38

    args = {"grad": Raw("NIGHT"), "fxBack": fx_back, "fx": fx, "vignette": vignette}

    if char:
        _, _, name = char
        side = 1 if k % 2 == 0 else -1
        x = 1290 if side > 0 else 640
        args["chars"] = [{
            "img": "assets/char/" + name, "front": True,       # front → S.fg：唱词从她身后穿过
            "x": x, "x2": x - side * 36, "y": 1104, "h": 940 + (k % 3) * 40,
            "q": 12,                                          # 12fps 两拍格，和片子的 koma 对齐
            "sway": 0.010, "breathe": 0.9, "phase": (k % 5) * 0.2,
            "in": {"dur": 0.55, "dy": 28, "ds": 0.03, "dx": -side * 56},
            "shadow": {"a": 0.30, "dx": side * 30, "dy": 0},
        }]

    row["args"] = args
    row["extra"] = {"lyric": "front",
                    "plan": "cf" if char else ("noir" if look == "pre-chorus" else "main")}
    if char:
        # 上游 timeline.js 的注释：daylight film 用 { bloom: 0.15, bloomThreshold: 0.93 }，
        # 因为 bloom 的亮部阈值按 max(R,G,B) 取，白发白衣在她身上全是高光，按夜景 0.72 会糊成一团。
        row["extra"]["post"] = {"bloom": 0.15, "bloomThreshold": 0.93}
    return row


def build(job_dir, dst, dur, title, artist, credit, sub, limit=None, art=None, density=0.62):
    sb_path = os.path.join(job_dir, "storyboard.json")
    if not os.path.exists(sb_path):
        raise SystemExit("找不到 %s" % sb_path)
    with open(sb_path, encoding="utf-8") as f:
        sb = json.load(f)
    shots = sb["shots"]

    lrc_rows = parse_lrc(os.path.join(job_dir, "lyric.lrc"))
    lrc_out = os.path.join(dst, "analysis", "lyrics_mv.lrc")
    os.makedirs(os.path.dirname(lrc_out), exist_ok=True)
    if lrc_rows:
        with open(lrc_out, "w", encoding="utf-8", newline="\n") as f:
            for t, tx in lrc_rows:
                f.write("[%02d:%05.2f]%s\n" % (int(t // 60), t % 60, tx))

    audio = None
    ap = os.path.join(dst, "analysis", "audio.json")
    if os.path.exists(ap):
        with open(ap, encoding="utf-8") as f:
            audio = json.load(f)

    pool = resolve_pool(art)
    n_art = copy_art(pool, os.path.join(dst, "assets", "char"))

    real = [s for s in shots if s.get("t1", 0) > s.get("t0", 0) + 0.05]
    if limit:
        a, b = limit
        real = [s for s in real if s["t0"] < b and s["t1"] > a]
    scene, look = scene_plan(real, audio, limit)
    cast = cast_plan(real, scene, look, pool, density)

    rows = [shot_row(k, sh, scene[k], look[k], cast.get(k), lrc_rows) for k, sh in enumerate(real)]

    # 转场：只在段落入口给一次，块内一律硬切（切点本来就在重音上）。类型全用上游那十种。
    TRANS = {"chorus": {"type": "flash", "dur": 0.16, "color": FF["hi"]},
             "pre-chorus": {"type": "slices", "dur": 0.3},
             "verse": {"type": "xfade", "dur": 0.45},
             "intro": {"type": "black", "dur": 0.6}}
    for nm, ks in blocks_of(real):
        k0 = ks[0]
        if k0 == 0:
            continue                     # 开场不转场
        t = TRANS.get(look[k0])
        if t:
            rows[k0]["extra"]["trans"] = t

    end = max(float(s["t1"]) for s in real) if real else float(dur or 0)
    tail_card = 3.0 if (not limit and rows and rows[-1]["scene"] == "illust"
                        and float(real[-1]["t1"]) - float(real[-1]["t0"]) > 8) else 0.0
    if tail_card:
        # 器乐尾没有唱词：只留一块星空会整段黑掉（28 秒）。按副歌的余烬重造这一镜，
        # 让她站在那里收尾 —— 上游的 embers / glow 本来就是给这口气用的。
        kk = len(real) - 1
        who = (pool[0][0], pool[0][1], pool[0][2]) if pool else None
        rows[-1] = shot_row(kk, real[kk], "illust", "chorus", who, lrc_rows)
        rows[-1]["id"] = "s%02d" % int(real[kk].get("i", kk))
        rows[-1]["t1"] = round(end - tail_card, 3)
        if who:
            c = rows[-1]["args"]["chars"][0]
            c.update({"x": 1120, "x2": 1096, "h": 1040,
                      "in": {"dur": 1.4, "dy": 40, "ds": 0.05}})

    head, tail = load_base()
    head = patch_head(head, title, sub, artist, credit, end, rows)
    tail = patch_tail(tail, rows)
    body = shots_js(rows, pool, tail_card, end)

    tl = head + body + "\n" + tail
    tl_out = os.path.join(dst, "engine", "timeline.js")
    os.makedirs(os.path.dirname(tl_out), exist_ok=True)
    with open(tl_out, "w", encoding="utf-8", newline="\n") as f:
        f.write(tl)

    dist = {}
    for r in rows:
        dist[r["scene"]] = dist.get(r["scene"], 0) + 1
    if tail_card:
        dist["afterimage"] = 1
    rep = {"shots": len(rows) + (1 if tail_card else 0), "scene": dist,
           "lrc_lines": len(lrc_rows), "art_refs": sum(1 for r in rows if r["char"]),
           "art_pool": [nm for _, _, nm in pool], "dur": round(end, 3),
           "out": os.path.relpath(tl_out, dst)}
    with open(os.path.join(dst, "analysis", "z_link.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    return rep


def patch_head(head, title, sub, artist, credit, end, rows):
    head = sub1(r"^  Z\.TITLE = .*\n", "  Z.TITLE = %s;\n" % js(title), head, "Z.TITLE")
    head = sub1(r"^  Z\.TITLE_SUB = .*\n", "  Z.TITLE_SUB = %s;\n" % js(sub), head, "Z.TITLE_SUB")
    head = sub1(r"^  Z\.ARTIST = .*\n", "  Z.ARTIST = %s;\n" % js(artist), head, "Z.ARTIST")
    head = sub1(r"^  Z\.CREDIT = .*\n", "  Z.CREDIT = %s;\n" % js(credit), head, "Z.CREDIT")
    head = sub1(r"^  const FF = \{.*?\};\n", "  const FF = %s;\n" % js(FF), head, "colour script FF", re.S | re.M)
    head = sub1(r"^  const NIGHT = \[.*?\];\n", "  const NIGHT = %s;\n" % NIGHT, head, "colour script NIGHT", re.S | re.M)
    head = sub1(r"^    const END = B\(13\);.*\n",
                "    const END = %s;   // 片尾：最后一镜的终点（demo 的 B(13) 是它自己 12 小节的写法，本曲不适用）\n"
                % js(round(end, 3)), head, "END")
    head = sub1(r"^    const TAIL = 2\.0;.*\n",
                "    const TAIL = 2.0;   // 尾卡长度见 shots 段（本片 3s）\n", head, "TAIL")
    head = sub1(r"^    for \(const s of secs\) s\.look = s\.label;",
                "    // analysis/sections.json 的 label 恒为 'section'（自动分段只有 A1/B1… 这类名字），\n"
                "    // 直接用会让 LOOKS 整套落空。按段落能量归到上游已有的四个档，不新增档位。\n"
                "    for (const s of secs) s.look = (s.energy || 0) >= 0.78 ? 'chorus'\n"
                "      : (s.energy || 0) >= 0.62 ? 'pre-chorus' : (s.energy || 0) >= 0.30 ? 'verse' : 'intro';",
                head, "section look 映射")
    head = sub1(r"ghostA: '#39C6E0', ghostB: '#9BD86A'",
                "ghostA: '#7A2430', ghostB: '#D98C5F'", head, "JIZURA ghost 配色")
    head = sub1(r"^    const TXT = .*\n",
                "    const TXT = Z.TITLE + Z.TITLE_SUB + Z.CREDIT + Z.ARTIST + '0123456789:/.—♩=';\n",
                head, "字体预热串")
    return head


def patch_tail(tail, rows):
    tail = sub1(r"^      full: \[.*?\],\n",
                "      full: %s,\n" % js([r["id"] for r in rows
                                      if r["scene"] in ("typeCard", "kanjiSlam", "converge")]),
                tail, "VERTICAL_CFG.full")
    tail = sub1(r"  const SEC_NAME = \{[^}]*\};",
                "  const SEC_NAME = { intro: 'INTRO', verse: 'A-MELO', 'pre-chorus': 'B-MELO',"
                " chorus: 'SABI', 'post-chorus': 'POST', tail: '—' };", tail, "HUD SEC_NAME")
    tail = sub1(r"SEC_NAME\[sec\.label\] \|\| String\(sec\.label\)\.toUpperCase\(\)",
                "SEC_NAME[sec.look] || String(sec.look).toUpperCase()", tail, "HUD section 名")
    tail = sub1(r"window\.__zankoMeta\.duration = END \+ TAIL;",
                "window.__zankoMeta.duration = END;   // 本片尾卡已含在 END 内", tail, "duration")
    return tail


def shots_js(rows, pool, tail_card, end):
    out = ["    // ===== SHOTS：本片切点（画面语言全部来自上游场景）",
           "    const S = [];",
           "    const shot = (id, t0, t1, scene, args, extra = {}) => "
           "{ if (t1 > t0 + 1e-3) S.push(Object.assign({ id, t0, t1, scene, args }, extra)); };"]
    if pool:
        out.append("    const CHAR = {")
        for tag, _, name in pool:
            out.append("      %s: %s," % (tag, js("assets/char/" + name)))
        out.append("    };")
    out.append("    // 巨字位置表（上游 demo 的 slamSlots：居中偏上，两个字不打架）")
    out.append("    const SLAM = [{ x: 960, y: 560, s: 1.25, r: -0.035 }, { x: 1000, y: 540, s: 1.40, r: 0.05 },"
               " { x: 920, y: 580, s: 1.50, r: 0.06 }, { x: 960, y: 520, s: 1.45, r: -0.06 }];")
    out.append("")

    for r in rows:
        out.append("    shot(%s, %s, %s, %s," % (js(r["id"]), js(r["t0"]), js(r["t1"]), js(r["scene"])))
        out.append("      %s," % js(r["args"], 1))
        out.append("      %s);" % js(r["extra"], 1))

    if tail_card:
        out.append("")
        out.append("    // ===== 尾卡：上游 afterimage（残影 + credit），剪影用她的立绘")
        out.append("    shot('s_end', %s, %s, 'afterimage', {" % (js(round(end - tail_card, 3)), js(end)))
        out.append("      text: Z.TITLE, sub: Z.TITLE_SUB.toLowerCase(), credit: Z.CREDIT, color: %s,"
                   % js(FF["hi"]))
        out.append("      char: %s },"
                   % (js("assets/char/" + pool[0][2]) if pool else "NO_IMAGE"))
        out.append("      { lyric: 'none', hud: false,"
                   " post: S => Object.assign(Z.INTRO.post.afterimage(S), { boil: 0 }) });")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="storyboard → Vocaloid-Style MV Pipeline 编排稿")
    ap.add_argument("--job", required=True, help="我们的工作区（含 storyboard.json / lyric.lrc）")
    ap.add_argument("--dst", required=True, help="上游工程的根目录（engine/ 在这里）")
    ap.add_argument("--title", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--credit", default="")
    ap.add_argument("--sub", default="")
    ap.add_argument("--dur", type=float, default=0.0, help="成片时长（秒）；不给就取 storyboard.meta.dur")
    ap.add_argument("--shots", default="", help="只翻译某个区间，如 0:12（调试用）")
    ap.add_argument("--art", default="", help="立绘文件名，逗号分隔；空则用内置三张")
    ap.add_argument("--art-density", type=float, default=0.62, help=">=0.85 时每个 illust 镜都挂")
    a = ap.parse_args(argv)

    sb = os.path.join(a.job, "storyboard.json")
    meta = {}
    if os.path.exists(sb):
        with open(sb, encoding="utf-8") as f:
            meta = json.load(f).get("meta") or {}
    title = a.title or meta.get("title") or "UNTITLED"
    artist = a.artist or meta.get("vocal") or meta.get("artist") or ""
    credit = a.credit or ("Vocal %s / Song %s" % (meta.get("vocal", ""), meta.get("artist", ""))).strip(" /")
    sub = a.sub or (meta.get("artist") or "")
    dur = a.dur or float(meta.get("dur") or 0)
    limit = None
    if a.shots:
        p, q = a.shots.split(":")
        limit = (float(p), float(q))
    art = [s.strip() for s in a.art.split(",") if s.strip()] or None

    rep = build(a.job, a.dst, dur, title, artist, credit, sub, limit, art, a.art_density)
    print("[z] %d 镜：%s" % (rep["shots"], rep["scene"]))
    print("[z] 唱词 %d 行 · 立绘 %d 镜 / %d 张 · 时长 %.2fs"
          % (rep["lrc_lines"], rep["art_refs"], len(rep["art_pool"]), rep["dur"]))
    print("[z] → %s" % os.path.join(a.dst, "engine", "timeline.js"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
