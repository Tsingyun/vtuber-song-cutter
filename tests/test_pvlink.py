# -*- coding: utf-8 -*-
"""pvlink（歌切产物 → 文字 PV 接入层）的回归测试。

跑法（两种都行）：
    python tests/test_pvlink.py
    python -m pytest tests/test_pvlink.py -q

锁住什么
--------
这一层是两条链之间唯一的契约面，任何一条判据松动都会让 PV 侧**安静地**用错素材
（错窗口、错版本歌词、把制作名单当歌词唱、把 .part 残件当录播源）。
所以这里只用临时目录 + 假数据，不碰真实素材、不联网、不跑 ffmpeg：

  1. 歌名规范化与四级匹配（精确/包含/模糊/序号），以及「找不到要有可读报错」
  2. 切点窗口的成对约束（只写一边必须退回粗窗口并给出警告）
  3. 歌词剔行按**非空行**序号（segments 的口径），没命中的行号要报出来
  4. 素材包字段与缺口清单（ready / missing）
  5. 画框几何是深拷贝（改一个预设不能污染另一个），序列名从 live.json 现读
"""
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from songcut import config as CFG        # noqa: E402
from songcut import pvlink as PL         # noqa: E402

FAILS = []


def ck(cond, name, detail=""):
    if cond:
        print("  ✓ %s" % name)
    else:
        print("  ✗ %s %s" % (name, detail))
        FAILS.append(name)


def raises(fn, exc=Exception):
    try:
        fn()
    except exc as e:
        return e
    return None


LRC = ("[00:00.00]专辑介绍语\n"
       "[00:02.00]第一句歌词\n"
       "\n"
       "[00:05.00]第二句歌词\n"
       "[00:08.00]作曲：某人\n")


def make_workdir(tmp):
    """造一个最小 workdir：segments + 歌词缓存 + 封面。"""
    seg = {"version": 1, "date": "2026-01-02", "srt": "", "songs": [
        {"seq": 1, "title_guess": "小丑的品格", "fetch_title": "小丑的品格(现场版)",
         "artist": "泠鸢yousa", "start": 100.0, "end": 110.0,
         "cut_start_abs": 98.5, "cut_end_abs": 112.0,
         "orig_duration_s": 269.1, "lrc_drop_lines": [1, 4, 9],
         "cover_path": os.path.join(tmp, "cover.img")},
        {"seq": 2, "title_guess": "带我走", "fetch_title": "带我走", "artist": "杨丞琳",
         "start": 200.0, "end": 210.0},          # 故意只给粗窗口
        {"seq": 3, "title_guess": "半 边", "fetch_title": "半边",
         "artist": "x", "start": 300.0, "end": 310.0,
         "cut_start_abs": 300.0},                 # 故意只写一边
        {"seq": 4, "title_guess": "只有起点的歌", "fetch_title": "只有起点的歌",
         "artist": "y", "cut_start_abs": 400.0},  # 连粗窗口都没有
    ]}
    os.makedirs(os.path.join(tmp, "segments"), exist_ok=True)
    with io.open(os.path.join(tmp, "segments", "20260102-x.json"), "w",
                 encoding="utf-8") as f:
        f.write(json.dumps(seg, ensure_ascii=False))
    os.makedirs(os.path.join(tmp, "_media_cache"), exist_ok=True)
    open(os.path.join(tmp, "cover.img"), "wb").write(b"\x89PNG\r\n")
    return seg


def setup_cfg(tmp):
    cfg = {"workdir": tmp, "recording_root": "", "streamer": {"name": "TEST"}}
    p = os.path.join(tmp, "config.json")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, ensure_ascii=False))
    os.environ["SONGCUT_CONFIG"] = p
    CFG.load(reload=True)
    PL._SRC_CACHE.clear()
    return p


def test_norm():
    print("[pvlink] 歌名规范化")
    ck(PL.norm_title("小丑的品格") == PL.norm_title("小丑的品格"), "同字面相同")
    ck(PL.norm_title("ＡＢＣ") == PL.norm_title("abc"), "全角+大小写")
    ck(PL.norm_title("a\u00a0b") == PL.norm_title("ab"), "NBSP 被吃掉")
    ck(PL.norm_title("1～2") == PL.norm_title("1~2"), "波浪号统一")
    ck(PL.norm_title("《小丑的品格》") == PL.norm_title("小丑的品格"), "书名号")
    ck(PL.norm_title("  空 白  ") == PL.norm_title("空白"), "空白")
    ck(PL.norm_title(None) == "", "None 不炸")


def test_find():
    print("[pvlink] 歌名匹配")
    e = PL.find_song("2026-01-02", "小丑的品格")
    ck(e["_index"] == 1 and e["_match"] == "exact", "精确命中第 1 首", e.get("_match"))
    e = PL.find_song("2026-01-02", "小丑的品格(现场版)")
    ck(e["_index"] == 1 and e["_match"] == "exact", "fetch_title 也参与精确匹配")
    e = PL.find_song("2026-01-02", "带我")
    ck(e["_index"] == 2 and e["_match"] == "contains", "包含匹配", e.get("_match"))
    e = PL.find_song("2026-01-02", "带我走")
    ck(e["_match"] == "exact", "包含与精确同时可行时优先精确")
    e = PL.find_song("2026-01-02", seq=3)
    ck(e["_index"] == 3 and e["_match"] == "seq", "按序号取")
    err = raises(lambda: PL.find_song("2026-01-02", "完全不存在的歌名"), PL.PvLinkError)
    ck(err is not None and "可用歌名" in str(err), "找不到时列出可用歌名")
    err = raises(lambda: PL.find_song("1999-09-09", "x"), PL.PvLinkError)
    ck(err is not None and "segments" in str(err), "没有 segments 时说明原因")


def test_window():
    print("[pvlink] 切点窗口")
    b = PL.bundle("2026-01-02", "小丑的品格", with_lrc=False)
    ck(b["window"]["source"] == "cut_abs", "成对 cut_abs 直接用")
    ck(abs(b["window"]["dur"] - 13.5) < 1e-6, "时长 = 13.5s", b["window"]["dur"])
    b2 = PL.bundle("2026-01-02", "带我走", with_lrc=False)
    ck(b2["window"]["source"] == "rough", "没写 cut_abs 时退回粗窗口")
    ck(not b2["window"]["warn"], "成对缺失不算异常（本来就没核定）")
    b3 = PL.bundle("2026-01-02", "半边", with_lrc=False)
    ck(b3["window"]["source"] == "rough" and "只写了一边" in b3["window"]["warn"],
       "只写一边 → 警告 + 退回粗窗口", b3["window"])
    ck(any("只写了一边" in m for m in b3["missing"]), "警告进 missing 清单")
    err = raises(lambda: PL.bundle("2026-01-02", "只有起点的歌", with_lrc=False),
                 PL.PvLinkError)
    ck(err is not None and "cut_start_abs" in str(err), "两边都没有时明确报错，不瞎猜")


def test_lrc():
    print("[pvlink] 歌词缓存与剔行")
    # key 规则只能有一份：这里用接入层暴露的路径函数写缓存，再让接入层自己读回来。
    cache = PL.lrc_cache_path("小丑的品格(现场版)")
    rec = {"lrc": LRC, "cover_path": "", "info": {"netease_duration_ms": 269100}}
    with io.open(cache, "w", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False))
    with io.open(PL.lrc_cache_path("带我走"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"lrc": "[00:01.00]带我走\n[00:04.00]带我离开\n",
                            "cover_path": "", "info": {}}, ensure_ascii=False))
    got = PL.lrc_cache("小丑的品格(现场版)")
    ck(got is not None and "第一句歌词" in got[0], "缓存命中")
    ck(PL.lrc_cache("不存在的歌") is None, "未命中返回 None")
    # 歌词必须按**实际演唱版本**（fetch_title）查，不是曲库裸名 —— 铁律的反向验证
    ck(PL.lrc_cache("小丑的品格") is None,
       "曲库裸名查不到（歌词只按演唱版本取，不串版本）")

    text, dropped, missing = PL.drop_lrc_lines(LRC, [1, 4])
    ck("专辑介绍语" not in text, "剔掉第 1 行（介绍语）")
    ck("作曲：某人" not in text, "剔掉第 4 个非空行（制作名单）")
    ck("第二句歌词" in text, "空行不占行号，第二句仍在")
    ck(dropped == ["[00:00.00]专辑介绍语", "[00:08.00]作曲：某人"], "回报剔掉的内容", dropped)
    ck(missing == [], "全部命中时没有漏号")

    b = PL.bundle("2026-01-02", "小丑的品格")
    ck(b["lrc"]["source"] == "cache", "素材包的歌词来自缓存")
    ck(len(b["lrc"]["dropped"]) == 2, "素材包按 segments 剔了 2 行", b["lrc"]["dropped"])
    ck(b["lrc"]["drop_missing"] == [9], "没命中的行号 9 被报出来", b["lrc"]["drop_missing"])
    ck(any("lrc_drop_lines" in m for m in b["missing"]), "漏号进 missing 清单")

    offline = PL.ensure_lrc({"title_guess": "压根没缓存"}, online=False)
    ck(offline["source"] == "none" and offline["text"] is None, "不联网时明确 none")


def test_bundle():
    print("[pvlink] 素材包")
    b = PL.bundle("2026-01-02", "小丑的品格", with_source=False)
    for k in ("date", "seq", "title", "fetch_title", "artist", "window", "source",
              "cover", "lrc", "assets", "style_ok", "missing", "ready", "notes"):
        ck(k in b, "字段 %s" % k)
    ck(b["cover"]["exists"], "封面存在")
    ck(b["style_ok"] == {"live": False, "jz": False}, "没素材时明确说没有")
    ck(not b["ready"], "有漏号（drop 9）时不算完全就绪")
    b2 = PL.bundle("2026-01-02", "带我走", with_source=False)
    ck(b2["ready"], "窗口 + 歌词齐 → ready")
    ck(b2["match"] == "exact", "匹配方式被带出来（可溯源）")


def test_geom():
    print("[pvlink] 画框几何与序列名")
    a = PL.geometry("pv")
    b = PL.geometry("pv")
    a["head"]["x"] = 1
    ck(b["head"]["x"] == 620, "返回的是深拷贝（改一份不影响另一份）")
    ck(PL.geometry("pv")["head"]["s"] != PL.geometry("player")["head"]["s"],
       "两套预设头窗尺寸确实不同（否则正是 §94 说的『以为统一了』）")
    ck(raises(lambda: PL.geometry("nope"), PL.PvLinkError) is not None, "未知预设报错")
    names = PL.sequence_names({"face": {"cx": 0.5}, "cut_start": 1.0,
                               "s1": {"dir": "a", "n": 3}, "s2": {"dir": "b", "n": 1},
                               "s3": {"dir": "", "n": 0}})
    ck(names == ["s1", "s2"], "序列名现读、跳过 face/cut_start/空目录", names)


def test_source():
    print("[pvlink] 录播源")
    err = raises(lambda: PL.source("2026-01-02"), PL.PvLinkError)
    ck(err is not None and "recording_root" in str(err), "没配 recording_root 时说清楚")
    p = os.path.join(PL.dirs()["workdir"], "fake.mp4")
    open(p, "wb").write(b"0")
    ck(PL.source("2026-01-02", explicit=p) == os.path.abspath(p), "显式 --src 直接采用")
    rt = PL.runtime()
    ck(set(("ffmpeg", "ffprobe", "node", "node_path", "python")) <= set(rt),
       "runtime 字段齐")


def main():
    tmp = tempfile.mkdtemp(prefix="pvlink_")
    setup_cfg(tmp)
    make_workdir(tmp)
    print("临时 workdir：%s" % tmp)
    for fn in (test_norm, test_find, test_window, test_lrc, test_bundle,
               test_geom, test_source):
        fn()
    print()
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
