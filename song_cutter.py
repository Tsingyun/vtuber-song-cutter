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
OUT_PAGES = 2                      # 并行渲染实例数（端到端实测：P=3 与编码进程互拖仅 13 fps，P=2 两阶段 9.2 min 最优）
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


def _rm_retry(path, tries=4, sleep_s=0.4):
    """删文件，遇OSError 重试。

    ⚠ Windows 上删除十几 GB 的大文件会**偶发瞬时失败**（杀毒软件 /
      索引服务 / shell 删除钩子此刻仍持有句柄），等 0.4s 重试几乎必成。
      不重试会静默留下一个巨额文件 —— 实测 14.47 GB 只清掉一半。
      返回 True 表示确实删掉了。
    """
    import time as _t
    for i in range(tries):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if i + 1 < tries:
                _t.sleep(sleep_s)
    return False


def _cleanup_render_tmp(out_path):
    """删掉本次渲染留下的 mjpeg / 分段 mp4 / concat 清单（失败路径兜底）。"""
    import glob as _g

    def _esc(p):
        return p.replace("[", "[[]").replace("]", "[]]").replace("?", "[?]").replace("*", "[*]")

    n, freed, fail = 0, 0, 0
    base = _esc(out_path)
    for pat in (base + ".p*.mjpeg", base + ".p*.mp4", base + ".concat.txt", base + ".render.mp4"):
        for f in _g.glob(pat):
            try:
                sz = os.path.getsize(f)
            except OSError:
                sz = 0
            if _rm_retry(f):
                n += 1
                freed += sz
            else:
                fail += 1
                log("  ⚠ 中间产物删不掉（可能被占用）：%s（%.2f GB）" % (f, sz / 1024 ** 3))
    if n:
        log("  渲染失败已清理中间产物 %d 个，回收 %.2f GB" % (n, freed / 1024 ** 3))
    if fail:
        log("  ⚠ 仍有 %d 个中间产物未删净，磁盘不会立即释放" % fail)


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
            "**/*.render.mp4", "**/*.render.mp4.concat.txt")
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
            "（若磁盘未立即释放，是回收站占用，可用 gio/Recycle Bin 清空）"
            % (n, freed / 1024 ** 3))
    return freed


def render_player_video(workdir, job, ffmpeg=None):
    """调 render_song.cjs：无头浏览器离线逐帧渲染播放器画面并编码落盘。

    job.encoder = "ffmpeg"   → 画布逐帧 JPEG(q98) 流式交给 ffmpeg libx264 编码（4K 默认，
                               因为 Chromium 软件 H.264 码率控制饱和，到不了 B站 不二压区间）
    job.encoder = "webcodecs"→ 页面内 WebCodecs 编码后整片 POST 回落（1080P 回退档）
    """
    # 0) 先扫历史残留。**必须放在预检之前** —— 清出来的空间要算进可用额度，
    #    否则上一轮崩溃留下的十几 GB 会让预检误报「空间不足」。
    try:
        _sweep_stale_render_tmp(workdir, older_than_h=6.0, log=log)
    except Exception as _e:
        log("  残留扫描跳过：%s" % _e)

    # ── 磁盘预检（2026-10-08 修正）────────────────────────────────────
    # 实测依据（280s/4K60 那次渲染留下的残留文件，双源互证）：
    #   · cuts/2026-10-07/*.render.mp4.p{0,1}.mjpeg 共 14.47 GB，
    #     按 JPEG SOI(ffd8ff) 计数得 8816 帧 → **2.03 MB/帧**
    #   · 渲染日志 recvBytes 累计 13200 帧 / 27981 MB → 2.12 MB/帧
    #   ⇒ 取 2.2 MB/帧（含 HTTP/流控开销的余量）。
    #
    # ⚠ 旧公式 `pages × frames × 2.2MB` 有两个错，**不要再改回去**：
    #   ① **不该乘 pages**。两阶段是「分段落盘」——第 p 个实例只写自己那一段
    #      （render_song.cjs: `p = floor(idx / SEG_PER)`），每帧全程只写一次。
    #      乘 pages 等于凭空翻倍，与磁盘实测对不上。
    #   ② 漏了分段 mp4 与 concat 输出的 .render.mp4（各约 frames×br/8/fps）。
    #   两者相抵后旧公式整体**高估 2.1~2.2 倍**，造成两个方向都错：
    #   「明明够却报不足」白等清盘，「报了不足其实够」渲到一半爆盘。
    # 真实峰值 = mjpeg + 视频侧三份（分段×P + render.mp4 + 最终成片）。
    try:
        _d = float(job.get("duration") or 0)
        _frames = int(_d * 60) or 1
        _pages = max(1, int(job.get("pages") or 1))
        _mjpeg = _frames * 2.2 * 1024 ** 2          # 不乘 pages：分段落盘，每帧只写一次
        _video = _d * (OUT_BITRATE / 8.0) * 2.2# 分段 mp4 + render.mp4 + 成片
        _need = _mjpeg + _video + 0.5 * 1024 ** 3     # +0.5GB 音频/波形/对齐余量
        _tot, _used, _free = shutil.disk_usage(
            os.path.dirname(os.path.abspath(job["out"])) or ".")
        if _free < _need:
            raise RuntimeError(
                "磁盘空间不足：本次渲染实测需约 %.1f GB"
                "（mjpeg %.1f GB + 视频流 %.1f GB），当前可用仅 %.1f GB。"
                "请先清理（渲染中间产物通常在回收站里）"
                % (_need / 1024 ** 3, _mjpeg / 1024 ** 3, _video / 1024 ** 3,
                   _free / 1024 ** 3))
        log("  磁盘预检：需 %.1f GB（mjpeg %.1f + 视频 %.1f）/ 可用 %.1f GB ✓"
            % (_need / 1024 ** 3, _mjpeg / 1024 ** 3, _video / 1024 ** 3,
               _free / 1024 ** 3))
    except RuntimeError:
        raise
    except Exception:
        pass

    job_f = os.path.join(workdir, "_render_job.json")
    io.open(job_f, "w", encoding="utf-8", newline="").write(
        json.dumps(job, ensure_ascii=False))
    env = dict(os.environ)
    env["NODE_PATH"] = NODE_PATH_ENV
    if ffmpeg:
        env["FFMPEG_BIN"] = ffmpeg       # render_song.cjs 优先用本管线的 ffmpeg，避免找错
    cmd = [pick_node(), _RENDER_CJS, "--job", job_f]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=10800, env=env, creationflags=flags)
    for line in (p.stdout or "").splitlines():
        if line.strip():
            log("  [render] " + line.strip()[:160])
    if p.returncode != 0 or not os.path.exists(job["out"]):
        tail = ((p.stderr or "") + (p.stdout or "")).strip()[-400:]
        _cleanup_render_tmp(job["out"])       # 失败也要清理，否则满盘 mjpeg 会拖垮下一次
        raise RuntimeError("渲染失败 rc=%s：%s" % (p.returncode, tail))
    return job["out"]


def _art_dataurl():
    """装饰立绘 → dataURL（未配置或文件缺失返回 None，播放器自动跳过该图层）。"""
    if not STREAMER_ART or not os.path.exists(STREAMER_ART):
        if STREAMER_ART:
            log("警告：装饰立绘不存在，跳过该图层：%s" % STREAMER_ART)
        return None
    head = io.open(STREAMER_ART, "rb").read(4)
    mime = "image/png" if head == b"\x89PNG" else "image/jpeg"
    import base64 as _b64
    return "data:%s;base64,%s" % (mime, _b64.b64encode(io.open(STREAMER_ART, "rb").read()).decode())


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
    _hint = lookup_library_artist(seg["title_guess"])
    ref_text = ""
    if srt_entries:
        ref_text = "\n".join(t for (a, b, t) in srt_entries if cs - 2 <= a <= ce + 2)
    lrc, cover_path, linfo = lyrics_fetch.fetch_lyrics_and_cover(
        seg["title_guess"], artist_hint=_hint, dur=ce - cs, ref_text=ref_text,
        cache_dir=os.path.join(workdir, "_media_cache"))
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
    sync = timeline_sync.analyze(seg["title_guess"], artist, ffmpeg, src,
                                 onset_abs, cs, ce, lrc or "", workdir, log=log)
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
    render_tmp = out_mp4 + ".render.mp4"
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
    rc, _, err = run([ffmpeg, "-y", "-v", "error", "-i", render_tmp, "-i", out_mp3,
                      "-map", "0:v:0", "-map", "1:a:0",
                      "-c:v", "copy", "-c:a", "aac", "-b:a", OUT_AUDIO_BR,
                      "-movflags", "+faststart", "-shortest", out_mp4])
    if os.path.exists(render_tmp):
        try:
            os.remove(render_tmp)
        except OSError:
            pass
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
    ap.add_argument("--pages", type=int, default=2,
                    help="并行渲染实例数（默认 2。端到端实测 P=2 两阶段最优：≥2 走先渲染落盘再并行编码，"
                         "P=3 起与编码进程互拖反而变慢；只求稳可设 1）")
    ap.add_argument("--raw-cut", action="store_true", help="旧档：直接切源视频画面（不做播放器渲染）")
    ap.add_argument("--scheme", type=int, default=None,
                    help="手动指定标题装饰方案 0~4（流光渐变/描边镂空/霓虹柔光/色块高亮/双色错位），"
                         "缺省则由歌曲意境自动匹配")
    args = ap.parse_args()

    global OUT_RES, OUT_BITRATE, PERF_NO, OUT_ENCODER, OUT_PRESET, LRC_OVERRIDE, OUT_VCODEC, OUT_PAGES
    OUT_RES, OUT_BITRATE = args.res, args.bitrate
    OUT_ENCODER, OUT_PRESET = args.encoder, args.preset
    LRC_OVERRIDE = getattr(args, "lrc_override", "") or ""
    OUT_VCODEC, OUT_PAGES = args.vcodec, args.pages
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
                    ref_extra = produce_one(ffmpeg, ffprobe, src, seg, out_dir, disp,
                                            args.date, args.workdir,
                                            srt_entries=parse_srt(srt_path),
                                            scheme_override=args.scheme)
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
