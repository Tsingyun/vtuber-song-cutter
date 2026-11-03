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
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from songcut import pvlink as PL                       # noqa: E402  歌切产物的唯一接入层

PY = sys.executable
_RT = PL.runtime()          # ffmpeg/ffprobe/node 的环境变量 → config → 内置探测，只有一份
FFMPEG = _RT["ffmpeg"]
FFPROBE = _RT["ffprobe"]
NODE = _RT["node"]
NODE_PATH = _RT["node_path"]


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
    """定位当天录播。统一走 pvlink → song_cutter.find_video（排除 .part、多场取最长）。

    以前这里是 glob + 取最大文件，压缩中断留下的 `<name>_compressed.mp4.part.mp4`
    体积同样很大，会被选中，然后在 ffprobe 处整条链崩掉。
    """
    return PL.source(date, explicit)


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


def lrc_from_segments(bundle, work):
    """把歌切侧定稿的歌词（含人工剔行）落到 work/lyric.lrc。

    为什么不再自己抓：segments 里的 `lrc_drop_lines` 是**人逐行核过**的专辑介绍语与
    制作名单（《小丑的品格》剔 5 行、《带我走》剔 14 行）。PV 侧重抓一份再自己数行，
    等于把已经核过的东西重做一遍，且大概率数错 —— 数错就是让制作名单上台唱。
    """
    text = (bundle.get("lrc") or {}).get("text")
    if not text:
        return "", {}
    p = os.path.join(work, "lyric.lrc")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return p, {}


def resolve_concept(a):
    """``--concept auto``：意象表写的就是本片这首歌才启用，否则自动关。

    为什么要有 auto：意象映射表是**逐句定稿**（Innocence 是 47 句，逐句指定意象与版面），
    拿它去跑别的歌必然 `映射 47 句 ≠ 窗口内 N 句`。报错本身是对的，但把它设成默认值
    的结果是「通用路径一进来就死」—— 换歌必须记得加 `--concept ""`，忘了就是一次失败。
    auto 保留原来「跑 Innocence 不带参就是晨光绘本」的行为，其余歌自动走抽卡路径。
    """
    if a.concept != "auto":
        return a.concept, ""
    p = a.imagery or os.path.join(ROOT, "docs", "innocence-v2-imagery-map.json")
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return "", "意象表读不到（%s），按不启用处理" % os.path.basename(p)
    want = ((m.get("_meta") or {}).get("song") or "").strip()
    norm = lambda s: re.sub(r"[\s\W_]+", "", (s or "").lower())  # noqa: E731
    if want and norm(want) == norm(a.song):
        return "dawn", "意象表命中《%s》" % want
    return "", "意象表是《%s》的逐句定稿，与本片《%s》不符" % (want or "?", a.song)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="直播日期 YYYY-MM-DD（用于定位录播与命名）")
    ap.add_argument("--song", default="", help="歌名；--from-segments 时可省（该场只有一首时）")
    ap.add_argument("--artist", default="", help="原唱（画面标原唱，见 RULES 铁律 1）")
    ap.add_argument("--vocal", default="")
    ap.add_argument("--src", default="", help="显式指定录播 mp4（缺省按日期定位）")
    ap.add_argument("--start", type=float, default=None, help="成片起点（源视频绝对秒）")
    ap.add_argument("--end", type=float, default=None, help="成片终点")
    ap.add_argument("--from-segments", action="store_true",
                    help="切点/原唱/歌词/源全部取自歌切侧 segments（推荐：不再手抄切点）")
    ap.add_argument("--seq", type=int, default=None, help="--from-segments 时按序号取（重唱/多场）")
    ap.add_argument("--online", action="store_true",
                    help="--from-segments 时，歌词缓存未命中才联网补一份")
    ap.add_argument("--lrc", default="", help="LRC 路径（缺省联网取）")
    ap.add_argument("--concept", default="auto", choices=["", "auto", "dawn"],
                    help="auto=意象表写的就是本片这首歌才启用（缺省）；dawn=强制；空=关")
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
    # ---- 风格（唯一真源 songcut/style.py）----
    # pv_one 是「手书正门」，只出 B 风格；A 风格没有编排稿，走 song_cutter.py --style A。
    ap.add_argument("--style", default="B",
                    help="风格名（B/手书/lettering）；pv_one 只支持 B，A 请走 song_cutter.py")
    ap.add_argument("--jz", default="", help="JIZURA 帧序列的 jz.json（tools/pv_jz.cjs 产出）")
    ap.add_argument("--fig-spans", default="", help="人物挂点表 JSON（键=窗口内句号）")
    ap.add_argument("--no-arc", action="store_true", help="关掉风格弧线（默认按风格开）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析素材来源（切点/源/歌词）并打印，不抽音频不渲染")
    a = ap.parse_args()

    # ---- ⓪ 素材来源：--from-segments ⇒ 切点/原唱/歌词全部取自歌切侧定稿 ----
    # 为什么默认不这么做：手抄切点这条老路要留着（人工核定的临时窗口、试片段落）。
    # 但只要歌切侧已经有 segments，就不该再抄一遍 —— 抄错一次，成片会安静地错一整首。
    seg = None
    if a.from_segments:
        try:
            seg = PL.bundle(a.date, a.song, a.seq, online=a.online, with_source=True)
        except PL.PvLinkError as e:
            raise SystemExit(str(e))
        a.song = a.song or seg["title"]
        a.artist = a.artist or seg["artist"]
        # 只在**没给**的时候才用 segments 的窗口：显式 --start/--end 是「试片段」
        # 或「人工复核窗口」，无条件覆盖会让 from-segments 没法出短样片，
        # 想看 40s 就必须先渲染 269s。
        if a.start is None:
            a.start = seg["window"]["start"]
        if a.end is None:
            a.end = seg["window"]["end"]
        a.src = a.src or (seg["source"].get("path") or "")
        log("歌切产物：%s 第 %s 首（%s）｜窗口 %.2f → %.2f（%s，%.1fs）"
            % (os.path.basename(seg["segments"] or "?"), seg["seq"], seg["match"],
               a.start, a.end, seg["window"]["source"], seg["window"]["dur"]))
        if (a.start, a.end) != (seg["window"]["start"], seg["window"]["end"]):
            log("  ↳ 用显式窗口 %.2f → %.2f（覆盖 segments 的 %.2f → %.2f；"
                "只出这一段，歌词/时间轴按此窗口重算）"
                % (a.start, a.end, seg["window"]["start"], seg["window"]["end"]))
        for m in seg["missing"]:
            log("  ⚠ " + m)
    if not a.song:
        raise SystemExit("缺 --song（或加 --from-segments 从歌切产物取）")
    if a.start is None or a.end is None:
        raise SystemExit("缺 --start/--end（或加 --from-segments 从歌切产物取）")

    # ---- 意象驱动：只有映射表对应的那首歌才开（否则自动走抽卡，不再一进来就死）----
    a.concept, _why = resolve_concept(a)
    if _why:
        log("意象驱动 %s（%s）" % ("开" if a.concept else "关", _why))

    from songcut import style as STYLE_SRC
    try:
        ST = STYLE_SRC.resolve(a.style)
    except STYLE_SRC.StyleError as e:
        raise SystemExit(str(e))
    if not ST["pv"]:
        raise SystemExit("pv_one 只出 B 风格（手书）；A 风格（唱片）没有编排稿，"
                         "请用 song_cutter.py --style A")
    use_arc = bool(ST["arc"]) and not a.no_arc
    log("风格 %s（%s）｜版式 %s｜弧线 %s｜人物 %s｜JIZURA %s" % (
        ST["name"], ST["en"], ST["page"], "开" if use_arc else "关",
        "有" if a.live_json else "无", "有" if a.jz else "无"))

    dur = round(a.end - a.start, 3)
    if dur <= 1:
        raise SystemExit("--end 必须大于 --start")
    seed = a.seed or int(a.date.replace("-", "")) * 100 + 1
    outdir = a.outdir or os.path.join(ROOT, "cuts", a.date, a.song.replace("/", "_"))
    os.makedirs(outdir, exist_ok=True)
    work = os.path.join(outdir, "_work")
    os.makedirs(work, exist_ok=True)
    stages, t_all = {}, time.time()

    if a.dry_run:
        # 只解析「这片要用什么素材」，不抽音频、不渲染。验收接入层时用它，
        # 避免每次都等一遍 4K。缺什么也会照实列出来（missing）。
        plan = {"date": a.date, "song": a.song, "artist": a.artist,
                "window": [a.start, a.end], "dur": dur, "src": a.src or None,
                "style": "%s %s" % (ST["key"], ST["name"]), "arc": use_arc,
                "outdir": outdir, "lrc": a.lrc or None,
                "from_segments": bool(seg),
                "link": ({"segments": seg["segments"], "seq": seg["seq"],
                          "match": seg["match"], "window_source": seg["window"]["source"],
                          "lrc_source": seg["lrc"]["source"]} if seg else None),
                "missing": (seg or {}).get("missing") or []}
        if not plan["src"]:
            try:
                plan["src"] = resolve_src(a.date, "")
            except Exception as e:          # 源没了也要如实报，不假装能跑
                plan["src_error"] = str(e).splitlines()[0]
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

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
    elif seg and (seg["lrc"] or {}).get("text"):
        lrc, linfo = lrc_from_segments(seg, work)
        log("歌词：歌切侧缓存 %d 行（已按核定的行号剔掉 %d 行）"
            % (seg["lrc"]["lines"], len(seg["lrc"]["dropped"])))
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
        if a.jz:
            cmd += ["--jz", a.jz, "--jz-layer", "behind", "--jz-alpha", "0.40"]
        if a.fig_spans:
            cmd += ["--fig-spans", a.fig_spans]
        if use_arc:
            cmd += ["--arc"]
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
        "page": ST["page"],
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
        # 序列名由 live.json 决定（LIVE_SPANS 的 s1..s6），不再写死三档 ——
        # 写死时新格式一条都传不进去，六处该有人的镜头全空，且不报错。
        # dir 统一转绝对路径：引擎拼的是 'file:///' + dir，相对路径在
        # file:// 协议下解析到盘符根，不会解析到仓库目录（静默取不到图）。
        for k, v in lj.items():
            if k in ("face", "cut_start") or not isinstance(v, dict) or not v.get("dir"):
                continue
            d = v["dir"].replace("\\", "/")
            if not os.path.isabs(d):
                d = os.path.join(ROOT, d).replace("\\", "/")
            live[k] = dict(v, dir=d)
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
                  # FFMPEG_BIN 显式传下去：render_song.cjs 不再内置 D:/ffmpeg 绝对路径，
                  # 而 PATH 里不一定有 ffmpeg —— 不传就是「找不到 ffmpeg」。
                  env=dict(os.environ, NODE_PATH=NODE_PATH, FFMPEG_BIN=FFMPEG))
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
        "style": "%s %s" % (ST["key"], ST["name"]), "arc": use_arc,
        "src": src, "win": [a.start, a.end], "dur": dur, "seed": seed,
        "fps": a.fps, "res": a.res, "koma": a.koma, "bitrate": bitrate, "jq": a.jq,
        "pages": pages, "out": out_mp4,
        "specs": sp, "specs_ok": ok, "spec_fails": fails,
        "stages_s": stages, "total_s": round(time.time() - t_all, 1),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    # 溯源：这一片到底是从哪条 segments 的第几首、按什么方式匹配来的。
    # 没有这段，事后无法回答「成片窗口是谁定的」（手抄还是定稿），只能靠回忆。
    if seg:
        man["link"] = {
            "segments": os.path.abspath(seg["segments"]) if seg.get("segments") else None,
            "seq": seg["seq"], "match": seg["match"],
            "window_source": seg["window"]["source"],
            "lrc_source": seg["lrc"]["source"],
            "lrc_dropped": len(seg["lrc"]["dropped"]),
            "cover": seg["cover"]["path"],
            "missing_at_start": seg["missing"],
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
        # 来自 segments 时优先用 segments 的值（人工核定过的那个，见 RULES 铁律）。
        "orig_duration_s": (seg.get("orig_duration_s") if (seg and seg.get("orig_duration_s"))
                            else (round(linfo["netease_duration_ms"] / 1000.0, 3)
                                  if linfo.get("netease_duration_ms") else None)),
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

    # ---- ⑩ 场景图：随片落盘（统一时间轴 + 图层描述 + 静默错清单）
    # 出片之后最常被问的是「这一段为什么没人 / 人为什么不动」。把镜头、人物序列、
    # 帧覆盖算一遍存下来，比事后拿成片去猜快得多。校验失败不阻断出片（成片已经在了），
    # 但必须显式打出来 —— 不能让它只躺在 json 里。
    try:
        from songcut import mvgraph as MG
        g = MG.build(a.date, a.song, storyboard=sb,
                     live_json=(a.live_json or ""), seq=a.seq)
        gp = os.path.join(outdir, "graph.json")
        with open(gp, "w", encoding="utf-8") as f:
            json.dump(g, f, ensure_ascii=False, indent=1)
        _s = MG.summary(g)
        log("场景图：%d 层｜人物 %.1f%% 时长｜%d error / %d warn → %s"
            % (_s["layers"], _s["footage_pct"], _s["errors"], _s["warns"],
               os.path.basename(gp)))
        for c in g["checks"]:
            if c["level"] == "error":
                log("  ✗ [%s] %s" % (c["code"], c["msg"]))
    except Exception as e:              # 场景图是旁证，不该把成片交付拖垮
        log("场景图跳过：%s" % e)
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
