# -*- coding: utf-8 -*-
"""岁己立绘素材库（只读）。

为什么改向（2026-11-03）：

原先的「人物穿插文字」走的是「从录播里抠人」—— 16:9 的直播画面裁成竖幅，
发丝/袖口的边缘本来就被码率和软编码糊掉了，抠出来一圈毛边；而且每一帧的姿势
都在动，抠像结果在帧间跳，成片里那几句读作「贴了一张抖动的人形」。

立绘不一样：它们**本来就有 alpha**，边缘是画师切好的。文字从她身后穿过去时，
会正好从发缝、羽翼的羽毛间隙里透出来 —— 这才是「穿插」成立的前提：
不是靠半透明把她压淡，而是靠她身上本来就有洞。

本模块只做三件事：盘点（尺寸 / 真实 alpha / 可穿透程度）、按用途挑图、把名字
解析成绝对路径。**不复制文件、不改原图**：素材是用户自己的图库，不是项目产物。

缓存落在 _cache/fig_art.json（按目录 mtime + 文件数失效），可以整删。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "_cache", "fig_art.json")
ENV_DIR = "SUI_ART_DIR"
DEFAULT_DIR = r"C:\Users\Tsing\Pictures\管人立绘\岁己立绘"
SCHEMA = 4


def art_dir():
    """素材目录。环境变量优先 —— 换一套立绘不该改代码。"""
    return os.path.abspath(os.environ.get(ENV_DIR) or DEFAULT_DIR)


def _stamp(d):
    try:
        st = os.stat(d)
    except OSError:
        return (0, 0)
    n = 0
    try:
        n = len([f for f in os.listdir(d) if f.lower().endswith(".png")])
    except OSError:
        pass
    return (int(st.st_mtime), n)


def _rows_of(alpha, y0, y1, x0, x1, n=9):
    """在 bbox 的「文字高度带」里量两条指标：**切分边界数**与**实心占比**。

    为什么是这两条（而不是「面积」「留白」）：
      · 文字是横着排的，能不能「穿过去」只取决于同一行上有几处 实体↔透明 的
        交界 —— 每一处交界都会把一串字切开一刀。发缝、羽毛间隙、手臂与身体
        之间的空档，在扫描线上都表现为一对交界。
      · 但只有交界也不行：整张图都是碎条的时候，字全从缝里漏过去，读作「没挡住」。
        所以还要一条实心占比，理想值 ≈ 0.55（一半遮、一半透）。

    返回 (平均交界数, 平均实心占比)。0.28 而不是 32：立绘的发丝边缘是半透明的，
    门槛抬高会把真实存在的缝判成实体，缝就被「填」掉了。
    """
    import numpy as np
    a = np.asarray(alpha)
    cuts, mass = [], []
    for i in range(n):
        y = int(y0 + (y1 - y0) * (0.26 + 0.52 * i / max(1, n - 1)))
        y = max(0, min(a.shape[0] - 1, y))
        row = a[y, x0:x1] >= 28
        if row.size < 8:
            continue
        cuts.append(int(np.count_nonzero(np.diff(row.astype(np.int8)))))
        mass.append(float(row.mean()))
    if not cuts:
        return (0.0, 1.0)
    return (round(sum(cuts) / len(cuts), 2), round(sum(mass) / len(mass), 3))


def _probe(path):
    from PIL import Image
    im = Image.open(path)
    w, h = im.size
    im = im.convert("RGBA")
    al = im.getchannel("A")
    bbox = al.getbbox() or (0, 0, w, h)
    # 抽样统计 alpha 分布（整图缩到 1/8 再算，146 张图也不必等）
    small = al.resize((max(1, w // 8), max(1, h // 8)), Image.NEAREST)
    hist = small.histogram()
    tot = float(sum(hist)) or 1.0
    op = sum(hist[16:]) / tot                    # 有任何不透明的比例
    full = sum(hist[240:]) / tot                 # 接近实心的比例
    bx0, by0, bx1, by1 = bbox
    bw, bh = max(1, bx1 - bx0), max(1, by1 - by0)
    cuts, mass = _rows_of(al, by0, by1, bx0, bx1)
    return dict(w=w, h=h, ar=round(w / float(h), 3),
                bbox=list(bbox), op=round(op, 3), full=round(full, 3),
                bw=bw, bh=bh, bar=round(bw / float(bh), 3),
                cuts=cuts, mass=mass, kb=round(os.path.getsize(path) / 1024.0))


def _kind(r):
    """按**人物包围盒**的长宽比分类 —— 不是画布。

    画布常常是 1170×2532 的整块透明纸，人物只占中间一条；按画布分类会把
    「胸像」和「全身」混成一类，选图就选不准了。
    """
    bar = r["bar"]
    if bar >= 1.18:
        return "wide"        # 横构图/大特写，脸占满
    if bar >= 0.62:
        return "bust"        # 胸像·半身，肩宽够，最适合压两行字
    return "slim"            # 竖长：全身或窄身半身


def _score(r):
    """穿插潜力分（0~1）。只用来排序，不参与渲染。

    两条同时要：① 文字高度带上有足够多的交界（字真的会被切开）；
    ② 又得是「一整块人」（实心占比 ≈ 0.55），不能是几根飘带 ——
    否则字全透过去，读作「没挡住」，穿插也就不存在了。
    """
    cut = min(1.0, r["cuts"] / 8.0)
    m = 1.0 - min(1.0, abs(r["mass"] - 0.55) / 0.55)
    return round(0.55 * cut + 0.45 * m, 3)


def scan(force=False, quiet=True):
    """盘点整个目录，返回 list[dict]（按穿插潜力降序）。结果按目录状态缓存。"""
    d = art_dir()
    if not os.path.isdir(d):
        if quiet:
            return []
        raise SystemExit("[art] 素材目录不存在：%s（可用环境变量 %s 覆盖）" % (d, ENV_DIR))
    stamp = _stamp(d)
    if not force and os.path.isfile(CACHE):
        try:
            c = json.load(open(CACHE, encoding="utf-8"))
            if c.get("schema") == SCHEMA and c.get("dir") == d and tuple(c.get("stamp") or ()) == stamp:
                return c["items"]
        except Exception:
            pass
    items = []
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith(".png"):
            continue
        p = os.path.join(d, fn)
        try:
            r = _probe(p)
        except Exception as e:                     # 坏图/半截下载：跳过并记下来
            items.append(dict(name=fn, path=p, err=str(e)[:120], score=0.0, kind="bad"))
            continue
        r.update(name=fn, path=p)
        r["kind"] = _kind(r)
        r["score"] = _score(r)
        r["hue"] = _hue(p)                          # 主色，用来提示「红帽/紫帽/白衣」
        items.append(r)
    items.sort(key=lambda x: (-x.get("score", 0), -x.get("w", 0)))
    # 排序位次就是 **#N 的唯一含义**。早先 list 子命令按「过滤后的列表」编号，
    # 而 resolve("#N") 按全量排序取值 —— 两个编号一错位，`--fig-art "#4"`
    # 取到的就是另一张图，而且不报错（图上还是有个人的，只是不是你看中的那个）。
    for i, r in enumerate(items):
        r["idx"] = i
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        json.dump(dict(schema=SCHEMA, dir=d, stamp=list(stamp), items=items),
                  open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except OSError:
        pass
    return items


def _hue(path):
    """主色标签：只用于人工挑图时一眼认出「红帽 / 黑帽 / 白衣」。"""
    try:
        from PIL import Image
        im = Image.open(path).convert("RGBA")
        im.thumbnail((96, 96))
        import numpy as np
        a = np.asarray(im).astype("float32")
        m = a[:, :, 3] > 200
        if m.sum() < 50:
            return ""
        rgb = a[:, :, :3][m]
        r, g, b = rgb[:, 0].mean(), rgb[:, 1].mean(), rgb[:, 2].mean()
        mx, mn = max(r, g, b), min(r, g, b)
        if mx - mn < 22:
            return "gray"
        if mx == r:
            return "red" if g < 0.75 * r else "warm"
        if mx == g:
            return "green"
        if b > r + 20 and b > g + 10:
            return "blue" if r < 0.8 * b else "purple"
        return "?"
    except Exception:
        return ""


def lib(kind="", min_w=0, force=False, any_alpha=False):
    """按用途过滤。kind 取 bust/slim/wide；min_w 用来挡掉低清缩略图。

    any_alpha=False（默认）时**排掉没有透明区的图**：满幅不透明的 PNG 压上去
    就是一整块方形色块，文字根本穿不过去 —— 它不配当「穿插用立绘」。
    """
    rows = [r for r in scan(force=force) if "err" not in r]
    if not any_alpha:
        rows = [r for r in rows if r["op"] < 0.96]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    if min_w:
        rows = [r for r in rows if r["w"] >= int(min_w)]
    return rows


def pick(kind="bust", min_w=700):
    """挑分最高的一张。没有就逐级放宽（宁可要一张宽的，也不要报错中断）。"""
    for k in ([kind] if kind else []) + ["bust", "slim", "wide", ""]:
        rows = lib(k, min_w=min_w)
        if rows:
            return rows[0]
    rows = lib("")
    return rows[0] if rows else None


def resolve(spec, kind="bust"):
    """名字 → 绝对路径。支持四种写法：

      ""/"auto"      按 kind 自动挑分最高的一张
      "#12"          在**当前排序**里取第 12 位（list 输出的序号，方便手抄）
      "1211081868701597696_100@repost.png"   精确文件名
      "121108186870" 子串；必须唯一命中，否则报出候选让人选（不猜）
    """
    s = (spec or "").strip()
    if not s or s.lower() == "auto":
        r = pick(kind)
        if not r:
            raise SystemExit("[art] 素材库里没有可用立绘（%s）" % art_dir())
        return r["path"]
    rows = scan()
    if s.startswith("#") and s[1:].isdigit():
        i = int(s[1:])
        for r in rows:
            if r.get("idx") == i:
                return r["path"]
        raise SystemExit("[art] 序号 %s 越界（共 %d 张）" % (s, len(rows)))
    for r in rows:
        if r["name"] == s:
            return r["path"]
    hit = [r for r in rows if s.lower() in r["name"].lower()]
    if len(hit) == 1:
        return hit[0]["path"]
    if not hit:
        raise SystemExit("[art] 找不到立绘：%s" % s)
    raise SystemExit("[art] %s 命中 %d 张，请写全一点：\n  %s"
                     % (s, len(hit), "\n  ".join(h["name"] for h in hit[:8])))


def info(path):
    """单张图的参数（引擎只需要源路径，但导演层要按真实长宽比算占位）。"""
    p = os.path.abspath(path)
    for r in scan():
        if os.path.abspath(r.get("path", "")) == p:
            return r
    return dict(name=os.path.basename(p), path=p, **_probe(p))


def doctor():
    """体检：目录在不在、有多少张能用、缓存新旧。"""
    d = art_dir()
    rows = scan()
    ok = [r for r in rows if "err" not in r]
    return dict(dir=d, exists=os.path.isdir(d), total=len(rows), usable=len(ok),
                bust=len([r for r in ok if r["kind"] == "bust"]),
                slim=len([r for r in ok if r["kind"] == "slim"]),
                wide=len([r for r in ok if r["kind"] == "wide"]),
                bad=len(rows) - len(ok),
                cached=os.path.isfile(CACHE),
                best=[(r["name"], r["score"]) for r in ok[:5]])


if __name__ == "__main__":
    t0 = time.time()
    d = doctor()
    print(json.dumps(d, ensure_ascii=False, indent=1))
    print("[art] %.1fs" % (time.time() - t0))
