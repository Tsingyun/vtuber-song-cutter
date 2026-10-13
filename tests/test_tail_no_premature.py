# -*- coding: utf-8 -*-
"""find_tail 不得「提前截断」回归测试（2026-10-13 事故）。

事故：《刻在我心底的名字》末句尾 5491.75，此后她仍在唱末段副歌（短窗 ASR 逐句
证实到 5558 都是官方 L41 歌词），伴奏 5567.25 才跌进静音。但末句之后人声压过
beat，帧 flags 在 5505.75 之后不再有 True → 旧 find_tail 直接返回
`last_music(5505.75) + TAIL_KEEP` = 5507.55，把 **78.8s 真演唱整段切掉**
（成片 267.0s vs 官方 320.2s，−53.2s）。

判据（RULES 第 6 条）：只要 rms > QUIET_DB 就仍在同一首歌里，**跌进静音**才算结束。
「最后一个像音乐的帧」只能说明 beat 判不出来，不能说明歌结束了。
"""
import os
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from songcut import music_bounds as MB   # noqa: E402


def _prof(rows):
    """rows: [(t, rms_db, flat, beat)] → profile 列表"""
    return [(float(t), float(r), float(f), float(b)) for (t, r, f, b) in rows]


def _mk(t0, t1, rms, flat=0.05, beat=0.30, hop=1.0):
    ts = np.arange(t0, t1 + 1e-6, hop)
    return [(t, rms, flat, beat) for t in ts]


def test_no_premature_truncation_when_audio_continues():
    """末句后 10s 起判不出音乐性、但仍有声直到 +50s → 结束点必须落在 +50s，而非 +10s。"""
    prof = _prof(_mk(0, 10, -20) + _mk(11, 50, -30.0) + _mk(51, 60, -95.0))
    # 让「像音乐」只在 0~10 出现：把 11~50 的 beat 压低（score < SCORE_T）
    prof = [(t, r, f, (b if t <= 10 else 0.0)) for (t, r, f, b) in prof]
    t, note = MB.find_tail(prof, 10.0, fwd=60.0)
    assert t is not None, note
    assert abs(t - (51.0 + MB.TAIL_KEEP)) <= 1.5, (
        "尾部被提前截断在 %.2f（应为 ~%.2f）: %s" % (t, 51.0 + MB.TAIL_KEEP, note))


def test_silence_boundary_is_used_not_last_music_frame():
    """静音出现在 +30s，且 +20s 处还有一个音乐帧 → 取静音边界，不取 music 帧。"""
    prof = _prof(_mk(0, 20, -20) + _mk(21, 29, -25) + _mk(30, 45, -90))
    t, note = MB.find_tail(prof, 10.0, fwd=60.0)
    assert t is not None, note
    assert abs(t - (30.0 + MB.TAIL_KEEP)) <= 1.0, (t, note)


def test_window_without_silence_returns_none_not_last_music():
    """整窗始终有声（歌后说话/加唱）→ 返回 None，绝不提前截断。"""
    prof = _prof(_mk(0, 60, -25))
    t, note = MB.find_tail(prof, 10.0, fwd=60.0)
    assert t is None, "窗口内无声静音时不得给出结束点（得到 %.2f / %s）" % (t, note)
    assert "未见静音" in note, note


def test_empty_prof():
    t, note = MB.find_tail([], 10.0)
    assert t is None and "无特征数据" in note


if __name__ == "__main__":
    import traceback
    ok = 0
    for nm, fn in sorted(list(globals().items())):
        if nm.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS %s" % nm)
                ok += 1
            except AssertionError as e:
                print("FAIL %s: %s" % (nm, e))
            except Exception:
                print("ERROR %s" % nm)
                traceback.print_exc()
    print("%d 项通过" % ok)
