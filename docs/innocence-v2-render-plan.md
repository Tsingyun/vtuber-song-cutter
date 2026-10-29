# 《Innocence》v2 渲染方案 —— 「晨光绘本」(Dawn Picture-Book)

> 状态：开发就绪（含逐句意象映射 JSON：`docs/innocence-v2-imagery-map.json`）
> 日期：2026-10-29 ｜ 成片目标：3840×2160@60，与实唱时长 232.17s 对齐

---

## 0. 曲目认定（含一处必须修正的旧错）

| 项 | 内容 | 依据 |
|---|---|---|
| **原唱** | **Avril Lavigne（艾薇儿·拉维尼）** | 联网核实：收录于 2007 年专辑《The Best Damn Thing》，与好友 Evan Taubenfeld 共同创作 |
| **⚠ 旧错修正** | 旧 job 元数据写的 `n.k/IA` 是 **Vocaloid 同名曲**，与本曲无关。实唱 47 句歌词全部是艾薇儿英文原词（"Waking up I see that everything is ok"…） | 对照 timing.json 逐句歌词 + 歌词库比对 |
| 演唱版本 | 岁己SUI 直播翻唱（实唱） | 铁律：**画面标原唱，内容按实唱唱版** |
| 封面 | 《The Best Damn Thing》专辑封面（原唱） | 同上 |
| 曲风 | **钢琴抒情流行**（Pop Ballad），不是朋克摇滚 | 多方乐评一致：钢琴前奏→弦乐铺底→吉他鼓组渐入，人声温暖清脆、无音准修正 |
| 主题 | 纯真瞬间的幸福与挽留——"幸福到想哭"的脆弱感 | 乐评 + 歌词语义 |
| 实唱规格 | 232.17s / 47 句 / 138.46 BPM（双倍速感知 ≈ 69 BPM 抒情）/ 12 段曲式 | `pv__timing.json` + `tmp__pro_analysis.json` |

---

## 1. 歌曲理解（意象词库）

把 47 句歌词归并为 **8 个核心意象**，每个意象对应一组视觉器件（这是全方案的映射骨架）：

| 意象 | 出处（句号） | 语义 | 视觉器件 |
|---|---|---|---|
| **① 醒来** | 00, 14 | 睁眼、第一道光 | 破晓渐变 + 天光光束（windowLight）+ 柔焦缓推 |
| **② 初见** | 01, 14 | "人生第一次"、崭新 | 大留白、单字辉光、极慢 weightGrow |
| **③ 慢下来** | 02 | 停下脚步环顾 | 全片唯一一次横移长镜（slow pan） |
| **④ 小事物** | 03 | 让生活美好的小事 | 微装饰群（tinyStars / 光尘 motes），小字号密集 |
| **⑤ 闪耀纯真** | 06, 19, 33, 35, 40 | brilliant | 金色辉光 halo + 日芒 rays + 巨字 |
| **⑥ 挽留** | 07-12, 36-39, 43-46 | 别走 / 抓紧 / 别让它溜走 | 拉扯感：字被轻微拖拽、边缘颤动、慢退场不舍 |
| **⑦ 安全之地** | 13-16 | 无泪、平静、归属 | 青瓷绿柔底 + 拱形庇护（arch）+ 安定居中排版 |
| **⑧ 美到想哭** | 26-32, 34 | 极乐、梦境、泪 | 梦紫水彩晕开（watercolor）+ 泪滴装饰 + 双行错位 |

**反复句（回访素材，P1-5 统一感直接吃到）**：
"This innocence is brilliant" ×5 ｜ "don't you let it pass you by" ×4 ｜ "I need you now" ×4 ｜
"And I'll hold on to it" ×4 ｜ "please don't go away" ×4 ｜ "This moment is perfect" ×3 ｜
"I hope that it will stay" ×3 ｜ "It's so beautiful, it makes you wanna cry" ×4 ｜ "I wouldn't change a thing about it" ×2 ｜ "This is the best feeling" ×2

→ 同句复现时**版面家族一致、配色随段落走**（副歌更亮、桥段转梦紫），既统一又不呆板。

---

## 2. 核心创意：从「故障印刷」到「晨光绘本」

v1 的视觉语法是 JIZURA 的**故障印刷 / 动态排版**（暗底、色差、碎字、锐利转场）——那套语法适配电子/摇滚曲，但与这首钢琴抒情曲的**温、柔、暖、真**完全相反。

**v2 全新逻辑 = 「晨光绘本」**：

- **画面像一本被晨光照亮的绘本**：奶油纸底、暖褐墨色、手绘翻页感（保留 koma=12 的"顿"，它恰恰是绘本翻页的质感，不是 bug）。
- **光是第一主角**：所有情绪起伏都用「光」表达——破晓→晴昼→辉→梦→暮，而不是用「打击感」表达。
- **字是被捧在手心里的**：不甩、不炸、不 glitch；入场是"浮现/托起"，退场是"不舍地淡去"。碎块（piece）只在"泪"与"挽留不住"两处破例使用。
- **一句话记忆点**：整支片子像清晨醒来翻开的一本相册，每一页都是"这个瞬间真好，别让它溜走"。

---

## 3. 色彩系统（新增 pack：`dawn`，5 套 scheme）

沿用 `pv_scheme.js` 的 pack/scheme 模型，新增一个 pack。全部为**浅底暖色**（与 noir/crimson 的深底形成段落对比的反面）。

| scheme | 名 | 用于 | bg | fg(墨) | accent | accent2 | glow |
|---|---|---|---|---|---|---|---|
| 0 | **破晓** | 片头 / V1 / Pre | `#F7F0E4` 奶油 | `#4A3527` 暖褐 | `#E8833A` 杏橙 | `#6B9BC9` 晨蓝 | `#FFD9A0` |
| 1 | **晴昼** | V2（安全之地） | `#EFF4EE` 青白 | `#2F4438` 松墨 | `#5B9A7E` 青瓷 | `#D9A441` 蜜金 | `#DCEBDD` |
| 2 | **辉** | 全部副歌 | `#FFF3DC` 金奶 | `#5A3B12` 焦糖 | `#E4A11B` 辉金 | `#E86A5C` 珊瑚 | `#FFE9B8` |
| 3 | **梦** | 桥段 | `#F2ECF7` 淡薰 | `#443361` 藤墨 | `#B072C7` 兰紫 | `#7FA8E0` 梦蓝 | `#E7D9F5` |
| 4 | **暮** | 尾奏 / 收束 | `#EFD9BE` 暮杏 | `#6B4A2E` 深栗 | `#D96C3F` 落日 | `#8E6AA8` 暮紫 | `#F5C99B` |

- **ghost（色差重影）降到 0.5**：是"温柔的双影"不是故障；ghostA 取 accent、ghostB 取 accent2。
- **paper 纹理常开**（picture-book 质感），grain 0.6，scan 0。
- 段落间 scheme 切换时**软溶 1~1.5s**（不是硬切）。

---

## 4. 场景语法（意象 → 器件）

新增/复用器件清单（`pv_bg.js` / `pv_parts_ext.js`）：

| 器件 | 类型 | 语义 | 用在意象 |
|---|---|---|---|
| `BG.sunrise` | 新 | 地平线暖渐变 + 低角日晕 | 醒来 / 片头 |
| `BG.windowLight` | 新 | 斜向天光束 + 浮尘 | 醒来 / 初见 |
| `BG.halo` | 新 | 文字背后柔光晕 | 闪耀纯真 |
| `BG.watercolor` | 新 | 水彩晕开边缘（梦紫） | 美到想哭 |
| `BG.arch` | 新 | 拱形庇护轮廓 | 安全之地 |
| `BG.petal` | 新 | 缓落花瓣/光点 | 小事物 / 尾奏 |
| `BG.leak` | 复用 | 柔光泄漏 | 全片点缀 |
| `BG.dust` | 复用 | 光尘 | 小事物 |
| `BG.rays` | 复用（降强） | 日芒 | 辉（副歌高潮） |
| `D.stitchLines` | 新 | 缝线/手账边 | 绘本感 |
| `D.photoCorners` | 新 | 相册角贴 | 初见 / 小事物 |
| `D.tinyStars` | 新 | 微星群 | 小事物 |
| `D.teardrop` | 新 | 泪滴 | 美到想哭 |
| `D.softCorners` | 新 | 圆角框 | 全片 |

**布局（layout）选用原则**：只取柔和家族——`block` / `center` / `lowerThird` / `marginNote` / `scaleSteps` / `titleCard`；**禁用** `cropGiant`（裁切压迫感）、`keySplit`（锐利分割）等攻击性版面。人物镜（fig）继续走 FIG_SAFE。

---

## 5. 分段设计（12 段曲式 × 逐句）

段落边界取自 `tmp__pro_analysis.json`，句子取自 `pv__timing.json`（47 句，句号 = JSON 下标）。

### 片头（0 – 8.3s，钢琴前奏）
- scheme 0 破晓。`titleCard`：主词 **Innocence** 用 Fraunces 斜体辉金，副行 "Avril Lavigne ｜ vocal: 岁己SUI" 小字。
- `BG.sunrise` + `BG.windowLight`，光束角度缓慢移动（≈8°/10s）。
- 无碎块、无转场——从黑柔溶进来（1.2s）。

### V1（句 00–05，8.3–36.4s）「醒来 → 小事物 → 最好的感觉」
| 句 | 歌词 | 意象 | 画面 |
|---|---|---|---|
| 00 | Waking up I see that everything is ok | ①醒来 | 天光束扫过，字随光浮现（softIn + weightGrow），背景 sunrise |
| 01 | The first time in my life and now it's so great | ②初见 | 大留白单行居中，字极慢放大 1.00→1.03，photoCorners 四角 |
| 02 | Slowing down I look around and I am so amazed | ③慢下来 | **全片唯一 slow pan**：字左→右缓移，camera drift |
| 03 | I think about the little things that make life great | ④小事物 | 字号缩小 0.85，四周撒 tinyStars + dust，marginNote 版面 |
| 04 | I wouldn't change a thing about it | ⑥挽留 | 定格感：hold=none，字完全静止 2s（"不变"） |
| 05 | This is the best feeling | ⑤闪耀 | halo 渐亮，center 版面，进入副歌前的第一次辉光 |

### C1（句 06–12，36.4–67.6s）「brilliant → 挽留」
- scheme 2 辉。转场：soft wipe（暖金边）。
- 06 "This innocence is brilliant" → **辉字**：halo + rays（降强 40%）+ scaleSteps 放大节奏。
- 07 "I hope that it will stay" → 字轻微向后拖拽 3px（挽留的拉扯感）。
- 08 "This moment is perfect" → 对称 center，镜像光斑左右各一。
- 09 "please don't go away" → 边缘 0.5px 颤动（wobble）。
- 10 "I need you now" → 向前 push（camera 2%），字重最粗。
- 11 "And I'll hold on to it" → block 版面，字距收紧（"抓紧"）。
- 12 "don't you let it pass you by" → 退场用 **pFall 慢速变体**（碎块缓落 = 时间流逝），这是全片第一次破例用碎块。

### V2（句 13–17，70.7–91.3s）「安全之地」
- scheme 1 晴昼，`BG.arch` 拱形庇护 + 青瓷底，相机几乎不动（安定）。
- 13 "I found a place so safe not a single tear" → arch 居中，字嵌在拱内。
- 14 "The first time in my life and now it's so clear" → ②初见回访（版面同 01，配色换晴昼）。
- 15 "Feel calm I belong I'm so happy here" → 三词分三行渐次浮现（calm / belong / happy）。
- 16 "It's so strong and now I let myself be sincere" → 字重最粗的一句（sincere），accent2 蜜金下划光。
- 17 "I wouldn't change a thing about it" → 回访句 04（P1-5 统一感）。

### C2（句 18–25，91.9–124.1s）
- 回访 C1 全部版面家族，但**更亮**：halo 强度 +20%，rays 半径 +15%。
- 25 "don't you let it pass you by" → 碎块缓落再次出现（呼应句 12）。

### Br（句 26–32，126.4–157.2s）「美到想哭」
- scheme 3 梦。转场：水彩晕开（watercolor ink）。
- 26-27 "state of bliss… happiness inside" → 双行错位（上行右移、下行左移），float 慢漂。
- 28 "It's so beautiful, it makes you wanna cry" → **泪滴装饰 + 字下缘柔化**（模糊 2px），teardrop 缓落。
- 29-32 重复句 → 逐次**去饱和 + 放大**（像眼泪里的世界），句 32 "cry" 独占一行巨字。

### FC（句 33–46，157.3–215.3s）「最终副歌」
- scheme 2 辉（全片最亮）→ 末段渐入 scheme 4 暮。
- 33/35/40 "This innocence is brilliant" → 同 06 版面家族，但每重复一次 halo 半径 +12%（记忆点递进）。
- 34 "it makes you wanna cry" → 梦紫残影一闪（桥段余韵）。
- 36-39 / 43-46 挽留句群 → 回访 C1 对应版面；**46 "don't you let it pass you by" 用 pFall 慢速**，字碎块飘落的同时暮色渐深。
- 44-46 配色由辉金渐溶到暮杏（时间真的在走）。

### 尾奏（215.3 – 232.17s）
- scheme 4 暮。字全部退去后只剩 `BG.petal` + 微光，最后 3s 柔溶入黑。
- 尾版小字："Innocence — Avril Lavigne ｜ 歌切：岁己SUI ｜ 20261029"。

---

## 6. 字体与排印

| 角色 | 字体 | 用法 |
|---|---|---|
| 强调词（Innocence / brilliant / cry） | **Fraunces Italic**（温柔衬线斜体） | 辉金、加大、halo |
| 正文歌词 | **Nunito**（圆润无衬线，SemiBold） | 主体字 |
| 档案/标签小字 | **Nunito Mono / JetBrains Mono** | 日期、署名、小注 |

- 歌词全英文 → **Latin 排印这次是主角**：词距 0.02em、行距 1.35，长句用 `scaleSteps` 保持 4K 下 ≥96px 可读。
- weightGrow（100→900）用于 00/02/14 三处"渐次看清"的句子。
- 全部左/居中对齐，**不用竖排**（英文竖排读作乱码）。

---

## 7. 运动语言

| 参数 | 值 | 理由 |
|---|---|---|
| koma | **12**（保留） | 手绘翻页感 = 绘本；60fps 连续会变成"网页动画" |
| 入场主用 | softIn / fadeSlow / dropBounce(弱) / pAssemble(仅泪/碎) | 浮现与托起，不甩不炸 |
| 退场主用 | fadeUp / blurOut / pFall(慢速, 2 处) | 不舍地淡去 |
| hold | breathe(0.6) / drift(弱) | 呼吸感 |
| 相机 | push(≤2%) / drift / float | 极轻微，"慢下来" |
| 转场时长 | 0.34~0.5s | 比 v1 略长，柔和 |
| 节拍吸附 | tol 0.18s（沿用 P1-6） | 边界贴半拍网格 |

**转场偏好权重**（P0-1 的 10 种里选）：soft wipe(4) / watercolor ink(3) / flash 柔化变体(2) / iris(2) / gentle push(2)；**禁用** blinds / clock / blocks（机械感太重，破坏绘本气质）。

---

## 8. 技术规格

| 项 | 值 |
|---|---|
| 分辨率/帧率 | 3840×2160 @ 60fps，yuv420p，bt709 |
| 码率 | 16000~18500 kbps（>19000 必二压） |
| 音频 | AAC ≤320k，与实唱轴对齐 |
| 时长 | 232.17s（= 原曲官方时长判据） |
| koma | 12 |
| ghost | 0.5（柔双影），chroma 偏移 -2.2/-0.9 与 2.0/1.2 |
| grain / paper | 0.6 / on |
| 确定性 | 同 seed 同 t 字节一致（pv_stills.cjs 验收） |
| 元数据 | artist = **Avril Lavigne**（修正 n.k/IA）、vocal = 岁己SUI、date = 20261029 |
| 封面 | 《The Best Damn Thing》专辑封面（原唱铁律） |

---

## 9. 开发落地清单（文件级）

| # | 文件 | 改动 | 优先级 |
|---|---|---|---|
| 1 | `pv_scheme.js` | 新增 `PACKS.dawn`（5 schemes，ghost 0.5，paper on） | P0 |
| 2 | `pv_bg.js` | 新增 sunrise / windowLight / halo / watercolor / arch / petal 六件 | P0 |
| 3 | `pv_parts_ext.js` | 新增 stitchLines / photoCorners / tinyStars / teardrop / softCorners | P1 |
| 4 | `tools/pv_direct.py` | 新增 `--concept dawn` 意象驱动模式：读 `innocence-v2-imagery-map.json` 直出编排稿（不再抽卡）；PERS 加 dawn 人格 | P0 |
| 5 | `docs/innocence-v2-imagery-map.json` | 逐句意象→器件映射（本方案附带，已写） | P0 |
| 6 | 渲染 job | artist 修正 + cover 修正 | P0 |
| 7 | RULES.md | 追加"曲目认定必须逐句比对歌词语言，同名曲不等于同曲"四段式条目 | P1 |

**实现顺序建议**：1→5→2→4→3→6→渲 → 验收。渲染量与 v3 相当（13,931 帧 ≈ 11 分钟 @21.6fps）。

---

## 10. 验收标准

1. `pv_check.py`：时长 232.17s±0.05 / 4K60 / 码率 / 纸底纯色残留内容四项判据（paper 判据对本片**例外**——绘本底就是纸底，需在 manifest 标 `paper_ok: true`）。
2. `pv_fdiff.py --koma 12`：0 静止镜。
3. `pv_stills.cjs`：确定性字节一致。
4. 逐句目视抽查 ≥8 句（覆盖 8 个意象各至少 1 处），核对：意象器件出现、配色段落正确、反复句版面家族一致。
5. 元数据：封面/ORIGINAL = Avril Lavigne（不再是 n.k/IA）。
