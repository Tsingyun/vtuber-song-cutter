# -*- coding: utf-8 -*-
"""mvgraph —— 统一时间轴 + 统一图层描述（MV Scene Graph，只读中间表示）。

它是什么
--------
把「这一片每一帧由哪些层、按什么顺序、在什么时刻画出来」从现有产物里**读出来**，
归到一个与渲染器解耦的数据结构里：

    输入（全部已有，不新增采集）
        segments/<日期>.json         切点窗口·原唱·歌词剔行（歌切侧定稿）
        _work/storyboard.json        镜头·版式·人物挂点·转场（编排稿）
        live.json / storyboard.meta.live
                                     人物序列目录·帧数·帧率·tOff
        _work/analysis.json          BPM·段落·能量（可选）

    输出
        graph = {
          project   {date,song,fps,dur,frames,seed,...}   统一时间基准
          timeline  {fps,dur,frames,audio,sections,...}
          layers[]  Layer Descriptor（见下）
          checks[]  一致性检查结果
          ok        bool
        }

它不是什么
----------
**不接管渲染**。现有逐帧渲染由 `renderer/player/pv/*` 消费 storyboard 完成，
人物已在逐帧合成阶段作为图层参与（`pv_engine.drawFigure` 按 shot.fig 画在
canvas 上，不是最后贴片）。这一层不重写那条路，只做两件事：

1. 让「时间轴 ↔ 图层」这份事实只有一个可读的出口（换引擎、加图层、做校验都读它）；
2. 把静默错变成可报的错 —— 人物帧不足、tOff 与窗口不符、镜头空洞……

为什么值得存在（都是本仓库踩过的）
--------------------------------
* 人物序列 `n` 帧不够覆盖镜头时，`getFrame` 会 clamp 到最后一帧并**静默返回上一张**：
  画面上人还在，只是不动了。肉眼要盯很久才发现「这句 8 秒她一直定格」。
* `tOff` 少加窗口起点时，整段落到序列外，`getFrame` 返回 null，那一镜静默没人。
* 换歌忘了换挂点表时，抽到的窗口落在片子之外，人在画面上但唱的是另一首。

这三类错在渲染侧都不会抛异常，只能靠「时间 ↔ 帧 ↔ 素材」的算术检查出来。

字段设计说明
------------
Layer Descriptor 的字段只在**有确定来源**时才填：`rect` 之类由引擎运行时
计算的几何，这里给 `None` 并标 `geom="engine"`，绝不按公式复算一份 ——
复算就是第二份真相（RULES §120 / §94 的坑）。任务书要求「位置/尺寸/遮罩可描述」，
描述由 `extra` 原样带出编排稿的显式参数（mode / side / scale / a / ...）满足。
"""
import json
import os

from songcut import pvlink as PL

VERSION = 1

# 八类图层（任务书 §3.2）。引擎目前实际产出的类型见 ENGINE_CAPS。
LAYER_TYPES = ("bg", "bg3d", "footage", "lyric", "text", "fx", "fg", "trans")

# 叠放次序基准。**与 pv_engine 的实际绘制顺序一致**（图先画、字后画，
# 人物 layer 默认在歌词之下；JIZURA 走 --jz-layer behind 时才沉到最底）。
Z = {"bg": 0, "bg3d": 5, "lyric_behind": 10, "footage": 20, "lyric": 30,
     "text": 40, "fx": 50, "fg": 55, "trans": 60}

# 引擎能力：哪些「描述上成立」的东西渲染侧其实还做不到。
# 写在这里而不是散在文档里 —— 校验器要据此把「希望」和「已完成」分开报。
ENGINE_CAPS = {
    "lyric_split": False,       # 同一句拆成人物前/后两层（任务书场景 C）：引擎按行整行画
    "footage_camera_space": False,   # 人物参与镜头变换：人物是屏幕空间（SAFE 边距）
    "bg3d": False,              # Blender 三维层：管线里还没有这条入口
    "fg": False,                # 独立前景遮挡层：目前只有装饰件与后期
    "per_layer_post": False,    # 分层后期（引擎是整帧 post）
}

TOL = 0.05                      # 时间比较容差（秒）
# 词级时间来自语音边界，镜头边界由编排稿按句/停顿划 —— 两者本来就不会逐毫秒相等。
# 0.05s 的容差会把「镜头按语义提前收尾」全部报成越界（实测 8 处 0.04~0.2s 的差），
# 噪音一多，闸门就等于没有。0.35s 与 pv_direct 的挂点容差同口径。
TOL_LYRIC = 0.35


class MvGraphError(RuntimeError):
    """编排稿/素材缺失，无法构造场景图。消息里要带下一步。"""


# ── 统一时间基准 ───────────────────────────────────────────────────────
def frame_of(t, fps):
    """秒 → 帧号。全图只有这一个换算（四舍五入，与编码器取整一致）。"""
    return int(round(float(t) * float(fps)))


def time_of(f, fps):
    """帧号 → 秒。"""
    return round(float(f) / float(fps), 6)


def timeline(sb, fps=None, dur=None):
    """统一时间轴：以**输出帧率下的帧号**为主键，其他单位都从它派生。"""
    meta = sb.get("meta") or {}
    f = float(fps or meta.get("fps") or 60)
    d = float(dur or meta.get("dur") or 0)
    if d <= 0:
        shots = sb.get("shots") or []
        d = max((float(s.get("t1") or 0) for s in shots), default=0.0)
    audio = sb.get("audio") or {}
    return {
        "fps": f,
        "dur": round(d, 3),
        "frames": frame_of(d, f),
        "audio": {"bpm": audio.get("bpm"), "bar_sec": audio.get("barSec")},
        "sections": audio.get("sections") or [],
        "unit": "frame",
        # 素材本身可能是 24/10/5fps（人物序列按需降帧抽），换算必须带各自帧率，
        # 这一步在这里只做声明，真正的换算在 _footage_frame_span 里按序列 fps 做。
        "mixed_rates": True,
    }


# ── 图层派生 ───────────────────────────────────────────────────────────
def _layer(lid, typ, name, t0, t1, z, source="", **kw):
    lay = {
        "id": lid, "type": typ, "name": name,
        "source": source,
        "t0": round(float(t0), 3), "t1": round(float(t1), 3),
        "frame0": None, "frame1": None,          # 由 build() 按输出 fps 填
        "z": z,
        "rect": None, "scale": 1.0, "rot": 0.0, "alpha": 1.0,
        "mask": None, "crop": None, "anim": None, "blend": "source-over",
        "visible": True, "camera_space": False, "geom": "engine",
    }
    lay.update(kw)
    return lay


def _bg_layers(sh):
    """背景场景层：bgl/bg 是「程序化背景件」清单（pv_bg.js 消费），聚成一层。"""
    items = []
    for key in ("bgl", "bg"):
        for it in (sh.get(key) or []):
            if isinstance(it, dict):
                items.append(dict(it))
            elif isinstance(it, str):
                items.append({"k": it})
    if not items:
        return []
    kinds = [str(i.get("k") or "?") for i in items]
    return [_layer("bg:%d" % sh["i"], "bg", "background", sh["t0"], sh["t1"], Z["bg"],
                   source="procedural:" + "+".join(kinds),
                   extra={"items": items})]


def _footage_layer(sh, live, fps):
    """真实录播人物层。字段原样带出编排稿的显式参数，几何留给引擎算。"""
    fig = sh.get("fig")
    if not isinstance(fig, dict) or not fig.get("seq"):
        return []
    seq = fig["seq"]
    mode = fig.get("mode") or "disc"
    # 遮罩语义：圆窗族是真圆形裁切，竖幅族是矩形（含羽化/出血变体）
    mask = "circle" if mode in ("disc", "duo", "round") else "rect"
    if mode == "soft":
        mask = "feather-rect"
    ent = (live or {}).get(seq) or {}
    return [_layer(
        "footage:%d" % sh["i"], "footage", "sui-live",
        sh["t0"], sh["t1"], Z["footage"],
        source=seq,
        mask=mask,
        alpha=float(fig.get("a", 1.0)),
        scale=float(fig.get("scale", 1.0) or 1.0),
        camera_space=bool(fig.get("cam")),      # 只有显式声明才认为参与镜头
        extra={
            "mode": mode,
            "side": fig.get("side"), "at": fig.get("at"), "y": fig.get("y"),
            "w": fig.get("w"), "h": fig.get("h"), "wf": fig.get("wf"),
            "hf": fig.get("hf"), "wRatio": fig.get("wRatio"),
            "rad": fig.get("rad"), "skew": fig.get("skew"),
            "feather": fig.get("feather"), "paper": fig.get("paper"),
            "tone": fig.get("tone"), "toneHi": fig.get("toneHi"),
            "reveal": fig.get("reveal"),
            "tOff": float(fig.get("tOff") or 0.0),
            "wDur": fig.get("wDur"),
            "seq_n": ent.get("n"), "seq_fps": ent.get("fps"),
            "seq_dir": ent.get("dir"),
        },
    )]


def _lyric_layers(sh, lines, jz_behind=False):
    """歌词层：一句一镜一层。line=-1 的 breath 镜没有词，不出层。"""
    if sh.get("kind") != "line":
        return []
    li = sh.get("line")
    if not isinstance(li, int) or li < 0 or li >= len(lines):
        return []
    ln = lines[li]
    lay = _layer(
        "lyric:%d" % sh["i"], "lyric", "line-%03d" % li,
        sh["t0"], sh["t1"],
        Z["lyric_behind"] if jz_behind else Z["lyric"],
        source="timing:line",
        extra={"text": ln.get("text") or "",
               "words": ln.get("words") or [],
               "t_speech": [ln.get("t0"), ln.get("t1")],
               "layout": sh.get("layout"), "enter": sh.get("enter"),
               "hold": sh.get("hold"), "exit": sh.get("exit")},
    )
    # 词级时间必须落在层区间内，否则「字还没到就已经在画」或「层没了词还在动」
    if ln.get("t0") is not None and ln.get("t1") is not None:
        lay["extra"]["span_ok"] = (
            float(ln["t0"]) >= float(sh["t0"]) - TOL_LYRIC
            and float(ln["t1"]) <= float(sh["t1"]) + TOL_LYRIC)
    return [lay]


def _text_layers(sh):
    """装饰性文字层（decor）：与歌词分开，因为它不参与逐字动画。"""
    dec = sh.get("decor") or []
    if not dec:
        return []
    return [_layer("text:%d" % sh["i"], "text", "decor", sh["t0"], sh["t1"], Z["text"],
                   source="decor:%d" % len(dec), extra={"items": dec})]


def _fx_layers(sh):
    """特效层（整帧后期）：post 列表。分层应用目前不支持，如实标出。"""
    post = sh.get("post") or []
    if not post:
        return []
    return [_layer("fx:%d" % sh["i"], "fx", "post", sh["t0"], sh["t1"], Z["fx"],
                   source="post:" + "+".join(str(p.get("k") or "?") for p in post),
                   blend="whole-frame",
                   extra={"items": post, "per_layer": ENGINE_CAPS["per_layer_post"]})]


def _trans_layer(sh):
    tr = sh.get("trans")
    if not isinstance(tr, dict):
        return []
    d = float(tr.get("dur") or 0.0)
    if d <= 0:
        # 硬切（k="cut" 之类）不占时间。产一个 0 时长的层只会让校验器报
        # 「layer.empty」，而那是转场种类，不是缺陷。
        return []
    return [_layer("trans:%d" % sh["i"], "trans", "to-%d" % (sh["i"] + 1),
                   sh["t0"], sh["t0"] + d, Z["trans"],
                   source="trans:" + str(tr.get("k") or "?"),
                   blend="transition", extra={"k": tr.get("k"), "dur": d})]


def layers_of(sb, live=None, fps=None):
    """把编排稿摊平成统一图层列表（按时间排序，同刻按 z 排序）。"""
    fps = float(fps or (sb.get("meta") or {}).get("fps") or 60)
    lines = sb.get("lines") or []
    jz_behind = bool((sb.get("meta") or {}).get("jz_layer") == "behind")
    out = []
    for sh in (sb.get("shots") or []):
        out += _bg_layers(sh)
        out += _footage_layer(sh, live, fps)
        out += _lyric_layers(sh, lines, jz_behind)
        out += _text_layers(sh)
        out += _fx_layers(sh)
        out += _trans_layer(sh)
    for lay in out:
        lay["frame0"] = frame_of(lay["t0"], fps)
        lay["frame1"] = frame_of(lay["t1"], fps)
    out.sort(key=lambda x: (x["t0"], x["z"], x["id"]))
    return out


# ── 素材与帧覆盖 ───────────────────────────────────────────────────────
def _seq_dir(ex):
    """序列目录。``dir`` 是 live.json 的原始键，``seq_dir`` 是图层里的副本。"""
    d = (ex.get("dir") or ex.get("seq_dir") or "").replace("\\", "/")
    if not d:
        return ""
    if os.path.isabs(d):
        return d
    cands = [os.path.join(os.getcwd(), d), os.path.join(PL.workdir(), d),
             os.path.join(PL.dirs()["workdir"], d)]
    for c in cands:
        if os.path.isdir(c):
            return os.path.abspath(c)
    return os.path.abspath(cands[0])


def _footage_frame_span(lay):
    """图层在它那条序列上的帧号区间。

    引擎：``i = round((t + tOff) * seqFps)``，越界 clamp 到 [0, n-1]。
    clamp 之后**不报错**——起点越界就卡在第一帧，终点越界就卡在最后一帧，
    画面里人还在，只是不动了。这就是要在这里算一遍的原因。
    """
    ex = lay.get("extra") or {}
    n = ex.get("seq_n")
    sf = ex.get("seq_fps")
    if not n or not sf:
        return None
    off = float(ex.get("tOff") or 0.0)
    i0 = frame_of(lay["t0"] + off, sf)
    i1 = frame_of(lay["t1"] + off, sf)
    return {"i0": i0, "i1": i1, "n": int(n),
            "clamped": i0 < 0 or i1 > int(n) - 1,
            "short_head": max(0, -i0),
            "short_tail": max(0, i1 - (int(n) - 1))}


def build(date, song="", storyboard="", live_json="", seq=None, fps=None,
          with_source=True):
    """构造一首歌的 MV 场景图。编排稿必须已经存在（pv_direct 的产物）。"""
    sbp = storyboard or _default_storyboard(date, song)
    if not sbp or not os.path.isfile(sbp):
        raise MvGraphError(
            "找不到编排稿：%s\n  → 先跑 tools/pv_one.py（会生成 _work/storyboard.json），"
            "或显式 --storyboard 指定。" % (sbp or "（未找到）"))
    with open(sbp, encoding="utf-8") as f:
        sb = json.load(f)
    meta = sb.get("meta") or {}

    # 人物序列：编排稿 meta.live 优先（pv_direct 已把相对路径解成绝对），
    # 其次显式 live_json。都没有就是「纯视觉片」，不是错。
    live = meta.get("live") or {}
    if live_json and os.path.isfile(live_json):
        with open(live_json, encoding="utf-8") as f:
            lj = json.load(f)
        root = os.path.dirname(os.path.abspath(live_json))
        live = {k: dict(v) for k, v in lj.items()
                if k not in ("face", "cut_start") and isinstance(v, dict)}
        for v in live.values():
            d = (v.get("dir") or "").replace("\\", "/")
            if d and not os.path.isabs(d):
                cands = [os.path.abspath(d), os.path.join(root, d)]
                v["dir"] = next((c for c in cands if os.path.isdir(c)), cands[0])
        if lj.get("face"):
            live["face"] = lj["face"]

    tl = timeline(sb, fps=fps)
    layers = layers_of(sb, live=live, fps=tl["fps"])

    g = {
        "version": VERSION,
        "project": {
            "date": date, "song": meta.get("title") or song,
            "artist": meta.get("artist") or "", "vocal": meta.get("vocal") or "",
            "seed": meta.get("seed"), "concept": meta.get("concept"),
            "storyboard": os.path.abspath(sbp),
            "live_json": os.path.abspath(live_json) if live_json else None,
            "seq": seq,
        },
        "timeline": tl,
        "layers": layers,
        "caps": dict(ENGINE_CAPS),
        "checks": [],
        "ok": True,
    }
    # 溯源块：窗口是谁定的（歌切定稿 / 手工），事后必须能回答
    try:
        b = PL.bundle(date, song or meta.get("title") or "", seq)
        g["project"]["window"] = b["window"]
        g["project"]["link"] = {"segments": b["segments"], "seq": b["seq"],
                                "match": b["match"],
                                "window_source": b["window"]["source"],
                                "lrc_source": b["lrc"]["source"]}
    except PL.PvLinkError:
        g["project"]["window"] = None
        g["project"]["link"] = None

    g["checks"] = validate(g, sb)
    g["ok"] = not any(c["level"] == "error" for c in g["checks"])
    return g


def _default_storyboard(date, song):
    if not song:
        return ""
    base = os.path.join(PL.dirs()["cuts"], date, song.replace("/", "_"), "_work")
    for name in ("storyboard.json", "sb.json"):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    return os.path.join(base, "storyboard.json")


# ── 一致性校验 ─────────────────────────────────────────────────────────
def validate(g, sb=None):
    """把「静默错」摊成清单。level: error / warn / info。

    只报**能证明的**问题：能算出帧号越界才报越界，算不出就报 info 说缺什么，
    不猜。渲染侧照旧跑，但出片前必须先看这张表。
    """
    ck = []
    tl = g["timeline"]
    dur, fps = tl["dur"], tl["fps"]

    def err(code, msg, at=None):
        ck.append({"level": "error", "code": code, "msg": msg, "at": at})

    def warn(code, msg, at=None):
        ck.append({"level": "warn", "code": code, "msg": msg, "at": at})

    def info(code, msg, at=None):
        ck.append({"level": "info", "code": code, "msg": msg, "at": at})

    if dur <= 0:
        err("timeline.empty", "时间轴长度为 0（编排稿 meta.dur 与 shots 都没给时长）")
    if fps <= 0:
        err("timeline.fps", "输出帧率为 0，无法建立帧基准")

    # ① 每层必须落在片内、且非空
    for lay in g["layers"]:
        if lay["t1"] <= lay["t0"]:
            err("layer.empty", "%s 时长 ≤0（%.3f→%.3f）" % (lay["id"], lay["t0"], lay["t1"]),
                lay["t0"])
        elif lay["t0"] < -TOL or lay["t1"] > dur + TOL:
            err("layer.out_of_range", "%s 超出片子 0~%.2f（%.3f→%.3f）"
                % (lay["id"], dur, lay["t0"], lay["t1"]), lay["t0"])

    # ② 镜头首尾必须相接：空洞 = 中间若干帧没有任何背景层（黑帧）
    if sb:
        shots = sorted((sb.get("shots") or []), key=lambda s: s["t0"])
        prev = 0.0
        for s in shots:
            if float(s["t0"]) - prev > TOL:
                warn("shot.gap", "%.2f→%.2f 没有镜头（%.2fs 空档）"
                     % (prev, float(s["t0"]), float(s["t0"]) - prev), prev)
            prev = max(prev, float(s["t1"]))
        if dur - prev > TOL:
            warn("shot.tail", "片尾 %.2fs 没有镜头" % (dur - prev), prev)

    # ③ 人物层：序列是否存在、帧数是否覆盖整个图层区间
    foot = [l for l in g["layers"] if l["type"] == "footage"]
    for lay in foot:
        ex = lay["extra"]
        if not ex.get("seq_n"):
            err("footage.seq_missing",
                "%s 引用的序列 %s 不在 live.json 里（该镜会静默没人）"
                % (lay["id"], lay["source"]), lay["t0"])
            continue
        d = _seq_dir(ex)
        if not d or not os.path.isdir(d):
            err("footage.dir_missing", "%s 序列目录不存在：%s" % (lay["id"], d), lay["t0"])
            continue
        try:
            on_disk = len([x for x in os.listdir(d) if os.path.splitext(x)[1].lower()
                           in (".jpg", ".jpeg", ".png", ".webp")])
        except OSError:
            on_disk = -1
        if on_disk >= 0 and on_disk < int(ex["seq_n"]):
            err("footage.frames_missing",
                "%s 序列声明 %d 帧，磁盘只有 %d 帧" % (lay["id"], ex["seq_n"], on_disk),
                lay["t0"])
        sp = _footage_frame_span(lay)
        if sp and sp["clamped"]:
            # 起点越界 → 卡首帧；终点越界 → 卡末帧。两种都是「人不动」而不是「没人」。
            err("footage.out_of_seq",
                "%s 需要序列帧 %d~%d（序列共 %d 帧）：%s"
                % (lay["id"], sp["i0"], sp["i1"], sp["n"],
                   ("头缺 %d 帧" % sp["short_head"]) if sp["short_head"] else
                   ("尾缺 %d 帧（画面会定格在最后一帧）" % sp["short_tail"])),
                lay["t0"])
        elif sp:
            cov = (sp["i1"] - max(0, sp["i0"])) / float(max(1, sp["i1"] - sp["i0"]))
            if cov < 0.9:
                warn("footage.low_coverage",
                     "%s 只有 %.0f%% 的区间能取到新帧" % (lay["id"], cov * 100), lay["t0"])

    # ④ 人物层的 tOff 与窗口约定：窗口起点应比镜头起点早 2.0s
    for lay in foot:
        off = float((lay["extra"] or {}).get("tOff") or 0.0)
        if abs((lay["t0"] + off) - 2.0) > 0.35:
            warn("footage.window_offset",
                 "%s 窗口起点=%.2f（按约定应为 2.00，差 %.2fs）"
                 % (lay["id"], lay["t0"] + off, lay["t0"] + off - 2.0), lay["t0"])

    # ⑤ 同一时刻同 z 的两层：叠放次序不确定（引擎按数组顺序，等于碰运气）
    acts = sorted(g["layers"], key=lambda x: x["t0"])
    for i in range(len(acts)):
        a = acts[i]
        for j in range(i + 1, len(acts)):
            b = acts[j]
            if b["t0"] >= a["t1"] - TOL:
                break
            if a["z"] == b["z"] and a["type"] != "trans" and b["type"] != "trans":
                warn("z.conflict", "%s 与 %s 同层叠放（z=%d，%.2f~%.2f）"
                     % (a["id"], b["id"], a["z"], b["t0"],
                        min(a["t1"], b["t1"])), b["t0"])

    # ⑥ 歌词：词级时间必须被层区间包住
    for lay in [l for l in g["layers"] if l["type"] == "lyric"]:
        if lay["extra"].get("span_ok") is False:
            t0, t1 = lay["extra"]["t_speech"]
            warn("lyric.span", "%s 词级 %.2f~%.2f 超出层 %.2f~%.2f"
                 % (lay["id"], t0, t1, lay["t0"], lay["t1"]), lay["t0"])
        if lay["extra"].get("split"):
            err("lyric.split_unsupported",
                "%s 请求把同一句拆到人物前后两层，但引擎按行整行画（caps.lyric_split=False）"
                % lay["id"], lay["t0"])

    # ⑦ 能力缺口：描述里有这类层但引擎不画（不报错，因为可能只是还没接）
    for typ, cap in (("bg3d", "bg3d"), ("fg", "fg")):
        n = len([l for l in g["layers"] if l["type"] == typ])
        if n and not ENGINE_CAPS.get(cap):
            warn("cap.%s" % cap, "有 %d 个 %s 层，但引擎还没有这类图层入口" % (n, typ))
    nc = len([l for l in foot if l["camera_space"]])
    if nc and not ENGINE_CAPS["footage_camera_space"]:
        warn("cap.footage_camera_space",
             "%d 个人物层声明参与镜头运动，但引擎按屏幕空间画（不会跟着推镜）" % nc)

    if not ck:
        info("ok", "时间轴与图层一致：%d 层 / %.2fs / %d 帧"
             % (len(g["layers"]), dur, tl["frames"]))
    return ck


# ── 摘要 ───────────────────────────────────────────────────────────────
def summary(g):
    """人读摘要：各类型层数、时长占比、人物覆盖。"""
    dur = max(1e-6, g["timeline"]["dur"])
    rows = {}
    for lay in g["layers"]:
        r = rows.setdefault(lay["type"], {"n": 0, "sec": 0.0, "ids": []})
        r["n"] += 1
        r["sec"] += max(0.0, lay["t1"] - lay["t0"])
        r["ids"].append(lay["id"])
    for k, v in rows.items():
        v["sec"] = round(v["sec"], 2)
        v["pct"] = round(v["sec"] / dur * 100, 1)
    foot = rows.get("footage") or {"n": 0, "sec": 0.0, "pct": 0.0}
    lyr = rows.get("lyric") or {"n": 0, "sec": 0.0, "pct": 0.0}
    return {
        "dur": g["timeline"]["dur"], "frames": g["timeline"]["frames"],
        "layers": len(g["layers"]), "by_type": rows,
        "footage_pct": foot.get("pct", 0.0), "lyric_pct": lyr.get("pct", 0.0),
        "errors": sum(1 for c in g["checks"] if c["level"] == "error"),
        "warns": sum(1 for c in g["checks"] if c["level"] == "warn"),
        "ok": g["ok"],
    }
