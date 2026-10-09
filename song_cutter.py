#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
song_cutter.py —— 全自动歌切主脚本

流程：SRT 转写 → 在线歌单表当日歌单（权威数据源）+ LLM 识别（google_search 联网核对）
      → 交叉验证修正歌名 → 切 MP3
      → 波形精修切点（伴奏起点定位 / 尾音静音果断切分，缓冲含讲话则提前截断）
      → 提取 MP3 → 歌词/封面自动匹配（LRCLIB → 网易云）
      → 播放器界面离线逐帧渲染 4K60（canvas → JPEG 流 → ffmpeg libx264 钉码率）
      → ffmpeg 混流 → ffprobe 规格校验 → 自动清理临时文件（仅留视频成品）

      --raw-cut：保留旧的「直接切源视频」档（产物为原始画面切片）

用法：
  python song_cutter.py --date 2026-09-25                # 完整流程
  python song_cutter.py --date 2026-09-25 --dry-run      # 只识别不切
  python song_cutter.py --date 2026-09-25 --redetect     # 强制重跑 LLM 识别
  python song_cutter.py --date 2026-09-25 --min-confidence 0.6
  python song_cutter.py --date 2026-09-25 --refresh-kdocs   # 强制重拉在线歌单表
  python song_cutter.py --date 2026-09-25 --keep-mp3        # 保留 MP3（默认验证通过后删除）

约定：
  * SRT 复用上游转写项目产物（全场统一时间轴，与视频 0 点对齐）；
  * LLM 配置可复用外部 config.json 的 summarize 段（模型 + 代理），见 config.json 的 llm_config；
  * 视频源用「录播根目录/日期/」下的压缩归档 MP4（音轨无损，源为 1080P60）；
  * 幂等：识别结果与切割成品均存在时跳过；日志写 cuts/<date>/cutter.log。
"""
import argparse
import difflib
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from songcut import config as CFG                     # noqa: E402  集中配置（config.json）
from songcut import lyrics_fetch                      # noqa: E402  歌词/封面自动匹配
from songcut import lyric_align                       # noqa: E402  歌词时间轴对齐
from songcut import ctc_align                         # noqa: E402  CTC 强制对齐
from songcut import wave_refine                       # noqa: E402  波形边界精修
from songcut import decor_pick                        # noqa: E402  标题装饰方案自动匹配
from songcut import timeline_sync                     # noqa: E402  切入点检测 + 时间轴同步
try:
    from songcut import qc as QC                       # noqa: E402  成片自检（PASS/WARN/BLOCK 判定化）
except Exception as _qc_e:                             # numpy 缺失等 → 自检降级，不阻断主流程
    QC = None
    _QC_IMPORT_ERR = str(_qc_e)

SRT_DIR = CFG.expand(CFG.get("srt_dir") or "")
SUM_CFG = CFG.expand(CFG.get("llm_config") or "")
REC_ROOT = CFG.expand(CFG.get("recording_root") or "")
DEFAULT_WORKDIR = CFG.expand(CFG.get("workdir") or ROOT)
SONG_LIB_JSON = CFG.expand(CFG.get("song_library_json") or "")

# 在线歌单表（当日演唱歌名的权威数据源）：地址与表序号见 config.json 的 songlist 段
KDOCS_URL = CFG.path("songlist", "url", default="") or ""
KDOCS_SHEET_INDEX = int(CFG.path("songlist", "sheet_index", default=3) or 3)
KDOCS_TTL_H = int(CFG.path("songlist", "ttl_hours", default=12) or 12)   # 歌单缓存有效期（小时）
KDOCS_FETCH_CJS = os.path.join(ROOT, "renderer", "kdocs_fetch.cjs")
NODE_CANDIDATES = CFG.node_candidates()
NODE_PATH_ENV = CFG.node_modules_path()

_PLAYER_DIR = os.path.join(ROOT, "renderer", "player")
_RENDER_CJS = os.path.join(ROOT, "renderer", "render_song.cjs")

# 画面底部注释行的字数上限：只放得下一句短句（中文 ≈40 字）
NOTE_MAX_CHARS = 40
# 切点自检：ASR 对齐推出的「原曲起点 − 切点」超过此值 → 判切点没对齐。
# 负值 = 起点晚于原曲起点（前奏被掐）；正值 = 起点早于原曲起点（混入多余静音/说话）。
TL_OFFSET_WARN_S = 3.0
MIN_SEC, MAX_SEC = 40, 900          # 单首合理区间
LRC_OVERRIDE = ""                   # --lrc-override：人工核定的歌词时间轴
PAD_START, PAD_END = 1.5, 2.0       # 边界留白（秒）

# ---- 成片输出规格（2026-09-29 起默认 4K60：1080P 下小字与歌词边缘不够锐利） ----
# res=2 → 3840×2160（画布放大 + setTransform 整体缩放，设计坐标恒为 1920×1080）
# res=1 → 1920×1080（回退档，渲染快约 4 倍）
OUT_RES = 2
# 视频码率(bps)：B站「真 4K」不被强制二压的安全区间是 16000~18500 kbps，
# 超过 19000 反而必然触发二压、画质更低；取中值 18 Mbps。
OUT_BITRATE = 18_000_000
# 音轨：B站要求 AAC-LC、≤320 kbps、双声道，取上限
OUT_AUDIO_BR = "320k"
# 编码器：4K 必须走 ffmpeg（Chromium 软件 H.264 码率控制饱和，实测 18 Mbps 目标只能出 4.24 Mbps）
OUT_ENCODER = "ffmpeg"
OUT_PRESET = "fast"                # libx264 preset（仅 vcodec=libx264 时生效）
OUT_VCODEC = "h264_nvenc"          # H.264 编码器：NVENC 硬编（实测与 x264 fast 同画质、快 3.3×）
DEFAULT_PAGES = 2                  # 未显式指定 --pages、也未开 --auto-pages 时的默认档（保持历史行为 P=2）
OUT_PAGES = DEFAULT_PAGES          # 并行渲染实例数（端到端实测：P=3 与编码进程互拖仅 13 fps，P=2 两阶段 9.2 min 最优）

# ── 渲染磁盘治理（2026-10-08）────────────────────────────────────────────
# 4K60 toBlob q0.98 实测 **2.03~2.12 MB/帧**（双源互证：残留 mjpeg 按 JPEG SOI 计数 /
# 渲染日志 recvBytes）。取 2.2 留 HTTP+流控开销余量。
# ⚠ 不要用 ffmpeg -q:v 2 抽帧测单帧（0.57 MB/帧）——那是解码后再压缩的帧，会低估 3~4 倍。
MJPEG_FRAME_MB = 2.2
# 视频侧份数：P>1 = 分段 mp4×P + concat 的 .render.mp4 + 最终成片；P=1 无分段、无 concat。
VIDEO_COPY_FACTOR = 2.2
VIDEO_COPY_FACTOR_P1 = 2.0
DISK_SAFETY_MARGIN = int(0.5 * 1024 ** 3)      # 音频/波形/对齐等杂项余量
# 渲染过程中：可用空间低于总量该比例即进入危险区 → 主动终止并清理，避免渲到 0 字节才失败
DISK_DANGER_RATIO = 0.15           # 可用占比低于此值视为危险区
DISK_ABORT_FLOOR = int(2.0 * 1024 ** 3)   # 判据①的底线余量：连这点都留不出就必然爆盘
# ⚠ 15% 占比必须设**绝对上限**：931 GB 盘上 15% = 140 GB，是一次 P=2 渲染需求的 4 倍，
#   照搬会把「空间充足」的正常渲染渲到一半掐掉（2026-10-08 实测：139 GB 可用被判危险区）。
DISK_DANGER_MAX_GATE = int(20 * 1024 ** 3)
DISK_MONITOR_INTERVAL_S = 30.0     # 渲染中磁盘采样间隔（秒）
STALE_TMP_HOURS = 6.0              # 历史临时产物的陈旧门槛（小于此值视为正在跑，不碰）
RENDER_TMP_ROOTNAME = "_render_tmp"  # 中间产物根目录（与 cuts/ 物理隔离）
AUTO_PAGES = False                 # 是否按磁盘空间自动选 P=1/P=2（--auto-pages）
GB = 1024 ** 3
MB = 1024 ** 2
# 码率验收下限（kbps）：低于此值视为未达标（用户要求「至少 4000」防二压模糊）。
# 4K 目标 18 Mbps，留足余量；res=1 同口径校验。
MIN_VIDEO_KBPS = 4000
# 第 N 次演唱编号：写入左下角档案号「<前缀>·P.N」；None 时退回仅前缀
PERF_NO = None


def footer_left_text():
    """左下角档案号文案：有演唱编号 → <前缀>·P.<N>，否则仅 <前缀>。
    前缀取 config.json 的 streamer.archive_prefix。"""
    pfx = CFG.path("streamer", "archive_prefix", default="SONG ARCHIVE") or "SONG ARCHIVE"
    return ("%s·P.%d" % (pfx, PERF_NO)) if PERF_NO else pfx


# 右侧装饰立绘（低存在感图层；取 config.json 的 streamer.art_image，未配置则自动跳过）
STREAMER_ART = CFG.expand(CFG.path("streamer", "art_image", default="") or "")


# ---------------- 基础工具 ----------------
def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)
    if log._fh:
        log._fh.write(time.strftime("[%H:%M:%S] ") + msg + "\n")
        log._fh.flush()


def open_log(workdir, date):
    d = os.path.join(workdir, "cuts", date)
    os.makedirs(d, exist_ok=True)
    log._fh = io.open(os.path.join(d, "cutter.log"), "a", encoding="utf-8")


log._fh = None


def hms_to_sec(s):
    s = s.strip()
    parts = s.split(":")
    parts = [p.replace(",", ".") for p in parts]
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def sec_to_hms(t):
    t = max(0, int(round(t)))
    return "%02d:%02d:%02d" % (t // 3600, (t % 3600) // 60, t % 60)


def run(cmd, timeout=1800):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, p.stdout, p.stderr


# ---------------- SRT 解析 ----------------
def parse_srt(path):
    txt = io.open(path, encoding="utf-8-sig").read()
    pat = re.compile(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\n|\Z)", re.S)
    out = []
    for m in pat.finditer(txt):
        s, e, t = hms_to_sec(m.group(1)), hms_to_sec(m.group(2)), m.group(3).strip()
        if t:
            out.append((s, e, t))
    out.sort(key=lambda x: x[0])
    return out


# ---------------- LLM 识别（L2） ----------------
SYSTEM_PROMPT = (
    "你是直播录播分析助手，擅长从语音转写文本中定位唱歌片段。"
    "输出必须是严格合法的 JSON，不要任何多余文本、注释或 markdown 代码围栏。"
)

USER_TMPL = """下面是一场直播的语音转写（每行形如 [HH:MM:SS] 文本，时间为该句在录播中的起点）。
请找出其中所有「唱歌片段」：主播完整演唱一首歌的段落（清唱、跟伴奏唱、哼唱整段都算）。
不算：随口哼一两句、只放原曲不唱、只是聊到歌名。

要求：
1. 每个片段给出 start / end（格式 HH:MM:SS，与转写时间轴同一坐标系）：
   - start：开始演唱的时刻（若唱歌前有报歌名/互动，取互动结束后真正开唱的时间）；
   - end：唱完收尾的时刻（唱完后的鼓掌/感谢不算）。
2. title_guess：歌名。请结合歌词内容与上下文判断，并纠正转写中的谐音错误（转写常把歌名/歌词听错）。
   ★ 重要：先拿识别出的歌词与文末「参考歌单」比对，能用歌单里的歌名解释的，必须直接采用歌单原名；
   歌单外的歌，请凭歌词特征检索核实真实歌名（不要凭模糊印象猜近音词）；
   检索后仍确定不了的才写"未知"，不要编造。
3. lang：歌曲主要语言（zh/ja/en/other）。
4. confidence：0~1 的整体把握（识别确定且边界清晰给高分）。
5. evidence：1~2 条原文依据（逐字引用转写里的句子，如报歌名的话或歌词行）。
6. 按演唱顺序输出；宁缺毋滥，没把握的片段 confidence 如实给低分。

转写全文如下：
{transcript}

参考歌单（主播唱过的歌，歌名以此为准，每行一个）：
{song_library}
"""


def load_sum_cfg():
    """载入 LLM 配置。

    优先 config.json 的 llm_config 所指文件（沿用上游结构：summarize 段 + paths 段）；
    未配置时回退到 config.json 的 llm 段，保证全新克隆也能给出可照做的提示。
    """
    if SUM_CFG and os.path.exists(SUM_CFG):
        return json.load(io.open(SUM_CFG, encoding="utf-8"))
    llm = CFG.get("llm") or {}
    log("提示：未配置 llm_config，改用 config.json 的 llm 段（模型 %s）" % (llm.get("model") or "未设置"))
    return {"summarize": llm, "paths": {}}


def _call_google(sc, system_prompt, user_content):
    import requests
    model = sc.get("model", "gemini-3.8-flash")
    base = (sc.get("api_host") or "https://generativelanguage.googleapis.com/v1beta/models").rstrip("/")
    url = "%s/%s:generateContent?key=%s" % (base, model, sc.get("api_key", ""))
    proxies = None
    if sc.get("proxy"):
        proxies = {"http": sc["proxy"], "https": sc["proxy"]}
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
        # 联网核对：歌词→真实歌名 靠检索而非记忆，治歌名幻觉
        "tools": [{"google_search": {}}],
    }
    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.post(url, json=payload, timeout=(20, 600), proxies=proxies)
            if r.status_code == 200:
                data = r.json()
                text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
                return text
            last_err = "HTTP %s: %s" % (r.status_code, r.text[:200])
            if r.status_code == 429:
                raise RuntimeError("Google 配额受限（429）")
        except RuntimeError:
            raise
        except Exception as e:  # noqa
            last_err = repr(e)
        log("  LLM 第 %d 次尝试失败：%s" % (attempt, last_err))
        time.sleep(8 * attempt)
    raise RuntimeError("LLM 调用失败：%s" % last_err)


def _call_zhipu(system_prompt, user_content):
    """备用通道：智谱 GLM（OpenAI 兼容），key 取 config.json 的 credentials.glm_credentials_file。"""
    import requests
    _gk = CFG.expand(CFG.path("credentials", "glm_credentials_file",
                              default="~/.songcut/glm_credentials.json"))
    cred = json.load(io.open(_gk, encoding="utf-8"))
    key = cred.get("api_key", "")
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    payload = {
        "model": "glm-4-flash",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": 8192,
    }
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=(20, 300))
            if r.status_code == 200:
                msg = (r.json().get("choices") or [{}])[0].get("message") or {}
                text = (msg.get("content") or "").strip()
                if text:
                    return text
                last_err = "空响应"
            else:
                last_err = "HTTP %s: %s" % (r.status_code, r.text[:200])
        except Exception as e:  # noqa
            last_err = repr(e)
        log("  GLM 第 %d 次尝试失败：%s" % (attempt, last_err))
        time.sleep(6 * attempt)
    raise RuntimeError("GLM 调用失败：%s" % last_err)


def call_llm(sc, system_prompt, user_content):
    try:
        return _call_google(sc, system_prompt, user_content)
    except RuntimeError as e:
        log("  " + str(e) + "，切换智谱 GLM 备用通道…")
        return _call_zhipu(system_prompt, user_content)


def extract_json(text):
    """稳健解析 LLM 回复：去代码围栏 → 容忍字符串内换行（strict=False）→
    优先取完整 {"songs":[…]} / 顶层数组，否则收集回复中所有完整的单首对象并去重。"""
    t = text.strip()
    t = re.sub(r"```+(?:json)?", "", t)          # 去掉所有 markdown 围栏标记
    dec = json.JSONDecoder(strict=False)
    positions = sorted([i for i, ch in enumerate(t) if ch in "{["])
    songs_out, seen = [], set()
    for i in positions:
        try:
            obj, _end = dec.raw_decode(t, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("songs"), list):
            songs_out = [x for x in obj["songs"] if isinstance(x, dict)]
            break                                 # 完整顶层结构，最优解
        if isinstance(obj, list):
            cand = [x for x in obj if isinstance(x, dict)]
            if cand:
                songs_out, done = cand, True
                break
        if isinstance(obj, dict) and "start" in obj and "end" in obj:
            key = json.dumps(obj, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                songs_out.append(obj)             # 碎裂输出中的完整单首对象，继续扫
    if songs_out:
        return {"songs": songs_out}
    raise ValueError("回复中找不到合法 JSON：" + t[:200])


def load_song_library():
    """从统计站歌曲库导出歌名清单（每行一个），失败返回空串。"""
    try:
        data = json.load(io.open(SONG_LIB_JSON, encoding="utf-8"))
        names = sorted({d["song_name"] for d in data if d.get("song_name")})
        return "\n".join(names)
    except Exception as e:  # noqa
        log("警告：歌曲库加载失败（%s），识别将不接地歌单" % e)
        return ""


_LIB_CACHE = None


def lookup_library_artist(title):
    """从统计站曲库查歌名的原唱歌手，作为歌词抓取的 artist_hint。

    同名歌曲极多（如《泡泡》就有牛佳钰、娃娃 Waa Wei 两版），不带歌手提示时
    LRCLIB / 网易云搜索会把「搜索排序第一」当成正确版本，导致成片配了不相干的歌词。
    """
    global _LIB_CACHE
    if not SONG_LIB_JSON:
        return ""
    if _LIB_CACHE is None:
        try:
            _LIB_CACHE = json.load(io.open(SONG_LIB_JSON, encoding="utf-8"))
        except Exception:
            _LIB_CACHE = []
    tn = norm_title(title)
    if not tn:
        return ""
    for d in _LIB_CACHE or []:
        if norm_title(d.get("song_name") or "") == tn:
            return (d.get("artist") or "").strip()
    return ""


# ---------------- 在线歌单表（权威歌名数据源） ----------------
def fetch_kdocs_songs(workdir, force=False):
    """读取在线歌单表（第 3 表）（kdocs_fetch.cjs 经 WPS JSAPI 抓取）。
    成功 → 返回 {songs_by_date: {date: [{name, requester, note}]}} 并写缓存；
    失败 → 打印具体原因，返回 None（调用方回退到 LLM 识别方案）。"""
    cache = os.path.join(workdir, "kdocs_songs.json")
    if not force and os.path.exists(cache):
        age_h = (time.time() - os.path.getmtime(cache)) / 3600
        if age_h < KDOCS_TTL_H:
            try:
                d = json.load(io.open(cache, encoding="utf-8"))
                if d.get("ok"):
                    log("歌单表：使用缓存（%.1fh 前，%d 天记录）" % (age_h, d.get("days", 0)))
                    return d
            except Exception:
                pass
    if not os.path.exists(KDOCS_FETCH_CJS):
        log("回退原因：找不到取数脚本 %s" % KDOCS_FETCH_CJS)
        return None
    env = dict(os.environ)
    env["NODE_PATH"] = NODE_PATH_ENV
    for node in NODE_CANDIDATES:
        if not (shutil.which(node) or os.path.exists(node)):
            continue
        log("读取在线歌单表（%s）…" % KDOCS_URL)
        try:
            p = subprocess.run([node, KDOCS_FETCH_CJS, cache, KDOCS_URL],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=180, env=env)
        except Exception as e:
            log("回退原因：调用 node 失败（%s）" % e)
            continue
        if p.returncode == 0 and os.path.exists(cache):
            try:
                d = json.load(io.open(cache, encoding="utf-8"))
                if d.get("ok"):
                    log("金山文档歌单获取成功：%d 天记录" % d["days"])
                    return d
                log("回退原因：%s" % d.get("error"))
            except Exception as e:
                log("回退原因：取数结果解析失败（%s）" % e)
        else:
            detail = (p.stdout or p.stderr or "").strip()[:200]
            log("回退原因：kdocs_fetch 退出码 %s%s" % (p.returncode, "，" + detail if detail else ""))
    log("回退原因：所有 node 候选均不可用或抓取失败")
    return None


def kdocs_titles_for(kdocs, date):
    """取某日的确认歌名列表（按表格行序=演唱顺序）。"""
    if not kdocs:
        return []
    return [s["name"] for s in (kdocs.get("songs_by_date") or {}).get(date, []) if s.get("name")]


def norm_title(t):
    return re.sub(r"[\s\u3000·・～~\-—_（）()【】\[\]]", "", str(t)).lower()


def cross_validate_titles(songs, expected):
    """用金山文档当日歌单交叉验证并修正识别歌名。返回修正条数。"""
    if not expected:
        return 0
    exp_norm = {norm_title(x): x for x in expected}
    used, fixed = set(), 0
    for sng in songs:
        g = norm_title(sng["title_guess"])
        if g in exp_norm:
            sng["title_source"] = "kdocs"
            used.add(g)
            continue
        best_n, best_src, score = None, None, 0.0
        for en, eo in exp_norm.items():
            if en in used:
                continue
            r = difflib.SequenceMatcher(None, g, en).ratio()
            if r > score:
                best_n, best_src, score = eo, en, r
        if best_src and score >= 0.55:
            old = sng["title_guess"]
            sng["title_guess"] = best_n
            sng["title_source"] = "kdocs"
            sng["kdocs_similarity"] = round(score, 2)
            used.add(best_src)
            fixed += 1
            log("  歌名修正（金山文档，相似度 %.2f）：%s → %s" % (score, old, best_n))
        else:
            sng["title_source"] = "llm"
    if len(expected) != len(songs):
        log("  ⚠ 数量对账：统计表当日 %d 首，识别 %d 首（请人工复核漏检/多检）" % (len(expected), len(songs)))
    else:
        log("  数量对账：统计表与识别结果一致（%d 首）✓" % len(expected))
    missing = [x for x in expected if norm_title(x) not in used]
    if missing:
        log("  ⚠ 统计表中有、但未识别到：%s" % "、".join(missing))
    extra = [s["title_guess"] for s in songs if s.get("title_source") == "llm"]
    if extra:
        log("  ℹ 清单外识别出的歌：%s" % "、".join(extra))
    return fixed


def _clean_time_field(v):
    """从 LLM 给出的时间字段中提取 HH:MM:SS（容忍混入的换行/围栏等垃圾字符）。"""
    m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", str(v))
    return m.group(0) if m else str(v)


def detect_songs(cfg, date, srt_path, out_json, force=False, expected_titles=None):
    if os.path.exists(out_json) and not force:
        data = json.load(io.open(out_json, encoding="utf-8"))
        log("识别缓存已存在：%s（%d 首，--redetect 可重跑）" % (out_json, len(data.get("songs", []))))
        return data

    entries = parse_srt(srt_path)
    lines = []
    for s, e, t in entries:
        lines.append("[%s] %s" % (sec_to_hms(s), t.replace("\n", " ")))
    transcript = "\n".join(lines)
    lib = load_song_library()
    prompt = (USER_TMPL.replace("{transcript}", transcript)
                       .replace("{song_library}", lib or "（歌单加载失败，跳过接地）"))
    if expected_titles:
        prompt += ("\n\n★ 今日已确认歌单（在线歌单表当日记录，权威参考，按演唱顺序）：\n"
                   + "\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(expected_titles))
                   + "\n要求：识别片段应与该清单一一对应（数量与顺序尽量一致）；"
                     "title_guess 必须直接采用清单中的歌名原文，不要改写；"
                     "若本场未唱清单中的某首、或唱了清单外的歌，请在 evidence 中说明。")
    log("转写规模：%d 句 / %d 字，歌单 %d 字，调用 LLM 识别…" % (
        len(lines), len(transcript), len(lib)))
    raw = call_llm(cfg["summarize"], SYSTEM_PROMPT, prompt)
    io.open(out_json + ".raw.txt", "w", encoding="utf-8", newline="").write(raw)  # 留存原始回复
    try:
        data = extract_json(raw)
    except ValueError:
        log("  JSON 解析失败，原始回复已留存：%s.raw.txt" % out_json)
        raise
    songs = data.get("songs") or []
    # 清洗
    cleaned, seen = [], []
    for i, sng in enumerate(songs):
        try:
            st = hms_to_sec(_clean_time_field(sng.get("start", "")))
            en = hms_to_sec(_clean_time_field(sng.get("end", "")))
        except Exception:
            log("  丢弃无法解析时间的条目 #%d" % (i + 1))
            continue
        if en <= st:
            st, en = en, st
        dur = en - st
        if dur < MIN_SEC or dur > MAX_SEC:
            log("  丢弃时长异常（%.0fs）：%s" % (dur, sng.get("title_guess", "?")))
            continue
        cleaned.append({
            "seq": len(cleaned) + 1,
            "title_guess": str(sng.get("title_guess") or "未知").strip() or "未知",
            "start": st, "end": en,
            "lang": str(sng.get("lang") or "zh"),
            "confidence": float(sng.get("confidence") or 0.5),
            "evidence": sng.get("evidence") or [],
        })
        seen.append((st, en))
    # 边界留白（防重叠）
    cleaned.sort(key=lambda x: x["start"])
    for i, sng in enumerate(cleaned):
        s = sng["start"] - PAD_START
        e = sng["end"] + PAD_END
        if i > 0:
            s = max(s, cleaned[i - 1]["end"] + 0.5)
        sng["start"], sng["end"] = max(0, s), e
        sng["start_hms"], sng["end_hms"] = sec_to_hms(sng["start"]), sec_to_hms(sng["end"])

    result = {"version": 1, "date": date, "srt": srt_path, "songs": cleaned}
    io.open(out_json, "w", encoding="utf-8", newline="").write(
        json.dumps(result, ensure_ascii=False, indent=2))
    log("识别完成：%d 首 → %s" % (len(cleaned), out_json))
    return result


# ---------------- 视频定位与规格 ----------------
def ffprobe_json(ffprobe, path):
    rc, out, err = run([ffprobe, "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", path])
    if rc != 0:
        raise RuntimeError("ffprobe 失败：%s" % err[:200])
    return json.loads(out)


def find_video(date):
    d = os.path.join(REC_ROOT, date)
    vids = [os.path.join(d, f) for f in os.listdir(d)
            if f.lower().endswith((".mp4", ".flv", ".mkv"))]
    if not vids:
        raise FileNotFoundError("日期目录下没有录播文件：" + d)
    vids.sort(key=os.path.getsize, reverse=True)
    if len(vids) > 1:
        log("警告：日期目录有多场录播，取最大的一场（建议后续按场次细分）")
    return vids[0]


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def out_basename(date, disp):
    """成片文件名统一格式：

        【<演唱者>】歌名【YYYYMMDD歌切】

    例：2026-09-28 + ひまわりの約束
        → 【示例UP主】ひまわりの約束【20260928歌切】
    date 中的非数字字符一律剔除（兼容 2026-09-28 / 20260928）；
    对 2026.9.28 这类缺前导零的写法会自动补零为 20260928。
    disp 传入前请先 sanitize（本函数不再二次处理，避免双重转义）。
    """
    m = re.match(r"\s*(\d{4})\D+(\d{1,2})\D+(\d{1,2})", str(date))
    if m:
        ymd = "%s%02d%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    else:
        ymd = re.sub(r"\D", "", str(date))
    who = CFG.path("streamer", "name", default="") or "Song Cut"
    return "【%s】%s【%s歌切】" % (who, disp, ymd)


# ---------------- 切割 ----------------
def cut_one(ffmpeg, src, seg, out_mp4, out_mp3, fast_copy=False):
    dur = seg["end"] - seg["start"]
    ss = seg["start"]
    # MP3（192k，重编码保证容器兼容）
    rc, _, err = run([ffmpeg, "-y", "-v", "error", "-ss", "%.3f" % ss, "-i", src, "-t", "%.3f" % dur,
                      "-vn", "-c:a", "libmp3lame", "-b:a", "192k",
                      "-metadata", "title=%s" % seg["title_guess"],
                      "-metadata", "artist=%s" % (CFG.path("streamer", "name", default="") or ""),
                      "-metadata", "date=%s" % seg.get("date", ""),
                      "-id3v2_version", "3", out_mp3])
    if rc != 0:
        raise RuntimeError("MP3 切割失败：%s" % err[-300:])
    # MP4（1080P60 H.264 NVENC 精确重编码；fast_copy 用流复制快速档）
    if fast_copy:
        cmd = [ffmpeg, "-y", "-v", "error", "-ss", "%.3f" % ss, "-i", src, "-t", "%.3f" % dur,
               "-c", "copy", "-movflags", "+faststart", out_mp4]
    else:
        cmd = [ffmpeg, "-y", "-v", "error", "-ss", "%.3f" % ss, "-i", src, "-t", "%.3f" % dur,
               "-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23",
               "-b:v", "0", "-maxrate", "16M", "-bufsize", "32M",
               "-r", "60", "-c:a", "aac", "-b:a", "256k",
               "-movflags", "+faststart", out_mp4]
    rc, _, err = run(cmd, timeout=3600)
    if rc != 0:
        raise RuntimeError("MP4 切割失败：%s" % err[-300:])


def verify_specs(ffprobe, path):
    info = ffprobe_json(ffprobe, path)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), {})
    num, den = (v.get("avg_frame_rate") or "0/1").split("/")
    fps = round(float(num) / float(den or 1), 2) if float(den or 0) else 0
    dur = float(info["format"].get("duration", 0) or 0)
    size = float(info["format"].get("size", 0) or 0)
    # 视频码率验收：优先取流级 bit_rate；MP4 未写入时用 (总大小-音轨大小)/时长 兜底
    vbr = int(v.get("bit_rate") or 0)
    abr = int(a.get("bit_rate") or 0)
    if not vbr and dur > 0:
        vbr = int(max(0.0, size - abr * dur / 8) * 8 / dur)
    return {
        "width": v.get("width"), "height": v.get("height"),
        "fps": fps, "video_codec": v.get("codec_name"),
        "audio_codec": a.get("codec_name"),
        "duration": round(dur, 2),
        "size_mb": round(size / 1048576, 1),
        "video_bitrate_kbps": round(vbr / 1000),
        "audio_bitrate_kbps": round(abr / 1000),
    }


# ---------------- 播放器渲染（4K60 成品） ----------------
def pick_node():
    """定位 Node 可执行文件：先按 PATH 解析，再退到绝对候选。"""
    for nd in NODE_CANDIDATES:
        if os.path.sep in nd or (os.path.altsep and os.path.altsep in nd):
            if os.path.exists(nd):
                return nd
        else:
            found = shutil.which(nd)
            if found:
                return found
    raise RuntimeError("找不到 node 可执行文件（可用环境变量 SONGCUT_NODE 指定）")


def _rm_retry(path, tries=4, sleep_s=0.4, log=None):
    """删文件，遇 OSError 重试（Windows 大文件必需）。

    ⚠ Windows 上删除十几 GB 的大文件会**偶发瞬时失败**（杀毒软件 /
      索引服务 / shell 删除钩子此刻仍持有句柄），等 0.4s 重试几乎必成。
      不重试会静默留下一个巨额文件 —— 实测 14.47 GB 只清掉一半。
    ⚠ 一律用 os.remove = **永久删除**。绝不能让几十 GB 的 mjpeg 进
      $RECYCLE.BIN —— 那样空间不会立即释放，下一次预检会误判。
    返回 True 表示「此刻路径已不存在」（本来就不存在也算，幂等）。
    """
    for i in range(tries):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except (NotADirectoryError, IsADirectoryError, PermissionError) as e:
            # 传进来的是目录：os.remove 在 Windows 上抛 PermissionError
            if log:
                log("  ⚠ 删除失败（不是普通文件，请用 rmtree）：%s —— %s" % (path, e))
            return False
        except OSError as e:
            if i + 1 < tries:
                time.sleep(sleep_s)
            elif log:
                log("  ⚠ 删除失败（已重试 %d 次，可能被占用）：%s —— %s" % (tries, path, e))
    return False


def _cleanup_render_tmp(out_path):
    """删掉本次渲染留下的 mjpeg / 分段 mp4 / concat 清单。

    ⚠ 成功路径也要调：cjs 正常分支会删，但异常分支可能留下几十 GB 的 mjpeg。
    ⚠ 不包含 out_path 本身（.render.mp4 混流还要用，由 produce_one 收尾删）。
    """
    n, freed, fail = 0, 0, 0
    for f in _iter_render_artifacts(out_path):
        try:
            sz = os.path.getsize(f)
        except OSError:
            sz = 0
        if _rm_retry(f, log=log):
            n += 1
            freed += sz
        else:
            fail += 1
            # 绝不静默：删不掉意味着空间不会释放，下一次预检还会被这笔账坑
            log("  ⚠ 中间产物删不掉（可能被占用）：%s（%.2f GB）" % (f, sz / GB))
    if n:
        log("  已清理渲染中间产物 %d 个，回收 %.2f GB" % (n, freed / GB))
    if fail:
        log("  ⚠ 仍有 %d 个中间产物未删净，磁盘不会立即释放" % fail)
    return freed


def _sweep_stale_render_tmp(workdir, older_than_h=6.0, log=print):
    """清掉**历史遗留**的渲染中间产物（不限于本次 out 路径）。

    ⚠ 为什么必须做：`_cleanup_render_tmp` 只在失败路径按本次 out 清理，
    但崩溃 / 断电 / 磁盘满被kill 时进程根本没走到那一步。实测2026-10-07
    就留下 **14.47 GB** 的 `*.render.mp4.p{0,1}.mjpeg` 躺在 cuts/2026-10-07/
    —— 那是成片体积的 22 倍，且**删掉后空间不立即释放**（在回收站里），
    会让后续每次渲染的磁盘预检都误报。

    只删「最后修改超过 older_than_h 小时」的文件，避免误删正在跑的任务。
    """
    import glob as _g
    import time as _time
    pats = ("**/*.render.mp4.p*.mjpeg", "**/*.render.mp4.p*.mp4",
            "**/*.render.mp4", "**/*.render.mp4.concat.txt",
            # _render_tmp/<date>/ 下的任何残留（哪怕命名对不上也一并收走）
            "_render_tmp/**/*.mjpeg", "_render_tmp/**/*.mp4", "_render_tmp/**/*.txt")
    now = _time.time()
    n, freed = 0, 0
    for pat in pats:
        for f in _g.glob(os.path.join(workdir, pat), recursive=True):
            try:
                st = os.stat(f)
            except OSError:
                continue
            if now - st.st_mtime < older_than_h * 3600:
                continue                      # 太新，可能是正在渲染
            if _rm_retry(f):
                n += 1
                freed += st.st_size
            else:
                log("  ⚠ 残留无法删除（可能被占用）：%s（%.2f GB）"
                    % (f, st.st_size / 1024 ** 3))
    if n:
        log("  已清理历史渲染残留 %d 个文件，回收 %.2f GB"
            "（全程 os.remove 永久删除，不进回收站；若空间仍未释放多为杀软/索引器持句柄）"
            % (n, freed / GB))
    return freed


def render_tmp_dir(workdir, date):
    """渲染中间产物目录：`_render_tmp/<date>/`。

    ⚠ 必须与成片目录 `cuts/<date>/` **物理隔离**：
      · mjpeg / 分段 mp4 / concat 清单 / .render.mp4 全在这里
      · 渲染失败或崩溃后一眼可辨，可整体清理
      · 不会被 `_trash/` 的旧片备份机制误判，正式输出目录保持干净
    """
    d = os.path.join(workdir, RENDER_TMP_ROOTNAME, str(date))
    os.makedirs(d, exist_ok=True)
    return d


def _iter_render_artifacts(out_path):
    """本次渲染可能产生的中间产物（mjpeg / 分段 mp4 / concat 清单）。

    ⚠ 不含 out_path 本身（那是 .render.mp4，混流还要用，不能提前删）。
    """
    import glob as _g

    def _esc(p):
        return p.replace("[", "[[]").replace("]", "[]]").replace("?", "[?]").replace("*", "[*]")

    base = _esc(out_path)
    for pat in (base + ".p*.mjpeg", base + ".p*.mp4", base + ".concat.txt"):
        for f in _g.glob(pat):
            yield f


def _render_tmp_size(out_path):
    """本次渲染已落盘的中间产物总字节（渲染中磁盘监控用）。"""
    n = 0
    for f in _iter_render_artifacts(out_path):
        try:
            n += os.path.getsize(f)
        except OSError:
            pass
    try:                                   # P=1 时 .render.mp4 本身也在增长
        n += os.path.getsize(out_path)
    except OSError:
        pass
    return n


def estimate_render_breakdown(duration, bitrate=None, pages=2, frame_mb=None):
    """估算本次渲染的**峰值**临时空间（字节），返回拆分明细 dict。

    ⚠ 公式依据（2026-10-08 实测，勿回退）：
      P>1 两阶段：mjpeg 全片落盘（每帧只写一次，**不乘 pages** —— 分段落盘，
                  第 p 个实例只写自己那段）+ 视频侧三份
      P=1  流式  ：帧直接走 stdin 进 ffmpeg，**完全不落盘** → 只有两份视频流
      ⚠ 旧式 `2GB + pages × frames × 2.2MB` 整体高估 2.1~2.2 倍，不要再改回去。
    """
    br = float(bitrate if bitrate is not None else OUT_BITRATE)
    fm = float(frame_mb if frame_mb is not None else MJPEG_FRAME_MB)
    d = float(duration or 0)
    frames = max(1, int(round(d * 60)))
    pages = max(1, int(pages or 1))
    if pages > 1:
        mjpeg = frames * fm * MB
        video = d * (br / 8.0) * VIDEO_COPY_FACTOR
    else:
        mjpeg = 0.0
        video = d * (br / 8.0) * VIDEO_COPY_FACTOR_P1
    return {"frames": frames, "duration": d, "bitrate": br, "pages": pages,
            "mjpeg": int(mjpeg), "video": int(video), "margin": DISK_SAFETY_MARGIN,
            "total": int(mjpeg + video + DISK_SAFETY_MARGIN)}


def estimate_render_bytes(duration, bitrate=None, pages=2, frame_mb=None):
    """estimate_render_breakdown 的总量简写。"""
    return estimate_render_breakdown(duration, bitrate, pages, frame_mb)["total"]


def disk_danger_threshold(total_bytes, need_left_bytes):
    """渲染过程中的「危险区」阈值（字节）：可用空间低于此值即应中止并清理。

    取两个判据的**较大者**：
      ① need_left + 2GB —— 剩下的空间已经不够把这次渲完，继续必然爆盘
      ② min(15% × 总容量, 20GB) —— 占比危险区
    ⚠ ② 必须设绝对上限：931 GB 盘上 15% = 140 GB，是一次 P=2 渲染需求的 4 倍。
      照搬会把「空间充足」的正常渲染渲到一半掐掉
      （2026-10-08 实测：139.2 GB 可用 / 931.5 GB = 14.9% 被判危险区而中止）。
    """
    gate_ratio = min(DISK_DANGER_RATIO * float(total_bytes or 0), DISK_DANGER_MAX_GATE)
    return int(max(float(need_left_bytes) + DISK_ABORT_FLOOR, gate_ratio))


def choose_render_pages(free_bytes, duration, bitrate=None, prefer=None):
    """按可用空间选 pages：够 → prefer（默认 P=2 速度优先），不够 → 降级 P=1。

    P=1 是最后保险：流式渲染，帧不落盘，峰值只有两份视频流（280s 约 1.8 GB），
    代价是渲染慢约 33%（12.2 min vs 9.2 min）。
    返回 dict: pages / need / need_p2 / need_p1 / fallback / breakdown。
    ⚠ 连 P=1 都不够时抛 RuntimeError —— 必须在渲染**开始前**失败，
      不能启动一个注定爆盘的长任务。
    """
    prefer = max(1, int(prefer if prefer is not None else DEFAULT_PAGES))
    cands, seen = [], set()
    for p in (prefer, 1):                  # prefer → 1 的降级链，去重
        if p not in seen:
            seen.add(p)
            cands.append(p)
    b2 = estimate_render_breakdown(duration, bitrate, 2)
    b1 = estimate_render_breakdown(duration, bitrate, 1)
    for p in cands:
        b = estimate_render_breakdown(duration, bitrate, p)
        if free_bytes >= b["total"]:
            return {"pages": p, "need": b["total"], "need_p2": b2["total"],
                    "need_p1": b1["total"], "fallback": p < prefer, "breakdown": b}
    raise RuntimeError(
        "磁盘空间不足：P=2 需约 %.1f GB，降级 P=1 仍需 %.1f GB，当前可用仅 %.1f GB。"
        "请清理磁盘后重试（渲染中间产物在 %s/<日期>/ 下，可整体删除）"
        % (b2["total"] / GB, b1["total"] / GB, free_bytes / GB, RENDER_TMP_ROOTNAME))


def resolve_pages(cli_pages=None, auto=False):
    """pages 优先级：显式 --pages > --auto-pages > 默认。返回 (pages, auto_on)。"""
    if cli_pages is not None:
        return max(1, int(cli_pages)), False      # 用户显式指定，绝不擅自改
    return max(1, int(DEFAULT_PAGES)), bool(auto)


def _prune_empty_tmp_dir(workdir, date):
    """本次渲染收尾后，`_render_tmp/<date>/` 空了就顺手删掉，不留空壳。"""
    d = os.path.join(workdir, RENDER_TMP_ROOTNAME, str(date))
    try:
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    except OSError:
        pass


def render_player_video(workdir, job, ffmpeg=None):
    """调 render_song.cjs：无头浏览器离线逐帧渲染播放器画面并编码落盘。

    job.encoder = "ffmpeg"   → 画布逐帧 JPEG(q98) 交给 ffmpeg 编码（4K 默认，
                               因为 Chromium 软件 H.264 码率控制饱和，到不了 18 Mbps）
    job.encoder = "webcodecs"→ 页面内 WebCodecs 编码后整片 POST 回落（1080P 回退档）

    ⚠ 磁盘策略（2026-10-08）：先清历史残留 → 量可用空间 → 估算 P=2/P=1 需求
      → --auto-pages 时自动选档 → 渲染中每 30s 采样 → 进危险区主动中止并清理。
      P=1/P=2 的选择**只在开始前做一次**，渲染途中绝不动态切档（切了会白渲）。
    """
    # 0) 先扫历史残留。**必须放在预检之前** —— 清出来的空间要算进可用额度，
    #    否则上一轮崩溃留下的十几 GB 会让预检误报「空间不足」。
    log("[DISK] Cleaning stale render temp...")
    try:
        _sweep_stale_render_tmp(workdir, older_than_h=STALE_TMP_HOURS, log=log)
    except Exception as _e:
        # 清理失败不许静默：可用空间账会算错，必须让人看见
        log("  ⚠ [DISK] 历史残留扫描失败（可用空间账会偏保守）：%r" % (_e,))

    _dir = os.path.dirname(os.path.abspath(job["out"])) or "."
    _tot, _used, _free = shutil.disk_usage(_dir)
    log("[DISK] Available: %.1f GB" % (_free / GB))

    _br = float(job.get("bitrate") or OUT_BITRATE)
    _dur = float(job.get("duration") or 0)
    _need_p2 = estimate_render_bytes(_dur, _br, 2)
    _need_p1 = estimate_render_bytes(_dur, _br, 1)
    log("[DISK] Estimated P=2 requirement: %.1f GB" % (_need_p2 / GB))
    log("[DISK] Estimated P=1 requirement: %.1f GB" % (_need_p1 / GB))

    if AUTO_PAGES:
        try:
            _pick = choose_render_pages(_free, _dur, _br,
                                        prefer=job.get("pages") or DEFAULT_PAGES)
        except RuntimeError as _e:
            log("[DISK] %s" % _e)      # 渲染开始前就失败，不启动注定爆盘的长任务
            raise
        job["pages"] = _pick["pages"]
        _need = _pick["need"]
        log("[DISK] Selected pages=%d (%s)" % (
            _pick["pages"], "speed priority" if _pick["pages"] > 1 else "low-space fallback"))
    else:
        job["pages"] = max(1, int(job.get("pages") or DEFAULT_PAGES))
        _need = estimate_render_bytes(_dur, _br, job["pages"])
        log("[DISK] Selected pages=%d (user-specified)" % job["pages"])
        if _free < _need:
            raise RuntimeError(
                "磁盘空间不足：本次渲染（pages=%d）需约 %.1f GB，当前可用仅 %.1f GB。"
                "空间紧张可改用 --auto-pages 或 --pages 1"
                "（低占用模式约 %.1f GB，代价是渲染慢约 33%%）"
                % (job["pages"], _need / GB, _free / GB, _need_p1 / GB))

    job_f = os.path.join(workdir, "_render_job.json")
    io.open(job_f, "w", encoding="utf-8", newline="").write(
        json.dumps(job, ensure_ascii=False))
    env = dict(os.environ)
    env["NODE_PATH"] = NODE_PATH_ENV
    if ffmpeg:
        env["FFMPEG_BIN"] = ffmpeg       # render_song.cjs 优先用本管线的 ffmpeg，避免找错
    cmd = [pick_node(), _RENDER_CJS, "--job", job_f]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    # ⚠ 输出接 TemporaryFile 而不是 PIPE：子进程日志写满 64KB 管道缓冲区会直接卡死
    import tempfile as _tf
    _so = _tf.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    _se = _tf.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    proc = None
    abort = None
    try:
        proc = subprocess.Popen(cmd, stdout=_so, stderr=_se, text=True,
                                encoding="utf-8", errors="replace",
                                env=env, creationflags=flags)
        t_start = time.time()
        last = t_start
        # ── 渲染中磁盘监控：只在开始检查一次是不够的，mjpeg 会一路涨到 30 GB+ ──
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if time.time() - t_start > 10800:
                abort = "渲染超时（>3h）"
                try:
                    proc.kill()
                except OSError:
                    pass
                break
            now = time.time()
            if now - last >= DISK_MONITOR_INTERVAL_S:
                last = now
                try:
                    _t2, _u2, _f2 = shutil.disk_usage(_dir)
                    _tmp_sz = _render_tmp_size(job["out"])
                    _left = max(0, _need - _tmp_sz)
                    log("[DISK] temp=%.1f GB, free=%.1f GB（还需约 %.1f GB）"
                        % (_tmp_sz / GB, _f2 / GB, _left / GB))
                    _th = disk_danger_threshold(_t2, _left)
                    if _f2 < _th:
                        if _left + DISK_ABORT_FLOOR >= _th:
                            abort = ("剩余空间不足以完成本次渲染：还需 %.1f GB，仅剩 %.1f GB"
                                     % (_left / GB, _f2 / GB))
                            log("[DISK] CRITICAL: not enough space to finish"
                                "（还需 %.1f GB，仅剩 %.1f GB）" % (_left / GB, _f2 / GB))
                        else:
                            abort = ("剩余空间进入危险区：%.1f GB / 共 %.1f GB（%.1f%%）"
                                     % (_f2 / GB, _t2 / GB, _f2 / _t2 * 100))
                            log("[DISK] CRITICAL: free space below %.0f%%"
                                % (DISK_DANGER_RATIO * 100))
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        break
                except Exception as _e:
                    log("  ⚠ [DISK] 监控取样失败：%r" % (_e,))
            time.sleep(1.0)
        rc = proc.poll()
        if rc is None:
            rc = proc.wait()
    except BaseException:
        # 被 Ctrl-C / 外部中断也要收干净，否则几十 GB mjpeg 留在盘上
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
        _cleanup_render_tmp(job["out"])
        _rm_retry(job["out"], log=log)
        raise
    finally:
        _so.seek(0)
        _se.seek(0)
        _out_txt = _so.read()
        _err_txt = _se.read()
        _so.close()
        _se.close()

    for line in (_out_txt or "").splitlines():
        if line.strip():
            log("  [render] " + line.strip()[:160])

    if abort:
        log("[DISK] Aborting render and cleaning temporary files...")
        _cleanup_render_tmp(job["out"])
        _rm_retry(job["out"], log=log)
        raise RuntimeError("渲染中止：%s（已清理临时文件）" % abort)
    if rc != 0 or not os.path.exists(job["out"]):
        tail = ((_err_txt or "") + (_out_txt or "")).strip()[-400:]
        _cleanup_render_tmp(job["out"])
        _rm_retry(job["out"], log=log)
        raise RuntimeError("渲染失败 rc=%s：%s" % (rc, tail))

    # 成功也要收尾：cjs 正常分支会删，但异常分支可能留下 mjpeg / 分段 mp4
    _cleanup_render_tmp(job["out"])
    return job["out"]



_ART_DATAURL_CACHE = {"done": False, "val": None}


def _art_dataurl():
    """装饰立绘 → dataURL（未配置或文件缺失返回 None，播放器自动跳过该图层）。

    结果按进程缓存：这是一张固定不变的静态立绘，base64 后约 2.2 MB，
    每首都重新读盘 + 编码一遍纯属浪费（还会让每首的渲染 job 多写 2 MB 文本）。
    """
    if _ART_DATAURL_CACHE["done"]:
        return _ART_DATAURL_CACHE["val"]
    val = None
    if not STREAMER_ART or not os.path.exists(STREAMER_ART):
        if STREAMER_ART:
            log("警告：装饰立绘不存在，跳过该图层：%s" % STREAMER_ART)
    else:
        head = io.open(STREAMER_ART, "rb").read(4)
        mime = "image/png" if head == b"\x89PNG" else "image/jpeg"
        import base64 as _b64
        val = "data:%s;base64,%s" % (
            mime, _b64.b64encode(io.open(STREAMER_ART, "rb").read()).decode())
    _ART_DATAURL_CACHE["done"] = True
    _ART_DATAURL_CACHE["val"] = val
    return val


HEAD_SIL_TARGET = 1.0     # 片头静音目标时长（秒）
HEAD_SIL_MIN = 0.8        # 低于该值则补插静音


def _shift_lrc(lrc_text, pad):
    """LRC 全部时间戳后移 pad 秒（片头补静音后，歌词时间轴同步平移）。"""
    def rep(m):
        t = float(m.group(1)) * 60 + float(m.group(2)) + pad
        return "[%02d:%05.2f]" % (int(t // 60), t % 60)
    return re.sub(r"\[(\d+):(\d+(?:\.\d+)?)\]", rep, lrc_text)


def _ensure_head_silence(ffmpeg, mp3_path, target=HEAD_SIL_TARGET, min_sil=HEAD_SIL_MIN):
    """保证 MP3 片头有 ~target 秒静音（听感上给伴奏一个"起手"）。
    检测前导静音长度，不足 min_sil 则在最前方插入静音；返回实际插入秒数。"""
    rc, _, err = run([ffmpeg, "-v", "info", "-t", "5", "-i", mp3_path,
                      "-af", "silencedetect=noise=-50dB:d=0.05", "-f", "null", "-"])
    txt = err or ""
    leading = 0.0
    m = re.search(r"silence_start:\s*([-\d.]+)", txt)
    if m and float(m.group(1)) <= 0.05:
        me = re.search(r"silence_end:\s*([\d.]+)", txt)
        if me:
            leading = float(me.group(1))
    if leading >= min_sil:
        return 0.0
    pad = max(0.0, target - leading)
    if pad <= 0.01:
        return 0.0
    ms = int(round(pad * 1000))
    tmp = mp3_path + ".pad.mp3"
    rc, _, err = run([ffmpeg, "-y", "-v", "error", "-i", mp3_path,
                      "-af", "adelay=%d:all=1" % ms,
                      "-c:a", "libmp3lame", "-b:a", "192k", tmp])
    if rc != 0 or not os.path.exists(tmp):
        log("  ⚠ 片头静音插入失败：%s" % (err or "")[-160:])
        return 0.0
    os.replace(tmp, mp3_path)
    return pad


class PreRenderGateError(Exception):
    """渲染前闸门未通过：成片已可判定为废片/错片，不值得再花 14 分钟去渲。"""


# 时长可信度上限：真唱跨度必 ≤ 官方时长 ×(1+余量)（现场加唱/拖拍不会超过 35%）。
# 2026-10-08 事故：网易云限流时 orig 静默取到 LRCLIB 的 131.3s（《反方向的钟》真实 258s），
# 成片被切掉一半且全程零报错 —— 这是唯一能拦住「时长填错」的判据。
DUR_TRUST_MAX = 0.35
DUR_LOWER, DUR_UPPER = 0.93, 1.10     # 成片/原曲 时长的合理区间（与收尾段日志同口径）
GATE_IGNORE = False                   # --ignore-gate：闸门降级为警告（应急放行）
GATE_ALLOW_NO_LRC = False             # --allow-no-lrc：放行无歌词的片


def _pre_render_gate(dur, lrc, linfo, tl_src, cov, date, disp):
    """渲染前闸门：返回 (fail[], warn[])。fail 非空 → 不该进渲染。

    只拦「成片一定有问题」的硬伤，判据全部来自既有常量与已发生的事故，
    不引入新阈值。每条都写明怎么修，避免拦下来却不知道下一步做什么。
    """
    fail, warn = [], []
    # G1 区间非法：比 MIN_SEC 还短的片段不是一首歌
    if dur < MIN_SEC:
        fail.append("G1 片段时长 %.1fs < 下限 %ds：切点没落在歌上，先核定 "
                    "seg.cut_start_abs/cut_end_abs" % (dur, MIN_SEC))
    # G2 无歌词：铁律「歌词不许遗漏」，没有歌词的片等于没做
    if not (lrc or "").strip():
        msg = ("G2 未取到歌词（来源=%s）：出片会整段无字幕。先确认歌名与歌手，"
               % (linfo.get("lyrics_source") or "无"))
        if GATE_ALLOW_NO_LRC:
            warn.append(msg + "本次按 --allow-no-lrc 放行")
        else:
            fail.append(msg + "或用 --allow-no-lrc 明确放行")
    # G3 原曲时长：缺失不许猜（静默填错值会把成片切短，且零报错）
    _od = linfo.get("netease_duration_ms")
    if not _od:
        fail.append("G3 原曲官方时长缺失：严禁猜测（2026-10-08 曾因静默填 LRCLIB 的错值 "
                    "把《反方向的钟》切掉一半）。请联网核实后写入 segments 的 "
                    "orig_duration_s，或确认歌词站恢复后重跑")
    else:
        _od_s = _od / 1000.0
        _dev = dur / _od_s
        if _dev > 1.0 + DUR_TRUST_MAX:
            fail.append("G3 成片 %.1fs 比原曲 %.1fs 长 %.0f%%（>%.0f%%）：真唱跨度不可能 "
                        "超出这么多，疑 orig 取到错版本或混入了歌后闲聊。核定 "
                        "seg.cut_end_abs" % (dur, _od_s, (_dev - 1) * 100,
                                             DUR_TRUST_MAX * 100))
        elif _dev > DUR_UPPER or _dev < DUR_LOWER:
            warn.append("G3 时长偏差 %+.0f%%（合理区间 %+.0f%%~%+.0f%%）：短了查前奏是否被掐，"
                        "长了查是否混入歌后闲聊" % ((_dev - 1) * 100,
                                                 (DUR_LOWER - 1) * 100, (DUR_UPPER - 1) * 100))
    # G4 歌词残缺：outro/重复副歌掉了（保留为警告，由人工决定是否放行）
    if lrc and cov is not None and cov < 0.80:
        warn.append("G4 歌词完整性 %.0f%% < 80%%：末行 %.1fs / 片段 %.1fs，疑漏 outro 或"
                    "重复副歌，请复核歌词版本" % (cov * 100,
                                               linfo.get("lyrics_tail_sec", 0.0), dur))
    return fail, warn


def produce_one(ffmpeg, ffprobe, src, seg, out_dir, disp, date, workdir, srt_entries=None,
                scheme_override=None):
    """切入点检测 → 波形精修 → 原曲分析（尾部切点/时间轴） → MP3 → 歌词/封面
    → 播放器渲染 → 混流。检测与同步各环节失败均自动降级，不影响正常产出。"""
    base = out_basename(date, sanitize(disp))   # 【演唱者】歌名【YYYYMMDD歌切】
    out_mp4 = os.path.join(out_dir, base + ".mp4")
    out_mp3 = os.path.join(out_dir, base + ".mp3")
    onset_abs = None

    # 1. 切点：优先人工核定（seg.cut_start_abs/cut_end_abs，诊断/复核后手工锁定），
    #    否则波形精修（切点以音频波形为准：伴奏起点 / 尾音静音果断切分）。
    #    BGM 不停的实况里自动终点会把歌后闲聊吞进来（泡泡实例 +20s），此时人工核定。
    _ex_s, _ex_e = seg.get("cut_start_abs"), seg.get("cut_end_abs")
    if _ex_s is not None and _ex_e is not None:
        cs, ce = float(_ex_s), float(_ex_e)
        ref = {"cut_start": cs, "cut_end": ce, "onset": None, "silence_at": None,
               "notes": ["人工核定切点（跳过波形精修）"]}
        log("  切点: 人工核定 %.2f → %.2f（%.1fs）" % (cs, ce, ce - cs))
    else:
        ref = wave_refine.refine_segment(ffmpeg, src, seg["start"], seg["end"],
                                         tmp_dir=os.path.join(workdir, "_tmp"))
        for nline in ref["notes"]:
            log("  波形: " + nline)
        cs, ce = ref["cut_start"], ref["cut_end"]
        log("  切点精修: %.2f → %.2f（%.1fs；粗切点 %.2f→%.2f）" % (
            cs, ce, ce - cs, seg["start"], seg["end"]))

        # 1.5 切入点检测（默认执行）：说话段 → 静音谷 → 能量回升起点。
        #     修复「粗切点偏晚吞掉渐强前奏」——wave_refine 只向回搜 12s 且切点
        #     不低于粗起点，粗点落在歌中间时（寄明月实例偏晚 15s+）无能为力。
        det = timeline_sync.detect_entry(ffmpeg, src, seg["start"], seg["end"],
                                         tmp_dir=os.path.join(workdir, "_tmpwav"), log=log)
        onset_abs = None
        if det.get("ok"):
            onset_abs = det["onset"]
            new_cs, moved, _note = timeline_sync.refine_head(cs, det)
            log("  切入点检测: onset=%.3f 静音谷=%.2f~%.2f%s" % (
                onset_abs, det["valley"][0], det["valley"][1],
                ("，cut_start %.2f → %.2f" % (cs, new_cs)) if moved else "，与精修切点一致，维持"))
            if moved:
                cs = new_cs
        else:
            log("  切入点检测: 不可用（%s）→ 维持波形精修切点" % det.get("reason", "?"))

    # 2. 歌词 + 封面自动匹配（封面：网易云专辑图；歌名识别不受影响）
    #    artist_hint：曲库里的原唱歌手，用于让搜索结果对准正确版本。
    #    ref_text：本片段的演唱转写，用于「歌词正文 vs 实际唱了什么」字面比对——
    #              同名不同歌的错版本（如《泡泡》牛佳钰版 vs 娃娃版）一测即出。
    #
    #    ⚠ 演唱版本 vs 原唱标注（用户 2026-10-09 定稿）：fetch_title 是实际演唱
    #    版本名（如「晚婚 (Live)」），歌词/原曲时长/DTW 对齐按它抓 —— 换成原唱版
    #    会导致前奏时长不符（江蕙版《晚婚》前奏 ~59s 套进切点 → 前 59s 全说话）、
    #    Live 改词句丢对齐。歌手名/封面由 segments 的 artist/cover_path 显式
    #    注入原唱（auto_cut 从曲库取），与演唱版本解耦。
    fetch_name = (seg.get("fetch_title") or seg["title_guess"] or "").strip()
    _hint = lookup_library_artist(fetch_name)
    ref_text = ""
    if srt_entries:
        ref_text = "\n".join(t for (a, b, t) in srt_entries if cs - 2 <= a <= ce + 2)
    lrc, cover_path, linfo = lyrics_fetch.fetch_lyrics_and_cover(
        fetch_name, artist_hint=_hint, dur=ce - cs, ref_text=ref_text,
        cache_dir=os.path.join(workdir, "_media_cache"))
    # 原唱封面显式覆盖（segments.cover_path，auto_cut 按曲库原唱抓取；缺失则回退抓取结果）
    _orig_cov = (seg.get("cover_path") or "").strip() if isinstance(seg.get("cover_path"), str) else seg.get("cover_path")
    if _orig_cov and os.path.exists(_orig_cov):
        cover_path = _orig_cov
        linfo["cover_source"] = "原唱封面(曲库:%s)" % (seg.get("artist") or "?")
    if _hint:
        log("  曲库歌手提示: %s" % _hint)
    src_lrc = lrc or ""          # 抓取原文快照：QC 用它做「逐字一致」比对（拦截漏行/翻唱版）
    # ⚠ note 会显示在主画面底部（给观众看的文案），不是技术备注位。
    #   超长会被播放器缩到 9px + 省略号，虽不再溢出画面，但一整行术语压在画面底部观感很差。
    #   技术细节写 note_technical（不上屏）。这里硬拦，避免整片渲完才发现。
    _note = (seg.get("note") or "").strip()
    if len(_note) > NOTE_MAX_CHARS:
        log("  ⚠ 歌曲注释 %d 字，超过 %d 字上限（画面底部只放得下一句短句）→ 已忽略 note，"
            "技术细节请写 note_technical" % (len(_note), NOTE_MAX_CHARS))
        log("    被忽略的内容：%s…" % _note[:60])
        seg["note"] = ""
        seg["note_rejected"] = _note
    elif _note:
        log("  歌曲注释（%d 字，上屏）：%s" % (len(_note), _note))
    #⚠ 取原唱要**优先 segments 的显式标注**：歌词站的 artist 字段常误配翻唱者
    #   （2026-10-08实测《反方向的钟》LRCLIB 返回「乐乐仔」，原唱应为周杰伦）。
    #   下方 meta 用的也是同一个值，这里必须同步，否则日志会打印出
    #   与画面不同的原唱名，误导排查（画面其实是对的）。
    artist = (seg.get("artist") or linfo.get("artist") or "").strip()
    tags = seg.get("tags") or "翻唱, 现场版"
    # 歌词完整性：末行时间戳 / 本片段时长的覆盖率。低于 80% 基本可断定尾部歌词掉了
    # （重复副歌、outro 是重灾区），显式告警以便人工复核，避免默默渲染残缺歌词。
    _cov = linfo.get("lyrics_coverage")
    log("  歌词: %s（%d 行）| 原唱: %s%s | 封面: %s" % (
        linfo.get("lyrics_source"), len((lrc or "").splitlines()),
        artist or "未知",
        "（歌词站标 %s，已用segments 标注纠正）" % linfo["artist"]
        if seg.get("artist") and linfo.get("artist")
        and seg["artist"] != linfo["artist"] else "",
        linfo.get("cover_source")))
    if lrc and _cov is not None and (ce - cs) > 1:
        _flag = " ⚠ 疑似残缺，请复核是否漏 outro/重复副歌" if _cov < 0.80 else ""
        log("  歌词完整性: 末行 %.1fs / 片段 %.1fs = %.0f%%%s" % (
            linfo.get("lyrics_tail_sec", 0.0), ce - cs, _cov * 100, _flag))
    if linfo.get("warn_t2s"):
        log("  ⚠ %s" % linfo["warn_t2s"])
    if linfo.get("warn_lyrics_mismatch"):
        log("  ⚠⚠ 歌词版本存疑：%s" % linfo["warn_lyrics_mismatch"])
    elif ref_text and linfo.get("lyrics_overlap") is not None:
        log("  歌词一致性: 与演唱内容字面重合 %.0f%%（OK）"
            % (linfo["lyrics_overlap"] * 100))

    # 2.5 原曲分析（默认执行）：全曲 DTW 速度比 + 局部互相关校正锚点。
    #     产出 ①精确伴奏结束点（尾部切点 = 乐句结束 + 余韵）②歌词时间轴。
    #     ⚠ 用 fetch_name + 抓取到的演唱版歌手下载原曲 —— 她唱的是哪版就对齐哪版；
    #       传原唱歌手会把原唱版伴奏下载来 DTW（编曲不同 → slope 跑飞 → 降级）。
    sync = timeline_sync.analyze(fetch_name, linfo.get("artist") or artist,
                                 ffmpeg, src, onset_abs, cs, ce, lrc or "",
                                 workdir, log=log)
    if sync.get("ok"):
        corr = ("，互相关校正 %+.3fs" % -sync["med"]) if sync.get("corrected") else ""
        log("  原曲分析: slope=%.5f 残差RMS=%.3fs%s，锚点=%.3f" % (
            sync["slope"], sync.get("rms", 0.0), corr, sync["onset_abs"]))
        new_ce = round(sync["song_end_abs"] + timeline_sync.TAIL_KEEP, 3)
        if cs + MIN_SEC <= new_ce < ce - 0.5:
            log("  尾部切点: %.2f → %.2f（伴奏结束 + %.1fs 余韵）" % (
                ce, new_ce, timeline_sync.TAIL_KEEP))
            ce = new_ce
        else:
            log("  尾部切点: 维持波形精修 %.2f（原曲结束点不在合理区间）" % ce)
    else:
        log("  原曲分析: 不可用（%s）→ 尾部/时间轴走降级链" % sync.get("reason", "?"))
    dur = ce - cs

    # 3. 提取 MP3（192k，尾部 1.2s 淡出防硬切/爆音）
    rc, _, err = run([ffmpeg, "-y", "-v", "error", "-ss", "%.3f" % cs, "-i", src,
                      "-t", "%.3f" % dur, "-vn",
                      "-af", "afade=t=out:st=%.3f:d=1.2" % max(0.0, dur - 1.3),
                      "-c:a", "libmp3lame", "-b:a", "192k",
                      "-metadata", "title=%s" % seg["title_guess"],
                      "-id3v2_version", "3", out_mp3])
    if rc != 0:
        raise RuntimeError("MP3 提取失败：%s" % err[-300:])

    # 3.2 片头静音保障：确保开头 ~1s 静音（不足则插入），避免"一进来就是伴奏"
    head_pad = _ensure_head_silence(ffmpeg, out_mp3)
    if head_pad > 0:
        log("  片头补静音 %.2fs（原前导静音不足 %.1fs）" % (head_pad, HEAD_SIL_MIN))

    # 3.5 歌词时间轴——四级策略：
    #   A) 精确同步（默认）：原曲锚点 + DTW 速度比（误差 0.1s 级）；
    #   B) CTC 强制对齐：SenseVoice 词级时间戳 + 人声乐句融合（实测 MAE 0.002s，
    #      不依赖原曲；原曲 DTW 失效/无参考时首选）；
    #   C) ASR 对齐（降级）：FunASR 演唱行配对 + 聚类常数偏移；
    #   D) 原始时间轴（兜底）。
    tl_src = "raw"
    tl_offset = None          # ASR 对齐推出的「原曲起点 − 切点」，用于切点自检
    if LRC_OVERRIDE and os.path.exists(LRC_OVERRIDE):
        lrc = io.open(LRC_OVERRIDE, encoding="utf-8").read()
        tl_src = "override"
        log("  歌词时间轴: 使用 --lrc-override 指定文件（%d 行，跳过对齐链）"
            % len(lrc.splitlines()))
        # ⚠ override 文件按「cut 相对时间」书写，与 dtw/asr/ctc 三条路径口径一致；
        # 片头补静音会让成片时间轴整体后移 head_pad，这里必须同步平移，
        # 否则渲染出的歌词会比人声早 head_pad（0.93s 级），QC 的 L09 也会系统性偏差。
        if lrc and head_pad > 0:
            lrc = _shift_lrc(lrc, head_pad)
            log("  歌词时间轴: 片头补静音 %.2fs → 歌词整体后移（override 口径对齐）" % head_pad)
    if tl_src == "raw" and lrc and sync.get("ok"):
        lrc2 = timeline_sync.render_lrc(sync, lrc, head_pad)
        if lrc2:
            lrc, tl_src = lrc2, "dtw"
            log("  歌词时间轴: 精确同步（原曲锚点+DTW，%d 行）" % len(lrc.splitlines()))
    if tl_src == "raw" and lrc:
        lrc2, cinfo = ctc_align.align_lrc(lrc, out_mp3, workdir, log=log)
        if lrc2:
            lrc, tl_src = lrc2, "ctc"
            log("  歌词时间轴: CTC 强制对齐（%d 行，匹配率 %.0f%%）"
                % (len(lrc2.splitlines()), cinfo["match_ratio"] * 100))
    if tl_src == "raw" and lrc:
        if srt_entries:
            lrc, ainfo = lyric_align.align_lrc(lrc, srt_entries, cs, ce)
            tl_src = "asr"
            log("  歌词时间轴: ASR 对齐降级（%s）" % ainfo.get("summary", "?"))
            tl_offset = ainfo.get("offset_in_cut")
            if tl_offset is not None and abs(tl_offset) > TL_OFFSET_WARN_S:
                log("  ⚠ 切点自检：ASR 对齐偏移 %+.2fs（阈值 %.1fs）——疑切点未对齐：%s"
                    % (tl_offset, TL_OFFSET_WARN_S,
                       "起点晚于原曲起点，前奏被掐掉" if tl_offset < 0
                       else "起点早于原曲起点，混入了额外静音/说话"))
                log("     建议 seg.cut_start_abs = %.2f（现 %.2f，差 %+.2f）后 --force 重渲"
                    % (cs + tl_offset, cs, tl_offset))
        else:
            log("  歌词时间轴: 无原曲对齐结果且无 SRT，保持原时间轴")
        if lrc and head_pad > 0:
            lrc = _shift_lrc(lrc, head_pad)   # 片头补静音 → 歌词整体后移，保持同步

    # 3.85 歌词快照落盘：src=抓取原文（QC 参照），final=时间轴对齐后（渲染实际使用）
    lrc_snap = os.path.splitext(out_mp4)[0] + ".lrc"
    src_snap = os.path.splitext(out_mp4)[0] + ".src.lrc"
    try:
        io.open(src_snap, "w", encoding="utf-8", newline="").write(src_lrc)
        io.open(lrc_snap, "w", encoding="utf-8", newline="").write(lrc or "")
    except OSError as e:
        log("  ⚠ 歌词快照落盘失败（QC 将无法比对文本）：%s" % e)
        lrc_snap = src_snap = None

    # ── 3.9 渲染前闸门（快速失败）──────────────────────────────────────────
    # 4K60 单首渲染约 14 分钟。以下任一条件成立时，成片**必然**是废片或错片，
    # 与其渲完才发现（浪费 14 min + 35 GB 临时空间），不如在渲染前就拦下来。
    # 判据全部复用已有常量，不新增阈值；每条都给出可照做的修复动作。
    _gate_fail, _gate_warn = _pre_render_gate(
        dur=dur, lrc=lrc, linfo=linfo, tl_src=tl_src,
        cov=linfo.get("lyrics_coverage"), date=date, disp=disp)
    if _gate_fail and not GATE_IGNORE:
        raise PreRenderGateError(
            "%s：渲染前闸门未通过 ——\n      %s\n    "
            "→ 修复后 --force 重渲；确认该片可接受时用 --ignore-gate 放行"
            % (disp, "\n      ".join(_gate_fail)))
    if _gate_fail and GATE_IGNORE:
        log("  ⚠ --ignore-gate 已指定，闸门降级为警告（成片可能不符规格）")
    for _w in _gate_warn:
        log("  ⚠ " + _w)

    # 3.8 标题装饰方案自动匹配（音频节奏 + 歌词意象 + 封面色调，四维权衡；--scheme 可手动覆盖）
    dec = decor_pick.pick(seg["title_guess"], artist, lrc or "", cover_path, out_mp3,
                          force=scheme_override)
    log("  装饰方案: %d %s（%s）" % (dec["scheme"], dec["name"], dec["reason"]))

    # 4. 播放器离线渲染（4K60 逐帧，WebCodecs 硬编/软编自动选型）
    import base64
    cover_data = None
    if cover_path and os.path.exists(cover_path):
        head = io.open(cover_path, "rb").read(4)
        mime = "image/png" if head == b"\x89PNG" else "image/jpeg"
        cover_data = "data:%s;base64,%s" % (mime, base64.b64encode(io.open(cover_path, "rb").read()).decode())
    # ⚠ 中间产物不再落进 cuts/<date>/：与成片物理隔离，崩溃后一眼可辨、可整体清理
    render_tmp = os.path.join(render_tmp_dir(workdir, date),
                              os.path.basename(out_mp4) + ".render.mp4")
    job = {
        "playerDir": _PLAYER_DIR,
        "audio": out_mp3,
        "title": seg["title_guess"],
        "artist": artist,   # artist 已在上方按「segments 显式标注 > 歌词站」取值
        "trackLabel": seg.get("track_label") or "",
        "romaji": seg.get("romaji") or "",
        "vocal": seg.get("vocal") or CFG.path("streamer", "name", default=""),   # 演唱者（档案行 VOCAL）
        "producer": seg.get("producer") or "",         # P 主（术力口曲必填，非术力口留空）
        "tags": tags,
        "note": seg.get("note") or "",              # 歌曲注释（segments JSON 的 note 字段）
        "footerLeft": footer_left_text(),          # 左下角档案号（<前缀>·P.N）
        "footerRight": date,
        "badge": CFG.path("streamer", "badge", default="") or "",   # 刊眉带品牌签名
        "mark": CFG.path("streamer", "mark", default="") or "",      # 品牌短标记（题饰/封面占位）
        "sticker": CFG.expand(CFG.path("assets", "sticker", default="") or ""),
        "watermark": CFG.expand(CFG.path("assets", "watermark", default="") or ""),
        "lrc": lrc,
        "cover": cover_data,
        "artImage": _art_dataurl(),
        "scheme": dec["scheme"],          # 标题装饰（自动匹配/手动覆盖）
        "res": OUT_RES,                   # 1=1920×1080，2=3840×2160
        "bitrate": OUT_BITRATE,           # 成片视频码率(bps)
        "encoder": OUT_ENCODER,           # ffmpeg（默认，4K）/ webcodecs（回退）
        "preset": OUT_PRESET,             # libx264 preset（仅 ffmpeg 通路 + vcodec=libx264）
        "vcodec": OUT_VCODEC,             # h264_nvenc（默认，硬编）/ libx264（CPU 回退）
        "pages": OUT_PAGES,               # 并行渲染实例数（默认 3）
        "duration": round(ce - cs, 2),      # 渲染前磁盘预检要用（估算 mjpeg 峰值）
        "out": render_tmp,
    }
    t0 = time.time()
    render_player_video(workdir, job, ffmpeg)

    # 5. 混流：渲染画面 + MP3 音轨 → 最终 MP4（视频流直拷，音轨 AAC 重编码）
    try:
        rc, _, err = run([ffmpeg, "-y", "-v", "error", "-i", render_tmp, "-i", out_mp3,
                          "-map", "0:v:0", "-map", "1:a:0",
                          "-c:v", "copy", "-c:a", "aac", "-b:a", OUT_AUDIO_BR,
                          "-movflags", "+faststart", "-shortest", out_mp4])
    finally:
        # 混流成功/失败都要收掉 .render.mp4：它是 0.6 GB 级的中间体，
        # 留在 _render_tmp 里会逐首累积（旧的 `if os.path.exists` 在 run() 抛异常时会漏掉）
        if not _rm_retry(render_tmp, log=log) and os.path.exists(render_tmp):
            log("  ⚠ 中间成片删不掉（可能被占用）：%s" % render_tmp)
        _prune_empty_tmp_dir(workdir, date)
    if rc != 0:
        raise RuntimeError("混流失败：%s" % err[-300:])
    log("  渲染+混流完成，耗时 %.0fs" % (time.time() - t0))
    tl_brief = None
    if sync.get("ok"):
        tl_brief = {k: sync[k] for k in ("slope", "onset_abs", "song_end_abs", "song_len",
                                         "rms", "med", "n", "sim", "corrected", "head")}
    _od = linfo.get("netease_duration_ms")
    if _od:
        _od = round(_od / 1000.0, 1)
        _dev = (ce - cs) / _od
        log("  原曲时长比对: 成片 %.1fs vs 原曲 %.1fs（%+.0f%%）" % (ce - cs, _od, (_dev - 1) * 100))
        if _dev > 1.10 or _dev < 0.93:
            log("  ⚠ 时长偏差超限（上限 +10%% / 下限 −7%%）：短了优先查前奏是否被掐"
                "（比对官方 LRC 首行偏移），长了查是否混入歌后闲聊；"
                "核定 seg.cut_start_abs/cut_end_abs 后 --force 重渲")
    return {"lrc_path": lrc_snap, "lrc_src_path": src_snap,
            "cut_start": cs, "cut_end": ce, "head_pad": round(head_pad, 3),
            "timeline_offset_s": (round(tl_offset, 2) if tl_offset is not None else None),
            "orig_duration_s": (round(linfo["netease_duration_ms"] / 1000.0, 1)
                                if linfo.get("netease_duration_ms") else None),
            "onset": round(onset_abs, 3) if onset_abs else None,
            "entry_shift": round(cs - ref["cut_start"], 3),
            "timeline_source": tl_src, "timeline": tl_brief,
            "lyrics_source": linfo.get("lyrics_source"),
            "cover_source": linfo.get("cover_source"),
            "scheme": dec["scheme"], "scheme_name": dec["name"],
            "scheme_reason": dec["reason"], "scheme_auto": dec["auto"],
            "scheme_signals": dec["signals"]}


# ---------------- 清理 ----------------
def cleanup_outputs(manifest, seg_dir, keep_mp3=False):
    """视频成品验证通过后，删除对应 MP3 与本次流程的临时文件（LLM 原始回复等），
    仅保留最终视频。返回删除文件数。"""
    removed = 0
    for s in manifest["songs"]:
        mp3 = s.get("mp3")
        if mp3 and os.path.exists(mp3) and s.get("specs_ok"):
            if keep_mp3:
                s["mp3_cleaned"] = False
                continue
            try:
                os.remove(mp3)
                s["mp3"] = None
                s["mp3_cleaned"] = True
                removed += 1
            except OSError as e:
                log("  MP3 删除失败（%s）：%s" % (mp3, e))
    # 残留渲染中间件（*.render.mp4）
    for s in manifest["songs"]:
        tmp = (s.get("mp4") or "") + ".render.mp4"
        if tmp.endswith(".render.mp4") and os.path.exists(tmp):
            try:
                os.remove(tmp)
                removed += 1
            except OSError:
                pass
    ymd = (manifest.get("date") or "").replace("-", "")
    if ymd:
        for f in glob.glob(os.path.join(seg_dir, "%s-*.json.raw.txt" % ymd)):
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
    return removed


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="直播日期 YYYY-MM-DD")
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR)
    ap.add_argument("--min-confidence", type=float, default=0.0)
    ap.add_argument("--limit", type=int, default=0, help="只切前 N 首（调试用）")
    ap.add_argument("--only", default="",
                    help="只重渲指定序号（1 起，逗号分隔，如 --only 2）。"
                         "与 --limit 不同：不受 --force 全场牵连，且保留 manifest 其余条目")
    ap.add_argument("--fast-copy", action="store_true", help="视频用流复制快速档（切点吸附关键帧）")
    ap.add_argument("--redetect", action="store_true", help="强制重跑 LLM 识别")
    ap.add_argument("--dry-run", action="store_true", help="只识别不切割")
    ap.add_argument("--refresh-kdocs", action="store_true", help="强制重拉在线歌单表（默认 12h 缓存）")
    ap.add_argument("--no-kdocs", action="store_true",
                    help="完全不读在线歌单表（全自动场景：歌名由转写 + 歌词联网核验自主判定）")
    ap.add_argument("--keep-mp3", action="store_true", help="保留 MP3（默认成品验证通过后自动删除）")
    ap.add_argument("--no-qc", action="store_true", help="跳过成片自检（默认每首都跑，约 15~20s）")
    ap.add_argument("--qc-strict", action="store_true",
                    help="自检不通过则以退出码 3 结束（供自动化流程判定是否继续）")
    ap.add_argument("--qc-deep", action="store_true",
                    help="自检启用深度项（V06 全片帧节奏扫描，约 +60s/首）")
    ap.add_argument("--lrc-override", default="",
                    help="直接用指定 LRC 渲染，跳过 CTC/ASR 对齐链（人工核定时间轴时用）")
    ap.add_argument("--force", action="store_true",
                    help="成品已存在也强制重渲（默认跳过；修时间轴/切点后复渲时用）")
    ap.add_argument("--res", type=int, default=2, choices=(1, 2),
                    help="输出倍率：2=3840×2160（默认，4K60），1=1920×1080")
    ap.add_argument("--bitrate", type=int, default=18_000_000,
                    help="成片视频码率 bps（默认 18000000；B站真 4K 不二压区间 16000~18500 kbps）")
    ap.add_argument("--perf-no", type=int, default=None,
                    help="第 N 次演唱编号 → 左下角档案号写为 <前缀>·P.N（前缀见 config.json）")
    ap.add_argument("--encoder", default="ffmpeg", choices=("ffmpeg", "webcodecs"),
                    help="视频编码通路：ffmpeg=画布 JPEG 流交给 libx264（默认，4K 必需）；"
                         "webcodecs=页面内编码（Chromium 码率控制饱和，仅适合 1080P 回退）")
    ap.add_argument("--preset", default="fast",
                    help="libx264 preset（仅 --encoder ffmpeg + --vcodec libx264 生效，默认 fast）")
    ap.add_argument("--vcodec", default="h264_nvenc", choices=("h264_nvenc", "libx264"),
                    help="H.264 编码器：h264_nvenc=NVENC 硬编（默认，实测 4K60 比 x264 fast 快 3.3×，"
                         "PSNR/SSIM 与 x264 持平）；libx264=CPU 软编回退")
    ap.add_argument("--pages", type=int, default=None,
                    help="并行渲染实例数（默认 2。端到端实测 P=2 两阶段最优：≥2 走先渲染落盘再并行编码，"
                         "P=3 起与编码进程互拖反而变慢）。显式给出时不作任何自动降级；"
                         "P=1 为低占用模式（帧不落盘，约 1.8 GB 峰值，但慢约 33%%）")
    ap.add_argument("--auto-pages", action="store_true",
                    help="按实测磁盘空间自动选档：空间够 → P=2（速度优先，约 35 GB 峰值）；"
                         "不够 → 自动降级 P=1（约 1.8 GB 峰值，慢约 33%%）；连 P=1 都不够则渲染前直接报错。"
                         "同时给了 --pages 时以 --pages 为准")
    ap.add_argument("--raw-cut", action="store_true", help="旧档：直接切源视频画面（不做播放器渲染）")
    ap.add_argument("--ignore-gate", action="store_true",
                    help="渲染前闸门只警告不拦截（应急放行；成片可能不符规格）")
    ap.add_argument("--allow-no-lrc", action="store_true",
                    help="放行「未取到歌词」的片（默认按铁律直接拦下，不出无字幕片）")
    ap.add_argument("--scheme", type=int, default=None,
                    help="手动指定标题装饰方案 0~4（流光渐变/描边镂空/霓虹柔光/色块高亮/双色错位），"
                         "缺省则由歌曲意境自动匹配")
    args = ap.parse_args()

    global OUT_RES, OUT_BITRATE, PERF_NO, OUT_ENCODER, OUT_PRESET, LRC_OVERRIDE, OUT_VCODEC
    global OUT_PAGES, AUTO_PAGES, GATE_IGNORE, GATE_ALLOW_NO_LRC
    GATE_IGNORE = bool(getattr(args, "ignore_gate", False))
    GATE_ALLOW_NO_LRC = bool(getattr(args, "allow_no_lrc", False))
    OUT_RES, OUT_BITRATE = args.res, args.bitrate
    OUT_ENCODER, OUT_PRESET = args.encoder, args.preset
    LRC_OVERRIDE = getattr(args, "lrc_override", "") or ""
    OUT_VCODEC = args.vcodec
    # 优先级：显式 --pages > --auto-pages > 默认 P=2。无显式指定时保持历史默认行为。
    OUT_PAGES, AUTO_PAGES = resolve_pages(getattr(args, "pages", None),
                                          getattr(args, "auto_pages", False))
    PERF_NO = args.perf_no

    # ---- 前置检查：配置缺失时给出可照做的提示，而不是在深处抛底层异常 ----
    need = []
    if not SRT_DIR:
        need.append(("srt_dir", "上游转写 SRT 所在目录"))
    if not REC_ROOT:
        need.append(("recording_root", "录播归档根目录（其下按日期建子目录）"))
    if need:
        print("配置不完整，无法开始：")
        for k, desc in need:
            print("  · %-16s %s" % (k, desc))
        print("")
        print("请复制 config.example.json 为 config.json 并填写上述项；")
        print("或用环境变量 SONGCUT_CONFIG 指向你的配置文件。")
        print(CFG.describe())
        return 2

    os.makedirs(args.workdir, exist_ok=True)
    open_log(args.workdir, args.date)
    log("=== 歌切任务开始：%s ===" % args.date)

    # 0. 在线歌单表（第3表）—— 权威歌名数据源
    if getattr(args, "no_kdocs", False):
        kdocs = None
        log("已指定 --no-kdocs：不读在线歌单表，歌名完全由转写识别 + 歌词联网核验判定")
    else:
        kdocs = fetch_kdocs_songs(args.workdir, force=args.refresh_kdocs)
    expected_titles = kdocs_titles_for(kdocs, args.date) if kdocs else []
    if expected_titles:
        log("统计表当日歌单（%d 首）：%s" % (len(expected_titles), "、".join(expected_titles)))
    elif kdocs:
        log("统计表中 %s 无唱歌记录 → 仅用转写识别" % args.date)
    else:
        log("歌单表不可用 → 回退原方案（本地歌单接地 + LLM 联网核对）")

    cfg = load_sum_cfg()
    _paths = cfg.get("paths") or {}
    ffmpeg = _paths.get("ffmpeg") or CFG.ffmpeg()
    ffprobe = _paths.get("ffprobe") or CFG.ffprobe()

    # 1. 找 SRT（当天可能多场，逐场处理）
    ymd = args.date.replace("-", "")
    srts = []
    if SRT_DIR and os.path.isdir(SRT_DIR):
        srts = [os.path.join(SRT_DIR, f) for f in os.listdir(SRT_DIR)
                if f.startswith(ymd) and f.lower().endswith(".srt")]
    if not srts:
        raise SystemExit(
            "找不到当天转写：%s\n"
            "  期望目录：%s\n"
            "  请确认 config.json 的 srt_dir 指向转写产物目录，"
            "且该目录下存在 %s*.srt（与视频 0 点对齐的整场时间轴）。"
            % (ymd, SRT_DIR or "（未配置）", ymd))
    srts.sort()
    log("找到 SRT %d 份：%s" % (len(srts), "；".join(os.path.basename(s) for s in srts)))

    seg_dir = os.path.join(args.workdir, "segments")
    os.makedirs(seg_dir, exist_ok=True)
    out_dir = os.path.join(args.workdir, "cuts", args.date)
    os.makedirs(out_dir, exist_ok=True)

    src = find_video(args.date)
    vinfo = ffprobe_json(ffprobe, src)
    vdur = float(vinfo["format"]["duration"])
    v = next(s for s in vinfo["streams"] if s["codec_type"] == "video")
    log("视频源：%s（%sx%s @%s，时长 %.0fs）" % (
        os.path.basename(src), v["width"], v["height"], v.get("avg_frame_rate"), vdur))

    qc_fail = 0          # 自检未通过计数（--qc-strict 时决定退出码）
    manifest = {"date": args.date, "source": src, "source_specs":
                {"width": v["width"], "height": v["height"], "fps": v.get("avg_frame_rate"),
                 "duration": round(vdur, 2)}, "songs": []}

    for si, srt_path in enumerate(srts, 1):
        tag = os.path.splitext(os.path.basename(srt_path))[0][:14]
        seg_json = os.path.join(seg_dir, "%s-%s.json" % (ymd, tag))
        log("--- 第 %d/%d 场：%s ---" % (si, len(srts), os.path.basename(srt_path)))
        data = detect_songs(cfg, args.date, srt_path, seg_json, force=args.redetect,
                            expected_titles=expected_titles)
        if expected_titles:
            cross_validate_titles(data["songs"], expected_titles)
            data["kdocs_expected"] = expected_titles
            io.open(seg_json, "w", encoding="utf-8", newline="").write(
                json.dumps(data, ensure_ascii=False, indent=2))
        all_songs = [s for s in data["songs"] if s["confidence"] >= args.min_confidence]
        songs = all_songs[:args.limit] if args.limit else list(all_songs)
        # --only：按**原始序号**挑歌（不受 --limit 影响），便于只重渲某一首
        only_set = set()
        if getattr(args, "only", ""):
            try:
                only_set = {int(x) for x in str(args.only).replace("，", ",").split(",") if x.strip()}
            except ValueError:
                raise SystemExit("--only 只接受数字序号，如 --only 2 或 --only 1,3")
            _bad = {n for n in only_set if n < 1 or n > len(all_songs)}
            if _bad:
                raise SystemExit("--only 序号越界：本场共 %d 首，收到 %s"
                                 % (len(all_songs), sorted(_bad)))
            _keep = []
            for i, s_ in enumerate(all_songs, 1):
                if i in only_set:
                    _keep.append(s_)
            songs = _keep
            log("--only %s → 本场只处理第 %s 首（共 %d 首）"
                % (args.only, sorted(only_set), len(songs)))
        log("本场识别 %d 首（置信度≥%.2f）" % (len(all_songs), args.min_confidence))

        # ⚠ 整场 SRT 只在**本场开头**解析一次（原先写在逐首循环里，N 首就重复解析
        # N 次同一份 150 KB 文本）。produce_one 只按切点切片引用它，不修改。
        srt_entries = parse_srt(srt_path)
        log("转写索引：%d 条（本场共用，不逐首重读）" % len(srt_entries))

        title_count = {}
        qc_fail = 0
        for idx, seg in enumerate(songs, 1):
            title = seg["title_guess"]
            title_count[title] = title_count.get(title, 0) + 1
            disp = title if title_count[title] == 1 else "%s(%d)" % (title, title_count[title])
            base = out_basename(args.date, sanitize(disp))   # 统一命名格式
            out_mp4 = os.path.join(out_dir, base + ".mp4")
            out_mp3 = os.path.join(out_dir, base + ".mp3")
            log("[%d/%d] %s  %s~%s  conf=%.2f" % (
                idx, len(songs), disp, seg["start_hms"], seg["end_hms"], seg["confidence"]))
            if args.dry_run:
                continue
            ref_extra = {}
            if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1048576 and not args.force:
                log("  成品已存在，跳过（--force 可强制重渲）")
            else:
                if args.raw_cut:
                    t0 = time.time()
                    seg["date"] = args.date
                    cut_one(ffmpeg, src, seg, out_mp4, out_mp3, fast_copy=args.fast_copy)
                    log("  原始切割完成，耗时 %.0fs" % (time.time() - t0))
                else:
                    try:
                        ref_extra = produce_one(ffmpeg, ffprobe, src, seg, out_dir, disp,
                                                args.date, args.workdir,
                                                srt_entries=srt_entries,
                                                scheme_override=args.scheme)
                    except PreRenderGateError as e:
                        # 渲染前就被拦下：本首不出片，但**不牵连同场其他首**
                        log("  ✗ " + str(e).replace("\n", "\n  "))
                        qc_fail += 1
                        manifest["songs"].append({
                            "title": disp, "title_guess": seg["title_guess"],
                            "start": seg["start_hms"], "end": seg["end_hms"],
                            "confidence": seg["confidence"],
                            "mp4": None, "specs": {}, "specs_ok": False,
                            "renderer": "blocked-by-gate",
                            "gate_blocked": str(e)[:400],
                        })
                        continue
            specs = verify_specs(ffprobe, out_mp4)
            ok = (specs["width"] == 1920 * OUT_RES and specs["height"] == 1080 * OUT_RES
                  and specs["fps"] >= 59.9
                  and specs["video_bitrate_kbps"] >= MIN_VIDEO_KBPS)   # B站二压防护：码率下限
            if ref_extra:
                want = ref_extra["cut_end"] - ref_extra["cut_start"] + ref_extra.get("head_pad", 0)
                if abs(specs["duration"] - want) > 1.5:
                    ok = False
                    log("  ⚠ 成片时长 %.2fs 与音频 %.2fs 偏差超 1.5s" % (specs["duration"], want))
            log("  规格：%sx%s @%.2ffps %s %d kbps / %s %d kbps %.1fs %.0fMB %s" % (
                specs["width"], specs["height"], specs["fps"], specs["video_codec"],
                specs["video_bitrate_kbps"], specs["audio_codec"], specs["audio_bitrate_kbps"],
                specs["duration"], specs["size_mb"],
                "✓" if ok else "✗ 规格不符"))
            entry = {
                "title": disp, "title_guess": seg["title_guess"],
                "title_source": seg.get("title_source", "llm"),
                "start": seg["start_hms"], "end": seg["end_hms"],
                "confidence": seg["confidence"], "evidence": seg.get("evidence", []),
                "mp4": out_mp4, "mp3": out_mp3, "specs": specs, "specs_ok": ok,
                "renderer": ("raw-cut" if args.raw_cut
                             else ("player-4k60" if OUT_RES >= 2 else "player-1080p60")),
                **ref_extra,
            }
            # ── 成片自检（判定化）：只解码成片，不重渲。失败项带帧号/时间码/行号 + 修复建议 ──
            if QC and not getattr(args, "no_qc", False):
                try:
                    rep = QC.run_qc(out_mp4, entry=entry, ffmpeg=ffmpeg,
                                    deep=getattr(args, "qc_deep", False))
                    entry["qc"] = {"passed": rep["passed"], "elapsed_s": rep["elapsed_s"],
                                   "n_block": len(rep["blockers"]), "n_warn": len(rep["warnings"]),
                                   "blockers": [{"id": b["id"], "name": b["name"],
                                                 "detail": b["detail"], "fix": b["fix"]}
                                                for b in rep["blockers"]]}
                    log("  自检：%s（%d BLOCK / %d WARN，%.0fs，未重渲）" % (
                        "通过 ✓" if rep["passed"] else "不通过 ✗",
                        len(rep["blockers"]), len(rep["warnings"]), rep["elapsed_s"]))
                    for b in rep["blockers"]:
                        log("    ✗ %s %s：%s" % (b["id"], b["name"], b["detail"][:150]))
                        log("      修复：%s" % (b["fix"] or "")[:200])
                    if not rep["passed"]:
                        qc_fail += 1
                except Exception as e:
                    log("  ⚠ 自检运行失败（不影响出片）：%s" % str(e)[:160])
            elif not QC:
                log("  自检：不可用（songcut.qc 导入失败：%s）" % str(_QC_IMPORT_ERR)[:120])
            manifest["songs"].append(entry)

    # 4. 清理：视频验证通过后删除 MP3 与临时文件（--keep-mp3 可保留）
    if not args.dry_run and manifest["songs"]:
        n_clean = cleanup_outputs(manifest, seg_dir, keep_mp3=args.keep_mp3)
        if args.keep_mp3:
            log("保留 MP3（--keep-mp3）")
        elif n_clean:
            log("清理完成：%d 个 MP3/临时文件已删除，仅保留视频成品" % n_clean)
        else:
            log("无待清理文件")

    mf = os.path.join(out_dir, "manifest.json")
    # ⚠ 局部重渲（--limit/--only）时，manifest 只含本轮处理的歌 → 其余条目会被抹掉，
    #   而 manifest 是重切的唯一钥匙（2026-10-08 连踩 3 次后才修）。
    #   规则：若本轮只处理了部分歌曲，则从旧 manifest 里把「未处理且仍在磁盘上」的条目原样带回。
    _partial = False
    try:
        _allseg = []
        for _f in sorted(os.listdir(seg_dir)):
            if _f.endswith(".json") and _f.startswith(ymd):
                try:
                    _d = json.load(io.open(os.path.join(seg_dir, _f), encoding="utf-8"))
                    _allseg += [s for s in (_d.get("songs") or [])
                                if s.get("confidence", 0) >= args.min_confidence]
                except Exception:
                    pass
        _done = {s["title"] for s in manifest["songs"]}
        _carry = [s for s in _allseg
                  if s.get("title_guess") not in _done and s.get("title_guess")]
        if _carry:
            _old = {}
            if os.path.exists(mf):
                try:
                    for s in json.load(io.open(mf, encoding="utf-8")).get("songs", []):
                        _old[s.get("title")] = s
                except Exception:
                    _old = {}
            for _c in _carry:
                _t = _c["title_guess"]
                _prev = _old.get(_t)
                # 带回上一轮的完整条目（带 lrc_path/cut_start 等），而不是裸 segments 数据
                manifest["songs"].append(_prev if _prev else {
                    "title": _t, "title_guess": _t,
                    "title_source": _c.get("title_source", "llm"),
                    "start": _c.get("start_hms"), "end": _c.get("end_hms"),
                    "confidence": _c.get("confidence"), "evidence": _c.get("evidence", []),
                    "mp4": os.path.join(out_dir, out_basename(
                        args.date, sanitize(_t)) + ".mp4"),
                    "specs": (_prev or {}).get("specs", {}),
                    "specs_ok": (_prev or {}).get("specs_ok", False),
                    "renderer": (_prev or {}).get("renderer", ""),
                })
            _partial = True
            log("⚠ 局部重渲：已从旧 manifest 带回 %d 首未处理的条目（%s）"
                % (len(_carry), "、".join(c["title_guess"] for c in _carry)))
    except Exception as e:
        log("  ⚠ manifest 带入未处理条目失败（%s）：%s" % (type(e).__name__, str(e)[:120]))

    io.open(mf, "w", encoding="utf-8", newline="").write(
        json.dumps(manifest, ensure_ascii=False, indent=2))
    n_ok = sum(1 for s in manifest["songs"] if s["specs_ok"])
    n_qc = sum(1 for s in manifest["songs"] if (s.get("qc") or {}).get("passed"))
    log("=== 完成：共 %d 首，规格达标 %d 首，自检通过 %d 首；manifest → %s ===" % (
        len(manifest["songs"]), n_ok, n_qc, mf))
    if getattr(args, "qc_strict", False) and qc_fail:
        log("⚠ 严格模式：%d 首未通过自检，退出码 3" % qc_fail)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
