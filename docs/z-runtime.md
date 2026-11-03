# 运行时接入：Vocaloid-Style MV Pipeline

> 一句话：同一份 `storyboard.json`，现在有两条渲染腿——我们自己的 `pv_engine.js`（能做场景 C
> 的歌词分层），和上游 Vocaloid-Style MV Pipeline 的引擎（画面质感 + JIZURA 版式）。
> 后者不再只是「读过的参考文档」，它的 `main.js` 真的在跑我们的片子。

---

## 1. 为什么之前「看不出关系」

`docs/pv-text-engine.md` §6 那张表里，我们自己写着：

> **Vocaloid-Style MV Pipeline** —— 是 skill / 模板，不是引擎。`jizura_adapter.js` + `06-jizura.md`
> 有参考价值（把编排拆成可配置步骤的思路）。**代码未复用**。

那句话在写下的当时是事实，但它回答的是「能不能直接拿来用」，不是「要不要接进来」。
结果就是：任务书里的「深度整合」被我做成了**概念对齐**——统一时间轴、统一图层描述、
九类图层都到位了，可成片里没有一行代码是它的。审片的人当然看不出关系，因为确实没关系。

当时的判断也有一个技术依据：它的引擎入口是 `window.renderFrame(t)`（秒），我们的播放器是
`renderFrame(i)`（帧号），两套驱动互不认识。但这条其实不成立——我们的 `PV.renderAt(ctx, t)`
同样是「秒」的函数，出帧方式也同样是 Playwright 抓 `canvas.toDataURL()`。同一个形状。

## 2. 现在的链路

```
_tmp/mvg/art_out/_work/storyboard.json     切点 / 强度 / 立绘 / 调色  ← 我们
_tmp/mvg/art_out/_work/lyric.lrc           唱词与时间                ← 我们
          │
          │  tools/pv_to_z.py                翻译层（本次新增）
          ▼
zrun/engine/timeline.js                    52 个 Z.SHOTS + 4 个 JIZURA plan + LOOKS + HUD
          │
          │  上游 engine/core/main.js：await Z.setup(audio) → 每帧 window.renderFrame(t)
          ▼
zrun/tools/render_frames.mjs               Playwright → ANGLE/D3D11 → NVENC
          │
          │  tools/pv_z_render.py            分段单进程 + concat + 混音（本次新增）
          ▼
        *.mp4
```

`zrun/` = `_tmp/mvg/zrun`，是**派生目录**，随时可以删掉重建（第 7 节）。`_ref/` 下的上游
clone 始终只读，一个字节都不改。

## 3. 谁贡献什么

| | 我们 | 上游 |
|---|---|---|
| 切点与镜头长度 | ✅ 51 镜，来自 `storyboard.json` | 只按 `Z.SHOTS` 执行 |
| 每镜的强度/配色 | ✅ tier → 渐变、粒子、转场 | 提供 `post` 参数面 |
| 人物 | ✅ 立绘素材库选定的那张 PNG | `illust` 的 `chars[].front` 负责压到文字层上 |
| 唱词 | ✅ `.lrc`（行级时间） | JIZURA 负责版式、入场、装饰 |
| 画面质感 | — | ✅ LUT 图谱、3 级 bloom、手描き boil、颗粒、色散 |
| 字体与排版部件 | — | ✅ 15 个家族 / 860 个版式部件 |
| 后期合成 | — | ✅ WebGL2（`gl.js` 的 4 张 float target） |

## 4. 翻译规则（`pv_to_z.py`）

| 我们的字段 | 变成 | 说明 |
|---|---|---|
| `kind == "title"` | `scene: typeCard` | 标题卡，`lead: 0.3` 慢慢起 |
| 最后一镜（截 8 秒档） | `scene: illust` + 末尾切 7 秒 `typeCard` | 见 §5 坑 2 |
| `fig.art` 存在 | `illust` + `chars:[{front:true}]` | `front` 就是「人物压到文字层之上」 |
| `tier` 达到全片最高且 ≥3 | `scene: converge` | 漩涡收白，落在切点上 |
| 其余 | `scene: illust` | 按 section 换渐变：B 段偏暖、低能段偏冷 |
| `tier` | 转场 | 1→`xfade 0.5`；2→`slices 0.3`；3→`flash 0.14` |
| `fig.art` / `tier≥3` | JIZURA plan | 有她→`cf`（中线留空）；高能→`noir`；其余→`main` |
| — | `post` | 全片 `boil 1.0 / bloom 0.30 / grain 0.055 / lutMix 0.10` |

立绘的坐标是**手填**的（`x:1180, y:1090, h:900`, `anchor:[0.5,1]`）——上游没有「立绘左缘 = 字形
bbox 62%」这种几何推导，`chars[].x` 就是画布像素。这是两套引擎在设计上的差别，不是配置能补的。

## 5. 四个必须绕的坑（都有实测）

1. **模板的 `assets/` 是空的**（只有 `.gitkeep`）。`kanjiSlam` 的 `night` 预设写死了
   `assets/char/lookup_tear.png` + `assets/bg/bg_sky.png`，`afterimage` 写死了
   `assets/char/REF_master.png`。不显式置 `char:null, bg:null` 就是 404，而 `window.ready`
   会因此 reject——整页不启动，报错还停在第 16 行 `assets.js` 的 `decode()` 里。
2. **`afterimage` 是给 2 秒残影写的**。我们的尾镜有 28 秒（器乐淡出），套上去 1.5 秒就衰到全黑。
   改成 `illust` 让她站着收尾，再从末尾切 7 秒做 `typeCard` 尾卡。
3. **`kanjiSlam` 的 `s` 是「画面高的倍数」**：`s=1.25` → 字形 1350px，本来就故意出血。
   四个字连排摊到 2400px 宽，画面上只剩笔画碎片。这个场景没有素材还能跑，但要用对：
   单字（`s≈0.6`）或两字。我们最终没用它——高能镜改用 `converge`（不需要任何字形）。
4. **同一时刻只能有一个浏览器在跑**：`--workers 2/3` 会在开头一百多帧后整段停住（两个 worker
   同时停在同一个时刻，分段 mp4 不再增长、进程还在）；改成「两个进程各一个 worker」并行也一样，
   先起来的那段照样停在 ~127 帧。单进程单 worker 连跑三段都稳定 2.6~2.9 fps。
   怀疑是 ANGLE/D3D11 的共享上下文或 NVENC 并发会话，没有再深挖——`pv_z_render.py` 因此改成
   **逐段串行**，并用 `chunk_XX.mp4` 的存在性做断点续渲。代价是全片（8075 帧）要 ~52 分钟。

## 6. 两套引擎的真实差别

**`backfront` ≠ 场景 C。** 上游 `main.js:109` 的 `backfront` 是把 JIZURA 的 `'back'` / `'front'`
两趟**都画进同一张 text canvas**；人物在 `fg` 层，由 `Z.post.render(scene, text, P, fg)` 整体压在
文字之上。所以一行字不可能「一半在她身上、一半沉到她身后」——要么全在下面，要么全在上面。

我们的 `splitAt` + `drawLyrics(ctx, shot, t, 'front'|'behind')` 是**人物夹在两趟之间**画的，
同一行字真的能前后分家。这是两套引擎在架构上的差别，不是参数问题，也是我们当初必须自己写
引擎的理由。

上游有我们没有的：LUT 图谱调色（16 张双色渐变映射）、3 级 bloom、12fps 的手描き boil、
860 个版式部件、`converge`/`afterimage` 这类成型的场面调度。这些现在能用了。

## 7. 怎么跑

`zrun/` 是派生的，删了重建是标准动作（上游 clone 不动）：

```bash
# 1) 铺工程（模板 → zrun，接入 JIZURA bundle 与 playwright）
SRC=_ref/vocaloid-style-mv-pipeline/skills/vocaloid-style-mv/template
mkdir -p _tmp/mvg/zrun && cp -r $SRC/{engine,tools,jizura,assets,package.json} _tmp/mvg/zrun/
cp renderer/pv/vendor/jizura_engine.js _tmp/mvg/zrun/jizura/app/
cp -r "$NODE_PATH"/playwright "$NODE_PATH"/playwright-core _tmp/mvg/zrun/node_modules/

# 2) 音轨与分析（约 2 分 50 秒）
ffmpeg -y -i _tmp/mvg/art_out/_work/audio.mp3 -ar 48000 -ac 2 -c:a pcm_s16le _tmp/mvg/zrun/audio/song.wav
python tools/pv_to_z.py --job _tmp/mvg/art_out/_work --dst _tmp/mvg/zrun     # 先出歌词，分析要用
python _tmp/mvg/zrun/tools/analyze_audio.py --audio _tmp/mvg/zrun/audio/song.wav --no-plot --no-click

# 3) 字体（15 个家族，约 100 MB，25 秒）
python _tmp/mvg/zrun/tools/fetch_fonts.py

# 4) 出片
python tools/pv_to_z.py --job _tmp/mvg/art_out/_work --dst _tmp/mvg/zrun
python tools/pv_z_render.py --dst _tmp/mvg/zrun --out _tmp/mvg/zrun/render/out.mp4 --chunks 4
#   分段是为了断点续渲：中断后重跑同一条命令，已完成的 chunk_XX.mp4 会被跳过
```

调试时先看静帧，比出片快一个数量级：

```bash
node _tmp/mvg/zrun/tools/stills.mjs --times 2,12,72,205,250 --out render/stills --sheet check
```

`--shots 0:12` 可以只翻译一段（区间外的镜头会被丢掉），配合 `--end` 用来试一小节。

## 8. 实测

| 项 | 值 |
|---|---|
| 镜头数 | 52（51 + 尾卡） |
| 场景分布 | `illust` 46 / `converge` 4 / `typeCard` 2 |
| 唱词 | 50 行，JIZURA 全接管 |
| 立绘 | 3 镜（10.2s / 69.7s / 85.5s），另加尾镜与尾卡 |
| 单 worker 出帧 | 2.4~2.7 fps（post 356 ms/帧，render 1~2 ms——瓶颈全在 WebGL 后期） |
| 20 秒 / 600 帧 | 248 秒（含浏览器启动 4.6 秒） |
| 40 秒样片 | `zrun/render/z_demo_40s.mp4`，146.1 MB，30.6 Mbps，496 秒 |
| 2 进程并行 | 不成立：先起的那段会停在 ~127 帧（见 §5 坑 4） |
| 1080p / 30fps / NVENC | cq17；全片 8075 帧约 56 分钟（串行） |

## 9. 边界

- **4K**：立绘只有 760×1000，1350px 高的字形旁边会发虚。要 4K 得换 ≥1500px 的图。
- **竖排**：上游有独立的 `engine/vertical.html` + `vertical.js`（9:16 重排版面），
  `Z.VERTICAL_CFG` 已经按场景名填好，但还没跑通——`--url engine/vertical.html
  --width 1080 --height 1920` 是入口。
- **上游更新**：`_ref/` 拉新版后 §5 的四处适配要重新对一遍（都是「模板不带素材」引起的）。
- **playwright 版本**：模板要求 `^1.63`，本机是 1.61.1，`render_frames.mjs:366` 用了 1.61 不认的
  三参数 `page.evaluate`。work 目录里已打了一行补丁（删掉那个 `{timeout}`），重建 zrun 时要重打。
