# JIZURA 逆向研究报告

> 目标：弄清「我们复刻的文字 PV」与 GitHub 预览版差距的**根因**，并提炼可直接迁移的机制。
> 对象：`852wa/JIZURA` @ `fc16bfe`（本地完整克隆在 `_tmp/JIZURA`，含 `src/` 源码而非仅有构建产物）
> 结论一句话：**差距不是参数没调对，而是我们复刻的是"被我们自己抽象过的二手版式"，官方是一套 862 部件的排列组合引擎。**

---

## 0. 先搞清楚：预览版到底是什么

| 事实 | 说明 |
|---|---|
| 预览版 = `index.html`（2.4 MB） | GitHub Pages 打开的就是它，`build.py` 把 `src/*.js` 按**文件名排序**拼接 + `app/` + `vendor/mp4-muxer` 生成 |
| 源码完全公开 | `src/` 共 36,928 行 / 44 个文件；`ae/` 是同一套经验的 After Effects 版；CEP/AE 的 `.jsx` 只是**另一条产物线** |
| 我们之前看的是什么 | 主要是 `JIZURA_AE.jsx`（AE 版）+ README → 相当于**照着产品说明书反向猜实现**，得到 13 个版面 |

**实测注册表（Node 加载真实 `src/` 统计，非估算）：**

```
styles: 27
layout 186 | enter 125 | hold 52 | exit 109 | decor 130
treat   62 | bg    66 | cam  36 | fx   69 | trans 27
== TOTAL parts: 862        packs: 18
```

正好对应 README 宣称的「860 の小さな部品（と 27 のスタイル）」。
→ **我们做了 13 个版面 vs 官方 184 个可抽选版面**，这还没算入场/退场/修饰的组合维度。

---

## 1. 根因：五个维度的落差

| # | 维度 | 官方 | 我们的实现 | 影响 |
|---|---|---|---|---|
| 1 | 部件库 | 862 部件 / 27 风格 | 13 版面 / 5 风格 | 组合空间差若干个数量级 |
| 2 | **字形拆解** | 汉字栅格化后做**连通域标记**，拆成多个 piece，每个独立飞散/碎裂 | 整 `fillText` 一行，字是整体 | 少了「字崩成部首再重组」的全部表现 |
| 3 | **色散残影** | B/A/main **三趟渲染**，各自 lag 1.6/24、0.8/24 秒错帧 | 无 lag 机制 | 没有原生 RGB 错位那个"脏"味 |
| 4 | **两拍格**（有限动画） | `koma=12`（24fps 时间基，每秒只画 12 张） | 60fps 全帧重算 | **我们的画面"太顺"**，缺日本 PV 的抽帧感 ← 最大的观感杀手 |
| 5 | 字体 | 24 种 Google Fonts 动态加载，含可变字重 100–900 | 本地字体 + 单一字重 | 缺 `weightGrow`（字在镜头里由细变粗） |

补充一条隐性判据：官方的随机**不均匀**。随机数要乘以 `style.bias`（每个流派对版面/入场/退场的权重）+ `mood.tags`（glitch/calm/pop/graphic/editorial/emotional/horror 七种情绪标定）双重加权，所以**每种风格有稳定人格**。我们是匀概率抽，导致"看起来每种风格都差不多"。

---

## 2. 渲染管线（每帧做了什么）

`09_render.js` 的 `frame()` 是全片唯一的 draw 入口，顺序严格如下：

```
t → tq = floor(t/stepDur)·stepDur        # ① 时间量子化：两拍格
  → 背景：平涂 + radial 提亮 + paper 纹理(按 bg 亮度选 multiply/screen)
  → events/beats 解算 spike / shake / beatPulse
  → BG 层背景图形（66 种）
  → 三趟 pass：  B(lag 1.6/24) → A(lag 0.8/24) → main(0)
       每趟内：camera(36 种) → drawCut = decor(back) → layout → decor(front)
  → morph：与上一镜的同字符做 transform 插值 glide
  → trans：上一镜 A 帧 + 本镜 B 帧 合成（27 种）
  → HUD
  → post：FX 事件 → bloom → scanline → grain → vignette
```

三个值得抄的细节：

- **相机模糊只做一次**：`camera.blur` 不开在逐字 `filter` 上，而是整层渲到离屏 canvas 后 blur 一次回贴。逐字 blur 在 3 趟色散下会慢到不可行。同样的思路也用在文字（见 §3 的 `drawItemLayered`）。
- **alphaGuard**：导出透明通道时，会猴补丁 `ctx.fillRect/drawImage`，把整帧不透明填充识别成"清空"，最后 `destination-in` 把 alpha 限制回"有内容的地方 + 特效重画的地方"，保证泛光/闪白只染色文字不糊背景。
- **paper 纹理是算出来的**：半分辨率生成棉纸底噪（900 条纤维 + 1400 个斑点，双层 ImageData），不是贴图；且同尺寸缓存复用。

---

## 3. 一个「镜」是怎么被组合出来的

`plan.cuts[i]` 是一个纯数据对象，每帧按它去注册表取实现：

```js
{ line, text, dur, inDur, outDur,
  layout, enter, hold, exit, treat,   // 五个互斥维度
  decor[], bg, cam, trans,            // 附加层
  scheme, seed, zone, morph, weightGrow }
```

理论组合空间 ≈ `186 × 125 × 52 × 109 × 62 × 66 × 36 × 27 × (decor 多选) × 27 风格 × 2~4 配色`。

### 文字的五级加工链

```
① typesetLine  文字整列
     假名 adv 0.90 / 拗音 0.86 / ー 0.94
     助词 f=0.78（は が を に で と の へ も，含前后字类判定）
     首字 f=1.18（头字放大）  拉丁 f=1.08  和日交界 gap +0.2em
② layoutText   逐字盒子；支持竖排 / r90 旋转 / 标点位移
③ TREAT 62 种   outline / extrude / marker…（改 item 属性）
④ charFn / pieceFn  由 enter·hold·exit 推入
     combineChar / combinePiece 累加：dx dy rot s sx sy a color skew blur clipXY hide
⑤ drawItem    离屏层(blur) → shadow → extrude → dash(虚线画字)
              → gradient / pattern(dots|stripes|hatch|grid|lines)
              → fill 与 stroke 分离 ⇒ **空心字是一个参数不是一种版面**
```

### 动画三段（关键设计：`hold` 不与 `enter` 打架）

```js
const amt = J.clamp((ltI - cut.inDur * 0.85) / 0.25) * (1 - pOut);
if (amt > 0 && !it.noHold) ho.apply(env, it, amt, ctx);   // 入场走完 85% 后才介入
```

- ENTER 125：cut / assemble(**部件飞入**) / slice / type / pop / drop / stretch / wipe / blur / spin / flicker / scramble / zoom…
- HOLD 52：still / jitter / drift / breathe / wave / glitchtick
- EXIT 109：explode(**碎裂**) / fall / drift / slice / wipe / shrink / blur / stretch / scatter / glitch

带 `pieces:true` / `shatter:true` 的手法则绕到第 ② 层：`drawPieces` 直接操作字形 piece，`assemble`、`explode`、`fall`、`drift` 都属于这一类。

---

## 4. 两个我们之前完全没注意、但直接可用的官方功能

### ① `keyBg = 'black' | 'green'` —— 官方合成通道

README 原话：背景可以设为绿幕/黑幕（只有白字和演出），**作为合成素材导出**。
代码路径：`resolveStyle → keyStyle` 把所有配色强制成白-on-黑、清零纹理；渲染末 `keyFinish()` 用 `globalCompositeOperation='saturation'` + 灰填充抽干颜色，green 模式再 screen 上纯绿。

> 也就是说：**作者本来就把 JIZURA 当成"给剪辑软件供素材"的前端**，而不是成片工具。

### ② `centerFree`（中央を空ける）—— 给人物留位置

比"避让"高一层的做法：**先把每句唱词对半拆开**，两个半句各自排进一条侧带，中间自然空出来。

```js
// 08_planner.js — splitCut
cut.text  = halves[0];                       // 「花が」 →  band 0（左/上）
twin.text = halves[1];                       // 「咲いた」 → band 1（右/下）
cut.zone = zones[0];  twin.zone = zones[1];
const LW = Z ? Z.w : W, LH = Z ? Z.h : H;    // 版面在缩小的框里重新适配
```

每个 band 是一个**完整独立的排版框**（不是简单把画面裁掉一块），渲染时用 `clip + translate` 落到 canvas 上，两半通过 `cut.companion` 成对绘制。

带宽实测值（`J.sideZones`）：

| 构图 | 分带方式 | 单侧 | **中间留给人物** |
|---|---|---|---|
| 横构图 | 左右 | 36% W | **28% W** |
| 横构图 > 2:1 | 左右 | 30% W | 40% W |
| 竖构图（默认 `tb`） | 上下 | 33% H | 34% H |
| 竖构图 `lr` | 左右 | 34% W | 32% W |

→ 这正是我们「直播画面人物层 + 文字穿插」的需求，官方原生支持，而我们是在外部手搓避让（`pv_direct.py` 的 `FIG_SAFE` / `availW`）。
→ 顺带得到一个可参考的量化基准：**横版中缝 28%**，比我们当前人物层占比更克制。

---

## 5. 其它可学习的工程手法

| 手法 | 位置 | 要点 |
|---|---|---|
| 确定性随机 | `01_util.js` | `J.h(a..e)` 五键哈希 + `J.rng` mulberry32；**同 seed 同 t 两次渲染逐像素一致**（我们已有同一思路） |
| 两级缓存 | `02_fonts.js` | metrics（逐字符 em 宽度）、glyph（按 px 分桶 64→512）、pattern、paper 全部缓存 |
| 可变字重 | `03_text.js` | `weightGrow` 时换 variable Noto Sans/Serif JP，`100 + e*800` 动态字重，字在一个镜内由发丝粗长到黑体 |
| 自动对比度 | `01_util.js` | `fitContrast` 按 WCAG 比值把 accent 推到可读为止 → 换底色不会出瞎色 |
| 导出 | `11_export.js` | WebCodecs `VideoEncoder` + mp4-muxer，与我们同路；有 HW 编码器高码率拒绝的降级链；附 AVA/JSON/连番 PNG |

---

## 6. 结论与建议（按性价比排序）

**A. 放弃"重写 862 部件"，改为"接一段合成"（强烈推荐）**
用官方 `keyBg='black'` 导出白字+演出素材（可选 `centerFree` 直接留好人物中缝），再与本项目已有的直播画面/人物层 screen 合成。
→ 成本从"重实现一个 generative engine"降到"一条 ffmpeg / canvas 合成链"，且能拿到 100% 原版观感，因为它就是原版。

**B. 若坚持自绘，只需先补这六项就能拿到大部分味道**
1. `koma=12` 抽帧（24fps 时间基）——观感收益最大、改动最小
2. B/A/main 三趟色散 + lag 错帧
3. glyph 连通域拆解 → piece 级动画
4. typeset 文字整列（中文歌尤其：标点、拉丁混排间隙）
5. enter/hold/exit 三段解耦，hold 延后 `0.85×inDur` 介入
6. 可变字重 `weightGrow`

**C. 填充与描边分离**（`fill:false` + `stroke`）应当是参数而非版面——我们之前把它当成了一种独立版式，这是结构性浪费。

---

*报告版本：基于 commit `fc16bfe`；部件数由 Node 实际加载注册表统计，非估算。*
