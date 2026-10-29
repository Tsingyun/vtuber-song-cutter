/* pv_font_specimen.cjs —— 把 PV.FACES 里的中文族排成一张样张，人眼过一遍。
 *
 * 为什么要看样张而不是只看 cmap：
 *   cmap 只能证明「字形表里有这个码位」，证明不了「浏览器真的用上了这个族」。
 *   CSS 里 family 名写错、font-weight 没注册、font-style 不匹配，浏览器都会**静默**
 *   落到兜底（Noto Sans SC），cmap 检查全绿、画面全错。样张里同一句话若有两行
 *   长得一模一样，就是其中一个没加载上。
 *
 * 用法：
 *   node tools/pv_font_specimen.cjs [--out _tmp/pv/font_specimen.png] [--text "…"]
 */
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright');

const arg = (k, dv) => {
  const i = process.argv.indexOf('--' + k);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : dv;
};

(async () => {
  const out = arg('out', '_tmp/pv/font_specimen.png');
  const text = arg('text', '永和九年 岁在癸丑 暮春之初 会于会稽山阴');
  const b = await chromium.launch({ args: ['--allow-file-access-from-files'] });
  const p = await b.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 });
  const url = 'file:///' + path.join(process.cwd(), 'renderer/player/index_pv.html').replace(/\\/g, '/') + '?res=1';
  await p.goto(url, { waitUntil: 'load' });
  await p.waitForFunction('window.PV && PV.FACES');

  const rows = await p.evaluate(async (txt) => {
    const keys = Object.keys(PV.FACES).filter(k => PV.FACES[k].cjk);
    // 先把每个字面按真实字号预热，量到的宽度才是「已加载」的宽度
    await PV.warmFonts(txt, [64]);
    await document.fonts.ready;
    const cv = document.createElement('canvas');
    const W = 1280, H = 72 * keys.length + 20;
    cv.width = W; cv.height = H;
    const g = cv.getContext('2d');
    g.fillStyle = '#F7F3EC'; g.fillRect(0, 0, W, H);
    const res = [];
    keys.forEach((k, i) => {
      const y = 60 + i * 72;
      g.font = '400 20px "IBM Plex Mono", monospace';
      g.fillStyle = '#8A8580';
      g.textAlign = 'left';
      g.fillText(k, 16, y - 18);
      g.font = PV.cssFace(k, 44);
      g.fillStyle = '#1A1A1A';
      g.fillText(txt, 130, y);
      // 顺带量一下「整串宽度」：同一个字在不同族里宽度应当不同，全相同 = 全部 fallback
      res.push({ k, w: +g.measureText(txt).width.toFixed(1) });
    });
    return { png: cv.toDataURL('image/png'), rows: res };
  }, text);

  fs.writeFileSync(out, Buffer.from(rows.png.split(',')[1], 'base64'));
  // 宽度指纹：把宽度相同的面归成一组，组里有 >1 个就提示「可能有一个没加载上」
  const byW = {};
  rows.rows.forEach(r => { (byW[r.w] = byW[r.w] || []).push(r.k); });
  const dup = Object.keys(byW).filter(w => byW[w].length > 1);
  console.log('[specimen] ' + rows.rows.length + ' 个中文字面 → ' + out);
  if (dup.length) {
    console.log('[注意] 这些字面宽度完全相同，可能有一个静默 fallback：');
    dup.forEach(w => console.log('   ' + w + 'px  ' + byW[w].join(' / ')));
  } else {
    console.log('[ok] 每个中文字面的量宽都不同（说明各自真的加载上了）');
  }
  await b.close();
})();
