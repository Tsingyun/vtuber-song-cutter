# -*- coding: utf-8 -*-
"""中文族字体体检：注册了没有 / 文件在不在 / 常用字缺不缺 / 许可证在不在。

为什么要有这一支：
  中文字体最容易出的不是「报错」而是「静默」——
  ① PV.FACES 里写了 family，pv_fonts/ 里却没有这个族 → 渲染时落到 Noto，没人报错；
  ② CSS 有、字库缺字（展示体常只收 3500 常用字）→ 该字露出宋体，同句两种字形；
  ③ 字体进了库、许可证没进 → 分发即违规（OFL 要求随字体附带）。
  这三条都能在出片前用文件本身判出来，不必等看到画面。

用法：
  python tools/pv_fonts_audit.py
  python tools/pv_fonts_audit.py --text "自定义取样文字"
"""
import argparse
import io
import os
import re
import sys

from fontTools.ttLib import TTFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 两份都要解析：pv_fonts/ 是新抓的拉丁+中文族，player/fonts/ 里装着播放器自带的
# Noto Sans SC（cnSans 三档就是它）。只看前者会把 cnSans 判成「CSS 里没有」。
CSS_FILES = [
    os.path.join(ROOT, "renderer", "player", "pv_fonts", "fonts.css"),
    os.path.join(ROOT, "renderer", "player", "fonts", "fonts.css"),
]
CSS = CSS_FILES[0]
TYPE_JS = os.path.join(ROOT, "renderer", "player", "pv", "pv_type.js")

# 取样：歌词真会用到的字 + 标点。展示体（马善政/龙藏/柳建毛草）普遍只收
# 3500~7000 字，缺的是生僻字；这一段覆盖「情歌常用」，缺一个都要知道。
SAMPLE = (
    "我你他她它们爱恨喜欢想念忘记等待离开回来再见永远刹那瞬间"
    "夜雨风花雪月春夏秋冬城市街道路灯车窗房间屋顶天空海洋山川"
    "时间记忆故事名字声音微笑眼泪心跳呼吸温柔孤单寂寞自由答案"
    "为什么怎么会就这样一直还是已经变得成为知道觉得希望害怕"
    "梦想世界明天昨天今天此刻从前以后最初最后所有一些每个"
    "心跳停止呼吸冷却月光洒落影子拉长掌心温度写下诗句远方"
    "岁己SUI 0123456789，。、！？…—·「」『』《》（）：；"
)
SAMPLE = "".join(dict.fromkeys(SAMPLE))          # 去重、保序

_cmap_cache = {}


def cmap_of(path):
    if path in _cmap_cache:
        return _cmap_cache[path]
    cp = set()
    if os.path.getsize(path) > 0:
        t = TTFont(path, fontNumber=0, lazy=True)
        try:
            cp = set(t.getBestCmap().keys())
        finally:
            t.close()
    _cmap_cache[path] = cp
    return cp


def parse_css():
    """family -> {weight: [(abs_path, set(codepoints))]}"""
    out = {}
    blocks = []
    for fp in CSS_FILES:
        if os.path.exists(fp):
            blocks += re.findall(r"@font-face\s*\{[^}]*\}",
                                 io.open(fp, encoding="utf-8").read())
    for blk in blocks:
        fam = re.search(r"font-family:\s*'([^']+)'", blk)
        wt = re.search(r"font-weight:\s*(\d+)", blk)
        url = re.search(r"url\(([^)]+)\)", blk)
        if not (fam and url):
            continue
        # url() 是相对**各自那份 css** 的；汇总文件在 pv_fonts/，分片在 pv_fonts/<slug>/
        rel = url.group(1).replace("/", os.sep)
        p = os.path.join(ROOT, "renderer", "player", "pv_fonts", rel)
        if not os.path.exists(p):
            p2 = os.path.join(ROOT, "renderer", "player", "fonts", rel)
            if os.path.exists(p2):
                p = p2
        out.setdefault(fam.group(1), {}).setdefault(int(wt.group(1)) if wt else 400, []).append(
            (p, cmap_of(p)))
    return out


def parse_faces():
    """从 pv_type.js 里抠出 FACES 表（正则足够：格式是自己写的，一行一条）。"""
    js = io.open(TYPE_JS, encoding="utf-8").read()
    body = js[js.index("PV.FACES = {"):]
    body = body[:body.index("\n  };")]
    faces = []
    for m in re.finditer(r"(\w+):\s*\{\s*family:\s*'\"([^\"]+)\"'\s*,\s*weight:\s*(\d+)"
                         r"([^}]*)\}", body):
        key, fam, wt, rest = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        faces.append({"key": key, "family": fam, "weight": wt,
                      "cjk": "cjk: true" in rest})
    return faces


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=SAMPLE)
    a = ap.parse_args()

    if not os.path.exists(CSS):
        print("[FAIL] 没有 %s，先跑 tools/pv_fetch_fonts.py" % os.path.relpath(CSS, ROOT))
        return 1
    css = parse_css()
    faces = [f for f in parse_faces() if f["cjk"]]
    print("[fonts] 注册的中文字面 %d 个，取样 %d 字\n" % (len(faces), len(a.text)))

    bad = 0
    print("  %-11s %-22s %-5s %6s  %-7s %s" % ("key", "family", "wt", "覆盖", "许可证", "缺字"))
    for f in faces:
        fam = css.get(f["family"])
        if not fam:
            print("  %-11s %-22s %-5d %6s  %-7s %s"
                  % (f["key"], f["family"], f["weight"], "-", "-", "CSS 里没有这个族（会静默 fallback）"))
            bad += 1
            continue
        chunks = fam.get(f["weight"])
        if not chunks:
            print("  %-11s %-22s %-5d %6s  %-7s %s"
                  % (f["key"], f["family"], f["weight"], "-", "-",
                     "CSS 里有族但没有 %d 这一档" % f["weight"]))
            bad += 1
            continue
        cov = set()
        for _, c in chunks:
            cov |= c
        miss = "".join(ch for ch in a.text if ord(ch) not in cov)
        # 只把「缺汉字」算问题。展示体（马善政/龙藏/柳建毛草/庆科黄油）普遍没有
        # 中圆点 · 这类西文标点，落回 Noto 是对的 —— 出片看不出接缝，不算缺陷。
        miss_cjk = "".join(ch for ch in miss if "\u4e00" <= ch <= "\u9fff")
        rate = 1 - len(miss_cjk) / max(1, sum(1 for ch in a.text if "\u4e00" <= ch <= "\u9fff"))
        # 许可证：族目录下任一带 OFL/LICENSE 字样的文本
        d = os.path.dirname(chunks[0][0])
        lic = [n for n in os.listdir(d) if re.match(r"(?i)(ofl|license)", n)]
        flag = ""
        if rate < 0.995:
            flag = "缺汉字"
            bad += 1
        if not lic:
            flag = (flag + " / 无许可证").strip(" /")
            bad += 1
        note = (("缺 " + miss_cjk) if miss_cjk else "") + (" " + flag if flag else "")
        print("  %-11s %-22s %-5d %6.2f%%  %-7s %s"
              % (f["key"], f["family"], f["weight"], rate * 100,
                 "有" if lic else "无", note.strip() or "—"))

    print()
    if bad:
        print("[FAIL] %d 处问题" % bad)
        return 1
    print("[OK] 全部中文字面：CSS 齐全、常用字覆盖完整、许可证随字体分发")
    return 0


if __name__ == "__main__":
    sys.exit(main())
