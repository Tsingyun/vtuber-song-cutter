# -*- coding: utf-8 -*-
"""集中配置。

查找顺序（先命中者胜）：

1. 环境变量 ``SONGCUT_CONFIG`` 指向的文件（显式指定）；
2. 仓库根目录的 ``config.json``（**不入库**，见 .gitignore）；
3. 都不存在 → 使用本文件的通用默认值，并在需要时以明确提示降级。

设计原则
--------
**真实的主播身份、房间号、本机绝对路径、在线歌单地址只允许写在 config.json 里**，
代码内一律使用通用占位值。这样仓库可以安全公开，而本机功能一字不变。

用法
----
>>> from songcut import config as CFG
>>> CFG.get("recording_root")
>>> CFG.path("streamer", "archive_prefix", default="SONG ARCHIVE")
>>> CFG.node_candidates()
"""
import io
import json
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "config.json")

# 通用占位默认值：任何用户都能跑起来，且不含任何特定身份信息
DEFAULTS = {
    # 工作目录：产物写 <workdir>/cuts/<date>/。留空则用仓库根。
    "workdir": ROOT,
    # 录播归档根目录，其下按日期分子目录存放压缩归档 MP4
    "recording_root": "",
    # 上游转写（SRT）目录
    "srt_dir": "",
    # 上游 LLM 配置（含 api_key / 代理）；留空则用下面的 llm 段
    "llm_config": "",
    # 曲库 JSON（可选，用于歌名模糊匹配的候选集）
    "song_library_json": "",
    # 在线歌单表（当日演唱歌名的权威数据源）
    "songlist": {
        "url": "",
        "sheet_index": 3,
        "ttl_hours": 12,
    },
    # 主播 / 品牌信息
    "streamer": {
        "name": "",                      # 演唱者名（写入档案行 VOCAL）
        "archive_prefix": "SONG ARCHIVE",  # 左下角档案号前缀
        "art_image": "",                 # 右侧装饰立绘（PNG/JPG，可留空）
    },
    # 播放器贴图（留空则用 renderer/player/assets/ 下的中性占位素材）
    "assets": {
        "sticker": "",                   # 右上角贴图
        "watermark": "",                 # 歌词区右侧背景水印
    },
    # 运行时可执行文件（留空则自动探测）
    "runtime": {
        "node": "",
        "node_modules_path": "",
        "ffmpeg": "",
        "ffprobe": "",
    },
    # 凭据文件路径（仅本人使用，缺失自动降级）
    "credentials": {
        "netease_cookie_file": "~/.songcut/netease_cookie.txt",
        "glm_credentials_file": "~/.songcut/glm_credentials.json",
    },
    # LLM 通道（llm_config 缺失时使用）
    "llm": {
        "api_host": "https://generativelanguage.googleapis.com/v1beta/models",
        "model": "gemini-2.5-flash",
        "api_key": "",
        "proxy": "",
    },
}

_cache = None
_source = None


def _merge(base, over):
    """浅层合并到嵌套一层（够用且可预期）。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def locate():
    """返回实际使用的配置文件路径；没有则返回 None。"""
    env = os.environ.get("SONGCUT_CONFIG")
    if env:
        return env if os.path.isfile(env) else None
    return DEFAULT_PATH if os.path.isfile(DEFAULT_PATH) else None


def load(reload=False):
    """载入并缓存配置（DEFAULTS 打底，配置文件覆盖）。"""
    global _cache, _source
    if _cache is not None and not reload:
        return _cache
    _source = locate()
    data = dict(DEFAULTS)
    if _source:
        try:
            with io.open(_source, encoding="utf-8") as f:
                user = json.load(f)
            # 兼容：用户可能把整份配置包在 "songcut" 键下
            if isinstance(user.get("songcut"), dict):
                user = user["songcut"]
            data = _merge(DEFAULTS, user)
        except Exception as e:            # 配置写坏不应让管线直接崩
            print("[config] 读取 %s 失败：%r → 使用默认值" % (_source, e))
    _cache = data
    return _cache


def source():
    """已载入的配置文件路径（未载入则先载入）。"""
    load()
    return _source


def get(key, default=None):
    cfg = load()
    if key in cfg:
        return cfg[key]
    return DEFAULTS.get(key, default)


def path(*keys, **kw):
    """取嵌套值：``path("streamer", "name", default="")``。"""
    default = kw.get("default")
    cur = load()
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return default if cur is None else cur


def expand(p):
    """展开 ~ 与环境变量；空值原样返回。"""
    if not p:
        return p
    return os.path.expanduser(os.path.expandvars(p))


def node_candidates():
    """Node 可执行文件候选（环境变量 → 配置 → 通用安装位置）。"""
    cands = []
    for c in (os.environ.get("SONGCUT_NODE"), path("runtime", "node", default=""),
              "node", r"C:\Program Files\nodejs\node.exe",
              "/usr/local/bin/node", "/usr/bin/node"):
        if c and c not in cands:
            cands.append(c)
    exe = shutil.which("node")
    if exe and exe not in cands:
        cands.insert(0, exe)
    return cands


def node_modules_path():
    """Node 依赖（playwright 等）的 NODE_PATH；留空则由 Node 自行解析。"""
    return expand(path("runtime", "node_modules_path", default="") or "")


def ffmpeg():
    """ffmpeg 可执行文件（环境变量 FFMPEG_BIN 优先）。"""
    for c in (os.environ.get("FFMPEG_BIN"), path("runtime", "ffmpeg", default=""),
              "ffmpeg", r"C:\ffmpeg\bin\ffmpeg.exe"):
        if c and (os.path.sep in c or shutil.which(c)):
            return c
    return "ffmpeg"


def ffprobe():
    """ffprobe 可执行文件。"""
    for c in (os.environ.get("FFPROBE_BIN"), path("runtime", "ffprobe", default=""),
              "ffprobe", r"C:\ffmpeg\bin\ffprobe.exe"):
        if c and (os.path.sep in c or shutil.which(c)):
            return c
    return "ffprobe"


def describe():
    """一行摘要，便于启动日志自证配置来源。"""
    s = source()
    return "配置来源：%s" % (s if s else "内置默认值（未找到 config.json）")


if __name__ == "__main__":
    import pprint
    print(describe())
    pprint.pprint(load())
