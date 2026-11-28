"""歌词中文翻译：获取、校验、对齐。

设计原则（对应 RULES §160）：
1. **只取真实译文，绝不伪造**。来源为网易云歌词接口的 tlyric 字段（平台侧用户投稿译文）。
2. **同名不是同一首歌**：实测「またあした」有 3 个候选版本，其中 2 个是同名不同曲，
   原文相似度只有 0.02。因此必须用「原文歌词正文相似度」判定版本，阈值 0.75。
3. **译文必须真的是中文**：排除越南语/英语/罗马音投稿（lrclib 就有日越混排的先例）。
4. **对齐不依赖时间戳数值**：不同版本的切句与起唱时间都不同，按行序做序列对齐
   （原文↔原文），再把译文挂到对齐结果上。匹配不上的行留空，不做猜测。
5. 任何一步失败都返回 None，调用方照常只显示原文。
"""
from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://music.163.com"}
TIMEOUT = 15
SIM_THRESHOLD = 0.75     # 原文正文相似度下限：低于此判定为「同名不同曲」
COVERAGE_MIN = 0.5       # 至少一半的行能对上译文，否则整首放弃

_LINE_RE = re.compile(r"^\[(\d+):(\d+)(?:[.:](\d+))?\]")


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """LRC → [(秒, 正文)]。忽略 [by:]/[ti:] 等元信息与空行。"""
    out: list[tuple[float, str]] = []
    for raw in (text or "").splitlines():
        m = _LINE_RE.match(raw.strip())
        if not m:
            continue
        mm, ss, frac = int(m.group(1)), int(m.group(2)), m.group(3) or "0"
        sec = mm * 60 + ss + int(frac.ljust(3, "0")[:3]) / 1000.0
        body = raw.strip()[m.end():]
        body = re.sub(r"<\d+:\d+(?:[.:]\d+)?>", "", body)   # 行内逐字时间标签
        body = re.sub(r"\[\d+:\d+(?:[.:]\d+)?\]", "", body)
        body = body.replace("\u3000", " ").strip()
        if body:
            out.append((sec, body))
    return out


def _clean(s: str) -> str:
    """比对用：去掉标点、空白、拉丁大小写差异与常见装饰。"""
    s = re.sub(r"<[^>]*>", "", s)
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[!-/:-@\[-`{-~、。，．・…—–~〜「」『』（）()《》〈〉·“”‘’]", "", s)
    return s.lower()


def body_similarity(a: str, b: str) -> float:
    """两段歌词正文的整体相似度（0~1）。"""
    ca, cb = _clean(a), _clean(b)
    if not ca or not cb:
        return 0.0
    return difflib.SequenceMatcher(None, ca, cb).ratio()


def is_chinese(text: str) -> bool:
    """判断一段文字是否为中文译文：汉字为主，且几乎不含假名。"""
    if not text.strip():
        return False
    han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    kana = sum(1 for c in text if "\u3040" <= c <= "\u30ff")
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    meaningful = han + kana + latin
    if meaningful < 8:
        return False
    return han / meaningful > 0.55 and kana / max(meaningful, 1) < 0.03


def _netease_search(title: str, limit: int = 8) -> list[dict]:
    r = requests.post("https://music.163.com/api/cloudsearch/pc",
                      data={"s": title, "type": 1, "limit": limit},
                      headers=HEADERS, timeout=TIMEOUT)
    songs = (r.json().get("result") or {}).get("songs") or []
    out = []
    for s in songs:
        out.append({"id": s.get("id"), "name": s.get("name"),
                    "artist": ((s.get("ar") or [{}])[0] or {}).get("name") or "",
                    "dur": s.get("dt") or 0})
    return out


def _netease_lyrics(mid) -> tuple[str, str]:
    rr = requests.get("http://music.163.com/api/song/lyric",
                      params={"id": mid, "lv": 1, "kv": 1, "tv": -1},
                      headers=HEADERS, timeout=TIMEOUT)
    j = rr.json()
    return (((j.get("lrc") or {}).get("lyric") or ""),
            ((j.get("tlyric") or {}).get("lyric") or ""))


def align_by_text(ours: list[tuple[float, str]],
                  cand: list[tuple[float, str]]) -> dict[int, int]:
    """按行文本做序列对齐：ours 下标 → cand 下标。只返回一对一匹配的块。"""
    a = [_clean(t) for _, t in ours]
    b = [_clean(t) for _, t in cand]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    mapping: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            mapping[i1 + k] = j1 + k
    return mapping


def _tr_by_index(cand: list[tuple[float, str]],
                 cand_tr: list[tuple[float, str]], tol: float = 1.2) -> dict[int, str]:
    """译文行按时间戳就近挂到候选原文行上（同版本的译文行与原文行时间几乎相同）。"""
    out: dict[int, str] = {}
    used: set[int] = set()
    for j, (t, _) in enumerate(cand):
        best, best_d = None, tol
        for k, (tt, _) in enumerate(cand_tr):
            if k in used:
                continue
            d = abs(tt - t)
            if d <= best_d:
                best, best_d = k, d
        if best is not None:
            used.add(best)
            out[j] = cand_tr[best][1]
    return out


def find_translation(title: str, our_lrc_text: str,
                     artist_hint: str = "") -> dict | None:
    """为「我方轴」找中文译文。

    返回 {"lines": {行下标: 中文}, "meta": {...}}，找不到返回 None。
    绝不返回部分猜测结果：覆盖率不足整首放弃。
    """
    ours = parse_lrc(our_lrc_text)
    if len(ours) < 4:
        return None
    our_body = "".join(t for _, t in ours)

    try:
        cands = _netease_search(title)
    except Exception:
        return None
    if not cands:
        return None

    best = None
    for c in cands:
        try:
            lrc, tr = _netease_lyrics(c["id"])
        except Exception:
            continue
        if not lrc or not tr:
            continue
        cand = parse_lrc(lrc)
        if len(cand) < 4:
            continue
        sim = body_similarity(our_body, "".join(t for _, t in cand))
        if sim < SIM_THRESHOLD:
            continue            # 同名不同曲，直接排除
        cand_tr = parse_lrc(tr)
        if not cand_tr:
            continue
        tr_text = "".join(t for _, t in cand_tr)
        if not is_chinese(tr_text):
            continue            # 不是中文译文（越南语/罗马音/英文投稿）
        mapping = align_by_text(ours, cand)
        if len(mapping) < len(ours) * COVERAGE_MIN:
            continue
        tr_idx = _tr_by_index(cand, cand_tr)
        # 第二遍：切句方式不同的行按「时间窗内一对一」补齐（±1s、单调、只用未占用的行）。
        # 只在原文相似度已确认同版本时启用，避免把别处的句子搬过来。
        used_j = set(mapping.values())
        last_j = -1
        for i in range(len(ours)):
            if i in mapping:
                last_j = mapping[i]
                continue
            t = ours[i][0]
            t_hi = ours[i + 1][0] - 0.05 if i + 1 < len(ours) else t + 6.0
            best, bd = None, 1.0
            for j in range(last_j + 1, len(cand)):
                if j in used_j or j not in tr_idx:
                    continue
                ct = cand[j][0]
                if not (t - 1.0 <= ct <= t_hi):
                    continue
                d = abs(ct - t)
                if d <= bd:
                    best, bd = j, d
            if best is not None:
                mapping[i] = best
                used_j.add(best)
                last_j = best
        lines = {}
        for i, j in mapping.items():
            # 整段译文已在上面判定为中文；逐行不再判，否则「我说」「找到的」这类短句会被误杀
            zh = tr_idx.get(j)
            if zh:
                lines[i] = zh
        if len(lines) < len(ours) * COVERAGE_MIN:
            continue
        cand_meta = {"netease_id": c["id"], "name": c["name"],
                     "artist": c["artist"], "similarity": round(sim, 4),
                     "lines": len(lines), "of": len(ours)}
        if best is None or cand_meta["lines"] > best["meta"]["lines"]:
            best = {"lines": lines, "meta": cand_meta}
    return best


def to_lrc(ours_lrc_text: str, lines: dict[int, str]) -> str:
    """把译文写回 LRC：行序与时间戳完全沿用我方轴，只替换正文。"""
    ours = parse_lrc(ours_lrc_text)
    out = []
    for i, (t, _) in enumerate(ours):
        if i not in lines:
            continue
        mm = int(t // 60)
        rest = t - mm * 60
        out.append("[%02d:%05.2f]%s" % (mm, rest, lines[i]))
    return "\n".join(out) + "\n"


def fetch_and_cache(title: str, our_lrc_text: str, cache_dir: Path,
                    key: str, artist_hint: str = "") -> dict | None:
    """取译文并写入缓存 JSON（幂等：命中即复用，除非 force）。"""
    cp = cache_dir / ("_tr_%s.json" % key)
    if cp.exists():
        try:
            d = json.loads(cp.read_text(encoding="utf-8"))
            if d.get("lrc"):
                return d
        except Exception:
            pass
    got = find_translation(title, our_lrc_text, artist_hint)
    if not got:
        return None
    rec = {"title": title, "lrc": to_lrc(our_lrc_text, got["lines"]),
           "meta": got["meta"], "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    cp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec
