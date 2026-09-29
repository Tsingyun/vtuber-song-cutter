# 第三方组件与素材声明

本仓库以 [MIT 许可证](LICENSE) 发布。以下是随仓库分发或运行时依赖的第三方组件，
其版权归各自作者所有，并按各自的许可证条款使用。

## 随仓库分发的字体

### Noto Sans SC

- 位置：`renderer/player/fonts/*.woff2`、`renderer/player/fonts/fonts.css`
- 许可证：**SIL Open Font License 1.1**
- 版权：Copyright © Google Inc.
- 说明：作为播放器界面的正文/UI 字体，按 Unicode 区段切分为多份 woff2 子集，
  以便在无网络的离线渲染环境中内联加载。
- 许可证全文：<https://scripts.sil.org/OFL>

### HarmonyOS Sans SC

- 位置：`renderer/player/fonts/harmonyos/*.woff2`
- 许可证：**SIL Open Font License 1.1**
- 版权：Copyright © Huawei Device Co., Ltd.
- 说明：作为成片画面中标题、歌词与页脚的主字体。字体文件**未做任何修改**，
  仅做 woff2 格式转换以便浏览器加载。
- 我们在此**显著声明本项目使用了 HarmonyOS Sans 字体**，并保留其原始版权声明，
  符合该字体的使用许可要求。
- 许可证全文：<https://scripts.sil.org/OFL>

## 随仓库分发的代码

### mp4-muxer

- 位置：`renderer/player/mp4-muxer.js`
- 许可证：**MIT**
- 版权：Copyright © Vanilagy
- 上游：<https://github.com/Vanilagy/mp4-muxer>
- 说明：在回退档（`--encoder webcodecs`）下，把页面内编码出的 H.264 码流封装为 MP4。

## 运行时依赖（不随仓库分发）

| 组件 | 用途 | 许可证 |
|---|---|---|
| [Playwright](https://playwright.dev/) | 无头 Chromium，逐帧渲染画布 | Apache-2.0 |
| [ffmpeg](https://ffmpeg.org/) / libx264 | 高码率 H.264 编码、混流、抽帧分析 | LGPL/GPL（视构建而定） |
| [NumPy](https://numpy.org/) | 音频包络、DTW、波形分析 | BSD-3-Clause |
| [Requests](https://requests.readthedocs.io/) | 歌词与封面接口调用 | Apache-2.0 |
| [Pillow](https://python-pillow.org/) | 图像处理、对照图生成 | MIT-CMU |

## 占位素材说明

`renderer/player/assets/sticker.png` 与 `assets/watermark.png` 是本项目**自行生成的
抽象几何图形**，用于替代作者本机使用的第三方美术素材，随仓库以 MIT 许可证分发。
若你要替换成自己的素材，把文件放到 `config.json` 的 `assets` 段所指向的位置即可。

## 关于本项目的使用范围

本仓库只包含**工具链代码**。它处理的是使用者自己的直播录播、音频与歌词：

- 不包含任何录音、歌曲音频、视频成片或第三方美术资源；
- 通过接口获取的歌词与封面版权归原权利人所有，仅供个人学习与本地使用；
- 使用者需自行确保其对所处理素材拥有相应权利，并遵守所在平台的服务条款。
