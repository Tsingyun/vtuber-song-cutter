#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""asr_auto.py —— 自建转写管线（无上游 SRT 时的降级通道）

职责单一：把一场录播的整段语音转写成「与视频 0 点对齐」的 SRT，
供下游（歌切识别）直接使用，输出契约与上游 SRT 完全一致。

为什么不用整段 VAD：
  唱歌时 BGM 连续不断，VAD 会把整场并成一两个超长句（实测 272s 并成一句），
  时间戳完全不可用。因此这里用「固定窗口分块 + 逐块推理」，窗口即时间基准。

用法：
  python -m songcut.asr_auto --input 录播.mp4 --out 输出.srt [--chunk 30] [--max-sec 600]
"""
import io
import os
import re
import subprocess
import sys
import time
import wave

import numpy as np

TAG_RE = re.compile(r"<\|[^|]*\|>")
MUSIC_TAGS = ("BGM", "Music", "Sing", "Song")
# ⚠ 音乐事件标记前缀：SenseVoice 判定「这段是伴奏/音乐」时会输出 <|BGM|> 等标签，
#   以前只用它统计 music_chunks 就把标签抹掉了，歌词正文照写进字幕 →
#   自动歌切无法区分「主播在唱」和「只是放了首BGM」，导致纯 BGM 被误判成演唱。
#   现在保留为 cue 文本前缀（零宽、不破坏 (start,end,text) 三元组、不进 LLM 正文比对）。
MUSIC_PREFIX = "\u266b"   # ♪
# 纯标点/语气符：SenseVoice 在无内容时也会吐「。」
PUNCT_ONLY = re.compile(u"[\\s，。！？、；：,.!?;:~\u3000\u2026\u2018\u2019\u201c\u201d'\"()（）\\[\\]【】]+")


def _ffmpeg_bin(cfg_ffmpeg=None):
    return cfg_ffmpeg or "ffmpeg"


def decode_wav(video, wav_path, ffmpeg=None, sr=16000, max_sec=None):
    """整段音频一次性解码为 16k 单声道 wav（分块切片在内存里做，避免反复 seek）。"""
    cmd = [_ffmpeg_bin(ffmpeg), "-y", "-v", "error", "-i", video, "-vn",
           "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le"]
    if max_sec:
        cmd[1:1] = ["-t", str(float(max_sec))]
    cmd.append(wav_path)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("音频解码失败：%s" % (p.stderr or "")[-300:])
    return wav_path


def _read_wav(path):
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _write_wav(path, x, sr=16000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())


def _model(device="cuda:0"):
    from funasr import AutoModel
    return AutoModel(model="iic/SenseVoiceSmall", device=device, disable_update=True)


def transcribe_to_srt(video, out_srt, chunk=30.0, lang="zh", max_sec=None,
                      ffmpeg=None, device="cuda:0", quiet=True):
    """整场转写 → SRT。返回统计 dict（含音乐块占比，用于「有没有唱歌环节」的旁证）。

    分块策略：固定 chunk 秒窗口，块内近静音（峰值 < 0.001）直接跳过，
    其余逐块推理；SenseVoice 事件标签（<|BGM|> 等）单独统计不写入正文。
    """
    t0 = time.time()
    tmp_dir = os.path.dirname(os.path.abspath(out_srt)) or "."
    os.makedirs(tmp_dir, exist_ok=True)
    wav_all = os.path.join(tmp_dir, "_asr_full.wav")
    chunk_wav = os.path.join(tmp_dir, "_asr_chunk.wav")
    try:
        decode_wav(video, wav_all, ffmpeg=ffmpeg, max_sec=max_sec)
        x = _read_wav(wav_all)
        sr = 16000
        total = len(x) / float(sr)
        step = int(chunk * sr)
        m = _model(device)
        cues, music_chunks, skipped = [], 0, 0
        n_chunks = int(np.ceil(len(x) / float(step)))
        for i in range(n_chunks):
            seg = x[i * step:(i + 1) * step]
            if len(seg) < sr:          # 尾部不足 1s 丢弃
                break
            # 近静音块：实测开场待机段 rms≈0.01 / peak≈0.04，模型只会吐「。」
            if float(np.abs(seg).max()) < 0.05 or float(np.sqrt((seg * seg).mean())) < 0.01:
                skipped += 1
                continue
            _write_wav(chunk_wav, seg, sr)
            try:
                r = m.generate(input=chunk_wav, cache={}, language=lang, use_itn=True,
                               batch_size_s=30, merge_vad=False, merge_length_s=5)
                text = (r[0].get("text") or "").strip() if r else ""
            except Exception:
                text = ""
            if not text:
                continue
            tags = TAG_RE.findall(text)
            is_music = any(t.strip("<|>") in MUSIC_TAGS for t in tags)
            if is_music:
                music_chunks += 1
            body = TAG_RE.sub("", text).strip()
            if not PUNCT_ONLY.sub("", body):          # 只剩标点的空句不写字幕
                continue
            # is_music 的块打上标记前缀（♪），下游据此判断「这段只有伴奏、没有主播人声」
            cues.append((i * chunk, min((i + 1) * chunk, total),
                         (MUSIC_PREFIX + body) if is_music else body))
            if not quiet and i % 50 == 0:
                sys.stdout.write("  ASR %.0f/%.0fs（%d 句）\n" % ((i + 1) * chunk, total, len(cues)))
                sys.stdout.flush()
    finally:
        for p in (wav_all, chunk_wav):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _srt_time(t):
        h = int(t // 3600)
        mm = int((t % 3600) // 60)
        ss = t % 60
        return "%02d:%02d:%02d,%03d" % (h, mm, int(ss), int(round((ss - int(ss)) * 1000)))

    with io.open(out_srt, "w", encoding="utf-8") as f:
        for i, (s, e, t) in enumerate(cues, 1):
            # ♪ 前缀只留在内存 cues 里供 auto_cut 判定用，不写进 SRT 正文
            f.write("%d\n%s --> %s\n%s\n\n"
                    % (i, _srt_time(s), _srt_time(e), t.lstrip(MUSIC_PREFIX)))
    return {"srt": out_srt, "cues": len(cues),
            "chars": sum(len(t.lstrip(MUSIC_PREFIX)) for _a, _b, t in cues),
            "music_cues": sum(1 for _a, _b, t in cues if t.startswith(MUSIC_PREFIX)),
            "span_sec": round(total, 2), "chunks": n_chunks, "silent_chunks": skipped,
            "music_chunks": music_chunks, "music_sec": round(music_chunks * chunk, 1),
            "engine": "FunASR/SenseVoiceSmall", "elapsed_s": round(time.time() - t0, 1)}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="录播整场转写 → SRT（自建降级管线）")
    ap.add_argument("--input", required=True, help="录播视频路径")
    ap.add_argument("--out", required=True, help="输出 SRT 路径")
    ap.add_argument("--chunk", type=float, default=30.0)
    ap.add_argument("--max-sec", type=float, default=None, help="只转写前 N 秒（调试用）")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--ffmpeg", default=None)
    a = ap.parse_args()
    st = transcribe_to_srt(a.input, a.out, chunk=a.chunk, max_sec=a.max_sec,
                           ffmpeg=a.ffmpeg, device=a.device, quiet=False)
    print("完成：%d 句 / %d 字，音乐块 %d（%.0fs），耗时 %.0fs → %s"
          % (st["cues"], st["chars"], st["music_chunks"], st["music_sec"],
             st["elapsed_s"], st["srt"]))


if __name__ == "__main__":
    main()
