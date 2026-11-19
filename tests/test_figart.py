# -*- coding: utf-8 -*-
"""立绘素材库（songcut/figart.py）的回归测试。

跑法：
    python tests/test_figart.py
    python -m pytest tests/test_figart.py -q

锁住什么
--------
这个模块只做「盘点 / 挑图 / 解析名字」，看起来全是 IO，但真正会出错的地方都在
**判定**上，而且都不会抛异常 —— 只会挑错图、或者挑到一张没有透明区的图，
成片里表现为「贴了一张方形贴纸」，最难发现。所以这里全部用临时目录造图来验：

  1. 分类按**人物包围盒**的长宽比，不是画布（画布常是整块透明纸）
  2. 满幅不透明的图必须能被识别出来（穿插效果的必要条件是真 alpha）
  3. 缝隙数只在 bbox 的胸部带里统计，整张透明纸不能凭空刷分
  4. #N 的编号与 resolve("#N") 必须一致（错位过一次，取到的是另一张图且不报错）
  5. 子串命中多张时必须报错并列出候选，不许猜
  6. 目录不存在时不崩，返回空库
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS = []


def ck(cond, name, detail=""):
    if cond:
        print("  ✓ %s" % name)
    else:
        print("  ✗ %s %s" % (name, detail))
        FAILS.append(name)


def make(path, w, h, box=None, mode="alpha"):
    """造一张测试图。box=None ⇒ 满幅不透明（用来验「没透明区」）；"""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if box is None:
        d.rectangle([0, 0, w - 1, h - 1], fill=(220, 60, 60, 255))
    else:
        d.rectangle(box, fill=(220, 60, 60, 255))
        if mode == "gaps":
            # 在胸部带挖两条竖缝：模拟发缝 / 羽毛间隙
            x0, y0, x1, y1 = box
            for k in (0.35, 0.65):
                xm = int(x0 + (x1 - x0) * k)
                # 缝必须贯穿整个人物高度：只在中间一段挖的话，采样线有一部分
                # 落在缝外面，均值会被稀释成 2 左右 —— 指标就测不到「有缝」这件事了。
                d.rectangle([xm - 6, y0, xm + 6, y1 - 1], fill=(0, 0, 0, 0))
    im.save(path)
    return path


def main():
    from songcut import figart

    tmp = tempfile.mkdtemp(prefix="figart_")
    old_env = os.environ.get(figart.ENV_DIR)
    old_cache = figart.CACHE
    try:
        d = os.path.join(tmp, "art")
        os.makedirs(d)
        figart.CACHE = os.path.join(tmp, "cache.json")
        os.environ[figart.ENV_DIR] = d

        # 1) 胸像：600×900 画布，人物落在中间一竖条（bar≈0.55 → slim）
        make(os.path.join(d, "bust_gaps.png"), 600, 900, box=(180, 60, 420, 880), mode="gaps")
        # 2) 胸像：人物横向更宽（bar≈0.95 → bust）
        make(os.path.join(d, "bust_wide.png"), 700, 700, box=(40, 60, 660, 700))
        # 3) 横构图特写（bar≈1.5 → wide）
        make(os.path.join(d, "closeup.png"), 1200, 800, box=(100, 100, 1100, 760))
        # 4) 满幅不透明（没有 alpha）
        make(os.path.join(d, "opaque.png"), 800, 800, box=None)

        rows = figart.scan(force=True)
        by = {r["name"]: r for r in rows}
        ck(len(rows) == 4, "扫到 4 张", str(len(rows)))

        ck(by["bust_wide.png"]["kind"] == "bust",
           "宽胸像 → bust", by["bust_wide.png"]["kind"])
        ck(by["closeup.png"]["kind"] == "wide",
           "横构图特写 → wide", by["closeup.png"]["kind"])
        ck(by["bust_gaps.png"]["kind"] == "slim",
           "窄高人物 → slim", by["bust_gaps.png"]["kind"])

        ck(by["opaque.png"]["op"] > 0.97, "满幅图的不透明占比 ≈ 1",
           str(by["opaque.png"]["op"]))
        ck(by["bust_gaps.png"]["op"] < 0.5, "带透明区的图 op 明显 < 1",
           str(by["bust_gaps.png"]["op"]))

        # 两道竖缝 ⇒ 每条扫描线 4 处 实体↔透明 交界
        ck(by["bust_gaps.png"]["cuts"] >= 3.0,
           "竖缝能被数成交界", str(by["bust_gaps.png"]["cuts"]))
        ck(by["opaque.png"]["cuts"] == 0.0, "满幅图没有交界", str(by["opaque.png"]["cuts"]))

        # PIL 的 bbox 右/下是**开区间**，所以 420→421、880→881
        ck(by["bust_gaps.png"]["bbox"] == [180, 60, 421, 881],
           "alpha 包围盒 = 人物实际范围", str(by["bust_gaps.png"]["bbox"]))

        # 排序与 #N 的一致性：这是踩过的坑（list 按过滤后编号、resolve 按全量编号）
        ck(all("idx" in r for r in rows), "每条都带全局序号 idx")
        for r in rows:
            got = figart.resolve("#%d" % r["idx"])
            if os.path.abspath(got) != os.path.abspath(r["path"]):
                ck(False, "#%d 与列表序号一致" % r["idx"], got)
                break
        else:
            ck(True, "#N 与列表序号一一对应（4 张全覆盖）")

        # 精确名 / 子串 / 多义
        ck(os.path.basename(figart.resolve("closeup.png")) == "closeup.png",
           "精确文件名可解析")
        ck(os.path.basename(figart.resolve("bust_gap")) == "bust_gaps.png",
           "唯一子串可解析")
        try:
            figart.resolve("bust_")
            ck(False, "多义子串必须报错")
        except SystemExit as e:
            ck("命中" in str(e), "多义子串报错并列出候选", str(e)[:60])
        try:
            figart.resolve("不存在的东西")
            ck(False, "找不到必须报错")
        except SystemExit:
            ck(True, "找不到时报错（不静默返回空）")

        # pick：只挑带 alpha 的，不能把 opaque.png 挑出来
        p = figart.pick("bust")
        ck(p is not None and p["name"] != "opaque.png",
           "自动挑图不会挑到满幅不透明的图", p and p["name"])

        # 空目录 / 目录不存在
        os.environ[figart.ENV_DIR] = os.path.join(tmp, "nope")
        ck(figart.scan(force=True) == [], "目录不存在 → 空库且不崩")
        try:
            figart.resolve("auto")
            ck(False, "空库时 resolve 应报错")
        except SystemExit:
            ck(True, "空库时 resolve 给出可读错误")

        # pvlink 是唯一读口：转发必须真的通到 figart
        os.environ[figart.ENV_DIR] = d
        from songcut import pvlink
        pp, meta = pvlink.art("closeup.png")
        ck(os.path.basename(pp) == "closeup.png" and meta["w"] == 1200,
           "pvlink.art 能解析名字并带出参数")
    finally:
        if old_env is None:
            os.environ.pop(figart.ENV_DIR, None)
        else:
            os.environ[figart.ENV_DIR] = old_env
        figart.CACHE = old_cache
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print("FAIL %d 项：%s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
