#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""准备阶段（渲染之前）的提速与闸门 —— 回归测试

覆盖 2026-10-09 的三项改动：
  ① 全库粗筛改为「本地歌词直读 + 歌手复核」，脏缓存才走联网（自愈）；
  ② 装饰立绘 dataURL 按进程缓存（2.2 MB/首 不再逐首重编码）；
  ③ 渲染前闸门：成片已可判定为废片时，在渲染前就拦下（省 14 min/首）。

跑法： python tests/test_prep_pipeline.py      （退出码 0 = 全过）
"""
import io
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import song_cutter as SC                      # noqa: E402
from songcut import lyrics_fetch as LF        # noqa: E402

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  → " + detail) if detail and not cond else ""))


# ── ① 本地歌词直读 ──────────────────────────────────────────────────────
def test_local_cached_lrc():
    import auto_cut as AC
    print("\n[①] 本地歌词直读（_local_cached_lrc）")
    wd = tempfile.mkdtemp(prefix="prep_")
    try:
        cache = os.path.join(wd, "_media_cache")
        os.makedirs(cache)
        good = "[00:01.00]第一行\n[00:05.00]第二行"
        # 1) 正常命中：歌手一致
        io.open(os.path.join(cache, "_lrc_%s.json" % LF.cache_key("晴天")), "w",
                encoding="utf-8").write(json.dumps(
                    {"lrc": good, "cover_path": None, "info": {"artist": "周杰伦"}}))
        ck("缓存有效且歌手一致 → 直读返回歌词",
           AC._local_cached_lrc("晴天", wd, "周杰伦") == good)
        # 2) 歌手不符 → 返回 None，交给 fetch_lrc 纠正（Blessing 事故回归）
        io.open(os.path.join(cache, "_lrc_%s.json" % LF.cache_key("反方向的钟")), "w",
                encoding="utf-8").write(json.dumps(
                    {"lrc": good, "cover_path": None, "info": {"artist": "乐乐仔"}}))
        ck("缓存是翻唱版（歌手不符）→ 返回 None 走纠正路径",
           AC._local_cached_lrc("反方向的钟", wd, "周杰伦") is None)
        # 3) 文件不存在 → None
        ck("无缓存文件 → 返回 None", AC._local_cached_lrc("不存在的歌", wd, "某某") is None)
        # 4) 缓存无歌词 → None
        io.open(os.path.join(cache, "_lrc_%s.json" % LF.cache_key("空歌词")), "w",
                encoding="utf-8").write(json.dumps({"lrc": "", "info": {}}))
        ck("缓存里歌词为空 → 返回 None", AC._local_cached_lrc("空歌词", wd, "") is None)
        # 5) 缓存无 artist 字段时从 lyrics_source 反解
        io.open(os.path.join(cache, "_lrc_%s.json" % LF.cache_key("旧缓存")), "w",
                encoding="utf-8").write(json.dumps(
                    {"lrc": good, "info": {"lyrics_source": "netease:旧缓存(周杰伦)"}}))
        ck("旧缓存无 artist 字段 → 从 lyrics_source 反解歌手",
           AC._local_cached_lrc("旧缓存", wd, "周杰伦") == good)
        # 6) 曲库没给歌手时不误杀（放行）
        ck("曲库未提供歌手 → 不误杀，放行直读",
           AC._local_cached_lrc("晴天", wd, "") == good)
        # 7) 缓存损坏 → None（不抛异常）
        io.open(os.path.join(cache, "_lrc_%s.json" % LF.cache_key("损坏")), "w",
                encoding="utf-8").write("{不是合法JSON")
        ck("缓存 JSON 损坏 → 返回 None 且不抛异常",
           AC._local_cached_lrc("损坏", wd, "") is None)
    finally:
        shutil.rmtree(wd, ignore_errors=True)

    # 8) 真实缓存下的命中率与速度（不联网的那部分）
    real = os.path.join(_ROOT, "_media_cache")
    if os.path.isdir(real):
        n = sum(1 for f in os.listdir(real) if f.startswith("_lrc_") and f.endswith(".json"))
        ck("本地歌词缓存可用（%d 首）→ 粗筛免联网" % n, n > 500)
    else:
        print("  · 跳过：无 _media_cache 目录")


# ── ② 立绘 dataURL 缓存 ─────────────────────────────────────────────────
def test_art_cache():
    print("\n[②] 装饰立绘 dataURL 按进程缓存")
    SC._ART_DATAURL_CACHE["done"] = False
    SC._ART_DATAURL_CACHE["val"] = None
    a = SC._art_dataurl()
    b = SC._art_dataurl()
    if a is None:
        print("  · 跳过：未配置装饰立绘（STREAMER_ART 为空或缺失）")
        return
    ck("两次调用返回同一对象（未重复 base64 编码 2.2 MB）", a is b)
    ck("缓存标记为已完成", SC._ART_DATAURL_CACHE["done"] is True)
    ck("返回值为 dataURL 前缀", a.startswith("data:image/"))


# ── ③ 渲染前闸门 ────────────────────────────────────────────────────────
def test_pre_render_gate():
    print("\n[③] 渲染前闸门（_pre_render_gate）")
    g = SC._pre_render_gate
    base = {"netease_duration_ms": 255900, "lyrics_coverage": 1.0}
    lrc = "[00:01.00]啊"

    # 正常片：任何一项都不该拦
    f, w = g(255.0, lrc, base, "dtw", 1.0, "2026-10-06", "测试")
    ck("正常片 → 不拦不告警", not f and not w, "f=%s w=%s" % (f, w))

    # G1 片段过短
    f, _ = g(20.0, lrc, base, "raw", None, "d", "x")
    ck("G1 片段 < %ds → 拦截" % SC.MIN_SEC, len(f) == 1 and f[0].startswith("G1"))

    # G2 无歌词（默认拦）
    SC.GATE_ALLOW_NO_LRC = False
    f, _ = g(255.0, "", base, "raw", None, "d", "x")
    ck("G2 无歌词 → 拦截（铁律：不出无字幕片）",
       len(f) == 1 and f[0].startswith("G2"), str(f))
    # --allow-no-lrc 放行
    SC.GATE_ALLOW_NO_LRC = True
    f, w = g(255.0, "", base, "raw", None, "d", "x")
    ck("G2 + --allow-no-lrc → 降级为警告", not f and any(x.startswith("G2") for x in w))
    SC.GATE_ALLOW_NO_LRC = False

    # G3 原曲时长缺失（2026-10-08《反方向的钟》切掉一半的事故）
    f, _ = g(255.0, lrc, {}, "raw", None, "d", "x")
    ck("G3 原曲时长缺失 → 拦截（严禁猜测）",
       len(f) == 1 and f[0].startswith("G3"), str(f))

    # G3 超可信度（+35%）
    f, _ = g(360.0, lrc, {"netease_duration_ms": 200000}, "raw", None, "d", "x")
    ck("G3 成片比原曲长 80% → 拦截（疑 orig 取错版本）",
       len(f) == 1 and f[0].startswith("G3"), str(f))

    # G3 略超合理区间 → 只警告
    f, w = g(230.0, lrc, {"netease_duration_ms": 200000}, "raw", None, "d", "x")
    ck("G3 偏差 +15% → 仅警告不拦", not f and any(x.startswith("G3") for x in w))

    # G3 偏短 → 只警告（前奏被掐的可能性，交给人工判断）
    f, w = g(180.0, lrc, {"netease_duration_ms": 200000}, "raw", None, "d", "x")
    ck("G3 偏差 −10% → 仅警告不拦", not f and any(x.startswith("G3") for x in w))

    # G4 歌词残缺 → 警告
    f, w = g(200.0, lrc, {"netease_duration_ms": 200000, "lyrics_coverage": 0.5,
                          "lyrics_tail_sec": 100.0}, "raw", 0.5, "d", "x")
    ck("G4 歌词完整性 50% → 警告（提示复核 outro）",
       not f and any(x.startswith("G4") for x in w))

    # 多条硬伤同时成立 → 全部列出（一次看全，不用改一遍跑一遍）
    f, _ = g(20.0, "", {}, "raw", None, "d", "x")
    ck("多条硬伤 → 一次性全部列出", len(f) >= 3, str(len(f)))

    # 常量自洽
    ck("可信度上限 0 < DUR_TRUST_MAX < 1", 0 < SC.DUR_TRUST_MAX < 1)
    ck("合理区间下限 < 上限", SC.DUR_LOWER < SC.DUR_UPPER)


# ── ④ 逐首不再重复解析 SRT ──────────────────────────────────────────────
def test_srt_parsed_once():
    print("\n[④] 整场 SRT 只解析一次")
    src = io.open(os.path.join(_ROOT, "song_cutter.py"), encoding="utf-8").read()
    i = src.find("for idx, seg in enumerate(songs, 1):")
    ck("找到逐首循环", i > 0)
    body = src[i:i + 6000]                    # 循环体内
    head = src[max(0, i - 2500):i]            # 循环体之前
    ck("逐首循环内不再调用 parse_srt", "parse_srt(" not in body)
    ck("循环外已预解析 srt_entries（本场共用）",
       "srt_entries = parse_srt(srt_path)" in head)


def main():
    print("=== 准备阶段提速 / 闸门 回归测试 ===")
    test_local_cached_lrc()
    test_art_cache()
    test_pre_render_gate()
    test_srt_parsed_once()
    print("\n=== 结果：%d 通过 / %d 失败 ===" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
