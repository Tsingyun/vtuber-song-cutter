# -*- coding: utf-8 -*-
"""11/21 补录第二首《如果有来生》。幂等：已存在则只更新切点。"""
import io, json, os, shutil

P = "segments/20261121-20261121-陪你这个猪.json"
shutil.copy(P, P + ".bak")

d = json.load(io.open(P, encoding="utf-8"))
songs = d["songs"]

TITLE = "如果有来生"
entry = {
    "seq": 2,
    "title_guess": TITLE,
    "start": 4890.0,
    "end": 5125.0,
    "lang": "zh",
    "confidence": 1.0,
    "evidence": (
        "用户指出本场还唱了《如果有来生》。回查 SRT 命中：01:21:05.74 报歌名「如果有来生」；"
        "01:21:36.21 起「以前人们在四月开始收获／躺在高高的谷堆上面笑着／（我）穿过金黄的麦田／"
        "去给稻草人唱歌／等着（落）山风吹过」；01:21:59「你从一座叫我的小镇经过」；"
        "01:22:06「刚好屋顶的雪化成雨飘落」；01:22:15「（你穿着透明的衣服）给我一个人唱歌／"
        "全都是我喜欢的歌」；01:22:23「我们去大草原的湖边」；01:22:26「等候鸟飞回来」；"
        "01:22:29「等我们都长大了，就生一个娃娃，他会自己长大」；01:22:39「我们也各自远（去）」；"
        "01:22:41「我给你写信」。01:23:12~01:23:42 整块重唱 A 段（含「你穿着透明的衣服」），"
        "01:24:00 再起副歌「（去）大草原的湖边」／01:24:03「候鸟飞回来」／01:24:05「等我们都长大了」／"
        "01:24:14「我们也各自远（去）」／01:24:21「（我）会灰心（给你写信）／说好了吧」，"
        "01:24:59「啦啦啦啦」收尾，01:25:06 最后一次「等候（鸟飞回来）」。"
        "01:21:36~01:25:19 连续演唱，与谭维维《如果有来生》（2010-03-30《谭某某》，"
        "词 高晓松／曲 格非，4:23）逐句一致（联网核实）。"
    ),
    "start_hms": "01:21:30",
    "end_hms": "01:25:25",
    "fetch_title": TITLE,
    "artist": "谭维维",
    "title_source": "user+srt+lyrics",
    "note": "Cover 岁己SUI · 原唱 谭维维",
    "note_technical": (
        "识别阶段漏检：本场 Gemini 429 降级到 GLM-4-flash，6h11m / 4269 句 / 10.2 万字转写一次灌入，"
        "备用通道召回衰减，只捞出《当你》与一条误报「笑嘻了」，中段 01:21 这首被跳过。"
        "手工补录。切点人工核定：入点 4896.20（4895.4~4896.2 由 −47dB 渐落到 −59.8dB 谷底，"
        "4896.3 抬起、4896.7 起稳定 −19dB，与 01:21:36.21 首句对齐；此曲首句即开头，无长前奏）；"
        "出点 5119.00（尾奏自 5115.5 由 −15dB 开始衰减，5119 −39dB、5120 −42dB，"
        "5120.8 后落到 −200dB 数字静音＝录流断点，不得计入片尾；5126.6 起 −14dB 那段是致谢说话不是唱）。"
    ),
    "artist_hint": "谭维维",
    "cut_start_abs": 4896.20,
    "cut_end_abs": 5119.00,
    "orig_duration_s": 263.0,
}

if any(s.get("title_guess") == TITLE for s in songs):
    for i, s in enumerate(songs):
        if s.get("title_guess") == TITLE:
            songs[i] = entry
else:
    songs.append(entry)

songs.sort(key=lambda x: x["start"])
for i, s in enumerate(songs):
    s["seq"] = i + 1

io.open(P, "w", encoding="utf-8", newline="").write(json.dumps(d, ensure_ascii=False, indent=2))
print("OK 现有 %d 首：" % len(songs))
for s in songs:
    print("  ", s["seq"], s["title_guess"], "| 窗口 %.1f~%.1f | 切点 %.2f~%.2f | 原唱 %s | 原曲 %.0fs" % (
        s["start"], s["end"], s["cut_start_abs"], s["cut_end_abs"], s["artist"], s["orig_duration_s"]))
