#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
gpu_render.py —— A 唱片「显卡直接算画面」渲染器（moderngl / OpenGL 3.3 + NVENC）

与 render_song.cjs（Chromium 逐帧 toBlob，72ms/帧）的根本区别：
  · 画面不再由浏览器光栅化，而是由 fragment shader 在显卡上逐像素计算；
  · CPU 只在启动时做一次性准备（文字→纹理、封面→纹理、噪点→纹理）；
  · 渲染循环里 CPU 只做两件事：推进动效状态、把 GPU 算好的 4K 帧读回来送编码器。

画面管线（每帧 3 个 GPU pass，全部直接写在 final 缓冲上）：
  Pass1  scene  → 纸底渐变 / 三片光晕 / 纸纹噪点 / 圆角封面 / 进度条
  Pass2  水印   → 歌词区右侧立绘衬景（预烘渐变遮罩 + 预乘模糊，低不透明度）
  Pass3  文字   → 标题/信息/歌词/刊眉/时间

⚠ 不要给歌词区再叠「毛玻璃面板」：CPU 版根本没有这一层（drawWatermark 直接画在
  纸底上），GPU 版多出来的模糊+混白+提亮会把那块抬成一块实心白板。见 RULES §146。

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
from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import moderngl
except ImportError:
    print("缺少 moderngl：pip install moderngl", file=sys.stderr)
    raise

DESIGN_W, DESIGN_H = 1920, 1080
SS = 2.0                     # 文字纹理超采样倍数（__init__ 里按输出分辨率覆写）

# ---- 歌词模糊：离线预烘多档高斯（RULES §145）----
# 曾经走「shader 里 11-tap 伪高斯」，半径一大就露馅：采样步长 = 半径 texel，
# 点与点之间毫无覆盖 → 每个笔画被复制成 11 个幽灵；再加 pad 只有 10 texel，
# 采样越界被 CLAMP_TO_EDGE 拉成整片实心 → 屏幕上就是一张网格状马赛克。
# 现在改成与 CPU 端（ctx.filter='blur()' 预烘 5 档）完全相同的策略：
# 准备阶段用 Pillow 真高斯烘 5 档纹理，运行期只做两张纹理按权重 mix，零开销。
BLUR_LV = (0.0, 3.0, 6.0, 9.0, 12.0)   # 设计 px，与 CPU 端 BLUR_LV 一致
LY_PAD = 44.0                # 歌词纹理四周留白（设计 px）：必须 > 3×最大模糊半径
BLUR_SS = 2.0                # 模糊档纹理的超采样（模糊本身是低频，2 倍足够）

# ---- 歌词区水印立绘（对齐 CPU 端 WM 参数：renderer/player/index.html drawWatermark）----
# CPU 通路一直是「纸底 → 水印立绘 → 歌词」三层，GPU 迁移时整层丢了。
WM_ALPHA = 0.17              # 整体不透明度（0.1–0.2 = 水印感）
WM_BLUR = 2.0                # 高斯半径（设计 px；4K 输出自动变 4 物理 px，与 CPU 同强度）
WM_HEIGHT = 0.90             # 占歌词视口高度比例
WM_RIGHT_PAD = 6.0           # 距视口右缘（设计 px）
WM_TOP = 0.04                # 顶部下移比例
WM_MASK_TOP = 0.12           # 头顶渐入带宽（0.30 → 1）
WM_MASK_BOTTOM = 0.76        # 底部渐出起点（→1 渐隐到 0）
WM_MASK_RIGHT = 0.93         # 右缘渐出起点
WM_SS = 2.0                  # 纹理超采样（4K 下 1 设计 px = 1 纹理 px = 2 物理 px）

# ---- A 唱片版式常量（对齐 renderer/player/index.html）----
COVER_X, COVER_Y, COVER_S, COVER_R = 268.0, 96.0, 560.0, 10.0
LY_X, LY_Y, LY_W, LY_H, LY_LH = 980.0, 140.0, 844.0, 844.0, 102.0
FOCAL_Y = LY_Y + LY_H * 0.242          # 当前行落点（≈344）
FILL_SEC_PER_CHAR = 0.45               # 逐字填充的时长估价（秒/字），只在行间长间奏时兜底
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


def text_mask_chars_d(text, px, weight=600, pad=8.0, ss=1.0, blur=0.0):
    """设计单位版 text_mask_chars：px/pad 都是**设计像素**，内部按 ss 放大栅格化。

    与旧接口的区别（旧接口 px/pad 是纹理像素，语义模糊）：字号随 ss 缩放，但版面
    尺寸（w/h/xs）统一按设计像素返回，调用方不必再除以 SS。blur>0 时在纹理上做
    Pillow 真高斯——连续卷积，不会像稀疏 tap 那样长出网格。

    **pad 必须足够大**：高斯在纹理边界外没有数据，pad 小于 3σ 就会被裁成硬边。
    """
    fpx = max(4, int(round(px * ss)))
    ppad = float(pad) * ss
    font = get_font(text, fpx, weight)
    dummy = ImageDraw.Draw(Image.new("L", (4, 4)))
    try:
        bbox = dummy.textbbox((0, 0), text, font=font)
    except Exception:
        bbox = (0, 0, int(fpx * len(text) * 0.6), fpx)
    w = max(1.0, bbox[2] - bbox[0] + ppad * 2)
    h = max(1.0, bbox[3] - bbox[1] + ppad * 2)
    img = Image.new("L", (max(1, int(round(w))), max(1, int(round(h)))), 0)
    d = ImageDraw.Draw(img)
    ox = ppad - bbox[0]
    oy = ppad - bbox[1]
    d.text((ox, oy), text, font=font, fill=255)
    if blur > 0.01:
        img = img.filter(ImageFilter.GaussianBlur(float(blur) * ss))
    xs = []
    for i in range(len(text)):
        try:
            pre = float(font.getlength(text[:i]))
        except Exception:
            pre = i * fpx * 0.6
        xs.append((ox + pre) / ss)
    # 返回的 w/h 用**实际纹理像素**换算，保证与 uv 严格同比例（取整误差 < 1 texel）
    return img, img.size[0] / ss, img.size[1] / ss, xs


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

    /* ---- 封面组：严格对齐 CPU 版 drawCover 的五个层次 ----
       ① 取色环境光晕  ② 两圈发丝轨道圆 + 冰蓝节点  ③ 贴底落影
       ④ 圆角封面本体  ⑤ 发丝级内描边（1px 界定轮廓）
       迁移时只做了 ④，封面于是「一块平贴的图」，少了 editorial 的悬浮感。
       呼吸缩放 9s ±0.6%（与 CPU 同一节奏），顺带让静止段也有帧间变化（V04）。 */
    float breathe = 1.0 + sin(u_time / 9.0 * 6.2831853) * 0.006;
    vec2 csz = u_cover_rect.zw * breathe;
    vec2 ccen = u_cover_rect.xy + u_cover_rect.zw * 0.5;
    vec2 c = p - ccen;
    float ea = 0.5;                       // 抗锯齿带宽（设计 px）
    vec3 cInk = vec3(0.118, 0.157, 0.212);
    vec3 cAcc = vec3(0.659, 0.698, 0.745);

    // ① 环境光晕：从封面半径 0.30 处向外柔和扩散到 1.06
    float da = length(c) / csz.x;
    col += cAcc * 0.048 * (1.0 - smoothstep(0.30, 1.06, da));

    // ② 轨道圆（两圈，自封面后缘露出）+ 环上冰蓝节点
    vec2 o1 = vec2(csz.x * 0.14, -csz.x * 0.06);
    float d1 = abs(length(c - o1) - csz.x * 0.63);
    col = mix(col, cInk, 0.085 * (1.0 - smoothstep(ea, ea * 1.5, d1)));
    vec2 o2 = vec2(-csz.x * 0.26, csz.x * 0.30);
    float d2 = abs(length(c - o2) - csz.x * 0.62);
    col = mix(col, cInk, 0.060 * (1.0 - smoothstep(ea, ea * 1.5, d2)));
    float ang = -0.55 + sin(u_time / 7.0) * 0.06;
    float dn = length(c - (o1 + vec2(cos(ang), sin(ang)) * csz.x * 0.63)) - 2.6;
    col = mix(col, vec3(0.220, 0.698, 0.816), 0.80 * (1.0 - smoothstep(ea, ea * 1.5, dn)));

    // ③ 落影：极轻的贴底椭圆（editorial 轻投影，不是重阴影）
    vec2 sh = p - vec2(ccen.x, ccen.y + csz.x * 0.52);
    float ds = length(vec2(sh.x / (csz.x * 0.60), sh.y / (csz.x * 0.13)));
    col = mix(col, vec3(0.157, 0.196, 0.275), 0.10 * (1.0 - smoothstep(0.0, 1.0, ds)));

    // ④ 封面本体（圆角裁切）
    float d = sdRoundRect(c, csz * 0.5, u_cover_r);
    float m = 1.0 - smoothstep(-1.2, 1.2, d);
    if (m > 0.001) {
        vec2 cuv = (c + csz * 0.5) / csz;
        vec3 cc = texture(u_cover, clamp(cuv, 0.0, 1.0)).rgb;
        col = mix(col, cc, m);
    }
    // ⑤ 内描边：只在封面内侧 1px 带
    float inb = step(d, 0.0) * (1.0 - smoothstep(-ea * 2.2, -ea * 0.6, d));
    col = mix(col, cInk, 0.14 * inb);

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

# Pass2：水印立绘（RGBA 原色直贴，不走 tint —— 人物配色要保留）
FS_IMG = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D u_img;
uniform float u_alpha;
void main() {
    vec4 c = texture(u_img, v_uv);
    if (c.a <= 0.001) discard;
    f_color = vec4(c.rgb, c.a * u_alpha);
}
"""


# Pass4：文字（清晰纹理 / 预烘模糊档 双纹理线性混合，见 RULES §145）
FS_TEXT = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D u_tex;
uniform sampler2D u_tex2;  // 相邻模糊档；清晰行绑同一张，靠 u_mix=0 跳过
uniform float u_mix;       // 0 → 全 u_tex，1 → 全 u_tex2
uniform float u_alpha;
uniform vec3  u_tint;
uniform vec2  u_texel;     // 1/纹理尺寸
uniform float u_kar;       // 卡拉OK 填充阈值（uv.x，<0 = 关闭）
void main() {
    vec2 uv = v_uv;
    float a = texture(u_tex, uv).a;
    if (u_mix > 0.002) {
        a = mix(a, texture(u_tex2, uv).a, u_mix);
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
        self.prog_img = self.ctx.program(vertex_shader=VS, fragment_shader=FS_IMG)
        self.prog_text = self.ctx.program(vertex_shader=VS, fragment_shader=FS_TEXT)

        # 全屏 quad（设计坐标）
        # 只有位置的版本（场景 pass）
        self.vbo_pos = self.ctx.buffer(np.array([
            0, 0,  DESIGN_W, 0,  0, DESIGN_H,
            0, DESIGN_H,  DESIGN_W, 0,  DESIGN_W, DESIGN_H,
        ], dtype="f4").tobytes())
        self.vbo_quad = self.ctx.buffer(reserve=6 * 4 * 4, dynamic=True)

        self.vao_scene = self.ctx.vertex_array(self.prog_scene, [(self.vbo_pos, "2f", "in_pos")])
        self.vao_img = self.ctx.vertex_array(self.prog_img, [(self.vbo_quad, "2f 2f", "in_pos", "in_uv")])
        self.vao_text = self.ctx.vertex_array(self.prog_text, [(self.vbo_quad, "2f 2f", "in_pos", "in_uv")])

        # FBO：只剩一张输出缓冲。曾经的「歌词区毛玻璃面板」两趟 pass 已移除——
        # CPU 版歌词区根本没有面板（drawWatermark 直接画在纸底上），GPU 版多出来的
        # 模糊+混白+提亮把那块抬成实心白板，与周围纸底明显分界。见 RULES §146。
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

    def _set_quad(self, x, y, w, h, cx0=None, cx1=None):
        """写 quad 顶点。给了 cx0/cx1 就做**横向 uv 裁剪**（等价 ctx.clip 左右边）。

        不能用 GL scissor：standalone context 下 ctx.scissor + enable_direct 不生效
        （长歌词行会直接冲出画布右缘）。裁 uv 是确定性的，不依赖任何 GL 状态。
        """
        if cx0 is None and cx1 is None:
            v = np.array([
                x, y, 0, 0,  x + w, y, 1, 0,  x, y + h, 0, 1,
                x, y + h, 0, 1,  x + w, y, 1, 0,  x + w, y + h, 1, 1,
            ], dtype="f4")
            self.vbo_quad.write(v.tobytes())
            return True
        cx0 = -1e9 if cx0 is None else cx0
        cx1 = 1e9 if cx1 is None else cx1
        x0, x1 = x, x + w
        if x1 <= cx0 or x0 >= cx1 or w <= 0.0:
            return False                     # 整块在裁剪区外：直接不画
        X0, X1 = max(x0, cx0), min(x1, cx1)
        u0 = (X0 - x0) / w
        u1 = (X1 - x0) / w
        v = np.array([
            X0, y, u0, 0,  X1, y, u1, 0,  X0, y + h, u0, 1,
            X0, y + h, u0, 1,  X1, y, u1, 0,  X1, y + h, u1, 1,
        ], dtype="f4")
        self.vbo_quad.write(v.tobytes())
        return True

    def _draw_text(self, tex, x, y, w, h, alpha, tint, kar=-1.0, tex2=None,
                   mix=0.0, cx0=None, cx1=None):
        """贴一张文字纹理。tex2/mix 用于模糊行：在相邻两档预烘纹理间线性插值。

        清晰行传 mix=0（tex2 绑回 tex 本身，避免采样器空绑定）。
        cx0/cx1 为横向裁剪边界（歌词视口），uv 会同步缩放，卡拉OK 阈值不受影响。
        """
        if not self._set_quad(x, y, w, h, cx0, cx1):
            return
        tex.use(location=0)
        t2 = tex2 if tex2 is not None else tex
        t2.use(location=1)
        p = self.prog_text
        p["u_tex"] = 0
        try:
            p["u_tex2"] = 1
            p["u_mix"] = float(mix) if tex2 is not None else 0.0
        except KeyError:
            pass                      # 编译器若优化掉该 uniform，退回单纹理
        p["u_alpha"] = float(alpha)
        p["u_tint"] = tuple(c / 255.0 for c in tint)
        # u_texel 只用于卡拉OK 的半像素羽化，仍按清晰纹理算（模糊行不开 kar）
        p["u_texel"] = (1.0 / tex.size[0], 1.0 / tex.size[1])
        p["u_kar"] = float(kar)
        try:
            p["u_res"] = (DESIGN_W, DESIGN_H)
        except KeyError:
            pass
        self.vao_text.render()

    # ------------------------------------------------------------------
    # 水印立绘：贴纸式「原图直接压上去」是不可接受的（浅色人物在米白纸上
    # 会变成一块生硬的白斑，深色人物又会压住歌词）。这里走 CPU 版同一套弱化：
    # 按 alpha 裁到人物主体 → 纵向/横向渐变遮罩（= CSS mask-image）→ 预乘模糊
    # → 低不透明度贴到纸底上。视觉上只是一层隐约的衬影。
    def _prep_watermark(self, path):
        im = Image.open(path).convert("RGBA")
        bb = im.getchannel("A").getbbox()
        if bb:
            im = im.crop(bb)                       # 只留人物，去掉大片透明画布
        h = LY_H * WM_HEIGHT
        w = h * im.width / max(1, im.height)
        im = im.resize((max(1, int(round(w * WM_SS))),
                        max(1, int(round(h * WM_SS)))), Image.LANCZOS)
        aw, ah = im.size
        arr = np.asarray(im, dtype=np.float32) / 255.0
        a = arr[..., 3]
        ys = np.linspace(0.0, 1.0, ah, dtype=np.float32)
        xs = np.linspace(0.0, 1.0, aw, dtype=np.float32)
        gv = np.interp(ys, [0.0, WM_MASK_TOP, WM_MASK_BOTTOM, 1.0],
                       [0.30, 1.0, 1.0, 0.0]).astype(np.float32)
        gh = np.interp(xs, [0.0, WM_MASK_RIGHT, 1.0], [1.0, 1.0, 0.0]).astype(np.float32)
        a = a * gv[:, None] * gh[None, :]
        # 预乘后再模糊：直接对 RGBA 各通道独立模糊，透明区的黑 RGB 会糊出一圈脏边
        pre = np.concatenate([arr[..., :3] * a[..., None], a[..., None]], axis=-1)
        pi = Image.fromarray(np.clip(pre * 255.0 + 0.5, 0, 255).astype(np.uint8), "RGBA")
        if WM_BLUR > 0:
            pi = pi.filter(ImageFilter.GaussianBlur(WM_BLUR * WM_SS))
        pa = np.asarray(pi, dtype=np.float32) / 255.0
        aa = pa[..., 3:4]
        rgb = np.clip(np.divide(pa[..., :3], np.maximum(aa, 1e-4)), 0.0, 1.0)
        img = Image.fromarray((np.concatenate([rgb, aa], axis=-1) * 255.0 + 0.5)
                              .astype(np.uint8), "RGBA")
        tex = self.ctx.texture(img.size, 4, img.tobytes())
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.tex_cache.append(tex)
        self.t_water = tex
        ww, hh = im.width / WM_SS, im.height / WM_SS
        self.water_geo = (LY_X + LY_W - ww - WM_RIGHT_PAD, LY_Y + LY_H * WM_TOP, ww, hh)
        log("[gpu] 水印立绘 %s → %.0fx%.0f 设计 px @ alpha %.2f"
            % (os.path.basename(path), ww, hh, WM_ALPHA))

    def _prep_sticker(self, path):
        """刊眉贴纸（右上角，对齐 CPU 版 drawInfo 的 STICKER 分支）。"""
        im = Image.open(path).convert("RGBA")
        h = 60.0
        w = h * im.width / max(1, im.height)
        im = im.resize((max(1, int(round(w * WM_SS))),
                        max(1, int(round(h * WM_SS)))), Image.LANCZOS)
        tex = self.ctx.texture(im.size, 4, im.tobytes())
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.tex_cache.append(tex)
        self.t_sticker = tex
        # CPU: drawImage(img, 1824-bw, 76-bh+11, bw, bh) —— 图片上部 33.6% 是留白
        self.sticker_geo = (1824.0 - w, 76.0 - h + 11.0, w, h)

    def _draw_image(self, tex, geo, alpha, cx0=None, cx1=None):
        """贴一张 RGBA 图（水印立绘 / 刊眉贴纸共用）。"""
        if not tex or not geo:
            return
        x, y, w, h = geo
        if not self._set_quad(x, y, w, h, cx0, cx1):
            return
        tex.use(location=0)
        p = self.prog_img
        p["u_img"] = 0
        p["u_alpha"] = float(alpha)
        # ⚠ VS 靠 u_res 做设计坐标→NDC；漏传会变成除以 0，三角形整个退化，
        #   画面「什么都没画」却零报错（毛玻璃 pass 早年踩过同一个坑）
        p["u_res"] = (DESIGN_W, DESIGN_H)
        self.vao_img.render()

    def _draw_watermark(self):
        # 立绘宽度可能超过歌词视口，横向裁到视口内（不能压到封面上）
        self._draw_image(self.t_water, self.water_geo, WM_ALPHA,
                         LY_X, LY_X + LY_W)

    def _draw_sticker(self):
        self._draw_image(self.t_sticker, self.sticker_geo, 1.0)

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

        # 歌词纹理：清晰一套 + 模糊四档**预烘**（BLUR_LV[1:]）。
        # 清晰与模糊都用 LY_PAD 留白（设计 px），保证两者 uv 严格同构，混合时不错位；
        # pad 必须 > 3×最大半径，否则高斯在纹理边界被裁掉，边缘拉成硬块。
        # 模糊档纹理多一道 resize，把尺寸锁成清晰纹理的 BLUR_SS/SS 倍，消掉取整误差。
        self.lyr = []
        for t, txt, words, fill in self.lyrics:
            m, w, h, xs = text_mask_chars_d(txt, 60.0, 700, pad=LY_PAD, ss=SS)
            tv = self._upload(m)
            tw = max(1, int(round(m.size[0] * BLUR_SS / SS)))
            th = max(1, int(round(m.size[1] * BLUR_SS / SS)))
            bl = []
            for lv in BLUR_LV[1:]:
                bm, _, _, _ = text_mask_chars_d(txt, 60.0, 700, pad=LY_PAD,
                                                ss=BLUR_SS, blur=lv)
                if bm.size != (tw, th):
                    bm = bm.resize((tw, th), Image.BILINEAR)
                bl.append(self._upload(bm))
            self.lyr.append({
                "tex": tv, "blur": bl,
                "w": w, "h": h,
                "xs": xs,
                "text": txt, "words": words, "fill": fill,
                "t": t,
            })
        # 行结束时间：优先行尾 <mm:ss.x>，否则下一行起点，末行用片长
        for i, sp in enumerate(self.lyr):
            sp["end"] = (sp["fill"] if sp["fill"] is not None
                         else (self.lyr[i + 1]["t"] if i + 1 < len(self.lyr) else duration))
            if sp["end"] is not None and sp["end"] <= sp["t"] + 0.05:
                sp["end"] = sp["t"] + max(1.0, duration / max(1, len(self.lyr)))
            # 行间长间奏保护（2026-11-25）：逐字填充的推进时长取「下一行起点」，
            # 副歌最后一句后面跟一整段间奏时（实测《快乐的扑满》L16：跨 10.2s，
            # 实际只唱 2.6s），字会被拖成每 1.1 秒才点亮一个 —— 观感是「唱完还
            # 卡在这句」。跨度超过字数估价 2.2 倍时压回估价；阈值放这么宽是为了
            # 不碰真正的慢歌（10 字唱 8 秒不会触发）。
            if sp["fill"] is None and sp["end"] is not None:
                _n = len(sp["words"]) if sp["words"] else len(
                    [c for c in sp["text"] if c.strip()])
                _est = max(1.6, FILL_SEC_PER_CHAR * _n)
                if sp["end"] - sp["t"] > 2.2 * _est:
                    sp["end"] = sp["t"] + _est
        # 水印立绘（歌词区右侧衬景，纸底之上 / 歌词之下）
        self.t_water = None
        self.water_geo = None
        self.t_sticker = None
        self.sticker_geo = None
        wm = (j.get("watermark") or "").strip()
        if wm and os.path.exists(wm):
            try:
                self._prep_watermark(wm)
            except Exception as e:
                log("[gpu] ⚠ 水印立绘加载失败，按无水印继续：%s" % e)
        elif wm:
            log("[gpu] ⚠ 水印立绘路径不存在：%s" % wm)
        else:
            log("[gpu] 未配置水印立绘（config.assets.watermark）")

        # 刊眉贴纸（右上角；CPU 版在最上层，这里放在文字之后画）
        st = (j.get("sticker") or "").strip()
        if st and os.path.exists(st):
            try:
                self._prep_sticker(st)
                # 贴纸占了右上角，TRACK 文字让位到贴纸左侧（CPU 版同样处理）
                tx, ty2, tw, th = self.track_geo
                self.track_geo = (1824.0 - self.sticker_geo[2] - 14.0 - tw, ty2, tw, th)
            except Exception as e:
                log("[gpu] ⚠ 贴纸加载失败，跳过：%s" % e)

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
        self._hi = -1              # 真正正在唱的行（-1 = 还没到第一行，不高亮）

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
        # raw = 真正「正在唱」的行号；还没到第一行时为 -1。
        # 旧代码把 -1 直接当 0，于是从片头到第一行之前，第一行一直是高亮 + 竖线 +
        # 逐字填充 —— 用户看到的就是「还没开始唱，歌词已经亮着/在滚」。
        raw = idx
        if idx < 0:
            idx = 0
        self._hi = raw
        anchor = idx
        # scrollY 指数缓动（τ≈0.18s）
        target = (idx * LY_LH) - (FOCAL_Y - LY_Y)
        self.scrollY += (target - self.scrollY) * min(1.0, self.dt * 5.5)

        for i in range(n):
            tg = 1.0 if i == raw else 0.0
            k = 9.0 if tg > self.act[i] else 4.2
            self.act[i] += (tg - self.act[i]) * min(1.0, self.dt * k)

            if i < anchor:
                base = 0.34
            elif i == raw:
                base = 1.0
            else:
                d = i - idx
                base = 0.24 + 0.36 * math.exp(-(d - 1) / 1.7)
            if self.bs[i] is None:
                self.bs[i] = base
            self.bs[i] += (base - self.bs[i]) * min(1.0, self.dt * 9.0)

            bt = 0.0 if i == raw else (0.0 if i < anchor else self._blur_level(abs(i - anchor)))
            if self.bl[i] is None:
                self.bl[i] = bt
            self.bl[i] += (bt - self.bl[i]) * min(1.0, self.dt * (12.0 if i == raw else 5.5))
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
        self.fbo_final.use()
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

        # ---------- Pass2: 水印立绘（纸底之上 / 歌词之下，与 CPU 版 drawWatermark 同层）----------
        self._draw_watermark()

        # ---------- Pass3: 文字 ----------
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
            # 视口左右边界（等价 CPU 端 ctx.clip）：当前行超宽横向滚动时不能冲出画布。
            # 走 uv 裁剪而非 GL scissor —— standalone context 下 scissor 不生效（踩过）。
            clip_l, clip_r = LY_X - 24.0, LY_X + LY_W + 24.0
            n = len(self.lyr)
            for k in range(n):
                sp = self.lyr[k]
                y = LY_Y + k * LY_LH - self.scrollY
                if y + LY_LH < LY_Y - 40 or y > LY_Y + LY_H + 40:
                    continue
                fs = 48.0 + 17.0 * self.act[k]
                sc = fs / 60.0
                cw = sp["w"] - 2 * LY_PAD          # 文字实际占宽（设计 px，扣掉留白）
                # 超宽行一律缩到视口内：旧实现只缩「非当前行」，当前行靠横向滚动兜底，
                # 但滚动量 = sung*sc − view*0.42，开唱头几个字时 want 仍是 0 → 行尾溢出
                # 视口直接消失（2026-11-24《东风破》L36「荒烟漫草的年头 就连分手都很沉默」
                # 实测「沉默」两字被裁；L24 左端「而如」也被裁）。缩放后 cw*sc≈view，
                # 滚动自然为 0；确实超长（>18 字）时仍由下面的滚动接管。
                if cw * sc > LY_W - 12:
                    sc = (LY_W - 12) / cw
                w, h = sp["w"] * sc, sp["h"] * sc
                cy = y + LY_LH * 0.5
                # 视口上下 80px 边缘淡出
                edge = min(
                    max(0.0, min(1.0, (y - (LY_Y - 72)) / 120.0)),
                    max(0.0, min(1.0, ((LY_Y + LY_H + 42) - (y + LY_LH)) / 120.0)))
                edge = edge * edge * (3 - 2 * edge)
                alpha = (self.bs[k] + (1 - self.bs[k]) * self.act[k]) * edge
                # 模糊：在相邻两档**预烘**纹理间线性插值（0=清晰 / 3 / 6 / 9 / 12 设计 px）
                tex, tex2, mix = sp["tex"], None, 0.0
                blv = self.bl[k] if self.bl[k] is not None else 0.0
                nb = len(sp["blur"])
                if blv > 0.4 and nb:
                    f = min(float(nb), max(0.0, blv) / BLUR_LV[1])
                    lo = int(math.floor(f))
                    hi = min(nb, lo + 1)
                    mix = f - lo
                    tex = sp["tex"] if lo == 0 else sp["blur"][lo - 1]
                    tex2 = sp["tex"] if hi == 0 else sp["blur"][hi - 1]
                x = LY_X + 4.0 * self.act[k]
                kar = -1.0
                if k == self._hi:
                    self._draw_text(self.t_bar, LY_X - 13.0, y + 12.0, 3.0, LY_LH - 24.0, 0.85, ROSE)
                    # 已唱比例 → shader 的 uv 阈值（分母是整幅纹理，故含 pad）
                    sung = self._sung_x(sp, t)
                    # ⚠ 分母必须是 sp["w"]（设计坐标宽），不是 sp["w"]*SS：
                    #   xs 已是设计坐标，再乘 SS 会让填充进度慢 SS 倍（4K 下只亮 1/4 行）。
                    kar = max(0.0, min(1.0, sung / max(1e-3, sp["w"])))
                    # 长行横向滚动：让「正在唱的字」稳定停在视口 42% 处
                    view = LY_W - 12.0
                    if cw * sc > view:
                        want = max(0.0, min(cw * sc - view, sung * sc - view * 0.42))
                    else:
                        want = 0.0
                    self.ox[k] += (want - self.ox[k]) * min(1.0, self.dt * 3.2)
                    x -= self.ox[k]
                elif abs(self.ox[k]) > 0.5:
                    self.ox[k] += (0.0 - self.ox[k]) * min(1.0, self.dt * 3.2)
                # x 是「文字左端」，quad 左边界要再退 LY_PAD*sc（纹理四周留白同比例缩放）
                self._draw_text(tex, x - LY_PAD * sc, cy - h * 0.5, w, h,
                                alpha, INK, kar, tex2, mix, clip_l, clip_r)

        self._draw_sticker()          # 刊眉贴纸压在最上层（CPU 版同样是最后画）

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
