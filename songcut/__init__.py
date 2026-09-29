# -*- coding: utf-8 -*-
"""songcut —— 歌切管线核心模块包。

各子模块均以 ``__file__`` 自定位（``sys.path.insert(0, dirname(__file__))``），
因此整个包可以整体搬迁而不影响模块间导入。对外入口见仓库根的 ``song_cutter.py``。

模块一览
--------
config            集中配置（本文件同目录，见 config.py）
lyrics_fetch      歌词与封面自动匹配（LRCLIB → 网易云）
lyric_align       歌词轴对齐（ASR 配对 + 聚类常数偏移）
ctc_align         CTC 强制对齐（词级时间戳 + 乐句融合）
wave_refine       波形边界精修（伴奏起点定位 / 尾部静音判切）
decor_pick        标题装饰方案自动匹配
timeline_sync     切入点检测 + 歌词时间轴精确同步（失败自动降级）
onset_v2          切入点检测 v2（说话段 → 静音谷 → 能量回升）
lyrics_sync       DTW 精同步（需原曲参考音频）
verify_sync       同步质量验收
vocal_activity    人声活动分析（VAD 兜底）
transcript_hub    转写源编排（盘点 / 定位 / 健康检查 / 降级）
"""

__all__ = [
    "config",
    "lyrics_fetch", "lyric_align", "ctc_align", "wave_refine", "decor_pick",
    "timeline_sync", "onset_v2", "lyrics_sync", "verify_sync",
    "vocal_activity", "transcript_hub",
]
