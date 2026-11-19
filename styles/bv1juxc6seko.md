# 风格档案：bv1juxc6seko

> 由 `tools/pv_ref.py` 自动生成，请勿手改；要改请改脚本或加 `--override`。

| 项 | 值 |
|---|---|
| 标题 | 满月纪念曲|写给我第一个喜欢的女孩【原创pv】 |
| UP | 艾薇拉-Elvira |
| 链接 | _tmp/ref/bv1juxc6seko.mp4 |
| 规格 | 1920x1080 / 30.00fps / 178.93s / 5367 帧 |
| 镜头 | 30 个，中位 5.28s，p90 10.85s，每分钟 10.1 切 |

## 一、量测结论

| 维度 | 量 | 读数 |
|---|---|---|
| 配色 | 底 / 字 / 强调 | `#040508` / `#ececf0` / `#5a5897` |
| 配色 | 亮度 / 饱和p95 / 对比 | 0.54 / 0.67 / 0.27 |
| 排版 | 字高 / 行数 / 面积 | 0.089 / 2 / 0.0619 |
| 排版 | 对齐 | center-mid |
| 排版 | 有字帧占比 | 83% |
| 人物 | 主体覆盖 / 水平偏置 / 出镜 | 0.024 / 0.67 / 67% |
| 质感 | 颗粒 / 网点 / 扫描线 / 色散 | 0.024 / 0.130 / 0.000 / 0.0px |
| 质感 | 泛光 / 画面变化率 | 0.0144 / 0.164 |
| 转场 | 渐变比例 / 池 | 72% / dissolve, grad, mask, ink |
| 画幅 | letterbox / 下条带 | 9.3% / 3.6% |
| 画幅 | 下条带长期有字（credit 条） | 100% |

## 二、映射到引擎（`pv_direct --style` 实际会用的值）

```json
{
  "pack": "violet",
  "pack_rank": [
    {
      "pack": "violet",
      "dist": 0.0
    },
    {
      "pack": "huaxin",
      "dist": 67.3
    },
    {
      "pack": "paper",
      "dist": 69.9
    },
    {
      "pack": "hud",
      "dist": 91.5
    }
  ],
  "pack_proposal": null,
  "layouts": {
    "titleCard": 3,
    "center": 2,
    "scaleSteps": 2,
    "cross": 1,
    "block": 2
  },
  "trans_pool": [
    "dissolve",
    "grad",
    "mask",
    "ink"
  ],
  "trans_dur": 0.4,
  "type": {
    "size_ladder": 1.196,
    "tracking": 0.045,
    "leading": 1.35,
    "accent_word_ratio": 0.18,
    "serif": false
  },
  "texture": {
    "grain": 0.019,
    "halftone": 0.104,
    "scanline": 0.0,
    "rgb_split": 0.0,
    "bloom": 0.173
  },
  "figure": {
    "presence": 0.667,
    "coverage_target": 0.357,
    "bias": "right",
    "frameless_hint": true,
    "confidence": 0.55
  },
  "frame": {
    "letterbox": 0.0926,
    "credit_bar": true,
    "aspect_visible": 1.959
  },
  "motion": {
    "cam_speed": 0.002,
    "zoom_rate": -0.00108,
    "churn": 0.1636,
    "shot_len_target": 5.284
  }
}
```

## 三、指标口径

| 量 | 怎么算的 |
|---|---|
| `lum` | 亮度均值 /255 |
| `contrast` | 亮度标准差 /255（同时用作「画面平不平」的判据） |
| `sat` | HSV 饱和度均值（max-min)/max |
| `edge` | Sobel 幅值 > p95 的像素占比（信息密度） |
| `text_h` | 疑似文字连通域高度中位数 /画高（=字号代理量） |
| `text_lines` | 疑似文字行数（按 y 重叠合并连通域） |
| `text_area` | 疑似文字总面积 /画面积 |
| `text_align` | 文字包围盒中心相对画面的位置：left/center/right × top/mid/bottom |
| `letterbox` | 纯黑行/列占画高的比例（上下黑边之和） |
| `grain` | 高通残差在平坦区的标准差（胶片颗粒代理） |
| `halftone` | 径向频谱 1/8~1/2 奈奎斯特频段的峰值比（网点/排线） |
| `scanline` | 行均值序列的周期性峰（扫描线） |
| `rgb_split` | R 与 B 列均值互相关的水平错位（像素，0=无套色错位） |
| `bloom` | 亮度 >240 的像素占比（光晕/泛光） |
| `subject` | 与画面主色距离 > 阈值的最大连通域面积占比（主体代理量） |
| `subject_cx` | 主体质心 x（0..1） |
| `motion` | 相邻帧灰度相位相关的位移模长 /画宽（相机运动代理） |
| `sat_max` | 饱和度 p95 |

> 自适应抽帧：19 个镜头被判为「文字密集/快切」，按 10fps 重扫；其余 2fps。
