#!/usr/bin/env python
"""切点连续性门禁：找出「切点上闪一下 / 动画走完还是上一镜」的帧。

判据从缺陷的物理形态来，不靠阈值拍脑袋。一次转场里，画面每换一张画
（koma 换态）就会产生一次帧差；这些帧差应当在转场窗口内**量级相近**。
真正出问题的地方，帧差会在窗口的两端**孤峰突起**：

  · 窗口开头（切点那一两帧）突然很大  → 退场已经把画面淡空，转场又把
    「退场起点」的满墨快照盖了回来（弹回）。观感：闪一下。
  · 窗口末尾（转场结束那一两帧）突然很大 → 转场几何写反了，A 在 p→1 时
    盖满全屏，然后被丢弃。观感：动画走完了还是上一张，然后瞬切。

于是判据 = 「两端帧差 / 窗口内中位帧差」。中位数按「换画帧」统计（帧差>0.03），
否则 koma 的同画重复帧（帧差≈0）会把基线拉到 0，任何转场都判失败。

用法：
    python tools/pv_flash.py --mp4 out.mp4 --sb sb.json [--fps 30] [--tol 2.0]
    python tools/pv_flash.py --mp4 out.mp4 --scan          # 全片孤立帧扫描

退出码：有超阈切点 → 1（可直接当门禁用）。
"""
import argparse
import json
import subprocess
import sys

import numpy as np

FF = "D:/ffmpeg/bin/ffmpeg.exe"
W, H = 320, 180


def decode(mp4, fps):
    cmd = [FF, "-v", "error", "-i", mp4, "-vf", "fps=%g,scale=%d:%d,format=gray" % (fps, W, H),
           "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    n = W * H
    out = []
    while True:
        b = p.stdout.read(n)
        if len(b) < n:
            break
        out.append(np.frombuffer(b, np.uint8).reshape(H, W))
    p.stdout.close()
    p.wait()
    return np.stack(out) if out else np.zeros((0, H, W), np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mp4", required=True)
    ap.add_argument("--sb", default="")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--tol", type=float, default=2.0, help="两端帧差 / 窗口内中位 的上限")
    ap.add_argument("--scan", action="store_true", help="额外做全片孤立帧扫描（慢，默认关）")
    a = ap.parse_args()

    F = decode(a.mp4, a.fps).astype(np.int16)
    n = len(F)
    if n < 10:
        print("[err] 没解出帧")
        return 1
    d = np.zeros(n)
    d[1:] = np.abs(F[1:] - F[:-1]).mean(axis=(1, 2))
    lut = F.mean(axis=(1, 2))                       # 每帧平均亮度
    dl = np.zeros(n)
    dl[1:] = lut[1:] - lut[:-1]                     # 亮度变化（正 = 变亮）
    print("[flash] %s  帧数 %d  时长 %.2fs  d: mean %.2f p95 %.2f max %.2f"
          % (a.mp4.split("/")[-1], n, n / a.fps, d.mean(), np.percentile(d, 95), d.max()))

    if not a.sb:
        print("[err] 需要 --sb（编排稿）才能定位到切点")
        return 1
    sb = json.load(open(a.sb, encoding="utf-8"))
    shots = sb.get("shots", [])
    # 只认 --fps：storyboard.meta.fps 是导演层的默认值（60），和成片帧率无关

    rows, bad, ncut = [], [], 0
    for s in shots:
        if s["i"] <= 1:
            continue
        t0 = s["t0"]
        tr = s.get("trans") or {}
        k = tr.get("k") or "(派生)"
        dur = float(tr.get("dur") or 0.42)
        ic = int(round(t0 * a.fps))                    # 切点第一帧
        iend = int(round((t0 + dur) * a.fps))          # 转场最后一帧
        lo = max(1, ic - 6)                            # 切点前 6 帧（上一镜自己的步长）
        hi = min(n, iend + 4)
        if ic < 2 or iend >= n or iend - ic < 3:
            continue
        ncut += 1
        # ① 切点必须接得上「它前面」：转场在 p=0 时整幅盖 A，A 就是上一帧，
        #    所以切点这一帧的帧差应当与上一镜自身的步长同量级，而不是孤峰。
        prev_step = float(np.max(d[lo:ic - 1])) if ic - 2 >= lo else float(d[ic - 1])
        # 抽帧滞后：t 被量化到 1/koma 栅格，切点后 0~2 个输出帧才真正换镜
        dcut = float(np.max(d[ic:min(n, ic + 3)]))
        # 下限 3.0 而不是 2.0：转场**第一格**本来就会吃掉一小块（mask/iris 从中心开、
        # wipe 的第一刀横跨 1/5 画幅），拿它跟「上一镜静止时的步长」比只在大出
        # 一个量级时才说明问题。真正的「弹回」实测在 15~37，3.0 挡得住。
        thr_cut = max(3.0, 1.6 * prev_step)
        # ② 转场结尾不能比自己内部的步长更大：p→1 时 A 只剩一条缝，那一步很小；
        #    若几何写反，p→1 时 A 盖满全屏、随即被丢弃 ⇒ 末端一根孤峰。
        seg = d[ic:iend + 1]
        # 「换画帧」的下限必须高于编码噪声：1080p 12Mbps 时同画的重复帧是**逐字节相同**
        # （d=0.00），4K 18Mbps 下同一张画的重编会有 0.03~0.07 的抖动。拿 0.03 当门槛
        # 会把 4K 成片里所有「静止帧」都算进中位，中位被拉到 0.07，于是每个切点都误报。
        act = seg[seg > 0.30]
        med = float(np.median(act)) if len(act) >= 2 else max(float(seg.max()), 0.05)
        # 转场之后的「本底节奏」：新镜自己也在动（逐词出现、相机推），拿它当中位基线的
        # 一部分，否则「转场结束正好撞上一个词跳出来」会被误判成末端瞬切。
        after = d[min(n - 1, iend + 2):min(n, iend + 9)]
        med_after = float(np.median(after[after > 0.30])) if (after > 0.30).sum() >= 2 else 0.0
        dend = float(np.max(d[iend:min(n, iend + 3)]))
        thr_end = max(2.0, 1.6 * max(med, med_after, 0.05))
        # ③ 闪白是**设计上的一次亮度冲击**：判据落在亮度而不是结构。
        #    只要那一两帧确实变亮了（且随后回落），切点帧差大是应该的。
        #    真正的缺陷（残留白幕 / 淡不回去）由 dend 抓，不在这里放过。
        is_flash = (k == "flash") and float(np.max(dl[ic:min(n, ic + 3)])) > 5.0
        # 0.05 的数值容差：阈值本身是「灰阶差」这种整数级量，卡在 3.00/3.00 上
        # 报失败没有意义；真正要抓的是成倍超出（实测缺陷在 15~37）。
        bad_cut = (not is_flash) and dcut > thr_cut + 0.05 and dcut > 1.0
        bad_end = dend > thr_end + 0.05 and dend > 1.0
        rows.append((t0, k, dur, dcut, thr_cut, dend, thr_end, med, bad_cut, bad_end, is_flash))
        if bad_cut or bad_end:
            bad.append(rows[-1])

    print("[cut] %d 个切点\n" % ncut)
    print("   t0      trans      dur   切点d /上限    末端d /上限   转场内中位")
    for t0, k, dur, dcut, tc, dend, te, med, bc, be, fl in rows:
        flag = ("  ·闪白(设计)" if fl else "") + ("  ❌切点弹回" if bc else "") + \
               ("  ❌末端瞬切" if be else "")
        print("  %7.2f  %-9s %5.2f  %6.2f /%5.1f  %6.2f /%5.1f   %5.2f%s"
              % (t0, k, dur, dcut, tc, dend, te, med, flag))

    if bad:
        print("\n[FAIL] %d 个切点不连续：" % len(bad))
        for t0, k, dur, dcut, tc, dend, te, med, bc, be, fl in bad:
            why = []
            if bc:
                why.append("切点弹回（%.1f > %.1f）" % (dcut, tc))
            if be:
                why.append("末端瞬切（%.1f > %.1f）" % (dend, te))
            print("   %7.2fs %-9s %s" % (t0, k, " / ".join(why)))
    else:
        print("\n[OK] 全部切点两端连续")

    if a.scan:
        span = np.zeros(n)
        span[1:-1] = np.abs(F[2:] - F[:-2]).mean(axis=(1, 2))
        hits = []
        for i in range(1, n - 1):
            if min(d[i], d[i + 1]) < 0.8:
                continue
            r = min(d[i], d[i + 1]) / max(span[i], 1e-6)
            if r > 3.0:
                hits.append((i / a.fps, r))
        print("\n[scan] 孤立帧（前后都不同、前后彼此相同）%d 处" % len(hits))
        for t, r in sorted(hits, key=lambda x: -x[1])[:12]:
            print("   %7.3fs  ×%.1f" % (t, r))

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
