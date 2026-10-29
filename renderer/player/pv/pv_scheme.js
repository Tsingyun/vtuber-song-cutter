/*! pv_scheme.js —— 配色体系（对齐 JIZURA 的 style pack / scheme 模型）
 *
 *  上一版的问题不是「颜色不好看」，而是**只有一套配色**：整片都是深纸 + 米白字，
 *  靠一个 accent 变色来区分段落。这在 232 秒里必然读作「同一张幻灯片」。
 *
 *  JIZURA 的做法（src/04_styles.js）：
 *    · 一个 style pack = 若干套 scheme + 字体角色 + 质感 + 装饰密度
 *    · scheme = 九个**有职能**的颜色，不是「主题色 + 强调色」：
 *        bg      底色（整幅铺满，决定这一段的性格）
 *        fg      主文字色
 *        sub     次级文字 / 细线（注记、索引、规则线）
 *        accent  强调（一个，且只用在「一个」元素上）
 *        accent2 第二强调（极少用，配合 accent 做对撞）
 *        ink     色块底（标签条、遮带、衬底块），永远配 onCol() 取反色字
 *        dim     压进底色的巨型字（「见切れ大文字」的背景副本）
 *        ghostA/B  色差通道（RGB 分离）的两个偏移色
 *
 *  为什么要有 dim：JIZURA 的招牌是「同一句话先在底色里出现一次巨大的暗影，
 *  再由清晰的一行盖上去」。没有 dim 就只能拿 fg 加透明度，那是灰，不是「压进纸里」。
 *
 *  本文件只做「解算」，不画东西。渲染层通过 PV.STYLE(name, idx) 拿一个 pal 对象。
 */
(() => {
  'use strict';
  const PV = window.PV;

  /* ---------------- 风格包（数值取自 JIZURA src/04_styles.js） ----------------
     只保留 5 个：noir / crimson / paper / hud / mono。
     为什么不是 12 个全抄：每一个 pack 都换一套字体角色与质感，
     232 秒里出现五种以上「字体人格」，读作拼贴而不是作品。
     这 5 个的组合已经能覆盖「黑 / 红 / 纸 / 炭 / 灰」五种底色性格。 */
  PV.PACKS = {
    noir: {
      name: 'ノワール・クロマ',
      schemes: [
        { bg: '#060607', fg: '#F5EEEA', sub: '#BDB6B2', accent: '#F5A50C', accent2: '#16F4D4', ink: '#F5EEEA', dim: '#2A2A2E', ghostA: '#F5A50C', ghostB: '#16F4D4' },
        { bg: '#F2EDE8', fg: '#0B0B0C', sub: '#4A4644', accent: '#E0600C', accent2: '#0FAE98', ink: '#0B0B0C', dim: '#CEC7C1', ghostA: '#F5A50C', ghostB: '#16C4B4', swap: true },
      ],
      fonts: { display: ['heavy', 'grotesk'], serif: ['mincho', 'didone'], body: ['sansMed'], mono: ['mono'] },
      texture: { grain: 0.9, paper: 0, scan: 0 }, ghost: 1.0,
      decor: { cropMarks: 0.9, rule: 1, barcode: 0.3, dots: 0.2, chapter: 1 },
    },
    crimson: {
      name: 'クリムゾン・シグナル',
      schemes: [
        { bg: '#C8103F', fg: '#FFFFFF', sub: '#FFD9E2', accent: '#140509', accent2: '#39F2C8', ink: '#140509', dim: '#A30C33', ghostA: '#FFFFFF', ghostB: '#39F2C8' },
        { bg: '#150509', fg: '#FF3D6E', sub: '#FF9DB6', accent: '#FFFFFF', accent2: '#39F2C8', ink: '#FF3D6E', dim: '#390F1B', ghostA: '#FF3D6E', ghostB: '#39F2C8' },
      ],
      fonts: { display: ['heavy', 'grotesk'], serif: ['mincho'], body: ['sansMed'], mono: ['mono'] },
      texture: { grain: 0.6, paper: 0, scan: 0.4 }, ghost: 0.8,
      decor: { cropMarks: 0.4, rule: 0.8, barcode: 1, dots: 0.5, chapter: 1, counter: 0.8 },
    },
    paper: {
      name: 'ペーパー・インク',
      schemes: [
        { bg: '#ECE9E3', fg: '#1B2350', sub: '#4D5270', accent: '#C2185B', accent2: '#111111', ink: '#111111', dim: '#C8C3B9', ghostA: '#C2185B', ghostB: '#1B2350', paper: true },
        { bg: '#151515', fg: '#F0EDE7', sub: '#B8B4AC', accent: '#C2185B', accent2: '#1B2350', ink: '#F0EDE7', dim: '#393939', ghostA: '#C2185B', ghostB: '#3A4690', paper: true },
        { bg: '#1B2350', fg: '#F0EDE7', sub: '#AEB2CC', accent: '#C2185B', accent2: '#FFFFFF', ink: '#F0EDE7', dim: '#293574', ghostA: '#C2185B', ghostB: '#FFFFFF', paper: true },
      ],
      fonts: { display: ['mincho', 'didone'], serif: ['mincho', 'minchoLt'], body: ['sansMed'], mono: ['mono'] },
      texture: { grain: 0.7, paper: 1, scan: 0 }, ghost: 0.5,
      decor: { cropMarks: 1, rule: 1, barcode: 0, dots: 0.3, chapter: 1 },
    },
    hud: {
      name: 'ダークHUD',
      schemes: [
        { bg: '#131315', fg: '#EFEDEA', sub: '#8E8B88', accent: '#F25A2B', accent2: '#7FD7FF', ink: '#EFEDEA', dim: '#33333A', ghostA: '#F25A2B', ghostB: '#7FD7FF' },
        { bg: '#0B0B0C', fg: '#FFFFFF', sub: '#9A9796', accent: '#F25A2B', accent2: '#FFFFFF', ink: '#F25A2B', dim: '#2C2C30', ghostA: '#F25A2B', ghostB: '#FFFFFF' },
      ],
      fonts: { display: ['grotesk', 'condensed'], serif: ['mincho'], body: ['sansMed'], mono: ['mono'] },
      texture: { grain: 1, paper: 0, scan: 0.2 }, ghost: 0.6,
      decor: { cropMarks: 1, rule: 1, barcode: 0.6, dots: 0.1, chapter: 1, counter: 1 },
    },
    mono: {
      name: 'モノ・RGB',
      schemes: [
        { bg: '#3B3D41', fg: '#FFFFFF', sub: '#B9BBBF', accent: '#FFE34D', accent2: '#FFFFFF', ink: '#1A1B1D', dim: '#5B5E65', ghostA: '#FF2A2A', ghostB: '#2AA8FF' },
        { bg: '#141517', fg: '#FFFFFF', sub: '#9EA0A4', accent: '#FFE34D', accent2: '#FFFFFF', ink: '#FFFFFF', dim: '#34363B', ghostA: '#FF2A2A', ghostB: '#2AFF7A' },
      ],
      fonts: { display: ['mincho', 'didone'], serif: ['mincho'], body: ['sansMed'], mono: ['mono'] },
      texture: { grain: 0.9, paper: 0, scan: 0.3 }, ghost: 1.3,
      decor: { cropMarks: 0.5, rule: 1, barcode: 0.4, dots: 1, chapter: 1 },
    },
    /* ---------------- 晨光绘本（Innocence v2 专用） ----------------
       与前 5 个相反：全部浅底暖色。故障印刷语法（暗底/色差/碎字）与钢琴抒情曲
       的「温柔软暖」是反义词，所以情绪改用「光」表达：
       破晓 → 晴昼 → 辉 → 梦 → 暮 五段光色弧。
       ghost 压到 0.5（柔双影不是故障）；paper 常开 = 绘本质感。 */
    dawn: {
      name: 'ダーン・ピクチャーブック',
      schemes: [
        /* 0 破晓：奶油底暖褐墨，杏橙 × 晨蓝 —— 醒来、第一道光 */
        { bg: '#F7F0E4', fg: '#4A3527', sub: '#8A6F58', accent: '#E8833A', accent2: '#6B9BC9', ink: '#4A3527', dim: '#E5D5BD', ghostA: '#E8833A', ghostB: '#6B9BC9', glow: '#FFD9A0', paper: true },
        /* 1 晴昼：青白底松墨，青瓷 × 蜜金 —— 安全之地 */
        { bg: '#EFF4EE', fg: '#2F4438', sub: '#6E8575', accent: '#5B9A7E', accent2: '#D9A441', ink: '#2F4438', dim: '#D5E2D5', ghostA: '#5B9A7E', ghostB: '#D9A441', glow: '#DCEBDD', paper: true },
        /* 2 辉：金奶底焦糖墨，辉金 × 珊瑚 —— 全部副歌 */
        { bg: '#FFF3DC', fg: '#5A3B12', sub: '#A67C3B', accent: '#E4A11B', accent2: '#E86A5C', ink: '#5A3B12', dim: '#F0DDB4', ghostA: '#E4A11B', ghostB: '#E86A5C', glow: '#FFE9B8', paper: true },
        /* 3 梦：淡薰底藤墨，兰紫 × 梦蓝 —— 桥段「美到想哭」 */
        { bg: '#F2ECF7', fg: '#443361', sub: '#8A76AC', accent: '#B072C7', accent2: '#7FA8E0', ink: '#443361', dim: '#DDD0EC', ghostA: '#B072C7', ghostB: '#7FA8E0', glow: '#E7D9F5', paper: true },
        /* 4 暮：暮杏底深栗墨，落日 × 暮紫 —— 尾奏收束 */
        { bg: '#EFD9BE', fg: '#6B4A2E', sub: '#A98562', accent: '#D96C3F', accent2: '#8E6AA8', ink: '#6B4A2E', dim: '#DCC0A0', ghostA: '#D96C3F', ghostB: '#8E6AA8', glow: '#F5C99B', paper: true },
      ],
      fonts: { display: ['frauncesItB'], serif: ['frauncesIt'], body: ['nunitoSemi'], mono: ['mono'] },
      texture: { grain: 0.6, paper: 1, scan: 0 }, ghost: 0.5,
      decor: { cropMarks: 0, rule: 0.3, barcode: 0, dots: 0.2, chapter: 0 },
    },
  };
  PV.PACK_ORDER = ['dawn', 'noir', 'crimson', 'paper', 'hud', 'mono'];

  const lum = hex => {
    const c = PV.hex(hex);
    return (c[0] * 0.299 + c[1] * 0.587 + c[2] * 0.114) / 255;
  };
  PV.packLum = lum;

  /* 解算一个 (pack, idx) → 渲染层直接可用的 pal 对象。
     字段与 pv_engine 的 buildStyle 一一对应，另加 bg/fg/sub/dim/acc2/inkBox
     与 texture/flat —— flat=1 表示「铺纯色底」，不再画纸纹渐变与暗角。 */
  PV.STYLE = (pack, idx) => {
    const p = PV.PACKS[pack] || PV.PACKS.noir;
    const s = p.schemes[((idx | 0) % p.schemes.length + p.schemes.length) % p.schemes.length];
    return {
      pack, idx: idx | 0, name: p.name,
      bg: s.bg, fg: s.fg, sub: s.sub,
      accent: s.accent, accent2: s.accent2,
      inkBox: s.ink, dim: s.dim,
      ghostA: s.ghostA, ghostB: s.ghostB,
      /* glow：晨光绘本的光晕色（halo/sunrise/windowLight 等 bg 件读它）。
         旧 pack 没有它 → undefined，画光晕的部件各自兑底即可。 */
      glow: s.glow,
      /* 兼容旧字段：paper/ink 是上一版的名字，渲染层其余部分仍在用 */
      paper: s.bg, ink: s.fg, void: s.dim, paper2: s.dim,
      fonts: p.fonts, texture: p.texture, decor: p.decor, ghostK: p.ghost,
      flat: 1, deep: lum(s.bg) < 0.42,
      schemes: p.schemes.length,
    };
  };

  /* 取反色字：色块 / 遮带上必须保证 3:1，否则标签读不出来。
     这是 JIZURA 的 onCol()，只是不引入它的对比度函数，直接用亮度差近似。 */
  PV.onCol = (pal, box) => {
    const a = Math.abs(lum(pal.bg) - lum(box)), b = Math.abs(lum(pal.fg) - lum(box));
    if (Math.max(a, b) < 0.22) return lum(box) > 0.5 ? '#111111' : '#FFFFFF';
    return a >= b ? pal.bg : pal.fg;
  };
})();
