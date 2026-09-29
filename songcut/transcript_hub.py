#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""transcript_hub.py —— 直播转录「接入层」（上游优先、独立管线备用）

策略（两条来源，不得颠倒）：
  1. 主链路 = 复用上游已有的转录产物（config.json → srt_dir），不重复建设；
  2. 自建管线（见 config.json → transcript.fallback_*）仅在上游缺失/不可用/不完整时降级启用，
     绝不与上游并行维护两套等价实现；
  3. 无论来自哪条路，统一归一化为同一份输出契约（transcript.json + 归一化 SRT），
     对下游（歌切等）完全一致。

输出契约（v1）：
  {
    "schema": "transcript/v1",
    "date": "2026-09-25", "session": "<场次标识>",
    "source": {"kind": "upstream|fallback", "path": "...", "engine": "...", "produced_at": "..."},
    "timebase": {"origin": "recording", "unit": "second", "offset_sec": 0.0},
    "segments": [{"i":0,"start":240.55,"end":251.93,"text":"..."}],
    "stats":   {"count":1061,"span_sec":...,"first_sec":...,"last_sec":...,"chars":...},
    "health":  {"ok":true,"checks":[{"id":"...","ok":true,"detail":"..."}]}
  }

用法：
  python transcript_hub.py --scan                    # 盘点上游全部转录资产
  python transcript_hub.py --date 2026-09-25         # 取某一场（上游优先，失败给降级方案）
  python transcript_hub.py --date 2026-09-25 --run-fallback   # 上游不可用时真的跑自建管线
"""
import argparse
import glob
import io
import json
import os
import re
import subprocess
import sys
import time

# ---------------- 配置 ----------------
# 全部取自仓库根 config.json（见各键说明），代码内不含任何本机路径与身份信息。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from songcut import config as CFG      # noqa: E402

_CREC = CFG.expand(CFG.get("recording_root") or "")
_CSRT = CFG.expand(CFG.get("srt_dir") or "")
DEFAULT_CFG = {
    "upstream": {
        # 上游转写产物目录（SRT / 同名 TXT 元信息 / 状态文件）
        "srt_dir": _CSRT,
        "txt_dir": os.path.join(os.path.dirname(_CSRT), "TXT") if _CSRT else "",
        "hist_srt_dir": CFG.expand(CFG.path("transcript", "hist_srt_dir", default="") or ""),
        "state_json": os.path.join(os.path.dirname(_CSRT), "state.json") if _CSRT else "",
    },
    "recording_root": _CREC,
    "ffmpeg": CFG.ffmpeg(),
    "ffprobe": CFG.ffprobe(),
    "fallback": {
        # 上游不可用时的本地转写兜底（指向你自己的转写脚本，留空即禁用）
        "enabled": bool(CFG.path("transcript", "fallback_script", default="")),
        "skill_dir": CFG.expand(CFG.path("transcript", "fallback_skill_dir", default="") or ""),
        "script": "scripts/transcribe.py",
        "python": CFG.expand(CFG.path("transcript", "fallback_python", default="") or ""),
        "engine": CFG.path("transcript", "fallback_engine", default="FunASR/SenseVoiceSmall+VAD+PUNC"),
    },
    # 健康检查阈值（= 降级触发条件，可调）
    "thresholds": {
        "min_cues": 50,            # 条目数下限（少于此视为空/残缺）
        "min_span_ratio": 0.60,    # 字幕覆盖时长 / 录播时长 下限
        "max_gap_sec": 600,        # 单段无字幕间隙上限（秒）；超过视为漏转
        "max_start_sec": 1800,     # 首句起点上限（秒）；超过说明时间轴不是录播 0 点
        "stale_hours": 6,          # 转录产物早于录播完成 N 小时 → 视为过期版本
    },
}


def load_cfg():
    if os.path.exists(CFG_PATH):
        try:
            c = json.load(io.open(CFG_PATH, encoding="utf-8"))
            for k, v in DEFAULT_CFG.items():
                c.setdefault(k, v)
            return c
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_CFG))


# ---------------- 基础 ----------------
def hms_to_sec(s):
    s = s.strip().replace(",", ".")
    p = s.split(":")
    if len(p) == 3:
        return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2])
    if len(p) == 2:
        return int(p[0]) * 60 + float(p[1])
    return float(p[0])


def sec_to_srt(t):
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return "%02d:%02d:%02d,%03d" % (h, m, int(s), int(round((s - int(s)) * 1000)))


SRT_RE = re.compile(r"(\d{1,2}:\d{2}:\d{2},\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2},\d{1,3})\s*\n(.*?)(?=\n\n|\Z)", re.S)


def parse_srt(path):
    txt = io.open(path, encoding="utf-8-sig", errors="replace").read()
    out = []
    for m in SRT_RE.finditer(txt):
        s, e, t = hms_to_sec(m.group(1)), hms_to_sec(m.group(2)), m.group(3).strip()
        if t:
            out.append((s, e, t))
    out.sort(key=lambda x: x[0])
    return out


TXT_META_RE = {
    "audio_sec": re.compile(r"音频时长[:：]\s*(\d+)"),
    "chars": re.compile(r"文本字数[:：]\s*(\d+)"),
    "cues": re.compile(r"分句数量[:：]\s*(\d+)"),
    "made_at": re.compile(r"转写时间[:：]\s*([\d\-: ]+)"),
}


def parse_txt_meta(path):
    """上游 TXT 头部带元信息（时长/字数/分句数/转写时间），用于交叉校验 SRT 完整性。"""
    meta = {}
    if not os.path.exists(path):
        return meta
    head = io.open(path, encoding="utf-8-sig", errors="replace").read(1200)
    for k, pat in TXT_META_RE.items():
        m = pat.search(head)
        if m:
            meta[k] = m.group(1).strip() if k == "made_at" else float(m.group(1))
    return meta


def probe_duration(ffprobe, path):
    if not (ffprobe and os.path.exists(ffprobe) and os.path.exists(path)):
        return None
    try:
        p = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "json", path], capture_output=True, text=True, timeout=180)
        return float(json.loads(p.stdout)["format"]["duration"])
    except Exception:
        return None


def find_video(rec_root, date):
    """录播视频：优先 <date>/ 目录下的 *_compressed.mp4（跳过未下载完的 .part）。"""
    d = os.path.join(rec_root, date)
    if not os.path.isdir(d):
        return None
    cands = [p for p in glob.glob(os.path.join(d, "*.mp4"))
             if ".part" not in os.path.basename(p).lower()]
    if not cands:
        return None
    return max(cands, key=lambda p: os.path.getsize(p))


# ---------------- 上游盘点 ----------------
def scan_upstream(cfg, verbose=True):
    """盘点上游全部可复用转录资产：SRT（主）+ TXT（元信息）+ 历史档案。"""
    rows = []
    for kind, dir_key in (("srt", "srt_dir"), ("hist", "hist_srt_dir")):
        d = cfg["upstream"].get(dir_key)
        if not d or not os.path.isdir(d):
            continue
        for p in sorted(glob.glob(os.path.join(d, "*.srt"))):
            segs = parse_srt(p)
            span = (max(e for _s, e, _t in segs) - segs[0][0]) if segs else 0.0
            gaps = []
            for i in range(len(segs) - 1):
                g = segs[i + 1][0] - segs[i][1]
                if g > 300:
                    gaps.append(round(g / 60.0, 1))
            rows.append({
                "kind": kind, "path": p, "file": os.path.basename(p),
                "cues": len(segs), "first": segs[0][0] if segs else None,
                "last": (max(e for _s, e, _t in segs) if segs else None),
                "span_h": round(span / 3600.0, 2), "gaps_min": gaps[:5],
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(p))),
                "kb": int(os.path.getsize(p) / 1024),
            })
    return rows


def locate_upstream(cfg, date):
    ymd = date.replace("-", "")
    hits = []
    for key in ("srt_dir", "hist_srt_dir"):
        d = cfg["upstream"].get(key)
        if not d or not os.path.isdir(d):
            continue
        for p in sorted(glob.glob(os.path.join(d, ymd + "*.srt"))):
            hits.append({"path": p, "kind": "upstream",
                         "txt": os.path.join(cfg["upstream"].get("txt_dir", ""),
                                             os.path.basename(p)[:-4] + ".txt")})
    return hits


# ---------------- 健康检查 = 降级触发条件 ----------------
def health_check(segments, meta, rec_dur, th, srt_mtime=None, video_mtime=None):
    """返回 (ok, checks[])。任一关键项失败 → 触发降级。"""
    checks = []

    def add(cid, ok, detail):
        checks.append({"id": cid, "ok": bool(ok), "detail": detail})

    add("E_EXISTS", segments, "条目 %d" % len(segments) if segments else "无有效字幕条目")
    if not segments:
        return False, checks

    first = segments[0][0]
    last = max(e for _s, e, _t in segments)
    span = last - first
    add("E_AXIS", first <= th["max_start_sec"],
        "首句 %.0fs（上限 %.0fs）→ 时间轴%s与录播 0 点对齐" % (
            first, th["max_start_sec"], "已" if first <= th["max_start_sec"] else "未"))

    add("E_VOLUME", len(segments) >= th["min_cues"],
        "条目 %d（下限 %d）" % (len(segments), th["min_cues"]))

    if rec_dur:
        ratio = span / rec_dur
        add("E_COVERAGE", ratio >= th["min_span_ratio"],
            "字幕跨度 %.0fs / 录播 %.0fs = %.0f%%（下限 %.0f%%）" % (
                span, rec_dur, ratio * 100, th["min_span_ratio"] * 100))
    else:
        add("E_COVERAGE", True, "未取得录播时长，跳过覆盖校验")

    gaps = []
    for i in range(len(segments) - 1):
        g = segments[i + 1][0] - segments[i][1]
        if g > th["max_gap_sec"]:
            gaps.append((round(segments[i][1]), round(g)))
    add("E_GAP", not gaps, "无 >%.0fs 空白" % th["max_gap_sec"] if not gaps
        else "发现 %d 处超长空白（首处 @%ss 持续 %ss）" % (len(gaps), gaps[0][0], gaps[0][1]))

    if meta.get("cues"):
        diff = abs(meta["cues"] - len(segments))
        add("E_META_MATCH", diff <= max(5, meta["cues"] * 0.02),
            "SRT %d 条 vs TXT 元信息 %d 条（差 %d）" % (len(segments), meta["cues"], diff))

    if srt_mtime and video_mtime:
        add("E_FRESH", srt_mtime >= video_mtime - th["stale_hours"] * 3600,
            "转录 mtime %s vs 录播 mtime %s" % (
                time.strftime("%m-%d %H:%M", time.localtime(srt_mtime)),
                time.strftime("%m-%d %H:%M", time.localtime(video_mtime))))

    hard = {"E_EXISTS", "E_AXIS", "E_VOLUME", "E_COVERAGE"}
    ok = all(c["ok"] for c in checks if c["id"] in hard)
    return ok, checks


# ---------------- 归一化输出（对下游统一） ----------------
def build_transcript(date, session, src_kind, src_path, engine, segments, checks, meta):
    first = segments[0][0] if segments else 0.0
    last = (max(e for _s, e, _t in segments) if segments else 0.0)
    return {
        "schema": "transcript/v1",
        "date": date, "session": session,
        "source": {"kind": src_kind, "path": src_path, "engine": engine,
                   "produced_at": time.strftime("%Y-%m-%d %H:%M:%S")},
        "timebase": {"origin": "recording", "unit": "second", "offset_sec": 0.0,
                     "note": "与录播视频 0 点对齐；下游无需再做时间换算"},
        "segments": [{"i": i, "start": round(s, 3), "end": round(e, 3), "text": t}
                     for i, (s, e, t) in enumerate(segments)],
        "stats": {"count": len(segments), "first_sec": round(first, 3),
                  "last_sec": round(last, 3), "span_sec": round(last - first, 3),
                  "chars": sum(len(t) for _s, _e, t in segments)},
        "upstream_meta": meta,
        "health": {"ok": all(c["ok"] for c in checks if c["id"] != "E_GAP"),
                   "checks": checks},
    }


def write_outputs(obj, out_dir, tag):
    os.makedirs(out_dir, exist_ok=True)
    jp = os.path.join(out_dir, "transcript-%s.json" % tag)
    io.open(jp, "w", encoding="utf-8").write(json.dumps(obj, ensure_ascii=False, indent=1))
    sp = os.path.join(out_dir, "transcript-%s.srt" % tag)
    with io.open(sp, "w", encoding="utf-8") as f:
        for i, sg in enumerate(obj["segments"], 1):
            f.write("%d\n%s --> %s\n%s\n\n" % (i, sec_to_srt(sg["start"]), sec_to_srt(sg["end"]), sg["text"]))
    return jp, sp


# ---------------- 降级：自建管线（仅备用） ----------------
def fallback_plan(cfg, date, video, reasons):
    fb = cfg.get("fallback", {})
    return {
        "enabled": bool(fb.get("enabled")) and bool(video),
        "reason": reasons,
        "engine": fb.get("engine", "FunASR"),
        "video": video,
        "cmd": [fb.get("python", "python"),
                os.path.join(fb.get("skill_dir", ""), fb.get("script", "")),
                "--input", video or "", "--out", os.path.join(os.path.dirname(CFG_PATH), "fallback", date)],
        "note": "无本地录播视频时不自建（没有输入源）；此时应先获取录播，而不是跑转录。",
    }


def run_fallback(plan):
    if not plan.get("enabled"):
        return {"ran": False, "why": plan.get("note") or "未启用"}
    try:
        p = subprocess.run(plan["cmd"], capture_output=True, text=True, timeout=7200)
        return {"ran": True, "rc": p.returncode, "tail": (p.stdout or p.stderr)[-500:]}
    except Exception as e:
        return {"ran": False, "why": repr(e)}


# ---------------- CLI ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="直播日期 YYYY-MM-DD")
    ap.add_argument("--scan", action="store_true", help="盘点上游全部转录资产")
    ap.add_argument("--run-fallback", action="store_true", help="上游不可用时真的执行自建管线")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(CFG_PATH), "out"))
    args = ap.parse_args()
    cfg = load_cfg()
    th = cfg["thresholds"]

    if args.scan or not args.date:
        rows = scan_upstream(cfg)
        print("上游转录资产盘点：共 %d 份（srt=主目录，hist=历史档案）" % len(rows))
        print("%-4s %-52s %6s %8s %8s %7s %-16s %s" % ("kind", "file", "cues", "first", "last", "span_h", "mtime", "gaps>5min"))
        for r in rows:
            print("%-4s %-52s %6d %8s %8s %7.2f %-16s %s" % (
                r["kind"], r["file"][:52], r["cues"],
                ("%.0f" % r["first"]) if r["first"] is not None else "-",
                ("%.0f" % r["last"]) if r["last"] is not None else "-",
                r["span_h"], r["mtime"], r["gaps_min"] or "无"))
        if not args.date:
            return 0

    ymd = args.date.replace("-", "")
    video = find_video(cfg["recording_root"], args.date)
    rec_dur = probe_duration(cfg["ffprobe"], video) if video else None
    print("\n[%s] 录播：%s（%.0fs）" % (args.date, os.path.basename(video) if video else "无", rec_dur or 0))

    hits = locate_upstream(cfg, args.date)
    print("[%s] 上游命中转录 %d 份" % (args.date, len(hits)))

    for h in hits:
        segs = parse_srt(h["path"])
        meta = parse_txt_meta(h["txt"])
        ok, checks = health_check(segs, meta, rec_dur, th,
                                  srt_mtime=os.path.getmtime(h["path"]),
                                  video_mtime=os.path.getmtime(video) if video else None)
        session = os.path.basename(h["path"])[:-4]
        print("   %s：%d 条，健康=%s" % (session[:46], len(segs), "OK" if ok else "FAIL"))
        for c in checks:
            if not c["ok"]:
                print("      ✗ %s %s" % (c["id"], c["detail"]))
        if ok:
            obj = build_transcript(args.date, session, "upstream", h["path"],
                                   "FunASR/SenseVoiceSmall(上游 bili-live-summary)", segs, checks, meta)
            jp, sp = write_outputs(obj, args.out, session[:24])
            print("   → 复用上游：%s" % jp)
            print("   → 归一化 SRT：%s" % sp)
            return 0

    reasons = ["上游无该日期转录"] if not hits else ["上游转录未通过健康检查（见上）"]
    plan = fallback_plan(cfg, args.date, video, reasons)
    print("   降级：%s" % ("、".join(reasons)))
    print("   自建管线：%s（%s）" % ("可启用" if plan["enabled"] else "不可启用 —— " + plan["note"],
                                 plan["engine"]))
    if args.run_fallback:
        print("   执行结果：%s" % json.dumps(run_fallback(plan), ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
