# -*- coding: utf-8 -*-
"""从当天 SRT 里定位歌词与演唱区间。

歌切第一步永远是「在转写里找到这首歌」。手写一次性脚本做过三轮，
每次都要重敲 SRT 解析 + 秒数格式化，故提升为工具。

用法：
    # 按关键词找提及
    python tools/srt_ctx.py --date 2026-11-01 --grep 小丑的品格 带我走

    # 打印某段时间的连续转写（分钟为单位）
    python tools/srt_ctx.py --date 2026-11-01 --range 142 152

    # 直接给绝对秒（粘贴到 segments / ffmpeg 里用）
    python tools/srt_ctx.py --date 2026-11-01 --range 142 143 --abs
"""
import argparse
import glob
import os
import re

DEFAULT_SRT_DIR = r"D:/岁己直播自动总结/SRT"
RE_TC = re.compile(r"(\d+):(\d+):(\d+),(\d+)\s*-->\s*(\d+):(\d+):(\d+),(\d+)")


def parse_srt(path):
    """→ [(t0, t1, text)]，秒为浮点。"""
    raw = open(path, encoding="utf-8-sig").read().strip()
    items = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [x for x in block.split("\n") if x.strip()]
        if len(lines) < 2:
            continue
        m = RE_TC.match(lines[1])
        if not m:
            continue
        g = list(map(int, m.groups()))
        t0 = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        t1 = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        items.append((t0, t1, " ".join(lines[2:]).strip()))
    return items


def find_srt(date=None, srt=None, srt_dir=DEFAULT_SRT_DIR):
    if srt:
        if not os.path.exists(srt):
            raise SystemExit("SRT 不存在: %s" % srt)
        return srt
    key = (date or "").replace("-", "")
    cands = sorted(glob.glob(os.path.join(srt_dir, "*%s*.srt" % key))) if key else []
    if not cands:
        raise SystemExit("在 %s 下找不到 %s 的 SRT（用 --srt 指定）" % (srt_dir, date))
    return cands[-1]


def fmt(t):
    return "%02d:%05.2f" % (int(t // 60), t % 60)


def main():
    ap = argparse.ArgumentParser(description="SRT 转写检索 / 定位工具")
    ap.add_argument("--date", help="YYYY-MM-DD，自动在 SRT 目录里找")
    ap.add_argument("--srt", help="直接指定 SRT 路径")
    ap.add_argument("--srt-dir", default=DEFAULT_SRT_DIR)
    ap.add_argument("--grep", nargs="+", metavar="KW", help="关键词（子串匹配）")
    ap.add_argument("--range", nargs=2, metavar=("A", "B"), type=float, help="时间区间，单位分钟")
    ap.add_argument("--abs", action="store_true", help="同时打印绝对秒")
    ap.add_argument("--width", type=int, default=110, help="正文截断宽度")
    a = ap.parse_args()

    path = find_srt(a.date, a.srt, a.srt_dir)
    items = parse_srt(path)
    print("SRT: %s  （%d 条，%s → %s）" % (os.path.basename(path), len(items),
                                        fmt(items[0][0]) if items else "-",
                                        fmt(items[-1][1]) if items else "-"))

    if a.grep:
        for kw in a.grep:
            print("\n===== 提及「%s」 =====" % kw)
            hit = [(i, x) for i, x in enumerate(items) if kw in x[2]]
            if not hit:
                print("  （无）")
            for i, (t0, _t1, txt) in hit:
                extra = "  abs=%.2fs" % t0 if a.abs else ""
                print("  [%4d] %s  %s%s" % (i, fmt(t0), txt[:a.width], extra))

    if a.range:
        A, B = a.range[0] * 60.0, a.range[1] * 60.0
        print("\n===== %s ~ %s =====" % (fmt(A), fmt(B)))
        for i, (t0, t1, txt) in enumerate(items):
            if B >= t0 and A <= t1:
                extra = "  abs=%.2fs-%.2fs" % (t0, t1) if a.abs else ""
                print("  [%4d] %s-%s | %s%s" % (i, fmt(t0), fmt(t1), txt[:a.width], extra))


if __name__ == "__main__":
    main()
