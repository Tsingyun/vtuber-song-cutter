# -*- coding: utf-8 -*-
"""List every official scene and what it preloads (asset deps that may be missing)."""
import glob
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for f in sorted(glob.glob(os.path.join(ROOT, 'engine', 'scenes', '*.js'))):
    s = io.open(f, encoding='utf-8').read()
    parts = re.split(r"Z\.scene\('", s)
    for p in parts[1:]:
        name = p.split("'")[0]
        body = p[:900]
        pm = re.search(r'preload:\s*([^\n]+)', body)
        dep = pm.group(1).strip() if pm else '(none)'
        print('%-12s %s' % (name, dep[:110]))
