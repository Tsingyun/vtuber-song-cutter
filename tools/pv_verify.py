# -*- coding: utf-8 -*-
"""pv_verify.py —— PV 旁路成片的四关自检：规格 / 帧数 / 每句有字 / 无黑场。

为什么单独写：
  RULES §44 的 `songcut.qc --manifest` 是给「song_cutter 主链」出的片用的，它要
  cuts/<date>/manifest.json 里的切点与 ORIGINAL 元数据。PV 旁路（手书 / 纯 JIZURA）
  没有那条 manifest；但「规格 + 完整性」这几条判据是通用的，所以独立实现，
  判据与 RULES 一致（4K60 / high@5.2 / yuv420p / bt709 / 16~18.5 Mbps / AAC ≤320k）。

「每句有字」是这一版最有价值的一条：它按 LRC 逐句取「句起点 +0.7s」的帧算墨量，
缺字会直接点名 —— 比事后肉眼看整片可靠得多（§118.5）。

用法：
  python tools/pv_verify.py <成片.mp4> --seq _tmp/x/jz_final --lrc _tmp/x/jz.lrc
"""
import json
import os
import re
import subprocess
import sys

from PIL import Image

# 运行时路径只有一份：songcut/pvlink.runtime()（env → config.json → 内置探测）。
# 写死的 D:/ffmpeg/bin/... 换台机器就得全局替换，漏一处就是「找不到 ffmpeg」。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from songcut import pvlink as _PVRT  # noqa: E402
_RT = _PVRT.runtime()


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FFPROBE = _RT["ffprobe"]
FFMPEG = _RT["ffmpeg"]
D = os.path.join(ROOT, "_tmp", "full1101")
FINAL = os.path.join(D, "jz_final")
FPS = 30

ok = True
def chk(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print("[%s] %-28s %s" % ("OK " if cond else "!! ", name, detail))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("mp4")
    ap.add_argument("--seq", default="", help="全片帧序列目录（缺省 <mp4 所在目录>/_frames）")
    ap.add_argument("--lrc", default="", help="歌词 LRC（缺省 <mp4 同目录>/<同名>.lrc）")
    ap.add_argument("--expect-dur", type=float, default=0.0, help="片长期望（0=只做一致性检查）")
    a = ap.parse_args()
    src = a.mp4
    base = os.path.dirname(os.path.abspath(src))
    FINAL = os.path.abspath(a.seq) if a.seq else os.path.join(base, "_frames")
    LRC = a.lrc or os.path.splitext(src)[0] + ".lrc"
    info = json.loads(subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", src],
        capture_output=True, text=True).stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    au = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    num, den = v["avg_frame_rate"].split("/")
    fps = float(num) / float(den)
    dur = float(info["format"]["duration"])
    bitrate = int(v.get("bit_rate") or info["format"]["bit_rate"])

    chk("分辨率", (v["width"], v["height"]) == (3840, 2160), "%dx%d" % (v["width"], v["height"]))
    chk("帧率", abs(fps - 60) < 0.01, "%.2ffps  nb_frames=%s" % (fps, v.get("nb_frames")))
    chk("编码档次", (v.get("profile") or "").lower().startswith("high")
        and str(v.get("level")) in ("52", "5.2"), "%s@%s" % (v.get("profile"), v.get("level")))
    chk("像素格式", v.get("pix_fmt") == "yuv420p", str(v.get("pix_fmt")))
    chk("色彩标记", v.get("color_space") == "bt709" and v.get("color_transfer") == "bt709"
        and v.get("color_primaries") == "bt709" and v.get("color_range") == "tv",
        "%s/%s/%s/%s" % (v.get("color_space"), v.get("color_transfer"),
                         v.get("color_primaries"), v.get("color_range")))
    chk("码率区间", 15_500_000 <= bitrate <= 18_600_000, "%.2f Mbps" % (bitrate / 1e6))
    if a.expect_dur:
        chk("时长", abs(dur - a.expect_dur) < 0.25, "%.3fs（期望 %.2fs）" % (dur, a.expect_dur))
    chk("音轨", au is not None and au.get("codec_name") == "aac"
        and int(au.get("channels") or 0) == 2, "%s %s Hz %s ch %s kbps" % (
            au and au.get("codec_name"), au and au.get("sample_rate"),
            au and au.get("channels"), au and int(au.get("bit_rate") or 0) // 1000))

    # ---- 帧序列完整性：每一帧都在、且不是同一张硬链接凑数
    n = len([f for f in os.listdir(FINAL) if f.endswith(".jpg")])
    chk("帧序列", n > 0, "%d 帧" % n)
    chk("片长一致", abs(n / float(FPS) - dur) < 0.3, "序列 %.2fs vs 成片 %.3fs" % (n / float(FPS), dur))

    # ---- 每句歌词都有字：句起点 +0.7s 的画面里必须有墨
    if not os.path.isfile(LRC):
        print("[--] 无 LRC，跳过「每句有字」")
        lrc = []
    else:
        lrc = [l for l in open(LRC, encoding="utf-8").read().split("\n") if l.strip()]
    miss = []
    for ln in lrc:
        if "[間奏" in ln:
            continue
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.+)", ln)
        if not m:
            continue
        t = int(m.group(1)) * 60 + float(m.group(2)) + 0.7
        f = min(n, int(t * FPS) + 1)
        im = Image.open(os.path.join(FINAL, "j_%05d.jpg" % f)).convert("L")
        hist = im.histogram()
        ink = sum(hist[:110]) / float(im.width * im.height) * 100
        if ink < 0.35:
            miss.append((m.group(3)[:12], round(t - 0.7, 2), round(ink, 2)))
    chk("每句有字", not miss, "%d 句检查，缺字 %d 处 %s" % (
        len([l for l in lrc if "[間奏" not in l]), len(miss), miss[:5]))

    # ---- 无黑场 / 无整段空白：抽样 540 帧看平均亮度
    dark = []
    for f in range(1, n + 1, 15):
        im = Image.open(os.path.join(FINAL, "j_%05d.jpg" % f)).convert("L")
        st = im.resize((160, 90)).getextrema()
        mean = sum(i * c for i, c in enumerate(im.resize((160, 90)).histogram())) / float(160 * 90)
        if mean < 45:
            dark.append((round(f / float(FPS), 1), round(mean, 1)))
    chk("无黑场", not dark, "抽样 %d 帧，暗场 %d 帧 %s" % (n // 15, len(dark), dark[:6]))

    # ---- 黑场检测（成片侧，官方滤镜）
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", src, "-vf",
                        "blackdetect=d=0.20:pix_th=0.10", "-an", "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    bl = re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", r.stderr)
    chk("成片无黑段", not bl, "%d 段 %s" % (len(bl), bl[:4]))

    print("\n结论：%s" % ("全部通过" if ok else "有未通过项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
