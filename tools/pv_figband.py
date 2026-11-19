#!/usr/bin/env python
"""对**已有**的 storyboard 补跑「文字版心让开大幅人物」这一趟（不改动任何随机结果）。

用途：导演层（tools/pv_direct.py）里的 fig_band_fix 只在重新生成编排时生效，
但重生成会重掷所有随机数、构图全变。已有编排稿要享受同一条规则，就用这个工具原地补。

    python tools/pv_figband.py --sb _tmp/pv/sb_full.json [--dry]

幂等：跑第二次不会再有任何改动。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pv_direct import fig_band_fix   # noqa: E402  唯一的规则来源


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sb", required=True)
    ap.add_argument("--dry", action="store_true", help="只打印将改动什么")
    a = ap.parse_args()

    sb = json.load(open(a.sb, encoding="utf-8"))
    shots = sb.get("shots") or []
    ch = fig_band_fix(shots)
    if not ch:
        print("[band] 无需改动（%d 个镜头已全部合规）" % len(shots))
        return 0
    print("[band] %d 个镜头补上文字版心" % len(ch))
    for i, t0, mode, side, cx, aw in ch:
        print("   #%-2s @%6.2fs %-5s/%-1s → cx=%-5d availW=%.2f" % (i, t0, mode, side, cx, aw))
    if a.dry:
        print("[dry] 未写盘")
        return 0
    json.dump(sb, open(a.sb, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("[out] %s  %.0fKB" % (a.sb, os.path.getsize(a.sb) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
