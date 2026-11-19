# -*- coding: utf-8 -*-
"""成片「风格」的唯一真源：A / B 两套模板的命名与参数映射。

为什么要有这个文件
------------------
2026-11-02 复盘：做《画心》demo 时搭的那套（文字 PV + JIZURA + 人物挂点 + 风格弧线）
一直只存在于「手工跑 tools/pv_direct.py」这条旁路里，主链 `song_cutter.py` 的
`PLAYER_PAGE` 默认是 `index.html`，于是 11-01 两首歌切**静默退回了 v9 版式**，
连 `pv_prepare.py` 都没被调用（`cuts/<date>/pv/` 目录压根不存在）。

根因不是「参数写错」，而是「风格」这个概念在全项目里没有名字 —— 每换一次版式，
就要在命令行里手拼 4 个底层参数（--player-page / --arc / --live / --jz），
漏一个就静默降级，且降级后画面依然「看起来正常」，没有任何报错。

所以这里给两套模板起名，并把「一个名字 → 一组底层参数」的映射收敛成唯一来源。
其余模块（song_cutter.py / pv_prepare.py）只能引用，不许再各自拼参数。

两套风格
--------
A 唱片（record）   index.html         静态纸底 + 封面档案 + 四行歌词梯形排版
                   —— 克制、像专辑内页；出片快，不需要任何额外素材。

B 手书（lettering）index_pv.html      逐字出现 + JIZURA 动态排版 + 人物挂点 + 风格弧线
                   —— 像 MV 手书；需要预渲素材（live.json / jz.json），缺素材时逐层降级。
"""
import os

# 底层参数组的字段含义：
#   page   renderer/player/ 下的页面文件名（决定版式本体）
#   pv     是否需要生成编排稿 storyboard（页面名含 "pv" 时必须为 True）
#   arc    风格弧线（按歌词意境 + 曲风能量逐句调制配色/字距/转场）
#   live   人物挂点层（需要 live.json + 裁好的图片序列）
#   jz     JIZURA 动态排版帧序列层（需要 jz.json + 预渲 PNG）
STYLES = {
    "A": {
        "key": "A",
        "name": "唱片",
        "en": "record",
        "page": "index.html",
        "pv": False,
        "arc": False,
        "live": False,
        "jz": False,
        "desc": "静态纸底 + 封面档案 + 四行歌词；克制、像专辑内页，无需任何额外素材",
    },
    "B": {
        "key": "B",
        "name": "手书",
        "en": "lettering",
        "page": "index_pv.html",
        "pv": True,
        "arc": True,
        "live": True,
        "jz": True,
        "desc": "逐字出现 + JIZURA 动态排版 + 人物挂点 + 风格弧线；需要预渲素材，缺则逐层降级",
    },
}

# 别名 → 标准键。用户说的话、目录名、英文名都收进来，避免「说了名字却认不出」。
ALIASES = {
    "a": "A", "A": "A", "唱片": "A", "record": "A", "album": "A", "内页": "A", "纸": "A",
    "b": "B", "B": "B", "手书": "B", "lettering": "B", "pv": "B", "PV": "B",
    "文字": "B", "文字pv": "B", "mv": "B",
}

# 未显式指定时用哪套。保持历史行为（v9）—— 换成 B 会让所有旧命令的产物形态变化，
# 必须由人明确说一句才切，不能靠默认值偷偷改变。
DEFAULT = "A"


class StyleError(ValueError):
    """风格名无法识别。带上可选清单，避免用户对着报错猜。"""


def resolve(spec):
    """把用户给的名字（A / 唱片 / lettering / ...）解析成风格字典。

    spec 为空 → 返回 DEFAULT 对应的那套。
    """
    if spec in (None, ""):
        return STYLES[DEFAULT]
    s = str(spec).strip()
    key = ALIASES.get(s) or ALIASES.get(s.lower())
    if not key:
        raise StyleError(
            "无法识别的风格：%r。可选：%s" % (spec, describe_all()))
    return STYLES[key]


def describe_all():
    """一行式清单，供 --help 与报错复用。"""
    return "；".join(
        "%s=%s（%s）" % (k, v["name"], v["desc"]) for k, v in sorted(STYLES.items()))


def asset_paths(out_dir, title):
    """B 风格的预渲素材约定位置（相对成片目录 cuts/<date>/）。

    约定而非参数，是为了让「素材放对地方 = 自动用上」，不必每条命令都带 --live/--jz。
    返回 (live_json, jz_json)，不存在的给 None —— 调用方据此逐层降级。
    """
    base = os.path.join(out_dir, title or "")
    live = os.path.join(base, "live", "live.json")
    jz = os.path.join(base, "jz", "jz.json")
    return (live if os.path.exists(live) else None,
            jz if os.path.exists(jz) else None)
