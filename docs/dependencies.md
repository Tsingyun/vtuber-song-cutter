> **说明**：本文是该项目在真实生产环境中开发时留下的**设计记录**（原文照录，仅将本机路径与私人数据源替换为配置键占位）。
> 其中出现的 `<...>` 占位符对应仓库根 `config.json` 里的同名配置项。

# 歌切视频生成：完整链路依赖分析 + 去 ASR 可行性评估

> 分析对象：`本仓库根目录`（song_cutter.py 主链路 + 6 个模块 + Node 渲染器）
> 结论基于代码实证（grep / 阅读调用点），非推测。

---

## 〇、链路全貌（一次歌切实际跑过的 10 步）

```
① 找当天 SRT ──────────► <srt_dir>\*.srt（FunASR 产物）
② detect_songs ────────► LLM(Gemini+google_search / GLM备用) 读转写全文
                          + 在线表格歌单 → segments/*.json（粗 start/end + 歌名）
③ wave_refine ─────────► ffmpeg 抽 wav → 包络 p5/p99 阈值 → 切点精修
④ onset_v2（切入点）───► ffmpeg 抽 wav → 说话段→静音谷→能量回升 → 真起点
⑤ lyrics_fetch ────────► LRCLIB → 网易云 → LRC + 封面
⑥ timeline_sync.analyze► 网易云原曲(fee=0下载) → chroma DTW → slope/锚点/song_end_abs
⑦ MP3 提取 ────────────► ffmpeg libmp3lame 192k + afade 尾淡出 + 片头补静音
⑧ 歌词时间轴 ──────────► ①DTW(精确) → ②ASR(lyric_align 降级) → ③raw(兜底)
⑨ decor_pick ──────────► librosa 节奏/音色 + 歌词意象 + 封面取色 → 标题装饰
⑩ 渲染 + 混流 ─────────► Node+Playwright(WebCodecs 逐帧) → ffmpeg -c:v copy + aac
```

---

## 一、外部依赖清单

### A. 本地可执行程序 / 运行时

| 依赖 | 在链路中的作用（调用位置） | 硬性？ |
|---|---|---|
| **ffmpeg**（`D:/ffmpeg/bin/ffmpeg.exe`） | ① `wave_refine._extract_mono_wav` 抽单声道 16k wav 做包络分析<br>② `onset_v2.extract_mono` 同上<br>③ `song_cutter.produce_one` 切 MP3（`-af afade` 尾部 1.2s 淡出、`libmp3lame 192k`）<br>④ `_ensure_head_silence` 片头静音检测/插入<br>⑤ 最终混流（`-c:v copy -c:a aac -movflags +faststart`）<br>⑥ `lyrics_sync` 解码网易云下载的 m4a/mp3 | **硬**（无任何替代路径，缺则整链瘫痪） |
| **ffprobe** | 源视频时长/规格读取；`verify_specs` 成品规格校验（1080P60 断言） | **硬** |
| **Node 22**（managed `22.22.2-3` 或系统） | `render_song.cjs`（渲染驱动）、`kdocs_fetch.cjs`（歌单抓取） | **硬**（不出视频） |
| **Python 3.10 系统解释器** | 主链路运行；managed 3.13 无 requests/numpy，跑不了 | **硬（事实）** |

### B. 第三方模型 / ASR

| 依赖 | 模型细节 | 作用位置 | 硬性？ |
|---|---|---|---|
| **FunASR**（上游项目 `上游转写项目`） | `iic/SenseVoiceSmall` + `iic/speech_fsmn_vad_zh-cn-16k-common-pytorch` + `iic/punc_ct-transformer`；产物写 `<srt_dir>\*.srt` | **本仓库不直接调用**，只消费其 SRT 文件。SRT 用途见第二节 | **入口硬依赖**：`main()` 找不到当天 SRT → `raise FileNotFoundError`；但**切点/出点计算全程不依赖**（详见第二节） |
| **Whisper** | 未使用（记忆中曾作为 FunASR 的备选方案，代码里无引用） | — | 否 |

### C. 在线服务 / API

| 依赖 | 用途 | 调用位置 | 硬性？ |
|---|---|---|---|
| **Gemini**（`generativelanguage.googleapis.com`，含 `google_search` 工具） | 从转写全文里找唱歌片段、定粗 start/end、判歌名/语种/置信度 | `song_cutter._call_google`，config 复用 `<llm_config>` 的 `summarize` 段（含代理） | **准硬**：无 LLM ⇒ 无粗切点与歌名；但有 `segments/*.json` 缓存时可完全跳过 |
| **智谱 GLM**（`glm-4-flash`，key 读 `config.json → credentials.glm_credentials_file`） | Gemini 失败时的备用通道 | `_call_zhipu` | 否（兜底） |
| **在线表格**（在线歌单表（config.json → songlist.url），Playwright 抓第 3 表） | 当日演唱歌单（权威歌名源）→ 修正 LLM 歌名 + 数量对账 | `kdocs_fetch.cjs`（TTL 12h 缓存，落在 `kdocs_songs.json`） | **软**：失败返回 None，回退「本地歌单 + LLM 联网核对」 |
| **LRCLIB**（`lrclib.net/api/search`） | 歌词首选源（含 artist，用于区分原唱/翻唱） | `lyrics_fetch` | **软**：失败转网易云 |
| **网易云音乐 Web API** | ① `cloudsearch/pc` 搜歌 ② `api/song/lyric` 官方 LRC（含 VIP 逐字 klyric）③ `enhance/player/url` 下原曲音频（供 DTW）④ 专辑封面图 | `lyrics_fetch` / `lyrics_sync.NetEase` | **部分软**：歌词/封面失败 → 视频仍出（无词/无封面）；**原曲下载失败 → DTW 精确同步降级**（走 ASR 或 raw） |
| **网易云 VIP cookie**（`config.json → credentials.netease_cookie_file`） | 非免费曲（`fee≠0`）的原曲下载与逐字 klyric | `lyrics_sync.load_cookie`，缺失则降级 | 否（仅影响精度） |

### D. Python 库

| 库 | 用到哪 | 硬性？ |
|---|---|---|
| **numpy** | `wave_refine` / `onset_v2` / `timeline_sync` / `lyrics_sync` 的核心计算（包络分位、形态学、DTW、互相关） | **硬** |
| **librosa** | `lyrics_sync`（`chroma_cens/CQT` 特征、`load`、resample）；`decor_pick.audio_signals`（rms/onset_strength/tempo/spectral_centroid，在函数内 `try import` 失败即降级返回 None） | `lyrics_sync`：**软**（无它 → DTW 不可用 → 降级 ASR/raw）；`decor_pick`：**软**（降级为固定装饰方案） |
| **requests** | LLM / 网易云 / LRCLIB 全部 HTTP | **硬**（网络环节） |
| **Playwright + Chromium**（Node 侧） | `render_song.cjs` 无头加载 player 并 WebCodecs 逐帧编码；`kdocs_fetch.cjs` 抓表 | **硬**（渲染环节） |

### E. 明确**不存在**的依赖（重要澄清）

- **人声分离模型（Demucs / Spleeter / UVR / MDX）**：全仓库无任何引用。**当前链路不做人声/伴奏分离**，所有音频分析都在混合音轨（直播混音）上做。
- **歌声检测专用模型（如 Silero VAD / CREPE / pyin / Vocal 分类头）**：无引用。`onset_v2` 是自研的能量包络 + 形态学 + 局部验证规则，不是模型推理。
- **音频指纹服务（ACRCloud / Shazam / AudD）**：无引用。歌名识别靠「LLM 读转写文本」，不靠听音识曲。

---

## 二、ASR（SRT）到底起了什么作用

### 2.1 三个作用点，权重完全不同

| # | 作用 | 代码位置 | 是否"判断何时在唱歌"的核心 |
|---|---|---|---|
| ① | **片段发现**：把全场转写给 LLM，找唱歌片段 + 定粗 start/end + 歌名 | `song_cutter.detect_songs` → `_call_google` | ✅ **是唯一的粗定位来源**（全局搜索阶段） |
| ② | **歌词时间轴降级对齐**：LRC 行与 ASR 句做覆盖率配对 → 聚类常数偏移 | `lyric_align.align_lrc`，仅在 DTW 不可用时启用 | ❌ 只是三级降级链的第二级，不做唱歌判断 |
| ③ | **逐句唱完点标注**（卡拉OK 逐句填充终点 `<mm:ss.x>`） | 仅在**外部脚本** `_render_fix0929.py`（尚未并入 `song_cutter`） | ❌ 属于歌词精度增强，非切点判断 |

### 2.2 切点 / 出点：ASR **完全不参与**

- **切入点**：`onset_v2.detect_onset`（能量包络 p5/p99 阈值 → 多阈值静音谷 → 形态学闭开 → 局部验证：对比度 ≥15dB、回升 ≥8dB、回升持续 ≥18s → 回溯谷底 +2dB）。纯音频。
- **出点**：`wave_refine`（包络尾部静音）+ `timeline_sync.analyze` 的 `song_end_abs`（DTW 伴奏结束点 + TAIL_KEEP 1.6s）。纯音频 + 原曲 DTW。
- 代码里 `produce_one` 全程未把 `srt_entries` 传给任何切点函数；`srt_entries` 只出现在歌词分支（`if srt_entries: lyric_align.align_lrc(...)`）。
- 出点"三源互证"中的 SRT 核验是**人工/外部脚本**行为（`_render_two.py`、`_render_fix0929.py` 里的注释与核验），不在主链自动逻辑里。

### 2.3 移除 ASR 会在哪一步失效

| 阶段 | 是否失效 | 后果 |
|---|---|---|
| `main()` 入口找 SRT | **立即硬失败** | `FileNotFoundError("找不到当天 SRT")` —— 这是唯一的代码级硬断点 |
| 片段发现（粗切点 + 歌名） | **失效** | 没有转写文本 ⇒ LLM 无输入 ⇒ 无法自动知道"唱了哪首、在哪唱"。需人工/歌单/指纹替代 |
| 歌词时间轴 | **降级** | DTW 可用 → 不受影响（`timeline_source: dtw`，实测误差 0.02s 级）；DTW 不可用且无 SRT → 落 `raw`（直接用 LRC 原曲时间轴，误差通常 1–3s，不逐句精确） |
| 逐句卡拉OK填充终点 | **降级** | 无 SRT 标注 → 播放器按「字重估算 + 下一行前」兜底（`est = 0.9 + 0.42*lineWeight`，`min(est, gap)`），间奏不再被摊入但精度低于 SRT 标注 |
| 切点 / 出点 / 渲染 / 混流 | **不受影响** | 全部音频侧，零改动可跑 |

---

## 三、不依赖 ASR，仅靠音频识别唱歌片段：方案评估

### 3.1 各技术方案对比

| 方案 | 原理 | 准确率（本项目语境估计） | 典型误判 | 成本 |
|---|---|---|---|---|
| **A. 能量/RMS 包络 + 自适应阈值**<br>（`wave_refine` 现役） | dB 包络 → p5/p99 → `th=max(p5*2.5, 0.04)` + 形态学 | 定位"活动段"很准（±0.2s）；**区分说话/唱歌几乎无效**（<50%） | 高能聊天、笑声、BGM、掌声、游戏音效全被当成唱歌 | 极低（已在跑） |
| **B. 通用 VAD**（WebRTC / Silero） | 判"有没有语音" | 语音检测 95%+，但**唱歌与说话都算语音**，对本项目目标≈无效 | 主播聊天整段被判成唱歌 | 低（Silero ~2MB onnx） |
| **C. 歌声特征**（pyin/CREPE 基频 + harmonicity/HNR + 音高稳定性 + chroma 周期性） | 唱歌＝持续稳定基频 + 强谐波 + 长音符；说话＝基频抖动、谐波弱、音节短 | **单特征 70–80%，多特征融合 + 时长≥15s 平滑可到 85–90%** | 念白/Rap、情感激昂的说话（基频起伏大）、电子伴奏掩盖人声、纯哼唱（反而判得准）、主播跟原唱合唱（混音两张基频） | 中（librosa pyin 已可用；CREPE 需 torch） |
| **D. 人声分离后活跃度**（Demucs v4 / UVR-MDX） | 分离 vocal/accompaniment，看 vocal 轨是否持续活跃 + 是否带旋律 | **最高（90%+）**，且能区分"放原曲不唱"（vocal 轨静默）与"真唱" | 分离残留（伴奏漏进 vocal）、清唱无伴奏时伴奏轨空但 vocal 强（判对）；代价最大 | **高**：GPU 上 3–5min 音频约 30–60s；CPU 上数分钟；模型 ~300MB + torch 依赖 |
| **E. 原曲指纹 / chroma 匹配定位**<br>（`lyrics_sync` 现役） | 拿候选原曲与录播做 DTW/互相关，直接定位该曲在某时段的精确起止 | **有原曲时最高（±0.1s）**，但它**只能精修，不能"发现"**——必须先知道歌名 | 翻唱编曲差异大时匹配失败（已用 chroma_cens + 稳健回归缓解） | 中（已有实现） |
| **F. 听音识曲**（ACRCloud/AudD/Shazam） | 音频指纹 → 歌名 | 有版权的流行曲识别率高；**Vocaloid/同人曲基本识别不出** | 术力口曲、冷门曲、翻唱全灭；付费 API | 低-中（需付费、联网） |

### 3.2 关键洞察：本项目其实已经有"歌名真值源"

在线歌单表+ 本地 `本地曲库 JSON（config.json → song_library_json）` **已经给出当天唱了哪些歌**（权威、人工维护、零成本）。
这意味着：**"唱了哪首"不需要 ASR 也不需要听音识曲**，只需要解决"这首歌出现在录播的哪一段时间"——而这正是 E 方案（chroma DTW 匹配）擅长的。

由此得到一条完全不依赖 ASR 的定位链路：

```
歌单 N 首（kdocs / 本地库，已有）
   ↓ 每首去网易云拿原曲音频（fee=0 免费；VIP 需 cookie）
   ↓ 录播全曲 chroma_cens 降采样 → 滑窗粗匹配（找候选区间）
   ↓ 候选区间内精细 DTW → 精修起止 + slope
   ↓ 与 wave_refine / onset_v2 的音频边界交叉验证 → 最终切点
```
`lyrics_sync` 已有 DTW、chroma、稳健回归全套实现，缺的只是「全曲滑窗搜索」这一步（当前是给定粗区间后做全曲 DTW）。

### 3.3 推荐方案（分层，按优先级）

**P0 — 立刻可做、零风险：把 ASR 从"硬依赖"降级为"可选输入"**
- `main()` 找不到 SRT 时不要 `raise`，改为警告 + 切换「无 ASR 模式」；
- 有 `segments/*.json` 缓存时继续跑（现已支持）；
- 影响面：切点/出点/渲染完全不变，只是歌词时间轴落到 dtw 或 raw。
- 代价：几乎为零；收益：ASR 缺失时不再整链瘫痪。

**P1 — 歌名 + 定位：歌单驱动 + chroma 滑窗搜索（替换 LLM/ASR 的粗定位）**
- 输入：kdocs 当日歌单（已有）；
- 新增 `locate_in_recording()`：全录播 chroma 降采样 → 对每首歌滑窗粗匹配 → 候选段精 DTW；
- 准确率预期：有原曲音频时 ±0.5s 级；**失败场景**：原曲下载不到（VIP 无 cookie）、翻唱编曲差异过大、同一首唱多次（需去重取 N 个峰）。
- 保留 LLM/ASR 作为「歌单为空或匹配失败」时的补充，而非主路。

**P2 — 唱歌/非唱歌判别：多特征融合（仅当需要"无歌单盲检"时）**
- 特征：RMS 包络 + `librosa.pyin` 基频存在率 + harmonicity(HNR) + 音高稳定性 + 段长 ≥ 30s；
- 阈值/逻辑回归小分类器，用现有 `cuts/` 历史成品（已知切点）做正负样本自评；
- 预期：段级 F1 0.85 左右；**不建议单独用于自动出片**，只用于「生成候选段供人工/歌单确认」。

**P3 — 人声分离：不进主链，仅作疑难场景的判别增强**
- 只在「歌单缺失 + 需区分放原曲 vs 真唱」或「伴奏极响导致能量法失败」时启用；
- 成本最高（GPU/时间/依赖），对本项目 ROI 低于 P1。

**P4 — 卡拉OK逐句填充：改用官方逐字歌词（klyric）替代 SRT 标注（推荐度高于 ASR）**
- `lyrics_sync` 已支持下载 VIP 逐字 `klyric`；
- 逐字时间经 DTW 映射后 → 行尾 `<mm:ss.x>` 唱完点标签，**比 ASR 组尾 +0.45s 更准**（官方逐字是原曲精标，直播 ASR 常把多句合并成一组）；
- 这是当前唯一真正依赖 ASR 的环节，且有更优的替代源。

### 3.4 取舍一览

| 路线 | 去 ASR 程度 | 精度 | 新增复杂度 | 推荐度 |
|---|---|---|---|---|
| P0 降级为可选 + 歌单驱动定位（P1） | 全链路可无 ASR 出片 | 切点 ±0.5s（有原曲） | 低（复用 lyrics_sync，加滑窗搜索） | ★★★★★ |
| P2 多特征歌声检测（盲检） | 全链路可无 ASR 出片 | 段级 85%，仍需歌名来源 | 中（特征工程 + 阈值调优） | ★★★☆ |
| P3 人声分离 | 可无 ASR | 最高 90%+ | **高**（torch + 模型 + 耗时） | ★★☆ |
| 维持现状（ASR 硬依赖） | 否 | 切点最准 + 有逐句标注 | 无 | ★★★★（现状已稳定，不建议为去 ASR 而去 ASR） |

**一句话结论**：ASR 在本项目里是「**发现者**」而不是「**裁判**」——它只负责告诉系统"这段可能在唱歌、歌名叫什么"，切点/出点/时间轴的裁判权早已在音频侧（onset_v2 + wave_refine + DTW）。所以去 ASR 的可行性很高，真正的瓶颈不是"判断是否在唱歌"，而是"**在不知道歌名的情况下确定歌名**"——而这个问题本项目恰好用在线表格歌单绕过了。优先做 P0+P1，把 ASR 从硬依赖降为可选增强，再考虑 P4 用官方逐字歌词替代 ASR 做逐句标注。
