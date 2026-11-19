# 本地透明 PNG 立绘能否直接进管线 —— 规范核对与实测

> 2026-11-15 05:00 · 结论：**可行，且官方文档明确把"AI 生成"与"外部产出"视为同一路径**。
> 但有三处必须手工覆盖的参数、一个官方工具链的缺口、一个会静默失败的场景。

---

## 1. 官方依据：这条路径是否被承认

上游 `references/04-asset-generation.md` 开头就定了调：

> The method is **model-agnostic**: one master reference image + a verbatim character sentence +
> a verbatim style block for every pose, transparent output via native alpha or a flat chroma-key
> background.

而 §9「Other image models」把这件事说死了：

> Anything that can (a) take a reference image and (b) produce either RGBA or a flat key-colour
> background works: replace the `run_job()` call in `imagegen.py` (**or generate by hand**), keep
> the jobs file, and run `alpha_matte.py key/trim` on the results.

**判定标准只有两条**：(a) 能出 RGBA 或纯色键控底；(b) 落到 `assets/char/*.png`。
"谁画的、用什么画的"不进契约。§7「File layout」也只规定了一件事：

> `assets/char/*.png` (RGBA, trimmed), `assets/char/alt/`, `assets/bg/*.png` (opaque)

引擎侧同样不看来源。`engine/scenes/scenes.js:70` 的 `drawChar` 第一行是：

```js
const img = Z.imgSync(c.img); if (!img) return;
```

`c.img` 就是一个路径字符串，`Z.imgSync` 是浏览器 `new Image()` 解码。**没有任何格式探测、没有元数据、没有"生成来源"字段**。
所以"本地已有 PNG"与"AI 生成 PNG"在引擎眼里是同一个东西——这不是我们绕过规范，规范本来就是这么设计的。

### 与官方唯一的表述差异

官方通篇没有"立绘"这个词，用的是 `character` / `cut-out` / `standee`。角色素材在官方语境里叫
`assets/char/*.png` 的 **pose**（姿势库），一个角色是 `REF_master.png` + N 个 pose。
我们用"立绘"指代的正是同一批文件。术语差异不构成路径差异。

---

## 2. 输入格式：实测这两张立绘的符合度

用官方 `tools/alpha_matte.py check`（只读）跑本地两张：

| 项目 | 规范要求 | `natianxiayule_umbrella.png` | `natianxiayule_reach.png` | 判定 |
|---|---|---|---|---|
| 色彩模式 | RGBA | RGBA | RGBA | ✅ |
| 尺寸 | 无硬性要求（残光实测 1024×1536，trim 后 ≈800×1500） | 1024×1536 | 1024×1536 | ✅ 同规格 |
| 透明区占比 | 应显著 >0 | 37.9% | 36.6% | ✅ |
| 绿色溢出 | 0（有绿边就废） | **0.0%** | **0.0%** | ✅ |
| 主体连通性 | 单一主体 | largest 100.0%，33 域 | largest 99.99%，57 域 | ✅ 杂点均为发丝/伞骨 |
| 是否贴边 | `touches_edge` | true（底边 B=0） | true（底边 B=0） | ⚠️ 见 §5.1 |
| alpha 上限 | 255（官方要求 normalize） | **254**，均值 0.989 | **254**，均值 0.989 | ⚠️ 见 §5.2 |
| 边缘高光比例 | 越低越好 | 41.1% | 38.3% | ⚠️ 见 §5.3 |

### §5.3 那条为什么判"通过"

`edge_bright_halo_pct` 41% 看着刺眼，但官方 `preview` 合成到黑底 / 白底 / 橙底三块上是干净的：
**没有白边、没有绿边、没有矩形框**。原因是这张图主体本身就是白发+透明伞，边缘天然高亮，
该指标按"边缘亮度"统计，对白衣角色会系统性误报。**这个指标不能单独当门禁**——和上一轮
"亮度/标准差找死气帧"是同一类陷阱，必须配合 `preview` 肉眼判定。

---

## 3. 文件组织

```
official_pv/
└── assets/char/
    ├── natianxiayule_umbrella.png      ← 已在用（6 个窗口里 3 个）
    └── natianxiayule_reach.png         ← 已在用（另 3 个）
```

命名 `assets/char/<songid>_<pose>.png` 是本项目约定（官方是 `<pose>.png`，单曲单角色，
所以不带歌名；我们是多首歌共用一个引擎目录，必须带前缀防撞）。

**preload 是自动的**：`illust` 的 `preload: a => assetsOf(a)`（`scenes.js:32`）会把
`args.chars[].img` 全部收进来，在**第 0 帧之前**解码完。所以只要图写进 args，就不会出现
"首帧还没加载好"的空窗。`kanjiSlam` 同理（`typo.js:389` 走 `slamCfg(a).char`）。

---

## 4. 调用流程：三处入口

### 4.1 `illust`（主力，最安全）

`05-engine.md:146` 的 char spec，我们实际用到的子集：

```js
{ img: 'assets/char/natianxiayule_umbrella.png',
  x: 1400, y: 1010, h: 900,       // y 是"脚底线"，anchor 默认 [0.5, 1] = 底边中点
  q: 12,                          // 手绘化：动作锁 12 fps
  sway: 0.012, breathe: 1,        // 微呼吸/摆动
  wave: { amp: 10, from: 0.3, q: 12 },   // 发丝横切条 warp
  rim: { color: '#FFA552' },      // 轮廓光
  sil: '#140B1E',                 // 剪影（给了就不画原图）
  shadow: { dx: 120, dy: -6, color: '#140B1E', a: 0.55 },
  in:  { dur: 0.7, dx: 40 },
  out: { at: 8.0, dur: 0.4, dx: 30 } }
```

### 4.2 `kanjiSlam`（**必须覆盖四件套**）

`typo.js:245` 的 `SLAM.default/night/hand` 三个 preset 把 `char / charX / charY / charH / face`
全写成了残光的图名与取值。传本地立绘必须**同时**给这五项，否则取景会跑到画面外。

### 4.3 `afterimage` / `panels` / `burst` 的 `char`

`afterimage` 直接吃 `char` 路径（`meta.js` 内部转成 drawChar spec），`panels`/`burst` 吃
`panels[].char` / `cuts[].char` + `charX/Y/H`。这三个都正常，无需额外覆盖。

---

## 5. 实测结果（真跑，不是读代码）

### 5.1 五种形态（`render/_verify_char.mjs`，已删）

同一张图、同一位置，只改一个衍生参数，各渲一帧 1080p：

| 形态 | 结果 | 画面 |
|---|---|---|
| `plain` | 成功 | 干净合成，无白边无框 |
| `sil: '#140B1E'` | 成功 | 轮廓精确跟随人物外形，**无矩形** |
| `rim: {color:'#FFA552'}` | 成功 | 橙光沿伞骨/肩/臂走，不过曝 |
| `shadow: {dx:120}` | 成功 | 投影沿人物外形，底部平切（见 §6.1） |
| `wave: false` | 成功 | 关掉发丝 warp，画面稳定 |

对比图：`docs/img_portrait_modes.jpg`（左上 plain / 右上 sil / 左下 rim / 右下 shadow）

### 5.2 硬编码坐标的四个场景（`render/_verify_slam.mjs`，已删）

| 调用 | 结果 | 关键信息 |
|---|---|---|
| `kanjiSlam` 覆盖 `char/charX/charY/charH/face` | 成功 | 巨型字 + 立绘构图成立 |
| `kanjiSlam` **不**覆盖（走 default preset） | **成功但画面上没有人** | preload 报 `profile_sing.png [MISSING]`，`drawImage` 静默跳过 |
| `afterimage` + `char` | 成功 | 结尾卡正常 |
| `panels[{char}]` | 成功 | 分格正常 |

`page error` 全程为零。

---

## 6. 限制（按实际影响排序）

### 6.1 无腿立绘不能用 `anchor` 解决"站位"，必须给地面

`anchor [0.5,1]` 在无腿图上没有意义：图的下沿就是裙摆切线，`y` 是"裙摆落在哪"。
上一轮用 `stage.ground`（前景水洼/地面遮挡）把它压住才是对的。**这不是缺陷，是这类素材的固有属性**：
任何"半身/无腿"素材都必须靠画面元素遮挡断口，否则一定像腰斩。

副作用：`shadow`（投影）会跟着变成"半截平切块"。用 shadow 时要确保断口被 ground 盖住，或者干脆不用。

### 6.2 alpha 254 → 亮背景上人物暗部会被提亮约 1.96%

实测：`alpha` 均值 0.989（253/255），最坏 250/255。

| 叠在什么底上 | 身体像素色偏 |
|---|---|
| 纯黑 | 0.00%（乘 0，无影响） |
| 纯白 | 最坏 5/255 = **1.96%**，平均 2.9/255 |

这就是官方 `normalize_alpha` 要修的东西（`04 §4`：*native model alpha often tops out at 252–254
… 1 % see-through: backgrounds ghost through the character*）。

**但官方工具链没有给外部素材留入口**：`alpha_matte.py` 的 CLI 只有 `check / key / trim / preview / tile`，
**没有 `normalize` 子命令**——`normalize_alpha()` 是 `imagegen.py` 在"自己刚生成完"时内部调用的。
已有 PNG 想 normalize 只能自己写五行（rescale 到 p99.5→255、alpha≤3 归零、颜色向透明区外溢）。

**我的建议：暂不处理。** 我们这条片子的立绘全部落在深色雨夜 plate 上（黑底色偏 0.00%），
且 LUT+bloom 之前先经过 `overlayGrad` 压暗。等到哪天要在高调日光片上放立绘，再补 normalize。

### 6.3 `kanjiSlam` 不覆盖参数 = 静默无人物（最容易踩）

这不是格式问题，是场景默认值问题，但它**恰好只在换素材时爆炸**：
`SLAM.default` 指向 `assets/char/profile_sing.png`，我们目录里没有 → `Z.imgSync` 返回 null →
`drawChar` 第一行 `if (!img) return` → 画面右侧一大片空天空，**零报错、零 warn**。
和上一轮 `ref: no shot undefined` 是同一类事故。

**对策**：进 `songs/<id>.json` 的 `lexicon.scenes` 时，`kanjiSlam` 一律写全五件套。
要不要给 `drawChar` 加一条"图缺失就画红框报错"，等你决定（我倾向加，但它属于改官方文件）。

### 6.4 `assets/char/` 是单曲单角色假设

官方 `REF_master.png` 是"一个角色，多个 pose"，文件名不带歌名。
我们是多首共用一个 `assets/char/`，所以必须 `<songid>_<pose>.png`。若要换歌，别复用同名文件——
`D.silhouette` / `D.rim` 用 `'sil'+c.img` / `'rim'+c.img+color` 做缓存 key，同名换内容会取到上一首的缓存。

### 6.5 每张图的取景参数都要重新量

`face [fx, fy]`（`kanjiSlam` 的取景中心、`D.vtext` 的避让锚点）是**按图量的**：
`[0.745, 0.235]` 意思是"脸在图的宽度 74.5%、高度 23.5% 处"。换一张图不重量，字就会压到脸上。
量法：`assets/char/*.png` 打开，读脸部中心像素坐标 ÷ 图宽高。

### 6.6 官方其它硬编码 landmarks 场景不可直接换图

`05-engine.md:248` 列了一批把**图像像素坐标写死**的场景：`dark.js` 的 `HR`（hand_reach 枢轴/飘带）、
`FALL`（fall 枢轴）、`FLAME_AT`；`glass.js` 跪姿锚点；`night.js` 的 `WET / LAMPS`；
`memory.js` 的 turn_smile 脸位。这些是残光那套图的量测结果，**换我们的立绘会错位**。
本次不用它们就行（当前 `lexicon` 没有引用），要用必须先重量并覆盖。

---

## 7. 结论

**这条路径成立，不需要任何引擎改动。**

- 规范层面：`04 §9` 明文 model-agnostic，"generate by hand" 是被点名的合法产出方式；
- 格式层面：两张立绘是 1024×1536 RGBA、零绿边、单一主体，与官方 `assets/char/` 同规格；
- 引擎层面：`illust` 五种形态 + `kanjiSlam` + `afterimage` + `panels` 全部实拍通过，零 page error；
- 唯一真实成本是**每张图要重量 `face` 与 `charX/Y/H`**，以及**无腿图必须有地面遮挡**。

下一步等你指定用哪几张透明立绘、各用在哪首歌的哪几句歌词，我再按 §4 的格式落配置。
