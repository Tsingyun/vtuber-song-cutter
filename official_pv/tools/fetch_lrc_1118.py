# -*- coding: utf-8 -*-
"""Fetch / stage official LRC + ORIGINAL-artist cover for the 2026-11-18 cuts.

Boundary (RULES.md 129): this script WRITES only inside official_pv/songs/.
A-side files under D:/岁己歌切/_media_cache/ are READ ONLY.  The A-side helper
lyrics_fetch.py is loaded by path (read-only, never imported as a package), and
its cache_dir is redirected into our own folder so it writes nothing back.

Usage:  python tools/fetch_lrc_1118.py
"""
import importlib.util, io, json, pathlib, re, shutil, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
A_CACHE = pathlib.Path(r'D:\岁己歌切\_media_cache')
DEST = ROOT / 'songs' / '_lrc_cache'
DEST.mkdir(parents=True, exist_ok=True)
SRT = pathlib.Path(r'D:\岁己直播自动总结\SRT\20261118-陪你这个猪过周三【新衣活动中 2026年11月18日20点场.srt')

# (id, search title, ORIGINAL artist, official duration)
SONGS = [
    ('zouma',          '走马',   '陈粒',      235.555),
    ('shuangrenchuan', '双人船', 'ChiliChill', 214.498),
    ('yuniyouguan',    '与你有关', '泠鸢yousa', 221.006),
]


def srt_window_text(t0, t1):
    """Concatenate ASR text in [t0,t1] seconds -- used as ref_text to reject
    wrong same-title versions (house rule: align to what was ACTUALLY sung)."""
    if not SRT.exists():
        print(f'[warn] SRT missing: {SRT}')
        return ''
    txt = SRT.read_text(encoding='utf-8', errors='replace')
    out = []
    for blk in re.split(r'\n\s*\n', txt):
        m = re.search(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->', blk)
        if not m:
            continue
        s = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000
        if t0 <= s <= t1:
            body = [l for l in blk.splitlines() if not re.match(r'^\d+$', l.strip())
                    and '-->' not in l and l.strip()]
            out.extend(body)
    return ' '.join(out)


REF = {
    'zouma': srt_window_text(6760, 6995),
    'shuangrenchuan': srt_window_text(7288, 7502),
    'yuniyouguan': srt_window_text(8011, 8230),
}


def load_a_helper():
    p = pathlib.Path(r'D:\岁己歌切\songcut\lyrics_fetch.py')
    spec = importlib.util.spec_from_file_location('_lf_readonly', str(p))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    lf = load_a_helper()
    report = {}
    for sid, title, artist, dur in SONGS:
        rec = {'title': title, 'original_artist': artist, 'official_duration': dur}
        print(f'[fetch] {title} / {artist}  ref_chars={len(REF[sid])}')
        lrc, cover_dst, info = lf.fetch_lyrics_and_cover(
            title, artist_hint=artist, dur=dur,
            cache_dir=str(DEST), ref_text=REF[sid])
        rec['source'] = 'online fetch (lrclib + netease)'
        rec['lyrics_source'] = info.get('lyrics_source')
        rec['info'] = {k: info.get(k) for k in
                       ('artist', 'netease_duration_ms', 'lyrics_lines',
                        'lyrics_tail_sec', 'lyrics_coverage', 'lyrics_overlap',
                        'warn_lyrics_mismatch', 'warn_artist_mismatch')}
        if cover_dst and pathlib.Path(cover_dst).exists():
            dst = DEST / f'_cover_{sid}.img'
            if pathlib.Path(cover_dst).resolve() != dst.resolve():
                shutil.copyfile(cover_dst, dst)
            rec['cover'] = str(dst)
        if not lrc:
            print(f'[FATAL] {title}: no LRC obtained', file=sys.stderr)
            sys.exit(2)
        lrc_path = ROOT / 'songs' / f'{sid}.lrc'
        lrc_path.write_text(lrc, encoding='utf-8')
        lines = re.findall(r'^\[(\d+):(\d+(?:\.\d+)?)\](.*)$', lrc, re.M)
        rec['lrc_file'] = str(lrc_path)
        rec['lines'] = len(lines)
        rec['first_line'] = f'[{lines[0][0]}:{lines[0][1]}]{lines[0][2]}' if lines else None
        rec['last_line'] = f'[{lines[-1][0]}:{lines[-1][1]}]{lines[-1][2]}' if lines else None
        report[sid] = rec
        print(f"[ok] {title:<8} orig={artist:<12} lines={rec['lines']:<3} "
              f"first={rec['first_line']} cover={'yes' if rec.get('cover') else 'NO'}")
    (DEST / 'fetch_report_1118.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[done] report -> {DEST / "fetch_report_1118.json"}')


if __name__ == '__main__':
    main()
