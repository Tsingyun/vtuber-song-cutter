# -*- coding: utf-8 -*-
"""成片「风格」的唯一真源：A 唱片（专辑封面形式）。

本对话只保留这一条产线：静态纸底 + 封面档案 + 四行歌词，出片不需要任何预渲素材。
旧的 --style 命令仍可用，但只接受 A 的别名。
"""
STYLES = {
    "A": {
        "key": "A",
        "name": "唱片",
        "en": "record",
        "page": "index.html",
        "desc": "静态纸底 + 封面档案 + 四行歌词；克制、像专辑内页，无需任何额外素材",
    },
}

ALIASES = {
    "a": "A", "A": "A", "唱片": "A", "record": "A",
    "album": "A", "内页": "A", "纸": "A",
}
DEFAULT = "A"


class StyleError(ValueError):
    """风格名无法识别。带上可选清单，避免用户对着报错猜。"""


def resolve(spec):
    """把用户给的名字解析成风格字典；空值返回 DEFAULT。"""
    if spec in (None, ""):
        return STYLES[DEFAULT]
    s = str(spec).strip()
    key = ALIASES.get(s) or ALIASES.get(s.lower())
    if not key:
        raise StyleError("无法识别的风格：%r。可选：%s" % (spec, describe_all()))
    return STYLES[key]


def describe_all():
    """一行式清单，供 --help 与报错复用。"""
    return "；".join(
        "%s=%s（%s）" % (k, v["name"], v["desc"]) for k, v in sorted(STYLES.items()))
