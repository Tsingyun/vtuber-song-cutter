# -*- coding: utf-8 -*-
"""mvgraph（统一时间轴 + 图层描述）的回归测试。

跑法：
    python tests/test_mvgraph.py
    python -m pytest tests/test_mvgraph.py -q

锁住什么
--------
这一层不渲染，所以它的价值全在**校验**：把渲染侧不会抛异常的错（人物帧不够、
序列缺失、层越界、镜头空洞）摊成清单。测试因此也全用临时目录 + 假编排稿：

  1. 帧↔秒换算是全图唯一的基准，四舍五入口径不能漂
  2. 八类图层能从编排稿派生出来，z 序与引擎绘制顺序一致
  3. 人物帧覆盖：够 → 无 error；不够 → footage.out_of_seq（并区分头缺/尾缺）
  4. 序列不在 live / 目录不存在 / 磁盘帧数少于声明 → 三种不同 error
  5. 层越界、镜头空洞、词级超出层区间 → 各自报出来
  6. 引擎做不到的能力（歌词拆前后层、人物随镜头）→ 明确报 unsupported，不假装支持
"""
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from songcut import mvgraph as MG       # noqa: E402

FAILS = []


def ck(cond, name, detail=""):
    if cond:
        print("  ✓ %s" % name)
    else:
        print("  ✗ %s %s" % (name, detail))
        FAILS.append(name)


def mkseq(root, name="s1", n=200, fps=30):
    """造一个假序列目录（空 jpg 文件足够 —— 校验只数帧数与名称）。"""
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    for i in range(n):
        open(os.path.join(d, "%05d.jpg" % i), "wb").close()
    return {"dir": d, "n": n, "fps": fps, "w": 720, "h": 720, "pattern": "%05d.jpg"}


def mk_sb(dur=10.0, fps=30, fig=None, kind2="line", t_speech=(1.0, 2.0)):
    """最小编排稿：片头卡 + 两句词。fig 传 None 就是纯视觉片。"""
    lines = [
        {"i": 0, "t0": t_speech[0], "t1": t_speech[1], "text": "第一句",
         "words": [{"text": "第一", "t0": t_speech[0], "t1": t_speech[0] + 0.4}]},
        {"i": 1, "t0": 3.0, "t1": 4.5, "text": "第二句", "words": []},
    ]
    base = {"layout": {"k": "top"}, "enter": {"k": "fade", "dur": 0.3},
            "hold": {"k": "breathe"}, "exit": {"k": "fade", "dur": 0.3},
            "decor": [], "post": []}
    shots = [
        dict(base, i=0, t0=0.0, t1=1.0, line=-1, kind="card",
             bgl=[{"k": "sunrise"}], fig=None),
        dict(base, i=1, t0=1.0, t1=2.0, line=0, kind="line",
             bgl=[{"k": "paper"}], fig=(json.loads(json.dumps(fig)) if fig else None),
             decor=[{"k": "credit"}], post=[{"k": "grain"}]),
        dict(base, i=2, t0=2.0, t1=3.0, line=-1, kind="line", bgl=[],
             fig=None, special="breath"),
        dict(base, i=3, t0=3.0, t1=4.5, line=1, kind="line", bgl=[{"k": "paper"}],
             fig=None, trans={"k": "wipe", "dur": 0.5}),
        dict(base, i=4, t0=4.5, t1=dur, line=-1, kind="card", bgl=[{"k": "dusk"}],
             fig=None),
    ]
    return {"version": 2,
            "meta": {"title": "测试曲", "artist": "原唱", "vocal": "岁己SUI",
                     "seed": 1234, "dur": dur, "fps": fps, "start": 0,
                     "concept": None},
            "audio": {"bpm": 120, "barSec": 2.0, "sections": [[0, dur, "V1"]]},
            "lines": lines, "shots": shots}


def write(tmp, sb):
    p = os.path.join(tmp, "storyboard.json")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(sb, ensure_ascii=False))
    return p


def codes(g, level=None):
    return [c["code"] for c in g["checks"] if level is None or c["level"] == level]


def test_timebase():
    print("[mvgraph] 帧↔秒换算")
    ck(MG.frame_of(1.0, 30) == 30, "1.0s @30fps = 30 帧")
    ck(MG.frame_of(269.15, 30) == 8074, "269.15s @30fps = 8074 帧（与成片帧数一致）")
    ck(MG.frame_of(0.016, 60) == 1, "四舍五入而非截断")
    ck(abs(MG.time_of(8074, 30) - 269.133333) < 1e-5, "帧→秒可逆")
    t = MG.timeline({"meta": {"fps": 30, "dur": 10}, "audio": {"bpm": 120}})
    ck(t["frames"] == 300 and t["unit"] == "frame", "时间轴以帧号为主键")
    t2 = MG.timeline({"meta": {"fps": 30}, "shots": [{"t0": 0, "t1": 4.2}]})
    ck(t2["dur"] == 4.2, "没有 meta.dur 时从镜头推时长")


def test_layers():
    print("[mvgraph] 图层派生与 z 序")
    tmp = tempfile.mkdtemp(prefix="mvg_")
    try:
        sq = mkseq(tmp, "s1", 200, 30)
        fig = {"seq": "s1", "mode": "disc", "tOff": 1.0, "wDur": 3.0, "a": 0.9,
               "side": "r", "r": 168}
        sb = mk_sb(fig=fig)
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb),
                     live_json="", seq=None)
        g["layers"]  # noqa: B018
        # live 通过 storyboard.meta.live 注入（与 pv_direct 的实际写法一致）
        sb["meta"]["live"] = {"s1": sq, "face": {"cx": 0.5, "cy": 0.4}}
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        types = [l["type"] for l in g["layers"]]
        ck(types.count("bg") == 4, "有背景件的镜头各一层（无 bgl 的 breath 镜不造层）", types)
        ck(types.count("footage") == 1, "只有挂了 fig 的镜头有人物层")
        ck(types.count("lyric") == 2, "line>=0 的镜头各一层歌词（breath 镜不出层）")
        ck(types.count("text") == 1, "decor 单独成装饰文字层")
        ck(types.count("fx") == 1, "post 成特效层")
        ck(types.count("trans") == 1, "trans 成转场层")
        f = [l for l in g["layers"] if l["type"] == "footage"][0]
        ck(f["z"] == MG.Z["footage"], "人物层 z=20", f["z"])
        ly = [l for l in g["layers"] if l["type"] == "lyric"]
        ck(all(l["z"] == MG.Z["lyric"] for l in ly), "歌词在人物之上（z=30）")
        ck(f["mask"] == "circle", "disc → 圆形遮罩")
        ck(f["geom"] == "engine", "几何标为引擎计算，不在图里复算第二份")
        ck(ly[0]["frame0"] == 30 and ly[0]["frame1"] == 60, "层带帧号区间",
           (ly[0]["frame0"], ly[0]["frame1"]))
        ck(g["ok"], "正常编排稿无 error", codes(g, "error"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_footage_coverage():
    print("[mvgraph] 人物帧覆盖")
    tmp = tempfile.mkdtemp(prefix="mvg_")
    try:
        # t0=1.0, tOff=1.0 → 需要从序列第 60 帧开始；镜头 1s@30fps → 到第 90 帧
        fig = {"seq": "s1", "mode": "disc", "tOff": 1.0, "wDur": 3.0}

        sb = mk_sb(fig=fig)
        sb["meta"]["live"] = {"s1": mkseq(tmp, "s1", 200, 30)}
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        ck("footage.out_of_seq" not in codes(g, "error"), "帧数够 → 不报越界")

        sb = mk_sb(fig=fig)
        sq = mkseq(tmp, "short", 70, 30)      # 序列名叫 short，live 的键也必须叫 short
        fig2 = dict(fig, seq="short")
        sb["shots"][1]["fig"] = fig2
        sb["meta"]["live"] = {"short": sq}
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        ck("footage.out_of_seq" in codes(g, "error"),
           "序列只有 70 帧却要用到 90 → 报尾缺", codes(g, "error"))
        ck(any("尾缺" in c["msg"] for c in g["checks"]), "报错说明是尾缺（画面会定格）")

        sb = mk_sb(fig=dict(fig, seq="nope"))
        sb["meta"]["live"] = {"s1": mkseq(tmp, "s1", 200, 30)}
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        ck("footage.seq_missing" in codes(g, "error"), "序列没在 live 里 → seq_missing")

        sq = mkseq(tmp, "few", 200, 30)
        for i in range(100):
            os.remove(os.path.join(sq["dir"], "%05d.jpg" % (i + 100)))
        sb = mk_sb(fig=dict(fig, seq="few"))
        sb["meta"]["live"] = {"few": sq}
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        ck("footage.frames_missing" in codes(g, "error"),
           "声明 200 帧 / 磁盘 100 帧 → frames_missing")

        sb = mk_sb(fig=dict(fig, seq="s1", tOff=0.0))
        sb["meta"]["live"] = {"s1": mkseq(tmp, "s1", 200, 30)}
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        ck("footage.window_offset" in codes(g, "warn"),
           "tOff 与窗口约定差 1s → 警告（不是硬错）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_structural():
    print("[mvgraph] 结构校验")
    tmp = tempfile.mkdtemp(prefix="mvg_")
    try:
        sb = mk_sb()
        sb["shots"][2]["t0"] = 2.6      # 2.0~3.0 之间挖一个 0.6s 空洞
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        ck("shot.gap" in codes(g, "warn"), "镜头之间有空档 → shot.gap")

        sb = mk_sb()
        sb["shots"][3]["t1"] = 12.0     # 超出片长
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        ck("layer.out_of_range" in codes(g, "error"), "层超出片长 → layer.out_of_range")

        sb = mk_sb(t_speech=(2.5, 3.2))  # 词比镜头晚 1.5s 才出现
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        ck("lyric.span" in codes(g, "warn"), "词级时间超出层区间 → lyric.span")

        sb = mk_sb(t_speech=(1.0, 100.0))
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        ck("时间轴仍能建起来", g["timeline"]["dur"] == 10.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_caps():
    print("[mvgraph] 图层能力：已落地的照描述走，缺入口的才报")
    tmp = tempfile.mkdtemp(prefix="mvg_")
    try:
        sb = mk_sb()
        sb["meta"]["live"] = {"s1": mkseq(tmp, "s1", 200, 30)}
        # 图层语义三件套：同一句拆前后层 + 人物镜头增益
        sb["shots"][1]["fig"] = {"seq": "s1", "mode": "band", "tOff": 0.0,
                                 "wDur": 3.0, "layer": "split", "splitAt": 0.45,
                                 "camGain": 0.4}
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        ly = [l for l in g["layers"]
              if l["type"] == "lyric" and l["extra"].get("tier")]
        ck(len(ly) == 2, "split → 歌词出前后两层", [l["id"] for l in ly])
        back = [l for l in ly if l["extra"]["tier"] == "behind"]
        front = [l for l in ly if l["extra"]["tier"] == "front"]
        foot = [l for l in g["layers"] if l["type"] == "footage"]
        ck(len(foot) == 1, "人物层照常出")
        if back and front and foot:
            ck(back[0]["z"] < foot[0]["z"] < front[0]["z"],
               "z 序：后段 < 人物 < 前段（这才是「交错」而不是一层盖一层）",
               [back[0]["z"], foot[0]["z"], front[0]["z"]])
            ck(front[0]["extra"].get("splitAt") == 0.45, "切分点写进描述")
            ck(foot[0]["camera_space"] is True
               and abs(foot[0]["extra"]["camGain"] - 0.4) < 1e-6,
               "人物镜头增益写进描述")
        ck("lyric.split_unsupported" not in codes(g, "error"),
           "引擎已支持分层 → 不再报 unsupported")

        # 切分点越界必须拦住：0 / 1 会切出空段，画面上是「半句话永远不出现」
        sb2 = mk_sb()
        sb2["meta"]["live"] = {"s1": mkseq(tmp, "s1", 200, 30)}
        sb2["shots"][1]["fig"] = {"seq": "s1", "mode": "band", "tOff": 0.0,
                                  "wDur": 3.0, "layer": "split", "splitAt": 1.0}
        g2 = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb2))
        ck("lyric.split_at" in codes(g2, "error"), "splitAt 越界 → error")

        # 人物镜头增益越界
        sb2["shots"][1]["fig"] = {"seq": "s1", "mode": "band", "tOff": 0.0,
                                  "wDur": 3.0, "camGain": 1.7}
        g3 = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb2))
        ck("footage.cam_gain" in codes(g3, "error"), "camGain 越界 → error")

        # bg3d 缺目录：不装 Blender 也必须能报出「这一层没素材」
        sb3 = mk_sb()
        sb3["shots"][1]["bg3d"] = {"dir": os.path.join(tmp, "no_such"), "n": 10,
                                   "fps": 24}
        g4 = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb3))
        ck("bg3d.dir_missing" in codes(g4, "error"), "三维层目录不存在 → error")
        ck(any(l["type"] == "bg3d" for l in g4["layers"]), "三维层仍然被描述出来")

        # 帧数不足
        d = os.path.join(tmp, "b3d")
        os.makedirs(d, exist_ok=True)
        for i in range(12):
            open(os.path.join(d, "%05d.jpg" % i), "wb").close()
        sb3["shots"][1]["bg3d"] = {"dir": d, "n": 40, "fps": 24, "seq": "b3"}
        g5 = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb3))
        ck("bg3d.frames_missing" in codes(g5, "error"), "声明 40 帧实际 12 帧 → error")
        sb3["shots"][1]["bg3d"] = {"dir": d, "n": 12, "fps": 24, "seq": "b3"}
        g6 = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb3))
        ck("bg3d.frames_missing" not in codes(g6, "error"), "帧数对上 → 不报")

        # 分层后期：背景与人物各一层，不能是一层整帧 post
        sb4 = mk_sb()
        sb4["shots"][1]["grade"] = {"bg": "blur(6px) saturate(1.2)", "fig": "none"}
        g7 = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb4))
        gr = [l for l in g7["layers"] if l["id"].startswith("grade:")]
        ck(len(gr) == 2 and {l["extra"]["target"] for l in gr} == {"bg", "fig"},
           "分层后期 → 背景/人物各一层", [l["id"] for l in gr])
        ck(all(l["blend"] == "layer-group" for l in gr),
           "分层后期不是整帧 post（blend=layer-group）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _png(path, w, h, box, alpha=True):
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0) if alpha else (200, 40, 40, 255))
    if alpha:
        d = ImageDraw.Draw(im)
        d.rectangle(box, fill=(200, 40, 40, 255))
    im.save(path)
    return path


def test_art():
    """立绘层（2026-11-03 改向）：会崩的错很少，会「贴一张方块」的错很多。

    这里锁的都是**不报错但画面废掉**的情形：文件不在（整镜没人）、
    没有透明区（文字穿不过去、读作贴纸）、尺寸/切点越界（画出空段或溢出）。
    """
    print("[mvgraph] 立绘层")
    tmp = tempfile.mkdtemp(prefix="mvg_art_")
    try:
        good = _png(os.path.join(tmp, "art.png"), 400, 600, (120, 40, 280, 600))
        opaque = _png(os.path.join(tmp, "opaque.png"), 400, 600, None, alpha=False)

        sb = mk_sb()
        sb["shots"][1]["fig"] = {"layer": "split", "splitAt": 0.62,
                                 "art": {"src": good, "h": 0.88, "shadow": 0.22}}
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        art = [l for l in g["layers"] if l["type"] == "art"]
        ck(len(art) == 1, "立绘出一层", [l["id"] for l in g["layers"]])
        if art:
            ck(art[0]["mask"] == "alpha", "遮罩是 alpha（不是矩形窗）", art[0]["mask"])
            ck(art[0]["rect"] is None and art[0]["geom"] == "engine",
               "几何不复算（交给引擎）")
        ck("art.opaque" not in codes(g, "error"), "带透明区 → 不报 opaque")
        ck("art.src_missing" not in codes(g, "error"), "文件在 → 不报 src_missing")

        # z 序：立绘与 footage 同格（都是「人」），必须落在歌词两层之间
        ly = [l for l in g["layers"] if l["type"] == "lyric"]
        if art and len(ly) == 2:
            back = [l for l in ly if l["extra"].get("tier") == "behind"]
            front = [l for l in ly if l["extra"].get("tier") == "front"]
            if back and front:
                ck(back[0]["z"] < art[0]["z"] < front[0]["z"],
                   "z 序：后段 < 立绘 < 前段")

        # 文件不在
        sb2 = mk_sb()
        sb2["shots"][1]["fig"] = {"layer": "split",
                                  "art": {"src": os.path.join(tmp, "nope.png")}}
        g2 = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb2))
        ck("art.src_missing" in codes(g2, "error"), "文件不存在 → error")

        # 没有透明区：这是「会崩 vs 不崩但难看」里最典型的一种，必须是 error
        sb3 = mk_sb()
        sb3["shots"][1]["fig"] = {"layer": "split", "art": {"src": opaque}}
        g3 = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb3))
        ck("art.opaque" in codes(g3, "error"), "没有透明区 → error（不是 warn）")

        # 尺寸越界
        sb4 = mk_sb()
        sb4["shots"][1]["fig"] = {"layer": "split", "art": {"src": good, "h": 3.0}}
        g4 = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb4))
        ck("art.scale" in codes(g4, "error"), "高度占比越界 → error")

        # 切点越界
        sb5 = mk_sb()
        sb5["shots"][1]["fig"] = {"layer": "split", "splitAt": 1.0,
                                  "art": {"src": good}}
        g5 = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb5))
        ck("art.split" in codes(g5, "error"), "splitAt 越界 → error")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_summary():
    print("[mvgraph] 摘要")
    tmp = tempfile.mkdtemp(prefix="mvg_")
    try:
        sb = mk_sb()
        sb["meta"]["live"] = {"s1": mkseq(tmp, "s1", 200, 30)}
        sb["shots"][1]["fig"] = {"seq": "s1", "mode": "band", "tOff": 1.0, "wDur": 3.0}
        g = MG.build("1999-01-01", "测试曲", storyboard=write(tmp, sb))
        s = MG.summary(g)
        ck(s["dur"] == 10.0 and s["frames"] == 300, "摘要时长/帧数")
        ck(abs(s["footage_pct"] - 10.0) < 0.01, "人物覆盖 = 1s / 10s = 10%", s["footage_pct"])
        ck(s["by_type"]["lyric"]["n"] == 2, "按类型统计层数")
        ck(s["layers"] == len(g["layers"]), "总层数一致")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_storyboard():
    print("[mvgraph] 缺编排稿要可读报错")
    try:
        MG.build("1999-01-01", "不存在的歌", storyboard="/no/such/storyboard.json")
        ck(False, "应抛 MvGraphError")
    except MG.MvGraphError as e:
        ck("先跑 tools/pv_one.py" in str(e), "报错里写了下一步")


def main():
    test_timebase()
    test_layers()
    test_footage_coverage()
    test_structural()
    test_caps()
    test_art()
    test_summary()
    test_missing_storyboard()
    print("")
    if FAILS:
        print("✗ %d 项失败：%s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
