# 歌切链 ⇄ 文字 PV 链：接入层设计与改动记录（2026-11-03）

> 一句话：两条链本来就住同一个仓库，但此前只共享**目录**、不共享**事实**。
> 本轮把「歌切侧定稿的东西」变成 PV 侧唯一可读的接口（`songcut/pvlink.py`），
> 并消灭三处「同一个名字写两份」的隐患（ffmpeg 路径、人物画框、录播源判定）。
>
> 本轮**没有**新增任何渲染路径：画面仍由 `renderer/player/index_pv.html` + `render_song.cjs` 出，
> 切点仍由歌切侧定。改动全部是「读谁的数据」而不是「怎么画」。

---

## 一、审计：仓库里其实有四层东西

| 层 | 目录 / 文件 | 职责 | 谁是权威 |
|---|---|---|---|
| ① 歌切判定 | `auto_cut.py`、`song_cutter.py`、`songcut/*.py` | 找歌 → 定切点 → 歌词/封面 → 对齐 → 出片 → QC | **权威**。切点、原唱、歌词版本、剔行、原曲时长都只在这里定 |
| ② 歌切产物 | `segments/<日期>-<场次>.json`、`_media_cache/`、`cuts/<日期>/` | 逐场定稿（16 场）、歌词与封面缓存（2274 个文件）、成片与素材 | **权威数据面**。人改过的结论都落在这里 |
| ③ 文字 PV | `tools/pv_direct.py`（导演层）、`renderer/player/pv/*`、`pv_timing.py`、`analyze_music.py` | 逐词轴、镜头编排、Canvas2D 版式、JIZURA/人物层挂点 | 视觉权威 |
| ④ 素材抽取 | `tools/prepare_live.py`、`pv_live.py`、`pv_jz.cjs` | 从录播裁人物序列、渲 JIZURA 帧序列 | 素材权威（按 `live.json` / `jz.json` 契约） |

**问题在于 ③④ 从来没读过 ②。** 具体表现（本轮逐条实测）：

| 现象 | 以前的做法 | 后果 |
|---|---|---|
| 切点 | `pv_one.py` 要人把 `--start/--end` 从 segments 里手抄 | 抄错一次，成片安静地错一整首 |
| 原唱 | `--artist` 手填 | 漏填则画面无 ORIGINAL，或写成翻唱者（违反铁律） |
| 歌词 | `fetch_lrc()` 自己联网再抓一份 | 丢掉 `lrc_drop_lines`（人工剔掉的专辑介绍/制作名单）→ 制作名单上台唱 |
| 录播源 | `pv_one` glob 取最大文件；`pv_live` 取 `sorted(os.listdir())[0]` | 会选中压缩中断的 `.part` 残件（moov 缺失），整段抽帧全空或直接崩 |
| ffmpeg | 17 个 `tools/*` 里各写一份 `D:/ffmpeg/bin/ffmpeg.exe` | 换机/换盘全局替换；`pv_livebed.py` 甚至写死了某一场的录播绝对路径（含本机目录） |
| 人物画框 | `prepare_live.py` 与 `pv_live.py` 各有一套（头窗 640 vs 720，偏移 645/52 vs 620/0） | 单独跑与经 `pv_live` 跑，取景不一致，且没人知道该信哪套 |

---

## 二、接入层：`songcut/pvlink.py`

只读、无网络、无渲染。四个概念：

| 概念 | 函数 | 说明 |
|---|---|---|
| 目录 | `dirs()` | workdir / segments / cuts / media_cache / render_tmp / srt / recording_root，全部按 `config.json` 定位 |
| 运行时 | `runtime()` | ffmpeg / ffprobe / node / node_path：环境变量 → config → 内置探测，**全仓一份** |
| 歌 | `find_song()` / `bundle()` | 歌名四级匹配（序号 → 精确 → 包含 → 相似度），返回素材包 |
| 自检 | `doctor()` | 「源在不在 / 歌词缓存有没有 / 人物序列抽没抽」一次摊开 |

### 2.1 素材包（`bundle`）—— 稳定契约

```json
{
  "date": "2026-11-01", "seq": 1, "title": "小丑的品格",
  "fetch_title": "小丑的品格", "artist": "泠鸢yousa",
  "match": "exact", "segments": "D:/岁己歌切/segments/20261101-....json",
  "window": {"start": 8544.0, "end": 8813.15, "dur": 269.15,
             "source": "cut_abs", "warn": ""},
  "source": {"path": "...mp4", "exists": true, "size_mb": 8172.2},
  "cover":  {"path": "_media_cache/_cover_小丑的品格.img", "exists": true},
  "lrc":    {"text": "...", "source": "cache", "lines": 50,
             "dropped": ["..."], "drop_missing": []},
  "orig_duration_s": 272.3,
  "assets":  {"live": null, "jz": null},
  "style_ok": {"live": false, "jz": false},
  "notes":  {"evidence": "...", "title_source": "kdocs"},
  "missing": [], "ready": true
}
```

设计取舍（每条都对应一次真实故障）：

* **`lrc.source` 必须显式**（cache / online / none）。以前歌词来源只有一个字符串，出问题时只能靠猜。
* **`window.source` 必须显式**（cut_abs / rough）。`cut_start_abs` 与 `cut_end_abs` 只写一边时**不猜**：
  有 `start/end` 就退回粗窗口并给 `warn`，连粗窗口都没有就报错。
* **歌词按 `fetch_title`（演唱版本）查缓存**，不按曲库裸名。这是「原唱标注 vs 演唱版本」铁律在代码上的落点：
  画面标原唱，歌词/对齐按实际演唱版本。测试里专门有一条反向断言锁住它。
* **`lrc_drop_lines` 按非空行序号剔**，没命中的行号进 `drop_missing` 并**让 `ready` 变 false** ——
  行号对不上说明歌词版本换了，这时静默继续就等于拿制作名单当歌词。

### 2.2 命令行（`tools/pvlink.py`）

```bash
python tools/pvlink.py sessions                     # 有 segments 的 16 场
python tools/pvlink.py list   --date 2026-11-01     # 当天切了哪几首（含切点来源标记）
python tools/pvlink.py show   --date 2026-11-01 --song 小丑的品格
python tools/pvlink.py bundle --date 2026-11-01 --song 小丑的品格 \
       --json --lrc-out _tmp/x.lrc                  # 给别的工具消费
python tools/pvlink.py doctor --date 2026-11-01     # 只读体检
python tools/pvlink.py pv-one --date 2026-11-01 --song 小丑的品格 --res 1 --fps 30
python tools/pvlink.py geom   --preset pv           # 画框几何唯一真源
```

---

## 三、接线改动（每个文件都能单独回退）

| 文件 | 改了什么 | 回退方式 |
|---|---|---|
| `songcut/pvlink.py` | **新增**接入层（约 500 行，含注释） | 删文件；其余改动都只在 `import` 处引用它 |
| `tools/pvlink.py` | **新增** CLI | 删文件 |
| `tools/pv_one.py` | ① 新增 `--from-segments`（切点/原唱/歌词/源全从 segments 取）、`--seq`、`--online`、`--dry-run`；② `--song/--start/--end` 改为可选（缺了会明确报错）；③ ffmpeg/ffprobe/node 走 `pvlink.runtime()`；④ 源解析走 `pvlink.source()`；⑤ manifest 增 `link` 溯源块 | 不传 `--from-segments` 时，命令行语义与改造前一致（只有源解析更严格：排除 `.part`） |
| `tools/pv_live.py` | ① 画框几何改读 `pvlink.geometry("pv")`；② 录播源改走 `pvlink.source()`（不再 `sorted(os.listdir())[0]`）；③ `wide` 几何补进默认 `G` | 改回模块内常量即可；两套值都在 `GEOMETRY_PRESETS` 里有据可查 |
| `tools/pv_livebed.py` | 去掉写死的录播绝对路径与 `OFFSET`，ffmpeg 走 `pvlink.runtime()` | 同上（但**不建议**回退：那是隐私泄漏） |
| `songcut/__init__.py` | `__all__` 增 `pvlink` | — |
| `tests/test_pvlink.py` | **新增** 60 项断言 | 删文件 |

**没有改动**：`song_cutter.py`、`auto_cut.py`、`pv_direct.py`、`pv_prepare.py`、`render_song.cjs`、
`renderer/player/*`。也就是说，**歌切主链与渲染管线一行未动**。

---

## 四、实测

| 项 | 命令 | 结果 |
|---|---|---|
| 单测 | `python tests/test_pvlink.py` | 60 项全过（Python 3.10.10 / 3.13.12 双环境各跑一次） |
| 场次 | `pvlink sessions` | 16 场 |
| 素材包 | `pvlink show --date 2026-11-01 --song 小丑的品格` | 窗口 8544.00→8813.15（cut_abs，269.1s）、原唱泠鸢yousa、歌词 cache 50 行（剔 5 行）、封面有、missing 空 |
| 歌词导出 | `pvlink bundle ... --lrc-out` | 制作名单/专辑介绍已剔除，50 行带时间戳 |
| 体检 | `pvlink doctor --limit 3` | 11-01 源在、歌词齐；**10-27 / 10-25 的源已被上游压缩删除**（RULES §86 的既定行为），doctor 如实报缺口而不是假装能跑 |
| PV 干运行 | `pv_one --from-segments --dry-run` | 切点/源/歌词来源/匹配方式全部来自 segments，`missing` 为空 |
| PV 实渲回归（新路径） | `pv_one --from-segments --song 小丑的品格 --res 1 --concept ""` | **8074 帧 / 269.15s / 1920×1080@30 / 12.03 Mbps / 394.6 MB**，总耗时 333.7s（其中渲染 303s、分析 17s、时间轴 4s）；`manifest.link` 记下 `seq=1 / match=exact / window_source=cut_abs / lrc_source=cache / lrc_dropped=5`；`songs[0].orig_duration_s = 272.3` 也来自 segments（以前这条恒为 null） |
| 抽帧目视 | 62s（副歌）与 150s（BRIDGE 大字）两帧 | 出图正常、歌词为缓存里那份（换行与剔行都已生效）、无黑场 |

> 回归口径：`segments` 窗口 8544.00→8813.15（269.15s）对成片 269.150s，**零漂移**。
> 1080p 试片必然触发 `songcut.spec` 的码率安全区告警（16000~18500 是 4K 口径），
> 这是既有判据、不是本轮引入；4K 出片时该判据照常生效。
>
> 另记一条踩坑：先用「手工窗口 42s + 整首 LRC」跑过一次，`pv_direct` 报
> `映射 47 句 ≠ 窗口内 4 句`、`≠ 窗口内 50 句` —— **手工窗口必须配已裁到该窗口的 LRC**。
> `--from-segments` 不会踩这个坑，因为窗口与歌词天然同源。

---

## 五、可复用清单（本轮已接 / 待接）

**已接（只读复用）**

| 资源 | 用途 |
|---|---|
| `segments/*.json` | 切点、原唱、封面、原曲时长、剔行、证据 |
| `_media_cache/_lrc_*.json` | 歌词（含 key 规则，借 `lyrics_fetch.cache_key`） |
| `_media_cache/_cover_*.img` | 原唱封面 |
| `songcut/style.py` | 风格 A/B 与素材约定路径（`asset_paths`） |
| `songcut/config.py` | 全部路径与运行时 |
| `song_cutter.find_video` | 录播源判定（排除 `.part`、多场取最长、回退根目录） |

**待接（有价值，但本轮不动）**

| 资源 | 为什么值得接 | 注意 |
|---|---|---|
| `songcut/qc.py --manifest` | PV 出片后可直接进 QC，不必另写判据 | 需要 manifest 的 `songs[]` 格式，`pv_one` 已写 |
| `songcut/lyric_locate.py`、`ctc_align.py` | 逐词轴比 `pv_timing` 的启发式更稳 | 两套轴并存会打架，需先定「谁是唯一轴」 |
| `kdocs_songs.json`（516 天 / 1463 条） | 当日歌单是认歌的最强证据 | 已在歌切侧用；PV 侧只该读结论不该重跑 |
| `styles/*.vsp.json` | 参考视频的调色/字体/镜头统计 | 只有 1 份，样本太少，先不动 |
| `songcut/music_bounds.py` | 段落/节拍边界 | `analyze_music.py` 已能出段落，避免双份 |

**明确不引入**

* `song_cutter.py` 的主流程（切点判定、波形精修、去重）：PV 侧一旦复制，就会出现第二套切点真相。
* `auto_cut.py` 的识别链（LLM 认歌、kdocs 交叉验证）：PV 侧没有「认歌」的职责。
* 任何 `cuts/` 下的中间产物（`_render_tmp`）：那是渲染缓存，不是素材契约。

---

## 六、风险与遗留

1. **仍有 17 个 `tools/*` 硬编码 `D:/ffmpeg/bin/ffmpeg.exe`**（`pv_build`、`pv_check`、`pv_verify`、
   `pv_flash`、`make_pro_clip` 等）。本轮只接了 PV 主路径三处；其余属于一次性取证工具，
   逐个改风险大于收益。判据：`grep -rln "D:/ffmpeg/bin" tools/*.py` 的数字应逐步下降，
   新写的工具**不许**再出现这个字面。
2. **`head_pad` 口径**：`segments.cut_start_abs` 对应「伴奏起点」，而人物抽帧要的是「成片 0 点」。
   两者相差 `PAD_START`（1.5s）这类留白，`pv_live` 仍由人传 `--cut-start`。
   接下去应让 `pv_live --from-segments` 自己算，而不是再抄一个数。
3. **`live.json` / `jz.json` 不在 segments 里**：它们是预渲素材，按 `cuts/<date>/<歌名>/` 约定探测，
   所以 `bundle.assets` 只回答「有没有」，不回答「窗口对不对」。窗口仍以 `pv_live` 的 `LIVE_SPANS` 为准。
4. **画框几何按场次变化**：`GEOMETRY_PRESETS` 里两套都是「1920×1080 + 特定 UI 版面」实测值。
   换主播/换主题必须重测（聊天栏右缘、礼物卡左缘决定安全区），不能假设通用。
5. **本机 Python 环境有三份**：`Python310`（numpy+requests+PIL，管线实际用的）、
   workbuddy 内置 3.13（有 numpy 无 requests）、miniconda 3.10（有 requests 无 numpy）。
   `pvlink` 刻意不依赖任何重库，所以三者都能跑；但 `pv_one` 子进程用 `sys.executable`，
   **必须用 Python310 启动**，否则 `analyze_music.py` 会 `ModuleNotFoundError`。
6. **`pv_one --concept dawn` 与歌词行数绑定**：`--concept dawn` 走的意象映射表是**按某一首**的
   句数建的（当前那张 47 句）。换成别的歌（如《小丑的品格》50 行）时 `pv_direct` 会直接报
   `映射 47 句 ≠ 窗口内 50 句`，而不是降级。这不是本轮引入的，但接入层让它更容易被撞上
   （因为现在一定拿到**剔行后**的定稿歌词）。换歌请显式 `--concept ""`，或给该歌补一份意象表。
