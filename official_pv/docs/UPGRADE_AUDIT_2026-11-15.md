# 全量升级排查 · 2026-11-15

> 范围：本项目 `official_pv/` 全部自有代码（10.7k 行）+ 工具链（1.6k 行）+ 配置（2.4k 行）
> + 上游 10 份规范 / SKILL.md / README / `examples/zanko` 示例工程（3.3k 行）+ 外部同类项目调研。
> 原则：**能实测的不推断**。凡是"某处可能有问题"，都跑一遍或抽一帧确认；凡是否证掉的，记在 §8。

---

## 0. 摘要

六条 P0，都不是代码风格问题：**前三条已经印在成片里**，**后三条让审计看不见问题**：

| # | 问题 | 影响 | 状态 |
|---|---|---|---|
| 1 | **节拍网格是假的**：`audio.json.beats` 填的是 97 个歌词行首，官方契约要求 onset-snapped 节拍网格（《残光》486 个，我们应有 322 个） | 全片所有 beat 驱动失效：`pulse`/`bar`/`downPulse`/`beatZoom`/`beatBloom`/`beatCA`/`kanjiSlam` 一拍一字/JIZURA 节拍动效 | 已在成片 |
| 2 | **`winning_phase` 把秒当索引**：`downbeat_info.winning_phase=1.6761`（秒）被当作 beat 索引传进 `makeClock` | `inBar()` 在 2199 个采样点里**一次都不等于 0**；`downPulse()` **恒为 0**；`bar()` 出现 **-1** | 已在成片 |
| 3 | **《残光》的字面量泄漏进画面**：`ember` 小字打字机写死 `'残光 / ZANKŌ'`，`titleRiff` 四角 HUD 默认 `「残光」ZANKŌ — afterglow / BPM 131 / 踏切 No.7 / 35°41′N 139°46′E / 03:44` | 《发如雪》1 镜、《坏女孩》2 镜、《下个，路口，见》2 镜 | 已在成片 |
| 4 | **12 轴差异化审计是字符串比对**：`signature()` 的 `world/timeArc/material/role` 四个轴直接取配置里手写的散文（`songs/*.json:596` 的 `axes` 块），`palette/motif` 散文优先，其余 6 轴也是配置派生 —— **没有一轴看画面**。实测 10 对里 **9 对通过、`faruxue × huainvhai` 只有 2/12**（这两首配置层几乎同构），而现有审计只拿新片互比，永远看不到这个塌陷 | "两片相差 ≥9 轴"= "两份 JSON 写得不一样"；防同质化的核心保障实际不存在 | 审计失效 |
| 5 | **审计尺子有三套**（18 / 22 / 24 项），且上一轮新加的"占位文本"检查正则只认英文（`/no shot\|missing scene\|undefined\|NaN\|Infinity/i`） | 老片缺的正是"占位文本 + 引用解析"两条；而「残光」两个字**不在那个正则里**，即使重跑也不会报警 | 审计失效 |
| 6 | **`cadence` 轴在 manual 模式下永久失效**：该字段只在 `buildActs`/`makeAct` 里生成，而手写 `cfg.acts` 九键无此字段 → `faruxue`/`huainvhai` 的 `cadence` 恒为 `""`，两首之间永远判"相同" | `FREE_AXES` 自称自由轴，实为死轴；节奏差异完全不进入差异化审计 | 已在数据 |

一句话：**上一轮修掉了"画面上的英文调试字"，但没发现画面上还印着另一部作品的标题；而"差异化"这道防线，守的是 JSON 不是画面。**

> 补记：§1.4 我第一版用静态推理说"门槛 9 结构上够不到"，实测证明上界对、结论错 ——
> 真实情况是 9 对通过、1 对塌到 2/12。更正过程记在 §8 最后一段。


---

## 1. P0 — 必修（影响成片正确性）

### 1.1 节拍网格：97 个歌词行首 vs 应有 322 拍

**官方契约**（`03-audio-and-lyrics.md`）：

```
L49  | beats         | 486 times, onset-snapped | hits: cuts, flashes, slams
L50  | beats_smooth  | same indices, jitter-free spline grid | continuous motion
L51  | downbeat_info.winning_phase | 1 → downbeat = beats[k] with k%4==1 | Z.makeClock(…, winning_phase)
L53  | downbeats     | 122 times | bar hits
L85  Z.clock = Z.makeClock(audio.beats, audio.bpm, audio.downbeat_info.winning_phase)
```

《残光》3.73 min @131 bpm = 489 拍，文档记 486 → **完全对应**。
我们 219.8 s @88 bpm = **322 拍**，实际 `beats` = **97**（`render/_clockcheck.mjs` 实测 `nBeats=97`）。

来源在 `tools/prepare.py:103`，注释自己写着 `beats = REAL lyric onsets + midpoints`。

**实测后果**（`render/_beatcheck.mjs`）：

```
clock beats total = 97 (should be ~322 for 219.8s @88bpm)
  s013 dur=1.79s beatsInShot=2 glyphs=4
  s017 dur=2.85s beatsInShot=1 glyphs=4
  s021 dur=3.41s beatsInShot=1 glyphs=4   ← 配了 4 个字，镜头内只有 1 拍
  s031 dur=3.09s beatsInShot=2 glyphs=4
  ...
```

`typo.js:394-396` 用 `shotBeats(S)` 过滤镜头内的 beat 当字形索引 → 镜头内只有 1 拍时 `k` 恒为 0 →
**只显示 `glyphs[0]`**。抽帧复核 s021（77.90–81.31 s，字表「呆 车 书 晴」）：
78.30 s 是「呆」，80.91 s **还是「呆」**。四个字只打出第一个，后三个从未出现。

同一根源影响 `main.js:190` 的时钟、JIZURA 的 `plan.beats`（`jizura_engine.js:3937` `beatAt`）、
以及 `beatZoom / beatBloom / beatCA / beatBump / glow.beat / tunnel / road.dolly`。

**数据其实已经算出来了**：`audio.json` 里有 `downbeats`（80 个，间隔稳定 2.727 s = 4×0.6818，
即 88 bpm 的完整小节）和 `onsets_strong`（220 个）。两者都没被正确使用：

- `downbeats` —— grep 全项目 **0 处**读取；
- `onsets_strong` —— 我第一版写"0 引用"，**这是错的**，实际有 1 处：`intro.js:64` 的
  `strongOnsets()` 做 `.filter(o => o.t > t0 && o.strength >= thr)`，即在等 **`{t, strength}` 对象**；
  而 `prepare.py:155` 写的是**裸浮点数组**。`o.t` 恒为 `undefined`，比较恒假 → 返回 `[]`，
  于是 `intro.js:497` 的 `hitsT` 永远是空数组。**不是没接线，是接了但形状不对，静默失效。**

### 1.1 已修复（2026-11-15 晚，代码已落盘未渲染）

| 改动 | 位置 | 结果 |
|---|---|---|
| `beats` 改为「标称 BPM 网格 + 反拍吸附真实 onset」 | `prepare.py` 新增 3c 段 | 97 → **320 拍**（理论 322），57% 反拍被吸附 |
| 歌词行首另存 `lyricOnsets` | 同上 | 97 条，与 `beats` 分家 |
| `onsets_strong` 改为 `[{t, strength}]` | 同上 | 匹配 `intro.js:64` 唯一的消费者 |
| `winning_phase` 改为 0..3 索引 | 同上 | 秒值另存 `phase_seconds`（1.6761） |
| 运行时兜底 | `main.js` 新增 `beatPhase()` | 旧 json 的秒值不再污染时钟，并 warn |
| 小节与下标永不错位 | 3c 用夹紧代替丢弃 | 断言 `len(beats) == len(nominal)`；实测每个 `k%4==0` 的 beat 精确等于 `downbeats[k//4]` |

验证方式：不改 `audio/`、不重抽音频、不渲染——用仓库里现成的 `audio/song.wav` 复算
3b→3c 全部逻辑（脚本见 §7）。输出：

```
duration 219.8 s   bpm 88   spb 0.6818 s
theoretical 322 beats   OLD grid 97
NEW grid 320 beats   phase_idx=0   snapped=183 (57%)
gap min/med/max  0.333 / 0.682 / 1.000 s
index stability: 每个 k%4==0 的 beat == downbeats[k//4] : True
```

**风险未消**：拍数变 3.3 倍后，所有节拍类效果强度都要重调（`beatCA 2.0` 在 0.68 s 一拍下会过密），
且 `meta.js` 的"每 N 拍切一刀"会真的开始按拍切。这一步必须靠重渲验证，本轮按指示**未渲染**。

**验证手段**：`render/_beatcheck.mjs` 已留档，改完重跑应显示 `nBeats≈320`、`beatsInShot≈4-5`。

### 1.2 `winning_phase` 单位错：秒当索引

`tools/prepare.py` 写 `winning_phase: 1.6761`（`downbeat_info.note` 自称"measured from the PCM onset envelope"），
`main.js:190` 原样传给 `Z.makeClock(beats, bpm, downbeatIndex0)`，
而 `util.js:71,81` 把它当**索引**用：

```js
const beatNo = t => idx(t) - downbeatIndex0;          // 整数 - 1.6761 = 永远小数
at: n => beats[n + downbeatIndex0] ?? ...
```

官方定义（`03 L51`）是 `1 → downbeat = beats[k] with k % 4 == 1`，即 **0–3 的相位索引**。

**实测**（`render/_clockcheck.mjs`，2199 个采样点）：

| t | idx | beatNo | bar | inBar | downPulse |
|---|---|---|---|---|---|
| 10 | 1 | -0.676 | **-1** | 3.324 | 0 |
| 30 | 8 | 6.324 | 1 | 2.324 | 0 |
| 50 | 21 | 19.324 | 4 | 3.324 | 0 |
| 80 | 38 | 36.324 | 9 | 0.324 | 0 |
| 120 | 56 | 54.324 | 13 | 2.324 | 0 |
| 180 | 81 | 79.324 | 19 | 3.324 | 0 |

`inBar(t) === 0` 命中 **0 / 2199 次**；`downPulse` 全为 0；`bar()` 出现负数。

`downPulse` 被 `typo.js:793`（kanjiSlam 的 `dpulse`）、`tunnel`、`road` 使用 → 这些"小节重拍"效果全部静默失效。

**修法**：`prepare.py` 输出 `winning_phase: <0..3 的整数>`（相位落在第几拍），另存 `phase_time` 保留秒值。
或者更稳：既然 1.1 会把 `beats` 换成规整网格，`winning_phase` 此时可以固定为 0（网格从 downbeat 起铺）。

### 1.3 《残光》字面量泄漏（3 首歌、5 个镜头）

| 场景 | 硬编码位置 | 是否可被 args 覆盖 | 用到它的片子 |
|---|---|---|---|
| `ember` | `intro.js:344` `const title = '残光 / ZANKŌ'` | **否**，函数内写死 | 发如雪 ×1、坏女孩 ×2 |
| `titleRiff` | `intro.js:875-876` HUD 默认 `title/when/bpm/geo/place/len` | 是（`S.args.hud`），但 songcut 没传 | 下个，路口，见 ×2 |

`titleRiff` 默认值原文：

```js
{ title: '「残光」 ZANKŌ — afterglow', when: 'DUSK / 逢魔が時', bpm: 'BPM 131 · 4/4',
  geo: '35°41′N 139°46′E', place: '踏切 No.7 — 上り', len: '03:44' }
```

`songcut.timeline.js:201-205` 的 `titleRiff` 分支只传了 `seed/whiteAt/grain/variant/char/sky`，
**没有 `hud`** → 全用残光默认。而且 `glyphs` 也没传，默认 `['残','光']` 被 slam 上屏。

**修法**：
- `titleRiff`：在 `sceneArgs` 里补 `hud: { title: cfg.title, bpm: `BPM ${cfg.bpm} · 4/4`, place: 歌手/场次, len: mm:ss }`，
  并补 `glyphs: cfg.glyphs.slice(0,2)`、`ruby: false`、`sub: cfg.titleSub`；
- `ember`：`intro.js:344` 是**官方原样文件**，改它必须登记为 modified。两条路：
  (a) 改 `intro.js:344` 为 `S.args.hudTitle || '残光 / ZANKŌ'`，songcut 传 `hudTitle: cfg.title`（改官方文件，21→22 行）；
  (b) 弃用 `ember`，`lexicon` 里换 `converge`/`solid`（不改官方文件，但少一种开场）。
  倾向 (a)——这是"官方原样"里少数真正阻碍复用的一处，和 `render_frames.mjs` 的补丁性质相同。

**验证手段**：抽 `ember` / `titleRiff` 镜头帧，肉眼确认无"残光"二字。**这两处必须在重出片前修掉。**

### 1.4 12 轴差异化审计核对的是配置字符串，不是画面

`engine/direction.js:276-302` 的 `signature()`：

```js
world:    A.world    || null,                       // A = cfg.direction.axes，配置里手写的散文
timeArc:  A.timeArc  || null,
material: A.material || null,
role:     A.role     || null,
palette:  A.palette  || (cfg.palette ? Object.values(cfg.palette).slice(0,4).join('') : null),
motif:    A.motif    || (cfg.motifs || []).slice(0,3).join('/'),
key:      A.key      || (d.grade.exposure > 0.15 ? 'high' : 'low'),
type:     `${styles.join('+')}|${schemes}|${plans}p`,
trans:    dedupe(acts.flatMap(a => a.trans)).sort().join(','),
cadence:  acts.map(a => a.cadence).join(''),
density:  String(d.structure.density),
ending:   d.ending,
```

`songs/natianxiayule.json:596` 的 `axes` 块实际内容：

```json
{ "world": "雨后市郊车站 · 候车月台 / 车窗 / 积水",
  "timeArc": "阴 → 雨停 → 一道很淡的彩虹",
  "palette": "冷青灰 #07121A/#1E3B48 + 单一暖金 #E8A34C",
  "motif": "竖直雨线 + 圆弧（透明伞 / 虹）",
  "material": "细笔水彩 + 纸纹，平涂 + 单硬阴影",
  "role": "安静撑伞的少女，越过肩膀回望" }
```

**先说一个更基础的问题：我们的 12 轴与官方 `02 §10` 的 12 行不是同一张矩阵。**

| 官方 `02 §93-108` 的 12 行 | `direction.js:33` 的 `AXES` | 对应 |
|---|---|---|
| world / setting | `world` | ✅ |
| time arc | `timeArc` | ✅ |
| key / exposure | `key` | ✅ |
| palette | `palette` | ✅ |
| master shape | `motif` | ⚠️ 部分（motif 是母题，不一定是"形状"） |
| material metaphor | `material` | ✅ |
| **line style**（brush-pen / thin pencil / thick marker / crayon / woodcut） | — | ❌ **缺** |
| character archetype | `role` | ⚠️ 部分 |
| typography voice | `type` | ⚠️ 部分 |
| **3D role**（flythrough / diorama / macro / none） | — | ❌ **缺** |
| transition grammar | `trans` | ✅ |
| ending | `ending` | ✅ |
| （官方在表后 "Also vary pacing"） | `cadence` | 表外 |
| （官方在表后 "density of typography"） | `density` | 表外 |

12 对 12 是**数字巧合**。缺席的 `line style` 恰恰是"手书 PV"最核心的质感轴
（笔压、线条粗细、用笔工具），而我们只能用 `material` 里的"细笔水彩 + 纸纹"一句话带过——
这句话不参与任何渲染决策，只参与签名比对。

**再看轴值怎么来的：**

也就是说：**12 个轴里 4 个是手写散文，2 个散文优先，其余 6 个也是配置值**。
`DIR.diff()` 数的是字符串不相等，`audit_official.mjs:190` 报出的 `worst: 11/12` 只是
"两首歌的 JSON 写得很不一样"。

**我第一版报告在这里写错了，用实测更正。** 我原先据 `direction.js:309` 判等语义做了个静态推理：
"两首都没写 axes 时 4 轴恒 null → 最多只能差 8 轴 → 门槛 9 在结构上够不到"。上界算对了，
但**结论方向错了** —— 只要一方写了 axes，门槛是够得到的。所以我直接跑了真实的
`normalize → buildActs → signature → diff`（复刻 `songcut.timeline.js:317-333` 的 LRC 解析与
`mode` 分支），5 首歌 10 对全量结果：

| 对 | 差值 | 相同的轴 |
|---|---|---|
| `faruxue` × `huainvhai` | **2 / 12** ← 唯一挂的 | world, timeArc, key, material, role, type, trans, cadence, density, ending |
| `faruxue` × `natianxiayule` | 9 / 12 | key, density, ending |
| `huainvhai` × `natianxiayule` | 9 / 12 | key, density, ending |
| `faruxue` × `yiyangdeyueguang` | 11 / 12 | key |
| `huainvhai` × `yiyangdeyueguang` | 11 / 12 | key |
| `yiyangdeyueguang` × `natianxiayule` | 11 / 12 | key |
| 含 `xiageluokoujian` 的 4 对 | 12 / 12 | — |

所以门禁的真实状态是：**10 对里 9 对过，只有两首老片之间塌到 2/12**。
而塌下去的原因不是"没画面差异"，是**两条老片在配置层几乎完全同构**：

- `type` 两首都是 `gold|dark|5p` —— 排版声音一模一样；
- `ending` 两首都是默认的 `afterimage`，`density` 都是 `2`；
- `trans` 池子相同（都从 `DENY` 后的通用池抽，见 §2.11）；
- 4 个散文轴两边都是 `null` → 恒判 same；
- 只剩 `palette`（两组 hex 不同）和 `motif`（`雪/月/墨` vs `霓虹/雨/拨号音`）是真的不同。

**这里还挖出一个比"核对字符串"更硬的 bug：`cadence` 轴在 manual 模式下永久失效。**

`cadence` 取自 `acts.map(a => a.cadence).join('')`（`direction.js:293`），而 `cadence` 字段只在
`buildActs` 的 `makeAct` 里生成（`direction.js:221`）。实测：

| 曲目 | `structure.mode` | LRC 行 | acts | 带 `cadence` 的 act |
|---|---|---|---|---|
| `faruxue` | `manual`（默认） | 49 | 10 | **0** |
| `huainvhai` | `manual`（默认） | 49 | 8 | **0** |
| `xiageluokoujian` | `auto` | 49 | 9 | 9 |
| `yiyangdeyueguang` | `auto` | 49 | 7 | 7 |
| `natianxiayule` | `auto` | 49 | 10 | 10 |

因为 `songcut.timeline.js:332` 只在 `mode === 'auto'` 时调 `buildActs`，否则优先用手写 `cfg.acts`；
而 `songs/*.json` 里手写的 acts 只有 `{id,t0,t1,look,intensity,lut,pool,fx,trans}` 九个键，**没有 `cadence`**。
结果：`cadence` 在 manual 两首里恒为 `""`，两首之间永远判"相同"——即使一首每句一切、一首八句一切。
`DIR.FREE_AXES = ['cadence','density']`（`direction.js:38`）声明它是"自由轴"，
实际它是**两根死轴之一**（另一根 `density` 取的也是 `String(d.structure.density)` 配置值，不是实测密度）。

> **补充更正**：`audit_official.mjs:190` 的 `worst` 是逐片与"其他片"比出来的最大值，
> 它报 `11/12` 是因为新三首互相之间有 12/12 的对，把最差值撑高了；
> 它**从不检查两首老片之间**。所以 2/12 这个塌陷在现有审计里完全不可见 ——
> 这也是 §1.5 "审计只跟新片比" 的另一面。

**而且比较本身也不对等**，两处机制决定了"有声明"永远占便宜：

- `palette` 轴有两种语义：有声明时取中文描述（`"冷青灰 #07121A/#1E3B48 + 单一暖金 #E8A34C"`，约 30 字），
  无声明时取 `Object.values(cfg.palette).slice(0,4).join('')`（4 个 hex 拼接）。
  两种形态 `String()` 比较 **100% 不等** —— 一首写了 axes、一首没写，palette 这一轴就白送一个差异。
  `world / timeArc / material / role` 同理（长句 vs `null`）。
- `ending` 有默认值：`direction.js:89` 的 `DEFAULTS.ending = 'afterimage'` 在 `normalize()` 阶段就填了，
  早于 `songcut.timeline.js:771` 的兜底。所以审计里 `ending` **永远非空**，
  "没想过片尾怎么收"与"明确决定用 afterimage"在数据层完全不可区分 —— 后者算创意决策，前者算默认值。

这与上一轮的 `ref: no shot undefined` 是同一类失效（审计 PASS ≠ 画面正确），但更隐蔽：
axes 本来就是"设计意图"，写得漂亮就必然通过，**永远不会 FAIL**。

> **归类说明**：这一条是**方法论缺陷**，不是引擎 bug。代码层可指认的只有两行
> （`direction.js:309` 的判等语义、`direction.js:34-37` 的成员与 02 §10 不一致），
> 真正的问题是"用配置字符串代理画面观感"。修它要改审计口径，不是改渲染。

官方 `02 §10` 的本意是**成片之间的观感差异**，不是台账差异。

**它还有一层不对称**：3 首新片（`natianxiayule` / `xiageluokoujian` / `yiyangdeyueguang`）
都写了 `axes` 块，12 轴都有值；2 首旧片（`faruxue` / `huainvhai`）**没有写**，
`world / timeArc / material / role` 这 4 轴恒为 `null`。

`DIR.diff`（`direction.js:305-310`）的判等是：

```js
const eq = (x == null || y == null) ? (x === y) : String(x) === String(y);
```

两边都 `null` 时 `x === y` 成立 → 记为 same。这就是上表里 `faruxue × huainvhai` 塌到 2/12 的机制：
4 个 null 轴全判"相同"，等于门禁只在那 8 个有值的轴上考。

于是这道门禁的真实激励是：**奖励"把设计说明写进 JSON"，惩罚"没写文档"**，与画面质量无关。
写了就过，不写就容易挂 —— 这比"永远 PASS"更危险，因为它会诱导以后每次改片先补文案。

**修法**（按成本递增）：
- (a) 立刻做：把这条检查改名为 `declared divergence`，并在 `COMPLETENESS.md` 里写明
  "这是意图层差异，不是画面差异"，避免继续拿它当质量证据；
- (b) 低成本硬指标：把 4 个散文轴换成可核验量 —— `world` → 该片 bg plate 的主色相直方图，
  `role` → 是否有 `chars` + 人物占比，`material` → 全片 `grain/boil` 均值，`timeArc` → 段落 LUT 序列；
- (c) 根治：两片各抽 12 帧（每段一帧），算色相分布 / 平均亮度 / 运动强度（相邻帧差分）三条量，
  要求至少两条的距离超过阈值。这才是"画面不同质"的证据。

> ⚠️ **改 (b)/(c) 时有个坑必须先预置，否则下一手会以为改坏了。**
> A12 的根子是**量纲错了**：`differ >= 9` 是绝对计数，却用来表达"12 轴里差 9 轴"这个比例要求。
> 两者在数学上互斥 —— 一旦把不可比的轴从分母里剔掉，分母收缩，9 就永远不可达。以
> `faruxue × natianxiayule` 为例，三种状态：
>
> | 状态 | 结果 | 差异轴 |
> |---|---|---|
> | 现状（`direction.js:309` 原样） | **9 / 12** 卡线通过 | 4 个 null 轴 + palette + cadence + motif + type + trans |
> | 只剔除 4 个 null 轴 | **5 / 8** | palette, cadence, motif, type, trans |
> | 再剔 palette（取值域不同）与 cadence（空串） | **3 / 6** | motif, type, trans |
>
> 所以三件事不是三条独立建议，是**同一个缺陷的三处**，必须一起动：
> (1) 判等语义（`direction.js:309`）、(2) 门槛口径（`audit_official.mjs:190` 改比例制）、
> (3) 退化轴的处置（null / 空串 / 取值域不同 → 记 `unknown` 不计入分母）。
> 只动 (1) 的症状是"修完审计全红" —— 分母 6、门槛 9，恒 FAIL，看起来像改坏了，其实是量纲没改。

**验证手段**：本轮已确认 `axes` 无一处引用画面数据（grep `signature(` 的入参只有 `cfg` 与 `acts`）。

### 1.5 审计证据链有三套尺子（18 / 22 / 24 项）

这一条是**交付与归档问题**，不是代码缺陷，但影响所有"PASS"的可比性：

| 曲目 | 审计项数 | 缺失项 |
|---|---|---|
| 发如雪 | **18** | 缺 10 项 |
| 坏女孩 | **18** | 缺 10 项 |
| 下个，路口，见 | 22 | 缺 3 项 |
| 一样的月光 | 22 | 缺 3 项 |
| 那天下雨了 | **24** | — |

老片缺的 10 项包括 `no placeholder / debug text on screen`、`every shot reference resolves`、
`film diverges >= 9/12 axes`、`character assets drawable + real alpha` 等 ——
**正是上一轮才补上的那两条**。

所以"《发如雪》18/18 PASS"与"《那天下雨了》24/24 PASS"**不能并列陈述**，
前者连"画面上有没有调试字"都没查过。而《发如雪》恰恰就是 `ember` 打了「残光」的那首。

**还有一层**：上一轮新加的占位检查正则只认英文

```js
/no shot|missing scene|undefined|NaN|Infinity/i
```

「残光」两个字**不匹配**。也就是说，即使把 24 项全部补跑一遍，
`no placeholder` 这条**仍然不会报警** —— 它只能抓"引擎自己的英文兜底文案"，
抓不到"另一部作品的中文字面量"。

**修法**：
- 短期：审计记录里必须带 `checks.length` 与生成的 commit，跨片比较时先对齐尺子；
  老片要么重跑，要么在文档里明确标注"18 项版本"；
- 中期：把占位检查改成**白名单制**——`hook fillText` 收集到的每一条字符串，
  必须能在 `cfg.title / cfg.titleSub / cfg.artist / cfg.credit / lyrics / glyphs / tunnelWords /
  typeLines / signature[].note` 里找到，否则报警。这比正则黑名单强得多，
  一条就能覆盖"残光"、"undefined"、"no shot"所有情况。

**验证手段**：`render/audit_*.json` 的 `checks.length` 逐片实测（见上表）。

### 1.6 `cadence` 是死轴：manual 模式下恒为空串

这一条独立成节，因为它是**代码 bug**（不是口径问题），且被 `FREE_AXES` 的声明掩盖了。

`cadence` 在 `signature()` 里是 `acts.map(a => a.cadence).join('')`（`direction.js:293`）。
而 `a.cadence` 这个字段**只有一个生产者**：`buildActs()` 内部的 `makeAct()`（`direction.js:221`，
`cadence: cadenceFor(look, st.density, rng)`）。

问题在调用侧 —— `songcut.timeline.js:332`：

```js
const acts = dir.structure.mode === 'auto' ? DIR.buildActs(cfg, lines)
                                          : (cfg.acts && cfg.acts.length ? cfg.acts : DIR.buildActs(cfg, lines));
```

`mode` 的默认值是 `'manual'`（`direction.js:47`）。一旦 manual 且有手写 `cfg.acts`，
就**直接用 JSON 里的数组**，`buildActs` 根本不跑。而手写 act 的键是
`{id,t0,t1,look,intensity,lut,pool,fx,trans}` 九个 —— **没有 `cadence`**。

实测五人组：

| 曲目 | `structure.mode` | acts | 带 `cadence` 的 act | 签名里的 `cadence` |
|---|---|---|---|---|
| `faruxue` | `manual`（默认） | 10 | **0** | `""` |
| `huainvhai` | `manual`（默认） | 8 | **0** | `""` |
| `xiageluokoujian` | `auto` | 9 | 9 | `323122131` |
| `yiyangdeyueguang` | `auto` | 7 | 7 | `3212113` |
| `natianxiayule` | `auto` | 10 | 10 | `3231221213` |

后果：`faruxue` 与 `huainvhai` 的 `cadence` 都是 `""`，`"" === ""` → **恒判"相同"**，
即使一首每句一切、一首八句一切。这正是 §1.4 里那对 `faruxue × huainvhai = 2/12` 的成因之一。

而 `direction.js:38` 的 `FREE_AXES = ['cadence', 'density']` 把这两轴标为"自由轴"
（官方 `02 §10` 表后 "Also vary pacing" / "density of typography"），
意味着门禁认为这两轴**一定会**贡献差异 —— 实际上 `density` 取的是
`String(d.structure.density)`（配置值 `2/2/1/3/2`，不是实测密度），`cadence` 在 manual 下恒空。
**两根"自由轴"都不自由。**

**修法**（推荐后者）：
- (a) 给两首老片的手写 acts 补 `cadence` 字段 —— 但这是人工填表，以后每加一首 manual 的歌都要记得填；
- (b) 让 `signature()` 在 `a.cadence == null` 时**从实际数据推**：
  用 `act` 覆盖的时长 ÷ 该区间内 LRC 行数，取整后夹到 1..4。这样 manual/auto 一视同仁，
  且反映的是"每几句一切"这个真实观感量。`density` 同样应该从实测镜头切点数推，而不是读配置。

**验证手段**：见上表（本轮用复刻 `songcut.timeline.js:317-333` 的脚本实测，五人全量）。

---

## 2. P1 — 应修（影响观感与可维护性）

### 2.1 排版库被继承了《残光》的 254 条禁令

`songcut.timeline.js:339-367` 的 `DENY` 表逐组与官方 demo 对比：

| 组 | 我们 | 官方 demo | 差异 |
|---|---|---|---|
| layout | 110 | 106 | 我们多禁 `condensed / glitchGrid / huge / shadowPlay` |
| fx | 34 | 34 | 完全相同 |
| decor | 56 | 53 | 多禁 `dataColumns / rainStreaks / timecodeBar` |
| cam / treat / enter / exit / bg | 8/8/5/3/21 | 同 | 完全相同 |

**254 条里 246 条（97%）是《残光》的偏好，被原样继承。**

后果（`render/_layoutdump.mjs` 实测）：

| 能力组 | 注册总数 | 禁后可用 | 本片实际用到 |
|---|---|---|---|
| layout | 186 | 75（40%） | **39（21%）** |
| decor | 130 | 74 | 3 |
| bg | 66 | 45 | 33 |
| fx | 69 | 35 | 7 内建 + 4 自定义 |
| treat | 62 | 54 | — |
| enter / exit | 125 / 109 | 120 / 106 | — |

而用户第一条需求原文是"**不要照搬官方案例、旧作品或现有视频模板**"。
`06 §5` 也写明 deny-list 是 "the STARTING point … adapt per film"。

**建议**：按段落重新过一遍 110 条 layout 禁令，把"残光式的叙事理由"与"通用的可读性理由"分开。
例如 `huge / condensed` 是为"大字溢出"禁的（真问题），`orbit / spiral / tunnel / circleWords`
是为"人物被字淹没"禁的（只对身体镜头成立，非立绘镜不必禁）。

### 2.2 chorus 段节奏比官方慢一档

官方（`05 §3`）：`chorus 0.9–2 s`、`verse 2.5–5 s`、`maxHold ~7 s`。

实测 57 镜：`median 3.25 s`、`p95 12.2 s`、`max 12.5 s`。

| 段落 | 我们的镜头长 | 官方建议 |
|---|---|---|
| chorus（48.39–84.71 等） | 3.0–3.4 s | 0.9–2 s |
| verse | 3.0–3.6 s | 2.5–5 s ✓ |
| 立绘窗口 | 8.0–12.5 s ×6 | ≤7 s ✗ |

根因：`songcut.timeline.js:582-592` 的切点**只来自歌词行首**（`step = act.cadence`，chorus 取 1 =
每行一刀），而中文慢歌一行约 3 s。**修 1.1 之后这里自然可以按拍切**
（chorus 一行 4–5 拍 → 2–4 刀），这也是 1.1 的连带收益。

### 2.3 超过 7 s 的六个立绘镜头，且注释与代码自相矛盾

文件头 `songcut.timeline.js:25` 自己写着 `09 §2 cuts on lyric onsets, no shot longer than 7 s`，
`:610` `maxHold = 6.4`，但 `:626` 的豁免把立绘窗口内的切点全删了：

```js
B = B.filter((t, i) => i === 0 || i === B.length - 1 || charEdge(t)
  || !charShots.some(w => t > w.t0 + 0.01 && t < w.t1 - 0.01));
```

实测 6 个 >7 s：`s001 9.6 / s008 12.2 / s019 12.5 / s027 12.5 / s044 8.2 / s053 8.0`，全是带 `chars` 的 illust。

豁免本身有理由（入场动画绑 `C.dur`，中间切会重播入场），但**没人记录这个例外**，
注释还在声称遵守 7 s。两个修法：
- (a) 把 `charParams` 的 `in/out` 从"镜头相对时间"改成"窗口绝对时间"，窗口内切点就不再重播入场（约 20 行，治本）；
- (b) 至少把注释改成"立绘窗口豁免 maxHold，共 6 例"，让审计能区分"违规"和"登记过的例外"。

### 2.4 两个碎片镜头

`s016 0.53 s`、`s018 0.80 s` —— 都是结构切点（live 窗 / 立绘窗）把前一镜截断的副产品。
`minShot = 0.62` 只管歌词切点，不管结构切点。官方 chorus 下限 0.9 s。
建议：结构切点也过一遍 `minShot`，被截短到 <0.9 s 的前一镜直接吞掉（并入再前一镜）。

### 2.5 竖版（9:16）有骨架但没接线

`engine/vertical.js`（120 行，官方原样）+ `vertical.html` 都在，但：

| 问题 | 证据 |
|---|---|
| 默认 timeline 指向不存在的文件 | `vertical.html:38` 默认 `tl=timeline.js`，我们只有 `songcut.timeline.js` |
| `Z.VERTICAL_CFG` 从未被设置 | grep 全项目 0 处 → 用残光默认：`title:'残光'`、`full:['s01','s02','s03','s64','s65','s66']`（我们是 `s001…` 三位格式，**一个都不匹配**，连结尾卡都会被裁成 6:5 图带） |
| `planMap` 只映射 4 个 plan | `{main,cf,noir,mono}`，我们有 7 个，`oncam/portrait/v1/v2/v3` 全部 fallback 到 `v_main`（立绘镜的中央预留语义会丢） |
| 渲染器默认 `#out` 是 `display:none` | `render_frames.mjs` 的 selector 需显式 `#vout`，否则 `getBoundingClientRect()` 为 0 |

官方 `08 §5` 要求 9:16 竖版交付。现在等于"官方能力完全没用上"。
工作量：配一份 `VERTICAL_CFG` + 扩 `planMap` + 给渲染器加 `--vertical` 开关（约半天）。

### 2.6 哈希清单与实际不符（我上轮的判断也要更正）

逐文件 `diff`（`\r\n` 归一化后）实测：

| 文件 | 变更行数 | 清单里被标为 |
|---|---|---|
| `engine/core/gl.js` | 6 | identical ❌ |
| `engine/core/main.js` | 21 | identical ❌ |
| `engine/index.html` | 21 | local_only ❌（上游 template 有同名文件） |
| `engine/vertical.html` | 4 | local_only ❌（同上） |
| `tools/render_frames.mjs` | 已登记 | modified ✓ |

所以真实是 **23 identical / 5 modified**，清单写的 `25 / 1`。
`docs/COMPLETENESS.md` §1「只有 1 个官方文件被修改」随之失效。

**更正说明**：我上一轮报告写"真实 23 / 3"，漏了 `index.html` 与 `vertical.html`
（当时误以为它们是"新增"，实际上游 template 里就有）。这次实测 diff 行数后更正为 23 / 5。

### 2.7 `Z.LOOKS[*].boil` 被写死值覆盖

`songcut.timeline.js:459-467` 定义了每段的 `boil: intro 0.7 / verse 0.9 / pre 1.0 / chorus 1.2 / bridge 0.8 / climax 1.3`，
`main.js:134` 也确实按 `Z.LOOKS[sec.look]` 合并进 post。但 `songcut.timeline.js:742` 的默认 post：

```js
boil: TYPO.has(scene) ? 0 : 1.0,
```

`Object.assign({}, base, sp)` 里 `sp` 覆盖 `base` → **17 个镜头被钉在 1.0**（实测分布：`1` ×17、
`0.6` ×9（livewin 写死）、`0.8` ×6（立绘 stage）、`0` ×3（排版）、`null` ×22）。
段落强度对 boil 的调节等于没用。

修法：`boil: TYPO.has(scene) ? 0 : (g.looks && g.looks[act.look]?.boil) ?? 1.0`。

### 2.8 `direction.type.plans = 6` 实际产出 7 个 plan

`songcut.timeline.js:431`：`const extraN = Math.max(0, (dir.type.plans || 6) - 3);`
固定 4 个（`main/cf/oncam/portrait`）+ `extraN` 个 → 写 6 得 7。
字段语义是"额外几个"还是"总共几个"，配置者无从得知。建议改名为 `extraPlans` 或改实现为 `plans - 4`。

### 2.9 preload 全有或全无 → 一张图缺失整页白屏

`assets.js:30` `await Promise.all(uniq.map(...))`，`decode()` 在 404 时 `throw`，
`main.js:197` `await Z.preload(urls)` **没有 try/catch** → 任何一张图 404，`window.ready` reject，整页白屏。

当前不触发（所有 preload 路径都存在），但 `kanjiSlam` 的 preset 默认指向
`assets/char/profile_sing.png` 与 `assets/bg/bg_sky.png`（**两者都不存在**），
一旦某首歌的 `lexicon` 引用 `kanjiSlam` 又忘了覆盖 `char`/`bg`，就是白屏而非"少画一个人"。

建议：`main.js:197` 改成逐张 `try/catch` 并计数，把缺失清单打进 `__cutAudit`，让审计报警而不是整页崩。

### 2.10 文档滞后（`COMPLETENESS.md` 的自述已过期）

| 自述 | 实际 |
|---|---|
| 18 项检查（§5）/ 22 项（§12.5） | 24 项 |
| 官方场景 11 个 | 9 个 |
| 5 个 JIZURA plan | 7 个 |
| `MAX_HOLD = 7 s`（§6） | 代码里 6.4，且有 6 个镜头突破 |
| live 6 窗 × 3.4 s（§11.3） | 9 个 live 镜头 / 20.4 s / 9.3% |
| 「只有 1 个官方文件被修改」 | 5 个 |

文档本身写得很好（有反证、有实测），但**数字没人跟着改**。
建议在 `cut.mjs` 出片成功后自动把本次 audit 的关键数字回写进文档末节，避免手抄。

### 2.11 转场是随机抽签，不是剪辑决定

`songcut.timeline.js:749`：

```js
const tt = (act.trans && act.trans.length) ? act.trans[Math.floor(rng() * act.trans.length) % act.trans.length] : 'cut';
```

每个镜头从所属段落的 `act.trans` 池里**按种子随机**取一个转场。
官方 `01 §4` 要求「a transition **when it's motivated**」，`examples/zanko/timeline.js` 是逐镜手写的
（`trans: { type: 'black', dur: 0.3 }` 明确挂在具体镜头上）。

我们的做法保证了"确定性"（同种子同结果），但不保证"动机"：
段落池里 3 个候选，同段落内 20 个镜头会被随机摊开，转场与前后画面无关。

实测分布（`audit_natianxiayule.json`）：`transitions` 8 类共命中 48 次，
`xfade / wipe / iris` 合计占多数，`shatter / irisClose / flash / cut` 一次没用。

**修法**：把 `act.trans` 从"候选池"改成"按段落内位置取"——段落首刀固定 `black`/`ink`，
段落收尾固定 `shatter`/`flash`，中间按强度插值挑。这不增加工作量，只是把随机换成语义。

### 2.12 `signature` 时刻表写好了，但没有任何代码消费

`songs/natianxiayule.json` 顶层有 5 条：

```json
[{"t":0.0,"note":"title card on the first bar (T21 quiet card)"},
 {"t":48.39,"note":"first chorus: per-beat kanji slam"},
 {"t":125.92,"note":"chorus 2 returns (ember + shatter)"},
 {"t":181.29,"note":"last chorus / climax"},
 {"t":208.5,"note":"end on a retinal afterimage of the title"}]
```

数量符合官方 `01 §39`「5–10 signature moments mapped to times」。
但全项目 grep：`cfg.signature` **零引用** —— engine 用的是 `DIR.signature(cfg, acts)`（12 轴签名），
同名字不同物。这 5 条纯粹是注释。

**修法**（低成本、高收益）：让它们真的**钉住**镜头——
`t` 落在哪个镜头内，就强制该镜头的 `post.bloom / trans.type / lyric plan` 用"签名档"，
并在 `audit` 里检查"每个签名点确实有一个不同寻常的镜头"。这才是 `01 §59` 说的 "Mark the signature moments"。

---

## 3. P2 — 可选（收益明确但非必需）

| # | 项 | 说明 |
|---|---|---|
| 3.1 | 真 beat 网格之后的强度重调 | 0.68 s/拍下，`beatCA 2.0`、`beatBloom 0.14` 会明显过密，需按段落降一档 |
| 3.2 | 字形预热 | `typo.js` 的 `glyphArt()` 懒光栅，首次命中 218–280 ms；`init` 里预光栅全部 glyph×style，8 个镜头约省 18 s 墙钟 |
| 3.3 | `D.sprite` 的 `wave` 按 12 fps 缓存 | 现在每帧 90 次 `drawImage`，`wave.q = 12` 意味着 29/30 帧结果相同 |
| 3.4 | live 帧降到 960×540 | 画面里只占 486×872 一小窗，1080p 源是纯浪费，612 次解码的开销可砍到 1/4 |
| 3.5 | 关 `preserveDrawingBuffer` + `texSubImage2D` | `gl.js` 每帧 3 次 `texImage2D` 全尺寸重分配（约 25 MB/帧） |
| 3.6 | `prepare.py` 的 `CUTS` 绝对路径 / `SEEDS` 硬编码 | 加新歌要改代码；`CUTS = pathlib.Path(r'D:\岁己歌切')` 仍在 |
| 3.7 | `E = Z.ease` 未使用 | `songcut.timeline.js:29`，grep 计数 0 |
| 3.8 | 无 lint | 一条 `no-undef` 就能抓住上轮的 `side`；建议加 `eslint --no-eslintrc --rule '{"no-undef":"error"}'` 到 `cut.mjs` 前置步骤 |
| 3.9 | `package.json` 无 `cut` 入口 | 现在只能 `node cut.mjs` |
| 3.10 | 场景参数契约 4 处不符 | `panels` 传 number（`songcut:257`）、`burst` 空 `cuts`（`:259`）、`solid`/`jz` 传了它们不读的 `chars`（`:184/:186`）。当前词表未引用，属潜伏 |
| 3.11 | `memories` 场景会崩 | `sceneArgs` 若给它传 `photos: string`，`meta.js` 里 `for (const P of a.photos)` 会逐字符迭代。当前未引用，潜伏 |
| 3.12 | 立绘窗口内切点（与 2.3 的 (a) 同源） | 窗口 8–12.5 s 内插一刀，需要 in/out 绑窗口时间 |
| 3.13 | `songs/` 目录混放三类文件 | 歌曲配置（`duration`+`lrc`）、场次配置（`2026-11-09.json`，键为 `date/source_video/whitelist/songs`）、调色片段（`*.palette.json`，约 450 B）。任何 `songs/*.json` 通配都会把后两类当歌曲读，实测 `probe_axes.mjs` 首版就因此吐出 5 个假的 0/12 行。建议按 `duration is number && lrc` 判真歌，或拆到 `songs/sessions/` |

---

## 4. 与官方规范的差距总表

| 规范 | 出处 | 我们的状态 | 判定 |
|---|---|---|---|
| `beats` = onset-snapped 节拍网格 | 03 §L49 | 97 个歌词行首（应 322） | ❌ P0 |
| `winning_phase` = 0..3 索引 | 03 §L51 | 1.6761（秒） | ❌ P0 |
| `downbeats` / `onsets_strong` 供场景读 | 03 §L53、L103 | 算了但从未读 | ❌ P1 |
| 不得残留另一部作品的字面量 | 09 §2 无死气/无占位 | ember/titleRiff 打「残光」 | ❌ P0 |
| chorus 0.9–2 s / verse 2.5–5 s | 05 §3 | chorus 3.0–3.4 s | ❌ P1 |
| 单镜 ≤ ~7 s | 01 §4 / maxHold 6.4 | 6 个 8–12.5 s | ❌ P1（有豁免但未登记） |
| 60–80 镜（3–4 min） | 05 §3 | 57 | ⚠️ 略少 |
| deny-list 需 per-film 适配 | 06 §5 | 继承残光 246/254 条 | ❌ P1 |
| 25 个官方场景优先适配 | 05 §8 | 用 9 个 | ⚠️ |
| `lyric:'none'` 只给排版场景 | 06 §6 | 17 处，全部合规 | ✅ |
| 每镜独立 post | 01 §4 | 57/57 | ✅ |
| 12 轴差异 ≥9 | 02 §10 | 10 对里 9 对达标（9–12），**`faruxue × huainvhai` 仅 2/12**；且轴值全部来自配置字符串，不看画面 | ❌ 见 1.4 / 1.6 |
| 中央を空ける 36% | JIZURA README:166 | `zoneW 0.36`，`outsideLive=0` | ✅ |
| 9:16 竖版交付 | 08 §5 | 有代码，未配置未产出 | ❌ P1 |
| 1–2 段 Blender 3D | 07 全文 | 一镜未用（`blender_*` 工具未引入） | ⚠️ 可选 |
| 角色 motion prop（飘带等） | 02 §75 | 立绘是静态素材，无 motion prop 概念 | ⚠️ 素材限制 |
| 官方源未被改动 | 哈希清单 | 实际 5 个 modified，清单写 1 个 | ❌ P1 |
| 无占位 / 无调试字 | 09 §2 | 上轮已修（24 项审计含两条新检查） | ✅ |

---

## 5. 外部可借鉴（调研结论，非本仓库）

| 项目 / 技法 | 对本管线的意义 | 建议 |
|---|---|---|
| **Mediabunny**（WebCodecs 的 mp4 muxer） | 现在 `VideoFrame → Encoder → POST 到 node → .h264 → setts 重打时间戳 → concat + mux` 五步，可在浏览器内一次 mux 带音轨的 mp4 | 值得试：能删掉 `setts` 与 `concat` 两步，`render_frames.mjs` 的补丁也少一半 |
| **pdoom-video**（逐帧自适应子帧） | 解决"高复杂度帧拖慢整片"的思路 | 我们不缺吞吐（80 fps），暂不需要 |
| line boil 的"8 drawings 循环 + 法线正弦位移" | 我们现在的 boil 是**整屏 value-noise 位移**（`gl.js:83-87`，noise 周期 `res/34`，12 fps 重种），不是笔画抖动 | **不建议照搬**：我们没有笔画级几何（全部 rasterize 进 FBO）。但可把 noise 频率提到笔画尺度（8–12 px）让观感更像"线条抖"而非"果冻晃"，成本 2 行 |
| rough.js / 手绘风几何库 | 只对"新画的几何图形"有效 | 不适用（我们的素材是 PNG 与字形） |
| BudouX / 中日文断行 | 理论上改善行首孤立 | **本片无收益**：`lyrics_mv.lrc` 里中文标点 0 个，JIZURA 也不做禁则，没有触发现象 |

---

## 6. 建议执行顺序

**第一批（半天，修 P0，必须重出片）**
1. 1.3 残光字面量（`titleRiff` 补 `hud`/`glyphs`；`ember` 的 `intro.js:344` 参数化）
2. 1.2 `winning_phase` 改 0..3 索引
3. 1.1 `prepare.py` 生成真 beat 网格 + `lyricOnsets` 分家
4. 1.6 `cadence` 死轴：给手写 acts 补 `cadence` 字段，或让 `signature()` 在 manual 模式下
   从 `t0/t1` 与实际歌词行数推出实测 cadence（后者更好，不依赖人工填表）
5. 1.4 先把那条门禁改名为 `declared divergence` 并在文档写明口径（5 分钟，防止继续拿它当质量证据）
6. 重出《那天下雨了》，抽帧确认：kanjiSlam 打出 4 个字、`downPulse` 非 0、无"残光"

> 注意：第 4、5 项**不要动 `direction.js:309` 的判等语义**。那是第三批的事，
> 且必须与 `audit_official.mjs:190` 的门槛一起改成比例制，否则会"越修越红"（见 1.4 末的警告框）。

**第二批（1 天，观感）**
7. 2.2 chorus 按拍切（依赖第一批）
8. 2.4 碎片镜头
9. 2.7 `LOOKS.boil` 接线
10. 2.1 重新审议 110 条 layout 禁令（先列一张"禁令 → 理由 → 是否只对身体镜头成立"的表给用户过目）
11. 2.11 转场从随机抽签改成语义化（段落首/尾/中间三档）
12. 2.12 signature moments 真正钉住镜头

**第三批（1 天，工程）**
13. 1.4 的 (b)/(c) + 判等语义 + 门槛口径**三处同时改**（改成 `differ / known` 比例制）
14. 2.9 preload 容错
15. 2.6 重生成哈希清单（23/5，含 `why`）
16. 3.8 lint 前置
17. 2.10 文档数字自动回写

**第四批（可选）**
18. 2.5 竖版接线
19. 3.2 / 3.3 / 3.4 性能
20. Mediabunny 试点

---

## 7. 本期核实方法

| 手段 | 脚本 | 用途 |
|---|---|---|
| 镜头元数据导出 | `tools/probe_shots.mjs` | 57 镜的时长/场景/plan/boil 分布 |
| 时钟体检 | `tools/probe_clock.mjs` | `bar/inBar/downPulse` 实际取值 |
| 节拍体检 | `tools/probe_beats.mjs` | 镜头内 beat 数 vs glyph 数 |
| JIZURA 能力普查 | `tools/probe_jizura.mjs` | 186 layout 用了几个 |
| **12 轴差异矩阵** | `tools/probe_axes.mjs` | 全 N×(N-1)/2 对、raw 与 strict 双口径、退化轴普查 |
| 抽帧 | `ffmpeg` / 引擎直渲 | kanjiSlam 是否只打一个字 |
| 逐文件 diff | `diff`（`\r\n` 归一化） | 哈希清单真实性 |

`probe_axes.mjs` 是本轮新增的，专治 §1.4/§1.6：`audit_official.mjs` 只把每片跟 `render/` 里现存
的片比一次并取最大值，所以「两首老片之间塌到 2/12」永远不进报告，且数字会随 `render/` 被清理而漂移。
新脚本每次都跑全矩阵，并把「两边都空」的轴单独列出来，避免 null-vs-null 被算成"相同"。

```bash
node official_pv/tools/probe_axes.mjs            # 全量矩阵 + 退化轴普查
node official_pv/tools/probe_axes.mjs --gate 9   # 任一对低于 9/12 则 exit 1（可接 CI）
```

实测输出（5 首真歌，已排除 `songs/2026-11-*.json` 场次配置与 `*.palette.json` 调色片段）：

```
-- faruxue x huainvhai : only 2/12 --
   genuinely differing : palette, motif
   blank on both sides : world, timeArc, material, role, cadence
```

四个脚本已经放进 `tools/`（不再是临时文件），可随时重跑复现：`node tools/probe_clock.mjs` 等，
都只读不写。`probe_beats.mjs` 会往 `render/_probe_beats/` 落几张抽帧。

---

## 8. 被否证的结论（记下来，避免下次重犯）

本轮排查中一度认为是问题、实测后**不成立**的四条：

| 一度认为 | 实测 | 结论 |
|---|---|---|
| `vertical.html` 缺 mulberry32 确定性注入，竖版不可复现 | `vertical.html:14` 有注入，与 `index.html:14` 完全相同；我之前读 diff 时把注释行当成了代码行 | 否证 |
| 两个 `?tl=` 白名单正则不等价（`[^\w./-]` vs `[^\w./_-]`） | 实测 `'songcut.timeline.js'.replace(...)` 两者输出相同——`\w` 本身包含 `_` | 否证 |
| `songs/xiageluokoujian.json` 顶层 `char` 指向不存在的图 | 该键 `items: []` 为空数组，是空壳死键，不是断链 | 否证（降级为死键） |
| `audit_*.json` 里的成片尺寸是编造的 | `cuts/` 目录当前为空（mtime 2026-11-15 12:53），文件是被清理掉的，不是审计撒谎 | 否证（改为"无法复核"） |

另外两条是**排查过程中由协作者提出、经我逐条核实后否证**的，一并记下（因为它们看起来都很像真 bug）：

| 一度认为 | 实测 | 结论 |
|---|---|---|
| `vertical.html` 的 `?tl=` 白名单 `[^\w./-]` 不含下划线，会把 `songcut.timeline.js` 改写成 `songcuttimeline.js` 导致 404 | `\w` 本身包含 `_`，实测两种写法输出完全相同 | 否证（真问题只是默认值 `tl=timeline.js` 指向上游文件名） |
| 5 首歌的配置全部没有 `direction.axes`，5 个轴恒 `null`，`diff` 上限 7 < 门槛 9，门禁数学上永不可达 | 3 首主力片（`natianxiayule`/`xiageluokoujian`/`yiyangdeyueguang`）**都有** axes 块；只有 2 首 11-09 旧片没有 | 否证（但"奖励写文档"的不对称是真的，见 1.4） |
| `signature` 只有 5 条，低于官方 6–10 的下限 | 官方 `01 §39` 原文是 **5–10**，5 条合规 | 否证（真问题是它没人消费，见 2.12） |

同时更正我上一轮的两处：

- 真实 modified 数是 **5** 不是 3（漏了 `index.html`、`vertical.html`，它们在上游 template 里存在）；
- 上轮说"`ref` 的 `meltAt/meltEnd` 原来传的是分数"，这个判断成立，但**只对 `melt` 模式**；
  `freeze` 模式不读这两个值，所以影响面比当时描述的窄。

**还有一条是本次报告内部的自我更正**（写在这里，因为它最容易被下一手当结论引用）：

| 我一度写成 | 实测 | 结论 |
|---|---|---|
| "两首都没写 axes 时 12 轴最多只能差 8 轴，**门槛 9 在结构上就够不到**" | 上界 8 是对的，但**门槛够得到**：只要一方写了 axes 就能到 9–12，10 对里 9 对通过；真正塌陷的是两首老片之间 **2/12** | 上界正确，**结论方向错**。改成实测表 + 补出「`cadence` 在 manual 模式恒空」这个真 bug（见 1.4） |
| 12 轴与官方 `02 §10` 是同一张矩阵，"12 对 12" | 官方有 `line style`、`3D role` 两行我们没有；我们的 `cadence`/`density` 在官方是表外散文 | 否证：数字巧合（见 1.4 对照表） |

教训：`DIR.diff` 这类"配置层判等"的问题，**静态推理只能给上界，必须跑真实数据**。
我第一次就是用上界替代了实测，把"某一对塌到 2"错说成"全部门槛不可达"。

---

## 9. 交付目录当前状态（与代码无关，但影响复核）

`D:/岁己歌切/cuts/` 当前**完全为空**（目录 mtime `2026-11-15 12:53`）。因此：

- 本轮无法对成片做字节级复核（上一轮记录的 6594 帧 / 944 MB 等数字属于历史快照）；
- `render/audit_*.json` 里的 `file_size` 断言在当前磁盘上找不到对应文件；
- `README.md` §2 与 `manifest.json` 里的交付清单同样只能视为历史记录。

按目录边界铁律，`cuts/` 顶部属于另一条并行渲染路径，本轮**未做任何读写**，仅 `ls` 观察。
后续重出片时，`cut.mjs` 会重建 `cuts/<date>/official/`。

---

## 10. 第一批 P0 修复记录（2026-11-15 晚，代码已落盘，**未渲染**）

按用户指示：**只改代码，先不渲染**；两首老片的 2/12 塌陷**只记录**，等重做那两首时一并处理。
因此本节只承诺"代码正确 + 静态可验证"，不承诺"成片已好"。

### 10.1 改了哪五处

| # | 文件 | 改动 | 对应条目 |
|---|---|---|---|
| 1 | `tools/prepare.py` | `beats` 从「歌词行首+中点」改为「标称 BPM 网格 + 反拍吸附 onset」；`lyricOnsets` 分家 | §1.1 |
| 2 | `tools/prepare.py` | `onsets_strong` 从裸浮点改为 `[{t,strength}]`，匹配 `intro.js:64` 唯一消费者 | §1.1 补记 |
| 3 | `tools/prepare.py` + `engine/core/main.js` | `winning_phase` 改为 0..3 索引；秒值另存 `phase_seconds`；`main.js` 新增 `beatPhase()` 兜底旧 json | §1.2 |
| 4 | `engine/scenes/intro.js` + `engine/songcut.timeline.js` + `engine/scenes/typo.js` | 残光字面量全部改为「跟随当前片」：`Z.TITLE / Z.TITLE_SUB / Z.CREDIT / Z.audio.bpm` 驱动，未知字段留空不编造 | §1.3 |
| 5 | `engine/direction.js` + `engine/songcut.timeline.js` | `cadence` 从「读配置」改为「按真实镜头数实测」；`signature(cfg, acts, ctx)` | §1.6 |

### 10.2 残光字面量：逐处清单

`intro.js` 里**七处**会把 残光 印到画面上，全部改为 film-aware（`Z.TITLE` 未设或等于 `残光` 时
逐字节还原上游示例）：

| 位置 | 原来 | 现在 |
|---|---|---|
| `:345` `emberHud` | `'残光 / ZANKŌ'` 写死，args 无法覆盖 | `args.hudTitle` → `Z.TITLE / Z.TITLE_SUB` |
| `:801` 巨石字形 | `['残','光']` | `args.glyphs` → 片名头两个汉字 |
| `:841` 注音 | `['ざん','こう']` | 非残光片默认关闭（args.ruby 可开） |
| `:875` 四角 HUD | `「残光」ZANKŌ — afterglow` / `BPM 131` / `踏切 No.7` / `35°41′N 139°46′E` / `03:44` | 片名 / 真实 BPM / 日期 / 原唱 / 真实时长；**坐标与地名留空**（我们确实没有这些数据，编一个更糟） |
| `:893` 副标题 | `a.sub ?? 'afterglow'` | `a.sub ?? filmSub()` |
| `:1031/:1034` afterimage | `a.text \|\| '残光'` | `a.text \|\| Z.TITLE` |
| `:1086` 落款 | `a.credit \|\| '「残光」 — MV'` | `a.credit \|\| Z.CREDIT \|\| 「片名」 — MV` |

`typo.js` 的 `tunnel` 场景另有**两处**同类（当前五首未使用该场景，属预防）：
墙面文字池 `LATIN` 含 `'ZANKŌ — AFTERGLOW'`、大盘字下方硬编码 `fillText('ZANKŌ — AFTERGLOW')`。
现在非残光片的 `latin` 池为空、`latinSub` 为空 → 该装饰不绘制，而不是编造英文句子填位。

`vertical.js:30` 仍有 `title:'残光', sub:'ZANKŌ — afterglow', artist:'NikusonP'` 默认值 ——
竖版整体尚未接线（§2.5），本轮不动，留作该条修复时的同批改动。

### 10.3 验证（全部离线，未产生任何视频）

| 检查 | 方法 | 结果 |
|---|---|---|
| 语法 | `node --check` ×5 + `ast.parse` | 全部 OK |
| 整站加载 | 按 `index.html` 顺序在 node 里执行 20 个脚本 | 15 OK，2 失败均为 stub 能力不足（URL/`getElementById`），非代码错 |
| 节拍网格 | 用现成 `audio/song.wav` 复算 3b→3c | 320 拍 / phase_idx=0 / 反拍吸附 57% / 下标零错位 |
| 相位兜底 | 抽取 `main.js` 的 `beatPhase()` 实跑 | 旧值 1.6761 → 0 并 warn；整数 0 → 0；缺失 → 不 warn |
| film 默认值 | 抽取 `intro.js` 的 film 助手 + `songcut` 的 `FILM_HUD` 实跑 | 残光示例逐字还原；三首真片无「残光 / BPM 131 / 踏切 / 东京坐标」 |
| cadence | `tools/probe_axes.mjs` | `faruxue` `1111111144`、`huainvhai` `11111114`（此前双双为空串） |
| 12 轴矩阵 | 同上 | 10 对中 9 对 9–12/12；`faruxue × huainvhai` 2/12 → **3/12**（cadence 活了），仍低于门槛 |

### 10.4 还没验证的部分（必须靠重渲）

- **观感**：拍数从 97 变 320，`beatCA`/`beatBloom`/`beatZoom`/`beatBump` 的强度大概率过密；
  `meta.js` 的「每 N 拍切一刀」会真的按拍切，镜头数可能明显变化。**这必须看片才能定。**
- **`kanjiSlam` 是否真的打出 4 个字**：静态只能证明镜头内 beat 数从 1 变 4–5，出字要靠抽帧确认。
- **`downPulse` 是否非 0**：同上，需读 `probe_clock.mjs` 的运行时输出（该脚本需要渲染页面）。
- **`titleRiff` 巨石字形取片名头两字是否好看**：《那天下雨了》取「那 / 天」，《下个，路口，见》取「下 / 个」。
  这是机械规则，可能不是最好的两个字，需要看片后人工指定 `glyphs`。
