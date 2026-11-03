# 运行时接入：Vocaloid-Style MV Pipeline

> 一句话：同一份 `storyboard.json`，有两条渲染腿。一条是我们自己的 `pv_engine.js`（唯一能做
> 场景 C 的「一行字一半在前一半在后」）；另一条是**整片交给上游**——它的 `timeline.js` 当母版，
> 我们只往里面填切点、唱词、立绘和三张图的色值。本文说的是第二条。

---

## 1. 为什么之前「看不出关系」

`docs/pv-text-engine.md` §6 那张表里，我们自己写着：

> **Vocaloid-Style MV Pipeline** —— 是 skill / 模板，不是引擎。……**代码未复用**。

第一版接入时，我把「整合」做成了**概念对齐**：九类图层、统一时间轴、统一读口全到位，可成片里
没有一行代码是它的。审片的人看不出关系，因为确实没关系。

第二版接的是「它的引擎跑我们的编排」，但 `tools/pv_to_z.py` 当时自己写了一份简化版 `setup`——
八个场景只用上三个、自定义 fx（星野 / 山脊 / 星轨 / 群萤聚字）一个没用、JIZURA 只挂两个 plan、
配色和 LOOKS 全是我们手写的。**用了它的引擎，还是我们的片子。**

第三版（现在这版）反过来：拿它的 `template/engine/timeline.js` 当母版，只替换两处——
片名 / 配色脚本，和镜头表。其余一字不改。

## 2. 现在的链路

```
_tmp/mvg/art_out/_work/storyboard.json     切点 / 段落 / 强度         ← 我们
_tmp/mvg/art_out/_work/lyric.lrc           唱词与行时间              ← 我们
_Tsing/Pictures/管人立绘/岁己立绘/*.png     三张定稿立绘              ← 我们
          │
          │  tools/pv_to_z.py            母版注入：读上游 timeline.js，
          ▼                              换掉 shots 段与 colour script
zrun/engine/timeline.js                    母版 helpers + DENY + 4 个 plan + LOOKS + HUD
          │                                + 我们的 52 个 shot
          │  上游 engine/core/main.js：await Z.setup(audio) → 每帧 window.renderFrame(t)
          ▼
zrun/tools/render_frames.mjs               Playwright → ANGLE/D3D11 → NVENC
          │
          │  tools/pv_z_render.py         分段串行 + concat + 混音
          ▼
        *.mp4
```

`zrun/` = `_tmp/mvg/zrun`，派生目录，随时可删可重建（§7）。`_ref/` 下的上游 clone 始终只读。

## 3. 谁贡献什么

| | 我们 | 上游 |
|---|---|---|
| 切点与镜头长度 | ✅ 51 镜，来自 `storyboard.json` | 只按 `Z.SHOTS` 执行 |
| 唱词与行时间 | ✅ `.lrc` | JIZURA 负责版式、入场、装饰 |
| 立绘 | ✅ 三张定稿 PNG（谁站在哪一镜也是我们定的） | `illust` 的 `chars[]` 负责运动与合成 |
| 配色脚本的值 | ✅ 九个色值（一部片子一份） | 色值怎么用、铺在哪、跟哪个 plan 走 |
| 镜头语言 | ✅ 切在哪、哪一镜重 | ✅ 五个场景（illust / converge / kanjiSlam / typeCard / afterimage） |
| 场面调度 | — | ✅ 星野、山脊、星轨、群萤聚字、余烬、日轮残影 |
| 版式 | — | ✅ JIZURA 860 个部件、四个 plan（main / cf / noir / mono） |
| 后期 | — | ✅ LUT 图谱、3 级 bloom、12fps 手描き boil、颗粒、色散 |
| 转场 | ✅ 用哪种、给哪一镜 | ✅ 十种实现（flash / slices / xfade / black …） |

一条界线：**画面里的东西没有一样是我们画的。** 我们只决定「切在哪、唱什么、谁站在哪、什么颜色」。

## 4. 母版补丁清单（`pv_to_z.py`）

`sub1()` 每一条替换都要求**命中恰好一次**，否则直接退出——上游改版后静默失配比报错难查得多
（成片会照常渲出来，只是参数全部落空）。

| 位置 | 补丁 | 为什么 |
|---|---|---|
| 文件头 | 换成生成说明 + MIT 声明 | 产物是衍生作品，得说清来源 |
| `Z.TITLE` / `TITLE_SUB` / `ARTIST` / `CREDIT` | 本片信息 | 上游 `new_project.py` 同样只改这四行 |
| `const FF = {...}` | 九个色值换成本片的红与金 | 这是「colour script」，一部片子一份 |
| `const NIGHT = [...]` | 四段停靠点 | 同上，`illust` 的底子 |
| `JIZURA common.colors` | `ghostA/ghostB` 从 demo 的蓝绿换成暗红 / 暖金 | 否则唱词会出现与调色无关的青色重影 |
| `const END = B(13)` | 换成最后一镜的终点（秒） | demo 只有 12 小节，本曲 132 小节 |
| `const TAIL = 2.0` | 保留，但 `duration = END` | 本片尾卡已含在 END 内，再加会多渲 3 秒 |
| `for (const s of secs) s.look = s.label` | 按段落能量归到 `intro/verse/pre-chorus/chorus` | `analysis` 的 `label` 恒为 `"section"`，直接用会让 `LOOKS` 整套落空 |
| `const TXT = ...` | 加本片标题 / 演员字符 | 字体预热串少了字，首帧会异步加载、各 worker 不一致 |
| `VERTICAL_CFG.full` | 按实际图形式镜 id 列出 | 9:16 重排时这些镜保留整幅构图 |
| `SEC_NAME` / HUD 行 | `sec.label` → `sec.look` | 同上，HUD 的段落名要能显示 |
| shots 段 | 整段替换 | demo 的 11 镜 → 本片 52 镜 |

**shots 段之外没有一行是我们写的。** 包括 `stars()` / `ridge()` / `starTrails()` / `swarmGlyph()`
四个自定义 fx 工厂、JIZURA 的 DENY 策展表、`globalFX`（副歌闪白 + 底鼓抖）、`Z.hud`、竖排配置。

自检就一条：`diff -u` 上游母版与产物，hunk 数应当正好 7 个（上面七类各一），其余每一行都相同。
本片实测：7 个 hunk，337 行 → 1351 行，新增的 1108 行全在 shots 段。

## 5. 镜头规则（只在上游的镜头语言里选）

| 判定 | 结果 | 依据 |
|---|---|---|
| `kind == "title"` | `scene: typeCard` | 上游开场写法 |
| 段落能量 ≥ 0.78 且是该块首镜 | `scene: kanjiSlam` | 副歌进来砸巨字，读该镜唱词的头两字 |
| 段落能量 ≥ 0.62 且是该块末镜 | `scene: converge` | 导句收白，漩涡落到切点上 |
| 其余 | `scene: illust` | 按能量换 `fxBack`：星野 / 双层山脊 / 星轨 / 余烬 |
| 最后一镜（>8s） | `illust`（余烬 + 她）+ 末尾 3s `afterimage` | 器乐尾没有唱词，只留星空会黑 28 秒 |
| 每个 section 块首镜 + 块内隔一镜 | 挂立绘 | 约六成 illust 镜 |
| 段落 look | 表情：chorus→105 抱臂，pre-chorus→102 笑，verse/intro→100 平静 | 姿态与能量匹配 |
| 段落入口 | `flash`（副歌）/ `slices`（导句）/ `xfade`（主歌）/ `black`（安静段） | 块内一律硬切 |
| 有立绘的镜 | `post: {bloom 0.15, bloomThreshold 0.93}` | 上游注释的 daylight 一档：她的白发白衣按夜景阈值会糊成一团 |

`kanjiSlam` / `converge` 都限量（6 / 5 处），按能量从高到低取——demo 一部片只砸两次巨字。

立绘坐标是**手填**的画布像素（`x: 1290 | 640`，`y: 1104`，`h: 940~1040`），左右交替、`x→x2`
缓慢漂移、`q: 12` 对齐片子的两拍格。上游没有「立绘左缘 = 字形 bbox 62%」这种推导——那是我们
自己引擎的做法，这里不适用。

## 6. 四个必须绕的坑（都有实测）

1. **模板的 `assets/` 是空的**（只有 `.gitkeep`）。`kanjiSlam` 的 `night` 预设写死了
   `assets/char/lookup_tear.png` + `assets/bg/bg_sky.png`，`afterimage` 写死了
   `assets/char/REF_master.png`。不显式置 `char:null, bg:null` 就是 404，而 `window.ready`
   会因此 reject——整页不启动，报错还停在 `assets.js` 的 `decode()` 里。
2. **`afterimage` 是给 2~3 秒残影写的**。28 秒的器乐尾套上去 1.5 秒就衰到全黑。改成
   `illust`（余烬 + 她站着）再切末尾 3 秒做尾卡。尾卡的剪影可以给真立绘，效果是日轮里一个
   逆光侧影——但它只排得下两个大字，五字标题会只剩首尾两字。
3. **`kanjiSlam` 的 `s` 是「画面高的倍数」**：`s=1.25` → 字形 1350px，本来就故意出血。
   四个字连排摊到 2400px 宽，画面上只剩笔画碎片。两个字刚好。
4. **同一时刻只能有一个浏览器在跑**：`--workers 2/3` 会在开头一百多帧后整段停住（两个 worker
   同时停在同一个时刻，分段 mp4 不再增长、进程还在）；改成「两个进程各一个 worker」并行也一样，
   先起来的那段照样停在 ~127 帧。单进程单 worker 稳定 2.4~2.9 fps。怀疑是 ANGLE/D3D11 的共享
   上下文或 NVENC 并发会话，没有再深挖——`pv_z_render.py` 因此改成**逐段串行**，用
   `chunk_XX.mp4` 的存在性做断点续渲。

## 7. 两套引擎的真实差别

**`backfront` ≠ 场景 C。** 上游 `main.js:109` 的 `backfront` 是把 JIZURA 的 `'back'` / `'front'`
两趟**都画进同一张 text canvas**；人物在 `fg` 层，由 `Z.post.render(scene, text, P, fg)` 整体压在
文字之上。所以一行字不可能「一半在她身上、一半沉到她身后」——要么全在下面，要么全在上面。

我们的 `splitAt` + `drawLyrics(ctx, shot, t, 'front'|'behind')` 是**人物夹在两趟之间**画的，
同一行字真的能前后分家。这是架构差别，不是参数问题。

所以两条腿并存：要场景 C 走 `pv_engine`，要 JIZURA 版式和 LUT/bloom/boil 质感走上游。

## 8. 怎么跑

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
python tools/pv_z_render.py --dst _tmp/mvg/zrun --out _tmp/mvg/zrun/render/out.mp4 --chunks 6
#   分段是为了断点续渲：中断后重跑同一条命令，已完成的 chunk_XX.mp4 会被跳过
```

调试先看静帧，比出片快一个数量级：

```bash
node _tmp/mvg/zrun/tools/stills.mjs --times 2,12,55,205,250 --out render/stills --sheet check
```

换立绘：`--art "100@repost,102@repost,105@repost"`（子串或 `#序号`，见 `docs/fig-art.md`）。
加/减立绘密度：`--art-density 0.9`（每个 illust 镜都挂）或 `0.3`（只挂块首镜）。

## 9. 实测（《小丑的品格》269.15s）

| 项 | 值 |
|---|---|
| 镜头数 | 52（51 + 尾卡） |
| 场景分布 | `illust` 44 / `converge` 5 / `kanjiSlam` 1 / `typeCard` 1 / `afterimage` 1 |
| 唱词 | 50 行，JIZURA 全接管 |
| 立绘 | 29 镜 / 3 张（约六成 illust 镜） |
| 单 worker 出帧 | 2.4~2.7 fps（post 356 ms/帧，render 1~2 ms——瓶颈全在 WebGL 后期） |
| 1080p / 30fps / NVENC | cq17；8075 帧约 56 分钟（串行） |

## 10. 边界

- **4K**：立绘只有 760×1000，1350px 的字形旁边会发虚。要 4K 得换 ≥1500px 的图。
- **竖排**：上游有独立的 `engine/vertical.html` + `vertical.js`（9:16 重排版面），
  `Z.VERTICAL_CFG` 已按镜 id 填好，但还没跑通——`--url engine/vertical.html --width 1080
  --height 1920` 是入口。
- **尾卡字数**：`afterimage` 的版面按两个字设计，五字标题只排得下首尾两字。
- **上游更新**：`_ref/` 拉新版后 §4 的十一条补丁要重新对（`sub1` 会把失配变成硬报错）。
- **playwright 版本**：模板要求 `^1.63`，本机是 1.61.1，`render_frames.mjs` 用了 1.61 不认的
  三参数 `page.evaluate`。work 目录里已打补丁，重建 zrun 时要重打。
