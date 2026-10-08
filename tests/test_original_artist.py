# -*- coding: utf-8 -*-
"""原唱一致性回归（2026-10-06《连名带姓》事故）。

事故：曲库提示「张惠妹」，但网易云候选里 en（王翊恩）翻唱版与原版歌名完全一致，
_pick 原先只给歌手 +0.05 加分（等于没约束）→ 选中王翊恩版，
于是画面 ORIGINAL 标成「en（王翊恩）」、封面用成 en 版专辑图、
LENGTH 取到翻唱版 dt=231.2s（真值 5:34=334s）。

三条防线：
  ① artist_matches()—— 歌手名一致性判定（含中英/昵称别名）
  ② _pick(reject_artist_mismatch=True) —— 原唱不符直接否决
  ③ 缓存 stale 时作废 cover_path —— 翻唱版封面不能靠缓存续命
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from songcut.lyrics_fetch import _pick, artist_matches  # noqa: E402

FAILS = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        FAILS.append(name)
        print("  FAIL %s -> got %r, want %r" % (name, got, want))


# ---------- ① artist_matches ----------
print("[1] artist_matches 歌手判定")
check("翻唱者≠原唱", artist_matches("张惠妹", "en（王翊恩）"), False)
check("同一人繁简/异写", artist_matches("周杰伦", "周杰倫"), True)
check("aMEI≈张惠妹", artist_matches("aMEI", "张惠妹"), True)
check("初音未来≡初音ミク", artist_matches("初音未来", "初音ミク"), True)
check("须田景凪≠初音ミク", artist_matches("初音ミク", "須田景凪"), False)
check("缺一侧不拦", artist_matches("", "张惠妹"), True)
check("完全相同", artist_matches("郭静", "郭静"), True)

# ---------- ② _pick 原唱否决 ----------
print("[2] _pick 原唱否决（连名带姓真实候选顺序）")
# 按网易云实际返回顺序：翻唱版排在前面
CANDS = [
    {"name": "连名带姓", "artist": "en（王翊恩）", "id": 1, "cover": "c1", "dur": 231186},
    {"name": "连名带姓", "artist": "张惠妹", "id": 2, "cover": "c2", "dur": 334000},
]
hit, _ = _pick(CANDS, "连名带姓", "张惠妹", reject_artist_mismatch=True)
check("否决模式选中原唱", (hit or {}).get("artist"), "张惠妹")
check("  且封面是原唱版", (hit or {}).get("cover"), "c2")
check("  且 dt 是原曲时长", (hit or {}).get("dur"), 334000)

# 关闭否决 → 复现旧行为（翻唱版靠前就赢），证明这条防线真的在起作用
hit_old, _ = _pick(CANDS, "连名带姓", "张惠妹")
print("     （对照）宽松模式选中 = %r—— 旧逻辑就是这里错的" % ((hit_old or {}).get("artist"),))

# 全候选都是翻唱版 → 否决模式下必须空手而归（宁可无封面，也不用错的）
ONLY_EN = [
    {"name": "连名带姓", "artist": "en（王翊恩）", "id": 1, "cover": "c1", "dur": 231186},
]
hit_none, score = _pick(ONLY_EN, "连名带姓", "张惠妹", reject_artist_mismatch=True)
check("无原唱候选时否决(返回None)", hit_none, None)

# 同名不同歌仍要能按歌名选（歌手信息缺失时不被误杀）
NO_ARTIST = [{"name": "连名带姓", "artist": "", "id": 3, "cover": "c3", "dur": 300000}]
hit_na, _ = _pick(NO_ARTIST, "连名带姓", "张惠妹", reject_artist_mismatch=True)
check("候选无歌手字段时仍可选中", (hit_na or {}).get("id"), 3)

# ---------- ③ 其它歌不回归 ----------
print("[3] 其它曲目不回归")
OTHER = [
    ("反方向的钟", "周杰伦", [{"name": "反方向的钟", "artist": "乐乐仔", "id": 9, "cover": "x", "dur": 1},
                      {"name": "反方向的钟", "artist": "周杰伦", "id": 8, "cover": "y", "dur": 2}], "周杰伦"),
    ("小夜子", "初音未来", [{"name": "小夜子", "artist": "初音ミク", "id": 7, "cover": "z", "dur": 3}], "初音ミク"),
]
for title, hint, cands, want in OTHER:
    h, _ = _pick(cands, title, hint, reject_artist_mismatch=True)
    check("%s 选中原唱" % title, (h or {}).get("artist"), want)

print("")
if FAILS:
    print("FAILED: %s" % ", ".join(FAILS))
    raise SystemExit(1)
print("全部通过")