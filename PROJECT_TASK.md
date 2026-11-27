# PROJECT_TASK — 全新歌切播放器（Live Cue）

> 跨会话唯一进度真源。重新开工前先读这里，再核对代码实际状态。

## 目标

保留现有歌切业务链路（找歌 / 切点 / 歌词 / 时间轴 / GPU 渲染），**新增一个独立的播放器页面**：
真实录播画面做主播视角（自动裁切跟随），右侧动态歌词，底部发丝级进度轨。

## 硬约束（不可妥协）

- 不重构歌切系统：`song_cutter.py` / `renderer/gpu_render.py` / `renderer/player/index.html` 一行未动。
- 新页面独立在 `renderer/player/live/`，不与出片页共享 DOM。
- 画面必须是**真实录制视频流的实时裁切**，不许静态图 / 随机帧 / 成片冒充。
- 歌词沿用现有 `.src.lrc`（cut 相对时间），播放器不重算任何时间。
- 每条能力如实标注：已完成 / 待验证 / 未完成。

---

## 阶段一 · 项目审计（已完成）

| 项 | 事实 |
|---|---|
| 录播源 | `D:/录播/.../录制-25788785-20261127-202300-700-…_merged.mp4`（1920×1080@60，19146.99s，14.5GB） |
| 成片台账 | `cuts/<date>/manifest.json` → `cut_start / cut_end / head_pad / lrc_path / lrc_src_path / artist` |
| 轴口径 | `*.src.lrc` = **cut 相对时间**（播放器用这个）；`*.lrc` = 已 +head_pad（对齐成片） |
| 现有播放器 | `renderer/player/index.html` 170KB 单文件画布渲染，出片专用 → 不改 |
| GPU 渲染 | `renderer/gpu_render.py`（moderngl）+ `render_song.cjs`（Playwright+WebCodecs），与本任务无耦合 |
| 画面构成 | Live2D 立绘居中（x 0.31~0.69，头 y≈0.12~0.40），左侧弹幕 0~570px、右侧礼物墙 1600px+，背景夜雨街景 |
| 检测可行性 | cv2 5.0 **无** CascadeClassifier；`FaceDetectorYN` 需外部 onnx 且对二次元召回差 → 改用亮块+模板 |

## 阶段二/三 · 设计与实现（已完成）

### 三个方向（先想三个，再选一个）

| 方向 | 构图 / 排版 / 材质 | 结论 |
|---|---|---|
| **A. Nocturne 夜行**（选中） | 人物 55% 全高出血、右缘溶解；歌词 36% 明朝体竖列；底栏只有一条发丝进度 | 画面即主体，排版克制，能长期看；且不需要额外材质资源 |
| B. Liquid Glass 玻璃双栏 | 两块玻璃面板叠在取色场上，毛玻璃折射 | **弃**：等于照抄 Apple Music，backdrop-filter 全屏开销大，且容易落进"圆角卡片"套路 |
| C. Poster 瑞士网格 | 竖排标题压在右缘、歌词缩到右下角、网格线外露 | **弃**：歌词阅读区被挤到角落，长曲目看完累；与"歌词要有足够阅读空间"冲突 |

最终语言：近黑底（#060608）+ 暖白（#f2ece6）+ 雨粉点缀（#e8a4b6，取自主播发色）；
明朝体歌词 + 无衬线 micro-label + 等宽数字；只有一条分割竖线、一条进度发丝、一条书脊竖排。

### 文件

| 文件 | 作用 |
|---|---|
| `renderer/player/live/index.html` | 页面骨架（人物 / 歌词 / 底栏 / 进度 / 播放门） |
| `renderer/player/live/live.css` | 全部视觉（容器查询单位 cqw/cqh，16:9 舞台 + 信箱化） |
| `renderer/player/live/live.js` | LRC 解析、逐字填充、裁切跟随、环境取色、播放控制 |
| `renderer/player/live/README.md` | 使用与设计说明、GPU 证据、已知限制、回退 |
| `tools/liveplayer.py` | `prepare`（切片+跟踪+字体子集）/ `serve`（Range 服务）/ `play`（一条命令） |
| `tools/track_host.py` | HSV 亮块 + 多尺度模板匹配的人物跟踪，输出 4Hz 轨迹 |

### 关键实现

- **裁切**：`cropH=980`（源高 91%，顶部对齐，头顶永不被切）；宽度由面板宽高比推出；
  中心横向跟随人物并右偏 56px（把左侧弹幕挤出画面）；只改 `translate3d`，不重绘视频帧。
- **跟踪**：4Hz 轨迹 → 线性插值 → 0.2s 指数平滑 → 0.5px 吸附消抖；极端前倾不反向补偿。
- **歌词**：整列 `translate3d` 位移（820ms）+ 活动行 `scale(1.22)`；逐字 `clip-path: inset()`
  按 `Range` 实测字位推进；长间奏按字数封顶，避免"最后一个字爬半分钟"。
- **字体**：fontTools 按曲目文本子集化 Noto Serif JP VF → woff2 128KB，`FontFace` 等加载完再量字位。
- **环境色**：96×54 画布 6Hz 取画面饱和色，双径向渐变叠在底层，随画面缓慢呼吸。

## 阶段四 · 测试（已完成，实测数据）

| 项 | 结果 |
|---|---|
| 播放 | 41 行歌词、`video.currentTime` 逐帧驱动进度与填充；`?t=` 定位正常 |
| 时间轴对齐 | 片段 vs 源互相关：最佳偏移 **21.4ms**（= AAC 编码器延迟），相关 **0.993** |
| 活动行锚点 | 11 个时间点实测 `offsetPx` 均 ≤0.5px（一次换行瞬间 7.7px，属动画中） |
| GPU 合成 | `gpu_compositing=enabled`，ANGLE/NVIDIA D3D11；层原因 **Video ×1 / WillChangeTransform ×5 / ActiveTransformAnimation ×4**；全页合成层 65 → 25（去掉逐行 will-change） |
| 长时稳定 | 60s 连续播放 + 20 次暴力 seek：JS 堆 2.8~3.3MB 无增长，DOM 节点恒 177，无 pageerror |
| 暂停/继续 | 空格键切换正常，暂停不重绘环境色 |
| 尺寸适应 | 1600×900 / 2560×1440 / 1440×900（信箱化）三档构图一致 |
| 原链路影响 | 未修改任何既有文件；`git status` 只有新增 |

## 未完成 / 待用户决定

1. **多曲目切换 UI**：`index.json` 支持多首、页面支持 `?i=` 与 `setlist`，但当前只有《またあした》一首
   已出片（《请杀死我的浪漫》无切点），多曲列表 **未在真实多曲环境下验证**。
2. 源为 1080p，舞台宽 >2560 时人物放大 >1.4× 略软（不打算改：源就这个分辨率）。
3. 极端左倾（322s 附近几秒）左缘可能掠过弹幕，已用 9.5% 左缘渐隐压制，**未做像素级消除**。
4. 出片链路（`--renderer gpu`）不受影响，但新播放器**不参与**出片，二者视觉不通用。

## 启动 / 回退

```bash
python tools/liveplayer.py play --date 2026-11-27 --index 0   # 备料+起服务+开浏览器
```

回退：删除 `renderer/player/live/`、`tools/liveplayer.py`、`tools/track_host.py`、`_media_cache/live/`。
