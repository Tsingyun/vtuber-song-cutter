# -*- coding: utf-8 -*-
"""pvlink.py —— 歌切产物 → 文字 PV 的命令行入口（songcut/pvlink 的薄壳）。

为什么需要它
------------
`tools/pv_one.py` 是「手书正门」，但它要人把切点、原唱、歌词逐个抄进命令行；
而这些东西歌切侧**早就定稿写进 segments 了**。抄一遍就多一处可能抄错的地方
（2026-11-01 那两首静默退回旧版式，根因就是这个形态）。

这里把「查歌切产物」变成一条命令：
  · list / show  —— 看某天切了哪几首、每首的窗口与原唱是什么
  · bundle       —— 一次取全 PV 需要的素材（窗口/原唱/歌词/封面/人物序列），可 --json
  · pv-one       —— 直接用 segments 起跑 pv_one，不再手抄切点
  · doctor       —— 只读体检：源在不在、歌词缓存有没有、人物序列抽没抽

用法
----
    python tools/pvlink.py sessions
    python tools/pvlink.py list   --date 2026-11-01
    python tools/pvlink.py show   --date 2026-11-01 --song 小丑的品格
    python tools/pvlink.py bundle --date 2026-11-01 --song 小丑的品格 --json --lrc-out _tmp/x.lrc
    python tools/pvlink.py doctor --date 2026-11-01
    python tools/pvlink.py pv-one --date 2026-11-01 --song 小丑的品格 --res 1 --fps 30
"""
import argparse
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from songcut import pvlink as PL          # noqa: E402
from songcut import style as STYLE_SRC    # noqa: E402


def _dump(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_sessions(a):
    rows = PL.sessions()
    if a.json:
        _dump(rows)
        return 0
    if not rows:
        print("没有任何 segments（%s）" % PL.dirs()["segments"])
        return 1
    print("%-12s %3s  %s" % ("日期", "首数", "segments"))
    for r in rows:
        print("%-12s %3d  %s" % (r["date"], r["n_songs"], os.path.basename(r["path"])))
    print("\nworkdir：%s" % PL.dirs()["workdir"])
    return 0


def cmd_list(a):
    ses = PL.load_segments(a.date)
    songs = ses.get("songs") or []
    if a.json:
        _dump({"date": ses.get("date"), "segments": ses["_path"],
               "songs": [{k: s.get(k) for k in
                          ("seq", "title_guess", "fetch_title", "artist",
                           "cut_start_abs", "cut_end_abs", "start", "end")}
                         for s in songs]})
        return 0
    print("%s  %d 首  ← %s" % (ses.get("date"), len(songs), os.path.basename(ses["_path"])))
    print("%-3s %-22s %-16s %10s %10s %8s" % ("#", "歌名", "原唱", "切点起", "切点止", "时长"))
    for s in songs:
        st = s.get("cut_start_abs", s.get("start"))
        en = s.get("cut_end_abs", s.get("end"))
        dur = (float(en) - float(st)) if (st is not None and en is not None) else 0
        mark = "" if (s.get("cut_start_abs") is not None and s.get("cut_end_abs") is not None) else "  (粗窗口)"
        print("%-3s %-22s %-16s %10.2f %10.2f %8.1f%s"
              % (s.get("seq"), (s.get("title_guess") or "")[:22],
                 (s.get("artist") or "")[:16], float(st or 0), float(en or 0), dur, mark))
    return 0


def cmd_show(a):
    b = PL.bundle(a.date, a.song, a.seq, online=a.online, with_source=True)
    if a.json:
        b = dict(b)
        b["lrc"] = dict(b["lrc"])
        b["lrc"].pop("text", None)
        _dump(b)
        return 0
    print("《%s》%s  ← %s（第 %s 首，匹配方式 %s）"
          % (b["title"], b["artist"], os.path.basename(b["segments"]), b["seq"], b["match"]))
    w = b["window"]
    print("  窗口   %.2f → %.2f  (%.1fs, %s)" % (w["start"], w["end"], w["dur"], w["source"]))
    print("  源     %s" % (b["source"].get("path") or "不可用：" + b["source"].get("error", "")))
    print("  封面   %s" % (b["cover"]["path"] if b["cover"]["exists"] else "缺失"))
    print("  歌词   %s（%d 行，剔 %d 行）" % (b["lrc"]["source"], b["lrc"]["lines"],
                                            len(b["lrc"]["dropped"])))
    print("  人物   %s" % (b["assets"]["live"] or "未抽（跑 tools/pv_live.py）"))
    print("  JIZURA %s" % (b["assets"]["jz"] or "未渲（跑 tools/pv_jz.cjs）"))
    if b["missing"]:
        print("  缺口：")
        for m in b["missing"]:
            print("    - " + m)
    return 0


def cmd_bundle(a):
    b = PL.bundle(a.date, a.song, a.seq, online=a.online, with_source=a.with_source)
    if a.lrc_out:
        if not b["lrc"]["text"]:
            print("歌词取不到，%s 未写。" % a.lrc_out, file=sys.stderr)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(a.lrc_out)), exist_ok=True)
            io.open(a.lrc_out, "w", encoding="utf-8", newline="\n").write(b["lrc"]["text"])
            print("[pvlink] 歌词 → %s（%d 行，来源 %s）"
                  % (a.lrc_out, b["lrc"]["lines"], b["lrc"]["source"]), file=sys.stderr)
    if a.json:
        out = dict(b)
        out.pop("lrc_dump", None)
        if not a.with_lrc:
            out["lrc"].pop("text", None)
        _dump(out)
    else:
        print("窗口 %.2f→%.2f (%.1fs) | 原唱 %s | 歌词 %s | 封面 %s | 人物 %s | JIZURA %s"
              % (b["window"]["start"], b["window"]["end"], b["window"]["dur"],
                 b["artist"] or "?", b["lrc"]["source"],
                 "有" if b["cover"]["exists"] else "无",
                 "有" if b["style_ok"]["live"] else "无",
                 "有" if b["style_ok"]["jz"] else "无"))
        for m in b["missing"]:
            print("  缺口：%s" % m)
    return 0


def cmd_doctor(a):
    rep = PL.doctor(date=a.date, online=a.online, limit=a.limit)
    if a.json:
        _dump(rep)
        return 0
    print("配置 %s" % rep["config"])
    print("运行时 " + "  ".join("%s=%s%s" % (k, v, "" if rep["runtime_ok"][k] else " ✗")
                               for k, v in rep["runtime"].items()))
    for k, v in rep["dirs"].items():
        print("  %-15s %s %s" % (k, "✓" if v["exists"] else "✗", v["path"]))
    s = rep["summary"]
    print("\n场次 %d，歌曲 %d：歌词 %d / 人物 %d / JIZURA %d"
          % (s["sessions"], s["songs"], s["with_lrc"], s["with_live"], s["with_jz"]))
    for row in rep["sessions"]:
        print("\n%s  %d 首  源 %s" % (row["date"], row["n_songs"],
                                     row.get("source") or "✗ " + (row.get("source_error") or "")))
        for sg in row["songs"]:
            print("  %s %-20s %9.2f→%9.2f %-8s 歌词:%-6s 封面:%s 人物:%s"
                  % (sg["seq"], sg["title"][:20], sg["window"][0], sg["window"][1],
                     sg["win_src"], sg["lrc"], "有" if sg["cover"] else "无",
                     "有" if sg["live"] else "无"))
            for m in sg["missing"]:
                print("      ! " + m)
    return 0


def cmd_pv_one(a):
    """用 segments 起跑 pv_one：不再手抄切点/原唱/歌词。"""
    b = PL.bundle(a.date, a.song, a.seq, online=a.online, with_source=True)
    if b["missing"]:
        print("[pvlink] 起跑前有缺口（照旧执行，缺的层会逐层降级）：", file=sys.stderr)
        for m in b["missing"]:
            print("   - " + m, file=sys.stderr)
    cmd = [sys.executable, os.path.join(ROOT, "tools", "pv_one.py"),
           "--date", b["date"], "--from-segments", "--song", b["title"]]
    if a.seq:
        cmd += ["--seq", str(a.seq)]
    if a.online:
        cmd += ["--online"]
    cmd += a.rest
    print("[pvlink] " + " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def cmd_geom(a):
    g = PL.geometry(a.preset)
    if a.json:
        _dump(g)
        return 0
    for k in ("head", "body", "wide", "face"):
        if k in g:
            print("%-5s %s" % (k, g[k]))
    print("\n序列名（唯一真源）：%s" % "、".join(PL.SEQUENCES))
    return 0


def cmd_style(a):
    rows = []
    for d in PL.sessions()[:20]:
        for s in (PL.load_segments(d["date"]).get("songs") or []):
            live, jz = STYLE_SRC.asset_paths(os.path.join(PL.dirs()["cuts"], d["date"]),
                                             s.get("title_guess") or "")
            rows.append({"date": d["date"], "title": s.get("title_guess"),
                         "live": live, "jz": jz})
    if a.json:
        _dump(rows)
        return 0
    print("%-12s %-24s %-6s %-6s" % ("日期", "歌名", "人物", "JIZURA"))
    for r in rows:
        print("%-12s %-24s %-6s %-6s" % (r["date"], (r["title"] or "")[:24],
                                         "有" if r["live"] else "-",
                                         "有" if r["jz"] else "-"))
    return 0


def main():
    ap = argparse.ArgumentParser(description="歌切产物 → 文字 PV 的接入层")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sessions", help="列出有 segments 的场次")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_sessions)

    p = sub.add_parser("list", help="列出某天切了哪些歌")
    p.add_argument("--date", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="看一首的素材全貌")
    p.add_argument("--date", required=True)
    p.add_argument("--song", default="")
    p.add_argument("--seq", type=int, default=None)
    p.add_argument("--online", action="store_true", help="歌词缓存未命中时联网补")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("bundle", help="导出素材包（可写 .lrc 供其它工具消费）")
    p.add_argument("--date", required=True)
    p.add_argument("--song", default="")
    p.add_argument("--seq", type=int, default=None)
    p.add_argument("--online", action="store_true")
    p.add_argument("--with-source", action="store_true", help="附带定位源录播（走 find_video）")
    p.add_argument("--no-lrc", dest="with_lrc", action="store_false", default=True)
    p.add_argument("--lrc-out", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_bundle)

    p = sub.add_parser("doctor", help="只读体检")
    p.add_argument("--date", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--online", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("pv-one", help="用 segments 起跑 tools/pv_one.py")
    p.add_argument("--date", required=True)
    p.add_argument("--song", default="")
    p.add_argument("--seq", type=int, default=None)
    p.add_argument("--online", action="store_true")
    p.add_argument("rest", nargs=argparse.REMAINDER, help="透传给 pv_one 的参数")
    p.set_defaults(fn=cmd_pv_one)

    p = sub.add_parser("geom", help="打印画框几何（唯一真源）")
    p.add_argument("--preset", default="pv", choices=sorted(PL.GEOMETRY_PRESETS))
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_geom)

    p = sub.add_parser("style", help="各场素材的 live/jz 就绪情况")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_style)

    a = ap.parse_args()
    try:
        return a.fn(a) or 0
    except PL.PvLinkError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
