# 歌切系统 · official_pv

一个**可在本地直接运行**的歌切成片系统。它把 [vocaloid-style-mv-pipeline](https://github.com/EGSECDA/vocaloid-style-mv-pipeline) 的引擎当作真正的渲染内核，而不是参考样板。

## 一句话

给一个日期 + 曲目白名单，一条命令完成：从录播切出精确音频 → 用官方 24 场景引擎生成整支歌词 MV → 运行时证明官方能力确实生效 → 编码成片 → 落到交付目录。

```bash
cd D:\岁己歌切\official_pv
node cut.mjs --date <YYYY-MM-DD>                    # 渲染该日白名单里的全部曲目
node cut.mjs --date 2026-11-10                       # 11-10：《那天下雨了》《一样的月光》《下个，路口，见》
node cut.mjs --date 2026-11-10 --song natianxiayule   # 只渲一首
node cut.mjs --date 2026-11-10 --audit-only          # 只准备素材 + 18 项运行时审计，不出片
```
`prepare.py` 的场次、录播路径、白名单、`[by:]` 落款全部按 `--date`（或单曲 json 的 `date` 字段）解析，不再绑定任何固定日期。

流水线**强制串行**：`prepare.py` → `audit_official.mjs`（运行时验收）→ `render_frames.mjs` → `ffprobe` → 交付。
审计不通过就拒绝渲染，所以不会出现"成片交了才说官方能力没接上"。

## 目录

```
official_pv/
  cut.mjs                      一键入口
  engine/
    index.html                 官方脚本顺序（8 个场景文件全加载）
    core/                      ★ 官方原版 6 个文件，逐字节复制
    scenes/                    ★ 官方原版 8 个文件 + 我们 1 个自定义 livewin.js
    fonts/                     43 个字体文件（自包含，不依赖 junction）
    songcut.timeline.js        通用剪辑驱动：读 songs/<id>.json 生成 Z.SHOTS
    __official_demo_timeline.js  官方 Demo「蛍火」时间线，用于冒烟测试
  jizura/app/                 官方 adapter + 构建好的 JIZURA bundle（MIT）
  assets/style/               官方 palettes.json + LUT PNG
  assets/bg/                  程序化生成的背景板（无 AI 图片、无外部素材）
  assets/live/<song>/frames/  只抽直播插入窗口的帧
  analysis/                   audio.json / envelope_30fps.json / lyrics_mv.lrc
  audio/song.wav              当前曲目的精确切片
  songs/<date>.json           日期 + 曲目白名单 + 录播绝对时间
  songs/<id>.json             单曲视听设定（配色、段落、场景池、直播窗口）
  songs/_lrc_cache/           官方 LRC / 原唱封面缓存与本场切点实证
  tools/
    prepare.py                素材准备
    audit_official.mjs        ★ 运行时验收（浏览器内真跑一遍）
    render_frames.mjs         官方渲染器 + 2 处已记录补丁
    stills.mjs / kit_env.mjs / render_final.mjs ... 官方工具
  docs/OFFICIAL_SOURCE_HASHES.json   ★ 与官方 template 的逐字节 sha256 核对结果
  docs/COMPLETENESS.md        ★ 逐项补齐对照表 + 源码核对 + 视觉 QA 记录
  docs/OFFICIAL_LICENSE.txt   上游 MIT 许可证（Copyright 2026 NikusonP）
  docs/OFFICIAL_PROJECT_README.md  官方 template/PROJECT_README.md 副本
```

## 官方能力是怎么被用上的

| 官方机制 | 本系统 |
|---|---|
| 24 个 `Z.scene()` 场景（05 §8） | 页面加载全部 8 个场景文件；**25 个场景的参数全部接线**（`songcut.timeline.js` 的 `sceneArgs` 注册表），每首按 `direction.lexicon` 选自己的子集 |
| 13 种转场（core/trans.js） | 全部 13 类可用（含 `cut` 计为文法选择），每镜按段落文法抽样，参数（`cx/cy/angle/dx/n/seed`）随镜抖动 |
| 11 种内建 fx（scenes.js:97-116） | 11 种全覆盖，含此前被静默丢弃的 `grad`；未解析的名字进 `__cutAudit.fxDropped` 并让审计 FAIL |
| 自定义 fx 工厂（官方 Demo 有 5 个） | 新增 `engine/recipes.js`：**18 个通用配方工厂**（按 falling/rising/radial/linear/texture/fracture/colour 分类），每首从 `direction.lexicon.recipes` 抽不同子集 |
| WebGL2 post（core/gl.js，15+ 参数） | 每镜 `post` + `Z.LOOKS` 段落分档；曲线（bloom/grain/vignette/lutMix）来自 `direction.grade`，高-key 片自动换一套 |
| JIZURA 多 plan（06 §3） | plan 数量由 `direction.type.plans` 决定（5–7），在 style / schemes / seed / fx / centerFree 上真分化 |
| 27 套 JIZURA style（06 §4） | `direction.type.styles` 直接点名；`schemes: 'light'` 用于白天片；DENY 名单支持逐片增删（06 §5「adapt per film」） |
| 调色板「每片新建」（STYLE_RESEARCH / 02 §13） | `prepare.py` 生成 `songs/<id>.palette.json`，`core/main.js` 把 `S_<id>` / `S_<id>_duo` 追加进 LUT atlas（32 行） |
| 官方 Demo 的零图片路线 | 系统不依赖任何 AI 图片；`illust` 同时走渐变分支与程序化 bg 相机分支 |
| 节拍锁定剪辑（09 §2） | 切点吸附真实 LRC 行起点；`audio.json` 的 downbeats 现在是**从 PCM onset 能量测出来的相位**，不再是写死的 0 |
| master 规格（08 §1） | 1920×1080 / 30fps / quantizer QP20 ≈ 30–40 Mb/s / AAC 320k |

**官方文件只有一个被改动**，其余全是本系统新增文件，逐项见 `docs/COMPLETENESS.md`：
1. `tools/render_frames.mjs`（唯一改过的官方文件）：h264 裸流 remux 时用 `setts` 重打时间戳（否则成片不可 seek）、并增加 `--wc-mode/--wc-qp` 让 quantizer 高码率生效。
2. 新增 `engine/scenes/livewin.js`：唯一自定义场景，用于「直播画面短促点缀」，纯 alpha 遮罩、零形变、含窗口帧号回退。
3. 新增 `engine/songcut.timeline.js` / `cut.mjs` / `tools/prepare.py` / `tools/audit_official.mjs`：驱动与编排层。

源码核对实测：**25 个官方文件逐字节一致**（6 个 core、8 个官方 scenes、JIZURA、工具），
1 个官方文件被改（`render_frames.mjs`），基准为本地官方克隆 `13b380d`。

## 差异化：direction 层（2026-11-11）

官方 README 写着 "a toolbox to adapt, not a look to reuse"，`02 §10` 规定「与范例撞超过 3 行就 push further」。
旧版驱动违反这条：每首歌都是同 10 段骨架、同一条强度曲线、`(i*5+ai*3) % pool.length` 的取模式分镜、
同一批 plan。三首成片只差配色——这就是「模板化」的技术来源。

现在每首歌由 `direction` 决定一切，驱动本身不做任何选择：

| 字段 | 作用 |
|---|---|
| `structure` | 段落序列（`pattern`）、各段权重、强度基线与抖动、字密度、最长镜长 → 自动从**歌词自己的时间位置**推导段落边界并吸附到真实行首 |
| `lexicon.scenes / fx / recipes / trans` | 这首歌的可用词汇（按段落分角色），场景用**种子加权抽样**选，不再取模 |
| `type` | JIZURA 的 27 套 style、`schemes`（light/dark）、plan 数量、逐片 DENY 增删、`centerDir` |
| `grade` | LUT 池（含专属 `S_<id>`）、key（exposure）、bloom/grain/vignette/lutMix 曲线、节拍闪白开关 |
| `opening / ending` | 开场与收尾由方向决定，不再恒定 `typeCard → afterimage` |
| `char` | 单张 AI 立绘 + 出场窗口；同一张图有 `full/bust/close/sil/rim/shadow` 六种形态（官方 `illust` 的 chars 分支） |

两道闸门，任一失败就拒绝出片：

1. `tools/direction_report.mjs`（`cut.mjs` 第一步自动跑）——把每首歌的 **12 轴签名**物化，
   两两比对；任意一对差异 < 9 轴、或出现**完全相同的段落骨架**、或整场词汇覆盖太窄，直接 FAIL。
2. `tools/audit_official.mjs`——22 项运行时检查，含「本片与每一部他片的差异 ≥ 9/12 轴」
   「所有声明的 fx 都被解析」「立绘可绘制」。

`engine/direction.js` 是这套东西的内核，`engine/recipes.js` 是通用配方库。

## 运行时验收与视觉 QA

`audit_official.mjs` 共 **22 项**检查，含**全片逐镜冒烟渲染**、**中央预留框作用域检查**、
**fx 静默丢弃检查**、**跨片差异度检查**与**立绘可用性检查**（每个镜头中点真渲一帧），
任何一项 FAIL 就拒绝出片。报告落在 `render/audit_<id>.json`，随成片一起交付。
视觉 QA 按官方 `09-qa-and-lessons.md` 走「抽联系表 → 发现问题 → 修 → 重抽」，
本轮两轮联系表与全部修复项记录在 `docs/COMPLETENESS.md §6`。

## 依赖

Node 20+（Playwright 已装）、Python 3.10+（numpy / soundfile / Pillow）、ffmpeg / ffprobe、Microsoft Edge 或 Chrome（GPU）。
全部资源都在本目录内，没有跨目录联接，可以直接整包拷走。

## 加一首新歌

1. 在 `songs/<date>.json` 的 `whitelist` 里加 id，并写 `songs[id]`（录播绝对起止时间、时长、文件名、切点依据）。
2. 写 `songs/<id>.json`：原唱信息、配色、`grad`、母题字、直播插入窗口，以及 **`direction`**：
   - `structure.pattern` 写这首歌真实的段落序列（不是照抄别人），`density` 决定字密度；
   - `lexicon` 写它自己的场景 / fx / 配方 / 转场词汇（按段落角色分）；
   - `type` 选 JIZURA style 与 `schemes`（白天片务必 `light` + 高-key `grade.exposure`）；
   - `axes` 把 12 轴答案写清楚，`direction_report` 用它算差异。
   - 素材依赖场景（`panels` `burst` `memories` `seq` `wetFloor` `train`）需要照片/片段素材，没有素材就别放进 `lexicon`。
3. 把官方 LRC 放到 `songs/<id>.lrc`（首次运行时 `prepare.py` 也会从 `_media_cache` 自动取）。
4. 可选：把立绘放到 `assets/char/<id>.png`（RGBA、≥1600 px 高、主体不碰画布边缘），
   在 `char` 里写 `count` / `roles` / `forms`。素材缺失时立绘窗口自动关闭，不阻塞出片。
5. `node cut.mjs --date <date> --song <id>`（会先跑 `direction_report` 再逐首 `prepare → audit → render`）。

## 交付规范

成片落在 `D:\岁己歌切\cuts\<date>\official\`，`manifest.json` **只包含白名单曲目**。
歌手名一律标**原唱**（不是翻唱者），封面/署名用原唱资料；歌词与切点按现场实唱版本对齐，两者解耦。

## 交付补充（2026-11-09）

- 白名单只有《发如雪》《坏女孩》；**《多余的解释》不纳入本系统切分、不出现在 `official/` 结果中**。
  ⚠️ 这只约束 `official/` 子目录，**不构成对 `cuts/<date>/` 顶层的任何处置权**，见下方「目录边界」。
- `analysis/*` 与 `audio/song.wav` 是**当前曲目共享路径**，由 `prepare.py` 每次重建。
  用 `cut.mjs` 时逐首串行，无需关心；单独对另一首跑审计/抽帧前需先 `python tools/prepare.py --song <id>`。
- 直播帧提取用 `ffmpeg -start_number` 原地覆盖写入，不做批量删除，重复运行安全。


## QA round 2（2026-11-10）

- 《发如雪》掐掉录播开头 14.5 s 死区：`abs_start 3612.25 / duration 284.80`，
  **音频 + 视频 + LRC + 段落 + 直播窗口整体平移 −14.5 s**，歌词与录播的绝对对齐不变；
  配置 `songs/<id>.json` 的 `lrcOffset` 控制，`prepare.py` 有开头静音硬门槛。
- JIZURA `centerFree`（`J.sideZones` 会把中间 40% 挖空并 `clip()` 文字到左右窄带）
  **只允许 `oncam` plan 在 `livewin` 岁己镜头使用**；`cf` 已恢复全幅排版。
  新增第 18 项审计 `centre-reserve plans only on live inserts`，详见 `docs/COMPLETENESS.md §9`。

## 目录边界（与另一条平行渲染路径共存）

`D:/岁己歌切/` 仓库里还有**另一条平行、可执行、正在使用中的歌切渲染路径**
（`song_cutter.py` / `auto_cut.py` / `renderer/`，4K60），它把产物**和正在使用的混流中间件**
写在 `cuts/<date>/` **顶层**。它不是旧管线，也不是遗留。

本系统被允许写的地方**只有两处**：`official_pv/` 内部，以及自己的交付子目录 `cuts/<date>/official/`。

**严禁**：对 `cuts/<date>/` 顶层的任何文件/目录做移动、重命名、删除、覆盖、"清理"或"隔离"；
把顶层产物称为「旧管线遗留 / 误生成结果」；因白名单排除了某首歌就去处理顶层的同名产物；
建议或执行「停掉那条管线」。也**不要动那条路径的项目文件和文件夹里的任何东西**。

「《多余的解释》不出现在结果中」里的**「结果」仅指 `official/` 子目录**，与顶层产物无关。
`cut.mjs` 用 `deliveryDir()` 把交付路径锁死在 `official/`，启动时只读列出顶层并打印并发提示。

> 事故：2026-11-10 03:14 顶层正在使用的 `.mp3` 中间件被误搬走，导致该链 03:29 混流失败、
> 573 MB 画面中间件被清掉、80 分钟渲染作废（仓库 `RULES.md` 129）。11:29 已全部原样放回。
