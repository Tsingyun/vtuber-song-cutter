# 统一时间轴与图层描述（mvgraph）

> 对应任务书「三、核心架构升级」「六、统一时间轴」「七、分层可复用中间表示」。
> 本文说明**已经落地的是什么、刻意没做什么、还剩什么**。

## 1. 为什么需要这一层

现有管线是两条链共用一个仓库：

```
歌切链   auto_cut.py / song_cutter.py / songcut/*  →  segments/、_media_cache/、cuts/
PV 链    tools/pv_one.py → pv_direct（编排稿）→ render_song.cjs（逐帧）→ ffmpeg
```

人物**已经**在逐帧合成阶段作为图层参与（`pv_engine.drawFigure` 按 `shot.fig` 画在 canvas 上，
不是最后贴片）。所以任务书担心的「人物被降级成后期贴片」在这个仓库里并没有发生。

真正的问题不是缺图层系统，而是**图层与时间的关系散落在三个文件里，没人能一次读全**：

| 事实 | 以前写在哪 | 出错时的表现 |
|---|---|---|
| 这一片多长、多少帧 | `storyboard.meta.dur/fps`、命令行、manifest | 三者不一致时**没人会发现** |
| 哪一帧该有谁 | `storyboard.shots[].fig`（含 `tOff`） | 序列帧不够 → `getFrame` clamp → **人物定格不动**，不报错 |
| 序列在哪、有多少帧 | `live.json` 的 `dir/n/fps` | 目录改名 → 静默返回 null → **那一镜没人** |
| 层级前后关系 | `pv_engine` 的绘制顺序（硬编码） | 想在字后面加东西 → 只能改引擎 |

`mvgraph` 把这几份事实读成一个结构（**不复制、不改写**），并做算术检查。

## 2. 定位：只读中间表示，不接管渲染

```
segments ─┐
storyboard├─→ songcut/mvgraph.build() ─→ graph{project, timeline, layers[], checks[]}
live.json ┤                                     │
analysis ─┘                                     ├→ tools/pvlink.py graph  （人读/落盘）
                                                └→ tools/pvlink.py check  （出片闸门）
```

渲染仍由 `renderer/player/pv/*` 消费 storyboard 完成。**这一层一行渲染代码都没加**，
理由很简单：现有的 Canvas2D + Playwright 逐帧链路是稳定且已验证的，
为了「架构好看」把它推翻重写，等于把「人物出现在正确的时间」这个已经做对的事押上去重赌。

它的价值在两处：

1. **唯一出口**：换引擎、加图层、做校验，都读同一个结构，不再各自去猜 storyboard 的字段。
2. **把静默错变成可报的错**：见 §5。

## 3. 时间基准

```python
timeline = {
  "fps": 30, "dur": 269.15, "frames": 8074,
  "audio": {"bpm": ..., "bar_sec": ...}, "sections": [...],
  "unit": "frame",            # 主键是输出帧号
  "mixed_rates": True,        # 素材有 24/10/5fps 的序列，换算必须带各自帧率
}
```

- 全图只有 `frame_of(t, fps)` / `time_of(f, fps)` 两个换算，四舍五入，与编码器取整一致。
- 层自带 `frame0/frame1`，便于「第 N 帧该画什么」直接查。
- **素材帧率与输出帧率解耦**：人物序列可能按 24/10/5fps 抽（长间奏降帧省内存），
  `getFrame` 用的是序列自己的 fps。`_footage_frame_span()` 按序列 fps 换算帧号，
  不假设全片同帧率。

## 4. 图层描述（Layer Descriptor）

```json
{
  "id": "footage:12", "type": "footage", "name": "sui-live",
  "source": "s3",
  "t0": 83.5, "t1": 108.32, "frame0": 2505, "frame1": 3249,
  "z": 20,
  "rect": null, "scale": 1.0, "rot": 0.0, "alpha": 0.9,
  "mask": "rect", "crop": null, "anim": null, "blend": "source-over",
  "visible": true, "camera_space": false, "geom": "engine",
  "extra": {"mode": "slab", "side": "r", "h": 925, "tOff": -83.5, "seq_n": 745, "seq_fps": 30}
}
```

字段说明：

| 字段 | 含义 | 备注 |
|---|---|---|
| `type` | 八类之一，见下表 | |
| `source` | 素材来源。背景是 `procedural:sunrise+petal`，人物是序列名，歌词是 `timing:line` | |
| `t0/t1/frame0/frame1` | 起止时间与帧 | 全部相对成片 0 点 |
| `z` | 叠放次序 | 与 `pv_engine` 实际绘制顺序一致 |
| `rect/scale/rot` | 位置尺寸 | **`geom="engine"` 时 `rect=None`** |
| `mask` | 遮罩语义：`circle` / `rect` / `feather-rect` | 由 `fig.mode` 推出 |
| `camera_space` | 是否参与镜头变换 | 引擎当前一律屏幕空间 |
| `extra` | 编排稿的显式参数原样带出 | 不重算 |

**为什么不填 `rect`**：人物窗口的几何是 `pv_engine` 运行时按 `mode/side/h/safe margin`
算出来的。在 `mvgraph` 里按公式再算一份，就是第二份真相 —— 改引擎忘了改这里时，
两边都不报错，只是「描述说在右边、画面在左边」。任务书要求的「位置/尺寸/遮罩可描述」
由 `extra` 里的显式参数满足：`mode` 决定形态，`side/h/r/wf` 决定大小，来源只有一个。

## 5. 校验项

`tools/pvlink.py check` 只看 `error`（非零退出），`graph` 全量打印。

| code | 级别 | 抓的是什么 |
|---|---|---|
| `footage.out_of_seq` | error | 人物要用的帧号超出序列范围（引擎会 clamp → 画面定格或整段没人） |
| `footage.seq_missing` | error | `fig.seq` 不在 `live.json` 里（该镜静默没人） |
| `footage.dir_missing` | error | 序列目录不存在 |
| `footage.frames_missing` | error | 声明 n 帧、磁盘不足 n 帧 |
| `footage.window_offset` | warn | `t0 + tOff` 偏离 2.0s 约定（挂点表与镜头错位） |
| `layer.out_of_range` | error | 层超出片长 |
| `layer.empty` | error | 层时长为 0 |
| `lyric.span` | warn | 词级时间超出层区间（差 >0.35s） |
| `lyric.split_unsupported` | error | 请求「同一句拆到人物前后」但引擎按行整行画 |
| `shot.gap` / `shot.tail` | warn | 镜头之间/片尾有空档（黑帧） |
| `z.conflict` | warn | 同刻同 z 的两层（叠放顺序取决于数组顺序） |
| `cap.bg3d` / `cap.fg` / `cap.footage_camera_space` | warn | 描述里有这类层，引擎还没入口 |
| `timeline.empty` / `timeline.fps` | error | 时长/帧率没给 |

容差口径：时间 `TOL=0.05s`；词级 `TOL_LYRIC=0.35s`（词边界来自语音、镜头边界来自语义，
0.05 的容差会把「按语义提前收尾」全报成越界，噪音一多闸门就等于没有）。

## 6. 八类图层与引擎能力

| 类型 | 引擎现状 | z |
|---|---|---|
| `bg` 背景场景 | ✅ `pv_bg.js` 程序化背景件 | 0 |
| `bg3d` Blender 三维 | ❌ 无入口（`caps.bg3d=False`） | 5 |
| `lyric` 歌词 | ✅ 按行整行画，`--jz-layer behind/front` 全局一层 | 10 / 30 |
| `footage` 真实录播人物 | ✅ `drawFigure`，屏幕空间 | 20 |
| `text` 装饰文字 | ✅ `decor` | 40 |
| `fx` 特效 | ✅ 整帧 `post`，**不支持分层** | 50 |
| `fg` 前景遮挡 | ❌ 无独立入口 | 55 |
| `trans` 转场 | ✅ `trans`（0 时长的硬切不建层） | 60 |

任务书 §3.3 的四个遮挡场景里：

- A（人物在歌词前方）：**已实现** —— `--jz-layer front` 把歌词提到人物之上，或反之。
- B（歌词从人物背后经过）：**已实现** —— 默认 `behind` + 人像面板。
- C（同一句歌词拆成前后两半）：**未实现**，`caps.lyric_split=False`，
  `mvgraph` 会对这种请求报 `lyric.split_unsupported`（明确说做不到，不假装）。
- D（人物作为主体 + 少量文字）：**已实现** —— 人物铺满 + 短句。

## 7. 用法

```bash
# 生成场景图（人读摘要 + 校验）
python tools/pvlink.py graph --date 2026-11-01 --song 小丑的品格 \
    --storyboard cuts/2026-11-01/小丑的品格/_work/storyboard.json

# 逐层打印
python tools/pvlink.py graph ... --layers

# 落盘给后续工具
python tools/pvlink.py graph ... --out _tmp/graph.json

# 出片闸门（有 error 即 rc=3）
python tools/pvlink.py check --date 2026-11-01 --song 小丑的品格 --verbose
```

## 8. 人物挂点表：从编排稿反推（`tools/pv_spans.py`）

分段人物序列不能随机挂 —— 随机挂出来的 `fig` 没有 `tOff`，引擎按成片时间直接索引，
会取到「另一句的画面」，而画面里照样有人（只能靠肉眼看口型才发现）。

所以引入「挂点表」，键是**句号**（`shot.line`），值是形态 + 序列名 + 窗口：

```json
{"13": {"seq": "s2", "mode": "band", "win": [67.69, 8.81]}}
```

以前这张表是 `pv_direct.py` 里写死的字面量（1027 那场六段），换歌只能手抄时间。
`tools/pv_spans.py` 从编排稿反推：

```bash
python tools/pv_spans.py --sb _work/storyboard.json --n 6 --out _tmp/spans.json
python tools/pv_live.py --date 2026-11-01 --from-segments --song 小丑的品格 \
    --spans _tmp/spans.json --out _tmp/live
python tools/pv_prepare.py ... --live _tmp/live/live.json --fig-spans _tmp/spans.json
```

三条约束（都有单测）：

1. 只挑 `kind=line` 且 `line>=0` 的句子（片头/片尾卡与 breath 镜不属于任何一句）；
2. `win = [t0-lead, 镜头长+lead+tail]`，且**不越出片长**（`pv_live` 的闸门会拦越界）；
3. 形态只用 `disc/band/slab` —— 这是 `fig_script` 有固定几何的三种。
   `arch/wash/split` 那种「逐句定稿」的几何只能手写完整表（轻量表里没有 `r/h/wf`，
   硬塞会得到「让位算错」而不是「画得难看」）。

`_fig_from_spans` 检测到表项缺几何时，用 `fig_script(mode=...)` 的固定几何补齐 ——
几何只有一处真源，表里不再抄一份。

## 9. 实测（2026-11-03，《小丑的品格》2026-11-01）

| 项 | 纯视觉 | 带真实人物 |
|---|---|---|
| 场景图 | 129 层（lyric 50 / trans 47 / text 20 / bg 12） | 131 层（+ footage 6） |
| 校验 | 0 error | 0 error / 0 warn |
| 人物覆盖 | 0% | 27.0% 时长 |
| 成片 | — | 8075 帧 / 269.17s / 1920×1080@30 / 12.08 Mbps / 396 MB |
| 耗时 | — | 合计 364s（render 330s，4 核 CPU 软编） |

目视抽帧：

- `t=11s`（disc）：右侧圆窗，真实录播画面（黑白双色调），歌词在左下，不重叠。
- `t=90s` / `t=243s`（slab）：右侧大竖幅，文字在另一侧。
- `t=30s` / `t=120s`（无人物镜）：版面全宽，右侧没有为「不存在的人」留白。

时序零漂移：`graph.timeline.frames = 8074`，成片 8075 帧（首尾各半帧的取整差），
`pvlink check` rc=0。

**确定性（任务书测试 H）**：同一 job 再渲一次，视频流 md5
`81f00029273b327b1c80d01f998a72e3` **两次完全一致**（`tools/pv_determinism.py`）。
人物是逐帧从图像序列按 `(t + tOff) × seqFps` 取的，不经浏览器视频解码，
所以没有「播放进度影响取到哪一帧」的问题。

## 10. 与任务书的差距（如实）

| 任务书要求 | 现状 |
|---|---|
| 统一时间轴（帧号为基准） | ✅ `timeline()` + `frame_of/time_of` |
| 统一图层描述（八类） | ✅ 描述层齐了；`bg3d`/`fg` 引擎无入口 |
| 人物作为图层参与逐帧合成 | ✅ 早已如此（`drawFigure`），本轮把它纳入统一描述 |
| 同一句歌词前后分层 | ❌ 引擎按行整行画 |
| 人物参与镜头运动 | ❌ 人物是屏幕空间；`camera_space` 字段与 `caps` 已就位 |
| Blender 三维 | ❌ 无入口（本轮未接） |
| 分层后期（背景模糊、人物保原画质） | ❌ 目前整帧 post |
| 确定性渲染 | ⚠️ 部分：seed 固定 + 素材按帧索引；未做逐字节比对回归 |
