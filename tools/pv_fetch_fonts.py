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
import time

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
    # 文字 PV 的「高对比衬线」：细横粗竖的 Didone，大字号下有真正的印刷质感。
    # 之前抒情段只能用 Zen Old Mincho（日文明朝，拉丁字形偏弱），换成这把才对得上
    # 日本 PV 里「細明朝 × 極太ゴシック」的对比结构。
    ("Bodoni+Moda:ital,opsz,wght@0,6..96,400;0,6..96,700;0,6..96,900;1,6..96,700",
     "bodonimoda", "OFL_bodonimoda.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/bodonimoda/OFL.txt"),
    # 超窄全大写：版面上「一行顶满」的重体，和 Archivo Black 的浑厚形成体型反差
    ("Bebas+Neue", "bebasneue", "OFL_bebasneue.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/bebasneue/OFL.txt"),
    # 参考 JIZURA（852wa/JIZURA）的字体表：它的 18 族里最常出现的是「日系笔触」——
    # Klee One 是教科书体，写起来像铅笔稿，独白段用它比用黑体更像「写在纸上」；
    # Shippori Mincho B1 是日本印刷品最常用的明朝，比 Zen Old Mincho 更「活字」。
    # 这一族一直在 PV.FACES 里（sans / sansMed / sansLt），但从没进过抓取清单 ——
    # 于是所有中等粗细的段落都 fallback 到 Noto Sans SC，字形偏「网页」。
    # 用户说的「部分段落字体廉价」，一部分就是它。
    ("Zen+Kaku+Gothic+New:wght@500;700;900", "zenkakugothicnew", "OFL_zenkakugothicnew.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/zenkakugothicnew/OFL.txt"),
    # ---- 晨光绘本（Innocence v2）专用：拉丁抒情 ----
    # Nunito：圆润人文无衬线，笔画收尾是圆的 —— 钢琴抒情曲的「温柔」要靠它，
    # Zen Kaku 是日系黑体，英文太「网页」；Archivo Black 是冲击型，更不对。
    ("Nunito:wght@400;600;700;800", "nunito", "OFL_nunito.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/nunito/OFL.txt"),
    # Fraunces Italic：软衬线斜体，「Innocence / brilliant / cry」这类情感词专用。
    # 只要 italic —— 正体由 Nunito 承担，斜体衬线是签名笔触。
    ("Fraunces:ital,wght@1,400;1,600;1,700", "fraunces", "OFL_fraunces.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/fraunces/OFL.txt"),
    # ---- 中文（简体）：日后中文歌的手书 PV 全靠这一组，缺一个字就会静默 fallback ----
    # 明体：中文最正文的衬线。Noto Serif SC 是思源宋体的 Google 版本，OFL，
    # 汉字字形是简体骨架（Zen Old Mincho 是日文明朝，直/骨/今 全是日式写法）。
    ("Noto+Serif+SC:wght@400;700;900", "notoserifsc", "OFL_notoserifsc.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/notoserifsc/OFL.txt"),
    # 毛笔楷：马善政。手书 PV 的「题字」层，笔锋起收都在，大字不虚。
    ("Ma+Shan+Zheng", "mashanzheng", "OFL_mashanzheng.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/mashanzheng/OFL.txt"),
    # 行书：志莽行书。连笔但可读，做副标题/情绪句比毛笔楷轻。
    ("Zhi+Mang+Xing", "zhimangxing", "OFL_zhimangxing.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/zhimangxing/OFL.txt"),
    # 手写/草：龙藏体。铅笔速写感，适合「便签 / 日记」版式。
    ("Long+Cang", "longcang", "OFL_longcang.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/longcang/OFL.txt"),
    # 毛草：柳建毛草。比龙藏更狂，只做单句/落款，不能整段。
    ("Liu+Jian+Mao+Cao", "liujianmaocao", "OFL_liujianmaocao.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/liujianmaocao/OFL.txt"),
    # 站酷快乐体：圆润活泼，副歌跳起来时用它，和「极太ゴシック」是两种情绪。
    ("ZCOOL+KuaiLe", "zcoolkuaile", "OFL_zcoolkuaile.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/zcoolkuaile/OFL.txt"),
    # 站酷小薇：细笔画宋，女声抒情段的正文，比 Noto Serif 轻一档。
    ("ZCOOL+XiaoWei", "zcoolxiaowei", "OFL_zcoolxiaowei.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/zcoolxiaowei/OFL.txt"),
    # 站酷庆科黄油体：超粗圆头黑，做「大字报」式主歌。
    ("ZCOOL+QingKe+HuangYou", "zcoolqingkehuangyou", "OFL_zcoolqingkehuangyou.txt",
     "https://raw.githubusercontent.com/google/fonts/main/ofl/zcoolqingkehuangyou/OFL.txt"),
    # 注意：本机代理下 fonts.gstatic.com 偶发 rc=35（SSL connect），全量重抓会失败。
    # 只抓需要的族：`--only zenkakugothicnew`。中文族同理，可以 `--only notoserifsc,mashanzheng`。
]

CSS_URL = "https://fonts.googleapis.com/css2?family=%s&display=block"


# 本机开着 Clash（http_proxy/https_proxy 指向本地端口），但 urllib 走 CONNECT 隧道会 502，
# 而 curl 直连正常，所以统一走 curl 子进程（Windows 自带 curl.exe）。
def get(url, binary=False, tries=6):
    """curl 直连（本机代理对 gstatic 偶发 rc=35 SSL connect），必须带重试。

    实测全量抓 400 个分片时，不重试几乎必挂：单个分片有 3~5% 概率 SSL 握手失败，
    400 次里有 15+ 次 ⇒ 整个族前功尽弃。重试是唯一稳的办法（同一 URL 立刻重试
    通常就能过，不是持续性故障）。已存在的分片直接跳过，所以重跑是断点续传。
    """
    last = ""
    for k in range(tries):
        r = subprocess.run(["curl", "-sL", "--fail", "--connect-timeout", "20",
                            "--max-time", "120", "-A", UA, url], capture_output=True)
        if r.returncode == 0:
            return r.stdout if binary else r.stdout.decode("utf-8")
        last = "curl rc=%d %s :: %s" % (r.returncode, url[:90], r.stderr[:120])
        time.sleep(0.8 + 0.6 * k)
    raise RuntimeError(last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join("renderer", "player", "pv_fonts"))
    ap.add_argument("--only", default="", help="逗号分隔的目录名，只处理这些")
    ap.add_argument("--css-only", action="store_true",
                    help="不下载任何分片，只按磁盘上已有的子目录重建汇总 fonts.css"
                         "（加完新字体、或改了 --only 之后用它收尾）")
    a = ap.parse_args()
    only = set(x for x in a.only.split(",") if x)

    os.makedirs(a.out, exist_ok=True)
    chunks = []
    total = 0
    for query, slug, lic_name, lic_url in FAMILIES:
        if only and slug not in only:
            continue
        if a.css_only:
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
            # 0 字节=上次下载残骸：必须当「不存在」重下，否则永远修不好
            # （实测留过 3 个 0 字节分片，字形在字体里、文件却加载不出来）
            if not os.path.exists(fp) or os.path.getsize(fp) == 0:
                data = get(m.group(1), binary=True)
                if not data:
                    raise RuntimeError("字体分片下载为空：%s" % m.group(1))
                open(fp, "wb").write(data)
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
    # `--only` 只重抓某几族时，其它族必须沿用磁盘上已有的 fonts.css ——
    # 否则汇总文件会被写成「只有这一族」，页面其余字体全部静默 fallback，
    # 而启动日志只会少几族、不报任何错（实测踩过一次）。
    # `done` 语义是「已写进这次汇总的族」。只跑 --css-only 时没有任何族被重新抓过，
    # 必须从空集开始，让下面两段「沿用磁盘上已有的 fonts.css」把每一族都并进来
    # ——否则 done 预先装满全部族，汇总会被写成空文件（实测踩过：743 条 @font-face
    # 被削成 3 条，页面不报错，全部静默 fallback）。
    done = (set(only) if only else set()) if a.css_only else (set(only) if only else set(s for _, s, _, _ in FAMILIES))
    for _, slug, _, _ in FAMILIES:
        if slug in done:
            continue
        fp = os.path.join(a.out, slug, "fonts.css")
        if os.path.exists(fp):
            chunks.append(open(fp, encoding="utf-8").read())
            done.add(slug)
            print("[keep] %-16s 沿用已有 fonts.css" % slug)
    # 非 Google Fonts 来源的族（tools/pv_fetch_cjk.py 抓的霞鹜文楷 / 得意黑）：
    # 它们不在 FAMILIES 里，但同样要出现在汇总 css 中。
    for d in sorted(os.listdir(a.out)):
        fp = os.path.join(a.out, d, "fonts.css")
        if d in done or not os.path.isfile(fp):
            continue
        chunks.append(open(fp, encoding="utf-8").read())
        done.add(d)
        print("[keep] %-16s 并入（非 Google 来源）" % d)
    missing = [s for _, s, _, _ in FAMILIES if s not in done]
    if missing:
        print("[warn] 这些族没有 fonts.css，会 fallback 到 Noto：%s" % ", ".join(missing))
    open(os.path.join(a.out, "fonts.css"), "w", encoding="utf-8", newline="\n").write(head + "\n".join(chunks))
    print("[out] %s  fonts.css 合计 %.1f MB" % (a.out, total / 1048576.0))


if __name__ == "__main__":
    sys.exit(main())
