# vendor/

## jizura_engine.js

JIZURA 字面 **v0.10.1**（上游 commit `fc16bfe`）的**引擎部分**。

**它是可复现产物，不是手改文件。** 拼接规则与上游 `build.py` 一致：
`src/*.js` 按**文件名排序**（`sorted(glob)`，不是「码点顺序」）后以 `\n` 连接，
全局替换 `@VERSION@` → `VERSION` 内容，最前面加一行 MIT 署名头。两处删减：

- 去掉 `12_ui.js`（浏览器编辑器界面，我们不跑 UI）；
- 不拼 `vendor/mp4-muxer.min.js`（导出走我们自己的链路）。`11_export.js` **保留**，
  它对 `Muxer` 的引用是惰性的，不调用导出就不会触碰 —— 所以包能正常加载。

- 上游：<https://github.com/852wa/JIZURA>
- 许可证：MIT, Copyright (c) 2026 hakoniwa —— 见同目录 `JIZURA_LICENSE.txt`（**必须随代码分发**）
- 本仓库**未修改**任何上游文件；我们自己的确定性驱动层在 `../jz_adapter.js`，在本文件之后加载。

### 重新生成 / 自检

```bash
node tools/build_jz_bundle.mjs --check   # 只比对，不写盘；不一致退出码 1
node tools/build_jz_bundle.mjs           # 真正写盘
node tools/build_jz_bundle.mjs <上游仓库>  # 指定 _ref/JIZURA 以外的克隆
```

`--check` 按**行尾归一**比较（git 入库时把 CRLF 变 LF），所以换机器 clone 后自检不会误报。
自检基准（v0.10.1，39 个文件 / 2,166,802 字节）：`sha(NF) = c0d62383767d71eb`。

### 为什么要入库

2026-11-01 之前 `renderer/pv/vendor/*.js` 被 `.gitignore` 排除，而 `_ref/`（上游克隆）同样不入库
⇒ **换台机器 clone 下来，JIZURA 通道永久缺失且无处重建**，这就是当时的 `tools/build_jz_bundle.mjs`
根本不存在所掩盖的隐患。现在改为：bundle 入库（MIT 允许，许可证同目录），上游克隆仍不入库，
需要重新生成时按上面的命令从任意克隆重建。

### 为什么锁版本

见 `RULES.md` 第 51 条：部件名、部件标签、配色方案索引都被我们的编排层引用，
换版本会让已经验收过的分镜全部重掷。
