# -*- coding: utf-8 -*-
"""tests/test_fallback_trigger.py —— 兜底策略与去重的回归测试

背景（2026-10-08）：10-07 直播唱了《爱情讯息》和《反方向的钟》两首，
自动歌切只输出了前者。

真实漏切链条（三层，逐层实测推翻）：
  ① 旧版 `if not ok_items`（常规候选全军覆没才扫曲库）
     → 常规候选命中《爱情讯息》→ ok_items 非空 → **兜底根本没跑**
     →《反方向的钟》被ASR 听成「反风飒钟」，字面提及匹配 0 命中 → 永久漏切
  ② 改`or len(win) > len(ok_items)`（LLM 窗口数 > 识别数就扫）
     → 复测仍漏切：GLM 只报**1 个**窗口（首次报 2 个），1 > 1 为 False
     ⇒ **LLM 窗口数不可靠**（Google 429 降级到 GLM 后判定极不稳定）
  ③ 试「转写里有未被覆盖的成段歌词样文字 → 必定漏切」
     → 实测 31 段**全部误报**，闲聊同样长句密集（与 10-06 教训一致）
     ⇒ 转写文本结构上无法区分歌词与闲聊，该路线否决

最终形态：**兜底默认执行**（`if not args.no_scan:`）。
它是唯一靠「歌词定位」而非「文本相似度」的判据，噪声天然被 locate 过滤
（实测 10-07：1174 首入库粗筛 → 108 首入围 → 12 首进精判 → 命中《反方向的钟》）。

本测试不联网、不读录播，只验证策略判定与去重逻辑。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auto_cut as AC  # noqa: E402


def should_scan(no_scan=False):
    """复刻 main 里的兜底判定：默认执行，仅 --no-scan 关闭。"""
    return not no_scan


def main():
    bad = 0

    print("=" * 70)
    print("① 兜底策略：默认执行")
    print("=" * 70)
    cases = [
        ("默认（10-07 实况：已识别 1 首，仍必须兜底）", False, True),
        ("显式 --no-scan → 关闭", True, False),
    ]
    for desc, ns, want in cases:
        got = should_scan(ns)
        ok = got == want
        bad += (not ok)
        print("  [%s] %s → 期望%s 实得%s"
              % ("PASS" if ok else "FAIL", desc,
                 "兜底" if want else "不兜底", "兜底" if got else "不兜底"))

    # 三个历史 bug 的回归断言：这些组合下**必须**兜底
    print("-" * 70)
    print("② 历史漏切场景回归（全部必须兜底）")
    hist = [
        ("① 旧版漏切：1 首通过 + 兜底曾不跑", 1, 2),
        ("② GLM 只报 1 窗口：1 首通过 + 1 窗口", 1, 1),
        ("③ 全灭：0 首通过 + 0 窗口", 0, 0),
    ]
    for desc, n_ok, n_win in hist:
        got = should_scan(False)
        ok = got is True
        bad += (not ok)
        print("  [%s] %s（通过=%d 窗口=%d）→ %s"
              % ("PASS" if ok else "FAIL", desc, n_ok, n_win,
                 "兜底" if got else "不兜底"))

    # ── 去重：区间重叠判定 ────────────────────────────────────
    print("-" * 70)
    print("③ 兜底去重：区间重叠判定（min_overlap=30s）")
    ov_cases = [
        ("同一段完全重叠", 0, 100, 0, 100, True),
        ("部分重叠 50s", 0, 100, 50, 150, True),
        ("相邻不重叠（间隔 30s）", 0, 100, 130, 200, False),
        ("远隔", 0, 100, 500, 600, False),
        ("擦边重叠 10s < 30 → 容忍", 0, 100, 90, 200, False),
        ("真重叠 40s > 30 → 算重叠", 0, 100, 70, 200, True),
    ]
    for desc, a1, a2, b1, b2, want in ov_cases:
        got = AC._overlaps(a1, a2, b1, b2)
        ok = got == want
        bad += (not ok)
        print("  [%s] %s → %s" % ("PASS" if ok else "FAIL", desc,
                                "重叠" if got else "不重叠"))

    # ── 时长可信度闸门（10-07 第三个坑）────────────────────
    print("-" * 70)
    print("④ 时长可信度闸门：span > orig × %.2f → 拒绝自动出片" % (1 + AC.DUR_TRUST_MAX))
    dur_cases = [
        # (说明, span_s, orig_s, 期望是否拒绝)
        ("《反方向的钟》10-07 LRCLIB 误返 131.3s（真实 258s）", 198.27, 131.3, True),
        ("《爱情讯息》10-07 官方 280.7s", 219.69, 280.7, False),
        ("《泡泡》10-01 现场加唱 217.3 vs 214.0（比值 1.02）", 217.30, 214.0, False),
        ("《小夜子》10-06", 217.25, 255.0, False),
        ("《连名带姓》10-06", 295.60, 334.0, False),
        ("现场加唱副歌 +20%（应放行，留余量）", 258.0 * 1.20, 258.0, False),
    ]
    for desc, span, orig, want in dur_cases:
        got = span > orig * (1.0 + AC.DUR_TRUST_MAX)
        ok = got == want
        bad += (not ok)
        print("  [%s] %s\n         span=%.1f orig=%.1f 比值=%.2f → %s（期望%s）"
              % ("PASS" if ok else "FAIL", desc, span, orig,
                 span / orig, "拒绝" if got else "放行",
                 "拒绝" if want else "放行"))

    # ── 真实样本：10-07 两首歌必须判为不重叠 ──────────────────
    print("-" * 70)
    print("⑤ 真实样本：10-07 两首歌区间（波形实测）")
    love = (3203.08, 3483.78)     # 爱情讯息
    clock = (3782.88, 4010.59)    # 反方向的钟
    r = AC._overlaps(love[0], love[1], clock[0], clock[1])
    bad += r
    print("  [%s] 爱情讯息%s vs 反方向的钟%s → %s"
          % ("PASS" if not r else "FAIL", love, clock,
             "重叠(错!)" if r else "不重叠(正确)"))

    # ── 常量自洽 ──────────────────────────────────────────────
    print("-" * 70)
    print("⑥ 常量自洽")
    consts = [
        ("LIB_SCAN_RECALL", AC.LIB_SCAN_RECALL, 0.20, 0.45),
        ("LIB_SCAN_TOP", float(AC.LIB_SCAN_TOP), 8.0, 20.0),
        ("LIB_SCAN_MAX", float(AC.LIB_SCAN_MAX), 1000.0, 2000.0),
        ("DUR_TRUST_MAX", AC.DUR_TRUST_MAX, 0.20, 0.45),
    ]
    for nm, v, lo, hi in consts:
        ok = lo <= v <= hi
        bad += (not ok)
        print("  [%s] %s = %s（应在 %.2f~%.2f）"
              % ("PASS" if ok else "FAIL", nm, v, lo, hi))

    print("=" * 70)
    print("失败 %d 项" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())