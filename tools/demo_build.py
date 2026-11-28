# -*- coding: utf-8 -*-
"""Demo / 试听片段拼装：按 job.json 抽 48 kHz 立体声音轨 → 混流 → 自检。

为什么要有这个脚本（RULES §165）：
  手工拼 Demo 时最容易犯两个错 ——
  ① 图省事拿 16 kHz 单声道识别素材当音源（音质直接掉成电话音）；
  ② 把整首歌的音轨整段接上，而画面是从 cutStart 开始的（音轨早几十秒，
     粗听只觉得「歌词对不上」，很难发现）。
  本脚本强制按「cutStart − head_pad」两段 seek 抽音频，并在混流后做互相关自检。

用法：
  python tools/demo_build.py --job _chk/demo/job.json \
      --video _chk/demo/demo_v5_render.mp4 --out _chk/demo/demo_v5.mp4
"""
import argparse
import io
import json
import os
import subprocess
import sys

import numpy as np

FF = "D:/ffmpeg/bin/ffmpeg"
FFPROBE = "D:/ffmpeg/bin/ffprobe"
SR = 4000
HOP = 40                     # 10 ms 能量包络


def sh(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "ignore")[-500:])
    return r.stdout


def probe(path, fields):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a:0",
                          "-show_entries", "stream=" + ",".join(fields),
                          "-of", "default=nw=1:nk=0", path],
                         stdout=subprocess.PIPE).stdout.decode()
    d = {}
    for ln in out.splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def pcm(path, ss=None, t=None, sr=SR):
    cmd = [FF, "-v", "error"]
    if ss is not None:
        cmd += ["-ss", "%.3f" % ss]
    cmd += ["-i", path]
    if t is not None:
        cmd += ["-t", "%.3f" % t]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL).stdout
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def env(x):
    n = len(x) // HOP
    return np.sqrt((x[:n * HOP].reshape(n, HOP) ** 2).mean(axis=1) + 1e-9)


def locate(base_env, tpl_audio, base_start):
    e = env(tpl_audio)
    n = 1 << int(np.ceil(np.log2(len(base_env) + len(e))))
    cc = np.fft.irfft(np.fft.rfft(base_env, n) * np.conj(np.fft.rfft(e, n)), n)
    cc = cc[:len(base_env) - len(e) + 1]
    den = np.sqrt((e ** 2).sum() * np.convolve(base_env ** 2, np.ones(len(e)), "valid"))
    r = cc / (den + 1e-9)
    k = int(np.argmax(r))
    return base_start + k * HOP / SR, float(r[k])


def band_ratio(path, ss, dur, lo=8000, hi=12000):
    fs = 48000                       # 频段分析必须用源采样率，SR=4000 测不到 8 kHz 以上
    x = pcm(path, ss=ss, t=dur, sr=fs)
    X = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1.0 / fs)
    return 100.0 * X[(f >= lo) & (f < hi)].sum() / (X.sum() + 1e-12)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--video", required=True, help="已渲染的画面（无音轨）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--audio", default=None, help="复用已抽好的音频（可选）")
    a = ap.parse_args()

    job = json.load(io.open(a.job, encoding="utf-8"))
    src = job["source"]
    cs = float(job["cutStart"])
    pad = float(job.get("head_pad") or 0.0)
    cover = float(job.get("cover_sec") or 0.0)
    dur = float(job["duration"])
    a_start = cs - pad
    a_len = dur + pad + 0.2

    audio = a.audio or os.path.join(os.path.dirname(os.path.abspath(a.out)),
                                    "_mux_48k.m4a")
    if not a.audio:
        # 两段 seek：前置粗定位到关键帧，后置精 seek 到目标（单段 -ss 会偏 ~1s）
        coarse = max(0.0, a_start - 10.0)
        sh([FF, "-y", "-v", "error", "-ss", "%.3f" % coarse, "-i", src,
            "-ss", "%.3f" % (a_start - coarse), "-t", "%.3f" % a_len,
            "-vn", "-ac", "2", "-ar", "48000", "-c:a", "aac", "-b:a", "320k",
            "-movflags", "+faststart", audio])
        print("[build] 音源 %s  起点 %.3f（cutStart %.3f − head_pad %.3f）" %
              (os.path.basename(audio), a_start, cs, pad))

    info = probe(audio, ["sample_rate", "channels"])
    sr, ch = int(info.get("sample_rate", 0)), int(info.get("channels", 0))
    if sr < 44100 or ch != 2:
        raise SystemExit("[build] ✗ 音源只有 %d Hz / %d ch —— 这是识别素材，不是原始音轨（RULES §165.1）"
                         % (sr, ch))
    hi = band_ratio(audio, 20.0, 8.0)
    if hi < 0.5:
        raise SystemExit("[build] ✗ 8~12 kHz 能量仅 %.2f%%（应 >0.5%%），音源已丢失高频" % hi)

    af = []
    if cover > 0.001:
        af.append("adelay=%d:all=1" % int(round(cover * 1000)))
    af.append("apad")
    sh([FF, "-y", "-v", "error", "-i", a.video, "-i", audio,
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
        "-af", ",".join(af), "-c:a", "aac", "-b:a", "320k",
        "-movflags", "+faststart", "-shortest", a.out])

    # 自检：成片音轨定位回原始录播
    base = pcm(src, ss=a_start - 10.0, t=min(140.0, a_len + 20.0))
    tpl = pcm(a.out, ss=cover + 8.0, t=10.0)
    t, score = locate(env(base), tpl, a_start - 10.0)
    file0 = t - (cover + 8.0)
    body0 = file0 + cover
    err = body0 - a_start
    print("[build] 定位峰 %.4f  文件0点 %.3f  → 正片0点 %.3f  期望 %.3f  差 %+.3f s"
          % (score, file0, body0, a_start, err))
    if score < 0.99 or abs(err) > 0.05:
        raise SystemExit("[build] ✗ 音画不同步（峰 %.4f / 差 %+.3f s）" % (score, err))
    print("[build] ✓ 通过：%s（%.1f MB）" %
          (os.path.basename(a.out), os.path.getsize(a.out) / 1048576.0))


if __name__ == "__main__":
    sys.exit(main())
