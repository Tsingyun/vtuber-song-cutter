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
共 **17 项检查**，任何一项 FAIL，`cut.mjs` 直接拒绝出片。

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
- **《多余的解释》不纳入切分、不出现在交付结果中**：它不在白名单里，不会被 prepare / audit / render，
  也不会写进 manifest。此前错误生成的旧文件属于上一版遗留，与本次 `official/` 交付目录隔离。
- 歌手名标**原唱**：《发如雪》周杰伦（2005《十一月的萧邦》）、《坏女孩》徐良 × 小凌（2011《不良少年》）；
  歌词与切点按现场实唱版本对齐，两者解耦。

## 8. 已知边界

- `analysis/audio.json`、`analysis/envelope_30fps.json`、`audio/song.wav` 是**当前曲目共享路径**，
  由 `prepare.py` 每次重建。`cut.mjs` 强制「prepare → 审计 → 渲染」逐首串行，因此顺序执行时正确；
  单独对另一首跑 stills/audit 前需先 `prepare.py --song <id>`。
- 渲染依赖本机 GPU（Edge/Chrome + ANGLE），无 GPU 环境 audit 的「real GPU renderer」会 FAIL 并拒绝出片。
- 官方 23 个上游工具（转写 / Blender / AI 出图）未引入，理由见 §1。
