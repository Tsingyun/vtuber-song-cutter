# codebase 审计报告 · official_pv

审计时间：2026-11-15
审计对象：`D:/岁己歌切/official_pv`（B 面 1080p30 歌切渲染管线）
审计范围：全部本地代码（`engine/` 自有文件、`tools/`、`cut.mjs`、`songs/` 配置），并与上游
`vocaloid-style-mv-pipeline`（clone HEAD `13b380d`）与其 skill 文档（`references/01–09`）逐项对照。
方法：静态通读 + 运行时探针（真实 GPU、真实页面、hook `Z.post.render` / `Z.SCENES[*].draw` /
`JZ.drawPlan` 分层计时）+ 成片抽帧核对。所有数字为本机实测（RTX 5070 Ti / ANGLE D3D11）。

---

## 0. 结论摘要

**审计口径下最严重的三件事**，都不在"代码风格"层面，而是"会直接毁掉成片"：

| # | 问题 | 影响 | 级别 |
|---|---|---|---|
| 1 | `ref` 场景 4 个镜头（16.58 s，占全片 **7.5%**）实际播出的是调试占位图 `ref: no shot undefined` | 成片硬伤，观众能看到英文报错 | **P0** |
| 2 | 审计 22/22 全 PASS，却放过了上面这 7.5% 的错误画面 | 整套"证明链"失去意义 | **P0** |
| 3 | `charParams` 的 `shadow` 形态引用未定义变量 `side`，一启用就 ReferenceError | 潜伏崩溃 | **P0** |

另外两项"对官方偏离"值得单列：单镜时长有 6 个超过官方 ~7 s 上限（8–12.5 s），以及
`docs/OFFICIAL_SOURCE_HASHES.json` 把两个**实际已修改**的官方文件标成了"官方原样"。

性能侧的结论和直觉相反：**画面绘制只占每帧 35%（3.1 ms），捕获+编码占 65%（5.7 ms）**，
所以继续抠 Canvas 绘制几乎没有收益，优化的杠杆在捕获链路和"首次命中尖峰"上。

---

## 1. P0 — 必须修复

### 1.1 `ref` 场景 4 个镜头播出调试报错卡（16.58 s / 7.5%）

**证据**

`engine/songcut.timeline.js:260`：

```js
case 'ref':
  return { args: { mode: C.mode('ref', ['melt','tear','burn']), meltAt: 0.3, meltEnd: 0.9,
                   cols: 14 + (i % 3) * 4, depth: 0.4, seed: 11 + i } };
```

`ref` 的契约（`references/05-engine.md` §8）是 **"re-renders another shot"**，必须带 `shot`（要复用的镜头
id）。这里从来没传，于是 `engine/scenes/meta.js:632` 走兜底：

```js
if (!Z.shotById(a.shot)) return missing(ctx, 'ref: no shot ' + a.shot);
```

`missing()`（`meta.js:573`）就是 `INK` 底 + 居中 28 px 的一行调试文字。成片实测：

| 镜头 | 时间 | 时长 | 画面上是什么 |
|---|---|---|---|
| s005 | 16.34 – 19.38 | 3.04 s | `ref: no shot undefined` |
| s010 | 41.23 – 44.87 | 3.63 s | 同上 |
| s025 | 91.32 – 97.00 | 5.68 s | 同上 |
| s028 | 112.50 – 116.73 | 4.23 s | 同上 |

合计 **16.58 s = 全片 7.5%**。抽帧存证：`render/_audit/ref_*.png`（4 张）。

**影响范围**：只有《那天下雨了》的 `direction.lexicon.scenes` 里出现了 `ref`（verse / pre 两个段落），
其余两首未用 `ref`，不受影响。

**修复建议（二选一，推荐 A）**

- **A. 真的让它复用镜头**：`sceneArgs` 里已经能拿到 `S`（此时前面的镜头都已 push），改成

  ```js
  case 'ref': {
    const prev = S[S.length - 1 - (i % 3)];            // 往回找 1–3 个已建镜头
    return { args: { shot: prev ? prev.id : null, mode: ..., meltAt: 0.3, ... } };
  }
  ```
  这才是官方 `ref` 的用法（freeze / melt / rewind 一个已存在的镜头），视觉上也比随机贴图更成立。
- **B. 从词表删掉 `ref`**：`songs/natianxiayule.json` 的 `lexicon.scenes.verse/pre` 移除 `"ref"`。
  最省事，但放弃了官方这一个"关于镜头的镜头"能力。

无论哪种，都建议在 `sceneArgs` 入口加一条断言：场景声明了 `required` 参数却没给 → 直接抛错，
而不是把兜底 UI 渲进成片。

---

### 1.2 审计无法发现"占位/空画面"，22 项检查形同虚设

**证据**：`render/audit_natianxiayule.json` 的 `checks` 全部 `pass: true`，其中包含

- `every shot renders without exception` — 57/57 帧
- `no page errors` — none

但同一份审计对应的成片有 7.5% 是报错卡。原因很直接：`tools/audit_official.mjs:146-184` 的 22 条
检查里，**没有一条检查"画出来的东西是什么"**。它只检查"没抛异常""场景注册了""用了几个"。

官方 `references/09-qa-and-lessons.md` §2 checklist 恰好有这一条：

> No dead air: no shot that is just black/grey without intent while the music plays.

**修复建议（精确且便宜）**：在 smoke 循环里把 `fillText` 包一层，记录所有被画出来的字符串：

```js
const drawnText = [];
const of = CanvasRenderingContext2D.prototype.fillText;
CanvasRenderingContext2D.prototype.fillText = function (s, ...a) { drawnText.push(String(s)); return of.call(this, s, ...a); };
// ... 跑完 57 个中点后
const junk = drawnText.filter(s => /no shot|missing scene|undefined|NaN|Infinity/.test(s));
checks.push(['no placeholder / debug text on screen', junk.length === 0, junk.slice(0, 6).join(' | ') || 'clean']);
```

再加一条数值兜底（防止别的沉默失败）：把每帧下采样到 32×18，统计均值与标准差，`std < 3` 且
`mean < 12` 超过 0.5 s 就报警（"这不是设计好的黑场就是坏帧"，人工复核一次即可）。
这两条加起来不到 30 行，能把这一类问题永久堵死。

> **本轮实测后修正**：上面这条数值方案**被否决**，换成结构性检查 `every shot reference resolves`。
> 原因与实测数据见 §7.2——合法镜头的亮度分布完全覆盖了占位卡，任何阈值都只能二选一。

**影响范围**：整个 `official_pv` 的证明链——现在所有"审计 PASS"的说法都缺一块。

---

### 1.3 `shadow` 形态引用未定义变量 `side`（潜伏 ReferenceError）

**证据**：`engine/songcut.timeline.js:98-101`

```js
case 'shadow': // cast shadow + tiny figure: distance, memory
  return fit(Object.assign(base, { x: W * (0.5 - 0.2 * cx * cxw), y: H * 0.94, h: 620, h2: 600,
    shadow: { dx: 210 * side, dy: -10, color: P.void, a: 0.52 }, sway: 0.006,
    in: { dur: 0.7, dx: 40 * side }, out: { at: C.dur - 0.4, dur: 0.4, dx: 30 * side } }));
```

`charParams` 里只有 `cx`（-1/0/1）和 `cxw`，**没有 `side` 这个变量**。JS 在这里不会编译报错，
只有在真的用到 `form: "shadow"` 的那一刻抛 `ReferenceError: side is not defined`，而且是
在 `renderFrame` 内部 → 整帧渲染失败。

当前 `songs/*.json` 只用 `full`，所以从未触发。但 6 个形态里 1 个是坏的，且写在新加的
立绘章节（`README.md`）旁边，**这是"文档说支持、代码一碰就炸"的典型**。

**修复**：`210 * cx` / `40 * cx` / `30 * cx`。同时建议给 `charParams` 的 6 个形态各写一条
一次性 smoke 断言（用一个假 shot 调一次，只验证不抛错），放进 `tools/`。

---

## 2. P1 — 应当修复（对齐 / 可维护性 / 性能）

### 2.1 单镜时长超官方上限：6 个镜头 8–12.5 s（全片 63 s 的立绘窗口）

**规范**：`references/05-engine.md` §3「60–80 shots for 3–4 min；chorus 0.9–2 s，verse 2.5–5 s，
a few long holds」；`engine/direction.js` 的 `structure.maxHold = 6.4` 注释直接引用
「01 §4: no shot may hold still longer than ~7 s」。

**实测**（`Z.SHOTS`）：

| 镜头 | 时长 | 场景 |
|---|---|---|
| s001 | 9.60 s | illust（开场立绘） |
| s008 | 12.20 s | illust |
| s019 | 12.50 s | illust |
| s027 | 12.50 s | illust |
| s044 | 8.20 s | illust |
| s053 | 8.00 s | illust |

其余 51 个镜头 median 3.25 s、p75 3.63 s，完全合规。**全部 6 个都是立绘窗口**——因为
`songcut.timeline.js:581` 有一条显式豁免：

```js
// a char window must not be split by the max-hold pass
B = B.filter((t,i) => i===0 || i===B.length-1 || charEdge(t)
  || !charShots.some(w => t > w.t0+0.01 && t < w.t1-0.01));
```

**为什么会这么写**：`charParams` 的 `in`/`out` 是按 `C.dur`（镜头时长）算的，一旦在窗口中间切一刀，
新镜头的入场动画会重播、人物会"抖一下再入场"。所以豁免是**被迫**的。

**影响**：12.5 s 不动的镜头上限在官方语境里是"观众会走神"的红线；本片的做法实际是靠背景板推拉 +
歌词变化撑住的。属于"有意为之的取舍"，但目前**没有在文档里登记为例外**，审计也不报告。

**建议**
1. 短期：在 `docs/COMPLETENESS.md` 明确登记"立绘窗口豁免 maxHold，原因是人物入场动画绑定镜头时长"，
   并让审计加一条 `maxShotLen` 检查（默认 7 s，`direction.char` 窗口内放宽到 13 s），把例外变成"显式豁免"而不是"看不见的违规"。
2. 中期：给立绘窗口一个**窗口级**动画时间轴（`t0/t1` 与 shot 解耦），窗口内允许 2–3 个镜头，
   入场只在第一个镜头、出场只在最后一个。这样既守 7 s 上限，又不抖。
3. 立绘占比已经到 28.7%，不需要再拉长单个窗口。

### 2.2 JIZURA plan 数量与"逐镜头随机换 plan"

- `songs/natianxiayule.json` `direction.type.plans = 6` → 实际建了 **7 个** plan
  （main / cf / oncam / portrait / v1 / v2 / v3）。每个 plan 都是 `J.plan()` 全量排一遍全部歌词。
- 实际使用分布：`main 10, v1 8, v3 6, portrait 6, oncam 9, cf 1, v2 1`。**v2 只用 1 次、cf 1 次**，
  却付了整套排版的启动与内存成本（页面 `ready` 实测 2.4 s）。
- `songcut.timeline.js:682` `plan = cw ? PORTRAIT_PLAN : pickPlan()` —— **每个镜头**随机抽 plan。
  后果：如果镜头边界落在一句歌词中间，同一句会在切点处换版式（字跳位）。官方语境是
  「几套 plan 按段落/角色分工」（06 §1–§3），不是逐镜头摇色子。

**建议**：`plans` 降到 4–5（`main / cf / oncam / portrait` + 至多 1 个 variant），并让 plan 按
**段落（act.role）** 固定映射，只有换段落才换 plan。这样既省启动成本，又消除句子跨 plan 跳版式。

### 2.3 `OFFICIAL_SOURCE_HASHES.json` 把两个已改文件标成"官方原样"

逐字节比对 `_ref/vocaloid-style-mv-pipeline/skills/vocaloid-style-mv/template` 后：

| 文件 | 清单里的说法 | 实际 |
|---|---|---|
| `engine/core/gl.js` | `identical` | **不同**：`LUT_ROWS 16 → 32`，LUT 纹理 `256×16 → 256×32` |
| `engine/core/main.js` | `identical` | **不同**：atlas `256×16 → 256×32`；新增 `graphic` 色带；新增按片读取 `songs/<id>.palette.json` |
| `tools/render_frames.mjs` | `modified` | 确实不同（webcodecs quantizer 模式 + PTS 重打时间戳）——标注正确，但清单没写"为什么" |

结论：**`identical_count: 25 / modified_count: 1` 是错的，真实是 23 / 3**。这份 JSON 正是
「官方文件未被改动」这一说法的唯一凭据，现在不可信。

**影响**：不影响出片，但影响"我们是否遵守了官方的引擎边界"这个论证。而且 `gl.js/main.js` 的改动
是**有意义的扩展**（多色带、每片专属 palette），本该被记成"扩展"而不是"原样"。

**建议**：
1. 重新生成清单（比较前统一 `\r\n → \n`，否则一个换行就能让整文件判为不同——这次 `render_frames.mjs`
   的 531 行 CRLF 就是例子）。
2. 每条 `modified` 加 `why` 与 `upstream_spec_ref`（例如 gl.js → `05-engine.md §7 LUT 行数`）。
3. 把这三处写进 `docs/COMPLETENESS.md`，说明"扩展点只加不改"。

### 2.4 配置里存在死键：顶层 `opening` / `ending` 被 `direction.*` 覆盖

`songs/natianxiayule.json` 同时有：

```
top-level : opening="typeCard"  ending="solid"
direction : opening="illust"    ending="afterimage"
```

引擎只读 `dir.opening` / `dir.ending`（`songcut.timeline.js:715,725`，`dir` 来自 `direction.normalize`）。
所以顶层的两个键**永远不生效**。这不是无害的冗余——上一轮"把 `ending` 改成 `solid`"就落在了死键上，
实际成片结尾仍是 `afterimage`（幸好如此）。

**建议**：删掉顶层两个键，或在校验里对"同名键存在于两处且值不同"直接报错。后者更好，能防止下一次
同样的误解。

### 2.5 `sceneArgs` 里存在"契约不符"的死分支，一引用就静默失败

| 分支 | 传入 | 场景期望 | 后果 |
|---|---|---|---|
| `panels`（`:257`） | `panels: 3 + (i % 3)`（**number**） | `panels: [{bg, view, char …}]`（**array**） | 渲染期对 number 取 `.length` → 空画面或异常 |
| `burst`（`:259`） | `cuts: []`, `shot: null` | `cuts: [{bg, char …}]` | 每拍一个空镜 |
| `solid`（`:184`） | `chars: [ch]` | `solid` **不读 chars**（`scenes.js:9-16`） | 人物被静默丢弃 |
| `jz`（`:186`） | `chars: [ch]` | `jz` 只画 plan（`scenes.js:19-21`） | 同上 |

当前 `panels` / `burst` 不在任何词表里，`solid` / `jz` 也不会撞上立绘窗口（窗口强制 `illust`），
所以**目前不出事**。但这是"写好了 25 个场景的参数映射，其中 4 个是错的"——只要哪天词表里加一个
`panels`，就会出现和 `ref` 一模一样的静默坏帧。

**建议**：给 `sceneArgs` 的返回值加一个极小的 schema（每场景声明必填字段与类型），
`Z.setup` 结束时统一校验一遍，不合法直接抛错。这也是把 1.1 那类问题从"抽帧才能发现"变成
"启动就报错"的通用办法。

### 2.6 显式立绘窗口不做互斥 / 不检查与 live 的重叠

`songcut.timeline.js:526-533`：显式窗口只检查"中点是否落在 live 窗口内"和"t1 > t0"，**不检查**：

1. **两个窗口互相重叠** → `charAt()` 返回第一个命中的，后面的窗口图可能整段不出现；
2. **窗口横跨 live 插入** → 中点测试通过，但 `:581` 的 maxHold 豁免会**把 live 的边界删掉**，
   于是 live 插入被立绘镜头整段吞掉，而审计的 `livePct <= 15` 只查上限、查不出"少播了"。

当前 6 个窗口都干净（`outsideLive=0`），属于潜伏问题。**建议**加两条校验：窗口两两不相交；
窗口区间与 `cfg.live` 任一区间不相交（不只测中点）。

### 2.7 无 lint / 无回归测试 / `package.json` 没有主入口

- `engine/songcut.timeline.js:29` `const Z = window.Z, D = Z.draw, E = Z.ease;` —— `E` **一次都没用到**（grep 计数 0）。
- `engine/recipes.js:24` 导出了 `CLASSES`（动机分类表），注释说"songcut 用它在每片里抽不同子集"，
  但全仓库没有任何消费点 → 死 API。
- `engine/tests/` 目录不存在，官方 05 §9 推荐的"单场景隔离测试 + stills"路径在本项目不可用。
- `package.json` 只有 `render/stills/analyze` 等官方脚本，**没有 `cut`**（真正的入口是 `node cut.mjs`）。
- 无 ESLint / Prettier / `.editorconfig`，也无 `tsc --checkJs`。考虑到文件已 783 行 + 2000 行级
  scene 文件，`// @ts-check` + JSDoc 才是防止 `side` 这类问题复发的最低成本手段。

**建议**：`npm run cut` 指向 `cut.mjs`；加 `eslint --no-eslintrc` 最小规则集（`no-undef` 就能抓住 1.3）；
把 `engine/tests/` 与 `tools/stills.mjs` 的用法补回 README。

---

## 3. P2 — 优化 / 锦上添花

### 3.1 动效性能

实测（真实 GPU，连续帧，`readPixels(1px)` 强制同步）：

| 场景 | avg | p50 | max | 说明 |
|---|---|---|---|---|
| illust（立绘窗口） | 7.1 ms | 5.1 | 58.5 | 稳态很好 |
| livewin | **21.1 ms** | 12.1 | 245.4 | 每帧解码一张 1080p JPEG |
| kanjiSlam | 14.9 ms | 2.8 | 218.6 | p50 只有 2.8 ms → 尖峰全是首次字形光栅 |
| jz（全屏字面） | 14.2 ms | 3.5 | 280.5 | 同上，首次排版 |
| afterimage | 2.9 ms | 1.9 | 31.2 | — |

**关键观察：mean 是 median 的 2–5 倍**，说明成本集中在"第一次"而不是"每一帧"。

1. **首次命中尖峰（218–280 ms）**：`typo.js:100` `glyphArt(ch, st)` 是懒加载缓存，而
   `kanjiSlam.init` 只做了 `loadFonts`（`:390`）。本片的 `cfg.glyphs` 是有限集合，完全可以
   在 `init` 里把每个字形×每种风格预光栅一遍。收益：kanjiSlam 从 14.9 → ≈3 ms，全片 8 个
   kanjiSlam 镜头共约 -90 帧 × 200 ms ≈ **省 18 s 墙钟**。
   *注意：这条要落在 `engine/scenes/typo.js`（官方文件）里，属于"扩展"而非"修 bug"，需同步更新
   `OFFICIAL_SOURCE_HASHES` 并把改动降到最小（新增一个 `init` 预热循环）。*
2. **`D.sprite` 的 `wave` 每帧 90 次 `drawImage`**（`core/draw.js:47-54`）：`N = 90` 条横向切片，
   每片都把 1024×1536 的源图缩放一次。实测立绘窗口 `drawImage` 计数 92–98/帧，其中 90 次来自这里。
   `wave.q = 12` 意味着这 90 次里 **29/30 帧算出来的结果完全一样**。
   → 可以按 `quant(t,12)` 的整数帧缓存"warp 后的精灵"（缓存 1 张 1080p 级 canvas），
   命中时 1 次 blit 代替 90 次。这是立绘镜最实惠的一处优化（7.1 → 约 5 ms）。
3. **`livewin` 每帧解一张 1080p JPEG**（`scenes/livewin.js:273` + `:71-77`）：9 个镜头 20.4 s
   ≈ **612 次全新解码**，`ensure()` 是 `await` 的，直接阻塞帧。
   → 三个方向（按收益排序）：(a) 在 `prepare.py` 阶段把 live 帧降到 960×540（它只占画面 486×872 的
   一个小窗，1080p 源是纯浪费）；(b) 改成 `createImageBitmap` + 小 LRU 预解码队列，而不是
   `new Image()` 逐张等；(c) 保留现状也不算错——21 ms 仍高于 30 fps 的 33 ms 预算线。
4. **`ref` 修好后**（见 1.1）它会 `await Z.renderShot(...)` 把另一个镜头整段重画一遍，
   成本会翻倍——届时别忘了它的 `p50` 会从 6.6 ms 涨到 ~10 ms。可接受。

### 3.2 渲染性能

上次出片的真实统计（`render/natianxiayule_stats.json`）：6594 帧 / 58.44 s 墙钟 / **112.84 fps**，
每帧 `render 3.14 ms + read 2.47 ms + post 0.24 ms`，墙钟 8.86 ms/帧。拆开看：

```
绘制（Canvas2D 全部场景）      3.1 ms   35%
捕获（new VideoFrame(canvas)） 2.5 ms   28%
编码背压 / MessageChannel 让出  ~3.0 ms  34%
WebGL 后处理                 0.24 ms    3%
```

**也就是说：继续优化绘制，天花板只有 35%。** 真正值得动的是捕获链路：

1. **`gl.readPixels` 不是瓶颈——`new VideoFrame(cv)` 才是**：它把 WebGL canvas 再复制一次。
   `engine/core/gl.js:197` 的 `preserveDrawingBuffer: true` 是**上游默认值**（不是我们改的），
   在 webcodecs 路径下并不必要（`VideoFrame` 构造是同步的，同一 task 内完成），关掉可以省掉
   一次隐式全缓冲保留。
2. **每帧 3 次 `texImage2D` 重分配**（`gl.js:226-232`）：`scene / text / fg` 各 1920×1080 RGBA，
   合计约 25 MB/帧 → 750 MB/s。用 `texSubImage2D`（同尺寸复用存储）代替 `texImage2D` 更轻；
   `text` 层在没有歌词的镜头（17/57）完全可以跳过上传。**在 1080p / 5070 Ti 上影响很小（post 只有
   0.24 ms），但这是 4K 化时第一个爆的点。**
3. **单镜路径多一次全屏 blit**：`core/main.js:160` `sceneX.drawImage(layerA, 0, 0)` —— `drawShot`
   本来可以直接画进 `sceneX`。这 1 次全屏拷贝在 1080p 约 0.3–0.5 ms，占了绘制的 15%。
   （官方代码，改动需登记。）
4. **转场时 `postFor()` 跑两次**（`main.js:175`）：每次还会执行 `Z.globalFX`（flash / shake）。
   不是 bug（A/B 两套参数确实都要算），但 `stateFor()` 一个对象要构 4 次/帧，是纯 GC 噪音。
5. **`--workers 1`**：官方渲染器支持分段并行。本机 112 fps 已经够用（220 s 片 58 s 渲完），
   但若要提速可开 2–3 个 worker；代价是每个 worker 重付 2.4 s 启动 + 各自的首次命中尖峰，
   而且 3.1 里那些懒缓存会失守 → 必须先把预热补齐再开多 worker，否则会出现 1/3、2/3 处的帧差。
6. **`loadMs = 2419 ms`**：7 个 JIZURA plan 的全量排版 + 字体 + 预加载。按 2.2 把 plan 降到 4–5
   可省掉一部分；单次渲染影响不大，但每次 `stills.mjs` 抽帧都要付这 2.4 s，累积起来很烦。

### 3.3 音频/节拍数据：`beats` 不是节拍

`tools/prepare.py` 把 `beats` 定义成"歌词行首 + 相邻行中点"：

```python
for i, (t, _) in enumerate(lines):
    beats.append(round(t, 3));  mid = (t + lines[i+1][0]) / 2; ...
```

于是 `Z.clock.pulse(t, k)`、`clock.bar()`、`clock.inBar()`、`downPulse()` 实际都挂在**歌词行**上，
不是音乐拍点上。受影响的是所有"beat 驱动"的表现：`illust` 的 `beatZoom`、looks 的 `beatCA`、
`D.glow` 的 `beat`、`kanjiSlam` 每拍一个字。副歌里一拍一句的时候听感是对的，一到长句/间奏就完全脱拍。

官方 `03-audio-and-lyrics.md` 的做法是 `analyze_audio.py` 产出真实 beat grid，歌词 onset 只用于切点。
本项目为了"绝对跟人声"选择了歌词作拍——这是**刻意的取舍**，但目前没写进文档，也没在审计里标注。
建议：至少把 `audio.json` 的 `beats` 改名为 `lyricGrid`，另存一份 `analyze_audio.py` 风格的
真实 `beats`（`bars` 已经有了，只差 beat 列），让 `Z.makeClock` 用真实拍、`nb(lyric onset)` 用歌词。

### 3.4 交付缺口：没有 9:16 竖版

官方 `08-render-and-delivery.md` §5 的交付清单包含竖版（`engine/vertical.html` + `Z.VERTICAL_CFG`）。
本项目 `engine/vertical.js` 是官方文件、原样保留，但从未跑过，`Z.VERTICAL_CFG` 也没配。
属于"可选但官方默认要求"的一项。

### 3.5 `prepare.py` 的可移植性

- `tools/prepare.py:18` `CUTS = pathlib.Path(r'D:\岁己歌切')` —— 绝对路径硬编码，换机器就废。
- `tools/prepare.py:23` `SEEDS = {'faruxue':164, 'huainvhai':2011, 'natianxiayule':20261110, ...}`
  —— 每加一首歌都要改代码，与 README「新建 `songs/<date>.json` + `songs/<id>.json` + `songs/<id>.lrc`
  三件套即可」的说法矛盾。建议把 seed 挪进 `songs/<id>.json` 的 `direction.seed`（已经有这个字段了），
  `CUTS` 从 `ROOT.parent` 推导（`cut.mjs` 已经是这么做的，`prepare.py` 没跟上）。

### 3.6 `cut.mjs` 的"最后一步才失败"

`cut.mjs:106` 在**渲染完成之后**才 `fs.copyFileSync(songs/<id>.deliver.lrc)`。如果这个文件缺失，
会白跑 58 s 渲染再崩在交付。建议把 `deliver.lrc` / 磁盘空间 / 输出目录可写性一起挪到前面做预检。
（现在 5 首歌都有 `deliver.lrc`，属于脆弱点而非现存故障。）

---

## 4. 与官方规范的逐项对齐表

| 规范条目 | 出处 | 现状 | 判定 |
|---|---|---|---|
| 8 个官方 scene 文件全加载 | 05 §1 | 8/8 | ✅ |
| ≥24 个官方场景注册 | 05 §8 | 25 | ✅ |
| shot 契约 `{id,t0,t1,scene,args,lyric,plan,post,trans,text,hud}` | 05 §3 | 一致 | ✅ |
| 每镜独立 post | 01 §4 | 57/57 | ✅ |
| `lyric:'none'` 只用在"场景本身就是排版"处 | 06 §6 | 17 处：kanjiSlam 8 / tunnel 3 / afterimage 3 / jz 3，全部合规 | ✅ |
| 中文字面「中央を空ける」两侧带 36% | JIZURA README | `zoneW 0.36` | ✅ |
| centerFree 两带共用同一 layout/运动/装饰/运镜 | JIZURA README | `portrait` plan 收窄为 reading layouts | ✅ |
| 每片独立 palette | 02 §13 | `songs/<id>.palette.json` + S_ 色带 | ✅ |
| 掌机手绘节奏 12 fps | 05 §2 | `q:12` / `S.tq` | ✅ |
| 纯时间函数、无 wall clock | 05 §2 | 无 `Date.now`，`Math.random` 被页面种子接管 | ✅ |
| 60–80 镜（3–4 min） | 05 §3 | **57** | ⚠️ 略少 |
| 单镜 ≤ ~7 s | 01 §4 / maxHold 6.4 | **6 个立绘镜头 8–12.5 s** | ❌ 见 2.1 |
| credit 上屏 | 09 §2 | `afterimage` ×3 带 `cfg.credit` | ✅ |
| 无死气镜头 | 09 §2 | **16.58 s 报错卡** | ❌ 见 1.1 |
| 官方源文件未被改动 | `docs/OFFICIAL_SOURCE_HASHES.json` | **gl.js / main.js 实际改过却标 identical** | ❌ 见 2.3 |
| LUT atlas 16 行 | 05 §7 | 已扩到 32 行（三处同步改，自洽） | ⚠️ 扩展，需登记 |
| 9:16 竖版交付 | 08 §5 | 未产出 | ⚠️ 可选 |
| 真 beat grid | 03 §5 | `beats` = 歌词行首+中点 | ⚠️ 见 3.3 |

---

## 5. 建议的修复顺序

**状态（2026-11-15 03:45）**：第一批、第二批、3.6 已完成并复渲，见 §7。第三、四批未动。

**第一批（半小时，纯收益）**
1. 1.3 `side` → `cx`（3 行）
2. 1.2 审计加"占位文本 + 近空帧"两条检查（~30 行）→ 让 1.1 暴露出来
3. 2.4 删死键 / 加冲突校验

**第二批（1 小时，出片质量）**
4. 1.1 `ref` 改成真的复用前一镜头（或删词表）
5. 2.6 窗口互斥 + 与 live 不相交的校验
6. 重渲《那天下雨了》——这一版才应该叫"复核通过"

**第三批（半天，结构与性能）**
7. 2.2 plan 降到 4–5、按段落固定
8. 3.1.1 字形预热 → 省 ~18 s
9. 3.1.2 sprite warp 按 12 fps 缓存
10. 2.3 重生成 `OFFICIAL_SOURCE_HASHES`（含 `why`）

**第四批（可选）**
11. 3.2 关 `preserveDrawingBuffer` + `texSubImage2D`
12. 3.3 真 beat grid
13. 2.1 立绘窗口内部切点（消除 7 s 违规）
14. 3.4 竖版交付

---

## 6. 附：本次审计用到的运行时证据

- 分层计时：hook `Z.post.render` / `Z.SCENES[*].draw` / `JZ.drawPlan`，9 个时间点 × 3 次取 min。
- 连续帧计时：illust / livewin / kanjiSlam / jz / ref / afterimage 各 30–40 连续帧。
- 计数：hook `CanvasRenderingContext2D.prototype.drawImage / createRadialGradient / createLinearGradient /
  save / fillRect / fillText` 与 `filter` setter。
- 成片抽帧：`ffmpeg -ss <t> -frames:v 1`，存于 `render/_audit/`。
- 官方对照：`D:/岁己歌切/_ref/vocaloid-style-mv-pipeline/skills/vocaloid-style-mv/{template,references}`，
  按行尾归一化后逐文件 diff。

---

## 7. 修复记录（2026-11-15 03:20–03:45）

### 7.1 已修

| 编号 | 改动 | 文件 | 反证方式 |
|---|---|---|---|
| 1.1 | `ref` 真的复用已建镜头：往回 1–3 个（跳过 `ref` 与 `livewin`，前者防递归、后者帧不可复渲）；`at` 取"观众最后看到的那一帧" `min(t0, prev.t1 − 1/30)`；**不再传 `meltAt/meltEnd`**——上游那两项是绝对时间，原来传的 0.3 / 0.9 是分数，等于一入场就已化完；`mode` 收敛为 `melt` / `freeze`（`tear`/`burn` 根本不是 meta.js 认识的值，只会静默落到 freeze） | `engine/songcut.timeline.js` | 把 `shot` 改回 `null` 重跑审计 → 两条新检查同时 FAIL 并点名 s005/s010/s025/s028；改回即 PASS |
| 1.2 | 审计新增两条检查：`no placeholder / debug text on screen`（hook `fillText` 抓 `/no shot\|missing scene\|undefined\|NaN\|Infinity/`）与 `every shot reference resolves`（`ref`/`burst`/`montage`/`freeze` 必须给出存在的 shot id） | `tools/audit_official.mjs` | 同上，坏代码 FAIL / 好代码 PASS，双向验证 |
| 1.3 | `side` → `cx`（`shadow` 形态的三处） | `engine/songcut.timeline.js` | 该分支不再引用作用域外变量 |
| 2.4 | 删顶层死键；并在 `direction.normalize` 增加校验：顶层与 `direction.*` 同名键值不同即抛错 | `songs/natianxiayule.json`、`engine/direction.js` | 顶层键已不存在；`opening`/`ending` 现只从 `direction.*` 读取 |
| 2.6 | 立绘窗口两两不相交 + 窗口区间与 live 区间不相交（不再只测中点，pad 0.05 与原有中点测试一致） | `engine/songcut.timeline.js` | 6 个窗口全部通过（`charShots=6/6`），无一个被误杀 |
| 3.6 | `deliver.lrc` 存在性检查从"渲染完成后"提前到渲染之前 | `cut.mjs` | — |

### 7.2 被否决的方案（重要）

审计初稿建议用「下采样 32×18，`std < 3` 且 `mean < 12`」检测死气帧。**实测否决**：

| 样本 | mean | std | 48×27 不同色数 |
|---|---|---|---|
| 占位卡 s010 | 14.4 | 3.0 | 4 |
| 占位卡 s005 | 16.0 | 19.1 | — |
| 占位卡 s028 | 17.8 | 23.7 | — |
| **合法** afterimage s057 | **5.7** | 20.5 | 29 |
| **合法** illust s058 | 17.7 | 4.7 | 20 |

合法镜头的分布完全覆盖占位卡：`afterimage` 比占位卡更暗，`illust` 比它更平。任何阈值都只能在
漏检与误杀之间二选一。换成结构性检查后零误报，且在构建期就能抓。

**结论：一条不能区分好坏的检查比没有检查更糟**——它会训练所有人忽略审计。所以这里宁可只留一条
确实能报警的检查，也不留一条看着全面的假检查。

### 7.3 修复过程中新发现的两处

- **`cut.mjs` 用 `--song` 局部重渲会把 manifest 从 3 首缩成 1 首**（本次实测踩中）。已改为 merge：
  未渲染的条目从旧 manifest 保留，且必须 mp4 真的在目录里才保留，manifest 不能比文件活得久。
  2026-11-10 的 manifest 已按磁盘事实（ffprobe 三支 mp4 + 配置 + `_audit_*.json`）重建，`bit_rate`
  与原值逐位一致（34,342,925）。
- **源录播被清理后 `prepare.py` 无解**：`D:/录播/25788785-岁己SUI/2026-11-10/` 已不存在。新增
  `cut.mjs --skip-prepare`，并断言 `audio/song.wav`、`analysis/audio.json`、`analysis/envelope_30fps.json`、
  `assets/live/<id>/frames` 齐全，同时打印 wav 的 mtime——复用可以，盲用不行。

### 7.4 复渲结果

```
node cut.mjs --date 2026-11-10 --song natianxiayule --skip-prepare
```

- 审计 **24 项全 PASS**（原 22 项 + 2 项新检查），25 个官方场景注册 / 9 个使用
- 6594 帧 · 219.8 s · 1920×1080 30/1 · 34.34 Mb/s · 944 MB
- 渲染 80.47 fps（81.9 s 渲 + 7.4 s mux），与上一版同量级
- 交付 `cuts/2026-11-10/official/【岁己SUI】那天下雨了【20261110歌切】.mp4`；另两首 mp4 与 lrc 的
  mtime 仍为 11-11 10:29 / 10:31，**未被触碰**
- 抽帧复核四个原 `ref` 镜头：17.86 / 18.90 s 为冻结帧 + 裂纹展开；43.05 / 94.16 / 95.60 s 为列式溶解；
  114.62 s 为另一处溶解。**四段都不再有英文字符**（原为 `ref: no shot undefined`）

### 7.5 仍未做（等指示）

第三批（2.2 plan 降到 4–5 并按段落固定 / 3.1.1 字形预热 / 3.1.2 sprite warp 缓存 / 2.3 重生成哈希清单）、
第四批（3.2 关 `preserveDrawingBuffer` + `texSubImage2D` / 3.3 真 beat grid / 2.1 立绘窗口内切点 / 3.4 竖版）、
以及 2.5 的 `sceneArgs` schema 校验（`panels`/`burst`/`solid`/`jz` 四处契约不符）。
