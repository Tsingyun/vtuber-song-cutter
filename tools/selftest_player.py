# -*- coding: utf-8 -*-
"""播放器渲染管线自检（2026-10-28）

一条命令回答一个问题：**现在这条管线还能不能出片？**
完全自包含 —— 现场用 ffmpeg 合成 8 秒测试音频 / 测试封面 / 测试歌词，
不依赖任何录播文件或网络，因此可以在任何时间、任何机器上跑。

用法：
    python tools/selftest_player.py                       # 测默认版 index.html
    python tools/selftest_player.py --page index_v10.html  # 测 v10
    python tools/selftest_player.py --page index_v10.html --res 2   # 顺带测 4K（慢约 40s）

判定：全部 PASS 才算管线就绪。任何一项 FAIL 都会打印原因与建议。
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from songcut import config as CFG  # noqa: E402

PLAYER_DIR = os.path.join(ROOT, "renderer", "player")
RENDER_CJS = os.path.join(ROOT, "renderer", "render_song.cjs")

LRC_TEXT = "\n".join(
    "[%02d:%02d.00]%s" % (i // 30, (i * 2) % 60, s)
    for i, s in enumerate([
        "自检第一行", "自检第二行", "这一行用来验证逐字点亮", "第四行",
        "第五行故意长一点 用来验证横向滚动与视口裁切", "第六行", "第七行", "第八行",
    ])
)

ok_all = True


def say(flag, name, detail=""):
    global ok_all
    mark = "PASS" if flag else "FAIL"
    if not flag:
        ok_all = False
    print("[%s] %-30s %s" % (mark, name, detail))


def run(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)


def pick_ffmpeg():
    for c in (os.environ.get("FFMPEG_BIN"), "ffmpeg", "D:/ffmpeg/bin/ffmpeg.exe"):
        if not c:
            continue
        try:
            if subprocess.run([c, "-version"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0:
                return c
        except Exception:
            pass
    raise SystemExit("找不到 ffmpeg")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default="index.html")
    ap.add_argument("--res", type=int, default=1)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--keep", action="store_true", help="保留临时目录（排查用）")
    a = ap.parse_args()

    page = os.path.join(PLAYER_DIR, a.page)
    if not os.path.exists(page):
        raise SystemExit("播放器页面不存在：%s" % page)
    say(True, "页面存在", a.page)

    ff = pick_ffmpeg()
    node = CFG.node_candidates()
    node_bin = next((n for n in node if n and shutil.which(n) or os.path.exists(n)), None)
    if not node_bin:
        raise SystemExit("找不到 node")
    say(True, "ffmpeg / node", "%s | %s" % (os.path.basename(ff), os.path.basename(node_bin)))

    tmp = tempfile.mkdtemp(prefix="selftest_", dir=os.path.join(ROOT, "_tmp"))
    try:
        # ── 合成素材（自包含）──────────────────────────────────────────
        mp3 = os.path.join(tmp, "a.mp3")
        r = run([ff, "-y", "-v", "error",
                 "-f", "lavfi", "-i", "sine=frequency=196:duration=%g" % a.seconds,
                 "-f", "lavfi", "-i", "anoisesrc=color=pink:duration=%g:amplitude=0.25" % a.seconds,
                 "-filter_complex",
                 "[0:a]volume=0.55,tremolo=f=2.2:d=0.75[s];[s][1:a]amix=inputs=2:duration=shortest,"
                 "afade=t=in:st=0:d=0.4,afade=t=out:st=%g:d=0.8" % max(0, a.seconds - 0.9),
                 "-c:a", "libmp3lame", "-b:a", "192k", mp3])
        say(r.returncode == 0 and os.path.getsize(mp3) > 10000, "合成测试音频",
            "%d KB / %gs" % (os.path.getsize(mp3) // 1024, a.seconds) if r.returncode == 0 else "ffmpeg 失败")

        cov = os.path.join(tmp, "c.png")
        r = run([ff, "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=800x800:duration=1", "-frames:v", "1", cov])
        say(r.returncode == 0 and os.path.exists(cov), "合成测试封面")
        import base64
        cover = "data:image/png;base64," + base64.b64encode(io.open(cov, "rb").read()).decode()

        out = os.path.join(tmp, "o.mp4")
        job = {
            "playerDir": PLAYER_DIR, "page": a.page,
            "audio": mp3, "cover": cover, "lrc": LRC_TEXT,
            "title": "渲染自检 · 中文标题", "artist": "SELFTEST",
            "trackLabel": "TRACK 00", "romaji": "RENDER PIPELINE SELF TEST",
            "vocal": "—", "producer": "—", "tags": "自检,管线",
            "note": "", "footerLeft": "SONG ARCHIVE", "footerRight": "SELFTEST",
            "badge": "MUSIC ARCHIVE", "mark": "SC",
            "scheme": 0, "res": a.res, "bitrate": 18_000_000,
            "encoder": "ffmpeg", "out": out,
        }
        jf = os.path.join(tmp, "job.json")
        io.open(jf, "w", encoding="utf-8").write(json.dumps(job, ensure_ascii=False))

        env = dict(os.environ)
        env["NODE_PATH"] = CFG.node_modules_path() or env.get("NODE_PATH", "")
        r = subprocess.run([node_bin, RENDER_CJS, "--job", jf],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        log = r.stdout.decode("utf-8", "replace")
        io.open(os.path.join(tmp, "render.log"), "w", encoding="utf-8").write(log)

        say(r.returncode == 0, "渲染进程退出码", str(r.returncode))
        perr = re.findall(r"^\[pageerror\].*", log, re.M)
        cerr = re.findall(r"^\[console\.error\].*", log, re.M)
        say(not perr, "页面无 JS 异常", (" ".join(perr)[:120] if perr else ""))
        say(not cerr, "页面无 console.error", (" ".join(cerr)[:120] if cerr else ""))

        if not os.path.exists(out):
            say(False, "成片已生成", "文件不存在")
            raise SystemExit(1)
        say(True, "成片已生成", "%.1f MB" % (os.path.getsize(out) / 1048576))

        pj = json.loads(subprocess.run(
            [ff.replace("ffmpeg", "ffprobe"), "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", out],
            stdout=subprocess.PIPE).stdout.decode("utf-8", "replace") or "{}")
        vs = [s for s in pj.get("streams", []) if s.get("codec_type") == "video"]
        dur = float(pj.get("format", {}).get("duration", 0) or 0)
        if vs:
            v = vs[0]
            want_w = 1920 * a.res
            say(int(v["width"]) == want_w and int(v["height"]) == 1080 * a.res,
                "分辨率", "%sx%s（期望 %d）" % (v["width"], v["height"], want_w))
            say(v.get("r_frame_rate") == "60/1", "帧率", v.get("r_frame_rate"))
        else:
            say(False, "视频流存在", "无 video stream")
        say(abs(dur - a.seconds) < 0.6, "时长", "%.2fs（期望 %.2fs）" % (dur, a.seconds))
        say("17" in log or "码率" in log, "编码器路径", 
            (re.search(r"\[encoder\].*", log).group(0)[:80] if re.search(r"\[encoder\]", log) else ""))
    finally:
        if a.keep:
            print("\n临时目录保留：%s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("=" * 58))
    print("管线就绪 ✓  （%s @ res=%d）" % (a.page, a.res) if ok_all
          else "管线异常 ✗  请按上面 FAIL 项排查")
    print("=" * 58)
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
