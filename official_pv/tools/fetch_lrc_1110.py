# -*- coding: utf-8 -*-
"""Fetch / stage official LRC + ORIGINAL-artist cover for the 2026-11-10 cuts.

Boundary (RULES.md 129): this script WRITES only inside official_pv/songs/.
A-side files under D:/岁己歌切/_media_cache/ are READ ONLY.  The A-side helper
lyrics_fetch.py is loaded by path (read-only, never imported as a package), and
its cache_dir is redirected into our own folder so it writes nothing back.

Usage:  python tools/fetch_lrc_1110.py
"""
import importlib.util, io, json, pathlib, re, shutil, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
A_CACHE = pathlib.Path(r'D:\岁己歌切\_media_cache')
DEST = ROOT / 'songs' / '_lrc_cache'
DEST.mkdir(parents=True, exist_ok=True)
SRT = pathlib.Path(r'D:\岁己直播自动总结\SRT\20261110-陪你这个猪过周二！ 2026年11月10日20点场.srt')

# (id, search title, A-side cache key, ORIGINAL artist, official duration)
SONGS = [
    ('natianxiayule', '那天下雨了', None,            '周杰伦', 223.0),
    ('yiyangdeyueguang', '一样的月光', '一样的月光', '徐佳莹', 240.386),
    ('xiageluokoujian', '下个, 路口, 见', '下个_路口_见', '李宇春', 210.373),
]


def srt_window_text(t0, t1):
    """Concatenate ASR text in [t0,t1] seconds -- used as ref_text to reject
    wrong same-title versions (house rule: align to what was ACTUALLY sung)."""
    if not SRT.exists():
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
    'natianxiayule': srt_window_text(5810, 6010),
    'yiyangdeyueguang': srt_window_text(6300, 6530),
    'xiageluokoujian': srt_window_text(6690, 6890),
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
    for sid, title, akey, artist, dur in SONGS:
        rec = {'title': title, 'original_artist': artist, 'official_duration': dur}
        if akey:
            # Reuse the vetted A-side cache (read-only), copy into our own tree.
            src = A_CACHE / f'_lrc_{akey}.json'
            d = json.loads(src.read_text(encoding='utf-8'))
            lrc, cover_src = d.get('lrc'), d.get('cover_path')
            info = d.get('info', {})
            rec['source'] = 'A-side vetted cache (read-only copy)'
            rec['lyrics_source'] = info.get('lyrics_source')
            if cover_src and pathlib.Path(cover_src).exists():
                dst = DEST / f'_cover_{sid}.img'
                shutil.copyfile(cover_src, dst)
                rec['cover'] = str(dst)
        else:
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
        print(f"[ok] {title:<12} orig={artist} lines={rec['lines']:<3} "
              f"first={rec['first_line']} cover={'yes' if rec.get('cover') else 'NO'}")
    (DEST / 'fetch_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[done] report -> {DEST / "fetch_report.json"}')


if __name__ == '__main__':
    main()
