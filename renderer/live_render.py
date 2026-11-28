# -*- coding: utf-8 -*-
"""歌切 MP4 渲染器（live 版式）：主播实时画面 + 动态歌词 → 4K60 MP4。

设计目标与边界
--------------
· 复用旧版 MP4 输出链路（song_cutter → 本脚本 → ffmpeg 混流），不新建管线。
· 与 renderer/gpu_render.py（A 唱片封面版）**并行存在**，旧版一行不动，可随时回退。
· 解码走 NVDEC、合成走 OpenGL、编码走 NVENC —— 三段都在显卡上，中间只有两次
  必要的内存搬运（源帧 nv12 下载 3.1MB/帧、成片 RGBA 回读 33MB/帧），
  这是「用 Python + ffmpeg 管道」这条链路无法消除的下限，已实测不是瓶颈。

歌词版式与动效继承 A 方案（renderer/player/index.html drawLyrics）：
  · 字体 —— 粗体无衬线（Noto Sans 700），不是细体字幕；译文同族 500，只降字号与字重。
  · 动效 —— scrollY dt*5.5；act 聚焦 dt*9 / 失焦 dt*4.2；bl 当前行 dt*12 / 其余 dt*5.5；
    透明度 已唱 .30 / 当前 1.0（未唱 .42，单遍卡拉 OK 填充）/ 未唱 .20+.30·exp(-(d-1)/1.7)；
    字号 1.0→1.16（以原文底边为锚向上生长）；视口上下沿淡出；长行按演唱进度横移。
  · 版面 —— 只有字、色、光：颜色由这一帧的环境色决定（深场 + 焦点柔光），
    没有任何框线 / 圆环 / 点阵 / 空心大字 / 频谱条（#164 明确移除）。
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time

import moderngl
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------- 设计坐标（×res 得输出分辨率） ----------------
SW, SH = 1920.0, 1080.0
PANEL_W = SW * 0.55                 # 人物面板宽（55%，与新播放器一致）
PANEL_X = 0.0
LYR_X = 1102.0                      # 歌词列左缘（文字起点 / 淡入边界都按这条线算）
LYR_MARK_X = LYR_X - 116.0          # 活动行标记竖条：必须落在淡出窗口**左端之外**，
                                    # 否则滚出的字会以半透明残影压在竖条上（§165）
LYR_W = 722.0                       # 列宽：LYR_X + 722 = 1824 = 底栏时间码右对齐线
LYR_FADE = (LYR_X - 104.0, LYR_X)   # 左缘软淡出区间（设计坐标 / 屏幕空间），宽度 ≈ 2 倍字宽：
                                    # 滚出列框的文字在这里化到 0；竖条右缘在 LYR_X−113，
                                    # 落在窗口之外，横移永远压不到它，也不会出现「半个字」的断口
LYR_TOP = 118.0                     # 歌词视口上沿
LYR_BOT = 902.0                     # 歌词视口下沿（再往下是底栏）
FOCAL_Y = 296.0                     # 活动行**顶边**（原文+译文整体中心 ≈ 33%，画面上部 1/3）
BASE_PX = 52.0                      # 歌词主字号（设计像素）—— A 方案的字号量级，不是小字幕
BASE_WEIGHT = 700                   # 原文一律粗体（A 方案辨识度的来源，不许退成细体）
LH = 118.0                          # 行高：52px 粗体 + 24px 译文 + 呼吸
LH_NOTRANS = 92.0                   # 无译文时收紧：52px 配 92px ≈ 1.77 倍行距
TRANS_PX = 24.0                     # 译文
TRANS_DY = 64.0                     # 译文相对原文顶的间距
TRANS_K = 0.60                      # 译文 / 原文 的不透明度比例（**常数**：两条曲线同步变化）
TRANS_WEIGHT = 500                  # 译文与原文同族同风格，只靠字号 + 字重分主次
INK_TINT = 0.22                     # 歌词墨色向画面色相偏移的强度（0 = 纯白，1 = 满色相）
COVER_SEC = 3.0                     # 片头封面的默认时长（秒，静音）
TITLE_PX = 74.0
LABEL_PX = 11.5
CLOCK_PX = 25.0

INK = (242 / 255.0, 236 / 255.0, 230 / 255.0)
ACCENT = (232 / 255.0, 164 / 255.0, 182 / 255.0)
BG = (6 / 255.0, 6 / 255.0, 8 / 255.0)

FSRC = {
    "serif_jp": ["official_pv/engine/fonts/NotoSerifJP_wght_.ttf",
                 "C:/Windows/Fonts/NotoSerifJP-VF.ttf"],
    "serif_sc": ["official_pv/engine/fonts/NotoSerifSC_wght_.ttf",
                 "C:/Windows/Fonts/NotoSerifSC-VF.ttf"],
    "sans_sc": ["official_pv/engine/fonts/NotoSansSC_wght_.ttf",
                "C:/Windows/Fonts/NotoSansSC-VF.ttf"],
    # 日文歌用同族的 JP 版（假名 / 汉字字形是日文写法，与 SC 同设计，视觉上是一套字）
    "sans_jp": ["official_pv/engine/fonts/NotoSansJP_wght_.ttf",
                "C:/Windows/Fonts/NotoSansJP-VF.ttf"],
}

VS = """
#version 330
in vec2 in_pos;
in vec2 in_uv;
uniform vec2 u_res;
out vec2 v_uv;
out float v_sx;          // 设计坐标 x（像素）：给 FS_TXT 做「屏幕空间」的左缘软淡入
void main(){
    v_uv = in_uv;
    v_sx = in_pos.x;
    float x = in_pos.x / u_res.x * 2.0 - 1.0;
    float y = 1.0 - in_pos.y / u_res.y * 2.0;
    gl_Position = vec4(x, y, 0.0, 1.0);
}
"""

NV12 = """
vec3 nv12(sampler2D ty, sampler2D tuv, vec2 uv){
    float y = texture(ty, uv).r * 255.0;
    vec2 c = texture(tuv, uv).rg * 255.0;
    float Y = (y - 16.0) / 219.0;
    float U = (c.r - 128.0) / 224.0;
    float V = (c.g - 128.0) / 224.0;
    return clamp(vec3(Y + 1.5748 * V, Y - 0.1873 * U - 0.4681 * V, Y + 1.8556 * U), 0.0, 1.0);
}
"""

FS_SMALL = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D u_y, u_uv;
""" + NV12 + """
void main(){
    f_color = vec4(nv12(u_y, u_uv, v_uv), 1.0);
}
"""

FS_BG = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D u_bg;
uniform vec2 u_res;
uniform float u_frame;
void main(){
    vec2 p = v_uv;
    /* 环境色：96x54 的源帧上取 6 个固定点做平均，得到这一帧的整体色调。
       只取「色」不取「形」——人形亮斑不会被放大成鬼影（那是旧版的硬伤）。 */
    vec3 a = texture(u_bg, vec2(0.14, 0.18)).rgb;
    vec3 b = texture(u_bg, vec2(0.50, 0.22)).rgb;
    vec3 c1 = texture(u_bg, vec2(0.86, 0.18)).rgb;
    vec3 d = texture(u_bg, vec2(0.14, 0.82)).rgb;
    vec3 e = texture(u_bg, vec2(0.50, 0.78)).rgb;
    vec3 f = texture(u_bg, vec2(0.86, 0.82)).rgb;
    vec3 amb = (a + b + c1 + d + e + f) / 6.0;
    float lum = dot(amb, vec3(0.299, 0.587, 0.114));
    amb = clamp(mix(vec3(lum), amb, 1.45), vec3(0.0), vec3(1.0));   // 提饱和：色要站得住

    /* ---- 左半（人物面板底）：几乎全被视频盖住，只在溶解边与底部渐隐处露一点 */
    vec3 left = mix(vec3(0.023, 0.021, 0.026), amb * 0.10, 0.40);
    vec2 dd = (p - vec2(0.21, 0.90)) * vec2(1.45, 4.6);
    left *= (1.0 - 0.86 * exp(-dot(dd, dd) * 1.55));

    /* ---- 右栏：一块「有颜色的深场」，不是纯黑。
       色相全部来自这一帧的环境色 → 换场景自动换色，不需要人工配色。
       亮度归一到 0.132：白字压上去的对比度 ≈ 12:1，任何色相下都清楚。 */
    vec3 field = amb;
    float fl = max(0.02, dot(field, vec3(0.299, 0.587, 0.114)));
    field = field * (0.112 / fl);
    field *= mix(1.32, 0.54, smoothstep(0.0, 1.0, p.y));          // 上亮下暗

    // 接缝溢光：人物那侧的光往歌词区洇过来 → 两块区域不是「板上钉板」拼的
    float sx = (p.x - 0.565) / 0.20;
    field += amb * 0.10 * exp(-sx * sx);

    // 焦点柔光：活动行所在高度一团极宽的椭圆光，把当前行「托」起来
    vec2 g = (p - vec2(0.775, 0.325)) * vec2(1.0 / 0.34, 1.0 / 0.30);
    field += amb * 0.080 * exp(-dot(g, g) * 2.1);

    // 右缘收暗：画面要有边界，但不用画线
    field *= mix(1.0, 0.74, smoothstep(0.86, 1.0, p.x));

    vec3 col = mix(left, field, smoothstep(0.30, 0.62, p.x));

    // 极轻颗粒（材质层：让大面积渐变不出现色带）
    float hn = fract(sin(dot(v_uv * u_res, vec2(12.9898, 78.233)) + u_frame * 3.7) * 43758.5453);
    col += (hn - 0.5) * 0.010;
    f_color = vec4(col, 1.0);
}
"""

FS_VID = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D u_y, u_uv;
uniform vec4 u_crop;
uniform vec4 u_fade;
""" + NV12 + """
void main(){
    vec2 p = v_uv;
    vec2 uv = u_crop.xy + p * u_crop.zw;
    vec3 c = nv12(u_y, u_uv, uv);
    // 右缘溶解 → 交给背景，不是硬边
    float a = 1.0 - smoothstep(u_fade.x, u_fade.y, p.x);
    // 底部渐隐到背景色（给底部标题让位）
    float b = smoothstep(u_fade.z, u_fade.w, p.y);
    a *= (1.0 - b * 0.92);
    f_color = vec4(c, a);
}
"""

FS_TXT = """
#version 330
in vec2 v_uv;
in float v_sx;
out vec4 f_color;
uniform sampler2D u_tex;
uniform sampler2D u_amb;   // 96x54 环境帧：墨色的色相从这里取
uniform vec4 u_color;
uniform vec4 u_fill;   // x: 填充像素（纹理空间，<0 关闭）; y: 过渡宽; z: 未唱系数
uniform vec2 u_fade_l; // 左缘软淡入区间（设计坐标，u_fade_l.y <= u_fade_l.x 即关闭）
uniform vec2 u_size;   // 纹理绘制尺寸（像素）
uniform float u_tint;  // 墨色向环境色相偏移的强度（0 = 纯白）
void main(){
    vec2 p = v_uv;
    float a = texture(u_tex, p).a;
    // 长行横向滚动时，滚出列框的字在 [x0, x1] 内化掉。作用在**屏幕横坐标**上，
    // 所以同一行滚到哪儿、放大多少倍，淡出边界都钉在列框上。
    if (u_fade_l.y > u_fade_l.x) {
        a *= smoothstep(u_fade_l.x, u_fade_l.y, v_sx);
    }
    if (u_fill.x >= 0.0) {
        float x = p.x * u_size.x;
        float m = smoothstep(u_fill.x - u_fill.y, u_fill.x + u_fill.y, x);
        // m=0 已唱（满亮）→ m=1 未唱（乘系数）。单遍完成，不再两遍叠加提亮。
        a *= mix(1.0, u_fill.z, m);
    }
    /* 墨色 = 近白 × 这一帧的色相。做法：把环境色按最大通道归一成「纯色相」，
       再只允许各通道向下偏移 → 亮度只降不升，色相跟着画面走，且永远不会变灰变脏。 */
    vec3 amb = texture(u_amb, vec2(0.52, 0.34)).rgb;
    float am = max(0.02, max(amb.r, max(amb.g, amb.b)));
    amb = amb / am;
    vec3 rgb = clamp(u_color.rgb * (vec3(1.0) + (amb - vec3(1.0)) * u_tint), 0.55, 1.0);
    f_color = vec4(rgb, a * u_color.a);
}
"""

FS_SOLID = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform vec4 u_color;
void main(){ f_color = u_color; }
"""

# 封面层：整张 RGBA 位图（PIL 里合成），只乘一个全局 alpha 做淡入淡出。
# 与 FS_TXT 的区别：文字层只读掩膜的 a 分量、颜色由 uniform 给；封面要保留 rgb。
FS_IMG = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D u_tex;
uniform float u_alpha;
void main(){
    vec4 c = texture(u_tex, v_uv);
    f_color = vec4(c.rgb, c.a * u_alpha);
}
"""

def log(*a):
    print(*a, flush=True)


# ---------------- 文字 ----------------
_font_cache: dict = {}


def font_path(kind: str) -> str | None:
    for c in FSRC.get(kind, []):
        p = c if os.path.isabs(c) else os.path.join(ROOT, c)
        if os.path.exists(p):
            return p
    return None


def get_font(kind: str, px: float, weight: int = 400):
    key = (kind, round(px, 1), weight)
    if key in _font_cache:
        return _font_cache[key]
    path = font_path(kind)
    f = None
    if path:
        try:
            f = ImageFont.truetype(path, int(round(px)))
            try:
                f.set_variation_by_axes([weight])
            except Exception:
                pass
        except Exception:
            f = None
    if f is None:
        f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def text_mask(text: str, kind: str, px: float, weight: int = 400,
              pad: float = 10.0, ss: float = 2.0, blur: float = 0.0):
    """生成 alpha 掩膜。返回 (PIL L 图, 设计宽, 设计高, 逐字 x[设计像素])。"""
    fpx = max(4, int(round(px * ss)))
    ppad = pad * ss
    font = get_font(kind, fpx, weight)
    dummy = ImageDraw.Draw(Image.new("L", (4, 4)))
    try:
        bbox = dummy.textbbox((0, 0), text, font=font)
    except Exception:
        bbox = (0, 0, int(fpx * len(text) * 0.6), fpx)
    w = max(1.0, bbox[2] - bbox[0] + ppad * 2)
    h = max(1.0, bbox[3] - bbox[1] + ppad * 2)
    img = Image.new("L", (max(1, int(round(w))), max(1, int(round(h)))), 0)
    d = ImageDraw.Draw(img)
    ox, oy = ppad - bbox[0], ppad - bbox[1]
    d.text((ox, oy), text, font=font, fill=255)
    if blur > 0.01:
        img = img.filter(ImageFilter.GaussianBlur(blur * ss))
    xs = []
    for i in range(len(text)):
        try:
            pre = float(font.getlength(text[:i]))
        except Exception:
            pre = i * fpx * 0.6
        xs.append((ox + pre) / ss)
    return img, img.size[0] / ss, img.size[1] / ss, xs


# ---------------- LRC ----------------
LRC_RE = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")
WORD_RE = re.compile(r"<(\d+):(\d+(?:[.:]\d+)?)>")


def parse_lrc(text: str) -> list[dict]:
    """解析 LRC，行内 <mm:ss.xx> 逐字标签转成 (秒) 列表。与 gpu_render 同口径。"""
    out = []
    for raw in text.splitlines():
        m = LRC_RE.search(raw)
        if not m:
            continue
        t = int(m.group(1)) * 60 + float(m.group(2).replace(":", "."))
        body = LRC_RE.sub("", raw).strip()
        words = []
        plain = []
        pos = 0
        for wm in WORD_RE.finditer(body):
            if wm.start() > pos:
                seg = body[pos:wm.start()]
                plain.append(seg)
                for _ in seg:
                    # 首个逐字标签之前的字按「行首时刻」算，不能留 None（min/max 会炸）
                    words.append(words[-1] if words else t)
            wt = int(wm.group(1)) * 60 + float(wm.group(2).replace(":", "."))
            words.append(wt)
            pos = wm.end()
        if pos < len(body):
            seg = body[pos:]
            plain.append(seg)
            for _ in seg:
                words.append(words[-1] if words else t)
        txt = "".join(plain).strip()
        if not txt:
            continue
        # 去掉逐字标签后可能残留的空白差异
        if words and len(words) != len(txt):
            words = []
        out.append({"t": t, "text": txt, "words": words})
    out.sort(key=lambda x: x["t"])
    return out


def body_lang(text: str) -> str:
    kana = sum(1 for c in text if "\u3040" <= c <= "\u30ff")
    han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    if kana >= 3:
        return "ja"
    if han >= 3:
        return "zh"
    if latin:
        return "en"
    return "ja"


class LiveRenderer:
    """一次构造，逐帧渲染，最后交给 NVENC。"""

    def __init__(self, job: dict, out_path: str):
        self.job = job
        self.out = out_path
        self.res = int(job.get("res") or 2)
        self.W = int(SW * self.res)
        self.H = int(SH * self.res)
        self.fps = int(job.get("fps") or 60)
        self.bitrate = int(job.get("bitrate") or 18_000_000)
        self.dt = 1.0 / self.fps
        self.head_pad = float(job.get("head_pad") or 0.0)
        # 片头封面：独立画面，**不参与**歌曲时间轴。帧序号前 cover_frames 帧画封面，
        # 之后所有时间换算都用 (i - cover_frames)/fps —— 这样歌词轴、head_pad、
        # 面部轨迹三处既有口径一行都不用改。
        self.cover_sec = float(job.get("cover_sec") or 0.0)
        self.cover_frames = int(round(self.cover_sec * self.fps))
        self.duration = float(job.get("duration") or 0.0) + self.head_pad
        self.cut_start = float(job.get("cutStart") or 0.0)
        self.src = job.get("source") or ""
        self.preset = str(job.get("nvencPreset") or job.get("preset") or "p5")
        if not re.match(r"^p[1-7]$", self.preset):
            # song_cutter 的 job.preset 是 libx264 的 fast/medium，nv 侧不认，退回默认档
            self.preset = "p5"
        self.track = None
        self.lines: list[dict] = []
        self.scrollY = 0.0
        self.frame_no = 0
        self._prev_idx = -99
        self._crop_x = None
        self._crop_y = None
        self._clock: dict = {}          # 时间码纹理缓存（每帧都在变，不能每次新建纹理）

    # ------------------------------------------------------------------
    def prepare(self):
        j = self.job
        lrc_path = j.get("lrc")
        if not lrc_path or not os.path.exists(lrc_path):
            raise SystemExit("缺少歌词文件：%s" % lrc_path)
        with io.open(lrc_path, "r", encoding="utf-8") as f:
            lrc_text = f.read()
        self.lines = parse_lrc(lrc_text)
        if not self.lines:
            raise SystemExit("歌词为空：%s" % lrc_path)

        # 译文（可选，缺失不影响原文）
        self.trans = {}
        tr = j.get("trans")
        if tr and os.path.exists(tr):
            with io.open(tr, "r", encoding="utf-8") as f:
                for it in parse_lrc(f.read()):
                    self.trans[round(it["t"] * 100)] = it["text"]

        main_text = "".join(l["text"] for l in self.lines)
        lang = body_lang(main_text)
        self.serif = "serif_sc" if lang == "zh" else "serif_jp"   # 片头封面（海报语言，保留衬线）
        self.sans = "sans_sc"
        # 歌词：粗体无衬线（A 方案）。日文歌用 JP 字形，中文歌用 SC 字形，同族同设计。
        self.body_font = "sans_sc" if lang == "zh" else "sans_jp"
        self.trans_font = "sans_sc"
        # 行高：有译文才需要 104（原文 + 译文 + 呼吸），纯原文用 80 更紧凑
        self.LH = LH if self.trans else LH_NOTRANS
        log("[live] %d 行歌词 · 语言 %s · 译文 %d 行 · %.2fs"
            % (len(self.lines), lang, len(self.trans), self.duration))

        # 行时长（含长间奏兜底，与旧版一致）
        for i, ln in enumerate(self.lines):
            nxt = self.lines[i + 1]["t"] if i + 1 < len(self.lines) else self.duration
            est = 0.42 * len(ln["text"]) + 0.9
            ln["end"] = min(nxt, ln["t"] + max(est * 1.6, 1.2), self.duration)

        # 轨迹
        tk = j.get("track")
        if tk and os.path.exists(tk):
            with io.open(tk, "r", encoding="utf-8") as f:
                self.track = json.load(f)
            log("[live] 面部轨迹 %d 点（%s）"
                % (len(self.track.get("frames") or []), self.track.get("anchor")))

        self._build_gl()
        self._build_sprites()
        if self.cover_frames:
            self._build_cover()
        self._open_pipes()

    # ------------------------------------------------------------------
    def _build_gl(self):
        self.ctx = moderngl.create_standalone_context(require=330)
        log("[live] %s | %s" % (self.ctx.info["GL_RENDERER"], self.ctx.info["GL_VERSION"]))
        self.p_bg = self.ctx.program(vertex_shader=VS, fragment_shader=FS_BG)
        self.p_small = self.ctx.program(vertex_shader=VS, fragment_shader=FS_SMALL)
        self.p_vid = self.ctx.program(vertex_shader=VS, fragment_shader=FS_VID)
        self.p_txt = self.ctx.program(vertex_shader=VS, fragment_shader=FS_TXT)
        self.p_sol = self.ctx.program(vertex_shader=VS, fragment_shader=FS_SOLID)
        self.p_img = self.ctx.program(vertex_shader=VS, fragment_shader=FS_IMG)

        self.vbo = {}
        self.vao = {}
        for name, prog in (("bg", self.p_bg), ("vid", self.p_vid),
                           ("txt", self.p_txt), ("sol", self.p_sol),
                           ("img", self.p_img), ("small", self.p_small)):
            buf = self.ctx.buffer(reserve=6 * 4 * 4, dynamic=True)
            self.vbo[name] = buf
            # FS_SOLID 不采样纹理，in_uv 会被着色器优化掉，不能按 4 分量绑定
            fmt = "2f" if name == "sol" else "2f 2f"
            attribs = ["in_pos"] if name == "sol" else ["in_pos", "in_uv"]
            self.vao[name] = self.ctx.vertex_array(prog, [(buf, fmt, *attribs)])

        self.tex_y = self.ctx.texture((1920, 1080), 1)
        self.tex_uv = self.ctx.texture((960, 540), 2)
        for t in (self.tex_y, self.tex_uv):
            t.filter = (moderngl.LINEAR, moderngl.LINEAR)
            t.repeat_x = False
            t.repeat_y = False

        # u_res 是顶点着色器把设计坐标映射到 NDC 的唯一依据，四层都必须设；
        # 漏设会让 in_pos/0 变成 inf，图层静默消失（排查了半天）。
        for prog in (self.p_bg, self.p_vid, self.p_txt, self.p_sol, self.p_img, self.p_small):
            if "u_res" in prog:
                prog["u_res"].value = (SW, SH)      # 设计坐标，与输出分辨率解耦

        self.tex_env = self.ctx.texture((96, 54), 4)
        self.tex_env.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.tex_env.repeat_x = False
        self.tex_env.repeat_y = False
        self.fbo_env = self.ctx.framebuffer(color_attachments=[self.tex_env])

        self.tex_out = self.ctx.texture((self.W, self.H), 4)
        self.fbo = self.ctx.framebuffer(color_attachments=[self.tex_out])
        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA


    def _upload(self, mask: Image.Image):
        rgba = Image.merge("RGBA", (Image.new("L", mask.size, 255),) * 3 + (mask,))
        tex = self.ctx.texture(mask.size, 4, rgba.tobytes())
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        tex.repeat_x = False
        tex.repeat_y = False
        return tex

    def _build_sprites(self):
        """每行：原文（含 5 档模糊）+ 译文。逐字 x 供卡拉 OK 填充。

        字体迁移自 A 方案：**粗体无衬线**（Noto Sans / 700），日文歌走 JP 版、
        中文歌走 SC 版（同设计的一族字，字形语言一致）。译文用同族 500，
        只靠字号与字重分主次，不换字族 —— 这是「原文与译文统一风格」的落点。
        """
        t0 = time.time()
        self.sprites = []
        blur_levels = (0.0, 3.0, 6.0, 9.0, 12.0)
        for i, ln in enumerate(self.lines):
            sp = {"i": i, "t": ln["t"], "end": ln["end"], "words": ln["words"],
                  "xs": None, "w": 0, "h": 0, "blurs": []}
            img, w, h, xs = text_mask(ln["text"], self.body_font, BASE_PX,
                                      BASE_WEIGHT, pad=16, ss=2.0)
            sp["w"], sp["h"], sp["xs"] = w, h, xs
            sp["blurs"] = [self._upload(img if b <= 0.01 else
                                        img.filter(ImageFilter.GaussianBlur(b * 2.0)))
                           for b in blur_levels]
            sp["base"] = sp["blurs"][0]
            tr = self.trans.get(round(ln["t"] * 100))
            if tr:
                # 译文与原文**同一套字号阶梯**：5 档模糊各烘一张，绘制时按原文当前的
                # 模糊档位取同一张 —— 这就是「同步模糊」的实现方式（不是叠加滤镜）。
                ti, tw, th, _ = text_mask(tr, self.trans_font, TRANS_PX, TRANS_WEIGHT,
                                          pad=12, ss=2.0)
                sp["tr"] = self._upload(ti)
                sp["tr_blurs"] = [self._upload(ti if b <= 0.01 else
                                               ti.filter(ImageFilter.GaussianBlur(b * 2.0)))
                                  for b in blur_levels]
                sp["tw"], sp["th"] = tw, th
            self.sprites.append(sp)
        # 固定文案
        # 底栏歌名与歌词同一套粗体，整片只有一个「字体声音」
        self.tex_title = self._label(self.job.get("title") or "", self.body_font, TITLE_PX, 800, 16)
        self.tex_artist = self._label(self._artist_line(), self.sans, LABEL_PX, 300, 8)
        self.tex_vocal = self._label(self._vocal_line(), self.sans, LABEL_PX, 300, 8)
        self.tex_clock = self._label("00:00", self.sans, CLOCK_PX, 400, 8)
        self.tex_total = self._label(self._fmt_dur(self.duration), self.sans, CLOCK_PX * 0.66, 300, 8)
        log("[live] 歌词纹理 %d 行（%.1fs）" % (len(self.sprites), time.time() - t0))

    def _label(self, text, kind, px, weight, pad):
        if not text:
            return None
        img, w, h, _ = text_mask(text, kind, px, weight, pad=pad, ss=2.0)
        return {"tex": self._upload(img), "w": w, "h": h}

    # ---------------- 片头封面 ----------------
    def _fit_px(self, text, kind, weight, max_w, track=0.0):
        """二分求字号：让整行宽度正好吃满 max_w（设计像素）。换歌换标题都自动适配。"""
        lo, hi = 8.0, 900.0
        for _ in range(26):
            mid = (lo + hi) * 0.5
            f = get_font(kind, mid * self.res, weight)
            w = sum(f.getlength(c) for c in text) + track * mid * max(0, len(text) - 1)
            if w > max_w * self.res:
                hi = mid
            else:
                lo = mid
        return lo

    def _build_cover(self):
        """封面 = 两张整帧 RGBA（底 + 字），各上传一次；每帧只画两个 quad。

        设计取向：**不是信息卡，是海报**。歌名吃满左侧 80% 宽度当主标题，
        右上一枚几乎看不见的「月」当视觉落点，其余全是留白与发丝线。
        裁切标记、印刷边线是版式语言，不是装饰堆砌。
        """
        t0 = time.time()
        r = self.res
        W, H = self.W, self.H
        rng = np.random.default_rng(20261128)
        xs = np.linspace(0.0, 1.0, W, dtype=np.float32)[None, :]
        ys = np.linspace(0.0, 1.0, H, dtype=np.float32)[:, None]

        # ---- 底：近黑，上冷下暖（黎明前的地平线），一团暖光 + 一枚"月"
        img = np.empty((H, W, 3), np.float32)
        img[:] = np.array([6, 6, 10], np.float32)
        img += (np.array([11, 10, 17], np.float32) - np.array([6, 6, 10], np.float32)) * (1.0 - ys)[..., None]
        img += (np.array([21, 13, 18], np.float32) - np.array([6, 6, 10], np.float32)) * (ys ** 1.7)[..., None]
        # 地平线上的一团暖光（很轻，只为了不「死黑」）
        g = np.exp(-((((xs - 0.66) / 0.55) ** 2 + ((ys - 0.88) / 0.30) ** 2) * 2.4))[..., None]
        img += g * np.array([232, 164, 182], np.float32) * (52.0 / 255.0)
        # 左上一点冷光，和暖光对着压出层次
        g = np.exp(-((((xs - 0.14) / 0.38) ** 2 + ((ys - 0.06) / 0.30) ** 2) * 3.0))[..., None]
        img += g * np.array([110, 140, 205], np.float32) * (20.0 / 255.0)
        # 右上一点极淡的冷光（不做圆盘：硬边圆在纯黑上会像脏点，只用散射）
        md = np.sqrt((xs - 0.80) ** 2 + ((ys - 0.22) * (SW / SH)) ** 2)
        img += np.exp(-(md / 0.18) ** 2)[..., None] * np.array([248, 246, 242], np.float32) * 0.055
        # 颗粒 + 暗角
        img += rng.normal(0.0, 2.3, (H, W, 1)).astype(np.float32)
        vg = np.exp(-(((xs - 0.5) ** 2) * 2.4 + ((ys - 0.5) ** 2) * 2.8))
        img *= (0.55 + 0.45 * vg)[..., None]
        bg = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB").convert("RGBA")

        # ---- 字层：主标题 + 双语副题 + 底栏 + 印刷标记
        ty = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(ty, "RGBA")
        ink = (242, 236, 230)
        acc = (232, 164, 182)
        ML, MR = 132.0, 1788.0                     # 版心左右边线
        title = (self.job.get("title") or "").strip()

        def tracked(text, x, y, px, weight, kind, color, track=0.0):
            f = get_font(kind, px * r, weight)
            cx = x * r
            for ch in text:
                d.text((cx, y * r), ch, font=f, fill=color, anchor="la")
                cx += f.getlength(ch) + track * px * r
            return cx

        def tw(text, px, weight, kind, track=0.0):
            f = get_font(kind, px * r, weight)
            return sum(f.getlength(c) for c in text) + track * px * r * max(0, len(text) - 1)

        def rect(x0, y0, x1, y1, color):
            d.rectangle([min(x0, x1) * r, min(y0, y1) * r,
                         max(x0, x1) * r, max(y0, y1) * r],
                        fill=color)

        # 印刷裁切标记（四角，极细）
        cm = 30.0
        for cx, sx in ((46.0, 1), (SW - 46.0, -1)):
            for cy, sy in ((46.0, 1), (SH - 46.0, -1)):
                rect(cx, cy, cx + sx * cm, cy + 0.5, (242, 236, 230, 58))
                rect(cx, cy, cx + 0.5, cy + sy * cm, (242, 236, 230, 58))
        # 右侧印刷边线 + 顶部横向发丝线
        rect(MR, 96, MR + 0.5, 984, (242, 236, 230, 26))
        rect(ML, 150, MR - 40, 150.5, (242, 236, 230, 42))
        # 品牌小方块 + 拉丁标题行
        rect(ML, 104, ML + 9, 113, acc + (235,))
        latin = (self.job.get("romaji") or "").strip().upper()
        if not latin:
            latin = "MUSIC VIDEO"
        tracked(latin, ML + 20, 99.0, 19.0, 400, "sans_sc", (242, 236, 230, 205), track=0.30)

        # 主标题：**拆两行**，左齐 + 第二行缩进。行距按**墨迹**算，不按 em 算：
        # 这个字体的墨迹从 ascender 下方 0.35em 才起笔，按 em 排会压在一起（踩过）。
        tpx = 0.0
        if title:
            n = len(title)
            cut = max(1, int(round(n * 0.40))) if n >= 4 else n   # またあした → また/あした
            l1, l2 = title[:cut], title[cut:]
            px = min(self._fit_px(l2 or l1, self.serif, 900, 780.0, track=-0.02), 280.0)
            tpx = px

            def ink_box(s):
                f = get_font(self.serif, px * r, 900)
                tops, bots = [], []
                for ch in s:
                    bb = f.getbbox(ch)
                    tops.append(bb[1] / r)
                    bots.append(bb[3] / r)
                return min(tops), max(bots)

            t1, b1 = ink_box(l1)
            ty_top = 214.0
            tracked(l1, ML - 2.0, ty_top, px, 900, self.serif, ink + (252,), track=-0.02)
            block_b = ty_top + b1
            if l2:
                t2, b2 = ink_box(l2)
                y2 = ty_top + b1 + (px * 0.055 - t2) * 1.0   # 上一行墨底 → 下一行墨顶 + 极小间隙
                tracked(l2, ML + 34.0, y2, px, 900, self.serif, ink + (252,), track=-0.02)
                block_b = y2 + b2
            # 中文译名：小一号、字距拉开，左侧一枚品牌色小方块做行首符
            zh = (self.job.get("title_zh") or "").strip()
            if zh:
                zy = block_b + 40.0
                rect(ML, zy + 16.0, ML + 7, zy + 23.0, acc + (235,))
                tracked(zh, ML + 24.0, zy, 44.0, 300, "serif_sc",
                        (242, 236, 230, 198), track=0.34)
                block_b = zy + 52.0
            # 主标题右侧：一条横向发丝线（把右上角的空白"钉"住）
            rect(1000, 556, MR, 556.5, (242, 236, 230, 40))
            rect(1000, 553, 1006, 559, acc + (215,))
            # 超大号**空心**回声：整字一横排、从右缘切出去，只留轮廓。
            # 实心主标题在左下、空心回声在右上 → 画面成对角线，右半张不再是死白。
            if len(title) >= 3:
                gpx = min(self._fit_px(title, self.serif, 900, 1180.0, track=-0.02), 340.0)
                gf = get_font(self.serif, gpx * r, 900)
                gx = 1030.0 * r
                for ch in title:
                    d.text((gx, 176.0 * r), ch, font=gf, fill=(0, 0, 0, 0),
                           anchor="la", stroke_width=max(1, int(1.5 * r)),
                           stroke_fill=(242, 236, 230, 40))
                    gx += gf.getlength(ch) - 0.02 * gpx * r
            # 右下：日期 + 拉丁行（与左下 meta 对称）
            dt0 = (self.job.get("footerRight") or "").strip()
            if dt0:
                wdt = tw(dt0, 19.0, 400, "sans_sc", track=0.22) / r
                tracked(dt0, MR - wdt, 914.0, 19.0, 400, "sans_sc",
                        (242, 236, 230, 150), track=0.22)
            if latin:
                wl = tw(latin, 17.0, 400, "sans_sc", track=0.26) / r
                tracked(latin, MR - wl, 950.0, 17.0, 400, "sans_sc",
                        (242, 236, 230, 110), track=0.26)

        # 底栏：original / vocal / 日期
        a = (self.job.get("artist") or "").strip()
        tracked(("original  " + a) if a else "", ML, 916.0, 19.0, 400, "sans_sc",
                (242, 236, 230, 172), track=0.10)
        v = (self.job.get("vocal") or "").strip()
        dt = (self.job.get("footerRight") or "").strip()
        meta = "   ".join(x for x in ("vocal  " + v if v else "", dt) if x)
        tracked(meta, ML, 952.0, 19.0, 400, "sans_sc", (242, 236, 230, 118), track=0.10)

        def upload(im):
            tex = self.ctx.texture((W, H), 4, im.tobytes())
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = False
            tex.repeat_y = False
            return tex

        self.tex_cover_bg = upload(bg)
        self.tex_cover_ty = upload(ty)
        log("[live] 片头封面 %.2fs（%d 帧）· 主标题 %.0fpx（%.1fs）"
            % (self.cover_sec, self.cover_frames, tpx, time.time() - t0))

    # ---------------- 右侧版面装饰 ----------------
    def _draw_img(self, tex, x, y, w, h, alpha):
        p = self.p_img
        p["u_tex"].value = 0
        p["u_alpha"].value = float(alpha)
        tex.use(0)
        self._quad("img", x, y, w, h)

    def _artist_line(self):
        a = (self.job.get("artist") or "").strip()
        return ("original · " + a) if a else ""

    def _vocal_line(self):
        v = (self.job.get("vocal") or "").strip()
        d = (self.job.get("footerRight") or "").strip()
        if v and d:
            return "vocal %s · %s" % (v, d)
        return ("vocal " + v) if v else d

    @staticmethod
    def _fmt_dur(s):
        s = max(0, int(round(s)))
        return "%02d:%02d" % (s // 60, s % 60)

    # ------------------------------------------------------------------
    def _open_pipes(self):
        ff = os.environ.get("FFMPEG_BIN") or "ffmpeg"
        if not os.path.exists(ff):
            ff = "ffmpeg"
        self.ff = ff
        dur = self.duration + 2.0
        self.dec = subprocess.Popen(
            [ff, "-v", "error", "-ss", "%.3f" % self.cut_start, "-t", "%.3f" % dur,
             "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", self.src,
             "-vf", "scale_cuda=1920:1080,fps=%d,hwdownload,format=nv12" % self.fps,
             "-pix_fmt", "nv12", "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE)
        log("[live] 解码：NVDEC 1920x1080@%d（片段 %.1fs）" % (self.fps, dur))
        self.enc = subprocess.Popen(
            [ff, "-y", "-v", "error", "-stats",
             "-f", "rawvideo", "-pix_fmt", "rgba",
             "-s", "%dx%d" % (self.W, self.H),
             "-r", str(self.fps), "-i", "pipe:0",
             "-vf", "vflip",
             "-c:v", "h264_nvenc", "-preset", self.preset, "-tune", "hq",
             "-b:v", str(self.bitrate), "-maxrate", str(int(self.bitrate * 1.03)),
             "-bufsize", str(int(self.bitrate * 2)), "-rc", "cbr",
             "-profile:v", "high", "-level", "5.2", "-pix_fmt", "yuv420p",
             "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
             "-color_range", "tv",
             "-bsf:v", "h264_metadata=colour_primaries=1:transfer_characteristics=1:"
             "matrix_coefficients=1",
             "-movflags", "+faststart", "-an", self.out],
            stdin=subprocess.PIPE, bufsize=0)
        log("[live] 编码：NVENC %dx%d@%d %s %.1f Mbps"
            % (self.W, self.H, self.fps, self.preset, self.bitrate / 1e6))
        self.fsize = 1920 * 1080
        self.csize = 960 * 540 * 2
        # 回读 → 写编码管道 必须**异步**：
        # 实测一帧 = 解码读 3.4ms + 绘制/回读 4.4ms + 写管道 11.0ms。
        # 写管道是同步阻塞的（64KB 内核缓冲，一帧 33MB 要写 540 次），串起来就是 15ms/帧；
        # 开一条写线程与渲染重叠后，整帧开销降到 max(11, 7.8) ≈ 11ms（66 → 90 fps）。
        # 三缓冲：渲染写一块、编码读一块、手里备一块，谁都不等谁。
        self._bufs = [bytearray(self._frame_bytes()) for _ in range(int(os.environ.get("LIVE_BUFS") or 3))]
        self._free = queue.Queue()
        for b in self._bufs:
            self._free.put(b)
        self._work = queue.Queue(maxsize=2)
        self._enc_err = None
        self._writer = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer.start()

    def _frame_bytes(self):
        return self.W * self.H * 4

    def _writer_loop(self):
        while True:
            buf = self._work.get()
            if buf is None:
                return
            try:
                self.enc.stdin.write(buf)
            except (BrokenPipeError, ValueError, OSError) as e:
                self._enc_err = e
                self._free.put(buf)
                return
            self._free.put(buf)

    # ------------------------------------------------------------------
    def _track_xy(self, t):
        """面部锚点（源视频 1920 坐标）。线性插值 + 目标位置平滑。"""
        if not self.track:
            return 960.0, 430.0
        fr = self.track.get("frames") or []
        if not fr:
            return 960.0, 430.0
        k = self.track.get("proc_w") or 1280.0
        scale = 1920.0 / k
        tf = t - self.head_pad                     # 轨迹时间轴 = cut 起点（LRC 含 pad）
        tf = max(0.0, tf)
        n = len(fr)
        i = min(n - 1, max(0, int(tf * (self.track.get("fps") or 4.0))))
        f = fr[i]
        return f.get("sx", f["x"]) * scale, f.get("sy", f["y"]) * scale

    def _update_motion(self, t):
        """旧版 Apple Music 动效状态机（参数逐条对齐 gpu_render._update_motion）。"""
        n = len(self.sprites)
        if n == 0:
            return -1
        idx = -1
        for i, sp in enumerate(self.sprites):
            if t >= sp["t"]:
                idx = i
            else:
                break
        raw = idx
        if idx < 0:
            idx = 0
        target = idx * self.LH - (FOCAL_Y - 0.0)
        self.scrollY += (target - self.scrollY) * min(1.0, self.dt * 5.5)
        for i, sp in enumerate(self.sprites):
            tg = 1.0 if i == raw else 0.0
            k = 9.0 if tg > sp.get("act", 0.0) else 4.2
            sp["act"] = sp.get("act", 0.0) + (tg - sp.get("act", 0.0)) * min(1.0, self.dt * k)
            if raw < 0:
                # 开唱前：第一行先亮起来（等它开口），其余按距离压在下面。
                # A 方案的稳态是「当前行 1.0」，这里给 0.70——比任何已唱行都亮，
                # 又不到满亮，等第一句真的唱出来再逐字点亮。
                base = 0.70 if i == 0 else 0.20 + 0.30 * math.exp(-i / 1.7)
            elif i < idx:
                base = 0.30
            elif i == raw:
                base = 1.0
            else:
                d = i - idx
                base = 0.20 + 0.30 * math.exp(-(d - 1) / 1.7)
            sp["bs"] = sp.get("bs", base)
            sp["bs"] += (base - sp["bs"]) * min(1.0, self.dt * 9.0)
            bt = 0.0 if i == raw else (0.0 if i < idx else min(12.0, 3.0 + 3.0 * max(0, abs(i - idx) - 1)))
            sp["bl"] = sp.get("bl", bt)
            sp["bl"] += (bt - sp["bl"]) * min(1.0, self.dt * (12.0 if i == raw else 5.5))
        return raw

    def _fill_px(self, sp, t):
        """已唱到的纹理像素 x（与 sp["xs"] 同系）。"""
        xs = sp["xs"]
        if not xs:
            return 0.0
        n = len(xs)
        words = [w for w in (sp.get("words") or [])]
        if words and len(words) == n and None not in words and max(words) - min(words) > 0.05:
            j = 0
            for k in range(n):
                if words[k] is not None and words[k] <= t:
                    j = k
                elif words[k] is not None:
                    break
            T0 = words[j]
            T1 = words[j + 1] if j + 1 < n and words[j + 1] is not None else sp["end"]
            f = 0.0 if T1 <= T0 else max(0.0, min(1.0, (t - T0) / (T1 - T0)))
            x0 = xs[j]
            x1 = xs[j + 1] if j + 1 < n else xs[j] + BASE_PX * 0.6
            return x0 + (x1 - x0) * f
        s, e = sp["t"], sp["end"]
        f = 0.0 if e <= s else max(0.0, min(1.0, (t - s) / (e - s)))
        return xs[0] + (xs[-1] + BASE_PX * 0.6 - xs[0]) * f

    # ------------------------------------------------------------------
    def _quad(self, name, x, y, w, h):
        if name == "sol":
            arr = np.array([x, y, x + w, y, x, y + h,
                            x, y + h, x + w, y, x + w, y + h], dtype="f4")
        else:
            arr = np.array([x, y, 0, 0, x + w, y, 1, 0, x, y + h, 0, 1,
                            x, y + h, 0, 1, x + w, y, 1, 0, x + w, y + h, 1, 1], dtype="f4")
        self.vbo[name].write(arr.tobytes())
        self.vao[name].render(moderngl.TRIANGLES)

    def _draw_tex(self, tex_info, x, y, w, h, color, fill=None, fadex=None, tint=None):
        p = self.p_txt
        p["u_tex"].value = 0
        p["u_amb"].value = 1
        tex_info["tex"].use(0)
        self.tex_env.use(1)          # 墨色色相取自这一帧的环境色（零回读，纯 GPU 取色）
        p["u_tint"].value = float(INK_TINT if tint is None else tint)
        p["u_color"].value = color
        if fill is None:
            p["u_fill"].value = (-1.0, 1.0, 1.0, 0.0)
        else:
            p["u_fill"].value = (fill[0], fill[1], fill[2], 0.0)
        p["u_fade_l"].value = fadex if fadex else (0.0, 0.0)
        p["u_size"].value = (w, h)
        self._quad("txt", x, y, w, h)

    def render_frame(self, i):
        self.frame_no = i
        # 片头封面：前 cover_frames 帧是独立画面，不读解码管道、不动歌词状态机。
        # 用「帧序号整体后移」而不是给时间加偏移，是为了让歌词轴 / head_pad /
        # 面部轨迹三处既有口径一行都不用改（偏移量只在这里出现一次）。
        if self.cover_frames and i < self.cover_frames:
            self._draw_cover(i)
            self._emit()
            return
        t = (i - self.cover_frames) * self.dt
        # 源时刻 = 音频时刻 − head_pad。**head_pad 期间不许拉解码帧**：那几帧若被读走，
        # 画面就会比音频提前 head_pad 秒，整片音画一起偏（歌词轴是对的，画面早了）。
        # 正确做法是把切点第一帧冻结在屏幕上，等进正片再开始拉。
        pad = self.head_pad > 1e-6 and (t - self.head_pad) < -1e-6
        if (not pad) or not getattr(self, "_last_ok", False):
            y = self.dec.stdout.read(self.fsize)
            c = self.dec.stdout.read(self.csize)
            if len(y) == self.fsize and len(c) == self.csize:
                self.tex_y.write(y)
                self.tex_uv.write(c)
                self._last_ok = True
            elif not getattr(self, "_last_ok", False):
                # 源第一帧就取不到（切点越界）：给黑帧，不崩
                self.tex_y.write(bytes(self.fsize))
                self.tex_uv.write(bytes(self.csize))
            # 否则：源已到尾，复用上一帧纹理（尾帧定格比黑屏可接受得多）

        idx = self._update_motion(t)
        ctx = self.ctx
        self.fbo.use()
        ctx.clear(*BG, 1.0)

        # ---- 环境底（源帧降到 96x54，只留色与光）
        self.fbo_env.use()
        ctx.clear(0, 0, 0, 1)
        ps = self.p_small
        ps["u_y"].value = 0
        ps["u_uv"].value = 1
        self.tex_y.use(0)
        self.tex_uv.use(1)
        self._quad("small", 0, 0, SW, SH)

        self.fbo.use()
        p = self.p_bg
        p["u_bg"].value = 0
        p["u_res"].value = (SW, SH)
        p["u_frame"].value = float(i)
        self.tex_env.use(0)
        self._quad("bg", 0, 0, SW, SH)

        # ---- 人物：面部锚点 → 裁切窗口
        ex, ey = self._track_xy(t)
        crop_h = 980.0
        crop_w = crop_h * (PANEL_W / SH)
        want_x = ex - crop_w * 0.52
        want_y = ey - crop_h * 0.30
        want_x = max(0.0, min(SW - crop_w, want_x))
        want_y = max(0.0, min(SH - crop_h, want_y))
        if self._crop_x is None:
            self._crop_x, self._crop_y = want_x, want_y
        else:
            a = min(1.0, self.dt * 2.2)
            self._crop_x += (want_x - self._crop_x) * a
            self._crop_y += (want_y - self._crop_y) * a
        p = self.p_vid
        p["u_y"].value = 0
        p["u_uv"].value = 1
        p["u_crop"].value = (self._crop_x / SW, self._crop_y / SH,
                             crop_w / SW, crop_h / SH)
        p["u_fade"].value = (0.58, 1.0, 0.74, 0.98)
        self.tex_y.use(0)
        self.tex_uv.use(1)
        self._quad("vid", PANEL_X, 0, PANEL_W, SH)

        # ---- 歌词
        self._draw_lyrics(t, idx)

        # ---- 底部
        self._draw_footer(t)

        # 封面 → 正片：封面末尾淡黑收尾，正片头 0.28s 再从黑里「浮」出来。
        # 两边各自淡，中间留一帧纯黑，转场才不像硬切（实测直接硬切有一下"跳"）。
        if self.cover_frames:
            k = t / 0.28
            if k < 1.0:
                self.p_sol["u_color"].value = (0.0, 0.0, 0.0, 1.0 - _ease(k))
                self._quad("sol", 0, 0, SW, SH)

        self._emit()

    def _emit(self):
        """回读到空闲缓冲 → 交给写线程（不阻塞渲染）。"""
        buf = self._free.get()
        if self._enc_err is not None:
            raise RuntimeError("编码管道已断开：%r" % (self._enc_err,))
        self.fbo.use()
        self.fbo.read_into(buf, components=4, dtype="f1")
        self._work.put(buf)

    def _draw_cover(self, i):
        t = i * self.dt
        self.fbo.use()
        self.ctx.clear(0.0, 0.0, 0.0, 1.0)
        # 封面**第一帧就是满亮**（不做淡入）：它是整支片子给观众看的第一眼，
        # 淡入会让第 0 帧变成纯黑 —— 缩略图/预览全都拿不到封面。
        # 收尾 0.60s 平滑落到黑，再接正片头的 0.28s 淡入，中间一两帧纯黑即"短暂黑场"。
        s = max(0.0, min(1.0, (self.cover_sec - t) / 0.60))
        a = s * s * (3.0 - 2.0 * s)
        p = i / max(1, self.cover_frames - 1)
        # 两层用不同速率推近（底 3.0%、字 1.6%），产生轻微视差 → 不是一张贴在镜头前的图
        s1 = 1.0 + 0.030 * p
        self._draw_img(self.tex_cover_bg, -(SW * (s1 - 1.0)) * 0.5, -(SH * (s1 - 1.0)) * 0.5,
                       SW * s1, SH * s1, a)
        s2 = 1.0 + 0.016 * p
        dy = 16.0 * (1.0 - _ease(t / 0.62))
        self._draw_img(self.tex_cover_ty, -(SW * (s2 - 1.0)) * 0.5,
                       -(SH * (s2 - 1.0)) * 0.5 + dy, SW * s2, SH * s2, a)

    def _draw_lyrics(self, t, idx):
        """歌词绘制：A 方案的粗体 + Apple Music 滚动，重新排版到右侧竖栏。

        版式只有三件事：**字**（粗体、大字号、紧行距）、**色**（墨色跟着画面色相走）、
        **光**（背景里那团焦点柔光）。没有任何框线、圆环、点阵或空心大字 ——
        空间感来自字号与留白，不来自装饰。
        """
        fade = LYR_FADE
        rows = []
        for i, sp in enumerate(self.sprites):
            ytop = i * self.LH - self.scrollY
            if ytop > LYR_BOT + self.LH or ytop + self.LH < LYR_TOP - self.LH:
                continue
            a = sp.get("act", 0.0)
            bs = sp.get("bs", 0.0)
            bl = sp.get("bl", 0.0)
            # 视口上下沿淡出：顶边收得急（不让字溢出到画面上部），底边留得缓。
            edge = min(_sstep(ytop, LYR_TOP - 8.0, LYR_TOP + 86.0),
                       _sstep(ytop + self.LH, LYR_BOT + 22.0, LYR_BOT - 112.0))
            edge = max(0.0, min(1.0, edge))
            alpha = (bs + (1.0 - bs) * a) * edge
            if alpha < 0.012:
                continue
            rows.append((i, sp, ytop, alpha, a, bl, edge))

        # ---- 活动行标记：整块版式里唯一的图形元素。
        # 它不是装饰，是「指针」—— 双语对照下，一眼看出正在唱的是哪一行。
        for (i, sp, ytop, alpha, a, bl, edge) in rows:
            if i != idx:
                continue
            y0 = ytop + sp["h"] * 0.14
            h = sp["h"] * 0.62
            self.p_sol["u_color"].value = (ACCENT[0], ACCENT[1], ACCENT[2], 0.92 * edge)
            self._quad("sol", LYR_MARK_X, y0, 3.0, h)
            self.p_sol["u_color"].value = (ACCENT[0], ACCENT[1], ACCENT[2], 0.16 * edge)
            self._quad("sol", LYR_MARK_X, y0 + h, 3.0, 8.0)

        for (i, sp, ytop, alpha, a, bl, edge) in rows:
            # 聚焦放大 1.16×：以**原文底边**为锚向上生长 —— 译文位置不动，
            # 活动行只是「往上顶出来一点」，不会把下面那行挤跑。
            sc = 1.0 + 0.26 * a
            w = sp["w"] * sc
            ox = 0.0
            if i == idx and w > LYR_W:
                fw = self._fill_px(sp, t) * sc
                want = max(0.0, min(w - LYR_W, fw - LYR_W * 0.42))
                cur = sp.get("oxs", 0.0)
                cur += (want - cur) * min(1.0, self.dt * 3.2)
                sp["oxs"] = cur
                ox = cur
            elif sp.get("oxs"):
                cur = sp["oxs"]
                cur += (0.0 - cur) * min(1.0, self.dt * 3.2)
                sp["oxs"] = cur
                ox = cur

            # 模糊档位（量化到 5 档，与预烘一致）；活动行恒为 0 档（对焦）
            lvl = 0 if bl < 1.5 else min(4, int(round(bl / 3.0)))
            tex = sp["blurs"][lvl]
            x = LYR_X - ox + 3.0 * a
            y = ytop + (1.0 - sc) * sp["h"]

            if i == idx:
                # 活动行：**单遍**绘制，未唱部分用 u_fill.z 压暗。
                # 旧版画两遍（薄底 + 已唱），两层 SRC_ALPHA 叠加会把左缘淡入算成
                # 1−(1−a)²，淡出窗口看着只有一半宽 —— 现在一遍解决，也省一次填充。
                fpx = self._fill_px(sp, t) * sc
                self._draw_tex({"tex": tex}, x, y, w, sp["h"] * sc,
                               (INK[0], INK[1], INK[2], alpha),
                               fill=(fpx, 28.0, 0.42), fadex=fade)
            else:
                self._draw_tex({"tex": tex}, x, y, w, sp["h"] * sc,
                               (INK[0], INK[1], INK[2], alpha), fadex=fade)

            # 译文：与原文同属一个视觉单元 —— 同一个 act / bs / edge / 模糊档位，
            # 同一族字体、同一套墨色（色相偏移一致），只是字号小一档、字重轻一档。
            if sp.get("tr"):
                tsc = 1.0 + 0.06 * a
                ta = alpha * TRANS_K
                ty = ytop + sp["h"] - 20.0          # 贴着原文底边（含 sprite 内边距）
                tb = sp.get("tr_blurs")
                ttex = tb[lvl] if tb else sp["tr"]
                self._draw_tex({"tex": ttex}, x + 1.0, ty,
                               sp["tw"] * tsc, sp["th"] * tsc,
                               (INK[0], INK[1], INK[2], ta), fadex=fade)

    def _draw_footer(self, t):
        # 歌名
        if self.tex_title:
            s = self.tex_title
            self._draw_tex(s, 96, 846, s["w"], s["h"],
                           (INK[0], INK[1], INK[2], 0.96))
        if self.tex_artist:
            s = self.tex_artist
            self._draw_tex(s, 98, 972, s["w"], s["h"], (INK[0], INK[1], INK[2], 0.66))
        if self.tex_vocal:
            s = self.tex_vocal
            self._draw_tex(s, 98, 993, s["w"], s["h"], (INK[0], INK[1], INK[2], 0.50))
        # 时间（每帧都在变：按「秒」缓存纹理，最多 400 张，杜绝每帧新建纹理）
        cur = self._fmt_dur(max(0.0, t - self.head_pad))
        s = self._clock.get(cur)
        if s is None:
            if len(self._clock) > 400:
                for k in list(self._clock.keys())[:200]:
                    try:
                        self._clock[k]["tex"].release()
                    except Exception:
                        pass
                    self._clock.pop(k, None)
            s = self._label(cur, self.sans, CLOCK_PX, 400, 8)
            self._clock[cur] = s
        self._draw_tex(s, SW - 96 - s["w"], 972, s["w"], s["h"], (INK[0], INK[1], INK[2], 0.92))
        if self.tex_total:
            s2 = self.tex_total
            self._draw_tex(s2, SW - 96 - s2["w"], 1000, s2["w"], s2["h"],
                           (INK[0], INK[1], INK[2], 0.34))
        # 进度条
        pr = max(0.0, min(1.0, (t - self.head_pad) / max(0.001, self.duration - self.head_pad)))
        y = 1032.0
        self.p_sol["u_color"].value = (1.0, 1.0, 1.0, 0.10)
        self._quad("sol", 96, y, SW - 192, 1.2)
        if pr > 0:
            self.p_sol["u_color"].value = (ACCENT[0], ACCENT[1], ACCENT[2], 0.85)
            self._quad("sol", 96, y - 0.5, (SW - 192) * pr, 2.2)
            self.p_sol["u_color"].value = (ACCENT[0], ACCENT[1], ACCENT[2], 0.14)
            self._quad("sol", 96, y - 2.4, (SW - 192) * pr, 6.0)

    # ------------------------------------------------------------------
    def prepare_still(self, t):
        """离线调试：不建编码/解码管道，只准备素材。"""
        self._no_pipe = True
        j = self.job
        lrc_path = j.get("lrc")
        with io.open(lrc_path, "r", encoding="utf-8") as f:
            lrc_text = f.read()
        self.lines = parse_lrc(lrc_text)
        self.trans = {}
        tr = j.get("trans")
        if tr and os.path.exists(tr):
            with io.open(tr, "r", encoding="utf-8") as f:
                for it in parse_lrc(f.read()):
                    self.trans[round(it["t"] * 100)] = it["text"]
        lang = body_lang("".join(l["text"] for l in self.lines))
        self.serif = "serif_sc" if lang == "zh" else "serif_jp"   # 片头封面（海报语言，保留衬线）
        self.sans = "sans_sc"
        # 歌词：粗体无衬线（A 方案）。日文歌用 JP 字形，中文歌用 SC 字形，同族同设计。
        self.body_font = "sans_sc" if lang == "zh" else "sans_jp"
        self.trans_font = "sans_sc"
        self.LH = LH if self.trans else LH_NOTRANS
        for i, ln in enumerate(self.lines):
            nxt = self.lines[i + 1]["t"] if i + 1 < len(self.lines) else self.duration
            est = 0.42 * len(ln["text"]) + 0.9
            ln["end"] = min(nxt, ln["t"] + max(est * 1.6, 1.2), self.duration)
        tk = j.get("track")
        if tk and os.path.exists(tk):
            with io.open(tk, "r", encoding="utf-8") as f:
                self.track = json.load(f)
        self._build_gl()
        self._build_sprites()
        if self.cover_frames:
            self._build_cover()
        log("[live] W,H=%d,%d  p_txt u_res=%s  p_vid u_res=%s"
            % (self.W, self.H, self.p_txt["u_res"].value, self.p_vid["u_res"].value))

    def export_still(self, sec):
        """把某一秒预跑一遍（让缓动收敛），再落 PNG。"""
        i = int(round(float(sec) * self.fps))
        # 先空跑 1.5 秒让滚动/亮度缓动收敛到稳态
        for k in range(max(1, i - int(self.fps * 2.2)), i + 1):
            self.render_still_frame(k)
        buf = self.fbo.read(components=4, dtype="f1")
        img = Image.frombytes("RGBA", (self.W, self.H), buf)
        img = img.transpose(Image.FLIP_TOP_BOTTOM).convert("RGB")
        path = self.out
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        img.save(path)
        log("[live] still → %s" % path)

    def render_still_frame(self, i):
        self.frame_no = i
        if self.cover_frames and i < self.cover_frames:
            self._draw_cover(i)
            return
        t = (i - self.cover_frames) * self.dt
        idx = self._update_motion(t)
        ctx = self.ctx
        if not getattr(self, "_still_src", False):
            self._load_still_source(t)

        # 环境底：源帧降到 96x54（只留色与光）
        self.fbo_env.use()
        ctx.clear(0, 0, 0, 1)
        ps = self.p_small
        ps["u_y"].value = 0
        ps["u_uv"].value = 1
        self.tex_y.use(0)
        self.tex_uv.use(1)
        self._quad("small", 0, 0, SW, SH)

        self.fbo.use()
        ctx.clear(*BG, 1.0)
        p = self.p_bg
        p["u_bg"].value = 0
        p["u_res"].value = (SW, SH)
        p["u_frame"].value = float(i)
        self.tex_env.use(0)
        self._quad("bg", 0, 0, SW, SH)
        self._draw_video(t)
        self._draw_lyrics(t, idx)
        self._draw_footer(t)

    def _load_still_source(self, t):
        ff = os.environ.get("FFMPEG_BIN") or "ffmpeg"
        cmd = [ff, "-v", "error", "-ss", "%.3f" % (self.cut_start + max(0.0, t - self.head_pad)),
               "-i", self.src, "-frames:v", "1", "-vf", "scale=1920:1080",
               "-f", "rawvideo", "-pix_fmt", "nv12", "-"]
        raw = subprocess.run(cmd, stdout=subprocess.PIPE).stdout
        if len(raw) >= 1920 * 1080 * 3 // 2:
            self.tex_y.write(raw[:1920 * 1080])
            self.tex_uv.write(raw[1920 * 1080:])
        self._still_src = True

    def _draw_video(self, t):
        ex, ey = self._track_xy(t)
        crop_h = 980.0
        crop_w = crop_h * (PANEL_W / SH)
        want_x = max(0.0, min(SW - crop_w, ex - crop_w * 0.52))
        want_y = max(0.0, min(SH - crop_h, ey - crop_h * 0.30))
        if self._crop_x is None:
            self._crop_x, self._crop_y = want_x, want_y
        else:
            a = min(1.0, self.dt * 2.2)
            self._crop_x += (want_x - self._crop_x) * a
            self._crop_y += (want_y - self._crop_y) * a
        p = self.p_vid
        p["u_y"].value = 0
        p["u_uv"].value = 1
        p["u_crop"].value = (self._crop_x / SW, self._crop_y / SH,
                             crop_w / SW, crop_h / SH)
        p["u_fade"].value = (0.58, 1.0, 0.74, 0.98)
        self.tex_y.use(0)
        self.tex_uv.use(1)
        self._quad("vid", PANEL_X, 0, PANEL_W, SH)

    def run(self):
        self.prepare()
        frames = self.cover_frames + int(round(self.duration * self.fps))
        log("[live] 输出 %dx%d@%d，%d 帧（%.1fs = 封面 %.2fs + 正片 %.2fs）→ %s"
            % (self.W, self.H, self.fps, frames,
               frames / float(self.fps), self.cover_sec, self.duration, self.out))
        t0 = time.time()
        el0 = time.time()
        try:
            for i in range(frames):
                self.render_frame(i)
                if i % self.fps == 0:
                    el = time.time() - t0
                    log("[live] %5d/%d  %.1f fps  %.0fs" % (i, frames, (i + 1) / max(el, 1e-6), el))
        finally:
            # 收尾顺序：先让写线程把队列里的帧吐完（否则最后一两帧会丢），再关管道。
            self._work.put(None)
            self._writer.join(timeout=30)
            try:
                self.enc.stdin.close()
            except Exception:
                pass
            # 解码端通常还剩一截没读（+2s 余量）：直接杀掉，避免 ffmpeg 刷
            # "Broken pipe / Error muxing a packet" 的假告警盖住真错误。
            try:
                self.dec.kill()
            except Exception:
                pass
            self.enc.wait()
            self.dec.wait()
        el = time.time() - t0
        log("[live] 完成：%d 帧 / %.1fs = %.2f fps（%.1f ms/帧；写线程收尾 %.1fs）"
            % (frames, el, frames / el, el / frames * 1000.0, time.time() - el0))
        return el


def _ease(x):
    x = max(0.0, min(1.0, x))
    return 1.0 - (1.0 - x) ** 3


def _sstep(x, e0, e1):
    """smoothstep，允许 e1 < e0（用于「往下渐隐」）。"""
    if abs(e1 - e0) < 1e-6:
        return 1.0 if x >= e1 else 0.0
    t = max(0.0, min(1.0, (x - e0) / (e1 - e0)))
    return t * t * (3.0 - 2.0 * t)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--still", default=None, help="只渲某一秒并导出 PNG（调试版式用）")
    a = ap.parse_args()
    with io.open(a.job, "r", encoding="utf-8") as f:
        job = json.load(f)
    out = a.out or job.get("out")
    if not out:
        raise SystemExit("缺少输出路径")
    r = LiveRenderer(job, out)
    if a.still is not None:
        r.prepare_still(float(a.still))
        r.export_still(a.still)
        return
    r.run()


if __name__ == "__main__":
    main()
