# -*- coding: utf-8 -*-
"""pv_one.py —— 一条命令出一首手书 PV（半自动流程的正门）。

为什么要它
----------
2026-10-30 的复盘里最刺眼的一条：旗舰片（Innocence 4K30）是**手工串**出来的 ——
抽音频、`analyze_music`、`pv_timing`、`pv_direct`、写 job、`render_song`、`pv_build` 封音轨，
每一步的参数都在人的记忆里，编排稿与 job 全落在 `_tmp/pv/`，一次清理就没了。
这个脚本把这条链固定下来，并把每一步的耗时打进日志与 manifest。

它做什么
--------
  ① 定位录播（`--src` 或按日期在 recording_root 下 glob，取最大的 mp4）；
  ② 抽音频（`-ss/--start -t/--dur`，只抽这一段，不落整场）；
  ③ `analyze_music.py` → 频段/人声/BPM/段落；
  ④ 歌词：`--lrc` 指定，或用 `songcut.lyrics_fetch` 联网取（LRCLIB→网易云）；
  ⑤ `pv_timing.py` → 逐词时间轴（带诊断：吸附比例/行偏移）；
  ⑥ `pv_direct.py` → 编排稿（默认 `--concept dawn` + 意象表）；
  ⑦ `render_song.cjs` → 4K 逐帧 + NVENC；
  ⑧ 封音轨 → `【岁己SUI】歌名【YYYYMMDD歌切】.mp4`；
  ⑨ 写 `manifest.json`：规格、各阶段耗时、闸门结论、参数快照。

不做的事
--------
不跑 QC 判定（那是 `songcut/qc.py --manifest` 的事，见 RULES §44），
只保证「产物 + 规格 + 耗时」三样落地，供 QC 接手。

用法
----
    python tools/pv_one.py --date 2026-10-27 --song "Innocence" \\
        --artist "Avril Lavigne" --vocal "岁己SUI" \\
        --start 4474.10 --end 4705.75 --concept dawn

    # 只重渲（编排稿已在）：加 --reuse-sb
    # 只出 1080p 试片：--res 1
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PY = sys.executable
FFMPEG = os.environ.get("SONGCUT_FFMPEG") or "D:/ffmpeg/bin/ffmpeg.exe"
FFPROBE = os.environ.get("SONGCUT_FFPROBE") or "D:/ffmpeg/bin/ffprobe.exe"
NODE = os.environ.get("SONGCUT_NODE") or r"C:/Users/Tsing/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"
NODE_PATH = os.environ.get("SONGCUT_NODE_PATH") or r"C:/Users/Tsing/.workbuddy/binaries/node/workspace/node_modules"


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def run(cmd, env=None, quiet=False):
    t0 = time.time()
    r = subprocess.run(cmd, cwd=ROOT, env=env,
                       capture_output=quiet, text=True, encoding="utf-8", errors="replace")
    dt = time.time() - t0
    if r.returncode != 0:
        print((r.stdout or "")[-3000:])
        print((r.stderr or "")[-3000:])
        raise SystemExit("命令失败 rc=%d：%s" % (r.returncode, " ".join(map(str, cmd[:4]))))
    return dt, (r.stdout or "")


def probe_dur(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def resolve_src(date, explicit):
    if explicit:
        return os.path.abspath(explicit)
    from songcut import config as CFG
    root = CFG.expand(CFG.get("recording_root") or "")
    if not root:
        raise SystemExit("没有 --src，config.json 里也没配 recording_root")
    cands = []
    for pat in ("%s/*.mp4", "%s/**/*.mp4"):
        cands += glob.glob(os.path.join(root, date, "*.mp4"), recursive=True)
        cands += glob.glob(os.path.join(root, date, "@*"), recursive=True)
    cands = [c for c in cands if c.lower().endswith(".mp4") and os.path.getsize(c) > 10 << 20]
    if not cands:
        raise SystemExit("在 %s/%s 下找不到 mp4" % (root, date))
    return max(cands, key=os.path.getsize)


def fetch_lrc(song, artist, dur, work):
    from songcut import lyrics_fetch
    lrc, cover, info = lyrics_fetch.fetch_lyrics_and_cover(
        song, artist, dur=dur, cache_dir=work)
    if not lrc:
        raise SystemExit("歌词取不到：用 --lrc 指定一份 LRC")
    p = os.path.join(work, "lyric.lrc")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(lrc)
    return p, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="直播日期 YYYY-MM-DD（用于定位录播与命名）")
    ap.add_argument("--song", required=True)
    ap.add_argument("--artist", default="", help="原唱（画面标原唱，见 RULES 铁律 1）")
    ap.add_argument("--vocal", default="")
    ap.add_argument("--src", default="", help="显式指定录播 mp4（缺省按日期 glob）")
    ap.add_argument("--start", type=float, required=True, help="成片起点（源视频绝对秒）")
    ap.add_argument("--end", type=float, required=True, help="成片终点")
    ap.add_argument("--lrc", default="", help="LRC 路径（缺省联网取）")
    ap.add_argument("--concept", default="dawn", choices=["", "dawn"])
    ap.add_argument("--imagery", default="", help="意象映射 JSON（--concept dawn 用）")
    ap.add_argument("--live-json", default="", help="prepare_live.py 的 live.json（人物层）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--koma", type=int, default=12)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--res", type=int, default=2)
    ap.add_argument("--pages", type=int, default=0, help="0=按分辨率自动（4K→3）")
    ap.add_argument("--bitrate", type=int, default=0)
    ap.add_argument("--jq", type=float, default=0.92)
    ap.add_argument("--reuse-sb", action="store_true")
    ap.add_argument("--outdir", default="", help="缺省 cuts/<date>/<song>/")
    ap.add_argument("--keep-silent", action="store_true", help="保留无音轨中间片")
    a = ap.parse_args()

    dur = round(a.end - a.start, 3)
    if dur <= 1:
        raise SystemExit("--end 必须大于 --start")
    seed = a.seed or int(a.date.replace("-", "")) * 100 + 1
    outdir = a.outdir or os.path.join(ROOT, "cuts", a.date, a.song.replace("/", "_"))
    os.makedirs(outdir, exist_ok=True)
    work = os.path.join(outdir, "_work")
    os.makedirs(work, exist_ok=True)
    stages, t_all = {}, time.time()

    # ---- ① 源
    src = resolve_src(a.date, a.src)
    log("源：%s（%.1f MB）" % (os.path.basename(src), os.path.getsize(src) / 1048576))
    s_dur = probe_dur(src)
    if a.end > s_dur + 1:
        raise SystemExit("--end %.2f 超过源时长 %.2f" % (a.end, s_dur))

    # ---- ② 抽音频
    audio = os.path.join(work, "audio.mp3")
    if not os.path.exists(audio):
        dt, _ = run([FFMPEG, "-y", "-hide_banner", "-loglevel", "warning",
                     "-ss", "%.3f" % a.start, "-t", "%.3f" % dur, "-i", src,
                     "-vn", "-c:a", "libmp3lame", "-b:a", "320k", "-ar", "48000", audio], quiet=True)
        stages["extract_audio"] = round(dt, 1)
    log("音频：%.2fs（抽轨 %.1fs）" % (probe_dur(audio), stages.get("extract_audio", 0)))

    # ---- ③ 音频分析
    ana = os.path.join(work, "analysis.json")
    if not os.path.exists(ana):
        dt, out = run([PY, "tools/analyze_music.py", "--audio", audio,
                       "--out", ana, "--fps", str(a.fps)], quiet=True)
        stages["analyze"] = round(dt, 1)
    log("分析：%.1fs" % stages.get("analyze", 0))

    # ---- ④ 歌词
    linfo = {}                     # --lrc 直给时没有元数据，manifest 要能容错
    if a.lrc:
        lrc = os.path.abspath(a.lrc)
    else:
        lrc, linfo = fetch_lrc(a.song, a.artist, dur, work)
        log("歌词：%s（原唱 %s）" % (os.path.basename(lrc), linfo.get("artist") or "?"))
    stages["lyrics"] = 0

    # ---- ⑤ 逐词时间轴
    timing = os.path.join(work, "timing.json")
    if not os.path.exists(timing):
        dt, out = run([PY, "tools/pv_timing.py", "--lrc", lrc, "--audio", audio,
                       "--analysis", ana, "--out", timing], quiet=True)
        stages["timing"] = round(dt, 1)
        for line in out.strip().splitlines():
            if line.startswith("[calib]") or line.startswith("[snap]") or line.startswith("[warn]"):
                log("  " + line)
    log("时间轴：%.1fs" % stages.get("timing", 0))

    # ---- ⑥ 编排稿
    sb = os.path.join(work, "storyboard.json")
    if not (a.reuse_sb and os.path.exists(sb)):
        cmd = [PY, "tools/pv_direct.py", "--timing", timing, "--analysis", ana,
               "--out", sb, "--start", "0", "--dur", str(dur), "--seed", str(seed),
               "--title", a.song, "--artist", a.artist, "--vocal", a.vocal,
               "--date", a.date.replace("-", "."), "--koma", str(a.koma), "--fps", str(a.fps)]
        if a.concept:
            cmd += ["--concept", a.concept]
        if a.imagery:
            cmd += ["--imagery", a.imagery]
        if a.live_json:
            cmd += ["--live-json", a.live_json]
        dt, out = run(cmd, quiet=True)
        stages["direct"] = round(dt, 1)
        for line in out.strip().splitlines():
            if line.startswith("["):
                log("  " + line)
    log("编排稿：%.1fs" % stages.get("direct", 0))

    # ---- ⑦ 渲染
    pages = a.pages or (3 if a.res >= 2 else 1)
    bitrate = a.bitrate or (18000000 if a.res >= 2 else 12000000)
    silent = os.path.join(work, "video.mp4")
    job = {
        "playerDir": os.path.join(ROOT, "renderer/player").replace("\\", "/"),
        "page": "index_pv.html",
        "audio": audio.replace("\\", "/"),
        "title": a.song, "artist": a.artist, "vocal": a.vocal,
        "storyboard": None, "pvDur": dur, "live": None,
        "res": a.res, "fps": a.fps, "bitrate": bitrate,
        "encoder": "ffmpeg", "pages": pages,
        "out": silent.replace("\\", "/"),
    }
    with open(sb, encoding="utf-8") as f:
        job["storyboard"] = json.load(f)
    if a.live_json:
        with open(a.live_json, encoding="utf-8") as f:
            lj = json.load(f)
        live = {}
        for k in ("head", "body", "wide"):
            if lj.get(k):
                d = lj[k].get("dir") or ""
                live[k] = dict(lj[k], dir=d.replace("\\", "/"))
        if lj.get("face"):
            live["face"] = lj["face"]
        job["live"] = live or None
    jobp = os.path.join(work, "job.json")
    with open(jobp, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)
    log("渲染：%dx%d @%dfps，%d 实例，%s" % (
        1920 * a.res, 1080 * a.res, a.fps, pages, "%.1f Mbps" % (bitrate / 1e6)))
    dt, out = run([NODE, os.path.join(ROOT, "renderer/render_song.cjs"), "--job", jobp,
                   "--encoder", "ffmpeg", "--jq", str(a.jq)],
                  env=dict(os.environ, NODE_PATH=NODE_PATH))
    stages["render"] = round(dt, 1)
    for line in out.strip().splitlines()[-6:]:
        log("  " + line)

    # ---- ⑧ 封音轨
    name = "【%s】%s【%s歌切】.mp4" % (a.vocal or "VOCAL", a.song, a.date.replace("-", ""))
    out_mp4 = os.path.join(outdir, name)
    dt, _ = run([FFMPEG, "-y", "-hide_banner", "-loglevel", "warning",
                 "-i", audio, "-i", silent,
                 "-map", "0:a:0", "-map", "1:v:0", "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
                 "-shortest", "-movflags", "+faststart", out_mp4], quiet=True)
    stages["mux"] = round(dt, 1)
    if not a.keep_silent:
        try:
            os.remove(silent)
        except OSError:
            pass

    # ---- ⑨ manifest（规格 + 耗时 + 参数快照）
    from songcut import spec as SPEC
    info = json.loads(subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", out_mp4],
        capture_output=True, text=True).stdout)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    au = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    num, den = (v.get("avg_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den or 1) if float(den or 0) else 0.0
    fmt = info.get("format", {})
    sp = {"width": v.get("width"), "height": v.get("height"), "fps": round(fps, 3),
          "video_bitrate_kbps": round(int(v.get("bit_rate") or 0) / 1000),
          "audio_bitrate_kbps": round(int(au.get("bit_rate") or 0) / 1000),
          "duration": round(float(fmt.get("duration") or 0), 3),
          "size_mb": round(float(fmt.get("size") or 0) / 1048576, 1),
          "pix_fmt": v.get("pix_fmt")}
    ok, fails = SPEC.check(sp)
    man = {
        "date": a.date, "song": a.song, "artist": a.artist, "vocal": a.vocal,
        "src": src, "win": [a.start, a.end], "dur": dur, "seed": seed,
        "fps": a.fps, "res": a.res, "koma": a.koma, "bitrate": bitrate, "jq": a.jq,
        "pages": pages, "out": out_mp4,
        "specs": sp, "specs_ok": ok, "spec_fails": fails,
        "stages_s": stages, "total_s": round(time.time() - t_all, 1),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    mp = os.path.join(outdir, "manifest.json")
    # songs[] 是 songcut.qc 的入口格式（`_pick_entry` 按 mp4 绝对路径匹配）。
    # 此前 pv_one 只写扁平字段，QC 一条都读不到 —— 于是「一条命令出片」的最后
    # 一步仍然要手工拼 manifest（RULES §88 的断点④），等于没闭环。
    man["songs"] = [{
        "mp4": os.path.abspath(out_mp4),
        "song": a.song, "artist": a.artist, "vocal": a.vocal,
        "cut_start": float(a.start), "cut_end": float(a.end), "dur": float(dur),
        # 原唱官方时长：D01 拿它判「成片有没有被切短」。取不到就留空，
        # QC 会明确写「缺 orig_duration_s，跳过」而不是误判。
        "orig_duration_s": (round(linfo["netease_duration_ms"] / 1000.0, 3)
                            if linfo.get("netease_duration_ms") else None),
        "timeline_offset_s": 0.0,
        "lrc_path": os.path.abspath(lrc), "lrc_src_path": os.path.abspath(lrc),
        "pipeline": "pv_one/%dK%ds-koma%d" % (a.res, a.fps, a.koma),
        "storyboard": os.path.abspath(sb),
    }]
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)

    log("=== 完成：%s ===" % out_mp4)
    log("规格 %sx%s @%.0ffps %d kbps %s｜规格 %s" % (
        sp["width"], sp["height"], sp["fps"], sp["video_bitrate_kbps"],
        "%.1f MB" % sp["size_mb"], "✓" if ok else "✗ " + "; ".join(fails)))
    log("耗时：%s｜合计 %.1fs" % (
        "，".join("%s %.0fs" % (k, v2) for k, v2 in stages.items()), man["total_s"]))
    log("manifest → %s" % mp)
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
