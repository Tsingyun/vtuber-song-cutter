#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""新播放器（Live Cue）的素材准备与本地服务。

两个子命令：

  prepare  从 cuts/<date>/manifest.json 里取一首歌的切点，抽出**真实录播片段**
           （1080p60 + 原始音轨）并跑人物跟踪，产出播放器直接消费的素材：
             _media_cache/live/<date>/<idx>.mp4   片段（faststart，可 Range 拖动）
             _media_cache/live/<date>/<idx>.lrc   轴（cut 相对时间，未加 head_pad）
             _media_cache/live/<date>/<idx>.track.json  人物中心轨迹
             _media_cache/live/<date>/<idx>.json  元数据
             _media_cache/live/index.json         总索引

  serve    带 HTTP Range 的本地静态服务（Chromium 拖动进度必须 206，否则只能顺播）

为什么单独抽片段而不是直接播 14GB 录播：
  · 浏览器要 seek 到 39 分钟处，需要 moov 在前 + Range；原录播 moov 在尾部，
    首帧要等整个 14GB 下载完，完全不可用；
  · 片段长度 = 一首歌，播放器的进度条与歌词轴口径一致（都以 cut_start 为 0）。

用法：
  python tools/liveplayer.py prepare --date 2026-11-27 --index 0
  python tools/liveplayer.py serve --port 8777
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "_media_cache" / "live"
PAGE_DIR = ROOT / "renderer" / "player" / "live"
MIME = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".lrc": "text/plain; charset=utf-8", ".mp4": "video/mp4",
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml", ".webp": "image/webp", ".ico": "image/x-icon",
}


# ---------------------------------------------------------------- ffmpeg
def _bin(name: str) -> str:
    for c in (os.environ.get(name.upper()), name, "D:/ffmpeg/bin/%s.exe" % name):
        if not c:
            continue
        try:
            subprocess.run([c, "-version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True)
            return c
        except Exception:
            continue
    raise SystemExit("找不到 %s（设置环境变量 %s 或在 PATH 里）" % (name, name.upper()))


def _run(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)


def _encoders(ffmpeg: str) -> str:
    return _run([ffmpeg, "-hide_banner", "-encoders"]).stdout.decode("utf-8", "ignore")


# ---------------------------------------------------------------- lrc
TS = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")


def _rebase(text: str, offset: float) -> str:
    """把所有行首时间整体减去 offset（用于把「成片时间」换算回「cut 相对时间」）。"""
    def rep(m):
        t = int(m.group(1)) * 60 + float(m.group(2).replace(":", ".")) - offset
        t = max(0.0, t)
        return "[%02d:%05.2f]" % (int(t // 60), t % 60)
    out = []
    for ln in text.splitlines():
        out.append(TS.sub(rep, ln, count=1) if ln.strip().startswith("[") else ln)
    return "\n".join(out) + "\n"


def _strip_comments(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.strip().startswith("#")) + "\n"


# 界面固定文案 + 标点 + 数字，保证子集字体覆盖所有可见字符
UI_CHARS = ("岁己歌词原唱演唱曲目点击开始空格暂停快进重播读取中暂无数据"
            "零一二三四五六七八九十秒分时"
            "0123456789:./·—–-、。！？「」（）【】…♪~ "
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _build_font(charset: str, out_path: Path) -> bool:
    """按当前曲目文本做衬线字体子集（woff2）。找不到字体就跳过，页面回退系统衬线。"""
    try:
        from fontTools import subset
    except Exception:
        print("  ⚠ 没有 fontTools，跳过字体子集（页面回退系统衬线）")
        return False
    cands = [os.environ.get("LIVE_SERIF_FONT"),
             "C:/Windows/Fonts/NotoSerifJP-VF.ttf",
             "C:/Windows/Fonts/NotoSerifSC-VF.ttf",
             "/System/Library/Fonts/Supplemental/Songti.ttc"]
    src = next((c for c in cands if c and Path(c).exists()), None)
    if not src:
        print("  ⚠ 未找到衬线字体源，跳过子集")
        return False
    try:
        opts = subset.Options()
        opts.flavor = "woff2"
        opts.layout_features = ["*"]
        opts.name_IDs = ["*"]
        opts.notdef_outline = True
        font = subset.load_font(src, opts)
        ss = subset.Subsetter(options=opts)
        ss.populate(text=charset)
        ss.subset(font)
        subset.save_font(font, str(out_path), opts)
        print("  [font] %s → %.0f KB" % (Path(src).name, out_path.stat().st_size / 1024))
        return True
    except Exception as e:
        print("  ⚠ 字体子集失败：%s" % e)
        return False


# ---------------------------------------------------------------- prepare
def _streamer_name() -> str:
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        return (cfg.get("streamer") or {}).get("name") or "岁己"
    except Exception:
        return "岁己"


def _seg_lookup(date: str, title: str) -> dict:
    """manifest 只留成片级字段，原唱/演唱者在 segments/*.json 里。
    同名多首时按标题取第一个命中；取不到就返回空，不影响出片。"""
    key = date.replace("-", "")
    out = {}
    for p in sorted((ROOT / "segments").glob("*%s*.json" % key)):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        arr = d if isinstance(d, list) else (d.get("songs") or [])
        for s in arr:
            name = s.get("title") or s.get("title_guess") or ""
            if name and name.strip() == (title or "").strip():
                for k in ("artist", "vocal", "romaji", "producer"):
                    if s.get(k) and not out.get(k):
                        out[k] = s[k]
                if out:
                    return out
    return out


def prepare(date: str, index: int, src: str | None, force: bool) -> int:
    mpath = ROOT / "cuts" / date / "manifest.json"
    if not mpath.exists():
        print("没有台账：%s" % mpath, file=sys.stderr)
        return 2
    man = json.loads(mpath.read_text(encoding="utf-8"))
    songs = man.get("songs") or []
    if index < 0 or index >= len(songs):
        print("index 越界（共 %d 首）" % len(songs), file=sys.stderr)
        return 2
    seg = songs[index]
    cs, ce = seg.get("cut_start"), seg.get("cut_end")
    if cs is None or ce is None:
        print("这首歌没有切点（%s），先出片再用播放器" % seg.get("title"), file=sys.stderr)
        return 2
    src = src or man.get("source")
    if not src or not Path(src).exists():
        print("找不到录播源：%s" % src, file=sys.stderr)
        return 2

    out_dir = CACHE / date
    out_dir.mkdir(parents=True, exist_ok=True)
    clip = out_dir / ("%d.mp4" % index)
    track = out_dir / ("%d.track.json" % index)
    lrc_out = out_dir / ("%d.lrc" % index)
    meta_out = out_dir / ("%d.json" % index)

    need_clip = force or not clip.exists()
    if need_clip:
        ffmpeg = _bin("ffmpeg")
        enc = _encoders(ffmpeg)
        vcodec = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                  "-cq", "23", "-maxrate", "10M", "-bufsize", "20M",
                  "-pix_fmt", "yuv420p", "-profile:v", "high"]
        if "h264_nvenc" not in enc:
            vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                      "-pix_fmt", "yuv420p", "-profile:v", "high"]
        dur = float(ce) - float(cs)
        cmd = [ffmpeg, "-y", "-v", "error", "-ss", "%.3f" % float(cs), "-t", "%.3f" % dur,
               "-i", src] + vcodec + [
               "-c:a", "aac", "-b:a", "192k", "-ac", "2",
               "-movflags", "+faststart", "-avoid_negative_ts", "make_zero",
               str(clip)]
        t0 = time.time()
        print("[prepare] 抽片段 %.2f→%.2f（%.1fs）…" % (float(cs), float(ce), dur))
        r = _run(cmd)
        if r.returncode != 0 or not clip.exists():
            print("抽片段失败：%s" % r.stderr.decode("utf-8", "ignore")[-400:], file=sys.stderr)
            return 3
        print("[prepare] 片段完成 %.1fs / %.1f MB（%.0fs）"
              % (dur, clip.stat().st_size / 1e6, time.time() - t0))

    # 轴：优先 src（cut 相对）；缺失则用成片轴减 head_pad
    if force or not lrc_out.exists():
        raw = None
        p = seg.get("lrc_src_path")
        if p and Path(p).exists():
            raw = Path(p).read_text(encoding="utf-8")
        else:
            p2 = seg.get("lrc_path")
            if p2 and Path(p2).exists():
                raw = _rebase(Path(p2).read_text(encoding="utf-8"),
                              float(seg.get("head_pad") or 0.0))
        if raw is None:
            print("没有歌词轴可用：%s" % seg.get("title"), file=sys.stderr)
            return 4
        lrc_out.write_text(_strip_comments(raw), encoding="utf-8")

    if force or not track.exists():
        py = sys.executable
        print("[prepare] 人物跟踪…")
        r = _run([py, str(ROOT / "tools" / "track_host.py"), "--src", src,
                  "--start", "%.3f" % float(cs), "--end", "%.3f" % float(ce),
                  "--out", str(track)])
        if r.returncode != 0 or not track.exists():
            print("跟踪失败：%s" % r.stderr.decode("utf-8", "ignore")[-400:], file=sys.stderr)
            return 5
        print(r.stdout.decode("utf-8", "ignore").strip())

    font_out = out_dir / ("%d.woff2" % index)
    if force or not font_out.exists():
        _build_font(UI_CHARS + lrc_out.read_text(encoding="utf-8"), font_out)

    probe = json.loads(_run([_bin("ffprobe"), "-v", "error", "-show_entries",
                             "format=duration", "-show_entries",
                             "stream=width,height,r_frame_rate", "-of", "json",
                             str(clip)]).stdout.decode("utf-8", "ignore") or "{}")
    dur = float(probe.get("format", {}).get("duration") or 0) or (float(ce) - float(cs))
    vs = (probe.get("streams") or [{}])[0]
    _sm = _seg_lookup(date, seg.get("title") or seg.get("title_guess"))
    meta = {
        "id": "%s-%d" % (date, index), "date": date, "index": index,
        "title": seg.get("title") or seg.get("title_guess"),
        "artist": seg.get("artist") or _sm.get("artist") or "",
        "vocal": seg.get("vocal") or _sm.get("vocal") or _streamer_name(),
        "romaji": _sm.get("romaji") or "",
        "duration": round(dur, 3),
        "cutStart": round(float(cs), 3), "cutEnd": round(float(ce), 3),
        "headPad": seg.get("head_pad"),
        "resolution": "%sx%s" % (vs.get("width"), vs.get("height")),
        "fps": vs.get("r_frame_rate"),
        "video": "/media/%s/%d.mp4" % (date, index),
        "lrc": "/media/%s/%d.lrc" % (date, index),
        "track": "/media/%s/%d.track.json" % (date, index),
        "font": ("/media/%s/%d.woff2" % (date, index)) if font_out.exists() else None,
        "source": os.path.basename(src),
    }
    meta_out.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 总索引：扫描所有已准备好的条目（幂等重建）
    ents = []
    for d in sorted([p for p in CACHE.iterdir() if p.is_dir()]):
        for m in sorted(d.glob("*.json")):
            if m.name == "index.json":
                continue
            try:
                j = json.loads(m.read_text(encoding="utf-8"))
            except Exception:
                continue
            if j.get("video") and (ROOT / "_media_cache" / "live" /
                                   j["video"].split("/media/")[-1]).exists():
                ents.append(j)
    ents.sort(key=lambda j: (j.get("date", ""), j.get("index", 0)))
    (CACHE / "index.json").write_text(
        json.dumps({"tracks": ents, "built": time.strftime("%Y-%m-%d %H:%M:%S")},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("[prepare] %s 就绪：%s  %.1fs  %s" % (meta["title"], meta["resolution"],
                                              dur, meta["video"]))
    return 0


# ---------------------------------------------------------------- serve
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LiveCue/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[serve] %s %s\n" % (self.address_string(), fmt % args))

    def _resolve(self, path: str):
        if path in ("", "/"):
            return PAGE_DIR / "index.html"
        if path.startswith("/media/"):
            rel = urllib.parse.unquote(path[len("/media/"):])
            p = (CACHE / rel).resolve()
            return p if str(p).startswith(str(CACHE.resolve())) else None
        if path.startswith("/api/index"):
            return CACHE / "index.json"
        rel = urllib.parse.unquote(path.lstrip("/"))
        p = (PAGE_DIR / rel).resolve()
        return p if str(p).startswith(str(PAGE_DIR.resolve())) else None

    def _send_file(self, p: Path, head=False):
        if p is None or not p.exists() or not p.is_file():
            self.send_error(404, "not found")
            return
        size = p.stat().st_size
        ctype = MIME.get(p.suffix.lower(), "application/octet-stream")
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):        # bytes=-N 尾部
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if head:
            return
        # 大文件用 sendfile（Windows 上退化为 64KB 分块读）
        with open(p, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(262144, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def do_GET(self):
        self._send_file(self._resolve(urllib.parse.urlsplit(self.path).path))

    def do_HEAD(self):
        self._send_file(self._resolve(urllib.parse.urlsplit(self.path).path), head=True)


def serve(port: int, host: str) -> int:
    if not PAGE_DIR.exists():
        print("页面目录不存在：%s" % PAGE_DIR, file=sys.stderr)
        return 2
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print("[serve] http://%s:%d/  （Ctrl+C 停止）" % (host, port))
    print("[serve] 页面目录 %s" % PAGE_DIR)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def play(date: str, index: int, port: int) -> int:
    """一条命令：备料（幂等）→ 起服务 → 开浏览器。"""
    rc = prepare(date, index, None, False)
    if rc != 0:
        return rc
    url = "http://127.0.0.1:%d/?i=%d" % (port, index)
    print("[play] %s" % url)

    def _open():
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            print("[play] 自动打开失败（手动访问上面的地址）：%s" % e)

    threading.Timer(1.2, _open).start()
    return serve(port, "127.0.0.1")


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("prepare")
    p1.add_argument("--date", required=True)
    p1.add_argument("--index", type=int, default=0)
    p1.add_argument("--src", default=None)
    p1.add_argument("--force", action="store_true")
    p2 = sub.add_parser("serve")
    p2.add_argument("--port", type=int, default=8777)
    p2.add_argument("--host", default="127.0.0.1")
    p3 = sub.add_parser("play")
    p3.add_argument("--date", required=True)
    p3.add_argument("--index", type=int, default=0)
    p3.add_argument("--port", type=int, default=8777)
    a = ap.parse_args()
    if a.cmd == "prepare":
        return prepare(a.date, a.index, a.src, a.force)
    if a.cmd == "play":
        return play(a.date, a.index, a.port)
    return serve(a.port, a.host)


if __name__ == "__main__":
    sys.exit(main())
