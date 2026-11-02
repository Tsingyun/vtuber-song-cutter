# -*- coding: utf-8 -*-
"""风格可用性自检：说一句「用 B 风格」之前，先确认那套模板真的能装起来。

为什么需要它
------------
11-01 的事故是**静默降级**：命令里少一个参数，版式就退回 v9，但画面依然「看起来正常」，
没有任何报错。这类问题不能靠出片后肉眼发现，必须在动手前用一次静态检查挡住。

它只读文件、不渲染、不改任何东西：
  · 版式页是否存在
  · B 风格依赖的 pv_arc.js 是否真的被 index_pv.html 引用
  · 按 cuts/<date>/<歌名>/{live,jz}/ 约定探测素材
  · 逐层给出「有 / 无 / 会降级」

用法：
  python tools/style_check.py --style B
  python tools/style_check.py --style B --date 2026-11-01 --song 小丑的品格
"""
import argparse
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from songcut import style as S  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", default=None, help="A / B / 唱片 / 手书 / record / lettering")
    ap.add_argument("--date", default="", help="直播日期 YYYY-MM-DD（查素材用）")
    ap.add_argument("--song", default="", help="歌名（查素材用）")
    a = ap.parse_args()

    try:
        st = S.resolve(a.style)
    except S.StyleError as e:
        raise SystemExit(str(e))

    player = os.path.join(ROOT, "renderer", "player")
    page = os.path.join(player, st["page"])
    ok_page = os.path.exists(page)

    print("风格 %s（%s / %s）" % (st["key"], st["name"], st["en"]))
    print("  说明：%s" % st["desc"])
    print("  版式：%s  %s" % (st["page"], "✓ 存在" if ok_page else "✗ 缺失"))

    if st["pv"]:
        html = io.open(page, encoding="utf-8").read() if ok_page else ""
        ok_arc = "pv/pv_arc.js" in html
        print("  弧线：%s  %s" % ("开" if st["arc"] else "关",
                                  ("✓ 页面已引用 pv_arc.js" if ok_arc else "✗ 页面未引用 pv_arc.js")
                                  if st["arc"] else ""))

    misses = []
    if a.date and a.song:
        out_dir = os.path.join(ROOT, "cuts", a.date)
        live, jz = S.asset_paths(out_dir, a.song)
        print("  素材（%s / %s）：" % (a.date, a.song))
        for label, want, p in (("人物", st["live"], live), ("JIZURA", st["jz"], jz)):
            if not want:
                print("    %-6s 该风格不使用" % label)
                continue
            if p:
                print("    %-6s ✓ %s" % (label, p))
            else:
                print("    %-6s ✗ 未找到（本片将缺这一层）" % label)
                misses.append(label)
    elif st["live"] or st["jz"]:
        print("  素材：未给 --date/--song，跳过探测")

    bad = (not ok_page) or bool(misses)
    print("\n结论：%s" % ("可以直接用" if not bad else
                        "可用但会降级（缺 %s）" % "、".join(misses) if ok_page else "不可用：版式缺失"))
    return 1 if not ok_page else 0


if __name__ == "__main__":
    sys.exit(main())
