# -*- coding: utf-8 -*-
"""播放器页面契约门禁。

背景（2026-10-30 复盘）：仓库里并存三套播放器页面，共用字体与素材，但渲染逻辑
各写一份。已经出过一次真实代价 —— 转场闪动修在 `pv/pv_trans.js` 里，而生产默认页
是 `index.html`，于是「修好了」只对另一条产线成立。

这里不做重构（合并三套页面是一次独立的大改），只守住两条**不会误伤**的契约：

  ① 每个页面必须在 <!doctype> 后声明 ROLE（它是什么、什么时候用）；
  ② `pv/*.js` 只能被 index_pv.html 引用 —— 否则等于又长出一套「共用引擎」，
     量具覆盖不到那个页面，下一次修 flash 又会漏。

用法：
    python tools/pv_pages.py          # 通过退出 0，失败退出 1
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYER = os.path.join(ROOT, "renderer", "player")
PAGES = ["index.html", "index_v10.html", "index_pv.html"]
PV_PAGE = "index_pv.html"

FAILS = []


def ck(cond, msg):
    if cond:
        print("  ✓ %s" % msg)
    else:
        print("  ✗ %s" % msg)
        FAILS.append(msg)


def main():
    print("[pages] 页面清单与角色声明")
    for name in PAGES:
        p = os.path.join(PLAYER, name)
        if not os.path.exists(p):
            ck(False, "%s 存在" % name)
            continue
        src = io.open(p, encoding="utf-8").read()
        head = src[:1200]
        ck("<!-- ROLE:" in head, "%s 声明了 ROLE" % name)

    print("[pages] pv 引擎的引用面")
    for name in PAGES:
        p = os.path.join(PLAYER, name)
        if not os.path.exists(p):
            continue
        src = io.open(p, encoding="utf-8").read()
        refs = set(re.findall(r'src="(pv/pv_[a-z_]+\.js)"', src))
        if name == PV_PAGE:
            ck(bool(refs), "%s 引用 pv/*.js（应有）" % name)
            # 引擎文件必须齐全：缺一个会在运行时报 undefined，而不是加载时报错
            for need in ("pv_core", "pv_bg", "pv_type", "pv_typo", "pv_parts",
                         "pv_piece", "pv_cam", "pv_trans", "pv_engine"):
                ck(any(need + ".js" in r for r in refs), "%s 载入 %s.js" % (name, need))
        else:
            ck(not refs, "%s 不引用 pv/*.js（避免长出第二套共用引擎）" % name)

    print("[pages] 引擎模块自洽")
    eng = os.path.join(PLAYER, "pv", "pv_engine.js")
    src = io.open(eng, encoding="utf-8").read()
    ck("window.PV.renderAt" in src or "PV.renderAt =" in src, "pv_engine 暴露 renderAt")
    ck("__komaOf" in io.open(os.path.join(PLAYER, PV_PAGE), encoding="utf-8").read(),
       "渲染页暴露 __komaOf（一格一编码依赖它）")
    ck("__tick" in io.open(os.path.join(PLAYER, PV_PAGE), encoding="utf-8").read(),
       "渲染页暴露 __tick（同格不重绘依赖它）")

    print()
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), "; ".join(FAILS)))
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
