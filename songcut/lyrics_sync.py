# -*- coding: utf-8 -*-
"""原曲音频 ↔ 翻唱音频 DTW 时间轴同步（网易云 VIP · 仅限个人使用）

=========================== 合规声明（务必阅读）===========================
1. 本模块只使用网易云官方 Web 接口，凭**本人账号 cookie** 拉取本人在该平台
   已获授权可播放/可下载的内容（VIP 音质、官方 LRC、VIP 逐字 klyric）。
2. **不做任何 DRM 破解**：不解密 .ncm / .uc! 等客户端加密缓存，不绕过付费校验，
   不使用第三方解析站。拿不到授权资源的歌曲一律返回 None，由调用方降级。
3. 派生出的音频/歌词仅供本人本地离线使用（如自制歌切字幕），**不得分发、上传
   或二次发布**；输出目录请保持私有。
4. cookie 只从本地文件读取，绝不硬编码进代码、不打印、不写日志。
=========================================================================

核心思路（为什么本项目用 DTW 而不是 ASR 对齐）
--------------------------------------------------------------------------
翻唱是「跟着原曲伴奏唱」，所以切出的翻唱音频里**本身就含有原曲伴奏**。
于是把 LRC（原曲时间轴）对齐到翻唱音频，等价于求一个时间映射：

        t_cover = f(t_ref)

f 由「原曲 studio 音频」与「翻唱音频」的色度(chroma)特征序列做 DTW 求得。
不涉及语音识别，因此不受 ASR 分段噪声/谐音错字影响，行级误差可压到 ~50-150ms。

流水线
------
  netease 取原曲音频 + 官方 LRC(含 VIP 逐字 klyric)
    → 降采样 22050/mono
    → chroma_cens 特征（coarse hop=4096）
    → FFT 互相关估全局偏移 → 裁剪参考窗
    → 多分辨率带约束 DTW（coarse → fine hop=1024）
    → 路径 → 单调时间映射 f
    → 重同步 LRC / 导出偏移对照表 CSV

用法
----
  # 1) 自检：用真实音频合成一个"伪翻唱"(裁剪+变速+加噪)，验证映射还原精度
  python lyrics_sync.py --self-test --ref ref.mp3

  # 2) 正式：指定翻唱音频 + 歌名，自动取原曲与官方歌词，输出重同步 LRC
  python lyrics_sync.py --cover cover.mp3 --song "寄明月" --artist "SING女团" \
                        --out cover.lrc --map-out offsets.csv --plot

  # 3) 已有本地原曲音频时跳过联网
  python lyrics_sync.py --cover cover.mp3 --ref ref.mp3 --lrc raw.lrc --out out.lrc
"""
import argparse, csv, io, json, os, re, sys, time
import numpy as np

SR = 22050               # 统一工作采样率（足够 chroma，且省一半算力）
HOP_COARSE = 4096        # 粗对齐帧移 ≈ 186ms/帧
HOP_FINE = 1024          # 精对齐帧移 ≈ 46ms/帧
FEAT = "chroma_cens"     # chroma_cens：对音色/人声叠加最鲁棒，MIR 同步标配

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    from songcut import config as CFG      # noqa: E402
    DEFAULT_COOKIE_FILE = CFG.expand(CFG.path(
        "credentials", "netease_cookie_file", default="~/.songcut/netease_cookie.txt"))
except Exception:                          # 独立运行且未装包时退到通用位置
    DEFAULT_COOKIE_FILE = os.path.join(os.path.expanduser("~"), ".songcut",
                                       "netease_cookie.txt")


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ==========================================================================
#  一、网易云（凭本人 VIP cookie，仅官方接口）
# ==========================================================================
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def load_cookie(path=None):
    """从本地文件读取 cookie（MUSIC_U=...; ...）。缺失返回空串 → 未登录降级。"""
    p = path or DEFAULT_COOKIE_FILE
    try:
        c = io.open(p, encoding="utf-8").read().strip()
        return c if c else ""
    except Exception:
        return ""


class Netease(object):
    """网易云官方 Web 接口封装。拿不到就返回 None，绝不尝试绕过。"""

    def __init__(self, cookie=None, cookie_file=None):
        import requests
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": "https://music.163.com"})
        ck = cookie if cookie is not None else load_cookie(cookie_file)
        if ck:
            self.s.headers["Cookie"] = ck
        self.logged = bool(ck)

    def search_top(self, title, artist_hint="", n=5):
        """返回按匹配度排序的候选列表（供回退链使用）。"""
        import difflib
        try:
            r = self.s.post("https://music.163.com/api/cloudsearch/pc",
                            data={"s": title, "type": 1, "limit": 12}, timeout=(10, 25))
            songs = (r.json().get("result") or {}).get("songs") or []
        except Exception:
            return []
        tn = re.sub(r"[\s\(\)（）【】\[\]]", "", title).lower()
        out = []
        for s in songs:
            nm = s.get("name") or ""
            sc = difflib.SequenceMatcher(
                None, tn, re.sub(r"[\s\(\)（）【】\[\]]", "", nm).lower()).ratio()
            ar = (s.get("ar") or [{}])[0].get("name") or ""
            if artist_hint and artist_hint.replace(" ", "") in ar.replace(" ", ""):
                sc += 0.35                   # 歌手匹配权重高：避免串到同名翻唱版
            if "live" in nm.lower() or "现场" in nm:
                sc -= 0.25                   # 翻唱通常对录音室版伴奏
            out.append((sc, {"id": s.get("id"), "name": nm, "artist": ar,
                             "fee": s.get("fee"),
                             "duration": (s.get("dt") or 0) / 1000.0}))
        out.sort(key=lambda x: -x[0])
        return [d for _, d in out[:n]]

    def search(self, title, artist_hint=""):
        top = self.search_top(title, artist_hint, n=1)
        return top[0] if top else None

    def lyric(self, song_id):
        """官方歌词。lrc=逐行；klyric=VIP 逐字(krc)；tlyric=翻译。"""
        try:
            r = self.s.get("https://music.163.com/api/song/lyric",
                           params={"id": song_id, "lv": 1, "kv": 1, "tv": -1},
                           timeout=(10, 25))
            d = r.json()
            return {"lrc": (d.get("lrc") or {}).get("lyric") or "",
                    "klyric": (d.get("klyric") or {}).get("lyric") or "",
                    "tlyric": (d.get("tlyric") or {}).get("lyric") or ""}
        except Exception:
            return {"lrc": "", "klyric": "", "tlyric": ""}

    def audio_url(self, song_id, br=320000):
        """播放地址。VIP/无版权歌在未登录或权限不足时 url=null（code=-110）。
        br: 320000=高品(VIP) / 999000=无损(VIP 需更高等级) / 192000=标准。"""
        try:
            r = self.s.post("https://music.163.com/api/song/enhance/player/url",
                            data={"ids": "[%s]" % song_id, "br": str(br)},
                            timeout=(10, 25))
            for it in (r.json().get("data") or []):
                if it.get("url"):
                    return it["url"], it.get("br"), it.get("size")
        except Exception:
            pass
        return None, None, None

    def download(self, song_id, out_path, br=320000):
        url, abr, size = self.audio_url(song_id, br)
        if not url:
            return None
        try:
            r = self.s.get(url, timeout=(10, 60), stream=True)
            if r.status_code != 200:
                return None
            with io.open(out_path, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if chunk:
                        f.write(chunk)
            return out_path
        except Exception:
            return None


def parse_krc(krc):
    """VIP 逐字歌词 krc → 逐字增强 LRC。

    krc 行格式： [行起始ms,行时长ms]字1(起始ms,时长ms)字2(起始ms,时长ms)...
    输出：[mm:ss.xx]字1[mm:ss.xx]字2 ...（播放器可据此做逐字卡拉OK）
    """
    out = []
    for line in (krc or "").splitlines():
        m = re.match(r"^\[(\d+),(\d+)\](.*)$", line.strip())
        if not m:
            continue
        start_ms = int(m.group(1))
        body = m.group(3)
        parts = re.findall(r"([^\[\(]*)(?:\((\d+),(\d+)\))?", body)
        # 逐个「字 + 起始ms」重建
        items = re.findall(r"([^()\[\]]*?)\((\d+),(\d+)\)", body)
        if not items:
            txt = re.sub(r"\[[^\]]*\]", "", body).strip()
            if txt:
                out.append("%s%s" % (lrc_tag(start_ms / 1000.0), txt))
            continue
        seg = []
        for chars, st, du in items:
            if not chars:
                continue
            for k, ch in enumerate(chars):
                t = (start_ms + int(st)) / 1000.0 + k * 0.0
                seg.append("%s%s" % (lrc_tag(t), ch))
        if seg:
            out.append("".join(seg))
    return "\n".join(out)


def lrc_tag(t):
    t = max(0.0, float(t))
    return "[%02d:%05.2f]" % (int(t // 60), t % 60)


# ==========================================================================
#  二、音频与特征
# ==========================================================================
def load_audio(path, sr=SR, offset=0.0, duration=None):
    """统一为 mono / sr。mp3/mp4 走 soundfile 或 audioread(需 ffmpeg)。"""
    import librosa
    y, _ = librosa.load(path, sr=sr, mono=True, offset=offset, duration=duration)
    y = np.asarray(y, dtype=np.float32)
    if y.size:
        y = y / (np.max(np.abs(y)) + 1e-9)      # 峰值归一，消除两版音量差
    return y


def chroma_features(y, sr=SR, hop=HOP_FINE, kind=FEAT):
    """返回 (12, T) 归一化色度特征。

    chroma_cens：CQT 色度 + 能量归一 + 时域平滑 → 对音色、人声叠加、压缩
    编码最鲁棒，是音乐同步(Müller/FMP)的标配特征。
    """
    import librosa
    if kind == "chroma_cens":
        C = librosa.feature.chroma_cens(y=y, sr=sr, hop_length=hop)
    else:
        C = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
    C = C - np.median(C, axis=1, keepdims=True)          # 去各音级基线
    C = C / (np.std(C, axis=1, keepdims=True) + 1e-9)
    C = C / (np.linalg.norm(C, axis=0, keepdims=True) + 1e-9)  # 列 L2 → 余弦距离
    return np.ascontiguousarray(C.T.astype(np.float32))   # (T, 12) 便于向量化


def _cos_dist_matrix(X, Y):
    """X:(N,d) Y:(M,d) 已 L2 归一 → 余弦距离矩阵 (N,M)。分块计算控内存。"""
    D = 1.0 - X @ Y.T
    return np.clip(D, 0.0, 2.0)


# ==========================================================================
#  三、DTW（带约束 + 多分辨率）
# ==========================================================================
def dtw_banded(X, Y, band=0.12, band_min=20, centers=None,
               free_start=True, free_end=True):
    """带 Sakoe-Chiba 带状约束的 DTW，复杂度 O(N * band) 而非 O(N*M)。

    X:(N,d) 目标（翻唱，短）  Y:(M,d) 参考（原曲，长）
    free_start/free_end: 允许路径在**参考轴**上自由起止 —— 这是"翻唱 ⊂ 原曲"
        （子序列对齐）的关键：翻唱可能从原曲任意秒开始、在任意秒结束。
    band:   带宽（占 N 的比例）；band_min: 最小绝对半宽（帧）
    centers: 长度 N 的数组，给出每行 i 的中心 j（精修时由粗路径插值而来）
    return: (path_i, path_j) 索引数组
    """
    N, M = len(X), len(Y)
    half = max(band_min, int(band * N))
    if centers is None:
        centers = np.linspace(0, M - 1, N)          # 默认：线性对角线
    centers = np.clip(centers, 0, M - 1)

    INF = np.float32(1e18)
    cost_rows, bptr_rows, lo_rows = [], [], []
    prev = None
    for i in range(N):
        c = int(round(centers[i]))
        lo = max(0, c - half)
        hi = min(M - 1, c + half)
        w = hi - lo + 1
        d = 1.0 - X[i] @ Y[lo:hi + 1].T             # 该行到窗口内所有 j 的距离
        d = np.clip(d, 0.0, 2.0).astype(np.float32)
        cur = np.full(w, INF, dtype=np.float32)
        bp = np.zeros(w, dtype=np.int8)             # 0=i-1,j-1  1=i-1,j  2=i,j-1
        if prev is None:
            cur[:] = d                              # 起点自由（可落在参考任意位置）
            bp[:] = 3                               # 3 = 路径起点，回溯到此终止
        else:
            plo, phi = lo_rows[-1]
            # 上一行窗口与当前窗口对齐后逐个 j 取 min(上, 左上, 左)
            for k in range(w):
                j = lo + k
                best, arg = INF, 0
                if plo <= j <= phi:
                    v = prev[j - plo]
                    if v < best:
                        best, arg = v, 1
                if plo <= j - 1 <= phi:
                    v = prev[j - 1 - plo]
                    if v < best:
                        best, arg = v, 0
                if k > 0 and cur[k - 1] < best:
                    best, arg = cur[k - 1], 2
                cur[k] = d[k] + (0.0 if best >= INF else best)
                bp[k] = arg
        cost_rows.append(cur)
        bptr_rows.append(bp)
        lo_rows.append((lo, hi))
        prev = cur

    # 回溯：终点取最后一行窗口内代价最小者
    # bp: 0=来自(i-1,j-1)  1=来自(i-1,j)  2=来自(i,j-1)（同一行内左移）
    i, j = N - 1, lo_rows[-1][0] + int(np.argmin(cost_rows[-1]))
    path_i, path_j = [], []
    guard = 0
    while True:
        path_i.append(i)
        path_j.append(j)
        guard += 1
        if guard > 8 * (N + M):
            break
        bp = bptr_rows[i][j - lo_rows[i][0]]
        if bp == 3:                     # 已到路径起点
            break
        if bp == 2 and j - 1 >= lo_rows[i][0]:
            j -= 1                      # 同一行继续左移（起点自由）
            continue
        i -= 1
        if bp == 0:
            j -= 1
        j = int(min(max(j, lo_rows[i][0]), lo_rows[i][1])) if i >= 0 else j
    path_i.reverse()
    path_j.reverse()
    return np.array(path_i), np.array(path_j)


def estimate_global_offset(Cref, Ccov, max_shift_frac=0.9):
    """FFT 互相关估全局常数偏移（假设速度比≈1），用于裁剪参考窗。

    返回 (offset_frames, score)：Ccov ≈ Cref 从第 offset_frames 帧开始。
    """
    N, M = len(Cref), len(Ccov)
    if N < M:
        return 0, 0.0
    L = 1
    while L < N + M:
        L <<= 1
    Fr = np.fft.rfft(Cref.T, n=L, axis=1)      # (12, L/2+1)
    Fc = np.fft.rfft(Ccov.T, n=L, axis=1)
    corr = np.fft.irfft(np.sum(Fr * np.conj(Fc), axis=0), n=L)
    corr = corr[:N - M + 1]
    k = int(np.argmax(corr))
    score = float(corr[k] / (M + 1e-9))
    return k, score


def align_audio(ref_y, cov_y, sr=SR, coarse_band=0.15, fine_sec=2.5, verbose=True,
                prior=None):
    """多分辨率 DTW：coarse 定全局 → fine 局部精修。返回 dict。

    prior: 先验全局偏移（秒）——「翻唱第 0 帧 ≈ 参考第 prior 秒」。
           当翻唱窗口长于原曲时 estimate_global_offset 的互相关会失效
           （返回 0 帧、相关度 0），此时必须传入可靠先验（如能量法 onset
           换算出的偏移），否则粗路径会整体跑飞。
    """
    t0 = time.time()
    # ---- 粗对齐：X=翻唱(短)，Y=参考(长)，参考轴自由起止（子序列对齐）----
    Cc = chroma_features(cov_y, sr, HOP_COARSE)
    Cr = chroma_features(ref_y, sr, HOP_COARSE)
    if len(Cc) > len(Cr):
        if verbose:
            log("  警告：翻唱长于参考，带宽将放宽")
    if prior is not None:
        off = int(round(prior * sr / HOP_COARSE))
        score = float("nan")
        if verbose:
            log("  粗对齐：使用先验偏移 %d 帧（%.2fs）" % (off, off * HOP_COARSE / sr))
    else:
        off, score = estimate_global_offset(Cr, Cc)
        if verbose:
            log("  粗估全局偏移 %d 帧（%.1fs），相关度 %.3f" %
                (off, off * HOP_COARSE / sr, score))
    # 中心：翻唱第 i 帧 ≈ 参考第 (off + i) 帧（速度比先验取 1，靠带宽容忍漂移）
    centers_c = off + np.arange(len(Cc), dtype=np.float64)
    pi, pj = dtw_banded(Cc, Cr, band=coarse_band, band_min=15, centers=centers_c)
    t_cov_c = pi * HOP_COARSE / sr
    t_ref_c = pj * HOP_COARSE / sr
    if verbose:
        log("  粗 DTW 完成（%d 帧路径）" % len(pi))

    # ---- 精对齐：以粗路径为中心，±fine_sec 带宽 ----
    Fc = chroma_features(cov_y, sr, HOP_FINE)
    Fr = chroma_features(ref_y, sr, HOP_FINE)
    cov_f_idx = np.arange(len(Fc)) * HOP_FINE / sr
    centers_f = np.interp(cov_f_idx, t_cov_c, t_ref_c) * sr / HOP_FINE
    centers_f = np.clip(centers_f, 0, len(Fr) - 1)
    half = int(fine_sec * sr / HOP_FINE)
    pi2, pj2 = dtw_banded(Fc, Fr, band=0.0, band_min=half, centers=centers_f)
    t_cov = pi2 * HOP_FINE / sr
    t_ref = pj2 * HOP_FINE / sr
    if verbose:
        log("  精 DTW 完成（%d 帧路径，%.1fs）" % (len(pi2), time.time() - t0))
    return {"t_ref": t_ref, "t_cov": t_cov, "offset": off * HOP_COARSE / sr,
            "score": score, "coarse": (t_ref_c, t_cov_c)}


# ==========================================================================
#  四、映射与导出
# ==========================================================================
def build_mapper(t_ref, t_cov):
    """路径 → 单调映射 f(t_ref) = t_cov。同 t_ref 取首个（onset 语义）。"""
    order = np.lexsort((t_cov, t_ref))
    tr, tc = t_ref[order], t_cov[order]
    # 每个唯一 t_ref 取最小 t_cov（起始时刻）
    uref, first = np.unique(tr, return_index=True)
    ucov = tc[first]
    # 强制单调不减（DTW 路径偶有局部回退）
    ucov = np.maximum.accumulate(ucov)

    def f(t):
        return np.interp(t, uref, ucov)

    # 反向映射（cover → ref），供排查
    def f_inv(t):
        return np.interp(t, ucov, uref)

    return {"f": f, "f_inv": f_inv, "t_ref": uref, "t_cov": ucov}


def map_report(mp):
    """健康度诊断：速度比、残差 RMS、单调性、覆盖率。"""
    tr, tc = mp["t_ref"], mp["t_cov"]
    n = len(tr)
    if n < 10:
        return {"ok": False, "reason": "路径过短"}
    A = np.vstack([tr, np.ones(n)]).T
    slope, intercept = np.linalg.lstsq(A, tc, rcond=None)[0]
    resid = tc - (slope * tr + intercept)
    rms = float(np.sqrt(np.mean(resid ** 2)))
    d = np.diff(tc)
    mono = float(np.mean(d >= -1e-6))
    span_c = float(tc[-1] - tc[0])
    span_r = float(tr[-1] - tr[0])
    ok = (0.8 <= slope <= 1.25) and rms < 2.0
    warn = []
    if not (0.8 <= slope <= 1.25):
        warn.append("速度比 %.3f 异常 → 可能不是同一版本伴奏/选错歌" % slope)
    if rms > 2.0:
        warn.append("线性残差 RMS %.2fs 偏大 → 路径可能局部跑飞，收紧带宽或换特征" % rms)
    if mono < 0.999:
        warn.append("映射非单调比例 %.3f → 检查特征/带宽" % (1 - mono))
    return {"ok": ok, "slope": float(slope), "intercept": float(intercept),
            "rms": rms, "monotonic": mono, "span_ref": span_r, "span_cov": span_c,
            "frames": n, "warnings": warn}


def parse_lrc(text):
    """→ [(t, text)]，兼容 [mm:ss.xx] 与 [mm:ss]"""
    out = []
    for line in (text or "").splitlines():
        for m in re.finditer(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]", line):
            txt = re.sub(r"\[[^\]]*\]", "", line).strip()
            if not txt:
                continue
            s = m.group(2).replace(":", ".")
            out.append((int(m.group(1)) * 60 + float(s), txt))
    out.sort(key=lambda x: x[0])
    return out


def resync_lrc(lines, mp, out_path=None):
    """按映射重写时间轴，输出 LRC 文本（越界的行直接丢弃）。"""
    tr0, tr1 = mp["t_ref"][0], mp["t_ref"][-1]
    tc0, tc1 = mp["t_cov"][0], mp["t_cov"][-1]
    out, dropped = [], 0
    for t, txt in lines:
        if t < tr0 - 0.05 or t > tr1 + 0.05:
            dropped += 1
            continue
        # 参考窗内的相对位置 → 翻唱时间
        out.append((float(mp["f"](t)), txt))
    # 平移到「翻唱 0 点」：以映射起点对齐
    base = float(mp["f"](tr0)) if len(lines) else 0.0
    body = "\n".join("%s%s" % (lrc_tag(max(0.0, t - base + (tc0 - tc0))), x)
                     for t, x in out)
    if out_path:
        io.open(out_path, "w", encoding="utf-8").write(body + "\n")
    return body, dropped, base


def write_offset_csv(mp, path, step=5.0):
    """偏移对照表：每 step 秒一行（参考时间 / 翻唱时间 / 相对偏移 / 漂移）。"""
    tr, tc = mp["t_ref"], mp["t_cov"]
    grid = np.arange(tr[0], tr[-1], step)
    base_off = float(np.interp(tr[0], tr, tc)) - tr[0]
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_ref_s", "t_cover_s", "offset_s", "drift_vs_global_s"])
        for t in grid:
            c = float(np.interp(t, tr, tc))
            w.writerow(["%.3f" % t, "%.3f" % c, "%.3f" % (c - t),
                        "%.3f" % ((c - t) - base_off)])


# ==========================================================================
#  五、自检：合成"伪翻唱"验证映射还原精度
# ==========================================================================
def self_test(ref_path, sr=SR):
    """把参考音频做 裁剪 + 变速 + 加噪 + 叠一层人声状谐波 → 当"翻唱"，
    再用本模块反推映射，检查能否还原已知的时间变换。

    通过判据：映射残差 RMS < 0.15s（相当于行级误差 100ms 级）
    """
    import librosa
    log("自检：加载参考音频 %s" % ref_path)
    y = load_audio(ref_path, sr=sr)
    dur = len(y) / sr
    trim = 7.3                       # 翻唱比原曲晚开始 7.3s（模拟开头）
    tempo = 1.012                    # 快 1.2%（模拟轻微速度差）
    keep = 90.0                      # 只取 90s 片段
    seg = y[int(trim * sr): int((trim + keep) * sr)]
    # 加速 tempo 倍：按"源采样率 = sr*tempo"重采样到 sr → 时长变为 1/tempo
    seg_fast = librosa.resample(seg, orig_sr=int(sr * tempo), target_sr=sr)
    seg_fast = seg_fast[: int(keep * sr)]
    # 叠加"人声"：低频谐波 + 噪声（模拟副唱轨，色度上不破坏伴奏主导）
    t = np.arange(len(seg_fast)) / float(sr)
    vocal = 0.18 * np.sin(2 * np.pi * 330 * t) + 0.10 * np.sin(2 * np.pi * 494 * t)
    rng = np.random.default_rng(7)
    cov = seg_fast + vocal + 0.01 * rng.standard_normal(len(seg_fast)).astype(np.float32)
    cov = cov / (np.max(np.abs(cov)) + 1e-9)

    log("  合成翻唱：裁剪 %.1fs，速度 ×%.3f，加噪+谐波层" % (trim, tempo))
    res = align_audio(y, cov, sr=sr)

    # 真值：t_cover = (t_ref - trim) / tempo
    tr = np.linspace(trim + 5, trim + keep - 5, 60)
    tc_true = (tr - trim) / tempo
    mp = build_mapper(res["t_ref"], res["t_cov"])
    tc_pred = np.array([float(mp["f"](t)) for t in tr])
    err = tc_pred - tc_true
    rms = float(np.sqrt(np.mean(err ** 2)))
    rep = map_report(mp)
    log("  速度比 估计 %.4f（真值 %.4f）" % (rep["slope"], 1.0 / tempo))
    log("  还原 RMS = %.3fs，最大误差 %.3fs" % (rms, float(np.max(np.abs(err)))))
    log("  诊断：ok=%s  RMS(线性)=%.3fs  单调=%.4f" %
        (rep["ok"], rep["rms"], rep["monotonic"]))
    for w in rep["warnings"]:
        log("    ⚠ " + w)
    ok = rms < 0.15
    log("  结论：%s" % ("通过（映射精度达 100ms 级）" if ok else "未通过，需调参"))
    return ok


# ==========================================================================
#  六、CLI
# ==========================================================================
def main():
    ap = argparse.ArgumentParser(description="原曲↔翻唱 DTW 歌词时间轴同步")
    ap.add_argument("--cover", help="翻唱音频（mp3/wav/mp4 均可，ffmpeg 解码）")
    ap.add_argument("--ref", help="原曲音频；不给则尝试网易云拉取")
    ap.add_argument("--song", help="歌名（--ref 缺省时用于拉取原曲）")
    ap.add_argument("--artist", default="", help="歌手，辅助检索")
    ap.add_argument("--lrc", help="原曲 LRC；不给则用网易云官方歌词")
    ap.add_argument("--out", help="重同步后的 LRC 输出路径")
    ap.add_argument("--map-out", help="偏移对照表 CSV 输出路径")
    ap.add_argument("--br", type=int, default=320000, help="音质码率（默认 320000）")
    ap.add_argument("--cookie-file", help="cookie 文件路径（默认见 config.json 的 credentials 段）")
    ap.add_argument("--cache-dir", default="_media_cache", help="原曲音频缓存目录")
    ap.add_argument("--sr", type=int, default=SR)
    ap.add_argument("--self-test", action="store_true", help="合成伪翻唱做精度自检")
    args = ap.parse_args()

    if args.self_test:
        if not args.ref:
            ap.error("--self-test 需要 --ref 音频")
        ok = self_test(args.ref, sr=args.sr)
        sys.exit(0 if ok else 1)

    if not args.cover:
        ap.error("需要 --cover")

    ref_path = args.ref
    lrc_text = io.open(args.lrc, encoding="utf-8").read() if args.lrc else ""
    if not ref_path or not lrc_text:
        nt = Netease(cookie_file=args.cookie_file)
        log("网易云：%s" % ("已载入 cookie（VIP 资源可用）" if nt.logged
                            else "无 cookie → 仅免费资源可用，VIP 歌会降级"))
        cands = nt.search_top(args.song or os.path.splitext(os.path.basename(args.cover))[0],
                              args.artist, n=5)
        if not cands:
            log("未检索到歌曲 → 无法对齐，退出")
            sys.exit(2)
        # 回退链：按匹配度依次尝试，直到拿到可播放的原曲音频
        os.makedirs(args.cache_dir, exist_ok=True)
        meta, ref_path = None, None
        for c in cands:
            p = os.path.join(args.cache_dir, "_ref_%s.mp3" % c["id"])
            got = p if os.path.exists(p) else nt.download(c["id"], p, br=args.br)
            if got:
                meta, ref_path = c, got
                log("  采用原曲：%s / %s（id=%s，fee=%s，%.1f MB）" %
                    (c["name"], c["artist"], c["id"], c["fee"],
                     os.path.getsize(got) / 1e6))
                break
            log("  候选不可用（无权限/需 VIP）：%s / %s（fee=%s）→ 试下一个"
                % (c["name"], c["artist"], c["fee"]))
        if not ref_path:
            log("  所有候选的原曲音频均不可用 → 降级：改用现行 ASR 偏移方案")
            sys.exit(3)
        if not lrc_text:
            ly = nt.lyric(meta["id"])
            lrc_text = ly["klyric"] and parse_krc(ly["klyric"]) or ly["lrc"]
            log("  官方歌词：%d 行（%s）" %
                (len(parse_lrc(lrc_text)), "VIP 逐字 klyric" if ly["klyric"] else "逐行 lrc"))

    if not lrc_text:
        log("没有可用歌词，仅输出时间映射")
    log("加载音频…")
    ref_y = load_audio(ref_path, sr=args.sr)
    cov_y = load_audio(args.cover, sr=args.sr)
    log("  原曲 %.1fs / 翻唱 %.1fs" % (len(ref_y) / args.sr, len(cov_y) / args.sr))
    res = align_audio(ref_y, cov_y, sr=args.sr)
    mp = build_mapper(res["t_ref"], res["t_cov"])
    rep = map_report(mp)
    log("对齐诊断：斜率 %.4f  线性残差 %.3fs  单调 %.4f  ok=%s"
        % (rep["slope"], rep["rms"], rep["monotonic"], rep["ok"]))
    for w in rep["warnings"]:
        log("  ⚠ " + w)

    if lrc_text:
        body, dropped, base = resync_lrc(parse_lrc(lrc_text), mp, args.out)
        log("重同步 LRC：%d 行（丢弃 %d 行越界），起点基准 %.2fs" %
            (len(body.splitlines()), dropped, base))
        if not args.out:
            print(body[:400])
    if args.map_out:
        write_offset_csv(mp, args.map_out)
        log("偏移对照表 → %s" % args.map_out)
    sys.exit(0 if rep["ok"] else 4)


if __name__ == "__main__":
    main()
