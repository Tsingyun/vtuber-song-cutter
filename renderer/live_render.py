# -*- coding: utf-8 -*-
"""歌切 MP4 渲染器（live 版式）：主播实时画面 + 动态歌词 → 4K60 MP4。

设计目标与边界
--------------
· 复用旧版 MP4 输出链路（song_cutter → 本脚本 → ffmpeg 混流），不新建管线。
· 与 renderer/gpu_render.py（A 唱片封面版）**并行存在**，旧版一行不动，可随时回退。
· 解码走 NVDEC、合成走 OpenGL、编码走 NVENC —— 三段都在显卡上，中间只有两次
  必要的内存搬运（源帧 nv12 下载 3.1MB/帧、成片 RGBA 回读 33MB/帧），
  这是「用 Python + ffmpeg 管道」这条链路无法消除的下限，已实测不是瓶颈。

歌词动效严格复刻旧版 Apple Music 参数（见 index.html drawLyrics / gpu_render._update_motion）：
  scrollY τ≈0.18s；act 聚焦 dt*9 / 失焦 dt*4.2；bl 当前行 dt*12 / 其余 dt*5.5；
  透明度 已唱 .34 / 当前 1.0 / 未唱 .24+.36·exp(-(d-1)/1.7)；字号 1.0→1.22；视口边缘淡出。
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
LYR_X = 1150.0                      # 歌词列左缘
LYR_W = 700.0
FOCAL_Y = 500.0                     # 活动行中心（46.3%）
BASE_PX = 42.0                      # 歌词主字号（设计像素）
LH = 104.0                          # 行高：原文 + 译文 + 呼吸（活动行放大 1.26× 也要塞得下）
TRANS_PX = 21.0                     # 译文
TRANS_DY = 52.0                     # 译文相对原文顶的间距（活动行另按放大比例下推）
TRANS_K = 0.50
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
}

VS = """
#version 330
in vec2 in_pos;
in vec2 in_uv;
uniform vec2 u_res;
out vec2 v_uv;
void main(){
    v_uv = in_uv;
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
    /* 环境底只取「色」，不取「形」：
       96x54 的源帧上取 6 个固定点做加权平均，得到这一帧的整体环境色，
       再用垂直渐变铺满。人形亮斑不会被放大成鬼影（那是上一版的硬伤）。 */
    vec3 a = texture(u_bg, vec2(0.14, 0.18)).rgb;
    vec3 b = texture(u_bg, vec2(0.50, 0.22)).rgb;
    vec3 c1 = texture(u_bg, vec2(0.86, 0.18)).rgb;
    vec3 d = texture(u_bg, vec2(0.14, 0.82)).rgb;
    vec3 e = texture(u_bg, vec2(0.50, 0.78)).rgb;
    vec3 f = texture(u_bg, vec2(0.86, 0.82)).rgb;
    vec3 base = (a + b + c1 + d + e + f) / 6.0;
    base = mix(vec3(dot(base, vec3(0.299, 0.587, 0.114))), base, 0.70);

    // 垂直渐变：上略亮（承接人物那侧的光），下压暗给底栏让位
    float v = smoothstep(0.0, 1.0, p.y);
    vec3 col = base * mix(1.30, 0.55, v);
    // 右侧再压一档，歌词才读得清
    col *= mix(1.0, 0.42, smoothstep(0.42, 1.0, p.x));
    col *= 0.42;
    // 一点品牌色空气光，避免右半张「空」
    vec2 g = (p - vec2(0.88, 0.26)) * vec2(1.4, 1.2);
    col += vec3(0.62, 0.36, 0.44) * exp(-dot(g, g) * 3.4) * 0.045;
    // 底部左侧暗池
    vec2 dd = (p - vec2(0.21, 0.90)) * vec2(1.45, 4.6);
    col *= (1.0 - 0.86 * exp(-dot(dd, dd) * 1.55));
    // 极轻颗粒
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
out vec4 f_color;
uniform sampler2D u_tex;
uniform vec4 u_color;
uniform vec4 u_fill;   // x: 填充像素（纹理空间，<0 关闭）; y: 过渡宽; z: 未唱系数
uniform vec2 u_size;   // 纹理绘制尺寸（像素）
void main(){
    vec2 p = v_uv;
    float a = texture(u_tex, p).a;
    if (u_fill.x >= 0.0) {
        float x = p.x * u_size.x;
        float m = smoothstep(u_fill.x - u_fill.y, u_fill.x + u_fill.y, x);
        // m=0 已唱（不透明）→ m=1 未唱（乘系数）
        a *= mix(1.0, u_fill.z, m);
    }
    f_color = vec4(u_color.rgb, a * u_color.a);
}
"""

FS_SOLID = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform vec4 u_color;
void main(){ f_color = u_color; }
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
        self.serif = "serif_sc" if lang == "zh" else "serif_jp"
        self.sans = "sans_sc"
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

        self.vbo = {}
        self.vao = {}
        for name, prog in (("bg", self.p_bg), ("vid", self.p_vid),
                           ("txt", self.p_txt), ("sol", self.p_sol),
                           ("small", self.p_small)):
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
        for prog in (self.p_bg, self.p_vid, self.p_txt, self.p_sol, self.p_small):
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
        """每行：原文（含 5 档模糊）+ 译文。逐字 x 供卡拉 OK 填充。"""
        t0 = time.time()
        self.sprites = []
        blur_levels = (0.0, 3.0, 6.0, 9.0, 12.0)
        for i, ln in enumerate(self.lines):
            sp = {"i": i, "t": ln["t"], "end": ln["end"], "words": ln["words"],
                  "xs": None, "w": 0, "h": 0, "blurs": []}
            img, w, h, xs = text_mask(ln["text"], self.serif, BASE_PX, 380, pad=14, ss=2.0)
            sp["w"], sp["h"], sp["xs"] = w, h, xs
            sp["blurs"] = [self._upload(img if b <= 0.01 else
                                        img.filter(ImageFilter.GaussianBlur(b * 2.0)))
                           for b in blur_levels]
            sp["base"] = sp["blurs"][0]
            tr = self.trans.get(round(ln["t"] * 100))
            if tr:
                ti, tw, th, _ = text_mask(tr, self.sans, TRANS_PX, 300, pad=10, ss=2.0)
                sp["tr"] = self._upload(ti)
                sp["tw"], sp["th"] = tw, th
            self.sprites.append(sp)
        # 固定文案
        self.tex_title = self._label(self.job.get("title") or "", self.serif, TITLE_PX, 380, 16)
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
        target = idx * LH - (FOCAL_Y - 0.0)
        self.scrollY += (target - self.scrollY) * min(1.0, self.dt * 5.5)
        for i, sp in enumerate(self.sprites):
            tg = 1.0 if i == raw else 0.0
            k = 9.0 if tg > sp.get("act", 0.0) else 4.2
            sp["act"] = sp.get("act", 0.0) + (tg - sp.get("act", 0.0)) * min(1.0, self.dt * k)
            if raw < 0:
                base = 0.20 + 0.30 * math.exp(-i / 1.7)
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

    def _draw_tex(self, tex_info, x, y, w, h, color, fill=None):
        p = self.p_txt
        p["u_tex"].value = 0
        tex_info["tex"].use(0)
        p["u_color"].value = color
        if fill is None:
            p["u_fill"].value = (-1.0, 1.0, 1.0, 0.0)
        else:
            p["u_fill"].value = (fill[0], fill[1], fill[2], 0.0)
        p["u_size"].value = (w, h)
        self._quad("txt", x, y, w, h)

    def render_frame(self, i):
        t = i * self.dt
        self.frame_no = i
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

        # ---- 输出：回读到空闲缓冲 → 交给写线程（不阻塞渲染）
        buf = self._free.get()
        if self._enc_err is not None:
            raise RuntimeError("编码管道已断开：%r" % (self._enc_err,))
        self.fbo.use()
        self.fbo.read_into(buf, components=4, dtype="f1")
        self._work.put(buf)

    def _draw_lyrics(self, t, idx):
        ctx = self.ctx
        left = LYR_X
        # 列裁剪：超出列宽的行做横向滚动（旧版长行策略）
        for i, sp in enumerate(self.sprites):
            ytop = i * LH - self.scrollY          # 旧版 yTop = LY.y + i*LH - scrollY（视口顶记 0）
            if ytop > SH + LH or ytop + LH < -LH:
                continue
            a = sp.get("act", 0.0)
            bs = sp.get("bs", 0.0)
            bl = sp.get("bl", 0.0)
            edge = min(_ease((ytop + 72.0) / 120.0),
                       _ease(((SH + 42.0) - (ytop + LH)) / 120.0))
            edge = max(0.0, min(1.0, edge))
            alpha = (bs + (1.0 - bs) * a) * edge
            if alpha < 0.01:
                continue
            sc = (BASE_PX * (1.0 + 0.26 * a)) / BASE_PX
            # 长行横向滚动：唱到的位置停在列宽 42%
            w = sp["w"] * sc
            ox = 0.0
            if i == idx and w > LYR_W:
                fw = self._fill_px(sp, t) * sc
                want = max(0.0, min(w - LYR_W, fw - LYR_W * 0.42))
                key = "oxs"
                cur = sp.get(key, 0.0)
                cur += (want - cur) * min(1.0, self.dt * 3.2)
                sp[key] = cur
                ox = cur
            elif sp.get("oxs"):
                cur = sp["oxs"]
                cur += (0.0 - cur) * min(1.0, self.dt * 3.2)
                sp["oxs"] = cur
                ox = cur

            # 模糊档位（量化到 5 档，与预烘一致）；活动行恒为 0 档（对焦）
            lvl = 0 if bl < 1.5 else min(4, int(round(bl / 3.0)))
            tex = sp["blurs"][lvl]
            x = left - ox + 6.0 * a
            y = ytop + 4.0
            if i == idx:
                # 活动行：左侧一道品牌色短标记（旧版 drawLyricMarker 的等价物）
                self.p_sol["u_color"].value = (ACCENT[0], ACCENT[1], ACCENT[2], 0.85 * a * edge)
                self._quad("sol", left - 18, ytop + LH * 0.26, 2.2, LH * 0.34)
                # 活动行：整行薄底 → 已唱部分按逐字进度点亮（两层同字形，不会重影）
                self._draw_tex({"tex": tex}, x, y, sp["w"] * sc, sp["h"] * sc,
                               (INK[0], INK[1], INK[2], alpha))
                fpx = self._fill_px(sp, t) * sc
                if fpx > 0.5:
                    self._draw_tex({"tex": tex}, x, y, sp["w"] * sc, sp["h"] * sc,
                                   (INK[0], INK[1], INK[2], alpha),
                                   fill=(fpx, 30.0, 0.0))
            elif i < idx:
                self._draw_tex({"tex": tex}, x, y, sp["w"] * sc, sp["h"] * sc,
                               (INK[0], INK[1], INK[2], alpha))
            else:
                self._draw_tex({"tex": tex}, x, y, sp["w"] * sc, sp["h"] * sc,
                               (INK[0], INK[1], INK[2], alpha))

            # 译文：贴在同一行下方，与原文同属一个视觉单元（一起缩放/淡入）
            if sp.get("tr"):
                tsc = 1.0 + 0.03 * a
                ta = alpha * (0.52 + 0.34 * a) if i <= idx else alpha * 0.42
                # 活动行原文放大了 (sc-1) 倍，译文必须跟着下推同样的量，
                # 否则放大后的字形会直接压住译文（实测活动行译文完全被吃掉）。
                ty = ytop + TRANS_DY + (sc - 1.0) * sp["h"] * 0.95
                self._draw_tex({"tex": sp["tr"]}, x + 2.0, ty,
                               sp["tw"] * tsc, sp["th"] * tsc,
                               (INK[0], INK[1], INK[2], ta))

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
        self.serif = "serif_sc" if lang == "zh" else "serif_jp"
        self.sans = "sans_sc"
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
        t = i * self.dt
        self.frame_no = i
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
        frames = int(round(self.duration * self.fps))
        log("[live] 输出 %dx%d@%d，%d 帧（%.1fs）→ %s"
            % (self.W, self.H, self.fps, frames, self.duration, self.out))
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
