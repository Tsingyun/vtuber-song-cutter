# -*- coding: utf-8 -*-
"""从被覆盖的成片 LRC 重建 override（口径 = cut 相对时间，不含片头补静音）。

背景：_shift_lrc 曾只平移行首不移逐字，导致该文件行首 1.65s / 首字 0.80s 劈叉。
逐字标签仍是未平移的原始值，因此以逐字为真值、用首字时间重算行首。
已删掉被硬塞到片尾的重复段末句「就这样吧」（她没唱）。
"""
import io, re

SRC = "cuts/2026-11-21/【岁己SUI】如果有来生【20261121歌切】.lrc"
OUT = "segments/ov_如果有来生.lrc"

LINE_RE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")
WORD_RE = re.compile(r"<(\d+):(\d+(?:\.\d+)?)>")


def sec(mm, ss):
    return int(mm) * 60 + float(ss)


def fmt(t):
    return "%02d:%05.2f" % (int(t // 60), t % 60)


rows = []
for ln in io.open(SRC, encoding="utf-8"):
    m = LINE_RE.match(ln.rstrip("\n"))
    if not m:
        continue
    parts = WORD_RE.split(m.group(3))
    words, i = [], 1
    while i + 2 <= len(parts):
        try:
            words.append([sec(parts[i], parts[i + 1]), parts[i + 2]])
        except ValueError:
            break
        i += 3
    if words:
        rows.append(words)

assert len(rows) == 40, "期望 40 行（已剔除末尾重复段），实际 %d" % len(rows)

joined = "".join("".join(w[1] for w in ws) for ws in rows)
# 官方 LRC 有三处「就这样吧」（第一遍副歌尾／第二遍副歌尾／末尾重复段），她只唱了前两处
assert "就这样吧" in joined and joined.count("就这样吧") == 2, "「就这样吧」应为两处（末尾重复段已剔除）"

mono = 0
for ws in rows:
    for k in range(1, len(ws)):
        if ws[k][0] <= ws[k - 1][0]:
            ws[k][0] = ws[k - 1][0] + 0.06
            mono += 1

out = [("[%s]%s" % (fmt(ws[0][0]), "".join("<%s>%s" % (fmt(t), x) for t, x in ws))) for ws in rows]
io.open(OUT, "w", encoding="utf-8", newline="").write("\n".join(out) + "\n")

print("写出 %s：%d 行，行内修正 %d 处" % (OUT, len(out), mono))
print("首行 %s ｜ 末行 %s" % (out[0].split("]")[0] + "]", out[-1][:10]))
