# 参考视频风格系统：项目审计与实施方案

> 2026-11-01 · 分支 `feature/ref-style-pv`，安全节点 tag `pre-refstyle-20261101`（HEAD=`8e0615a`，工作区干净）
> 本文是**阶段一（审计）+ 阶段二（方案）**的产物。阶段一未修改任何核心代码。
> 参考视频：<https://www.bilibili.com/video/BV1jUxC6SEko>《满月纪念曲｜写给我第一个喜欢的女孩【原创pv】》

---

## 0. 结论先行

| 问题 | 答案 |
|---|---|
| 现有系统能不能用 | 能。刚才交付的《画心》4K30 就是它产的（PASS 23 / WARN 3 / BLOCK 0） |
| 「原视频人物插入」在哪 | **已在生产链路里，是主干**。`prepare_live.py → live.json → pv_direct._live_fig → storyboard.fig → pv_engine.placeFigure/drawFigure` |
| 「JIZURA」在哪 | **已 vendor + 已写适配层，但没进生产链路**。`index_pv.html` 里一行都没加载它 |
| 参考视频分析能力 | **完全没有**。全仓无抽帧分析、无风格档案、无外部视频下载入口 |
| 要不要重写 | **不要**。缺的是一层「风格档案 → 导演层约束」，不是新引擎 |
| 最大风险 | ① 旧链路被新默认值污染 ② JIZURA 通道换机即失 ③ 把「参考风格」做成硬模板 |

**一句话方案**：新增 `styles/<id>.vsp.json`（视觉风格档案）这一层数据，由新的 `tools/pv_ref.py` 从参考视频里算出来，再让 `pv_direct.py` 用它**收窄**已有部件池（版面/转场/质感/字体阶梯/人物窗形），渲染引擎一行不改。JIZURA 以「帧序列层」身份接入，复用已经跑通的 live 序列机制。

---

## 1. 项目审计

### 1.1 坐标与版本

- 仓库 `D:/岁己歌切`，Python 3.10，Node 22，ffmpeg `D:/ffmpeg/bin/`。
- `RULES.md` **110 节**，是唯一真源；文档与它冲突时以 RULES 为准。
- 顶层：`songcut/`（检测/切点/歌词/QC 库）、`tools/`（45 个脚本）、`renderer/player/`（播放器页 + PV 引擎）、`renderer/pv/`（JIZURA 通道）、`docs/`（12 篇）、`_ref/`（3 个上游参考仓库，gitignore）、`cuts/`（成片与工作区，gitignore）。

### 1.2 两条并存、互不干扰的生成链路

**A. 传统歌切（1080p60，卡拉 OK 字幕）**
```
song_cutter.py --date D --player-page index.html|index_v10.html --only N
   └─ 切点精修 → 歌词三级降级 → job.json → render_song.cjs（Playwright+WebCodecs 逐帧）
      → 无声 mp4 → ffmpeg 混音 → 成品 + manifest.json → songcut.qc
```

**B. 文字 PV（4K30，koma=12，当前主用）**
```
录播 mp4 ─┬─ analyze_music.py ──► analysis.json（fps=30 帧级 bands/rms/voiced/midi10 + BPM + sections）
          ├─ pv_timing.py ─────► timing.json（行级来自 LRC，词级按音节权重+起音吸附）
          └─ prepare_live.py ──► live/hNN/{head,body,wide}/*.jpg + live.json   ← 人物层素材
                                    │
意象表 docs/<歌>.json ──────────────┼──► pv_direct.py ──► storyboard.json（40~48 镜）
                                    │                        │
                                    └────────────────────────┴──► pv_build.py
                                           （job → index_pv.html 逐帧 → mjpeg 序列 → ffmpeg 两阶段 → mp4）
```

两条链**共用** `songcut/` 的切点、歌词、封面、QC，只在「最后一段播放器页面」分叉。这是本项目最重要的结构事实：**升级 PV 层不会碰到传统歌切**。

### 1.3 核心资产 A：原视频人物插入（已在生产，必须原样保留）

| 环节 | 实现位置 | 契约 |
|---|---|---|
| 抽帧 | `tools/prepare_live.py` | `--src 录播 --cut-start 绝对秒 --start --dur`；产出 `head`（方形，640）`body`（3:4）`wide`（16:9）三种序列。裁切几何全参数化（`--head-x/y/s`、`--body-w/h`、`--wide-x/y/w/h`、`--face-x/y`） |
| 索引 | `live.json` | `{"cut_start":7654.5, "face":{"cx":0.5,"cy":0.5}, "h00":{"dir","n","fps","w","h","pattern":"w_%05d.jpg"}, …}` |
| 挂点 | `cuts/<date>/<歌>/live_spans.json` | 键=**句号 int**，值 `{"win":[起点, 时长], "mode","seq","side"}` ⚠ `win` 是 `[起点, 时长]` 不是 `[起点, 终点]`（RULES §106） |
| 编排 | `pv_direct._live_fig()` L1392 + `_split_spans()` L1377 | 生成 `shot.fig = {mode, seq, tOff, side, a, reveal, zoom0/1, fy, ph, fps}` |
| 闸门 | `pv_direct._check_live_spans()` L567 | 反查「窗口起点 = 镜头起点 − 2.0s」，错位即 `SystemExit` |
| 渲染 | `pv_engine.placeFigure()` L179 / `drawFigure()` L451 / `figBox` L1161 | 10 种窗形：`disc / round / pill / arch / soft / diag / crop / band / split / wash` |
| 指标 | RULES §103 | 出镜时长 ≥70% **且** 平均像素覆盖 ≥28% 画幅（《画心》实测 94.5% / 32.5%） |

**结论**：这块不但要保留，而且新系统里所有「人物与文字的空间关系」都必须建立在它之上，而不是另起一套。

### 1.4 核心资产 B：JIZURA（已 vendor、已有适配层、**未接生产**）

| 事实 | 证据 |
|---|---|
| 上游完整克隆在 `_ref/JIZURA/`（v0.10.1，MIT，40 个 `src/*.js`） | 目录存在 |
| 引擎拼接产物 `renderer/pv/vendor/jizura_engine.js`（2.18 MB） | 实物存在 |
| 我们的适配层 `renderer/pv/jz_adapter.js`（139 行）：把 `J.plan`/`J.Renderer` 包成「时间 t 的纯函数」，`setAudio` 吃**我们自己的** librosa 拍点，`drawPlan(name,ctx,t,mode)` 支持 `front/back/transparent` | 文件存在 |
| **唯一调用者是 `renderer/pv/probe.html`（探针页）** | `grep JZ\.` 仅命中 probe.html |
| **生产页 `index_pv.html` 只加载 11 个 `pv/*.js`，不含 jizura** | 脚本引用清单 |
| 换机即失 | `.gitignore` 第 65 行 `renderer/pv/vendor/*.js`；且 README 说的重生成脚本 `tools/build_jz_bundle.mjs` **不存在** |

**结论**：JIZURA 目前是「实验通道」，一条已交付成片都没用过它。它最有价值的三个官方能力我们一个都没接入：

1. `keyBg = 'black'|'green'` —— 官方原生合成通道（白字+演出，供外部 screen 叠加）。
2. `centerFree` —— 把一句唱词对半拆成两条侧带，中间 28%W 天然留给人（官方 `J.sideZones` 实测值）。我们现在是在 `pv_direct` 里手搓 `FIG_SAFE`/`availW` 避让。
3. 862 部件 / 27 风格 / 27 转场 —— 部件库规模我们只有约 1/8。

### 1.5 我们自己的引擎：真实部件盘点（不是估算）

| 族 | 文件 | 数量 | 说明 |
|---|---|---|---|
| 版面 layout | `pv_typo.js` | **13** | titleCard/keySplit/cropGiant/lowerThird/cross/bandHide/indexTable/vRuler/scaleSteps/marginNote/fullTrack/block/center |
| 入场/保持/退场/字饰 | `pv_parts.js` + `pv_parts_ext.js` | **55** | 注册在 `PV.ENTER/HOLD/EXIT/TREAT` |
| 背景 | `pv_bg.js` | **19** | halftone/hatch/grid/topo/rays/mega/edge… |
| 相机 | `pv_cam.js` | **11** | push/float/orbit/drift/pendulum/tiltDown/snapPan/focusIn/jelly… |
| **转场** | `pv_trans.js` | **13** | wipe/blinds/blocks/clock/dissolve/flash/grad/ink/iris/mask/push/slice/zoom |
| 配色 pack | `pv_scheme.js` | **7** | noir/crimson/paper/huaxin/hud/mono/dawn，每 pack 2~5 套 scheme + 字体角色 + 质感 |
| 合计 | | **约 111** | |

### 1.6 歌词与音频时间轴（禁止改动的部分）

- **行级时间 = LRC**（对齐链校正过），`timing.json.lines[i].t0/t1`。
- **词级/字级时间是估计值**：按音节权重分配 + 向 ±0.18s 起音峰吸附 + 强制单调 + 最小驻留 0.09s（实测吸附位移中位数 0.094s）。
- `analysis.json` 提供 `fps=30` 的帧级 `bands(48)/rms/voiced/midi10`、`bpm`、`sections`（段落标签 B1/C1/A1…）。
- **铁律**：任何新动画只能读这套时间轴，只能在既有 `t0..t1` 区间内细分；禁止平均分配、禁止整体延迟、禁止改文本。

### 1.7 素材管理

- 立绘：`C:/Users/Tsing/Pictures/管人立绘/岁己立绘`（145 张透明底，不入库），体检 `tools/art_audit.py`，方法论 `docs/tachie-pv-methods.md`。
- 字体：`renderer/player/pv_fonts/`（自托管 OFL，5 族 / 382 分片），拉取 `pv_fetch_fonts.py`，体检 `pv_fontcheck.py` / `pv_fonts_audit.py`。
- 直播序列：`cuts/<date>/<歌>/_work/live/`（单首约 280 MB，独立于成片目录）。

### 1.8 文档与代码的 6 处偏差（审计发现，按危害排序）

| # | 文档说 | 实际 | 危害 |
|---|---|---|---|
| 1 | `vendor/README.md`：`node tools/build_jz_bundle.mjs` 可重建 bundle | **脚本不存在**；bundle 本身被 gitignore | 换机 clone 后 JIZURA 通道永久失效，且无法在 CI/新环境复现 |
| 2 | `docs/jizura-gap-report.md`：「转场 27:0，我们完全没有，是最大单一原因」 | 已有 **13** 个转场，且《画心》全片在用 | 会误导后续决策（以为要新建转场系统） |
| 3 | `docs/pv-text-engine.md` §9.3：「不抄 JIZURA 的 trans，只用左右延迟擦除」 | 同上，已有 13 个 | 同上 |
| 4 | `pv-text-engine.md` §1 目录树 | 缺 `pv_trans.js / pv_scheme.js / pv_live.js / pv_slice.py / pv_figband.py` 等 | 新人/新会话会漏掉整层能力 |
| 5 | `jizura-gap-report.md` 说 JIZURA「bundle 留在 vendor（已 gitignore），可作可选通道」 | 属实，但**没写清楚适配层已完备** | 让人以为要从零接 |
| 6 | `docs/architecture.md` | 是**歌切链路**的设计记录，完全不含 PV 层 | 容易被当成全系统架构图 |

> 本次升级会把 1~5 一并修掉（阶段三第一件事）。

### 1.9 兼容性风险清单

| 风险 | 触发条件 | 缓解 |
|---|---|---|
| 新默认值污染旧片 | 给 `pv_direct` 加了新默认（如转场池/质感强度） | 所有新参数**默认关闭**；`--style` 不给 = 完全走原路径 |
| 意象表 `_meta.pack` 与新风格档案打架 | 歌已指定 `huaxin`，又传了 `--style` | 明确优先级：`--style` 只在**未指定 pack 的字段**上生效，pack 冲突时报警而不是覆盖 |
| 时间轴被第二套系统污染 | JIZURA 自己检测 BPM/拍点 | `JZ.setAudio()` 只喂我们 `analysis.json` 的拍点，禁用其内置检测 |
| 4K 渲染 OOM | 再加一层全幅序列 | 新层只在需要的镜头出现；`--pages 2` 已是定论（§107） |
| 参考视频版权/可下载性 | 部分视频需登录/地区限制 | 下载失败时降级为「用户本地文件」输入，不阻断 |

---

## 2. 参考视频实测（可行性已验，不是设想）

### 2.1 拉取与结构

`yt-dlp 2026.08.19` 直连可用，无需登录：

| 项 | 实测值 |
|---|---|
| 标题 | 满月纪念曲｜写给我第一个喜欢的女孩【原创pv】 |
| UP / PV 制作 | 艾薇拉-Elvira / @炸蛋-Egg（原唱 封茗囧菌，此为翻唱 PV） |
| 规格 | 1920×1080 / 30fps / **178.93s** / 5367 帧 / 46.7 MB |
| 镜头数 | `scene>0.28` 检出 **30 个**，平均 **5.9s/镜**（最长 25.6s，最短 1.9s） |

### 2.2 风格特征（人工判读，作为 VSP 的**验收基准**）

| 维度 | 观察 |
|---|---|
| **构图** | 画面内实际是 **≈2.4:1 letterbox**（上下黑边），底部**常驻一条 credit 条**（词曲/曲名/署名）。人物多为**右偏或左偏的中近景**，与「星芒粒子」同层，脸常被前景光斑半遮 |
| **人物** | 白发紫瞳少女（皮套立绘），**不是方框/圆窗**——直接融进画面，靠**轮廓光**（rim light）和粒子把她从背景撕开；只有少数镜头用「拍立得相框」把人物装进去（倾斜叠放） |
| **排版** | 白色**明朝/宋体**小字，双行块，右下方或左下方；**强调字换色**（桃粉/金），偶尔加半透明深色笔刷色块作底衬。另有**纯文字卡**：浅底居中两行，字号大、字距松、带淡描边 |
| **色彩** | 紫罗兰 / 蓝紫 / 冷白为主，高光偏青，暗部偏靛；整体**低饱和高明度**，靠光晕提亮 |
| **质感** | 柔和 bloom + 星芒（四角星）+ 轻微颗粒；不强调网点/套色错位（与我们的印刷语汇相反） |
| **图形** | 胶片齿孔边、拍立得相纸、飘落花瓣/光点、细线框 |
| **镜头语言** | 多为**缓推/缓移**，切点密（5.9s），转场以**淡入淡出 + 光晕闪白**为主，几乎不用硬切 |
| **节奏** | 副歌段切点明显加密、粒子密度上升；间奏段出现纯文字卡与长镜头 |

### 2.3 这个视频暴露的、我们**现在做不到**的 5 件事

1. **letterbox + 常驻 credit 条**：我们没有画幅内 letterbox 概念（只有满幅底）。
2. **轮廓光/粒子把人物从背景撕开**：我们的 `wash` 只会把人物洗淡，没有 rim light / 光斑层。
3. **人物直接融进画面（无窗形）**，且与文字**共层交错**：我们最接近的是 `crop`（半边出画），但没有「文字压在人身上/人压在字上」的显式层序。
4. **强调字换色**：我们整句同色，没有「一句里 1~2 个字换色」。
5. **浅底纯文字卡**（大字号+松字距+淡描边）：我们的 `titleCard` 只在片头片尾。

> 这 5 条正好是 VSP 要表达的「风格参数」，也正好都**不需要改引擎结构**——现有 13 版面 / 55 部件 / 13 转场里已有可组合的原料，缺的是「按参考风格收窄选择 + 少量新增部件」。

---

## 3. 实施方案

### 3.1 总原则

1. **引擎不动结构**：`pv_engine.js` 只做**加法**（新增可选层 `jz`、新增少量部件），不改既有绘制顺序与既有部件行为。
2. **风格是约束，不是模板**：VSP 输出的是**权重与取值范围**（例如「转场池 = dissolve/flash/grad」「字号阶梯 ×1.15」「颗粒 0.18」），由 `pv_direct` 在既有抽卡机制里按权重取，**同一份 VSP 重渲仍能出新构图**。
3. **旧路径零感知**：不传 `--style` 时，`pv_direct` 的随机序列、部件池、默认值**逐位不变**（用《画心》编排稿做回归比对）。
4. **时间轴只有一个**：JIZURA 与 PV 引擎共用 `timing.json` + `analysis.json`。

### 3.2 新增模块（4 个，全部是加法）

```
tools/pv_ref.py          参考视频 → 视觉风格档案（下载/抽帧/度量/聚合）
styles/<id>.vsp.json     风格档案（每片一份，互不覆盖）
styles/_schema.json      schema + 合法取值范围（给手改兜底）
tools/pv_jz.py           JIZURA 离线出帧 → 帧序列（复刻 live 的契约）
```

修改（均为可选分支、默认关闭）：

| 文件 | 改动 | 规模 |
|---|---|---|
| `tools/pv_direct.py` | 新增 `--style`：读 VSP → 覆盖 `LOOK/FIT/FIG_SAFE/转场池/质感/字号阶梯`；新增 `--jz` 挂 JIZURA 层 | 约 +180 行，1 个新函数组 |
| `renderer/player/pv/pv_engine.js` | 新增可选层 `shot.jz`（画在人物层之后/之前，按 `layer` 字段） | 约 +35 行 |
| `renderer/player/index_pv.html` | 条件加载 `jz_adapter`（仅 `--jz` 时） | +3 行 |
| `tools/build_jz_bundle.mjs` | **补齐**（修审计隐患 1） | 新文件约 40 行 |

### 3.3 VSP 数据契约（草案）

```jsonc
{
  "id": "bv1juxc6seko",
  "source": { "url": "…", "title": "…", "dur": 178.93, "w": 1920, "h": 1080, "fps": 30 },
  "frame": { "aspect": 2.39, "letterbox": 0.21, "credit_bar": 0.09 },   // 画幅内构图
  "palette": { "bg": ["#1a1230","#2a1d4a"], "fg": "#f2eefb", "accent": "#f0a8c8", "accent2": "#8fd6ff",
               "sat_mean": 0.31, "lum_mean": 0.42, "contrast": 0.68 },
  "type": { "family_role": "serif", "weight_range": [300,600], "size_ladder": 1.15,
            "tracking": 0.04, "leading": 1.35, "align": "right",
            "accent_word_ratio": 0.12, "vertical": false, "outline": 0.0 },
  "figure": { "presence": 0.83, "coverage_mean": 0.34, "bias": "right",
              "framing": ["none","polaroid","none"], "rim_light": 0.7, "crop_edge": 0.2 },
  "texture": { "grain": 0.18, "halftone": 0.0, "scanline": 0.0, "rgb_split": 0.06,
               "bloom": 0.55, "vignette": 0.12, "paper": 0.0 },
  "motion": { "shot_len_med": 5.9, "shot_len_p90": 12.4, "cut_per_min": 10.1,
              "cam_speed": 0.35, "cam_amplitude": 0.55, "motion_energy": 0.42 },
  "trans": { "pool": ["dissolve","flash","grad"], "dur_med": 0.55, "hard_cut_ratio": 0.1 },
  "decor": ["sparkle","petal","film_strip","polaroid","thin_rule"],
  "map": { "pack": "violet", "layouts": {"block":3,"cross":2,"lowerThird":2,"titleCard":1},
           "treats": ["softGlow","solidShadow"], "excluded": ["halftone","bag","outline"] },
  "shots": [ { "i":0,"t0":0.0,"t1":6.4,"types":"text_card","fig":0.0,"words":9 } ],
  "confidence": { "figure":0.66, "typography":0.58, "texture":0.71 }
}
```

`confidence` 是硬要求：凡靠启发式估出来的量（人物边界、字重、字距）必须标可信度，低于阈值时**降级为「仅建议」**，由 `--style-strict` 决定是否采纳。

### 3.4 JIZURA 接入方式：**帧序列层**（本次最关键的设计取舍）

三条可选路径，选第 ③ 条：

| 路径 | 做法 | 为什么不行/行 |
|---|---|---|
| ① 运行时直画 | 每帧调 `JZ.drawPlan(ctx,t)` | JIZURA 每帧走三趟色散 pass，4K 下必然拖垮渲染（现 8.2 fps 都靠 mjpeg 优化） |
| ② 替换引擎 | 用 JIZURA 当渲染器 | 直接违反「不推倒重写」，且丢失全部 QC 与确定性保证 |
| **③ 离线帧序列** | `pv_jz.py` 用 `probe.html` 无头渲出 PNG 序列 → 按 `live.json` 同款契约交给 `pv_direct` → 引擎当一层贴图合成 | **复用已经跑通的 live 机制**；渲染热路径零新增成本；确定性可验（同 job 两遍 md5）；JIZURA 仍吃我们的 `timing.json` |

契约与人物层**逐字段同构**，可直接复用 `placeFigure` 的窗形逻辑：

```jsonc
"jz": { "dir":"…/jz/h03", "n":118, "fps":6, "w":3840, "h":2160,
        "pattern":"j%05d.png", "layer":"behind"|"front", "blend":"screen"|"normal",
        "tOff":-42.0, "a":0.9 }
```

`layer:"front"` = 歌词压在人物之上；`"behind"` = 人物压住部分字（这正是「人物与文字真实交叠」）。`blend:"screen"` 对应官方 `keyBg='black'` 白字层。

### 3.5 旧工作流如何保证不变

1. `--style` / `--jz` 都**不给** ⇒ `pv_direct` 走原代码路径，随机序列不变。
2. 回归判据：用《画心》的 `timing/analysis/live` 重跑导演层，**storyboard.json 除时间戳外逐字段相等**（`--seed` 固定）。
3. `song_cutter.py` 传统链路**完全不碰**（不 import、不改参）。
4. 新增文件全部在新目录 `styles/`，不动 `cuts/`（gitignore）。

### 3.6 分阶段与验收

| 阶段 | 交付 | 验收（必须实跑） |
|---|---|---|
| **S1 · 修文档与隐患** | 补 `build_jz_bundle.mjs`、更新 3 篇过期文档 | `node tools/build_jz_bundle.mjs` 能从 `_ref/JIZURA/src` 重新产出与现有 `jizura_engine.js` 一致的 bundle（比对 md5 或差异行） |
| **S2 · 分析器** | `tools/pv_ref.py`（fetch/analyze） | 对 BV1jUxC6SEko 产出 `styles/bv1juxc6seko.vsp.json`，人工核对 §2.2 的 8 条特征至少命中 6 条；**同一视频跑两次结果一致** |
| **S3 · 风格生效** | `pv_direct --style` | 用 30~60s 片段：① 不给 `--style` 与原编排稿逐字段一致 ② 给了之后 pack/转场/字体阶梯/质感按 VSP 变化，且 `pv_check` 全绿 |
| **S4 · JIZURA 层** | `pv_jz.py` + 引擎 `jz` 层 | 单镜预演出图 → 合成进 30s 样片 → 目视「字在人上/人在字上」两种层序都对 |
| **S5 · 人物交互升级** | 新增 `rimLight` / `noFrame` 窗形 + 强调字换色 | 用岁己立绘 + 《画心》素材各跑 30s，人物不形变（RULES §103 指标不回退） |
| **S6 · 整片** | 一首完整歌 | `pv_flash → songcut.qc → pv_check → 成片抽帧` 四关，与《画心》同标准 |

---

## 4. 需要你拍板

| # | 决策 | 选项 | 我的建议 |
|---|---|---|---|
| ① | 参考风格的作用力 | A. 只影响「取值收窄」（保守）／B. 允许新增 pack 与部件（彻底） | **B**，否则学不到「紫罗兰光晕 + 无框人物」这类结构性差异 |
| ② | JIZURA 的定位 | A. 帧序列层（离线预渲，稳）／B. 运行时直画（省磁盘，慢） | **A** |
| ③ | 落地曲目 | A. 用现成《画心》素材做对照实验（快）／B. 直接上一首新歌 | **A 先对照**，再上新歌 |
| ④ | 参考视频数量 | A. 只做这一支／B. 做成能攒「风格库」 | **B**，`styles/` 目录天然支持多份 |
