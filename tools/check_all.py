# -*- coding: utf-8 -*-
"""一键检查：在改完代码、还没出片之前，用 30 秒确认「没有把地基弄坏」。

分三层（快的在前，任一层失败即停）：
  ① 语法/导入 —— compileall + 关键模块 import；
  ② 纯逻辑单测 —— tests/test_spec.py（规格唯一真源）+ 其余可独立运行的测试；
  ③ 素材/字体 —— pv_fonts_audit.py（16 个中文字面在不在、许可证在不在）。

不做的事（故意的）：
  · 不跑 4K 渲染、不跑 pv_flash/pv_uniform —— 它们要片子，属于「出片后验收」；
  · 不跑 pytest 收集到的全部测试（其中部分依赖真实录播/GPU）。

用法：
    python tools/check_all.py            # 全跑
    python tools/check_all.py --fast     # 只跑 ①（改一行代码时用）
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def run(name, cmd, cwd=ROOT, quiet=True):
    print("  · %s" % name, flush=True)
    r = subprocess.run(cmd, cwd=cwd, capture_output=quiet, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("    ✗ 失败（rc=%d）" % r.returncode)
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.strip().splitlines()[-25:]:
            print("      " + line)
        return False
    if not quiet and r.stdout:
        for line in r.stdout.strip().splitlines()[-5:]:
            print("    " + line)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="只做语法/导入")
    a = ap.parse_args()

    ok = True
    print("[① 语法与导入]")
    ok &= run("compileall", [PY, "-m", "compileall", "-q",
                             "songcut", "song_cutter.py", "auto_cut.py", "tools"])
    ok &= run("import songcut.spec / qc / config", [PY, "-c",
              "import sys;sys.path.insert(0,%r);"
              "from songcut import spec, config;"
              "import songcut.qc as q" % ROOT])
    if a.fast:
        print("\n%s" % ("通过 ✓" if ok else "失败 ✗"))
        return 0 if ok else 1

    print("[② 纯逻辑单测]")
    ok &= run("tests/test_spec.py", [PY, os.path.join("tests", "test_spec.py")], quiet=False)

    print("[③ 页面契约]")
    ok &= run("pv_pages.py", [PY, os.path.join("tools", "pv_pages.py")])

    print("[④ 字体库]")
    ok &= run("pv_fonts_audit.py", [PY, os.path.join("tools", "pv_fonts_audit.py")])

    print("\n%s" % ("全部通过 ✓" if ok else "有失败 ✗（先修再出片）"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
