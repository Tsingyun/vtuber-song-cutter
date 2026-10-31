/* pv_pair_specimen.cjs —— 一首歌的「字面配对」样张：标题 / 歌词 / 档案行
 *
 * 为什么不用 pv_font_specimen.cjs：
 *   那个把 16 个中文字面排成一列，是「体检」；这里要的是「决策」——同一句歌词、
 *   同一个尺寸、同一张纸，只换字面，看哪一种配得上这首歌的情绪。选字不是选
 *   字库覆盖率，是在选「笔画的呼吸」。
 *
 * 判据（人眼，但要按这个顺序看）：
 *   ① 同一行里有没有字突然变粗/变形（= 落到兜底，全怪）;
 *   ② 大标题有没有「被压扁」的横竖对比（宋/明体才有，黑体没有）;
 *   ③ 小字歌词在 4K 缩放 50% 后还认不认得出笔画（细过头 = 远处糊成一片）;
 *   ④ 标题与歌词是不是同一个「笔的时代」（毛笔标题 + 现代黑体 = 拼贴）。
 *
 * 用法：
 *   node tools/pv_pair_specimen.cjs --title 画心 --lyric "看不穿 是你失落的魂魄" \
 *        --credit "张靓颖 · 画皮 · 2008" --out _tmp/pv/pair.png
 */
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright');

const arg = (k, dv) => {
  const i = process.argv.indexOf('--' + k);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : dv;
};

(async () => {
  const out = arg('out', '_tmp/pv/pair.png');
  const title = arg('title', '画心');
  const lyric = arg('lyric', '看不穿 是你失落的魂魄');
  const credit = arg('credit', '张靓颖 · 画皮 · 2008');
  const bg = arg('bg', '#F2EDE4');
  const fg = arg('fg', '#2B2B33');
  const fg2 = arg('fg2', '#3A3A42');
  const isDark = !!arg('dark', '');
  const titleFaces = arg('title-faces', 'cnBrush,cnXing,cnSerifB,cnElegant').split(',');
  const lyricFaces = arg('lyric-faces', 'cnSerifLt,cnKai,cnKaiLt,cnElegant').split(',');

  const b = await chromium.launch({ args: ['--allow-file-access-from-files'] });
  const p = await b.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  const url = 'file:///' + path.join(process.cwd(), 'renderer/player/index_pv.html').replace(/\\/g, '/') + '?res=1';
  await p.goto(url, { waitUntil: 'load' });
  await p.waitForFunction('window.PV && PV.FACES');

  const png = await p.evaluate(async ({ title, lyric, credit, bg, fg, fg2, isDark, titleFaces, lyricFaces }) => {
    const TITLE_FACES = titleFaces;
    const LYRIC_FACES = lyricFaces;
    const warm = title + lyric + credit + '画心张靓颖画皮';
    await PV.warmFonts(warm, [240, 96, 22]);
    await document.fonts.ready;

    const W = 1920, H = 1080;
    const cv = document.createElement('canvas');
    cv.width = W; cv.height = H;
    const g = cv.getContext('2d');

    /* 纸底：不用引擎的 paperFiber（那要整套 drawPaper），这里只要「不是纯白」，
       否则细宋的笔画会在纯白上读起来比 4K 成片里更脆，判断会偏。 */
    g.fillStyle = bg; g.fillRect(0, 0, W, H);
    for (let i = 0; i < 26000; i++) {
      const x = Math.random() * W, y = Math.random() * H;
      g.fillStyle = isDark
        ? 'rgba(220,210,190,' + (0.012 + Math.random() * 0.028) + ')'
        : 'rgba(120,100,70,' + (0.02 + Math.random() * 0.05) + ')';
      g.fillRect(x, y, 1 + Math.random() * 2, 1);
    }

    const label = (t, x, y, col) => {
      g.font = '400 18px "IBM Plex Mono", monospace';
      g.fillStyle = col || '#9A9184';
      g.textAlign = 'left';
      g.fillText(t, x, y);
    };

    /* ---- 标题：四个字面横排，各自独立居中，字号 190 ---- */
    const tw = W / 4;
    TITLE_FACES.forEach((k, i) => {
      const cx = tw * i + tw / 2;
      g.textAlign = 'center';
      g.textBaseline = 'alphabetic';
      g.font = PV.cssFace(k, 190);
      g.fillStyle = fg;
      g.fillText(title, cx, 250);
      label(k + '  ·  ' + PV.FACES[k].family.replace(/"/g, ''), cx, 300);
    });

    /* ---- 歌词：四个字面各占一行，左对齐，字号 84 ---- */
    LYRIC_FACES.forEach((k, i) => {
      const y = 430 + i * 118;
      label(k, 90, y - 44);
      g.textAlign = 'left';
      g.font = PV.cssFace(k, 84);
      g.fillStyle = fg2;
      g.fillText(lyric, 300, y);
    });

    /* ---- 档案行：原唱 / 出处 / 年份，是「画面标原唱」的落点 ---- */
    g.textAlign = 'left';
    g.font = '400 30px "IBM Plex Mono", monospace';
    g.fillStyle = '#8A7F6E';
    g.fillText(credit, 90, 980);
    g.font = '400 22px "IBM Plex Mono", monospace';
    g.fillStyle = '#A79E90';
    g.fillText('VOCAL 岁己SUI / ORIGINAL ' + credit, 90, 1024);
    return cv.toDataURL('image/png');
  }, { title, lyric, credit, bg, fg, fg2, isDark, titleFaces, lyricFaces });

  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, Buffer.from(png.split(',')[1], 'base64'));
  console.log('[pair] ' + out);
  await b.close();
})();
