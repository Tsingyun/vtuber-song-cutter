# -*- coding: utf-8 -*-
"""成片规格唯一真源的回归测试。

跑法（两种都行）：
    python tests/test_spec.py
    python -m pytest tests/test_spec.py -q

为什么单独给它写测试：规格判据曾经在三个文件里各写一遍，其中 `song_cutter.py`
写死了 `fps >= 59.9`，导致 4K30 的手书 PV 在主流程里永远「规格不达标」。
这个文件锁住三件事：30/60 都收、非法值不收、码率安全区双向夹逼。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from songcut import spec  # noqa: E402

FAILS = []


def ck(cond, name, detail=""):
    if cond:
        print("  ✓ %s" % name)
    else:
        print("  ✗ %s %s" % (name, detail))
        FAILS.append(name)


def main():
    print("[spec] 帧率")
    for f in (30, 30.0, 29.97, 30.03, 60, 59.94, 60.06):
        ck(spec.fps_ok(f), "接受 %s" % f)
    for f in (25, 50, 24, 120, 0, None, "abc"):
        ck(not spec.fps_ok(f), "拒绝 %r" % (f,))

    print("[spec] 分辨率")
    ck(spec.res_ok(1920, 1080), "1080p")
    ck(spec.res_ok(3840, 2160), "2160p")
    ck(not spec.res_ok(2560, 1440), "拒绝 1440p")
    ck(not spec.res_ok(None, 1080), "拒绝缺值")

    print("[spec] 码率（双向夹逼：过低会被平台压糊，过高同样触发二压）")
    lo, hi = spec.VBR_SAFE_KBPS
    ck(spec.vbr_ok(lo), "下限 %d 收" % lo)
    ck(spec.vbr_ok(hi), "上限 %d 收" % hi)
    ck(not spec.vbr_ok(lo - 1), "下限-1 拒")
    ck(not spec.vbr_ok(hi + 1), "上限+1 拒")
    ck(not spec.vbr_ok(spec.VBR_HARD_MAX_KBPS + 500), "硬上限外拒")

    print("[spec] 总判 check()")
    ok, fails = spec.check({"width": 3840, "height": 2160, "fps": 30.0,
                            "video_bitrate_kbps": 18000})
    ck(ok and not fails, "4K30/18Mbps 合格")
    ok, fails = spec.check({"width": 1920, "height": 1080, "fps": 60.0,
                            "video_bitrate_kbps": 8000})
    ck((not ok) and len(fails) == 1, "低码率被拒且只报一条", str(fails))
    ok, fails = spec.check({"width": 2560, "height": 1440, "fps": 25,
                            "video_bitrate_kbps": 18000})
    ck((not ok) and len(fails) == 2, "分辨率+帧率各报一条", str(fails))

    print("[spec] 吸附与描述")
    ck(spec.fps_nearest(29.97) == 30 and spec.fps_nearest(59.94) == 60, "帧率吸附")
    ck("30/60" in spec.describe() and "3840x2160" in spec.describe(), "describe 含关键口径")

    print()
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
