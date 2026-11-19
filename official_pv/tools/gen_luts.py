# -*- coding: utf-8 -*-
"""Rebuild assets/style/luts/P1..P5(.png) from assets/style/palettes.json.

The upstream project ships five LUT ramps as PNGs, but only `palettes.json` is tracked in
this repo (the PNGs are binary assets and were never committed). `core/main.js:buildLutAtlas`
draws each of them into the atlas as `drawImage(img, 0,0, img.width,img.height, 0,row, 256,1)`
-- i.e. every one of them is consumed as a **256x1 horizontal gradient**. palettes.json still
carries the exact stops (`gradmap` for the colour ramp, `duotone` for the 2-stop variant), so
the PNGs can be reconstructed losslessly instead of hunted down:

    P<n>_name.png      <- gradmap   stops
    P<n>_name_duo.png  <- duotone   [dark, light]

Deterministic: same json in, byte-identical PNGs out (Pillow, no randomness).

Usage:  python tools/gen_luts.py [--check]
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAL = ROOT / 'assets' / 'style' / 'palettes.json'
OUT = ROOT / 'assets' / 'style' / 'luts'


def _rgb(h):
    h = h.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, k):
    return tuple(round(a[i] + (b[i] - a[i]) * k) for i in range(3))


def ramp_pixels(stops, n=256):
    """Piecewise-linear interpolation over [(k, '#RRGGBB'), ...] -> n RGB triples."""
    st = sorted((float(k), _rgb(c)) for k, c in stops)
    if not st:
        raise ValueError('empty ramp')
    if st[0][0] > 0:
        st.insert(0, (0.0, st[0][1]))
    if st[-1][0] < 1:
        st.append((1.0, st[-1][1]))
    out = []
    j = 0
    for i in range(n):
        x = i / (n - 1)
        while j + 1 < len(st) - 1 and x > st[j + 1][0]:
            j += 1
        k0, c0 = st[j]
        k1, c1 = st[j + 1]
        t = 0.0 if k1 <= k0 else (x - k0) / (k1 - k0)
        out.append(_mix(c0, c1, max(0.0, min(1.0, t))))
    return out


def main():
    from PIL import Image
    data = json.loads(PAL.read_text(encoding='utf-8'))
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    for p in data['palettes']:
        jobs = [(p['lut'], p['gradmap']),
                (p['lut_duotone'], [[0, p['duotone'][0]], [1, p['duotone'][1]]])]
        for rel, stops in jobs:
            dst = ROOT / rel
            px = ramp_pixels(stops)
            im = Image.new('RGB', (256, 1))
            im.putdata(px)
            im.save(dst, optimize=True)
            made.append((rel, dst.stat().st_size))
    for rel, size in made:
        print(f'[lut] {rel}  {size} B')
    print(f'[done] {len(made)} ramps -> {OUT}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:                      # noqa: BLE001
        print(f'[FATAL] {e}', file=sys.stderr)
        sys.exit(1)
