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
| **JIZURA** | 引擎可跑，风格不可控 | 自动分镜质量随随机波动（`jz_20` 读作 PPT）。**借鉴其「文字部件 + 自动编排」分层**，引擎自建。bundle 留在 `renderer/pv/vendor/`（已 gitignore），可作可选通道 |
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
