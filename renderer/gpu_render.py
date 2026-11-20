#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
gpu_render.py —— A 唱片「显卡直接算画面」渲染器（moderngl / OpenGL 3.3 + NVENC）

与 render_song.cjs（Chromium 逐帧 toBlob，72ms/帧）的根本区别：
  · 画面不再由浏览器光栅化，而是由 fragment shader 在显卡上逐像素计算；
  · CPU 只在启动时做一次性准备（文字→纹理、封面→纹理、噪点→纹理）；
  · 渲染循环里 CPU 只做两件事：推进动效状态、把 GPU 算好的 4K 帧读回来送编码器。

画面管线（每帧 4 个 GPU pass）：
  Pass1  scene  → 纸底渐变 / 三片光晕 / 纸纹噪点 / 圆角封面 / 进度条
  Pass2  毛玻璃 → 对歌词区做两趟可分离高斯（1/2 分辨率降采样），得到磨砂底
  Pass3  合成   → scene + 歌词区毛玻璃面板（圆角 + 磨砂噪点）
  Pass4  文字   → 标题/信息/歌词/刊眉/时间，歌词用 9-tap 可变半径高斯做连续模糊

歌词动效（对齐 index.html drawLyrics 的 Apple Music 质感）：
  · scrollY 指数缓动（τ≈0.18s）→ 平滑滚动
  · act[] 聚焦度缓动（聚焦 τ≈0.11s / 失焦 τ≈0.24s）→ 字号 48↔65、亮度连续变化
  · bs[] 明暗缓动（τ≈0.11s）→ 行切换不闪
  · bl[] 模糊量缓动（1 行 3px → 4 行 12px）→ 非焦点行渐进模糊
  · 视口上下 80px 边缘淡出

输入（--job job.json，字段与 render_song.cjs 兼容的子集）：
  title / artist / vocal / lrc / cover(dataURL 或路径) / duration / fps / res / bitrate / out
  --sec N   只渲染前 N 秒（试片用）

输出：无音轨的 H.264 MP4（音轨由 song_cutter.py 后续混流）
"""
import argparse
import base64
import io
import json
import math
import os
import re
import subprocess
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    import moderngl
except ImportError:
    print("缺少 moderngl：pip install moderngl", file=sys.stderr)
    raise

DESIGN_W, DESIGN_H = 1920, 1080
SS = 2.0                     # 文字纹理超采样倍数（__init__ 里按输出分辨率覆写）
_GL_SCISSOR_TEST = 0x0C11    # moderngl 未导出该常量，直接给 GL 值

# ---- A 唱片版式常量（对齐 renderer/player/index.html）----
COVER_X, COVER_Y, COVER_S, COVER_R = 268.0, 96.0, 560.0, 10.0
LY_X, LY_Y, LY_W, LY_H, LY_LH = 980.0, 140.0, 844.0, 844.0, 102.0
FOCAL_Y = LY_Y + LY_H * 0.242          # 当前行落点（≈344）
BAR_X, BAR_Y, BAR_W = 96.0, 1002.0, 1728.0
INK = (30, 40, 54)                     # #1E2836
PAPER_TOP = (250, 249, 246)
PAPER_BOT = (241, 240, 235)
ROSE = (218, 93, 119)                  # 当前行标记 / 进度条绯玫红

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 字体/路径一律相对仓库根，禁止写本机绝对路径（RULES：禁入库本机路径与主播名）
FONT_JP = os.path.join(_ROOT, "official_pv", "engine", "fonts", "NotoSansJP_wght_.ttf")
FONT_SC = os.path.join(_ROOT, "official_pv", "engine", "fonts", "NotoSansSC_wght_.ttf")
_WIN_FONTS = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "Fonts")
FONT_FALLBACK_JP = os.path.join(_WIN_FONTS, "YuGothM.ttc")
FONT_FALLBACK_SC = os.path.join(_WIN_FONTS, "msyh.ttc")

KANA_RE = re.compile(r"[\u3040-\u30FF]")
CJK_RE = re.compile(r"[\u3040-\u30FF\u4e00-\u9fff]")
LRC_RE = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")
# 增强型 LRC 的逐字标签 <mm:ss.xx>（对齐链产出）；与行首 [] 标签是两套记号
WORD_TS = r"(\d+):(\d+(?:[.:]\d+)?)"          # 带分组：取 group(1)/group(2)
WORD_TS_NC = r"(?:\d+):(?:\d+(?:[.:]\d+)?)"   # 无分组：给 re.split 用（否则子组会混进切分结果）
WORD_RE = re.compile(r"<%s>" % WORD_TS)
WORD_SPLIT = re.compile(r"(<%s>)" % WORD_TS_NC)


def log(*a):
    print(*a, flush=True)


def _ffmpeg_path():
    """ffmpeg 位置：环境变量 → songcut 配置 → PATH。不要写死本机绝对路径。"""
    cand = os.environ.get("FFMPEG_BIN")
    if cand and os.path.exists(cand):
        return cand
    try:
        if _ROOT not in sys.path:
            sys.path.insert(0, _ROOT)
        from songcut import config as CFG
        p = CFG.ffmpeg()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    return "ffmpeg"


# ----------------------------------------------------------------------------
# LRC
# ----------------------------------------------------------------------------
def _tag_sec(m, s):
    return int(m) * 60 + float(s.replace(":", "."))


def parse_lrc(text, duration):
    """LRC → [(t, text, words, fill_end)]，**必须剥净所有时间标签**。

    三种写法都要认（CPU 端 renderer/player/index.html 的 parseLyrics 同样支持）：
      ① 行首 [mm:ss.xx]词                —— 纯行级 LRC
      ② 行首 [mm:ss.xx]<mm:ss.xx>字<...> —— 增强型逐字轴（CTC/DTW/ASR 对齐器的输出）
      ③ 行尾独立 <mm:ss.x>               —— 该行实际唱完时刻（卡拉OK填充终点）

    ⚠ 历史 bug：只做 ①，②的 `<...>` 被当成正文 → 成片整行变成
      `<00:56.03>时<00:56.15>我<...>` 且因超长被压到极小字号。凡对齐链
      （dtw/ctc/asr）产出的 lrc 都是 ②，所以只有走 --lrc-override 的曲子正常。
    """
    raw = []
    for line in (text or "").splitlines():
        tags = LRC_RE.findall(line)
        if not tags:
            continue
        body = LRC_RE.sub("", line).strip()
        if not body:
            continue

        # ③ 行尾独立标签 = 整行唱完时刻；先摘下来，剩下的才是正文
        fill = None
        m_end = re.search(r"<%s>\s*$" % WORD_TS, body)
        if m_end:
            fill = _tag_sec(m_end.group(1), m_end.group(2))
            body = body[:m_end.start()].rstrip()

        # ② 内嵌尖括号 = 逐字时间轴
        words = None
        if WORD_RE.search(body):
            toks = WORD_SPLIT.split(body)
            ws, pending = [], None
            for tok in toks:
                if not tok:
                    continue
                mm = WORD_RE.fullmatch(tok)
                if mm:
                    pending = _tag_sec(mm.group(1), mm.group(2))
                    continue
                for ch in tok:
                    if ch in "\r\n":
                        continue
                    if not ch.strip() and ch != " ":
                        continue
                    ws.append((pending if pending is not None else 0.0, ch))
            # 同一标签下的一串字共享起点：按累计宽度均分它们的时间，避免整词同帧点亮
            if len(ws) >= 2:
                words = ws
            body = WORD_RE.sub("", body).strip()

        if not body:
            continue
        if body.strip("♪ ~—-·　 ").lower() in ("", "interlude", "instrumental"):
            continue
        for m, s in tags:
            raw.append((_tag_sec(m, s), body, words, fill))

    raw.sort(key=lambda x: x[0])
    out, seen = [], set()
    for t, c, w, fill in raw:
        k = (round(t, 2), c)
        if k in seen:
            continue
        seen.add(k)
        if out and abs(out[-1][0] - t) < 0.02:
            if c not in out[-1][1]:
                out[-1] = (out[-1][0], out[-1][1] + "  " + c, out[-1][2], out[-1][3])
            continue
        out.append((t, c, w, fill))
    return out


def cur_index(t, lyr):
    lo, hi = 0, len(lyr)
    while lo < hi:
        mid = (lo + hi) // 2
        if lyr[mid][0] <= t + 1e-6:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


# ----------------------------------------------------------------------------
# 字体 / 文字纹理
# ----------------------------------------------------------------------------
_font_cache = {}


def get_font(text, px, weight=600):
    """统一用 NotoSansSC：它同时覆盖简体中文 + 日文假名（NotoSansJP 缺简体字，
    会把「线」「仆」渲染成方框）。风格也最接近播放器原版。"""
    key = (px, weight)
    if key in _font_cache:
        return _font_cache[key]
    f = None
    for path, w in ((FONT_SC, weight), (FONT_FALLBACK_SC, None)):
        if not os.path.exists(path):
            continue
        try:
            f = ImageFont.truetype(path, int(px))
            if w is not None:
                try:
                    f.set_variation_by_axes([w])
                except Exception:
                    pass
            break
        except Exception:
            f = None
    if f is None:
        f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def text_mask(text, px, weight=600, pad=10):
    font = get_font(text, px, weight)
    dummy = ImageDraw.Draw(Image.new("L", (4, 4)))
    try:
        bbox = dummy.textbbox((0, 0), text, font=font)
    except Exception:
        bbox = (0, 0, int(px * len(text) * 0.6), px)
    w = max(1, bbox[2] - bbox[0] + pad * 2)
    h = max(1, bbox[3] - bbox[1] + pad * 2)
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    d.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=255)
    return img, w, h


def text_mask_chars(text, px, weight=600, pad=10):
    """text_mask 的增强版：额外返回每个字符的横向起点（纹理像素，含 pad）。

    卡拉OK填充需要「唱到第几个字 → 已唱多少像素」，因此必须知道字形级偏移。
    用同一个 font 对象 `getlength(prefix)` 累加，与上面 textbbox 的排版一致。
    """
    font = get_font(text, px, weight)
    dummy = ImageDraw.Draw(Image.new("L", (4, 4)))
    try:
        bbox = dummy.textbbox((0, 0), text, font=font)
    except Exception:
        bbox = (0, 0, int(px * len(text) * 0.6), px)
    w = max(1, bbox[2] - bbox[0] + pad * 2)
    h = max(1, bbox[3] - bbox[1] + pad * 2)
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    d.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=255)
    ox = pad - bbox[0]
    xs = []
    for i in range(len(text)):
        try:
            pre = float(font.getlength(text[:i]))
        except Exception:
            pre = i * px * 0.6
        xs.append(ox + pre)
    return img, w, h, xs


def mask_to_rgba(mask):
    r = Image.new("L", mask.size, 255)
    return Image.merge("RGBA", (r, r, r, mask))


# ----------------------------------------------------------------------------
# Shaders
# ----------------------------------------------------------------------------
VS = """
#version 330
in vec2 in_pos;
in vec2 in_uv;
uniform vec2 u_res;
out vec2 v_uv;
out vec2 v_pos;
void main() {
    v_uv = in_uv;
    v_pos = in_pos;
    float x = in_pos.x / u_res.x * 2.0 - 1.0;
    float y = 1.0 - in_pos.y / u_res.y * 2.0;
    gl_Position = vec4(x, y, 0.0, 1.0);
}
"""

# 只吃位置（场景 pass 不需要 uv，避免 attribute 被优化掉）
VS_POS = """
#version 330
in vec2 in_pos;
uniform vec2 u_res;
out vec2 v_pos;
void main() {
    v_pos = in_pos;
    float x = in_pos.x / u_res.x * 2.0 - 1.0;
    float y = 1.0 - in_pos.y / u_res.y * 2.0;
    gl_Position = vec4(x, y, 0.0, 1.0);
}
"""

# Pass1：场景
FS_SCENE = """
#version 330
in vec2 v_pos;
out vec4 f_color;
uniform vec2  u_res;
uniform float u_time;
uniform float u_frame;
uniform float u_progress;
uniform vec3  u_top, u_bot;
uniform sampler2D u_grain;
uniform sampler2D u_cover;
uniform vec4  u_cover_rect;
uniform float u_cover_r;

float sdRoundRect(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + vec2(r);
    return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}
float halo(vec2 p, vec2 c, float r, float k) {
    float d = length(p - c) / r;
    return exp(-d * d * k);
}
void main() {
    vec2 p = v_pos;
    vec3 col = mix(u_top, u_bot, clamp(p.y / u_res.y, 0.0, 1.0));

    float a1 = u_time / 26.0, a2 = u_time / 34.0;
    col += vec3(0.66, 0.70, 0.75) * halo(p, vec2(300.0 + sin(a1) * 200.0, 820.0 + cos(a1 * 0.8) * 110.0), 900.0, 2.6) * 0.030;
    col += vec3(0.75, 0.77, 0.80) * halo(p, vec2(1490.0 + cos(a2) * 240.0, 700.0 + sin(a2 * 0.7) * 130.0), 860.0, 2.8) * 0.024;
    col += vec3(0.66, 0.70, 0.75) * halo(p, vec2(568.0 + sin(a2 * 0.5) * 70.0, 410.0 + cos(a1 * 0.6) * 46.0), 940.0, 2.4) * 0.042;

    /* 胶片颗粒必须**逐帧跳变**：纸底、封面、光晕在长前奏/间奏里几乎不动，
       若颗粒也固定，连续帧会逐像素完全一致 —— QC V04 会判「冻结帧/渲染卡死」
       （2026-11-20 实测：蒲公英 27.7s 前奏 42% 冻结窗口，BLOCK）。
       幅度仍是 ±0.014（≈3.6/255 级），肉眼只是颗粒在呼吸，但帧间 MAD 足以越过 0.002 阈值。 */
    vec2 goff = vec2(fract(sin(u_frame * 12.9898) * 43758.5453),
                     fract(sin(u_frame * 78.233 + 1.7) * 43758.5453));
    float g = texture(u_grain, (p + goff * 320.0) / 320.0).r;
    col += (g - 0.5) * 0.028;

    /* 逐帧变化层（V04 防守）：必须满足三个条件 —— 每帧都在变、变化幅度过 0.002 MAD、
       又不贵。踩过的两个坑：
         · 低频颗粒（/320，整屏才 6 纹素）挪动几乎无变化 → MAD 0.0007 不过；
         · 细颗粒（/3，1 纹素/3px）能到 0.0017，但纹理缓存全失效，1080p 从 75fps 掉到 24fps；
         · 整帧亮度微抖（±0.8%）会因随机数偶然接近而漏帧（实测有帧对只差 0.0013）。
       定案：**中频颗粒**（/12，≈1 纹素/12 设计px，整屏 160×90 纹素）逐帧随机平移。
       中频在 H.264 里保得住，纹理访问也仍是缓存友好的，幅度 ±0.010（±2.5/255 级）。 */
    /* 逐帧颗粒：不用纹理采样，改用 ALU 哈希 —— 纹理按 /6 采样虽能把 MAD 做到 0.0055，
       但纹理缓存全失效，实测 1080p 从 75fps 掉到 22.8fps（比浏览器还慢，方案失去意义）。
       哈希版每像素一次 sin，无内存访问，相位随 u_frame 跳变 → 图案逐帧完全不同。 */
    float hn = fract(sin(dot(p, vec2(12.9898, 78.233)) + u_frame * 3.7) * 43758.5453);
    col += (hn - 0.5) * 0.060;

    vec2 c = p - (u_cover_rect.xy + u_cover_rect.zw * 0.5);
    float d = sdRoundRect(c, u_cover_rect.zw * 0.5, u_cover_r);
    float m = 1.0 - smoothstep(-1.2, 1.2, d);
    if (m > 0.001) {
        vec2 cuv = (c + u_cover_rect.zw * 0.5) / u_cover_rect.zw;
        vec3 cc = texture(u_cover, clamp(cuv, 0.0, 1.0)).rgb;
        col = mix(col, cc, m);
    }

    float ty = abs(p.y - 1002.0);
    if (p.x >= 96.0 && p.x <= 1824.0 && ty < 1.6) {
        float t = (p.x - 96.0) / 1728.0;
        vec3 bar = (t <= u_progress) ? vec3(0.855, 0.365, 0.42) : vec3(0.80, 0.80, 0.78);
        float aa = 1.0 - smoothstep(0.7, 1.6, ty);
        col = mix(col, bar, aa * 0.92);
    }
    if (length(p - vec2(96.0, 1002.0)) < 3.2) {
        col = mix(col, vec3(0.855, 0.365, 0.42), 0.95);
    }

    f_color = vec4(col, 1.0);
}
"""

# Pass2：可分离高斯（对歌词区）
FS_BLUR = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D u_scene;
uniform vec4 u_rect;      // 歌词区设计坐标 x,y,w,h
uniform vec2 u_dir;       // 采样方向（设计坐标单位向量）
uniform float u_radius;   // 模糊半径（设计坐标像素）
uniform vec2 u_scene_res;
void main() {
    vec2 uv = (u_rect.xy + v_uv * u_rect.zw) / vec2(1920.0, 1080.0);
    uv.y = 1.0 - uv.y;
    vec2 texel = vec2(1.0) / u_scene_res;
    vec2 stepv = u_dir * (u_radius * 0.5) * texel;
    // 9-tap 高斯（权重 1/16,2/16,1/16 的 3x3 近似，扩展到 radius）
    vec3 sum = texture(u_scene, uv).rgb * 4.0;
    sum += texture(u_scene, uv + stepv).rgb * 2.0;
    sum += texture(u_scene, uv - stepv).rgb * 2.0;
    sum += texture(u_scene, uv + stepv * 2.0).rgb;
    sum += texture(u_scene, uv - stepv * 2.0).rgb;
    sum += texture(u_scene, uv + stepv * 0.5).rgb * 2.0;
    sum += texture(u_scene, uv - stepv * 0.5).rgb * 2.0;
    sum += texture(u_scene, uv + stepv * 1.5).rgb;
    sum += texture(u_scene, uv - stepv * 1.5).rgb;
    f_color = vec4(sum / 16.0, 1.0);
}
"""

# Pass3：合成（scene + 毛玻璃面板）
FS_COMP = """
#version 330
in vec2 v_uv;
in vec2 v_pos;
out vec4 f_color;
uniform sampler2D u_scene;
uniform sampler2D u_blur;
uniform sampler2D u_grain;
uniform vec4  u_glass_rect;
uniform float u_glass_r;
uniform vec2  u_res;

float sdRoundRect(vec2 p, vec2 b, float r) {
    vec2 q = abs(p) - b + vec2(r);
    return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;
}
void main() {
    vec3 col = texture(u_scene, vec2(v_uv.x, 1.0 - v_uv.y)).rgb;

    vec2 c = v_pos - (u_glass_rect.xy + u_glass_rect.zw * 0.5);
    float d = sdRoundRect(c, u_glass_rect.zw * 0.5, u_glass_r);
    float m = 1.0 - smoothstep(-1.5, 1.5, d);
    if (m > 0.001) {
        vec2 guv = (c + u_glass_rect.zw * 0.5) / u_glass_rect.zw;
        guv.y = 1.0 - guv.y;
        vec3 b = texture(u_blur, clamp(guv, 0.0, 1.0)).rgb;
        vec3 paper = vec3(0.965, 0.962, 0.949);
        vec3 glass = mix(b, paper, 0.16) * 1.015;      // 提亮 + 降对比 → 磨砂
        float g = texture(u_grain, v_pos / 240.0).r;
        glass += (g - 0.5) * 0.035;                     // 磨砂颗粒
        col = mix(col, glass, m * 0.94);
    }
    f_color = vec4(col, 1.0);
}
"""

# Pass4：文字（9-tap 可变半径高斯）
FS_TEXT = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D u_tex;
uniform float u_alpha;
uniform vec3  u_tint;
uniform float u_blur;      // 模糊半径（纹理像素）
uniform vec2  u_texel;     // 1/纹理尺寸
uniform float u_kar;       // 卡拉OK 填充阈值（uv.x，<0 = 关闭）
void main() {
    vec2 uv = v_uv;
    float a;
    if (u_blur <= 0.01) {
        a = texture(u_tex, uv).a;
    } else {
        vec2 d = u_texel * u_blur;
        float s = texture(u_tex, uv).a * 4.0;
        s += (texture(u_tex, uv + vec2(d.x, 0.0)).a + texture(u_tex, uv - vec2(d.x, 0.0)).a) * 2.0;
        s += (texture(u_tex, uv + vec2(0.0, d.y)).a + texture(u_tex, uv - vec2(0.0, d.y)).a) * 2.0;
        s += texture(u_tex, uv + d).a + texture(u_tex, uv - d).a;
        s += texture(u_tex, uv + vec2(d.x, -d.y)).a + texture(u_tex, uv + vec2(-d.x, d.y)).a;
        s += texture(u_tex, uv + d * 2.0).a + texture(u_tex, uv - d * 2.0).a;
        a = s / 18.0;
    }
    if (a <= 0.002) discard;
    vec3 col = u_tint;
    float alpha = a * u_alpha;
    if (u_kar >= 0.0) {
        /* 已唱实色 / 未唱薄底：同字形只换色与透明度（换字重会重影）。
           阈值带半像素羽化，避免 4K 下看到锯齿竖边。 */
        float e = 1.2 * u_texel.x;   // 1.2 纹理像素羽化
        float w = smoothstep(u_kar - e, u_kar + e, uv.x);
        col = mix(u_tint, vec3(0.52, 0.55, 0.60), 0.72 * w);
        alpha *= mix(1.0, 0.30, w);
    }
    f_color = vec4(col, alpha);
}
"""


class GpuRenderer:
    def __init__(self, job, out_path, sec=None, res=2, start=0.0):
        global SS
        self.job = job
        self.out = out_path
        self.sec = sec
        self.t0 = float(start or 0.0)   # 起始时间（抽副歌片段用；坐标仍按全片时间）
        self.res = res
        self.W = DESIGN_W * res
        self.H = DESIGN_H * res
        # 文字纹理超采样倍数必须跟着输出分辨率走：
        # 设计坐标 1px = 输出 res 物理px，SS<res 就是「小图放大」→ 边缘发虚。
        # 4K(res=2) 取 4 = 2× 超采样（等效 SSAA），1080p 取 2。见 RULES §144。
        SS = 4.0 if int(res) >= 2 else 2.0
        self.SS = SS
        self.fps = int(job.get("fps") or 60)
        self.bitrate = int(job.get("bitrate") or 18_000_000)
        self.dt = 1.0 / self.fps

        self.ctx = moderngl.create_standalone_context(require=330)
        log("[gpu] %s | %s" % (self.ctx.info["GL_RENDERER"], self.ctx.info["GL_VERSION"]))

        self.prog_scene = self.ctx.program(vertex_shader=VS_POS, fragment_shader=FS_SCENE)
        self.prog_blur = self.ctx.program(vertex_shader=VS, fragment_shader=FS_BLUR)
        self.prog_comp = self.ctx.program(vertex_shader=VS, fragment_shader=FS_COMP)
        self.prog_text = self.ctx.program(vertex_shader=VS, fragment_shader=FS_TEXT)

        # 全屏 quad（设计坐标）
        self.vbo_fs = self.ctx.buffer(np.array([
            0, 0, 0, 0,  DESIGN_W, 0, 1, 0,  0, DESIGN_H, 0, 1,
            0, DESIGN_H, 0, 1,  DESIGN_W, 0, 1, 0,  DESIGN_W, DESIGN_H, 1, 1,
        ], dtype="f4").tobytes())
        # 只有位置的版本（场景 pass）
        self.vbo_pos = self.ctx.buffer(np.array([
            0, 0,  DESIGN_W, 0,  0, DESIGN_H,
            0, DESIGN_H,  DESIGN_W, 0,  DESIGN_W, DESIGN_H,
        ], dtype="f4").tobytes())
        self.vbo_quad = self.ctx.buffer(reserve=6 * 4 * 4, dynamic=True)

        self.vao_scene = self.ctx.vertex_array(self.prog_scene, [(self.vbo_pos, "2f", "in_pos")])
        self.vao_blur = self.ctx.vertex_array(self.prog_blur, [(self.vbo_fs, "2f 2f", "in_pos", "in_uv")])
        self.vao_comp = self.ctx.vertex_array(self.prog_comp, [(self.vbo_fs, "2f 2f", "in_pos", "in_uv")])
        self.vao_text = self.ctx.vertex_array(self.prog_text, [(self.vbo_quad, "2f 2f", "in_pos", "in_uv")])

        # FBO：scene(4K) / blur(歌词区，与输出同分辨率) 两趟 / final(4K)
        # ⚠ 曾用 1/2 分辨率，毛玻璃区放大回 4K 时细节被抹平（肉眼「整块糊」）
        #   且与文字锐度形成对比，观感像整体降分辨率。见 RULES §144。
        self.tex_scene = self.ctx.texture((self.W, self.H), 4)
        self.tex_scene.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.fbo_scene = self.ctx.framebuffer(color_attachments=[self.tex_scene])

        bw = max(64, int(LY_W * res))
        bh = max(64, int(LY_H * res))
        self.tex_b1 = self.ctx.texture((bw, bh), 4)
        self.tex_b1.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.fbo_b1 = self.ctx.framebuffer(color_attachments=[self.tex_b1])
        self.tex_b2 = self.ctx.texture((bw, bh), 4)
        self.tex_b2.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.fbo_b2 = self.ctx.framebuffer(color_attachments=[self.tex_b2])

        self.tex_final = self.ctx.texture((self.W, self.H), 4)
        self.fbo_final = self.ctx.framebuffer(color_attachments=[self.tex_final])

        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA

        # 噪点
        rng = np.random.default_rng(20261120)
        n = rng.integers(0, 256, (512, 512), dtype=np.uint8)
        noise = np.dstack([n, n, n, np.full((512, 512), 255, np.uint8)])
        self.tex_grain = self.ctx.texture((512, 512), 4, noise.tobytes())
        self.tex_grain.repeat_x = True
        self.tex_grain.repeat_y = True
        self.tex_grain.filter = (moderngl.LINEAR, moderngl.LINEAR)

        self.tex_cover = self._load_cover(job.get("cover"))
        self.tex_cache = []

    # ------------------------------------------------------------------
    def _load_cover(self, cover):
        img = None
        if isinstance(cover, str) and cover.startswith("data:"):
            img = Image.open(io.BytesIO(base64.b64decode(cover.split(",", 1)[1]))).convert("RGB")
        elif isinstance(cover, str) and os.path.exists(cover):
            img = Image.open(cover).convert("RGB")
        if img is None:
            img = Image.new("RGB", (600, 600), (235, 236, 232))
        s = min(img.size)
        img = img.crop(((img.width - s) // 2, (img.height - s) // 2,
                        (img.width + s) // 2, (img.height + s) // 2)).resize((1200, 1200), Image.LANCZOS)
        tex = self.ctx.texture((1200, 1200), 3, img.tobytes())
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        return tex

    def _upload(self, mask):
        rgba = mask_to_rgba(mask)
        tex = self.ctx.texture(rgba.size, 4, rgba.tobytes())
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.tex_cache.append(tex)
        return tex

    def _set_quad(self, x, y, w, h):
        v = np.array([
            x, y, 0, 0,  x + w, y, 1, 0,  x, y + h, 0, 1,
            x, y + h, 0, 1,  x + w, y, 1, 0,  x + w, y + h, 1, 1,
        ], dtype="f4")
        self.vbo_quad.write(v.tobytes())

    def _draw_text(self, tex, x, y, w, h, alpha, tint, blur_px=0.0, kar=-1.0):
        self._set_quad(x, y, w, h)
        tex.use(location=0)
        p = self.prog_text
        p["u_tex"] = 0
        p["u_res"] = (DESIGN_W, DESIGN_H)
        p["u_alpha"] = float(alpha)
        p["u_tint"] = tuple(c / 255.0 for c in tint)
        p["u_blur"] = float(blur_px * SS)
        p["u_texel"] = (1.0 / tex.size[0], 1.0 / tex.size[1])
        p["u_kar"] = float(kar)
        self.vao_text.render()

    # ------------------------------------------------------------------
    def prepare(self):
        j = self.job
        title = (j.get("title") or "未命名歌曲").strip()
        artist = (j.get("artist") or "").strip()
        vocal = (j.get("vocal") or "").strip()
        # 片头补静音（head_pad）必须计入片长：浏览器通路是从「解码后的音频」取时长，
        # 而 mp3 已被 _ensure_head_silence 用 adelay 拉长 head_pad；GPU 通路只读 job.duration
        # （= 切点跨度），漏掉 pad 会让成片比音频短，QC A02 报「时长偏差 -head_pad」。
        # 歌词时间轴在 produce_one 里已按 head_pad 整体后移，故 t=0 即补静音起点，口径一致。
        duration = float(j.get("duration") or 0) + float(j.get("head_pad") or 0)
        self.duration = duration
        self.lyrics = parse_lrc(j.get("lrc") or "", duration)
        n = len(self.lyrics)

        # 标题
        nch = max(1, len(title))
        if CJK_RE.search(title):
            hf = max(64, min(112, int(780 / (nch * 1.08))))
        else:
            hf = max(52, min(112, int(780 / (nch * 0.62))))
        m, w, h = text_mask(title, int(hf * SS), 700)
        self.t_title = self._upload(m)
        ty = COVER_Y + COVER_S + 30.0
        self.title_geo = (96.0, ty, w / SS, h / SS)

        # 信息行
        length = "%d:%02d" % (int(duration) // 60, int(duration) % 60)
        info = "VOCAL %s   ·   ORIGINAL %s   ·   STYLE 翻唱/现场版   ·   LENGTH %s" % (vocal, artist, length)
        m, w, h = text_mask(info, int(13 * SS), 500)
        self.t_info = self._upload(m)
        self.info_geo = (96.0, self.title_geo[1] + self.title_geo[3] + 10.0, w / SS, h / SS)

        # 刊眉 / 时间
        m, w, h = text_mask("SUI　MUSIC　ARCHIVE", int(13 * SS), 500)
        self.t_brand = self._upload(m)
        self.brand_geo = (96.0, 76.0 - h / SS * 0.78, w / SS, h / SS)

        track = j.get("track") or "TRACK 01"
        m, w, h = text_mask(track, int(13 * SS), 500)
        self.t_track = self._upload(m)
        self.track_geo = (1824.0 - w / SS, 76.0 - h / SS * 0.78, w / SS, h / SS)

        m, w, h = text_mask(length, int(13 * SS), 500)
        self.t_len = self._upload(m)
        self.len_geo = (1824.0 - w / SS, 1030.0 - h / SS * 0.78, w / SS, h / SS)

        m, w, h = text_mask("2026.11.19", int(11 * SS), 400)
        self.t_date = self._upload(m)
        self.date_geo = (1824.0 - w / SS, 1054.0 - h / SS * 0.78, w / SS, h / SS)

        # 歌词纹理（清晰一套，模糊由 shader 做）
        # 每行额外存：字符横向起点 xs（设计坐标）+ 逐字时间 words + 行尾时间 fill，
        # 供卡拉OK填充用（与 CPU 端 karaokeFrac/karaokeWidth 同一套算法）。
        self.lyr = []
        for t, txt, words, fill in self.lyrics:
            m, w, h, xs = text_mask_chars(txt, int(60 * SS), 700)
            self.lyr.append({
                "tex": self._upload(m),
                "w": w / SS, "h": h / SS,
                "xs": [v / SS for v in xs],
                "text": txt, "words": words, "fill": fill,
                "t": t,
            })
        # 行结束时间：优先行尾 <mm:ss.x>，否则下一行起点，末行用片长
        for i, sp in enumerate(self.lyr):
            sp["end"] = (sp["fill"] if sp["fill"] is not None
                         else (self.lyr[i + 1]["t"] if i + 1 < len(self.lyr) else duration))
            if sp["end"] is not None and sp["end"] <= sp["t"] + 0.05:
                sp["end"] = sp["t"] + max(1.0, duration / max(1, len(self.lyr)))
        # 当前行左侧绯玫红标记（3×78 设计像素的纯白块，绘制时 tint 成红色）
        self.t_bar = self._upload(Image.new("L", (max(1, int(3 * SS)), max(1, int(78 * SS))), 255))

        # 动效状态
        self.scrollY = 0.0
        self.act = [0.0] * n
        self.bs = [None] * n
        self.bl = [None] * n
        self.ox = [0.0] * n          # 当前行横向滚动偏移（长行跟唱）
        self._cur_sec = None
        self._cur_sec_tex = None
        self._prev_idx = -999

        log("[gpu] 准备完成：%d 行歌词 / %d 张文字纹理 / 输出 %dx%d@%d"
            % (n, len(self.tex_cache), self.W, self.H, self.fps))

    # ------------------------------------------------------------------
    def _blur_level(self, dist):
        # 1 行 3px → 4 行 12px
        return min(12.0, 3.0 + 3.0 * max(0, dist - 1))

    def _update_motion(self, t):
        lyr = self.lyrics
        n = len(lyr)
        if n == 0:
            return -1
        idx = cur_index(t, lyr)
        if idx < 0:
            idx = 0
        anchor = idx
        # scrollY 指数缓动（τ≈0.18s）
        target = (idx * LY_LH) - (FOCAL_Y - LY_Y)
        self.scrollY += (target - self.scrollY) * min(1.0, self.dt * 5.5)

        for i in range(n):
            tg = 1.0 if i == idx else 0.0
            k = 9.0 if tg > self.act[i] else 4.2
            self.act[i] += (tg - self.act[i]) * min(1.0, self.dt * k)

            if i < anchor:
                base = 0.34
            elif i == idx:
                base = 1.0
            else:
                d = i - idx
                base = 0.24 + 0.36 * math.exp(-(d - 1) / 1.7)
            if self.bs[i] is None:
                self.bs[i] = base
            self.bs[i] += (base - self.bs[i]) * min(1.0, self.dt * 9.0)

            bt = 0.0 if i == idx else (0.0 if i < anchor else self._blur_level(abs(i - anchor)))
            if self.bl[i] is None:
                self.bl[i] = bt
            self.bl[i] += (bt - self.bl[i]) * min(1.0, self.dt * (12.0 if i == idx else 5.5))
        self._prev_idx = idx
        return idx

    # ------------------------------------------------------------------
    def _sung_x(self, sp, t):
        """已唱到的横向位置（**设计坐标**，与 sp["xs"] 同系，含左右 pad）。

        有逐字轴 → 按字推进（字内线性）；无逐字轴 → 行时长线性推进。
        与 CPU 端 karaokeFrac/karaokeWidth 等价，但去掉了「同时刻词链」特判：
        这里把同刻字整段视为一个区间，宽度近似即可（4K 下肉眼不可辨）。
        """
        xs = sp["xs"]
        n = len(xs)
        if n == 0:
            return 0.0
        words = sp["words"]
        if words and len(words) == n:
            ts = [w[0] for w in words]
            if max(ts) - min(ts) > 0.05:
                j = 0
                for i in range(n):
                    if ts[i] <= t:
                        j = i
                    else:
                        break
                T0, T1 = ts[j], (ts[j + 1] if j + 1 < n else sp["end"])
                frac = max(0.0, min(1.0, (t - T0) / max(0.05, T1 - T0)))
                x0 = xs[j]
                x1 = xs[j + 1] if j + 1 < n else xs[j] + 60.0
                return x0 + (x1 - x0) * frac
        s, e = sp["t"], sp["end"]
        f = 0.0 if e <= s else (t - s) / (e - s)
        f = max(0.0, min(1.0, f))
        x1 = xs[-1] + 60.0
        return xs[0] + (x1 - xs[0]) * f

    # ------------------------------------------------------------------
    def render_frame(self, i):
        t = self.t0 + i / self.fps
        ctx = self.ctx
        idx = self._update_motion(t)

        # ---------- Pass1: scene ----------
        self.fbo_scene.use()
        ctx.clear(0.96, 0.96, 0.95, 1.0)
        ps = self.prog_scene
        ps["u_res"] = (DESIGN_W, DESIGN_H)
        ps["u_time"] = t
        ps["u_frame"] = float(i % 997)   # 颗粒抖动的帧种子（取模避免大数精度损失）
        ps["u_progress"] = max(0.0, min(1.0, t / max(0.001, self.duration)))
        ps["u_top"] = tuple(c / 255.0 for c in PAPER_TOP)
        ps["u_bot"] = tuple(c / 255.0 for c in PAPER_BOT)
        ps["u_cover_rect"] = (COVER_X, COVER_Y, COVER_S, COVER_S)
        ps["u_cover_r"] = COVER_R
        ps["u_grain"] = 1
        ps["u_cover"] = 0
        self.tex_grain.use(location=1)
        self.tex_cover.use(location=0)
        self.vao_scene.render()

        # ---------- Pass2: 毛玻璃（水平→b1，垂直→b2）----------
        pb = self.prog_blur
        pb["u_res"] = (DESIGN_W, DESIGN_H)
        pb["u_scene"] = 0
        pb["u_rect"] = (LY_X, LY_Y, LY_W, LY_H)
        pb["u_scene_res"] = (float(self.W), float(self.H))
        pb["u_radius"] = 34.0 * self.res
        self.tex_scene.use(location=0)
        self.fbo_b1.use()
        pb["u_dir"] = (1.0, 0.0)
        self.vao_blur.render()
        self.fbo_b2.use()
        self.tex_b1.use(location=0)
        pb["u_dir"] = (0.0, 1.0)
        self.vao_blur.render()

        # ---------- Pass3: 合成 ----------
        self.fbo_final.use()
        ctx.clear(0.96, 0.96, 0.95, 1.0)
        pc = self.prog_comp
        pc["u_res"] = (DESIGN_W, DESIGN_H)
        pc["u_scene"] = 0
        pc["u_blur"] = 1
        pc["u_grain"] = 2
        pc["u_glass_rect"] = (LY_X, LY_Y, LY_W, LY_H)
        pc["u_glass_r"] = 12.0
        self.tex_scene.use(location=0)
        self.tex_b2.use(location=1)
        self.tex_grain.use(location=2)
        self.vao_comp.render()

        # ---------- Pass4: 文字 ----------
        self._draw_text(self.t_brand, *self.brand_geo, 0.55, INK)
        self._draw_text(self.t_track, *self.track_geo, 0.55, INK)
        self._draw_text(self.t_len, *self.len_geo, 0.55, INK)
        self._draw_text(self.t_date, *self.date_geo, 0.42, INK)

        sec = int(t)
        if sec != self._cur_sec:
            m, w, h = text_mask("%d:%02d" % (sec // 60, sec % 60), int(13 * SS), 500)
            self._cur_sec_tex = self._upload(m)
            self._cur_sec_geo = (96.0, 1030.0 - h / SS * 0.78, w / SS, h / SS)
            self._cur_sec = sec
        self._draw_text(self._cur_sec_tex, *self._cur_sec_geo, 0.55, INK)

        self._draw_text(self.t_title, *self.title_geo, 1.0, INK)
        self._draw_text(self.t_info, *self.info_geo, 0.62, INK)

        # 歌词（Apple Music 式）
        if idx >= 0 and self.lyr:
            # 视口裁剪（等价 CPU 端 ctx.clip）：当前行超宽横向滚动时文字会顶到画布边缘，
            # 不裁就像「跑出面板」。GL 的 scissor 原点在左下，y 要翻转。
            # moderngl 没有 SCISSOR_TEST 常量，用 enable_direct(GL_SCISSOR_TEST=0x0C11)。
            self.ctx.scissor = (int((LY_X - 24) * self.res),
                                int((DESIGN_H - (LY_Y + LY_H)) * self.res),
                                int((LY_W + 48) * self.res),
                                int(LY_H * self.res))
            _sc = True
            try:
                self.ctx.enable_direct(_GL_SCISSOR_TEST)
            except Exception:
                _sc = False
            n = len(self.lyr)
            for k in range(n):
                sp = self.lyr[k]
                y = LY_Y + k * LY_LH - self.scrollY
                if y + LY_LH < LY_Y - 40 or y > LY_Y + LY_H + 40:
                    continue
                tex = sp["tex"]
                fs = 48.0 + 17.0 * self.act[k]
                sc = fs / 60.0
                w, h = sp["w"] * sc, sp["h"] * sc
                if k != idx and w > LY_W - 12:
                    s2 = (LY_W - 12) / sp["w"]
                    w, h = sp["w"] * s2, sp["h"] * s2
                    sc = s2
                cy = y + LY_LH * 0.5
                # 视口上下 80px 边缘淡出
                edge = min(
                    max(0.0, min(1.0, (y - (LY_Y - 72)) / 120.0)),
                    max(0.0, min(1.0, ((LY_Y + LY_H + 42) - (y + LY_LH)) / 120.0)))
                edge = edge * edge * (3 - 2 * edge)
                alpha = (self.bs[k] + (1 - self.bs[k]) * self.act[k]) * edge
                blur = self.bl[k] if self.bl[k] is not None else 0.0
                x = LY_X + 4.0 * self.act[k]
                kar = -1.0
                if k == idx:
                    self._draw_text(self.t_bar, LY_X - 13.0, y + 12.0, 3.0, LY_LH - 24.0, 0.85, ROSE)
                    # 已唱比例 → shader 的 uv 阈值（分母是整幅纹理，故含 pad）
                    sung = self._sung_x(sp, t)
                    # ⚠ 分母必须是 sp["w"]（设计坐标宽），不是 sp["w"]*SS：
                    #   xs 在 prepare 里已除以 SS 转成设计坐标，再乘 SS 会让填充进度
                    #   慢 SS 倍（4K 下只亮 1/4 行）。
                    kar = max(0.0, min(1.0, sung / max(1e-3, sp["w"])))
                    # 长行横向滚动：让「正在唱的字」稳定停在视口 42% 处
                    view = LY_W - 12.0
                    if w > view:
                        want = max(0.0, min(w - view, sung * sc - view * 0.42))
                    else:
                        want = 0.0
                    self.ox[k] += (want - self.ox[k]) * min(1.0, self.dt * 3.2)
                    x -= self.ox[k]
                elif abs(self.ox[k]) > 0.5:
                    self.ox[k] += (0.0 - self.ox[k]) * min(1.0, self.dt * 3.2)
                self._draw_text(tex, x, cy - h * 0.5, w, h, alpha, INK, blur, kar)
            if _sc:
                try:
                    self.ctx.disable_direct(_GL_SCISSOR_TEST)
                except Exception:
                    pass

        return self.fbo_final.read(components=4, dtype="f1")

    def warmup(self, t, steps=90):
        """抽片段时把动效状态预热到 t（否则从 0 起滚会有一段明显「下坠」过渡）。

        缓动都是指数形式，跑满 90 帧（1.5s）后误差 <0.1%，等价于「一直在播」。
        """
        if not self.lyrics:
            return
        for _ in range(steps):
            self._update_motion(t)

    # ------------------------------------------------------------------
    def run(self):
        self.prepare()
        if self.t0 > 0:
            self.warmup(self.t0)
        dur = self.duration - self.t0
        if self.sec is not None:
            dur = min(self.sec, dur)
        dur = max(0.1, dur)
        frames = max(1, int(round(dur * self.fps)))
        ff = _ffmpeg_path()
        if not os.path.exists(ff):
            ff = "ffmpeg"
        cmd = [
            ff, "-y", "-hide_banner", "-loglevel", "warning", "-stats",
            "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "%dx%d" % (self.W, self.H),
            "-r", str(self.fps), "-i", "pipe:0",
            "-vf", "vflip",
            "-c:v", "h264_nvenc", "-preset", "p7", "-tune", "hq",
            "-b:v", str(self.bitrate), "-maxrate", str(int(self.bitrate * 1.03)),
            "-bufsize", str(int(self.bitrate * 2)), "-rc", "cbr",
            "-profile:v", "high", "-level", "5.2",
            "-pix_fmt", "yuv420p", "-colorspace", "bt709",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
            "-bsf:v", "h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1",
            "-movflags", "+faststart", "-an", self.out,
        ]
        log("[gpu] 输出 %dx%d@%d，%d 帧（%.2fs）→ %s" % (self.W, self.H, self.fps, frames, dur, self.out))
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=0)
        t0 = time.time()
        try:
            for i in range(frames):
                proc.stdin.write(self.render_frame(i))
                if i % self.fps == 0:
                    el = time.time() - t0
                    log("[gpu] %5d/%d  %.1f fps  %.1fs" % (i, frames, (i + 1) / max(el, 1e-6), el))
            proc.stdin.close()
            proc.wait()
        except BrokenPipeError:
            log("[gpu] ffmpeg 提前退出 rc=%s" % proc.poll())
            raise
        el = time.time() - t0
        log("[gpu] done：%d 帧 / %.1fs = %.2f fps（%.1f ms/帧）"
            % (frames, el, frames / el, el / frames * 1000.0))
        return el


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--sec", type=float, default=None)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--res", type=int, default=None)
    a = ap.parse_args()
    job = json.load(io.open(a.job, "r", encoding="utf-8"))
    out = a.out or job.get("out")
    if not out:
        raise SystemExit("缺少输出路径")
    GpuRenderer(job, out, sec=a.sec, res=int(a.res or job.get("res") or 2),
                start=a.start).run()


if __name__ == "__main__":
    main()
