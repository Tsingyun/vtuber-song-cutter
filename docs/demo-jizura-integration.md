# 画心 · 副歌 Demo —— JIZURA 在工作流里的接入位置与作用范围

> 本文对应交付物：`cuts/2026-10-31/画心/demo/【岁己SUI】画心·副歌Demo【JIZURA】.mp4`
> 生成日期：2026-11-01　分支：`feature/ref-style-pv`

---

## 1. 先说清一件事：为什么是「带音轨的视频」而不是纯音频

JIZURA 是**排版引擎**，它只作用于画面，对音频没有任何作用。所以「让 JIZURA 实际纳入
生成流程」这件事，只能在**画面**上验证。

如果只交一个 mp3，JIZURA 就必然只能是「引用了但没参与」——那正是任务书里禁止的。

因此主交付物是**一段带音轨的 34.8 秒视频**：音频取自《画心》副歌，画面由我们的 PV 引擎
+ JIZURA 帧序列层合成。另外从同一条视频里抽出纯音频（mp3 320k），两者严格同源
（不是各自压一遍，所以不存在「音频和画面对不上」的可能）。

---

## 2. 接入位置：JIZURA 在七道工序里的确切坐标

```
《画心》副歌 216.33–251.13s
   │
   ├─① 音频切片 ────────────── ffmpeg -ss 216.33 -t 34.8（afade 0.45/1.3）
   │                            → _tmp/jz/chorus2/chorus.wav（48k/16bit/立体声）
   │
   ├─② JIZURA 预渲 ──────────── tools/pv_jz.cjs          ★ JIZURA 本体在这里跑
   │      │  输入：timing.json / bpm=138.46 / style=noir / accent=#5A5897
   │      │       窗口 start=216.33 dur=34.8 fps=15
   │      ├─ renderer/pv/probe.html        （无头页面，适配层入口）
   │      ├─ renderer/pv/jz_adapter.js     （J.plan + J.Renderer 包成 t 的纯函数）
   │      └─ renderer/pv/vendor/jizura_engine.js（JIZURA 字面 v0.10.1，MIT，2.17MB）
   │      → 522 张 1920×1080 透明 PNG（j_00001…j_00522）+ jz.json（计划摘要）
   │
   ├─③ 编排 ────────────────── tools/pv_direct.py --jz
   │      → storyboard.json：meta.live.jz 登记序列；覆盖范围内每镜挂 shot.jz
   │        {seq:"jz", tOff:0, layer:"behind", blend:"screen", a:0.40}
   │
   ├─④ 装载 ────────────────── renderer/render_song.cjs（job.live → ST.seq）
   │      与 live 照片序列**同一套加载器**：同一个 ST.seq、同一个 getFrame、
   │      同一个滑窗预载 spanOf —— 没有第二套时间轴，也没有第二个加载器
   │
   ├─⑤ 合成 ────────────────── renderer/player/pv/pv_engine.js  drawJz()
   │      绘制顺序：纸底 → 背景 → 背景装饰 → **JIZURA（后）** → 人物（后）
   │                → 歌词 → 人物（前）→ JIZURA（前，仅 layer=front）→ 前景装饰
   │
   ├─⑥ 编码 ────────────────── ffmpeg / NVENC p1 / CBR 12 Mbps / 1920×1080@30
   │
   └─⑦ 封音轨 ──────────────── ffmpeg -map 0:a -map 1:v -c:v copy -c:a aac 256k
```

**关键点：JIZURA 只负责 ②（产出一批帧），从 ③ 开始它就只是「一层贴图」。**
这不是削弱它，而是让它的产物变成可检查、可复现、可回滚的静态资产——
同一批 PNG 重渲一百次，画面逐字节一致；而运行时逐帧直画做不到这一点，
且 JIZURA 每帧要跑三趟色散 pass，4K 下会把渲染时间翻几倍。

---

## 3. 时间轴为什么不会漂（这是最容易出事的环节）

JIZURA 自带一套「前奏/间奏/段落」规划。如果让它自己从音频检测时间轴，它会按自己的
理解重排歌词 —— 歌词整体位移几十毫秒到几秒，正是任务书里明令禁止的
「因加入动画导致歌词延迟」。

本流程的处理：

1. 给 JIZURA 的 LRC 由**我们自己的** `timing.json` 生成，并按窗口起点整体平移
   （`t' = t − 216.33`），逐句时间一个不改；
2. 拍点不让它检测，直接把实测 BPM=138.46 换算成等间距网格喂进去；
3. 它只被允许决定「怎么画」，不允许决定「什么时候唱」；
4. 合成时取帧用 `im = getFrame('jz', t + tOff)`，`tOff = 0`（序列帧 0 = 窗口 0s），
   与 live 照片序列共用同一条公式。

---

## 4. 作用范围：实测到哪、盖了多少

### 4.1 时长与镜头覆盖

| 指标 | 实测值 | 说明 |
|---|---|---|
| 名义覆盖 | 4/4 镜（0.00–34.80s） | `shot.jz` 挂在每一个镜头上 |
| **有效覆盖** | **0–23.2s** | 之后 JIZURA 自己输出**全透明帧** |
| 有内容帧占比 | 58.3%（31/53 个采样秒） | 每 1 秒采一帧统计 alpha>8 的像素 |
| 空白段 | 6s、13s、23–34s | 6/13s 是它分镜间的留白；23s 之后是整段退场 |

**23s 之后全空不是接线错误**：直接检查 `j_00405.png`（t=27.0s）与 `j_00500.png`
（t=33.3s），alpha 通道**全为 0** —— JIZURA 自己在尾段就不产内容了。
`plan.duration` 确实是 34.8，不是时长传错。这是它的规划行为，本流程没有为了
「让 demo 好看」去扭曲它的输入（例如假装总长 40s 骗它多排几镜），
因为那样得到的画面就不再是「JIZURA 的真实能力」了。

### 4.2 像素贡献（同一编排稿，挂 / 不挂 JIZURA 各渲同帧）

| 时间点 | 变化像素占比 | 平均差（/255） | 最大差 |
|---|---|---|---|
| t = 3.0s | 1.9% | 0.18 | 8 |
| t = 9.5s | 2.4% | 0.31 | 20 |
| t = 16.5s | **20.6%** | 1.71 | 14 |
| t = 27.0s | 0.0% | 0.00 | 0（该帧 JIZURA 本就全透明） |

对照口径：同一份编排稿，`--jz` 与不带 `--jz` 各用 `pv_stills` 渲同一时刻，
逐像素比 RGB。**不用成片帧做对照** —— 成片经过 H.264 有损编码，
编码噪声本身就有 5% 上下的「差异像素」，会把 0% 的那一帧也测成 5.3%（实测踩过）。

这就是「不是占位」的量化证据：去掉 `--jz`，t=16.5 那一帧有 **20.6%** 的像素会变。

### 4.3 JIZURA 实际画出了什么

从成片抽帧可见（t=16.5）：

- 背景整片铺开的**巨型重复唱词**（同一句被放大、重复、平铺成纹理）
- 纵向虚线、编号标签（`82`）、细线框
- 低透明度的色块与网格

这些都是它的引擎**实际渲出来**的，不是我们引擎的装饰件（我们的装饰件是另一套：
`No.34`、刻度线、角落标记）。

---

## 5. 边界：JIZURA 在这条流程里**不做**什么

| 事项 | 由谁负责 |
|---|---|
| 歌词文本 | `timing.json`（JIZURA 只接收，不改写） |
| 歌词时间 | `timing.json`（禁用它自检，见 §3） |
| 人物 | 我们自己的 live 序列（JIZURA 没有人物层） |
| 版面/字号/字距 | `pv_direct` 抽卡 + 风格档案 VSP |
| 主色调/纸包 | `pv_scheme.js` 的 `violet` 包（JIZURA 的 style 只影响它自己那层） |
| 转场 | `pv_trans.js` 的 13 种（JIZURA 的 cuts 是层内切换，不是镜头切换） |

换句话说：**JIZURA 是「后景排版层」的供应者，不是导演**。
它提供质感与第二套排印语言，镜头怎么切、字怎么排、人在哪儿，仍然由我们的编排层决定。

---

## 6. Demo 的具体参数（可复现）

| 项 | 值 |
|---|---|
| 歌曲窗口 | 216.33 – 251.13s（《画心》副歌：爱着你 / 画着你 / 记着你的脸色 / 你是我一首唱不完的歌） |
| 风格档案 | `styles/bv1juxc6seko.vsp.json`（参考片 BV1jUxC6SEko，pack=violet 提案） |
| 纸包 | `violet/1`（月白） |
| 人物窗形 | pill（横窗）/ disc（圆窗）/ arch（拱窗）/ band（横幅），四句四种，左右 l/r/l/r 交替 |
| 人物序列 | `live/h21…h24`（画心定案素材，未改动 `live_spans.json`） |
| JIZURA | style `noir`，accent `#5A5897`，15fps，layer=behind，blend=screen，**α 0.40** |
| 成片 | 1920×1080 @30fps，H.264 CABR 12 Mbps，AAC 256k 48kHz 立体声 |

**α 为什么是 0.40 而不是 1.0**：1.0 时它的巨字与前景唱词是同一句话，
两层叠在一起读作「字在打架」；0.55 仍偏抢；0.40 才读成「质感层」。

**wash 窗形为什么被换掉**：原计划第 1 句用 `wash`（满幅淡底），实渲发现它在月白纸上
等于「白雾压白纸」——人像几乎读不出来。`wash` 的定案参数（`a=0.4/veilK=0.74/paper=0.58`）
是在深色纸上调的，浅底不成立。这条结论已写进 `fig_spans.json` 的 `_meta.drop_wash`。

**crop 窗形为什么也被换掉**：原计划第 4 句用 `crop`（半边出画），配 16:9 素材实测被裁成
一条约 170px 的窄缝——那不是「半边出画」，是「切片」，属于缺陷。`fig_script` 的注释里
本来就写着「crop 宽度不可控，和平涂版面抢版心时算不准要让多少」，这次是它第一次被
喂真实素材，问题立刻现形。换成 `band`（横幅，h=720）后人物完整、不压字。

---

## 7. 复现命令

```bash
# ① 切片（副歌）
ffmpeg -ss 216.33 -t 34.8 -i ".../_work/audio.mp3" \
  -af "afade=t=in:st=0:d=0.45,afade=t=out:st=33.5:d=1.3" \
  -c:a pcm_s16le -ar 48000 -ac 2 -y _tmp/jz/chorus2/chorus.wav

# ② JIZURA 预渲（★ 本体在这里）
NODE_PATH=... node tools/pv_jz.cjs \
  --timing cuts/2026-10-31/画心/_work/timing.json --out _tmp/jz/chorus2 \
  --start 216.33 --dur 34.8 --fps 15 --bpm 138.46 --res 1080 \
  --style noir --accent "#5A5897"

# ③ 编排（挂人物 + 挂 JIZURA）
python tools/pv_direct.py --timing ... --analysis ... --start 216.33 --dur 34.8 \
  --title 画心 --artist 张靓颖 --vocal 岁己SUI --fps 30 --koma 12 --seed 20261027 \
  --style styles/bv1juxc6seko.vsp.json \
  --fig-spans _tmp/jz/chorus2/fig_spans.json --live .../live/live.json \
  --jz _tmp/jz/chorus2/jz.json --jz-layer behind --jz-alpha 0.40 \
  --out _tmp/jz/chorus2/sb.json

# ④⑤ 渲染
node renderer/render_song.cjs --job _tmp/jz/chorus2/job.json --encoder ffmpeg

# ⑥⑦ 封音轨
ffmpeg -i chorus.wav -i video.mp4 -map 0:a -map 1:v -c:v copy \
  -c:a aac -b:a 256k -shortest -movflags +faststart -y demo.mp4
```

---

## 8. 本次改动清单（只列与 JIZURA / 人物接入相关的）

| 文件 | 改动 | 为什么 |
|---|---|---|
| `tools/pv_jz.cjs` | 日志格式串 `%d/%.1f` → 模板串 | Node 的 `util.format` 不认 `%.1f`，会静默输出字面量**且不消耗参数**，导致后面每个占位符都错位（实测把 style 打成 1080、lines 打成 NaN） |
| `tools/pv_direct.py` | 新增 `--fig-spans` | 抽卡路径原本的 fig 来自 `fig_script()`，`seq` 写死 `"head"/"body"`，而画心的真实序列叫 `h00…h33` ⇒ 取不到图、静默不画，编排稿上却写着「4 个有人镜头」 |
| `tools/pv_direct.py` | `_fig_from_spans` / `_live_fig` 的 `tOff = w0 − win.start` | 原式 `tOff = −win.start` 是**绝对**值，而镜头 t 是**相对窗口**的。整片（w0=0）看不出问题；切片时帧号变负，人物全片消失且零报错 |
| `tools/pv_direct.py` | `fig_reserve()` 补 `wash/soft/split` 分支，并让 `pill` 用显式 `w`、`arch` 用 `h×wRatio` | 原来只认 `disc`（用 r）与其余（一律用 `h×0.752`）。`wash` 没有 h ⇒ KeyError（而它是画心定案用得最多的形态，12/34 处）；`pill` 有 w=660 却按 h=410 估成 332px ⇒ 实测 t=3.0 的字被窗压住半个「爱」字 |
| `tools/pv_direct.py` | `_check_live_spans()` 加 `expect`，抽卡路径也接闸门 | 挂点表换了条数（34 → 4）时报错要说得准；抽卡路径原来没有这道闸门 |

---

## 9. 已知问题

1. **JIZURA 在 23s 后自己退场**（§4.1）。视觉上第 4 镜后半段只剩「人物 + 歌词」两层，
   层次比前面薄。要补，得从它的 `cuts` 规划入手，不是改 α 能解决的。
2. **`--jz-alpha` 仍然是个手调旋钮**。理想做法是按纸的亮度自动定 α（浅底压低、深底放宽），
   目前没有标定样本。
3. **JIZURA 的 `enabled` 分组未接**：它会把自己那套歌词层一起渲出来。现在只能靠 α 压低，
   正确做法是关掉它的歌词分组、只保留动效与装饰。
4. **人物窗形是我为 demo 选的**，不是画心定案的原样。定案第 21/22/23/24 句是
   `wash / wash / band / wash`，demo 改成 `pill / disc / arch / band` ——
   一是为了让四句各自展示一种参与方式（一屏能看清差别），二是 `wash` 在月白纸上不成立（§6）。
   `cuts/2026-10-31/画心/live_spans.json` 本身**没有改动**，定案成片不受影响。

## 10. 一分钟自查清单

打开 `【岁己SUI】画心·副歌Demo【JIZURA】.mp4`，按这五条看：

1. **听**：第 0 秒进「爱着你」，第 34.8 秒收在「你是我一首唱不完的歌」尾音，中间不断层。
2. **人物**：四个镜头四种窗形 —— 横窗（0–6.5s）→ 圆窗（6.5–13.4s）→ 拱窗（13.4–20.4s）
   → 横幅（20.4–34.8s）。没有一帧的字被窗压住。
3. **JIZURA**：把进度拖到 **13–20 秒**（拱窗那段）——背景会铺满半透明的巨型重复唱词、
   纵向虚线、编号标签。这就是 JIZURA 那层。可对照同目录的 `对照_四帧.jpg`。
4. **层次**：从上到下应能数出四层 —— 纸底 / JIZURA 巨字纹理 / 人物 / 前景歌词，互不打架。
5. **23 秒之后**：JIZURA 那层会消失（它自己的规划，见 §4.1），画面只剩人物 + 歌词。
   这不是渲染失败。

要复验「JIZURA 真的进了渲染」，而不是只看画面：

```bash
# 同一份编排稿，去掉 --jz 再渲同帧，逐像素比
python tools/pv_direct.py ...（不传 --jz）--out sb_nojz.json
node tools/pv_stills.cjs --sb sb.json      --times 16.5 --out A --res 1
node tools/pv_stills.cjs --sb sb_nojz.json --times 16.5 --out B --res 1
# A 与 B 应有约 20.6% 的像素不同；完全相同就说明那一层根本没画上去
```
