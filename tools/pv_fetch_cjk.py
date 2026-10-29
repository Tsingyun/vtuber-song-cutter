# -*- coding: utf-8 -*-
"""抓「不在 Google Fonts 上」的开源中文字体，转成 woff2 自托管到 pv_fonts/。

为什么单独一支脚本：
  Google Fonts 的中文族是按 unicode-range 切成 ~90 个分片的，pv_fetch_fonts.py 那套
  「抓 CSS + 改 url」的流程直接适用。但霞鹜文楷、得意黑只发在 GitHub Releases 上，
  给的是整包 TTF/OTF（20MB 级），浏览器能直接吃 TTF，但 4K 长片要开两个渲染页、
  每页都要把整包解析一遍 —— 转成 woff2（Brotli）体积砍一半、解析也快一截。

授权（都可商用，随字体分发许可证文本）：
  · 霞鹜文楷 LXGW WenKai —— SIL OFL 1.1（基于 Klee One / Fontworks，OFL 派生允许商用）
  · 得意黑 Smiley Sans  —— SIL OFL 1.1
  两者都必须保留 LICENSE，且**不得单独售卖字体本身**；嵌进视频成片不受限。

用法：
  python tools/pv_fetch_cjk.py                # 缺什么抓什么（已存在的跳过）
  python tools/pv_fetch_cjk.py --force        # 强制重下重转
  python tools/pv_fetch_cjk.py --only lxgwwenkai
"""
import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "renderer", "player", "pv_fonts")

# ---- 字体清单 ----------------------------------------------------------------
# faces: (源文件名, font-weight, 输出名, 备注)
GH = [
    {
        "slug": "lxgwwenkai",
        "family": "LXGW WenKai",
        "why": "楷体：手书 PV 的「写下来」层。基于 Klee One 改造，把日文字形换成了简体骨架，"
               "笔画有楷书的提按，比黑体像手写、比毛笔楷更像钢笔。整段歌词可用。",
        "repo": "lxgw/LxgwWenKai",
        "assets": [
            ("LXGWWenKai-Light.ttf", 300, "LXGWWenKai-Light.woff2", "细：大字号的抒情句"),
            ("LXGWWenKai-Regular.ttf", 400, "LXGWWenKai-Regular.woff2", "常规：整段正文"),
        ],
        "license": ("OFL_lxgwwenkai.txt",
                    "https://raw.githubusercontent.com/lxgw/LxgwWenKai/main/OFL.txt"),
    },
    {
        "slug": "smileysans",
        "family": "Smiley Sans",
        "why": "得意黑：倾斜的超粗展示黑，中文标题的「海报感」全靠它。字形自带斜切，"
               "所以 CSS 里按 normal 注册（否则 font-style 不匹配会静默落到兜底）。",
        "repo": "atelier-anchor/smiley-sans",
        "zip": True,          # 只发整包 zip
        "faces": [("SmileySans-Oblique.ttf.woff2", 400, "SmileySans-Oblique.woff2",
                   "唯一字重（oblique 是字形本身斜，不是合成）")],
        "license": ("OFL_smileysans.txt",
                    "https://raw.githubusercontent.com/atelier-anchor/smiley-sans/main/LICENSE"),
    },
]


def curl(url, binary=True, tries=6):
    """curl 直连（本机代理对 GitHub release 偶发 502，用系统 curl 更稳）。"""
    last = ""
    for k in range(tries):
        r = subprocess.run(["curl", "-sL", "--fail", "--connect-timeout", "20",
                            "--max-time", "600", "-A", UA, url], capture_output=True)
        if r.returncode == 0 and r.stdout:
            return r.stdout if binary else r.stdout.decode("utf-8")
        last = "curl rc=%d %s" % (r.returncode, url[:90])
        import time
        time.sleep(1.0 + 0.8 * k)
    raise RuntimeError(last)


def release_url(repo, name):
    api = "https://api.github.com/repos/%s/releases/latest" % repo
    d = json.loads(curl(api, binary=False).encode("utf-8").decode("utf-8"))
    for a in d.get("assets", []):
        if a["name"] == name:
            return a["browser_download_url"], d.get("tag_name", "")
    raise RuntimeError("%s 的 release 里没有 %s" % (repo, name))


def to_woff2(src, dst):
    """TTF/OTF → woff2。用 fontTools 的 flavor 机制，保留全部字形与 cmap。"""
    from fontTools.ttLib import TTFont
    f = TTFont(src, recalcBBoxes=False, recalcTimestamp=False)
    f.flavor = "woff2"
    f.save(dst)
    f.close()


def write_css(d, family, faces, slug):
    """一个族一份 fonts.css。中文不切 unicode-range（整包加载），所以每条只有一行 src。

    src 必须带 `slug/` 前缀：这份 css 会被并进 pv_fonts/fonts.css 由页面**直接引用**，
    相对路径是按汇总文件所在目录解析的，不带前缀就指到 pv_fonts/ 根上（文件不存在，
    浏览器静默 fallback）。Google 那一批由 pv_fetch_fonts.py 重写成 `slug/fNNNN.woff2`，
    正是同一个原因。
    """
    lines = ["/* %s —— 由 tools/pv_fetch_cjk.py 生成，勿手改。 */" % family]
    for _, weight, fn, note in faces:
        lines.append("/* %s */" % note)
        # font-style 一律 normal：得意黑的斜是字形本身，标成 oblique 会让
        # `ctx.font = '400 120px "Smiley Sans"'`（无 italic 关键字）匹配不上而静默 fallback。
        lines.append("@font-face {")
        lines.append("  font-family: '%s';" % family)
        lines.append("  font-style: normal;")
        lines.append("  font-weight: %d;" % weight)
        lines.append("  font-display: block;")
        lines.append("  src: url(%s/%s) format('woff2');" % (slug, fn))
        lines.append("}")
    io.open(os.path.join(d, "fonts.css"), "w", encoding="utf-8", newline="\n").write(
        "\n".join(lines) + "\n")


def do_lxgw(spec, force):
    d = os.path.join(OUT, spec["slug"])
    os.makedirs(d, exist_ok=True)
    faces = []
    for srcname, weight, outname, note in spec["assets"]:
        dst = os.path.join(d, outname)
        if force or not os.path.exists(dst) or os.path.getsize(dst) == 0:
            url, tag = release_url(spec["repo"], srcname)
            print("[dl] %-26s %s" % (srcname, tag))
            data = curl(url)
            tmp = os.path.join(tempfile.gettempdir(), srcname)
            open(tmp, "wb").write(data)
            print("     源 %.1f MB → 转 woff2" % (len(data) / 1048576.0))
            to_woff2(tmp, dst)
            os.remove(tmp)
        print("[ok] %-26s %6.1f MB" % (outname, os.path.getsize(dst) / 1048576.0))
        faces.append((srcname, weight, outname, note))
    write_css(d, spec["family"], faces, spec["slug"])
    return d


def do_zip(spec, force):
    d = os.path.join(OUT, spec["slug"])
    os.makedirs(d, exist_ok=True)
    need = [f for f in spec["faces"]
            if force or not os.path.exists(os.path.join(d, f[2]))
            or os.path.getsize(os.path.join(d, f[2])) == 0]
    if need:
        url, tag = release_url(spec["repo"], [a["name"] for a in
                                              json.loads(curl(
                                                  "https://api.github.com/repos/%s/releases/latest"
                                                  % spec["repo"], binary=False))["assets"]
                                              if a["name"].endswith(".zip")][0])
        print("[dl] %s %s" % (os.path.basename(url), tag))
        blob = curl(url)
        zf = zipfile.ZipFile(io.BytesIO(blob))
        for _, _, outname, _ in need:
            # 包里的文件名带双扩展（SmileySans-Oblique.ttf.woff2），按前缀匹配
            stem = outname.split(".")[0]
            hit = [n for n in zf.namelist() if n.startswith(stem) and n.endswith(".woff2")]
            if not hit:
                raise RuntimeError("zip 里找不到 %s 的 woff2" % outname)
            open(os.path.join(d, outname), "wb").write(zf.read(hit[0]))
            print("[ok] %-26s %6.1f MB（包内直取，无需转换）"
                  % (outname, os.path.getsize(os.path.join(d, outname)) / 1048576.0))
    write_css(d, spec["family"], spec["faces"], spec["slug"])
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    only = set(x for x in a.only.split(",") if x)
    for spec in GH:
        if only and spec["slug"] not in only:
            continue
        d = do_zip(spec, a.force) if spec.get("zip") else do_lxgw(spec, a.force)
        lic_name, lic_url = spec["license"]
        lp = os.path.join(d, lic_name)
        if a.force or not os.path.exists(lp):
            txt = curl(lic_url, binary=False)
            # OFL 要求许可证随字体一起分发；版权行里的年份作者名不能丢
            io.open(lp, "w", encoding="utf-8", newline="\n").write(txt)
            print("[lic] %s" % lic_name)
    print("\n下一步：python tools/pv_fetch_fonts.py --css-only   # 重建汇总 fonts.css")
    return 0


if __name__ == "__main__":
    sys.exit(main())
