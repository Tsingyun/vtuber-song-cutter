# 官方能力逐项补齐与源码核对记录

> 这份文档回答一个问题：**最终成品到底有没有真的用上官方 `vocaloid-style-mv-pipeline` 的能力？**
> 结论由两类证据支撑：① 与官方源码的逐字节哈希核对；② 浏览器里真跑一遍引擎后的运行时审计报告。
> 不是"读文档觉得应该能用"，也不是"代码里 import 了就算用上"。

---

## 1. 核对基准

| 项 | 值 |
|---|---|
| 上游 | `https://github.com/EGSECDA/vocaloid-style-mv-pipeline` |
| 本地官方克隆 | `D:\岁己歌切\_ref\vocaloid-style-mv-pipeline` |
| 克隆 HEAD（实测 `git rev-parse HEAD`） | `13b380dab077d4c8ea956825c9693c720e0c2b91`（浅克隆） |
| 核对方式 | 对 `skills/vocaloid-style-mv/template/**` 与本系统同名文件做 sha256 逐字节比对 |
| 核对结果文件 | `docs/OFFICIAL_SOURCE_HASHES.json` |
| 官方参考文档 | `skills/vocaloid-style-mv/references/` 共 10 份（01-workflow、02-creative-direction、03-audio-and-lyrics、04-asset-generation、05-engine、06-jizura、07-blender-3d、08-render-and-delivery、09-qa-and-lessons、effect-cookbook） |

> 说明：早期构建脚本里曾写入 commit `1b13be4…`，该对象在**当前这份本地浅克隆中并不存在**（`git cat-file` 查不到）。
> 因此本系统改用可当场验证的 `13b380d` 作为核对基准，并把结果重新落盘；不再引用无法在本地复现的提交号。

### 逐字节核对结果

- **25 个文件完全一致**，含全部 6 个 `engine/core/*.js`、全部 8 个官方 `engine/scenes/*.js`
  （`scenes.js / intro.js / typo.js / glass.js / dark.js / night.js / memory.js / meta.js`）、
  `jizura/app/*` 以及大部分 `tools/*.mjs`。
- **只有 1 个官方文件被修改**：`tools/render_frames.mjs`（见 §4，两处补丁）。
- 官方 template 里另有 23 个工具/时间线文件未引入：主要是 `transcribe_*`（Whisper 转写链）、
  `blender_*`（3D）、`imagegen.py`（AI 出图）、`gigafile_upload.mjs` 等。
  这些属于官方**上游生产链**，不是渲染引擎能力；本歌切系统的音频来自已对齐的直播录播 + 官方 LRC，
  且按约定不使用 AI 生成图片，因此不需要它们。官方 Demo 时间线以
  `engine/__official_demo_timeline.js` 之名保留，可用 `?tl=` 直接加载做冒烟对照。
- **本地新增文件**：`cut.mjs`、`engine/index.html`、`engine/songcut.timeline.js`、
  `engine/scenes/livewin.js`、`tools/prepare.py`、`tools/audit_official.mjs`、`songs/*.json`、
  `assets/bg/*.png`、`assets/live/**`。全部列在哈希文件的 `local_only` 段。

---

## 2. 上一轮「官方 Demo 多变、我的版本单调」的诊断 → 逐项补齐

这是本次交付的核心整改表。左列是此前自查出的每一条根因，右列是本次的落点与**运行时证据**（不是静态代码断言）。

| # | 诊断出的缺失 | 官方机制 / 出处 | 本系统实现 | 运行时证据 |
|---|---|---|---|---|
| 1 | 官方 7~8 个场景文件、24 个注册场景**根本没被页面加载** | `05-engine.md §1`；官方 `template/engine/index.html` 脚本顺序 | `engine/index.html` 按上游顺序加载全部 8 个官方场景文件 | `8 official scene files loaded` PASS；`registeredScenes = 25` |
| 2 | `cutreplan.js` 把每个镜头强制改成 `livewin` / `plates` | `05-engine.md §8`：优先适配 24 个已注册场景 | 删掉强制改写；`songcut.timeline.js` 每个段落配置官方场景池，镜头 `scene` 全部指向 `Z.SCENES` 里的真实注册名 | `no missing scene names` PASS；`officialScenesUsed` = 11 个；`every distinct scene executed` PASS |
| 3 | 自写 `plates.js` 的 11 个 case 是"假多样" | 官方 24 个场景各有独立绘制实现 | 实际分派 `illust / solid / converge / kanjiSlam / tunnel / road / ref / ember / afterimage / jz / typeCard` | `sceneCounts` 见两份 audit JSON，最多的场景也只占约 1/4 |
| 4 | 为修 bug 把整类效果删掉了 | `02 §6`、`09 §3`：按段落**升级强度**，不得整类删除 | 11 类转场、9 类内建 fx、5 个自定义工厂、7 档 `Z.LOOKS`、每镜独立 `post` | `>=6 built-in fx`、`>=4 custom factories`、`>=5 transition types`、`every shot has post = 100%` 全 PASS |
| 5 | 没用官方 fx / post / trans / JIZURA 多 plan | `scenes.js:97-116`、`core/trans.js`、`core/gl.js`、`06 §3` | 每镜独立 `fxBack`/`fx` + `post` + 动机化 `trans`；5 套 JIZURA plan 在 `style / seed / fx / centerFree` 上真分化 | `fxTypes`、`customFactories`、`transitions`、`jizuraPlans`、`planCounts` 均有非零分布 |
| 6 | 误以为"没有图片素材只能用几何背景" | 官方 Demo 本身就是**零图片**；`effect-cookbook` 50+ 配方 | `illust` 同时走官方**渐变分支**与官方 **bg 相机分支**（`bgFrom/bgTo/beatZoom`）；6 张程序化背景板按歌命名空间，零 AI 图片 | 两首联系表可见冷月雪原／墨色山影／朱砂灯／霓虹雨／暗巷色块／聚光舞台 |
| 7 | JIZURA 多 plan 不够分化、`condensed` 重复同一句 | `06 §3/§5/§6` 的 `enabled` 白名单、`lyric:'none'` 只给排版场景 | 5 套 plan；`DENY` 白名单禁用 `condensed / huge` 等；`companion.scheme` 继承 `c.scheme` | `planCounts` 五套都有镜头；联系表歌词排版明显分化 |

---

## 3. 官方能力在本系统的落点（代码级）

| 官方能力 | 官方文件 | 本系统调用点 |
|---|---|---|
| 24 场景注册表 `Z.scene()` | `engine/scenes/*.js` | `engine/index.html` 全量加载；`songcut.timeline.js` 的 `act.pool` 分派 |
| 13 类转场 | `engine/core/trans.js` | 每镜 `trans`，按段落 `act.trans` 文法挑选 |
| 11 类内建 fx | `engine/scenes/scenes.js:97-116` | `builtin(type, cfg, act, i)` 按官方签名传参（含 `rays` 的 `x/y/len/a/seed`） |
| 自定义 fx 工厂 | 官方 Demo `timeline.js:50-128` 有 5 个 | `factories(cfg)` 每首歌 5 个确定性纯函数（雪/月/墨/朱砂/光尘；霓虹/雨玻璃/拨号环/…） |
| WebGL2 post | `engine/core/gl.js` | 每镜 `post` + `Z.LOOKS` 段落分档 + `Z.globalFX` 节拍闪白 |
| JIZURA 多 plan | `jizura/app/jizura_adapter.js` | `JZ.addPlan` ×5、`JZ.drawPlan`、`JZ.cutsOf` 排错 |
| LUT / 调色 | `assets/style/*.png` | 每镜 `lutA/lutMix`，audit 校验名称有效性 |
| 节拍锁定剪辑 | `09 §2` | 切点吸附真实 LRC 行起点；`analysis/audio.json` 的 beats 即这些 onset |
| 官方 Demo 的溜め/后像 | `intro.js` `Z.INTRO.black` / `afterimage` | 按官方语义使用（黑帧窗口是**绝对歌曲时间**） |
| master 规格 | `08 §1` | 1920×1080 / 30fps / quantizer QP20 / AAC 320k |

---

## 4. 所有非官方改动（全部记录在案，无隐藏补丁）

| 文件 | 性质 | 原因 |
|---|---|---|
| `tools/render_frames.mjs` | **修改官方文件（唯一一个）** | ① h264 裸流 remux 用 `setts` 重打时间戳，否则成片不可 seek；② 增加 `--wc-mode/--wc-qp`，让 WebCodecs quantizer 高码率真正生效（VBR/CBR 实测只有 ~6 Mb/s，QP20 约 30–40 Mb/s） |
| `engine/index.html` | 新增 | 官方脚本顺序 + `?song=`/`?tl=` 路由 |
| `engine/scenes/livewin.js` | 新增自定义场景 | 直播画面**短促点缀**（占比 7.2% / 8.8%），纯遮罩 + 等比 cover 裁切，零形变。含窗口帧号 `snap`，避免边界 404 |
| `engine/songcut.timeline.js` | 新增驱动 | 读 `songs/<id>.json` 生成 `Z.SHOTS`；官方场景分派、post/trans/fx/JIZURA plan、运行时审计数据 |
| `tools/prepare.py` / `audit_official.mjs` / `cut.mjs` | 新增编排 | 素材准备、运行时验收、一键交付 |
| `songs/*.json` | 新增配置 | 两首歌的段落、场景池、配色、直播窗口 |

**零形变硬约束**：直播帧只允许「等比缩放 cover + 裁切 + alpha 遮罩 + 滤镜/叠加」。
`livewin.js` 里所有 blit 都经过 `blit9`/`blit1` 单一 scale 因子；audit 读取 `Z.LIVEWIN.audit.violations`，
两首均为 **0**。

---

## 5. 运行时验收（关键：交付前真跑，不是交付后才说明）

`tools/audit_official.mjs` 打开真实页面、等 `window.ready`、读引擎自己的注册表，并**逐个渲染每个镜头的中点帧**。
共 **18 项检查**（QA round 2 新增「中央预留框只允许出现在岁己镜头」），任何一项 FAIL，`cut.mjs` 直接拒绝出片。

| 检查 | 《发如雪》 | 《坏女孩》 |
|---|---|---|
| 8 个官方场景文件加载 | PASS | PASS |
| 注册场景数 | 25 | 25 |
| 实际使用的官方场景 | 11 | 11 |
| 每个不同场景真执行 | PASS | PASS |
| **全片逐镜冒烟渲染**（新增） | 96/96 帧 | 55/55 帧 |
| 内建 fx / 自定义工厂 / 转场 / JIZURA plan | 9 / 5 / 11 / 5 | 8 / 5 / 11 / 5 |
| 每镜有 post | 96/96 | 55/55 |
| 直播画面占比 | 7.2% | 8.8% |
| 零形变违规 | 0 | 0 |
| 页面错误 / 404 | 0 | 0 |
| 真实 GPU | RTX 5070 Ti / D3D11 | 同左 |

报告路径：`render/audit_faruxue.json`、`render/audit_huainvhai.json`；交付时各复制一份到 `cuts/<date>/official/_audit_<id>.json`。

> 冒烟渲染是本次新增的：**只渲染每个不同场景一次是不够的**。
> 《坏女孩》曾在 t=60 因调色板缺 `gold` 键在 `motes` 工厂里抛 `Z.hex(undefined)`，
> 旧版审计查不出来；现在逐镜渲染会当场拦下。

---

## 6. 视觉 QA 记录（官方 `09-qa-and-lessons.md` 的 fix → re-review 流程）

| 轮次 | 联系表 | 发现 | 修复 |
|---|---|---|---|
| 第一轮 | `render/review_faruxue/pass1.jpg` | ① 0–22 s 一整张静止标题卡；② `t=160` 纯黑；③ `t=45` JIZURA `huge` 排版把六字放大到溢出不可读；④ 三张背景板仅亮度不同；⑤ `kanjiSlam` 白底 `day` 天空每段重复；⑥ 直播窗边界 6 个 404 + 空镜 | 见下 |
| 第二轮 | `render/review_faruxue_v2/pass2.jpg` | 开头已拆成 4 个镜头；纯黑消失；大字可读；出现圆环字/螺旋星云/水墨/朱砂/放射线 | 通过 |
| 直播专项 | `render/review_faruxue_live/live.jpg` | 404 清零，9 个直播窗全部出现人物、无形变 | 通过 |
| 《坏女孩》 | `render/review_huainvhai/pass1.jpg` | 冷青蓝+洋红霓虹、聚光舞台/粒子网格/放射线/多套歌词排版/直播人像 | 通过 |

具体修复：
1. `DENY.layout` 增加 `huge`（官方 `enabled` 白名单机制，删一个排版而非删一整类）。
2. `ember` 的 `stops` 从 `[[t0,t1]]`（整整一镜全黑）改成 2 帧溜め——`Z.INTRO.black()` 把 `stops` 当**绝对歌曲时间**读。
3. 新增 `MAX_HOLD = 7 s`：长镜头自动补切点，且不切进直播窗口。
4. `prepare.py` 重写背景板：每首歌 3 个不同母题，文件名按 song 命名空间，互不覆盖。
5. `kanjiSlam` 轮换官方 `SLAM` 变体（`night/default/hand`），白色 `day` 天空只留给 climax。
6. `Z.LIVE_WINDOWS` + `livewin.js` 的 `snap/valid`：边界帧回退到窗口内真实帧，消除 404 与空镜。
7. `PAL_DEFAULT`：补齐调色板缺省键，避免任一歌曲配置漏键导致运行时崩溃。
8. `prepare.py` 直播帧改为 `ffmpeg -start_number` 原地覆盖写入，去掉批量删除（沙箱会拦截）。
9. `cut.mjs` 修正 `kit_env.mjs` 的 import 路径（此前从未端到端跑过，第一次真跑才暴露）。

---

## 7. 交付范围

- 白名单（见 `songs/2026-11-09.json`）：**仅 `faruxue`（发如雪）、`huainvhai`（坏女孩）**。
- `manifest.json` 由 `cut.mjs` 生成，`songs` 数组**恰好等于白名单**。
- **《多余的解释》不纳入本系统切分、不出现在 `official/` 交付结果中**：它不在白名单里，不会被
  prepare / audit / render，也不会写进 `official/manifest.json`。
  ⚠️ 这条约束**只作用于 `official/` 子目录**，不构成对 `cuts/<date>/` 顶层的任何处置权；
  顶层是另一条平行、正在使用的渲染路径的输出区，其产物一律不碰（详见 10）。
- 歌手名标**原唱**：《发如雪》周杰伦（2005《十一月的萧邦》）、《坏女孩》徐良 × 小凌（2011《不良少年》）；
  歌词与切点按现场实唱版本对齐，两者解耦。

## 8. 已知边界

- `analysis/audio.json`、`analysis/envelope_30fps.json`、`audio/song.wav` 是**当前曲目共享路径**，
  由 `prepare.py` 每次重建。`cut.mjs` 强制「prepare → 审计 → 渲染」逐首串行，因此顺序执行时正确；
  单独对另一首跑 stills/audit 前需先 `prepare.py --song <id>`。
- 渲染依赖本机 GPU（Edge/Chrome + ANGLE），无 GPU 环境 audit 的「real GPU renderer」会 FAIL 并拒绝出片。
- 官方 23 个上游工具（转写 / Blender / AI 出图）未引入，理由见 §1。


---

## 9. QA round 2（2026-11-10）：用户反馈的三处裁切 / 开头静音

### 9.1 《发如雪》开头 12 秒没有伴奏 —— 波形实证与整体平移

用 `ffmpeg` 把成片解码成 16 kHz 单声道 PCM，按 0.25 s 窗算 RMS：

| 成片时间 | RMS | 判定 |
|---|---|---|
| 0.00–1.25 s | 0.03 → 0.35 | 上一首的尾音 / 切歌爆音，不是本曲伴奏 |
| 1.50–14.25 s | 0.0009 – 0.014（多数 < 0.002） | **数字静音**，源录播这段音频丢失 |
| 14.50 s 起 | 0.0589 → 持续 0.02–0.06 | 伴奏真正起音 |

源录播同样：`3600.0–3609.5 s` RMS ≈ 0.001，`3612.0 s` 起恢复。也就是说**录播在这首歌的起点之后有 14.5 s 死区**，
不是渲染器加的静音，也不是切点算错——SRT 锚点 `3619.77 「狼牙月…」` 与 LRC `22.02` 推得的 `abs_start=3597.75`
本来就正确。

修法：**掐掉死区，整条时间轴同步平移 −14.5 s**，而不是只挪音频。

| 项 | 旧 | 新 |
|---|---|---|
| `abs_start` | 3597.75 | **3612.25** |
| `duration` | 299.30 | **284.80** |
| LRC（113 行） | 22.02 … 284.02 | **7.52 … 269.52** |
| `acts` / `live` / `signature` | 原值 | 全部 −14.5（首个 act t0 clamp 到 0） |

因为 `录播绝对时刻 = 新 abs_start + (旧相对时刻 − 14.5) = 旧 abs_start + 旧相对时刻`，
**每一句歌词、每个镜头、每一帧直播插入仍锁在录播的同一个绝对时刻**，对齐关系零改变，
只是伴奏现在从成片 0 秒响起。`prepare.py` 新增 `lrcOffset` 配置（从原始官方 LRC 生成平移后的
`analysis/lyrics_mv.lrc` 与交付 LRC，原始 LRC 保持为 source of truth），并新增**开头静音硬门槛**：
`0.5–2.5 s` PCM RMS < 0.008 直接拒绝出片。实测新《发如雪》`0.0251`、《坏女孩》`0.0819`。

### 9.2 满屏大字被切 / 多阶段动画中央缺失 —— 同一个根因

**根因在 JIZURA 的 `centerFree`，不是画布裁切、不是 mask、不是直播框。**

源码链（`jizura/app/jizura_engine.js`，上游 MIT 组件）：

- `:2684` `centerFree: false, // 中央を空ける: lay the cuts out in side bands ... around a character`
- `:2944` `const zones = project.centerFree ? J.sideZones(W, H, project.centerDir, project.zoneW) : null;`
- `:3428` `J.sideZones` 宽屏默认左右各 **30%**、中间 **40%** 完全留给角色
- `:2946` `if (zones && en.bg) en.bg.bigChar = false;` —— 连满屏大字背景都被关掉
- `:3994–3999` `const Z = plan.centerFree && cut.zone ? cut.zone : null; ... X.rect(Z.x, Z.y, Z.w, Z.h); X.clip();`

也就是：**每个 cut 的文字/装饰/相机都被 `clip()` 到左右两条窄带**。旧 `songcut.timeline.js` 把开了
`centerFree: true` 的 `cf` plan 用在了 intro/outro、verse 的一般镜头以及 `jz` 场景上，于是：

- 《发如雪》「惹尘埃是非」的大字被硬 `clip()` 成左右两半（t=36.40 实测：右带大字的左半缺失）；
- 《坏女孩》t=7 以及多个阶段的动画/文字被挤到左右两侧，中间 40% 一条从上到下的空白——
  这正是用户指出的「预留给岁己出镜的框」，但它**在没有岁己镜头的时段也生效了**。

修复（全部在 `engine/songcut.timeline.js`）：

1. `cf` 去掉 `centerFree`，恢复全幅 JIZURA 排版（含 `bigChar`）；
2. 新增 `oncam`（`centerFree: true`），**只**赋给 `scene === 'livewin'` 的镜头；
3. 加**硬守卫**：生成 `Z.SHOTS` 前遍历所有镜头，凡非 `livewin` 却引用了任一 centerFree plan，
   一律改写回 `cf` 并计数；
4. `window.__cutAudit` 暴露 `centreFreePlans / centreReserveOutsideLive / oncamShots`，
   `audit_official.mjs` 新增第 18 项检查：centerFree plan 必须存在、`outsideLive === 0`、
   且 `oncamShots === liveShots`，否则拒绝出片。

实测两首：`centerFree=[oncam] outsideLive=0 oncam=12/12`（发如雪）、`8/8`（坏女孩）。
修复后抽帧复核：《发如雪》`t=21.4/21.9/22.4`（平移后的「惹尘埃是非」）大字整行完整、
`t=121.2` 第二次副歌大字完整；《坏女孩》`t=7` 中央变成连续的 `do / 坏女孩 / n't` 大字排版，
`t=190` 透视舞台与中央文字连续，均无竖直空白带。岁己镜头（`t=10.5 / 50 / 84 / 135` 等）
仍保持 centerFree，歌词分列两侧、中间是直播人像——只在真的有岁己出镜时才留白。

### 9.3 本轮改动文件

`engine/songcut.timeline.js`、`tools/audit_official.mjs`、`tools/prepare.py`、
`songs/faruxue.json`、`songs/2026-11-09.json`、`docs/COMPLETENESS.md`、`README.md`。
官方上游文件未再改动；`engine/core/*`、8 个官方 `scenes/*`、JIZURA bundle 仍与上游逐字节一致。

---

## 10. 目录边界：与另一条平行渲染路径共存（RULES.md 129）

2026-11-10 事故（仓库 `RULES.md` 129 已完整记录，此处只记我方整改）：

- 同一仓库被**两条独立、并行、都在使用**的交付链共用：
  `song_cutter.py` / `auto_cut.py` / `renderer/`（A 面，4K60）与 `official_pv/cut.mjs`（本系统，1080p30）。
- 我在 03:14 把 `cuts/2026-11-09/` 顶层那批**正被 A 面使用**的中间件
  （`.mp3`、`.lrc`、`manifest.json`、`cutter.log` 等）误判为「误生成的旧结果」搬到
  `cuts/_legacy_2026-11-09/`；03:29 A 面渲完 16032 帧混流时找不到 mp3，
  `finally` 又清掉 573 MB 画面中间件 ⇒ **80 分钟渲染全部作废**。
- 11:29 已将两批共 7 个文件**原样放回** `cuts/2026-11-09/` 顶层，未覆盖任何现存文件；
  我自建的 `_legacy_*` 目录及其中早期副本保留不动，交由用户处置。

**本系统此后的硬边界（代码 + 文档双重固化）**：

| 允许 | 禁止 |
|---|---|
| 写 `official_pv/` 内部文件 | 移动 / 重命名 / 删除 / 覆盖 `cuts/<date>/` 顶层任何文件或目录 |
| 写 `cuts/<date>/official/` 子目录 | 把顶层产物称为「旧管线遗留 / 误生成结果」并做「清理」「隔离」 |
| 只读列出顶层并打印并发提示 | 建议或执行「停掉那条管线」；动那条路径的项目文件与文件夹 |
| 在 `official/manifest.json` 排除非白名单曲目 | 因白名单排除而对顶层文件采取任何动作 |

`cut.mjs` 落地：`deliveryDir()` 从 `ROOT` 父目录推导并强制 basename 恒为 `official`，两处 `outDir` 全走它；
启动时**只读**扫描顶层并打印一行并发提示，绝不写入。
「《多余的解释》不出现在结果中」中的「结果」= **仅 `official/` 子目录**，与 A 面产物无关。

---

## 11. 2026-11-10 三首：date 参数化后的第一个新场次

用户给的合并录播在 `D:/录播/25788785-岁己SUI/2026-11-10/`（8.06 GB，1920x1080 / 60fps / HEVC，
22416.95 s，5 段 flv 已合并为单一连续时间线）；SRT 落在
`D:/岁己直播自动总结/SRT/20261110-陪你这个猪过周二！ 2026年11月10日20点场.srt`。

### 11.1 切点是实测出来的，不是猜的

`tools/probe_cuts_1110.py` 用 SRT 歌词首句与官方 LRC 对应行组成锚点对，取中位数抵消
ASR 块粒度误差；再用 ffmpeg 解出的真实 PCM（0.05 s 窗 dB 曲线）找数字静音谷定出点；
开头死区则整体平移（与 11-09《发如雪》QA2 同一手法，音频 / LRC / acts / live /
duration 一起移动，歌词与录播的绝对对齐不变）。

| 曲目 | 锚点中位数 | 头部平移 | 最终 abs_start → abs_end | 时长 | 开口 RMS |
|---|---|---|---|---|---|
| 那天下雨了 | 5787.80（7 锚点，极差 2.26 s） | +3.40 s | 5791.20 → 6011.00 | 219.80 s | 0.0509 |
| 一样的月光 | 6291.77（3 锚点，极差 0.35 s） | +5.00 s | 6296.77 → 6525.60 | 228.83 s | 0.0185 |
| 下个，路口，见 | 6698.61（4 锚点，极差 0.29 s） | 0 | 6698.61 → 6904.00 | 205.39 s | 0.0685 |

出点依据：6011.00 的 -114 dB 数字静音（之后 6012 起说话）、6525.60 的 -62 dB 渐弱谷
（之后 6526 起说话与补唱）、6904.00 起持续 27.6 s 的 -180 dB 绝对静音。三首尾奏都比
官方原曲短 5–7 s（主播提前收伴奏），与 11-09《多余的解释》同理，歌词内容无缺失。

### 11.2 同名双曲的判定

SRT 首句「倔强的表情 / 一样的月光，怎么看得我越来越心慌」→ **徐佳莹 2009 原创版**
（《LaLa 首张创作专辑》），不是苏芮《搭错车》1983 版。成片 ORIGINAL 标 徐佳莹，
歌词 / 切点 / 时长按主播实唱版，两者解耦。另两首原唱已联网核实：周杰伦
（2026《太阳之子》）、李宇春（2009 同名专辑）。三份官方 LRC 与三张原唱封面在
`tools/fetch_lrc_1110.py` 中备齐；A 面 `_media_cache` 全程只读，缓存重定向到
`songs/_lrc_cache/`，没有往那条路径写回任何东西。

### 11.3 交付实测（2026-11-11）

| 曲目 | 帧数 | 码率 | 大小 | 审计 |
|---|---|---|---|---|
| 那天下雨了 | 6594 / 219.80 s | 32.05 Mb/s | 880 MB | 18/18 PASS，逐镜 54/54 |
| 一样的月光 | 6865 / 228.83 s | 29.63 Mb/s | 847 MB | 18/18 PASS，逐镜 57/57 |
| 下个，路口，见 | 6162 / 205.40 s | 30.27 Mb/s | 777 MB | 18/18 PASS，逐镜 54/54 |

全部 1920x1080 / 30fps / H.264 / AAC 48k 立体声，`ffmpeg -ss 60` 实测可 seek。
三首 live 插入各 6 窗 × 3.4 s（占 8.8%–9.9%），零形变违规，中央预留框
`centerFree=[oncam]` 且 `outsideLive=0`，真实 GPU 为 RTX 5070 Ti。
`cuts/2026-11-10/official/manifest.json` **只含这三首**；顶层属于 A 面，本轮一个字节都没动。

### 11.4 为此解除的 11-09 硬编码

`tools/prepare.py` 的 session 路径、`source_video`、`--song choices`、`LRC_CACHE`、
`[by:]` 落款、plates 随机种子全部改为按 `--date` / 单曲 `date` 解析，`cut.mjs` 把
`--date` 透传给 prepare；新增 rain / moon / city 三套各不相同的程序化背景板母题，
避免三首共用同一套「霓虹雨」。11-09 两首的既有分支保持原样，可继续重跑。

