# -*- coding: utf-8 -*-
"""Restore engine/fonts/*.ttf (Google Fonts, OFL) for the official PV engine.

The upstream engine ships 37 TTFs but only their OFL licence texts are tracked in this repo
(binaries are not committed). Without them Chromium falls back to a system serif, which
changes -- not breaks -- the JIZURA layer, and the runtime audit fails on the 404s.

Sources: github.com/google/fonts (main), every family below is OFL-1.1. Variable fonts keep
their `[wght]` glyph in the URL and are stored as `<Family>_wght_.ttf`, which is the name the
engine's @font-face rules ask for.

Usage:  python tools/fetch_fonts.py [--check]
        --check   only report which files are missing (no network)
"""
import pathlib
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEST = ROOT / 'engine' / 'fonts'
RAW = 'https://raw.githubusercontent.com/google/fonts/main/'

# local name -> path inside the google/fonts repo
FONTS = {}


def add(family_dir, names, rename=None):
    for n in names:
        remote = f'ofl/{family_dir}/{n}'
        local = (rename or {}).get(n, n)
        FONTS[local] = remote


add('delagothicone', ['DelaGothicOne-Regular.ttf'])
add('dotgothic16', ['DotGothic16-Regular.ttf'])
add('kaiseitokumin', ['KaiseiTokumin-Regular.ttf', 'KaiseiTokumin-Medium.ttf',
                      'KaiseiTokumin-Bold.ttf', 'KaiseiTokumin-ExtraBold.ttf'])
add('kleeone', ['KleeOne-Regular.ttf', 'KleeOne-SemiBold.ttf'])
add('mplusrounded1c', ['MPLUSRounded1c-Thin.ttf', 'MPLUSRounded1c-Light.ttf',
                       'MPLUSRounded1c-Regular.ttf', 'MPLUSRounded1c-Medium.ttf',
                       'MPLUSRounded1c-Bold.ttf', 'MPLUSRounded1c-ExtraBold.ttf',
                       'MPLUSRounded1c-Black.ttf'])
# variable fonts: `[wght]` in the repo, `_wght_` on disk
add('notosansjp', ['NotoSansJP[wght].ttf'], {'NotoSansJP[wght].ttf': 'NotoSansJP_wght_.ttf'})
add('notosanssc', ['NotoSansSC[wght].ttf'], {'NotoSansSC[wght].ttf': 'NotoSansSC_wght_.ttf'})
add('notoserifjp', ['NotoSerifJP[wght].ttf'], {'NotoSerifJP[wght].ttf': 'NotoSerifJP_wght_.ttf'})
add('notoserifsc', ['NotoSerifSC[wght].ttf'], {'NotoSerifSC[wght].ttf': 'NotoSerifSC_wght_.ttf'})
add('rampartone', ['RampartOne-Regular.ttf'])
add('reggaeone', ['ReggaeOne-Regular.ttf'])
add('shipporiminchob1', ['ShipporiMinchoB1-Regular.ttf', 'ShipporiMinchoB1-Medium.ttf',
                         'ShipporiMinchoB1-SemiBold.ttf', 'ShipporiMinchoB1-Bold.ttf',
                         'ShipporiMinchoB1-ExtraBold.ttf'])
add('yujisyuku', ['YujiSyuku-Regular.ttf'])
add('zenkakugothicnew', ['ZenKakuGothicNew-Light.ttf', 'ZenKakuGothicNew-Regular.ttf',
                         'ZenKakuGothicNew-Medium.ttf', 'ZenKakuGothicNew-Bold.ttf',
                         'ZenKakuGothicNew-Black.ttf'])
add('zenoldmincho', ['ZenOldMincho-Regular.ttf', 'ZenOldMincho-Medium.ttf',
                     'ZenOldMincho-SemiBold.ttf', 'ZenOldMincho-Bold.ttf',
                     'ZenOldMincho-Black.ttf'])


def ok_ttf(p):
    try:
        head = p.open('rb').read(4)
    except OSError:
        return False
    return head in (b'\x00\x01\x00\x00', b'OTTO', b'true', b'ttcf')


def main():
    check = '--check' in sys.argv
    DEST.mkdir(parents=True, exist_ok=True)
    missing = [n for n in FONTS if not (ok_ttf(DEST / n))]
    print(f'[fonts] {len(FONTS)} declared, {len(missing)} missing')
    if check:
        for n in missing:
            print('  MISS', n)
        return 0
    total = 0
    for i, name in enumerate(sorted(missing), 1):
        url = RAW + requests.utils.quote(FONTS[name], safe='/[]')
        r = requests.get(url, timeout=120)
        if r.status_code != 200 or len(r.content) < 1024:
            print(f'  [{i}/{len(missing)}] FAIL {name}  HTTP {r.status_code}')
            continue
        dst = DEST / name
        dst.write_bytes(r.content)
        if not ok_ttf(dst):
            print(f'  [{i}/{len(missing)}] FAIL {name}  not a TTF')
            dst.unlink(missing_ok=True)
            continue
        total += len(r.content)
        print(f'  [{i}/{len(missing)}] {name}  {len(r.content)/1048576:.1f} MB')
    print(f'[done] {total/1048576:.1f} MB written to {DEST}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
