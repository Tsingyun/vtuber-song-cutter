# -*- coding: utf-8 -*-
"""把 Google Fonts 的 woff2 自托管到本地，生成可直接给 file:// 页面用的 fonts.css。

为什么不用 <link> 拉 CDN：
· 4K 长片逐帧渲染要跑十几分钟，中途任何一次字体请求抖动都会让**部分帧**用 fallback 字形
  —— 同一帧两次渲染不一致，确定性直接破掉（RULES 51）。
· 离线机器上等于没有字体。

做法：用现代 UA 请求 CSS2（拿到 woff2 而不是 ttf），下载它引用的每个分片，把 url()
改写成相对路径，原样保留 unicode-range —— 浏览器仍然按需只取用到的分片。

用法：python tools/pv_fetch_fonts.py [--out renderer/player/pv_fonts] [--only 目录名…]
"""
import argparse
import os
import re
import sys
import subprocess

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# family 查询串 → 本地目录名。只挑「术力口文字 PV 真会用到的」几把，
# 不是把 Google Fonts 搬下来：粗黑展示 / 明朝 / 干净黑体 / 等宽 / 拉丁重体。
FAMILIES = [
    ("Archivo+Black", "archivoblack", "OFL_archivoblack.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/archivoblack/OFL.txt"),
    ("Anton", "anton", "OFL_anton.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/anton/OFL.txt"),
    ("Dela+Gothic+One", "delagothicone", "OFL_delagothicone.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/delagothicone/OFL.txt"),
    ("Zen+Old+Mincho:wght@700;900", "zenoldmincho", "OFL_zenoldmincho.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/zenoldmincho/OFL.txt"),
    ("IBM+Plex+Mono:wght@400;600", "ibmplexmono", "OFL_ibmplexmono.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/ibmplexmono/OFL.txt"),
]

CSS_URL = "https://fonts.googleapis.com/css2?family=%s&display=block"


# 本机开着 Clash（http_proxy/https_proxy 指向本地端口），但 urllib 走 CONNECT 隧道会 502，
# 而 curl 直连正常，所以统一走 curl 子进程（Windows 自带 curl.exe）。
def get(url, binary=False):
    r = subprocess.run(["curl", "-sL", "--fail", "-A", UA, url], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("curl rc=%d %s :: %s" % (r.returncode, url[:90], r.stderr[:200]))
    return r.stdout if binary else r.stdout.decode("utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join("renderer", "player", "pv_fonts"))
    ap.add_argument("--only", default="", help="逗号分隔的目录名，只处理这些")
    a = ap.parse_args()
    only = set(x for x in a.only.split(",") if x)

    os.makedirs(a.out, exist_ok=True)
    chunks = []
    total = 0
    for query, slug, lic_name, lic_url in FAMILIES:
        if only and slug not in only:
            continue
        css = get(CSS_URL % query)
        if "@font-face" not in css:
            print("[skip] %s 没有返回 @font-face" % slug)
            continue
        d = os.path.join(a.out, slug)
        os.makedirs(d, exist_ok=True)
        n = 0
        # 每个 @font-face 块里的 url(...) 换成 fNNNN.woff2
        def repl(m):
            nonlocal n
            n += 1
            fn = "f%04d.woff2" % n
            fp = os.path.join(d, fn)
            if not os.path.exists(fp):
                open(fp, "wb").write(get(m.group(1), binary=True))
            return "url(%s/%s)" % (slug, fn)
        css2 = re.sub(r"url\((https://fonts\.gstatic\.com/[^)]+)\)", repl, css)
        open(os.path.join(d, "fonts.css"), "w", encoding="utf-8", newline="\n").write(css2)
        size = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))
        total += size
        # 许可证必须随字体分发（OFL 要求）
        try:
            lic = get(lic_url)
            open(os.path.join(d, lic_name), "w", encoding="utf-8", newline="\n").write(lic)
        except Exception as e:                                    # noqa: BLE001
            print("[warn] %s 许可证下载失败：%s" % (slug, e))
        chunks.append(css2)
        print("[font] %-16s %3d 个分片  %6.1f MB" % (slug, n, size / 1048576.0))

    # 汇总一份 fonts.css，页面只需引一次
    head = ("/* 自托管字体（OFL），由 tools/pv_fetch_fonts.py 生成，勿手改。\n"
            "   许可证文本在各子目录内。 */\n")
    open(os.path.join(a.out, "fonts.css"), "w", encoding="utf-8", newline="\n").write(head + "\n".join(chunks))
    print("[out] %s  fonts.css 合计 %.1f MB" % (a.out, total / 1048576.0))


if __name__ == "__main__":
    sys.exit(main())
