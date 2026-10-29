#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""字体落点检查：查「每个字最终由哪个字族画出」，专治「一个字宋体、其他黑体」。

为什么需要它：
  浏览器的字形回退是**逐字**的。字族栈里前一个字体没有某个字形，就会静默滑到
  下一个，最后滑到泛型（monospace / sans-serif）——此时由系统默认字体接管，
  简体中文环境下 monospace 通常落宋体。于是同一个词里会出现两种字体，
  而 canvas 不报错、截图不放大也看不出来。

判定依据（硬事实，不依赖渲染）：
  ① 解析 CSS 拿到每个 family 的实际 cmap（字体文件本身有哪些码位）；
  ② 解析引擎里所有 `ctx.font` 的栈；
  ③ 对给定文本逐字走栈，得到「落点 family」；
  ④ 落点是泛型关键字 ⇒ FAIL；同一段文字里落点跨 family ⇒ FAIL（混排）。

用法：
  python tools/pv_fontcheck.py                       # 用默认文本（片头卡 + 曲名）
  python tools/pv_fontcheck.py --lrc archive/.../x.lrc
  python tools/pv_fontcheck.py --text "VOCAL 岁己SUI · 20261027"
  python tools/pv_fontcheck.py --sb _tmp/pv/sb.json   # 编排稿（若还在）
"""
import argparse
import glob
import os
import re
import sys

from fontTools.ttLib import TTFont     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS_FILES = [
    "renderer/player/pv_fonts/fonts.css",
    "renderer/player/fonts/fonts.css",
]
JS_GLOB = "renderer/player/pv/*.js"
GENERIC = {"monospace", "sans-serif", "serif", "system-ui", "cursive", "fantasy"}

_cmap_cache = {}


def cmap_of(path, broken):
    if path in _cmap_cache:
        return _cmap_cache[path]
    cp = set()
    try:
        if os.path.getsize(path) == 0:
            raise ValueError("0 字节")
        t = TTFont(path, fontNumber=0, lazy=True)
        cp = set(t.getBestCmap().keys())
        t.close()
    except Exception as e:
        broken.append((os.path.relpath(path, ROOT).replace("\\", "/"), str(e)))
    _cmap_cache[path] = cp
    return cp


def load_css(broken):
    """(family, weight) -> 码位；family -> 码位（各档并集）。unicode-range 由文件真实 cmap 兜住。"""
    fam, famw = {}, {}
    seen = set()
    for rel in CSS_FILES:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            continue
        base = os.path.dirname(p)
        css = open(p, encoding="utf-8").read()
        for blk in re.findall(r"@font-face\s*\{(.*?)\}", css, re.S):
            mf = re.search(r"font-family: *['\"]([^'\"]+)['\"]", blk)
            mu = re.search(r"url\(([^)]+)\)", blk)
            mw = re.search(r"font-weight: *(\d+)", blk)
            if not (mf and mu):
                continue
            name = mf.group(1)
            w = int(mw.group(1)) if mw else 400
            path = os.path.join(base, mu.group(1).strip().strip("'\""))
            path = os.path.normpath(path)
            # 去重键必须含字重：Google 的可变字体切片会让 400/500/700/900 指向**同一个** woff2
            # （实测 408 个 @font-face 只有 105 个文件），只按路径去重会把后面的字重档吞掉。
            key = (path, name, w)
            if key in seen:
                continue
            seen.add(key)
            cov = cmap_of(path, broken)
            famw.setdefault((name, w), set()).update(cov)
            fam.setdefault(name, set()).update(cov)
    return fam, famw


def load_stacks():
    """从引擎里抓 ctx.font 的栈：[(位置, [family...], 末尾泛型 or None)]"""
    out = []
    for f in sorted(glob.glob(os.path.join(ROOT, JS_GLOB))):
        for i, line in enumerate(open(f, encoding="utf-8"), 1):
            if "ctx.font" not in line:
                continue
            # 字族名在这些栈里一律是双引号包起来的；不能用 ['"] 交替，
            # 否则 `'px "Zen Kaku…'` 会先把 "px " 当成名字、把后面那个名字的开引号吃掉。
            fams = [x.strip() for x in re.findall(r'"([^"]+)"', line)]
            fams = [x for x in fams if x and x.lower() not in GENERIC]
            gen = None
            tail = re.search(r"(monospace|sans-serif|serif|system-ui)\s*['\"`]?\s*;?\s*$", line.strip())
            if tail:
                gen = tail.group(1)
            if fams:
                out.append(("%s:%d" % (os.path.basename(f), i), fams, gen))
    return out


def resolve(ch, fams, famc, famw, weight):
    """返回 (落点, 是否跨过缺字, 字重告警)。落点 '(泛型)' 表示交给系统字体。"""
    for f in fams:
        if ord(ch) in famw.get((f, weight), ()):
            return f, f != fams[0], None
        if ord(ch) in famc.get(f, ()):
            return f, f != fams[0], "%s 无 w%d，浏览器会合成/换档" % (f, weight)
    return "(泛型)", True, None


def default_text():
    lrc = sorted(glob.glob(os.path.join(ROOT, "archive/*/notes/*.lrc")))
    txt = ["VOCAL 岁己SUI · 20261027", "Innocence", "ORIGINAL SONG", "Avril Lavigne"]
    for p in lrc[:1]:
        for ln in open(p, encoding="utf-8", errors="ignore"):
            ln = ln.strip()
            if ln and not ln.startswith("["):
                txt.append(ln)
    return txt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="")
    ap.add_argument("--lrc", default="")
    ap.add_argument("--sb", default="")
    a = ap.parse_args()

    if a.text:
        lines = [a.text]
    elif a.lrc:
        lines = [ln.strip() for ln in open(os.path.join(ROOT, a.lrc), encoding="utf-8", errors="ignore")
                 if ln.strip() and not ln.startswith("[")]
    elif a.sb:
        import json
        sb = json.load(open(os.path.join(ROOT, a.sb), encoding="utf-8"))
        lines = []
        for s in sb["shots"]:
            if s.get("text"):
                lines.append(s["text"])
            lay = s.get("layout") or {}
            for k in ("kicker", "artist", "meta", "tag"):
                if lay.get(k):
                    lines.append(str(lay[k]))
    else:
        lines = default_text()

    broken = []
    coverage, covw = load_css(broken)
    stacks = load_stacks()

    print("[css] 字族 %d 个：%s" % (len(coverage), ", ".join(sorted(coverage))))
    print("[js ] font 栈 %d 条" % len(stacks))
    if broken:
        print("[broken] 字体文件损坏 %d 个：" % len(broken))
        for p, e in broken:
            print("    %-52s %s" % (p, e))
    else:
        print("[broken] 无")

    cjk = lambda s: [c for c in s if ord(c) > 0x2E80 and not c.isspace()]  # noqa: E731
    bad_generic, bad_mix, bad_weight, detail = [], [], [], []
    for loc, fams, gen in stacks:
        line_src = open(os.path.join(ROOT, "renderer/player/pv", loc.split(":")[0]), encoding="utf-8").readlines()
        raw = line_src[int(loc.split(":")[1]) - 1]
        mw = re.search(r"\|\|\s*(\d{3})", raw) or re.search(r"^[^\d]*['\"`]?(\d{3})\s", raw)
        weight = int(mw.group(1)) if mw else 400
        for ln in lines:
            chars = cjk(ln)
            if not chars:
                continue
            hits = {}
            for ch in chars:
                where, fell, ww = resolve(ch, fams, coverage, covw, weight)
                hits.setdefault(where, []).append(ch)
                if ww:
                    bad_weight.append((loc, ch, ww))
            if "(泛型)" in hits:
                bad_generic.append((loc, ln, "".join(hits["(泛型)"])))
            if len(hits) > 1:
                bad_mix.append((loc, ln, {k: "".join(v) for k, v in hits.items()}))
            detail.append((loc, fams, ln, hits))

    print("\n=== 逐条栈的落点（只列含中文的行）===")
    shown = 0
    for loc, fams, ln, hits in detail:
        if shown >= 12:
            break
        shown += 1
        print("  %-14s %-46s -> %s" % (loc, " / ".join(fams)[:46],
              ", ".join("%s:%s" % (k, "".join(v)) for k, v in hits.items())))

    print("\n=== 判定 ===")
    if bad_generic:
        print("[FAIL] 有中文落到泛型（系统字体接管，通常是宋体）%d 处：" % len(bad_generic))
        seen = set()
        for loc, ln, chs in bad_generic:
            k = (loc, chs)
            if k in seen:
                continue
            seen.add(k)
            print("    %-14s 「%s」 例：%s" % (loc, chs, ln[:30]))
    if bad_mix:
        print("[FAIL] 同一行内落点跨字族（混排）%d 处：" % len(bad_mix))
        for loc, ln, h in bad_mix[:6]:
            print("    %-14s %s  |  %s" % (loc, ln[:26], {k: "".join(v) for k, v in h.items()}))
    if bad_weight:
        print("[WARN] 字重缺档（同一 run 内会出现字重不一致）%d 处：" % len(bad_weight))
        for loc, ch, ww in bad_weight[:6]:
            print("    %-14s 「%s」 %s" % (loc, ch, ww))

    ok = not bad_generic and not bad_mix
    print("\n结论：%s" % ("全部通过" if ok else "不通过"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
