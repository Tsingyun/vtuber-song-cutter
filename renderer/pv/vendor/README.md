# vendor/

## jizura_engine.js

JIZURA 字面 v0.10.1（commit `fc16bfe`）的**引擎部分**，由 `src/*.js` 按码点顺序拼接、去掉 `12_ui.js`（编辑器 UI）
与 `vendor/mp4-muxer.min.js`（我们不从这里导 MP4）得到，唯一替换是 `@VERSION@` → `VERSION` 文件内容。

- 上游：https://github.com/852wa/JIZURA
- 许可证：MIT License, Copyright (c) 2026 hakoniwa —— 见同目录 `JIZURA_LICENSE.txt`（必须随代码分发）
- 本仓库**未修改**任何上游文件；我们自己的确定性驱动层在 `../jz_adapter.js`，在本文件之后加载。

重新生成：

```bash
node tools/build_jz_bundle.mjs
```

锁定版本的理由见 `RULES.md` 第 51 条：部件名、部件标签、配色方案索引都被我们的编排层引用，
换版本会让已经验收过的分镜全部重掷。
