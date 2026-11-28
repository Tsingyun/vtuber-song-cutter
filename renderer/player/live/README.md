# Live Cue · 歌切实时播放器

一个**独立**的新播放器页面：用真实录播画面做主播视角（自动裁切跟随），右侧动态歌词，
底部发丝级进度轨。它**不参与出片**，也**不改动** `index.html`（出片用）与 `index_pv.html`（手书 PV）。

```
录播 MP4 ──ffmpeg 切片──► _media_cache/live/<date>/<i>.mp4 ──► <video>  真实画面
manifest.json ──轴──►      <date>/<i>.lrc（cut 相对时间）   ──►   逐字填充
tools/track_host.py ──►    <date>/<i>.track.json（人物中心） ──►   GPU 纹理平移
```

## 快速开始

```bash
# 一条命令：备料（幂等）+ 起服务 + 开浏览器
python tools/liveplayer.py play --date 2026-11-27 --index 0

# 或分开
python tools/liveplayer.py prepare --date 2026-11-27 --index 0 [--force]
python tools/liveplayer.py serve --port 8777
# 浏览器打开 http://127.0.0.1:8777/
```

调试参数：`?i=<列表位置>`（不是当天的第几首——不同场次会撞号）、`?t=<秒>`（起播位置）、`?play=1`、`?muted=1`。

键盘：`空格` 播放/暂停，`←/→` 快退/快进 5 秒，进度条可拖拽。

## 字体体系

两族四角色，全站统一（详见 RULES §160）：

| 角色 | 字族 | 用在哪 |
|---|---|---|
| 衬线 `LC Serif` | Noto Serif JP（日语歌）/ Noto Serif SC（中文歌） | 歌词原文、歌名 |
| 黑体 `LC Sans` | Noto Sans SC | 中文译文、界面标签、按钮、时间数字 |

拉丁字母不另配字体，直接吃同族自带拉丁——同族内基线/字宽一致，中英日混排才不会跳。
`prepare` 按「本曲歌词 + 译文 + 界面固定文案」子集化成两个 woff2（约 80~175KB），
页面用 `FontFace` 加载完再量字位。字体请求失败只降级到系统 Noto/宋体，不报错、不换版式。

字号阶梯：歌名 3.06cqw / 译文 = 原文 0.50em / 标签 .60cqw / 时间 1.30cqw。
歌词主字号由 `fitLyrics()` 按最长行自适应。

## 双语歌词

日语、英语歌自动带中文译文，中文歌不重复显示。

- 译文来源：网易云 `tlyric`（真实投稿译文），**不生成、不翻译、不改写**。
- 版本判定：用**原文文本相似度 ≥0.75** 排除同名不同曲（实测同名版本相似度只有 0.02）。
- 对齐：先按行序做序列对齐，再挂译文；残留行用 ±1s 时间窗一对一补齐；对不上的行留空。
- 视觉：原文在上、译文在下、同一 `.line` 容器，缩放/淡入淡出/位移动画天然同步；
  译文 0.50em、字重 300、对比度更低，活动行时一起提亮。
- 降级：译文缺失、404、请求被拦，都只影响译文，原文与播放不受任何影响。

英文歌词单词长，走 `wrap` 模式允许折行（活动行放大降到 1.06），并用整行羽化推进代替逐字填充。

多曲：`index.json` 里 ≥2 条时，右上角出现单行步进器 `またあした 01/04 ‹ ›`，
`‹ ›` 循环切换（同时改写 `?i=`、书脊序号与底部曲目信息）。只有一首时整个元素高度为 0，不占版面。
之所以是单行而不是列表：多行列表下探到 11.4cqh 会压住乐谱头的行号（`017 / 041`）和右侧竖排书脊。

## 为什么必须起服务，不能直接双击打开

`file://` 下浏览器不允许 `Range` 请求，14GB 的录播没法 seek；而且画布取色会被跨源污染。
`tools/liveplayer.py serve` 是一个带 `Range`（206）的本地静态服务，只暴露
`renderer/player/live/` 与 `_media_cache/live/` 两个目录。

## 素材契约

`prepare` 产出（全部在 `_media_cache/live/<date>/`，已 gitignore）：

| 文件 | 内容 |
|---|---|
| `<i>.mp4` | 该曲切点区间的 1080p60 片段（faststart，10 Mbps，AAC 192k） |
| `<i>.lrc` | **cut 相对时间**的轴（优先 `lrc_src_path`；缺失则用成片轴减 `head_pad`） |
| `<i>.track.json` | 人物中心轨迹：`fps / cx[] / cy[] / conf[]`（源视频像素坐标） |
| `<i>.woff2` | 按本曲文本子集化的明朝体（Noto Serif JP VF，~128KB） |
| `<i>.json` | 元数据（标题/原唱/演唱者/时长/切点/子集字体路径） |

`index.json` 汇总所有已备好的曲目，页面通过 `/api/index` 读取。

## 取景与跟踪

- 分析：`tools/track_host.py` 把片段按 4fps 解到 480p，取**HSV 亮块最大连通域**
  （白发的强对比目标）的上 45% 行做头部质心，再用首帧头部做**多尺度模板匹配**，
  限制在上次位置附近搜索（±14% 画面宽）——两源互相校验，避免被弹幕/礼物墙带跑。
- 后处理：5 点中值 + 一阶低通，输出 4Hz 轨迹。
- 播放时：按 `video.currentTime` 线性插值轨迹，再做一次时间常数 0.2s 的指数平滑，
  位移 < 0.5px 直接吸附（消抖）；`translate3d` 只改合成层变换，**不重绘视频帧**。
- 取景：高度固定 `cropH=980`（源 1080 行里取 91%，头顶永不被切），宽度由面板宽高比推出；
  中心横向跟随人物并右偏 `biasX=56px`，把左侧直播弹幕挤出画面。

> 人物横向活动范围实测 882~1014px（中位 974，σ=13.7），所以跟随即便只走 100 多像素也够用；
> 极端前倾时画面会自然变近（Live2D 本身缩放），不做反向补偿。

## 时间轴口径

播放器**不重算任何时间**，直接用出片时已经核定的 `.src.lrc`（cut 相对时间），
片段从 `cut_start` 精确重编码切出。已用互相关验证片段与源的对齐：
最佳偏移 21.4 ms（= AAC 编码器延迟 1024 样本），相关系数 0.993。

逐字填充：行起点 → 下一行起点（长间奏时按字数估上限），按字符宽度与权重（假名/汉字 1、
拉丁 0.58、空格 0.30）分配；用 `Range.getBoundingClientRect()` 量出每个字的右边界，
换算成列宽百分比后用 `clip-path: inset()` 裁出已唱部分。

## GPU 路径（实测，不是"用了 CSS 动画"就算）

Chrome 现场核查（`SystemInfo.getInfo` + `LayerTree.compositingReasons`）：

```
gpu_compositing: enabled
gl: ANGLE (NVIDIA RTX 5070 Ti, D3D11)
layer reasons: Video ×1 · WillChangeTransform ×5 · ActiveTransformAnimation ×4 · ActiveOpacity ×2
视频层 Video = 1   ← 视频由合成器以纹理方式持有，transform 不触发逐帧像素搬运
```

- 视频：`<video>` 独立合成层，`translate3d` 由合成器作用于纹理；`requestVideoFrameCallback`
  级别的工作一次都不做，只有 6Hz 的 96×54 取色（约 20KB/次）。
- 歌词：整列一个合成层做 `translate3d`；逐字填充改的是单个元素的 `clip-path`。
- 颗粒/环境光：`transform` 与 `opacity` 动画，均为合成属性；颗粒原来是 `background-position`
  动画（每 267ms 整屏重绘），已改为合成层位移。

## 已知限制

- 源是 1080p，舞台宽 2560 以上时人物会被放大（>1.4×）略软。
- 人物极端左倾时（全曲仅 322s 附近几秒）左缘可能掠过直播弹幕，已用 9.5% 左缘渐隐压制。
- 只有 manifest 里**已出片**（有 `cut_start/cut_end`）的歌才能备料。
- 子集字体只覆盖本曲歌词 + 界面固定文案；改歌词后需要重新 `prepare --force`。

## 回退

新播放器完全独立：删掉 `renderer/player/live/`、`tools/liveplayer.py`、`tools/track_host.py`
与 `_media_cache/live/` 即回到原状，`song_cutter.py` / `renderer/gpu_render.py` /
`renderer/player/index.html` 一行未动。
