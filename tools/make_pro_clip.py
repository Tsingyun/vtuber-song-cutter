# -*- coding: utf-8 -*-
"""make_pro_clip.py —— 生成「专业背景方案」测试片段（短视频，1080P）

流程：切音频 → 切歌词 → 切分析数据（逐帧切片，时间轴归零）→ render_song.cjs 出画面
      → ffmpeg 混音 → 交付 mp4

用法:
  python tools/make_pro_clip.py --audio a.mp3 --analysis _tmp/pro_analysis.json \\
      --cover img --lrc song.lrc --start 40 --dur 12 --bg spec --out _tmp/clip_spec.mp4
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = r"C:/Users/Tsing/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"
FF = r"D:/ffmpeg/bin/ffmpeg.exe"


def sh(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, **kw)
    if p.returncode != 0:
        sys.stderr.write((p.stdout or b"").decode("utf-8", "ignore")[-2000:])
        sys.stderr.write((p.stderr or b"").decode("utf-8", "ignore")[-2000:])
        raise SystemExit("命令失败：%s" % " ".join(cmd[:4]))
    return p


def slice_analysis(j, start, dur):
    fps = j["fps"]
    nb = j["nbands"]
    N = j["nframes"]
    i0 = int(round(start * fps))
    i1 = min(N, int(round((start + dur) * fps)))
    i1 = max(i0 + 1, i1)
    n = i1 - i0

    def u8(key, expect=None):
        a = np.frombuffer(base64.b64decode(j[key]), dtype=np.uint8)
        m = expect or N
        return a[:m] if len(a) >= m else np.pad(a, (0, m - len(a)))

    # ⚠ bands 是 N×nb 的一维展开，不能按 N 截断（否则 reshape 失败）
    bands = u8("bands_b64", N * nb).reshape(N, nb)[i0:i1].reshape(-1)
    out = {}
    for k in ("dur", "fps", "sr", "nbands", "band_hz"):
        if k in j:
            out[k] = j[k]
    out["nframes"] = n
    out["dur"] = round(n / fps, 3)
    out["bpm"] = j["bpm"]
    out["bar_sec"] = j["bar_sec"]
    out["bands_b64"] = base64.b64encode(bands.tobytes()).decode()
    out["rms_b64"] = base64.b64encode(u8("rms_b64")[i0:i1].tobytes()).decode()
    out["flux_b64"] = base64.b64encode(u8("flux_b64")[i0:i1].tobytes()).decode()
    out["voiced_b64"] = base64.b64encode(u8("voiced_b64")[i0:i1].tobytes()).decode()
    midi = np.frombuffer(base64.b64decode(j["midi10_b64"]), dtype=np.int16)
    midi = midi[:N] if len(midi) >= N else np.pad(midi, (0, N - len(midi)))
    out["midi10_b64"] = base64.b64encode(midi[i0:i1].astype(np.int16).tobytes()).decode()
    out["bars"] = [round(b - start, 3) for b in j["bars"] if 0 <= b - start <= out["dur"]]
    secs = []
    for a, b, nm in j["sections"]:
        a2, b2 = max(0.0, a - start), min(out["dur"], b - start)
        if b2 - a2 > 0.3:
            secs.append([round(a2, 3), round(b2, 3), nm])
    out["sections"] = secs
    return out, i0, i1


TS = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")


def slice_lrc(text, start, dur):
    keep = []
    for ln in text.splitlines():
        m = TS.match(ln.strip())
        if not m:
            continue
        t = int(m.group(1)) * 60 + float(m.group(2))
        if -1.0 <= t - start <= dur + 0.5:
            keep.append((max(0.0, t - start), m.group(3)))
    if not keep:
        raise SystemExit("切片内没有歌词行，检查 --start")
    keep.sort()
    lines = []
    for t, s in keep:
        mm = int(t // 60)
        lines.append("[%02d:%05.2f]%s" % (mm, t - mm * 60, s))
    return "\n".join(lines) + "\n"


def build_live(mode, d):
    """把 prepare_live.py 产出的 live.json 转成播放器可用的 live 配置。

    目录必须转成绝对路径：播放器页在 renderer/player/ 下，相对路径会被浏览器
    解析到 renderer/player/_tmp/... —— 图片全部 404，而画面只是「什么都没有」，
    不会报错（又是一个静默失效）。
    """
    if not mode or mode == "none":
        return None
    jp = os.path.join(d, "live.json")
    if not os.path.isfile(jp):
        raise SystemExit("缺 %s —— 先跑 tools/prepare_live.py" % jp)
    with open(jp, encoding="utf-8") as f:
        info = json.load(f)
    cfg = {"mode": mode, "t0": 0.0, "face": info.get("face") or {"cx": .5, "cy": .5}}
    for k in ("head", "body", "wide"):
        s = info.get(k)
        if not s:
            continue
        s = dict(s)
        s["dir"] = os.path.abspath(s["dir"]).replace("\\", "/")
        cfg[k] = s
    print("[clip] live=%s  head=%s  wide=%s" %
          (mode, (cfg.get("head") or {}).get("n", 0), (cfg.get("wide") or {}).get("n", 0)))
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True, help="整首 mp3")
    ap.add_argument("--analysis", required=True)
    ap.add_argument("--lrc", required=True)
    ap.add_argument("--cover", required=True, help="封面图路径")
    ap.add_argument("--start", type=float, default=40.0)
    ap.add_argument("--dur", type=float, default=12.0)
    ap.add_argument("--bg", default="spec", choices=["daw", "spec", "vocal", "none"])
    ap.add_argument("--live", default="", choices=["", "none", "lens", "strip", "behind"],
                    help="直播画面融合方式（素材由 tools/prepare_live.py 生成）")
    ap.add_argument("--live-dir", default="_tmp/live_seq", help="含 live.json 的序列目录")
    ap.add_argument("--out", required=True)
    ap.add_argument("--res", type=int, default=1)
    ap.add_argument("--bitrate", type=int, default=12000000)
    ap.add_argument("--title", default="Innocence")
    ap.add_argument("--artist", default="Avril Lavigne")
    ap.add_argument("--tmp", default=None)
    args = ap.parse_args()

    tmp = args.tmp or os.path.join(ROOT, "_tmp", "proclip")
    os.makedirs(tmp, exist_ok=True)
    tag = args.bg
    seg_mp3 = os.path.join(tmp, "seg_%s.mp3" % tag)
    seg_vid = os.path.join(tmp, "seg_%s.video.mp4" % tag)
    job_p = os.path.join(tmp, "job_%s.json" % tag)

    # ① 切音频（保持原采样率，重编码为 mp3 便于播放器解码）
    sh([FF, "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % args.start,
        "-t", "%.3f" % args.dur, "-i", args.audio, "-ac", "2", "-b:a", "192k", "-y", seg_mp3])

    # ② 切分析 + 切歌词
    j = json.load(open(args.analysis, encoding="utf-8"))
    # 不再切片：整曲分析 + off（片段时间偏移）一起注入。
    # 好处：DAW 方案能画出**全曲编排**并把播放头放在真实位置，而不是「12s 就一整条」。
    ana = dict(j)
    ana["off"] = round(args.start, 3)
    i0, i1 = 0, j["nframes"]
    # 歌词：整份 LRC 原样注入 + lrcOffset（不切片）。
    # 切片会把 t<0 的已唱行全丢掉 ⇒ 当前行顶到第 1 行，预览看着像「刚开头」，
    # 而正片里当前行上方永远有上文（Apple Music 的滚动记忆）。整份注入才能还原。
    lrc = open(args.lrc, encoding="utf-8").read()
    n_lines = sum(1 for ln in lrc.splitlines() if TS.match(ln.strip()))
    print("[clip] %s  %.1fs~%.1fs  帧 %d~%d (%d 帧)  段落 %s  歌词 %d 行（整份 + offset %.2fs）" %
          (tag, args.start, args.start + args.dur, i0, i1, ana["nframes"],
           [s[2] for s in ana["sections"]], n_lines, args.start))

    # ③ job
    cover = base64.b64encode(open(args.cover, "rb").read()).decode()
    job = {
        "playerDir": os.path.join(ROOT, "renderer", "player"),
        "page": "index_v10.html",
        "audio": seg_mp3,
        "title": args.title, "artist": args.artist,
        "trackLabel": "TRACK 01", "romaji": "",
        "vocal": "岁己SUI", "producer": "",
        "tags": "欧美,流行", "note": "",
        "footerLeft": "SONG ARCHIVE", "footerRight": "2026.10.27",
        "badge": "MUSIC ARCHIVE", "mark": "SC",
        "lrc": lrc,
        "lrcOffset": round(args.start, 3),
        "cover": "data:image/jpeg;base64," + cover,
        "scheme": 1,
        "res": args.res, "bitrate": args.bitrate,
        "encoder": "ffmpeg", "preset": "fast", "vcodec": "h264_nvenc",
        "pages": 2,
        "duration": round(args.dur, 2),
        "out": seg_vid,
        "bg": args.bg,
        "proAnalysis": ana,
        "live": build_live(args.live, args.live_dir),
    }
    with open(job_p, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)
    print("[clip] job %.1f KB → %s" % (os.path.getsize(job_p) / 1024.0, job_p))

    # ④ 渲染
    env = dict(os.environ)
    env["NODE_PATH"] = r"C:/Users/Tsing/.workbuddy/binaries/node/workspace/node_modules"
    p = subprocess.run([NODE, os.path.join(ROOT, "renderer", "render_song.cjs"), "--job", job_p],
                       capture_output=True, env=env)
    tail = (p.stdout or b"").decode("utf-8", "ignore").strip().splitlines()[-6:]
    for ln in tail:
        print("   " + ln)
    if p.returncode != 0:
        sys.stderr.write((p.stderr or b"").decode("utf-8", "ignore")[-3000:])
        raise SystemExit("渲染失败 rc=%s" % p.returncode)

    # ⑤ 混音
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    sh([FF, "-hide_banner", "-loglevel", "error", "-y",
        "-i", seg_vid, "-i", seg_mp3,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-shortest", args.out])
    sz = os.path.getsize(args.out) / 1048576.0
    print("[done] %s  %.1f MB" % (args.out, sz))
    os.remove(seg_vid)


if __name__ == "__main__":
    main()
