# 术力口文字 PV（文字演出引擎）

面向「歌词即画面主角」的第三套视觉方案。与现有两套播放器（`index.html` 明纸版 /
`index_v10.html` 深夜电台版）**并存**，互不影响，用 `--player-page` 切换。

```
song_cutter.py --date 2026-10-27 --player-page index_pv.html --only 3
```

---

## 1. 这套东西由什么组成

```
renderer/player/
  index_pv.html          页面壳 + 渲染契约（HL.ready → __setup → __frame）
  pv/pv_core.js          基础工具：噪声、渐变贴图、随机（全部可播种）
  pv/pv_type.js          字体表、字形构建、9 种构图（layout）
  pv/pv_parts.js         入场 / 保持 / 退场 / 字形样式（treat）四类部件
  pv/pv_engine.js        镜头调度、相机、纸底、装饰、人物层、合成
  pv_fonts/              自托管字体（OFL，5 族 / 382 分片 / 7.1 MB）

tools/
  pv_prepare.py          一条命令：音乐分析 → 逐词轴 → 编排稿（主链内部调用）
  pv_direct.py           导演层：段落 + 歌词 → storyboard.json（可人工编辑）
  pv_timing.py           LRC 行级 → 逐词/逐字时间轴（起音吸附）
  pv_build.py            样片出片（编排稿 → job → 逐帧渲染 → 封音轨）
  pv_determinism.py      成片级「重复渲染一致性」验收
  pv_stills.cjs          抽帧预览（不编码视频）
  pv_fetch_fonts.py      字体拉取与子集化
```

数据流：

```
MP3 ──analyze_music──► analysis.json ─┐
LRC ──pv_timing──────► timing.json  ──┼─► pv_direct ──► storyboard.json ──► PV 页逐帧渲染
                                       │                                        │
segments/cut 信息 ─────────────────────┘                                   ffmpeg 封装
```

**编排稿是唯一需要人看/改的东西**（`cuts/<date>/pv/<歌名>.storyboard.json`）。
每个镜头一条，字段直白：`layout / type / enter / hold / exit / treat / decor / fig / pal / sync`。
改完把 `--player-page index_pv.html` 再跑一次就是新版本，随机量走 `meta.seed`，可复现。

---

## 2. 视觉规则（为什么不是随便撒特效）

| 层 | 做法 |
|---|---|
| **档位** | 按段落能量分 5 档（tier 0~4）。档位只决定「可选范围」：tier0 只准用细字/大留白/慢入场，tier4 才准用超粗字/裁切/抖动/色差 |
| **一档一级** | 每句只取 1 layout + 1 enter + 1 hold + 1 exit + 1 treat。档与档靠**反差**制造节奏，不靠单句堆效果 |
| **字体阶梯** | 档越高字重越粗、字距越紧、字号越大。这条阶梯全局一致，观众能感到「又上来了」 |
| **重复句同处理** | 副歌同一句反复出现时复用第一次的编排（`unify`），否则结构会散 |
| **克制** | 全片只一处色彩反转（最高能段），只在少数句子开抖动/色差/放射线 —— 是「事件」，不是背景噪声 |
| **可读性** | 长句自动折两行；窄字（I/l/1）描边后补实心，避免「豆腐块」；构图按可用面积反推字号，再经安全区兜底 |

---

## 3. 引擎里踩过的坑（改代码前必读）

1. **确定性六重根因**：字体必须在预排**之前** warm；大面积渐变必须烘焙成贴图；
   每帧必须先铺底（canvas 从不清空，相机 `zoom<1` 时会露出上一帧的边缘）；
   `PV.grain()` 要返回 canvas 而不是 pattern；pattern 缓存必须与 ctx 绑定；首帧栅格要预热。
   → 任一条破掉，同一 t 连渲三次会得到三个不同 md5。
2. **位置参数归一化**：`frac/xOf/yOf/cyOf` 里 ≤1.5 视作比例、否则像素。导演层写比例、
   排版层当像素用 ⇒ 字会被塞进画面左上角 1px 内。
3. **构图必须自己适配面积**：`justify` 对超长句会算出负字距（字压成一团）；
   `stack` / `split` / `diagonal` / `band` 同理会溢出。统一做法是按可用宽高**反推字号**。
4. **逐字错相只能有一个来源**：`g.t0` 来自逐词轴（已按唱点错开），再叠加 `enter.stagger`
   就是双重计时 —— 34 字的行要多等 1.5s 才铺满，而整句只有 3.6s，结果「唱完了字还没出齐」。
5. **`hold` 签名是 `(g,t,ph,spec,amp)`**：传错位会让 `jitter` 算出 NaN，
   整面墙一个字都不画、且**零报错**。
6. **`clip` 归一化到字形视觉框**（`TOP = by - size*1.0`, `BH = size*1.26`）。
   早先按「基线 ± size」解释，`sliceUp` 只露出基线以下的降部 ⇒ 整行字变成几条细线。
7. **逐镜头调色板要传到纸色**：`drawPaper` 读 `shot.pal || ST.sb.palette`，
   否则反转段只有字变色、纸面还是黑的。

---

## 4. 歌词时间轴的可信度（重要）

- **行级时间来自 LRC**（对齐链已校正），可信。
- **词级时间是估计值**：按音节权重分配 + 向 ±0.18s 内的起音峰吸附，并强制单调 + 最小驻留
  0.09s。实测吸附位移中位数 0.094s。
- 拿不准的句子**必须人工校正**，入口是编排稿：
  ```jsonc
  { "i": 5, "t0": 23.31, "t1": 29.46,
    "line": 4,            // 对应 timing.json 的 lines[4]
    "text": "…",          // 缺省沿用 timing 的歌词
    "sync": false,        // true = 整行同时出现，放弃逐字
    "layout": { "k": "justify", "cy": 0.52 }
  }
  ```
  想整体提前/延后，改 `pv_timing.py --lead <秒>`；想动单行，直接在 `timing.json` 里改
  `lines[i].words[*].t0/t1`，再 `pv_prepare.py --reuse` 之外的步骤重跑。

---

## 5. 验收方法与实测数字

```bash
# 单帧确定性（页面内部三连渲比字节）
node tools/pv_stills.cjs --sb _tmp/pv/sb66.json --times 24.4

# 成片级确定性（同一条 job 渲两遍，比视频流 md5）
python tools/pv_determinism.py --a _tmp/pv/x.mp4 --job _tmp/pv/job_x.json

# 覆盖度扫描（每镜 3 个时刻，抓「空白镜 / 过满镜」）
node _tmp/pv/scan.cjs
```

实测（Innocence，66s / 1080p60 / 12 Mbps）：

| 项 | 结果 |
|---|---|
| 成片 | 3960 帧 / 66.00s / 96.2 MB / 11.97 Mbps |
| 单帧 8 个时刻三连渲 | md5 全一致 |
| 成片重复渲染 | 视频流 md5 一致（`b8ab2849…`） |
| 镜头数 / 字形数 | 14 / 841 |
| 覆盖度 | tier0~2 段 0.5~6%，tier3~4 段 20~88%（爆发段故意压满） |

---

## 6. 开源方案的实测结论

| 项目 | 能不能用 | 结论 |
|---|---|---|
| **JIZURA**（`852wa/JIZURA`，v0.9.0，38MB） | 引擎可跑，风格不可控 | 把文字 PV 拆成 **10 组 / 860 部件 / 27 样式**（`layout / enter / hold / exit / decor / treat / bg / cam / fx / trans`），每 cut 独立选一件。自动分镜质量随随机波动（`jz_20` 读作 PPT）。**借鉴其分层与具体手法**（见第 9 节），引擎自建。bundle 留在 `renderer/pv/vendor/`（已 gitignore），可作可选通道 |
| **Vocaloid-Style MV Pipeline** | 是 skill / 模板，不是引擎 | `jizura_adapter.js` + `06-jizura.md` 有参考价值（把编排拆成可配置步骤的思路）。代码未复用 |
| **Phrasync** | 不可复用 | 依赖形态与本项目差异过大 |

**没有为了用开源而重构现有系统**：现有切点/音频/歌词/时间轴/编码链全部沿用，只在
「播放器页面」这一层插入 PV 页。

---

## 7. 仍需人工处理

1. **词级时间轴是估计值**，见第 4 节。逐字出现越密集的歌（快歌、连唱）越需要抽查。
2. **编排稿的审美判断**：档位和部件是规则选出来的，允许出现「技术上没问题但不好看」的镜头。
   改 `storyboard.json` 里对应镜头即可，不必改代码。
3. **人物层素材**：主链默认不抽（`tools/prepare_live.py` 单独跑，整首约 280 MB / 1~2 min）。
   要上人物层时把 `live.json` 传给 `pv_prepare.py --live`。
4. **竖排（`column`）只对 CJK 有效**，拉丁文歌会退到其它构图。
5. **`megaCrop` 视线裁切**：歌词会被切到只剩中间一条，全片只该出现 1~2 次，
   放多了会被读作「画面坏了」。

---

## 8. 视觉依据：这些选择是从哪来的（2026-10-28 补）

### 8.1 日本 PV 的排版语法（联网核实）

| 手法 | 在本引擎里的落点 |
|---|---|
| **細明朝 × 極太ゴシック 的对撞** | `didone / didoneLt`（Bodoni Moda）+ `heavy / grotesk`（Dela Gothic One / Archivo Black）。一档之内只允许一种语气，跨档才换族 |
| **明朝体压到 70~85% 当显示体** | `squeeze`（tier3 0.90 / tier4 0.86），纵向压扁让衬线在小字号也读得出版面感 |
| **大小混植（一个大词、其余半号）** | `L.mixed`：大词取强调词或最长词，其余 `size×(0.66-0.02×tier)`，`gapEm 0.30` |
| **縦組み（竖排）** | `L.column`（CJK 专用）+ `BG.mega` 的 `vert` 巨型竖排底字 |
| **留白（ma）** | 版心 `pad = W×0.085`、安全区 `76px`、`fitShot` 只缩不撑 |
| **袋文字（二重縁）** | `T.bag` —— **只在浅底/反转镜头用**，深底会留暗缝（见 RULES §52） |
| **硬投影（非模糊）** | `T.shadowSolid`：整字错位一层 `void` 色，无 `shadowBlur` |
| **网点 / 排线 / 网格** | `pv_bg.js` 的 `halftone / hatch / grid`，一次性烘焙成 1024² sprite 再拉伸 |
| **被画框切掉的人** | `placeFigure` 的 `crop` 模式（半边出画的竖幅） |
| **极淡的巨大影子** | `ghost` 模式 + `BG.mega`（巨型底字） |
| **反面清单（一律不做）** | 荧光笔块、模糊阴影、逐帧渐变、色差故障、频谱条 |

### 8.2 配色为什么换掉

旧版 `#F2F6FA` 冷白 + `#FFB067` 橙，是「科技发布会」配色：饱和度高、色相夹角小、
在深底上读作 UI 而不是印刷品。现在四色是 墨 `#12171E` / 生成 `#F4EEE1` /
朱 `#E2604A` / 藍 `#5E90B6` —— 前三个构成日本印刷品最常见的「墨・生成・朱」三色，
藍只做冷调呼应（`leak`、`ghostA`）。

### 8.3 「字体廉价」的判据

不是字体选得丑，是三件事叠在一起：

1. **空心字**（`outline`，0.017em 描边）—— 4K 下 2.5px 一圈线，字腔全空。
   已从主体样式池移除，空心只留背景层。
2. **深底暗缝** —— 袋文字/反色描边用纸色描内圈，而纸色就是底色，等于把笔画啃细。
   深底改「同色加粗 + 硬投影」。
3. **字被自己压住** —— 满宽字块与人像圆窗抢版面，字的末词被圆窗吃掉。
   人进画面 ⇒ 字收 78% 宽、版心收到 60%（RULES §52）。

### 8.4 背景为什么不再死黑

根因不是配色不够亮，而是**每层 alpha 被平方了**（`a * SAFE_A(a)`，0.16→0.026），
九层叠起来不到 1/255。修掉之后同一套参数立刻有纸感；随后才谈得上加层：
`mega`（巨型底字，给画面一个「沉默的声部」）、`edge`（版式线框，让留白读作留白）。

---

## 9. 对照 JIZURA：抄了什么、为什么、没抄什么（2026-10-28）

参考实现：<https://github.com/852wa/JIZURA/>（`src/01_util.js` 缓动与确定性随机、
`src/05_anim.js` enter/hold/exit 回调、`src/09_render.js` 双帧合成与相机、
`src/11p_bgcamB.js` 12 相机 + 44 背景、`src/11p_treattrans.js` 20 过渡）。

### 9.1 架构层面对齐的三件事

| JIZURA 的做法 | 我们原来的状态 | 现在 |
|---|---|---|
| 每个 cut 从 **`J.register(group,key,def,pack)`** 里各选一件（10 组独立） | 有 layout/enter/hold/exit/treat/bg/decor/post，**没有 cam** | 补齐 cam（`pv_cam.js`），并给 cam 加了 tier 池 + 相邻去重 + 全局配额 |
| 部件用**回调**（`it.charFns` / `pieceFns`）拿 `(g, p, i)` 逐字/逐片算 | 已是回调（`PV.ENTER/HOLD/EXIT`） | 不变，只加 `rowClamp` 兜住「跨行掉字」 |
| `J.E` 缓动库（`outExpo/outBack/outElastic/inOutExpo`） | 有 outCubic/outQuint/outBack | 新增 `outExpo` 用于 `assemble`，`bounceAt` 用于 `dropBounce` |

### 9.2 抄了的手法（都在用）

| 手法 | JIZURA 出处 | 我们的落地 |
|---|---|---|
| `assemble`：散片飞入 + **`outExpo` 末段急停** | `J.ENTER.assemble`（`spread = size*3.2`） | `E.assemble`（`dist = size*2.4`，随机角度 + 轻旋） |
| 每镜慢推 `1.00 → 1.03` | `J.CAMERA.push` 默认行为 | `C.push`：`1.002 → 1.04×amp`，实测 push 类 s 从 1.0028 到 1.017 |
| 相机噪声漂移 | `J.noise1` / `floatNoise` | `C.float` / `C.drift`（一维平滑值噪声，seed 由 `shot.i` 决定） |
| 逐片爆散退场 | `J.EXIT.explode/fall/drift` | 已有 `PV.EXIT.scatter/fall/blurOut`，本轮未改（够用） |
| 双帧合成过渡 | `J.TRANS[cut.trans].draw(ctx,A,B,p)` | 我们用**更便宜的一条**：逐帧渲染时画布上本来就有上一镜 → 退场前存 `XC` 快照，下一镜前 0.22s 擦掉。见 9.3 |

### 9.3 没抄的，以及理由

| JIZURA 有 | 我们的选择 | 理由 |
|---|---|---|
| **20 种 trans**（`cubeTurn` / `shatterTiles` / `inkBlob` / `pixelate` …） | 只用「左右延迟擦除 + accent 亮线」 | 它每镜都走 `TRANS`，A 帧要**重渲一遍上一镜**再合成；我们是顺序渲染 13931 帧，成本翻倍。且抽帧实测：四方向 + 0.32s 的版本会把片头标题**拦腰横切**、A/B 交错读作「画面没刷新」。收敛到左右各一 + 0.22s + `1-(1-q)^2.2` 之后，切镜处的帧差尖峰从 16.8 降到 4.0/5.6 |
| **44 种背景** | 加了 `topo`（等高线自转）与 `rays`（光芒摆动）两种，按 tier 概率接入（tier≤1 0.62 / tier2 0.55 / tier3 0.45） | 背景层已有 12 种 + 烘焙场，再多会变成「每镜都在换滤镜」。日本 PV 的层次感来自**同一底子上的缓慢差异**，不是每镜换背景 |
| **18 族日文字体，全量预载** | 只加载当前编排**实际用到的**（`PV.FACES` + `warmFonts(实际歌词)`） | 全量预载在 4K 长片上是几十秒的空白启动；而且字体是分 unicode-range 的，英文歌请求日文分片纯属浪费（RULES §56 的误报就是这么来的） |
| `J.rng` mulberry32 + 运行期随机 | 全部随机量在**排版期**一次算定（`PV.rng(seed, shot.i, k)`），渲染期零随机 | 交付要求「同一 t 两次渲染字节一致、同一 job 渲两遍 md5 相同」。运行期 RNG 会破坏这条 |

### 9.4 量化对照（本轮实测）

| 指标 | 改前 | 改后 |
|---|---|---|
| 镜头内有运动的镜头 | 0 / 48（只有常量 zoom/shake） | **48 / 48**（最小位移 0.0142W，0 个露边） |
| 相机手法种类 | 1（常量缩放） | 10，实际分布 `orbit13 / float11 / push10 / pendulum5 / focusIn4 / tiltDown2 / snapPan2 / jelly1` |
| 入场手法种类 | 1 主导演（`slam` 23/48 = 48%） | 11，`slam` 降到 8/48（17%） |
| 切镜处帧差尖峰 | 16.8 | 4.0 ~ 5.6（14s 样片，逐帧均值差） |
| 帧间差异 p95 | 3.70 | 4.17（更「活」，但无突变） |
