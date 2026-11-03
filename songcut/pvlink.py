# -*- coding: utf-8 -*-
"""pvlink —— 歌切侧与文字 PV 侧之间唯一的接入层（2026-11-03）。

为什么要有这一层
----------------
本仓库其实是两条链共用一个根目录：

    歌切链（权威数据方）
        auto_cut.py / song_cutter.py / songcut/*
        产物：segments/<日期>-<场次>.json（切点·原唱·封面·剔行·证据）
              _media_cache/_lrc_*.json（歌词 + 封面缓存）
              cuts/<日期>/…（成片、live/、jz/）

    文字 PV 链（消费方）
        tools/pv_one.py、tools/pv_live.py、renderer/player/pv/*
        需要：源录播、成片窗口、原唱、歌词、封面、人物序列

2026-11-03 之前，PV 侧只能靠「人把切点抄进命令行」：``--start/--end`` 手填；
ffmpeg 路径在 20 个文件里各写一份；录播源自己 glob（``pv_live.py`` 取
``sorted(os.listdir())[0]``，连压缩残件都不排除）；歌词自己再联网抓一遍。
同一份事实存在两处，改一处必然漏另一处 —— 这正是 RULES §94 的形态。

这一层只做三件事，**不引入新的渲染路径**：

1. **只读地复用歌切产物**：segments / _media_cache / cuts 全部按 config 定位，
   不复制、不改写；PV 侧要的字段一次给全（``bundle``）。
2. **收敛运行时与几何参数**：ffmpeg / ffprobe / node、人物画框几何在这里
   各只有一份；工具里不再写 ``D:/ffmpeg/bin/ffmpeg.exe``，也不再各自维护
   一套 head/body/face。
3. **给出「能不能起跑」的判据**：每个素材包都带 ``missing`` 清单；
   ``doctor`` 一条命令把「源没了 / 封面没了 / 歌词没缓存 / 人物序列没抽」
   摊开，而不是等渲到第 3000 帧才发现没人。

不做什么
--------
不做渲染、不做切点判定、不写 segments。歌切侧仍然是唯一真源。
本模块的所有函数都不修改库内文件；唯一会写盘的是 ``ensure_lrc(online=True)``
—— 那是「缓存未命中时联网补一份」的既有行为，落点仍是 ``_media_cache``。

用法
----
>>> from songcut import pvlink as PL
>>> b = PL.bundle("2026-11-01", "小丑的品格")
>>> b["window"], b["artist"], b["missing"]
>>> PL.source("2026-11-01")          # 复用 song_cutter.find_video 的判定
>>> PL.runtime()["ffmpeg"]

命令行入口见 ``tools/pvlink.py``（sessions / list / show / bundle / doctor / pv-one）。
"""
import glob
import io
import json
import os
import re
import sys
import unicodedata

try:
    from songcut import config as CFG
except ImportError:                                  # 直接以脚本方式跑时兜底
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from songcut import config as CFG

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class PvLinkError(RuntimeError):
    """素材包取不到 / 歌名无法定位。消息里必须带「下一步该做什么」。"""


# ── 目录 ────────────────────────────────────────────────────────────────
def workdir():
    """库根（config.workdir，缺省=仓库根）。所有产物目录都在它下面。"""
    return CFG.expand(CFG.get("workdir") or "") or ROOT


def dirs():
    w = workdir()
    return {
        "workdir": w,
        "segments": os.path.join(w, "segments"),
        "cuts": os.path.join(w, "cuts"),
        "media_cache": os.path.join(w, "_media_cache"),
        "render_tmp": os.path.join(w, "_render_tmp"),
        "srt": CFG.expand(CFG.get("srt_dir") or ""),
        "recording_root": CFG.expand(CFG.get("recording_root") or ""),
    }


# ── 运行时（ffmpeg / ffprobe / node）────────────────────────────────────
def runtime():
    """外部可执行文件与 Node 依赖路径。环境变量优先，其次 config，最后内置探测。

    以前 ``tools/*.py`` 各自写 ``D:/ffmpeg/bin/ffmpeg.exe``（20 处），
    换一台机器就要全局替换；这里收成一份，键名与历史环境变量保持一致。
    """
    node = ""
    for c in CFG.node_candidates():
        if c and (os.path.sep in c or os.path.exists(c)):
            node = c
            break
    return {
        "ffmpeg": os.environ.get("SONGCUT_FFMPEG") or CFG.ffmpeg(),
        "ffprobe": os.environ.get("SONGCUT_FFPROBE") or CFG.ffprobe(),
        "node": node or "node",
        "node_path": os.environ.get("SONGCUT_NODE_PATH") or CFG.node_modules_path() or "",
        "python": sys.executable,
        "root": ROOT,
    }


# ── 人物画框几何（head / body / wide / face）───────────────────────────
# 两套实测值，都来自「1920×1080 源坐标」，差别在取景意图：
#   pv     —— tools/pv_live.py 用的那套：头窗 720 方图（脸占满圆窗），
#             半身 748×997 竖幅（贴着聊天栏右缘 592 起，刚好关在 UI 外）。
#   player —— tools/prepare_live.py 的历史默认（640 方图 + 580×772），
#             4K 圆窗直径约 1040px 时靠 1.6× 放大，肉一些但更稳。
# 以前这两套分散在两个文件里、且头窗偏移不同（620/0 vs 645/52），
# 单独跑 prepare_live 和经 pv_live 抽出来的画面取景并不一致。
GEOMETRY_PRESETS = {
    "pv": {
        "head": {"x": 620, "y": 0, "s": 720},
        "body": {"x": 592, "y": 0, "w": 748, "h": 997},
        "face": {"x": 965, "y": 372},
        "wide": {"x": 600, "y": 0, "w": 770, "h": 433},
    },
    "player": {
        "head": {"x": 645, "y": 52, "s": 640},
        "body": {"x": 675, "y": 0, "w": 580, "h": 772},
        "face": {"x": 965, "y": 372},
        "wide": {"x": 600, "y": 0, "w": 770, "h": 433},
    },
}
SEQUENCES = ("head", "body", "wide")      # 序列名只在这里出现一次（RULES §94 的判据）


def geometry(preset="pv", overrides=None):
    """取一套画框几何（深拷贝），可再叠加 config 的 pv.geometry 覆盖。"""
    base = GEOMETRY_PRESETS.get(preset)
    if base is None:
        raise PvLinkError("未知的画框几何预设：%r。可选：%s"
                          % (preset, "、".join(sorted(GEOMETRY_PRESETS))))
    out = json.loads(json.dumps(base))
    cfg_geo = CFG.path("pv", "geometry", default=None)
    for src in (cfg_geo, overrides):
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update(v)
            else:
                out[k] = v
    return out


def sequence_names(live_json):
    """从 live.json 里读出真实的序列名（跳过 face / cut_start）。

    序列名由抽帧方决定（s1..s6 之后不再固定三种），任何工具都不许再写死
    ``("head","body","wide")`` —— 写死之后新序列一条都传不进去，且不报错。
    """
    if isinstance(live_json, str):
        live_json = json.load(io.open(live_json, encoding="utf-8"))
    return [k for k, v in (live_json or {}).items()
            if k not in ("face", "cut_start") and isinstance(v, dict) and v.get("dir")]


# ── 歌名规范化与匹配 ───────────────────────────────────────────────────
def norm_title(s):
    """歌名比较用的规范形：全半角、NBSP、波浪号、书名号、空白、大小写。

    RULES 里记过 16 首歌因 NBSP / 波浪号 / 全半角导致匹配全失联；比较前统一。
    注意：**只用于比较**，写文件名/画面仍用 segments 里的原字面。
    """
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = s.replace("\u00a0", " ").replace("\u3000", " ")
    for a, b in (("〜", "~"), ("～", "~"), ("－", "-"), ("‐", "-"), ("—", "-")):
        s = s.replace(a, b)
    s = re.sub(r"[「」『』《》〈〉\"'“”‘’]", "", s)
    s = re.sub(r"\s+", "", s)
    return s.strip().lower()


def _titles_of(entry):
    return [entry.get("title_guess") or "", entry.get("fetch_title") or ""]


# ── segments ───────────────────────────────────────────────────────────
def segments_files():
    """{date: [路径…]}。同一自然日可能有多场（凌晨续场 + 晚上主场）。"""
    out = {}
    pat = os.path.join(dirs()["segments"], "*.json")
    for p in sorted(glob.glob(pat)):
        try:
            d = json.load(io.open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        date = d.get("date") or ""
        if date:
            out.setdefault(date, []).append(p)
    return out


def sessions():
    """可用的场次清单：[{date, path, n_songs, srt}]，按日期倒序。"""
    out = []
    for date, paths in segments_files().items():
        for p in paths:
            try:
                d = json.load(io.open(p, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"date": date, "path": p,
                        "n_songs": len(d.get("songs") or []),
                        "srt": d.get("srt") or ""})
    out.sort(key=lambda x: (x["date"], x["path"]), reverse=True)
    return out


def load_segments(date):
    """读某天第一个可解析的 segments；多场时取歌曲数最多的那份并提示。"""
    paths = segments_files().get(str(date))
    if not paths:
        raise PvLinkError("没有 %s 的 segments（歌切侧还没跑，或 workdir 配错）。\n"
                          "  → 先跑 auto_cut.py / song_cutter.py 让歌切链落 segments，"
                          "或用 --start/--end 手工指定窗口。" % date)
    best = None
    for p in paths:
        try:
            d = json.load(io.open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if best is None or len(d.get("songs") or []) > len(best[1].get("songs") or []):
            best = (p, d)
    if best is None:
        raise PvLinkError("segments 全部无法解析：%s" % "、".join(paths))
    p, d = best
    d = dict(d)
    d["_path"] = p
    if len(paths) > 1:
        d["_multi"] = [os.path.basename(x) for x in paths]
    return d


def find_song(date, song="", seq=None):
    """在 segments 里定位一首歌。

    匹配顺序：序号 → 精确（title_guess / fetch_title）→ 包含 → 相似度。
    返回的是**副本**，额外带 ``_match``（怎么匹配上的）/ ``_index`` / ``_date`` /
    ``_segments``，便于调用方在日志里自证「取的是哪一条」，避免同天两场串台。
    """
    ses = load_segments(date)
    songs = ses.get("songs") or []
    if not songs:
        raise PvLinkError("%s 的 segments 里没有 songs" % date)

    def pick(entry, how):
        e = dict(entry)
        e["_match"] = how
        e["_date"] = str(date)
        e["_segments"] = ses["_path"]
        e["_index"] = songs.index(entry) + 1
        return e

    if seq:
        try:
            return pick(songs[int(seq) - 1], "seq")
        except (IndexError, ValueError):
            raise PvLinkError("%s 只有 %d 首，没有第 %s 首" % (date, len(songs), seq))
    if not song:
        if len(songs) == 1:
            return pick(songs[0], "only")
        raise PvLinkError("%s 有 %d 首，必须指定歌名：%s"
                          % (date, len(songs),
                             "、".join(s.get("title_guess") or "?" for s in songs)))
    n = norm_title(song)
    exact = [s for s in songs if n in [norm_title(t) for t in _titles_of(s)]]
    if exact:
        # 同日重复演唱同一首：默认取第一遍（与歌切链的“第一遍为准”一致），
        # 要第二遍请用 --seq。
        return pick(exact[0], "exact" if len(exact) == 1 else "exact(first of %d)" % len(exact))
    inside = [s for s in songs
              if any(n in norm_title(t) or norm_title(t) in n for t in _titles_of(s) if t)]
    if inside:
        return pick(inside[0], "contains")
    import difflib
    scored = []
    for s in songs:
        r = max([difflib.SequenceMatcher(None, n, norm_title(t)).ratio()
                 for t in _titles_of(s)] or [0.0])
        scored.append((r, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] >= 0.6:
        return pick(scored[0][1], "fuzzy:%.2f" % scored[0][0])
    raise PvLinkError("%s 的 segments 里找不到 %r（相似度最高 %.2f）。\n"
                      "  可用歌名：%s"
                      % (date, song, scored[0][0] if scored else 0.0,
                         "、".join(s.get("title_guess") or "?" for s in songs)))


# ── 源录播 ─────────────────────────────────────────────────────────────
_SRC_CACHE = {}


def source(date, explicit="", use_cutter=True, quiet=True):
    """定位当天录播 mp4/flv。

    优先复用 `song_cutter.find_video`（它排除了 ``.part`` 残件、会在日期目录缺文件时
    回退到录播根目录按场次日期匹配、并在多场时取最长的一场）。以前 PV 侧自己写
    ``sorted(os.listdir())[0]``，压缩中断时会挑到 moov 缺失的残件，整条链崩在 ffprobe。

    失败一律抛 PvLinkError 并说明「是不是被上游压缩删掉了」（RULES §86）。
    """
    if explicit:
        return os.path.abspath(explicit)
    key = (str(date), bool(use_cutter))
    if key in _SRC_CACHE:
        return _SRC_CACHE[key]

    buf = []
    if use_cutter:
        try:
            import contextlib
            import song_cutter as SC              # 懒导入：它是主链入口，导入有成本
            with contextlib.redirect_stdout(io.StringIO() if quiet else sys.stdout) as fh:
                try:
                    p = SC.find_video(str(date))
                finally:
                    if quiet:
                        buf = (fh.getvalue() or "").splitlines()
            _SRC_CACHE[key] = p
            return p
        except PvLinkError:
            raise
        except Exception as e:                    # 找不到/ffprobe 缺失 → 走自己的兜底
            buf.append("[pvlink] find_video 不可用：%r" % (e,))

    root = dirs()["recording_root"]
    if not root:
        raise PvLinkError("config.json 没有 recording_root，也没有 --src")
    exts = (".mp4", ".flv", ".mkv")
    cands = []
    for p in glob.glob(os.path.join(root, str(date), "*")):
        if p.lower().endswith(exts) and ".part" not in os.path.basename(p).lower():
            cands.append(p)
    ymd = re.sub(r"\D", "", str(date))
    for p in glob.glob(os.path.join(root, "*")):
        if (os.path.isfile(p) and p.lower().endswith(exts)
                and ".part" not in os.path.basename(p).lower() and ymd in re.sub(r"\D", "", os.path.basename(p))):
            cands.append(p)
    if not cands:
        raise PvLinkError(
            "%s 找不到录播：日期目录与录播根目录都没有可用文件。\n"
            "  常见原因：上游压缩完成后删掉了原始录播（RULES §86），当天只剩 _compressed.mp4 也被清理。\n"
            "  → 若已有成片，直接用成片；否则只有等下一场。目录：%s"
            % (date, os.path.join(root, str(date))))
    p = max(cands, key=lambda x: os.path.getsize(x))
    _SRC_CACHE[key] = p
    return p


# ── 歌词（复用歌切侧缓存，含人工剔行）──────────────────────────────────
def lrc_cache_path(title):
    """歌词缓存文件路径。key 规则与抓取侧一致（优先借 ``lyrics_fetch.cache_key``）。

    抓取模块依赖 requests 等网络库；缺失时（精简环境）退回同规则的正则，
    保证「只查缓存」这条路径在任何环境下都能用 —— PV 侧没网也该能出片。
    """
    try:
        from songcut import lyrics_fetch as LF
        key = LF.cache_key(title)
    except Exception:
        key = re.sub(r"[^\w]", "_", norm_title(title))[:40]
    return os.path.join(dirs()["media_cache"], "_lrc_%s.json" % key)


def lrc_cache(title):
    """``_media_cache/_lrc_<key>.json`` 命中则返回 (lrc, cover_path, info)。

    key 规则必须与歌词抓取侧完全一致，所以直接借 ``lyrics_fetch.cache_key``，
    不在这里重写一遍正则（重写就会像 10-09 那次一样，索引查不到缓存而退化成逐首联网）。
    """
    p = lrc_cache_path(title)
    if not os.path.isfile(p):
        return None
    try:
        d = json.load(io.open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (d.get("lrc") or d.get("cover_path")):
        return None
    return d.get("lrc") or "", d.get("cover_path") or "", d.get("info") or {}


def drop_lrc_lines(lrc_text, lines):
    """按歌词**非空行**序号剔除指定行（segments 的 lrc_drop_lines 就是这个口径）。

    专辑介绍语、制作人员名单混在 LRC 里是常态（《小丑的品格》《带我走》都剔过），
    歌切侧已经人工核定过行号，PV 侧不该再自己数一遍 —— 数错就是把制作名单当歌词唱。
    返回 (新文本, 实际剔掉的行内容列表, 没找到的行号列表)。
    """
    if not lrc_text or not lines:
        return lrc_text, [], []
    want = {int(x) for x in lines if str(x).strip().lstrip("-").isdigit()}
    keep, dropped, hit = [], [], set()
    idx = 0
    for raw in lrc_text.splitlines():
        if not raw.strip():
            keep.append(raw)
            continue
        idx += 1
        if idx in want:
            dropped.append(raw.strip())
            hit.add(idx)
        else:
            keep.append(raw)
    text = "\n".join(keep).rstrip("\n") + "\n"
    return text, dropped, sorted(want - hit)


def ensure_lrc(entry, online=False, apply_drops=True):
    """取这首歌的歌词：先查歌切侧缓存，miss 且 ``online=True`` 时才联网补。

    返回 dict：{text, source, cover, info, dropped, drop_missing, title}
    ``source`` ∈ cache / online / none —— 出问题时先看这行，别猜。
    """
    title = entry.get("fetch_title") or entry.get("title_guess") or ""
    out = {"text": None, "source": "none", "cover": "", "info": {},
           "dropped": [], "drop_missing": [], "title": title}
    hit = lrc_cache(title)
    if hit:
        out["text"], out["cover"], out["info"] = hit
        out["source"] = "cache"
    elif online:
        try:
            from songcut import lyrics_fetch as LF
            lrc, cover, info = LF.fetch_lyrics_and_cover(
                title, entry.get("artist") or "",
                dur=entry.get("orig_duration_s"),
                cache_dir=dirs()["media_cache"])
            out.update({"text": lrc, "cover": cover or "", "info": info or {},
                        "source": "online" if lrc else "none"})
        except Exception as e:
            out["error"] = repr(e)
    if apply_drops and out["text"] and entry.get("lrc_drop_lines"):
        out["text"], out["dropped"], out["drop_missing"] = drop_lrc_lines(
            out["text"], entry["lrc_drop_lines"])
    return out


# ── 素材包 ─────────────────────────────────────────────────────────────
def _window(entry):
    """(start, end, 来源)。cut_start_abs/cut_end_abs 必须成对，否则退回 ASR 粗窗口。

    RULES 的「人工核定通道」规定两个字段成对出现；只写了一边说明手改过又没改全，
    这种情况不能猜 —— 宁可退回粗窗口并把警告带出来。
    """
    cs, ce = entry.get("cut_start_abs"), entry.get("cut_end_abs")
    if cs is not None and ce is not None:
        return float(cs), float(ce), "cut_abs", ""
    warn = ""
    if (cs is None) != (ce is None):
        warn = "cut_start_abs/cut_end_abs 只写了一边，已退回粗窗口（start/end）"
    if entry.get("start") is not None and entry.get("end") is not None:
        return float(entry["start"]), float(entry["end"]), "rough", warn
    raise PvLinkError("这首歌既没有 cut_start_abs/cut_end_abs，也没有 start/end")


def bundle(date, song="", seq=None, online=False, with_source=False, with_lrc=True):
    """一首歌的完整素材描述 —— PV 侧起跑所需的全部输入，只读。

    字段（稳定契约，改动需同步 docs/integration-pvlink.md）::

        date / seq / title / fetch_title / artist
        window   {start, end, dur, source('cut_abs'|'rough'), warn}
        source   {path, exists, size_mb}
        cover    {path, exists}
        lrc      {text, source, dropped, drop_missing, lines}
        assets   {live, jz}（按 cuts/<date>/<歌名>/{live,jz}/ 约定探测）
        style_ok {live, jz}（该层素材是否齐）
        missing  人读的缺口清单
        ready    窗口 + 歌词 + 源 三者齐才算能起跑
    """
    entry = find_song(date, song, seq)
    st, en, wsrc, wwarn = _window(entry)
    d = dirs()
    title = entry.get("title_guess") or ""

    lc = {"text": None, "source": "off", "dropped": [], "drop_missing": []}
    if with_lrc:
        lc = ensure_lrc(entry, online=online)

    cover = entry.get("cover_path") or lc.get("cover") or ""
    cover = CFG.expand(cover) if cover else ""
    cover_ok = bool(cover and os.path.exists(cover))

    src = {"path": None, "exists": False, "size_mb": 0.0}
    if with_source:
        try:
            p = source(date)
            src = {"path": p, "exists": True,
                   "size_mb": round(os.path.getsize(p) / 1048576.0, 1)}
        except PvLinkError as e:
            src["error"] = str(e)

    try:
        from songcut import style as STYLE_SRC
        live, jz = STYLE_SRC.asset_paths(os.path.join(d["cuts"], str(date)), title)
    except Exception:
        live = jz = None

    missing = []
    if wwarn:
        missing.append(wwarn)
    if with_source and not src["exists"]:
        missing.append("源录播不可用：%s" % (src.get("error") or "未找到"))
    if with_lrc and not lc.get("text"):
        missing.append("歌词未命中缓存（%s）→ 用 --lrc 指定，或加 --online 联网补"
                       % lc.get("source"))
    if not cover_ok:
        missing.append("原唱封面缺失（画面标原唱需要）")
    if lc.get("drop_missing"):
        missing.append("lrc_drop_lines 里有行号没命中：%s" % lc["drop_missing"])

    return {
        "date": str(date),
        "seq": entry.get("_index"),
        "title": title,
        "fetch_title": entry.get("fetch_title") or title,
        "artist": entry.get("artist") or "",
        "match": entry.get("_match"),
        "segments": entry.get("_segments"),
        "window": {"start": st, "end": en, "dur": round(en - st, 3),
                   "source": wsrc, "warn": wwarn},
        "source": src,
        "cover": {"path": cover, "exists": cover_ok},
        "lrc": {"text": lc.get("text"), "source": lc.get("source"),
                "dropped": lc.get("dropped") or [],
                "drop_missing": lc.get("drop_missing") or [],
                "lines": len([x for x in (lc.get("text") or "").splitlines() if x.strip()])},
        "orig_duration_s": entry.get("orig_duration_s"),
        "assets": {"live": live, "jz": jz},
        "style_ok": {"live": bool(live), "jz": bool(jz)},
        "notes": {k: entry.get(k) for k in ("note", "note_technical", "evidence", "title_source")
                  if entry.get(k)},
        "missing": missing,
        "ready": bool(not any(x.startswith(("cut_start_abs", "源录播")) for x in missing)
                      and not lc.get("drop_missing")
                      and (not with_lrc or lc.get("text"))),
    }


# ── 自检 ───────────────────────────────────────────────────────────────
def doctor(date="", online=False, limit=0):
    """只读体检：运行时 / 目录 / 各场素材齐不齐。返回可 json 化的报告。"""
    d = dirs()
    rt = runtime()
    rep = {"workdir": d["workdir"], "config": CFG.source() or "(内置默认)",
           "runtime": {k: rt[k] for k in ("ffmpeg", "ffprobe", "node")},
           "runtime_ok": {k: bool(os.path.exists(rt[k]) if os.path.sep in rt[k]
                                  else _which(rt[k]))
                          for k in ("ffmpeg", "ffprobe", "node")},
           "dirs": {k: {"path": v, "exists": bool(v and os.path.isdir(v))}
                    for k, v in d.items()},
           "sessions": [], "summary": {}}

    ses = sessions()
    if date:
        ses = [s for s in ses if s["date"] == str(date)]
    if limit:
        ses = ses[:limit]

    n_ok = n_lrc = n_live = n_jz = 0
    for s in ses:
        row = {"date": s["date"], "segments": os.path.basename(s["path"]),
               "n_songs": s["n_songs"], "songs": []}
        try:
            src = source(s["date"])
            row["source"] = os.path.basename(src)
        except PvLinkError as e:
            row["source"] = None
            row["source_error"] = str(e).splitlines()[0]
        for e in (load_segments(s["date"]).get("songs") or []):
            b = bundle(s["date"], seq=e.get("seq"), online=online, with_source=False,
                       with_lrc=True)
            row["songs"].append({
                "seq": b["seq"], "title": b["title"], "artist": b["artist"],
                "window": [b["window"]["start"], b["window"]["end"]],
                "win_src": b["window"]["source"],
                "lrc": b["lrc"]["source"], "cover": b["cover"]["exists"],
                "live": b["style_ok"]["live"], "jz": b["style_ok"]["jz"],
                "missing": b["missing"],
            })
            n_lrc += 1 if b["lrc"]["text"] else 0
            n_ok += 1
            n_live += 1 if b["style_ok"]["live"] else 0
            n_jz += 1 if b["style_ok"]["jz"] else 0
        rep["sessions"].append(row)

    rep["summary"] = {"sessions": len(ses), "songs": n_ok, "with_lrc": n_lrc,
                      "with_live": n_live, "with_jz": n_jz}
    return rep


def _which(exe):
    import shutil
    return bool(shutil.which(exe))
