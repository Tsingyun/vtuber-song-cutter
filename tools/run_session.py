# -*- coding: utf-8 -*-
"""对同一自然日里的**指定一场**录播跑 auto_cut 全流程。

背景（RULES 第 35 / 71 条）
-------------------------
`song_cutter.find_video(date)` 同一天有多场录播时按 **ffprobe 时长最长** 选一场，
这挡的是「.part 残件 + 别场成品」；但遇到「白天长游戏联动场 + 夜间较短主场」的组合，
最长的一场恰恰不是会唱歌的那场，夜间主场会被**整场跳过**。

本工具用 monkeypatch 把 `find_video` 换成指定文件（**不改生产线代码、不动用户文件**），
再把该场的报告另存一份 `_<date>_<tag>.json`，方便逐场跑完后合并定性。

用法::

    python tools/run_session.py --date 2026-10-28 \\
        --src "D:/录播/25788785-岁己SUI/2026-10-28/录制-...-22点场.mp4" \\
        --tag 22点场 --pages 1

多余的命令行参数（``--pages / --no-cut / --no-llm / --no-scan ...``）原样透传给 auto_cut。
"""
from __future__ import print_function

import io
import os
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    passthru = [a for a in argv]
    try:
        i = passthru.index("--src")
        src = passthru[i + 1]
        del passthru[i:i + 2]
    except (ValueError, IndexError):
        print("用法: run_session.py --src <录播文件> --tag <场次标签> [--date YYYY-MM-DD] [其它 auto_cut 参数]")
        return 2
    try:
        i = passthru.index("--tag")
        tag = passthru[i + 1]
        del passthru[i:i + 2]
    except (ValueError, IndexError):
        tag = os.path.splitext(os.path.basename(src))[0][:24]

    import song_cutter as SC
    import auto_cut as AC

    if not os.path.isfile(src):
        print("录播文件不存在：%s" % src)
        return 2

    SC.find_video = lambda date: src  # 指定场次，绕开「取最长」

    _orig_write = AC.write_report

    def _wr(rep_dir, date, rep):
        p = _orig_write(rep_dir, date, rep)
        try:
            shutil.copy(os.path.join(rep_dir, date + ".json"),
                        os.path.join(rep_dir, "_%s_%s.json" % (date, tag)))
        except Exception as e:  # 备份失败不影响主流程
            print("警告：分场报告副本写入失败：%s" % e)
        return p

    AC.write_report = _wr

    sys.argv = ["auto_cut.py"] + passthru
    AC.main()
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
