#!/usr/bin/env node
/* JIZURA 帧序列层：离线预渲 PNG 序列，交给 pv_direct / pv_engine 当一层贴图合成。
 *
 *   node tools/pv_jz.cjs --timing <timing.json> --out _tmp/jz/j1 \
 *        --start 90 --dur 4 --fps 6 --bpm 138.46 --res 1080 --style noir [--accent '#5A5897']
 *
 * 为什么是「预渲帧序列」而不是运行时直画 JIZURA：
 *   JIZURA 每帧要跑三趟色散 + 纸纹，4K 下逐帧直画会把渲染时间翻几倍；而帧序列在我们
 *   这边只是一层贴图，渲染热路径零新增成本，确定性又由「同一批 PNG」直接保证。
 *
 * 关键一点：**歌词时间轴用我们自己的**。给 JIZURA 的 LRC 由 timing.json 生成、
 * 并按窗口起点整体平移，不让它自己检测 —— 否则「加入动画导致歌词整体延迟」这条
 * 禁止项一定会被踩到（JIZURA 有它自己的前奏/间奏规划）。
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const argOf = (k, d) => { const i = process.argv.indexOf('--' + k); return i > 0 ? process.argv[i + 1] : d; };

const timingPath = argOf('timing', '');
const outDir = path.resolve(ROOT, argOf('out', '_tmp/jz/j1'));
const start = Number(argOf('start', '0'));
const dur = Number(argOf('dur', '4'));
const fps = Number(argOf('fps', '6'));
const bpm = Number(argOf('bpm', '0'));
const res = Number(argOf('res', '1080'));
const style = argOf('style', 'noir');
const accent = argOf('accent', '');
const seed = Number(argOf('seed', '20261027'));
/* 2026-11-02 晚：接上官方 theme 与配色子集。
 *   --theme  <lyricpv|kinetic|wa|horror|pop|ballad>  走 J.omakase（会换掉 --style，见 RULES §117.3）
 *   --schemes <dark|light|0|0,1|all>                 缺省 dark（= 原有行为）；'all' = 不限制
 *   --deny   <a,b,c>                                 按 layout 名关掉（中文长句下读不清的版式）
 *   --theme-seed <n>                                 omakase 的随机种子，默认 0x9e3779b9
 *   --single                                         一句一镜（不再按 0.5~1.3s 切碎）
 * 三个都不传时，输出与改动前逐字节一致。 */
const theme = argOf('theme', '');
const themeSeed = Number(argOf('theme-seed', '2654435769'));
const deny = (argOf('deny', '') || '').split(',').map(s => s.trim()).filter(Boolean);
/* --single：一句歌词一个镜头（引擎 nC=1）。不给就还是官方默认的多镜头快切。 */
const single = process.argv.includes('--single');
let schemes = 'dark';
const schemesArg = argOf('schemes', '');
if (schemesArg) {
  if (schemesArg === 'dark' || schemesArg === 'light') schemes = schemesArg;
  else if (schemesArg === 'all') schemes = null;
  else {
    const idx = schemesArg.split(',').map(x => parseInt(x, 10));
    if (idx.some(n => !Number.isFinite(n) || n < 0)) {
      console.error('--schemes 只接受 dark | light | all | 逗号分隔的索引'); process.exit(2);
    }
    schemes = idx;
  }
}
/* 2026-11-02：新增「整帧」输出。
 * 原来只出透明 PNG，JIZURA 只能当一层贴图（背景、装饰、转场全由我们的引擎出）。
 * 出整帧时它自己就是画面：背景平涂 + 纸纹 + 三趟色散 + post 全都在引擎内部完成，
 * 我们这边只负责把帧按时序列贴上去。`normal` = 带背景，`transparent` = 只要字。 */
const mode = argOf('mode', 'transparent');
const fmt = argOf('fmt', 'png');
const quality = Number(argOf('q', '92'));
if (mode !== 'transparent' && mode !== 'normal') {
  console.error('--mode 只接受 transparent | normal'); process.exit(2);
}
if (fmt !== 'png' && fmt !== 'jpg') { console.error('--fmt 只接受 png | jpg'); process.exit(2); }
const MIME = fmt === 'png' ? 'image/png' : 'image/jpeg';
const DATAHDR = 'data:' + MIME + ';base64,';

if (!timingPath) { console.error('缺 --timing'); process.exit(2); }
const tdoc = JSON.parse(fs.readFileSync(path.resolve(ROOT, timingPath), 'utf8'));

/* ---- 从 timing.json 生成窗口内的 LRC（时间整体减 start） ---- */
const mmss = s => {
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return String(m).padStart(2, '0') + ':' + sec.toFixed(2).padStart(5, '0');
};
const lines = (tdoc.lines || []).filter(l => l.t1 > start + 0.05 && l.t0 < start + dur - 0.05);
if (!lines.length && !argOf('lrc', '')) { console.error('窗口内没有歌词行'); process.exit(3); }
/* --lrc <file>：直接用给好的 LRC，不从 timing.json 重建。
 * 为什么需要：timing.json 的 lines 里没有间奏、没有段落空行，还带着制作人员行；
 * 而 JIZURA 的分镜止于最后一句歌词（尾部会纯黑，§115.3），多样性又靠段落空行（§116.2）。
 * 这些都得在 LRC 层面表达，所以允许外部传入。传了就不做时间平移，按绝对时间用。 */
const lrcFile = argOf('lrc', '');
const lrc = lrcFile
  ? fs.readFileSync(path.resolve(ROOT, lrcFile), 'utf8').replace(/\r/g, '')
  : lines.map(l => {
      const t = Math.max(0, (l.t0 - start));
      return '[' + mmss(t) + ']' + String(l.text || '').trim();
    }).join('\n');

/* 拍点：由 bpm 推等间距网格（bpm 是实测的，网格是推导的 —— 这一点写进输出 JSON 的 note）。
   不传 beats 会让 JIZURA 用自己的一致性检测，和我们的时间轴未必同步。 */
const beats = [];
if (bpm > 0) {
  const b = 60 / bpm;
  for (let t = 0; t <= dur + 1e-6; t += b) beats.push(Number(t.toFixed(4)));
}

(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch({ args: ['--allow-file-access-from-files', '--enable-gpu'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errs = [];
  page.on('pageerror', e => errs.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errs.push('[console] ' + m.text()); });

  await page.goto('file:///' + path.join(ROOT, 'renderer/pv/probe.html').replace(/\\/g, '/'));
  await page.waitForFunction('typeof window.__init === "function"', { timeout: 30000 });

  const sum = await page.evaluate(async cfg => window.__init(cfg), {
    dur, beats, bpm, lrc, style, res, seed,
    accent: accent || undefined,
    title: tdoc.meta && tdoc.meta.title || argOf('title', ''),
    artist: tdoc.meta && tdoc.meta.artist || argOf('artist', ''),
    lang: argOf('lang', 'auto'),
    theme: theme || undefined, themeSeed, deny, single,
    schemes: schemes === 'dark' && !schemesArg ? undefined : schemes,
  });

  const n = Math.max(1, Math.round(dur * fps));
  const t0 = Date.now();
  for (let i = 0; i < n; i++) {
    const t = i / fps;
    const url = await page.evaluate(a => window.__shot(a.t, a.mode, a.mime, a.q),
      { t, mode, mime: MIME, q: quality / 100 });
    if (!url || url.indexOf(DATAHDR) !== 0) {
      console.error('[jz] 第 %d 帧没拿到 %s', i, fmt.toUpperCase()); process.exit(4);
    }
    const buf = Buffer.from(url.slice(DATAHDR.length), 'base64');
    fs.writeFileSync(path.join(outDir, 'j_' + String(i + 1).padStart(5, '0') + '.' + fmt), buf);
  }
  await browser.close();

  const meta = {
    kind: 'jizura-seq', dir: outDir.replace(/\\/g, '/'), n, fps,
    w: sum.W || res, h: sum.H || res, pattern: 'j_%05d.' + fmt,
    mode,
    source: { timing: timingPath, start, dur, style, bpm, res, seed, lines: lines.length,
              mode, fmt, q: quality },
    plan: sum,
    note: 'beats 由 bpm 推等间距网格（非 onset 检测）；歌词时间来自 timing.json，窗口内已平移',
    ms: Date.now() - t0,
  };
  fs.writeFileSync(path.join(outDir, 'jz.json'), JSON.stringify(meta, null, 1));
  /* 不能用 %d/%.1f 这类 printf 说明符：Node 的 util.format 只认 %d %i %f %s %j %o %O %c %%，
     `%.1f` 不是合法说明符 —— 它会被原样输出且**不消耗参数**，后面每一个占位符都跟着错位
     （实测把 style 打成 1080、lines 打成 NaN）。一律用模板串，别用 printf。 */
  console.log(`[jz] ${n} 帧 ${dur.toFixed(1)}s  ${meta.w}x${meta.h}  style=${sum.style} `
    + `lines=${sum.lines} cuts=${sum.cuts}  ${(meta.ms / Math.max(1, n)).toFixed(0)}ms/帧`);
  if (sum.missingFonts && sum.missingFonts.length) console.log('[jz] ! 缺字体 %s', sum.missingFonts.join(','));
  if (errs.length) console.log('[jz] ! 页面错误 %d 条：%s', errs.length, errs.slice(0, 3).join(' | '));
})();
