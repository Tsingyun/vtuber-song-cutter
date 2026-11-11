# -*- coding: utf-8 -*-
"""Generate the 2026-11-10 session + three per-song configs for official_pv.

All numbers come from real measurement:
  * abs_start  = median of (SRT lyric onset - official LRC onset) anchors
  * abs_end    = first sustained digital-silence valley after the last lyric
  * head shift = drop the dead-air / whisper-quiet head, shifting the WHOLE
                 timeline (LRC, acts, live, duration) by the same amount
Writes ONLY official_pv/songs/.  Usage: python tools/gen_songs_1110.py
"""
import json, pathlib, re, statistics

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / 'songs'
VIDEO = 'D:/录播/25788785-岁己SUI/2026-11-10/录制-25788785-20261110-201630-486-陪你这个猪过周二！.mp4'
DATE = '2026-11-10'

# --- measured facts (see songs/_lrc_cache/cut_points.json + tail/head dB curves) ---
CUTS = {
    'natianxiayule': dict(
        title='那天下雨了', title_sub='NA TIAN XIA YU LE', artist='周杰伦',
        credit='词曲 周杰伦 / 编曲 林迈可 · 2026《太阳之子》',
        file='【岁己SUI】那天下雨了【20261110歌切】',
        anchors=5787.80, abs_start=5791.20, abs_end=6011.00,
        duration=219.80, lrc_offset=3.40, official=223.0,
        evidence=('7 ASR anchors vs official LRC (spread 2.26 s) -> 5787.80; '
                  '0.5-3.4 s digital dead air (-49..-60 dB) -> start advanced 3.40 s and '
                  'LRC/acts/live/duration shifted -3.40 s together; '
                  'out 6011.00 = -114 dB digital silence before the 6012 s speech. '
                  'Live length 219.80 s vs official 223 s.'),
        bpm=88,
        palette=dict(void='#07121A', ink='#0D1E29', mid='#1E3B48', glow='#EAF4F6',
                     accent='#8FC7D8', warm='#E8A34C', gold='#D9B26A', cold='#6FA3BE'),
        grad=[[0, '#050E15'], [0.42, '#132C39'], [0.78, '#2B5060'], [1, '#07121A']],
        glyphs=['雨', '车', '伞', '书', '虹', '晴', '站', '呆'],
        tunnelWords=['车子缓缓的开', '你慢慢走来', '雪白的天空', '等待彩虹出现',
                     '那一个下雨天', '我绝不再转身离开'],
        typeLines=['那天下雨了', '等待彩虹出现'],
        motifs=['雨', '车窗', '伞', '彩虹', '毕业册'],
        thesis='雨天的车站 · 一句没说出口的 hi，被十年后的彩虹补上。全片从阴青走到暖金，最后停在那把没撑开的伞。',
        theme='rain',
    ),
    'yiyangdeyueguang': dict(
        title='一样的月光', title_sub='THE SAME MOONLIGHT', artist='徐佳莹',
        credit='词曲 徐佳莹 · 2009《LaLa 首张创作专辑》',
        file='【岁己SUI】一样的月光【20261110歌切】',
        anchors=6291.77, abs_start=6296.77, abs_end=6525.60,
        duration=228.83, lrc_offset=5.00, official=240.386,
        evidence=('3 ASR anchors vs official LRC (spread 0.35 s) -> 6291.77. '
                  'SRT 首句「倔强的表情／怎么看得我越来越心慌」判定为徐佳莹 2009 原创版，'
                  '不是苏芮《搭错车》版；成片标原唱 徐佳莹。'
                  '0.5 s 处 -67 dB 谷 + 15 s 轻钢琴前奏导致开头 RMS 0.0070 < 0.008 门槛 '
                  '-> start advanced 5.00 s, whole timeline shifted -5.00 s. '
                  'out 6525.60 = -62 dB fade-out before the 6526 s speech; '
                  'live length 228.83 s vs official 240.386 s (尾奏提前收 6.6 s).'),
        bpm=76,
        palette=dict(void='#050A16', ink='#0A1226', mid='#1B2740', glow='#EEF2FF',
                     accent='#9DB4E8', warm='#B9825C', gold='#CFC0A0', cold='#7E9BD4'),
        grad=[[0, '#040814'], [0.45, '#101B33'], [0.8, '#273A5C'], [1, '#050A16']],
        glyphs=['月', '窗', '慌', '影', '寂', '步', '暗', '光'],
        tunnelWords=['倔强的表情', '一样的月光', '越来越心慌', '孤单的方向',
                     '照不亮未来的形状', '占据我的窗'],
        typeLines=['一样的月光', '怎么看得我越来越心慌'],
        motifs=['月', '窗框', '影子', '脚步', '月晕'],
        thesis='同一片月光 · 人没变，方向变了。冷银的窗一格格亮起来，最后只剩脚步声往远处走。',
        theme='moon',
    ),
    'xiageluokoujian': dict(
        title='下个，路口，见', title_sub='SEE YOU AT THE NEXT CROSSING', artist='李宇春',
        credit='词曲 李宇春 / 编曲 谭伊哲 · 2009 同名专辑《李宇春》',
        file='【岁己SUI】下个，路口，见【20261110歌切】',
        anchors=6698.61, abs_start=6698.61, abs_end=6904.00,
        duration=205.39, lrc_offset=0.0, official=210.373,
        evidence=('4 ASR anchors vs official LRC (spread 0.29 s) -> 6698.61; '
                  'opening RMS 0.0968 (healthy, no head shift). '
                  'out 6904.00 = -180 dB absolute digital silence lasting 27.6 s '
                  'until the 6931.5 s speech; live length 205.39 s vs official 210.373 s '
                  '(尾奏提前收 5.0 s).'),
        bpm=120,
        palette=dict(void='#0A0713', ink='#150E22', mid='#2A1B3D', glow='#FFF0F6',
                     accent='#F06FA8', warm='#FFB36B', gold='#E8C48A', cold='#63C9DE'),
        grad=[[0, '#08050F'], [0.42, '#1B102C'], [0.8, '#43204F'], [1, '#0A0713']],
        glyphs=['路', '钟', '差', '铁', '雨', '茶', '遇', '见'],
        tunnelWords=['刚下的地铁还不算拥挤', '东京下雨淋湿巴黎', '秒针转动 DI DI DA',
                     '小小时差 DI DI DA', '平行的画', '下个路口再见吧'],
        typeLines=['下个，路口，见', '下个路口再见吧'],
        motifs=['地铁', '路口', '时钟', '霓虹', '时差'],
        thesis='两个城市一个时差 · 平行的画总在滴答声里交叉。洋红与青色交错，最后定格在没亮的人行灯。',
        theme='city',
    ),
}

# act templates: 10 segments, three DISTINCT visual grammars (QA round 1: no repeated template)
POOLS = {
    'rain': [
        ['typeCard', 'converge', 'solid'],
        ['illust', 'jz', 'solid', 'road'],
        ['tunnel', 'illust', 'converge'],
        ['kanjiSlam', 'illust', 'converge', 'solid'],
        ['illust', 'road', 'jz', 'afterimage'],
        ['tunnel', 'converge', 'ember'],
        ['kanjiSlam', 'converge', 'illust', 'solid'],
        ['afterimage', 'ref', 'illust', 'tunnel'],
        ['kanjiSlam', 'illust', 'converge', 'road'],
        ['afterimage', 'typeCard', 'solid', 'illust'],
    ],
    'moon': [
        ['typeCard', 'solid', 'converge'],
        ['illust', 'solid', 'jz', 'illust'],
        ['tunnel', 'converge', 'afterimage'],
        ['kanjiSlam', 'illust', 'solid', 'converge'],
        ['illust', 'ref', 'jz', 'solid'],
        ['tunnel', 'illust', 'ember'],
        ['kanjiSlam', 'converge', 'illust', 'solid'],
        ['afterimage', 'illust', 'tunnel', 'ref'],
        ['kanjiSlam', 'illust', 'converge', 'solid'],
        ['afterimage', 'solid', 'typeCard', 'illust'],
    ],
    'city': [
        ['typeCard', 'converge', 'solid'],
        ['illust', 'road', 'jz', 'solid'],
        ['tunnel', 'converge', 'illust'],
        ['kanjiSlam', 'solid', 'illust', 'converge'],
        ['illust', 'road', 'jz', 'afterimage'],
        ['tunnel', 'converge', 'ember'],
        ['kanjiSlam', 'illust', 'solid', 'road'],
        ['afterimage', 'ref', 'illust', 'tunnel'],
        ['kanjiSlam', 'road', 'converge', 'illust'],
        ['afterimage', 'typeCard', 'solid', 'illust'],
    ],
}
FX = {
    'rain': [['dust', 'glow', 'custom'], ['rain', 'dust', 'glow', 'custom'],
             ['rays', 'dust', 'custom'], ['rain', 'feathers', 'glow', 'custom'],
             ['rain', 'cracks', 'dust', 'custom'], ['embers', 'rays', 'custom'],
             ['embers', 'feathers', 'glow', 'custom'], ['dust', 'grad', 'cracks', 'custom'],
             ['fireflies', 'glow', 'rays', 'custom'], ['feathers', 'dust', 'custom']],
    'moon': [['dust', 'glow', 'custom'], ['dust', 'glow', 'grad', 'custom'],
             ['rays', 'dust', 'custom'], ['glow', 'feathers', 'dust', 'custom'],
             ['rain', 'dust', 'cracks', 'custom'], ['rays', 'embers', 'custom'],
             ['glow', 'feathers', 'embers', 'custom'], ['dust', 'grad', 'custom'],
             ['fireflies', 'glow', 'rays', 'custom'], ['dust', 'feathers', 'custom']],
    'city': [['dust', 'glow', 'custom'], ['rain', 'dust', 'glow', 'custom'],
             ['rays', 'embers', 'custom'], ['glow', 'rays', 'glow', 'custom'],
             ['rain', 'cracks', 'dust', 'custom'], ['embers', 'rays', 'custom'],
             ['glow', 'embers', 'feathers', 'custom'], ['dust', 'grad', 'custom'],
             ['fireflies', 'rays', 'glow', 'custom'], ['feathers', 'dust', 'custom']],
}
TRANS = {
    'rain': [['xfade', 'black'], ['xfade', 'iris', 'push'], ['slices', 'zoom'],
             ['flash', 'iris', 'shatter'], ['ink', 'xfade', 'wipe'], ['zoom', 'slices'],
             ['flash', 'shatter', 'iris'], ['black', 'xfade', 'wipe'],
             ['flash', 'irisClose', 'shatter'], ['flash', 'xfade']],
    'moon': [['xfade', 'black'], ['xfade', 'iris', 'wipe'], ['zoom', 'slices'],
             ['flash', 'iris', 'black'], ['ink', 'xfade', 'push'], ['slices', 'zoom'],
             ['flash', 'iris', 'shatter'], ['black', 'xfade', 'wipe'],
             ['flash', 'irisClose', 'zoom'], ['xfade', 'black']],
    'city': [['xfade', 'black'], ['wipe', 'push', 'xfade'], ['slices', 'zoom'],
             ['flash', 'shatter', 'iris'], ['wipe', 'xfade', 'ink'], ['zoom', 'slices'],
             ['flash', 'iris', 'shatter'], ['black', 'wipe', 'xfade'],
             ['flash', 'zoom', 'shatter'], ['flash', 'xfade']],
}
LOOKS = [
    ('intro', 0.18, 'night'), ('verse', 0.34, 'night'), ('pre', 0.48, 'mono'),
    ('chorus', 0.72, 'P4'), ('verse', 0.40, 'night'), ('pre', 0.55, 'mono'),
    ('chorus', 0.82, 'dawn'), ('bridge', 0.30, 'mono'),
    ('climax', 1.00, 'dawn'), ('outro', 0.22, 'day'),
]
IDS = ['intro', 'verse1', 'pre1', 'chorus1', 'verse2', 'pre2',
       'chorus2', 'bridge', 'climax', 'outro']
PROPS = [0, 0.035, 0.16, 0.22, 0.36, 0.52, 0.58, 0.72, 0.82, 0.92, 1.0]


def lrc_times(sid, off):
    out = []
    for line in (S / f'{sid}.lrc').read_text(encoding='utf-8').splitlines():
        m = re.match(r'^\[(\d+):(\d+(?:\.\d+)?)\]', line)
        if m:
            t = int(m.group(1)) * 60 + float(m.group(2)) - off
            if t >= 0:
                out.append(t)
    return sorted(out)


def acts_for(sid, d, off):
    times = lrc_times(sid, off)
    raw = [p * d for p in PROPS]
    bounds = [0.0]
    for i in range(1, len(raw) - 1):
        # snap each interior boundary to the nearest real lyric onset within 6 s
        near = [t for t in times if abs(t - raw[i]) < 6]
        b = min(near, key=lambda t: abs(t - raw[i])) if near else raw[i]
        b = round(max(bounds[-1] + 2.0, min(b, d - 2.0)), 2)
        bounds.append(b)
    bounds.append(round(d, 2))
    theme = CUTS[sid]['theme']
    out = []
    for i, (look, inten, lut) in enumerate(LOOKS):
        out.append(dict(id=IDS[i], t0=bounds[i], t1=bounds[i + 1], look=look,
                        intensity=inten, lut=lut,
                        pool=POOLS[theme][i], fx=FX[theme][i], trans=TRANS[theme][i]))
    return out


def live_windows(d):
    return [[round(p * d, 2), round(p * d + 3.4, 2)]
            for p in (0.045, 0.25, 0.40, 0.63, 0.80, 0.92)]


def signature_for(acts, d):
    return [
        {'t': 0.0, 'note': 'title card on the first bar (T21 quiet card)'},
        {'t': acts[3]['t0'], 'note': 'first chorus: per-beat kanji slam'},
        {'t': acts[6]['t0'], 'note': 'chorus 2 returns (ember + shatter)'},
        {'t': acts[8]['t0'], 'note': 'last chorus / climax'},
        {'t': round(d - 11.3, 2), 'note': 'end on a retinal afterimage of the title'},
    ]


def main():
    session = {
        'date': DATE,
        'source_video': VIDEO,
        'source_spec': '1920x1080 / 60fps / HEVC + AAC / 22416.950 s',
        'cut_method': ('ASR onset anchors from the 11-10 SRT (D:/岁己直播自动总结/SRT/) '
                       'cross-checked against official LRC; median offset per song. '
                       'Out-point from a -68 dB / -180 dB digital-silence valley measured '
                       'on the real PCM. Dead-air / whisper-quiet heads dropped by shifting '
                       'abs_start, LRC, acts, live windows and duration together.'),
        'whitelist': list(CUTS),
        'songs': {},
    }
    for sid, c in CUTS.items():
        acts = acts_for(sid, c['duration'], c['lrc_offset'])
        cfg = {
            'id': sid, 'title': c['title'], 'titleSub': c['title_sub'],
            'artist': c['artist'], 'credit': c['credit'], 'date': DATE,
            'absStart': c['abs_start'], 'absEnd': c['abs_end'],
            'duration': c['duration'], 'bpm': c['bpm'],
            'lrc': 'analysis/lyrics_mv.lrc',
            'thesis': c['thesis'], 'palette': c['palette'], 'grad': c['grad'],
            'glyphs': c['glyphs'], 'tunnelWords': c['tunnelWords'],
            'typeLines': c['typeLines'], 'motifs': c['motifs'],
            'live': live_windows(c['duration']),
            'lookDefault': {'boil': 1.0, 'bloom': 0.3, 'bloomThreshold': 0.8,
                            'grain': 0.055, 'vignette': 0.34, 'lutMix': 0.0},
            'acts': acts, 'signature': signature_for(acts, c['duration']),
            'lrcOffset': c['lrc_offset'],
            'theme': c['theme'],
            'timeline_note': c['evidence'],
            'original_artist_verified': True,
        }
        (S / f'{sid}.json').write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        session['songs'][sid] = {
            'file': c['file'], 'abs_start': c['abs_start'], 'abs_end': c['abs_end'],
            'duration': c['duration'], 'evidence': c['evidence'],
        }
        print(f"[ok] {c['title']:<12} {c['abs_start']} -> {c['abs_end']} "
              f"({c['duration']}s) acts={len(acts)} live={len(cfg['live'])} "
              f"lrcOffset={c['lrc_offset']}")
    (S / f'{DATE}.json').write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding='utf-8')
    (S / '_lrc_cache' / 'cut_points_final.json').write_text(
        json.dumps({k: {'abs_start': v['abs_start'], 'abs_end': v['abs_end'],
                        'duration': v['duration'], 'lrc_offset': v['lrc_offset']}
                    for k, v in CUTS.items()}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[done] session -> {S / (DATE + ".json")}')


if __name__ == '__main__':
    main()
