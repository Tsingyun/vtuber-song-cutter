#!/usr/bin/env node
// kdocs_fetch.cjs —— 读取在线表格（金山文档）指定工作表：当日演唱歌单
// 用法：node kdocs_fetch.cjs <输出JSON路径> [kdocs链接]
// 输出：{ ok, fetched_at, source, sheet_names, songs_by_date: { "YYYY-MM-DD": [{name, requester, note}] } }
// 依赖：playwright（由调用方通过 NODE_PATH 提供）
const path = require('path');

/* 默认留空：实际地址由调用方从 config.json 的 songlist.url 传入 */
const URL_DEFAULT = '';
const SHEET_INDEX = 3; // 第3个表格（1-based）

// Excel 序列号 → YYYY-MM-DD（Windows 1900 系统：1899-12-30 起算）
function serialToDate(n) {
  const ms = (n - 25569) * 86400 * 1000;
  const d = new Date(ms + 6 * 3600 * 1000); // 按东八区日界取整
  const y = d.getUTCFullYear(), m = d.getUTCMonth() + 1, day = d.getUTCDate();
  return '%04d-%02d-%02d'.replace('%04d', String(y).padStart(4, '0'))
    .replace('%02d', String(m).padStart(2, '0')).replace('%02d', String(day).padStart(2, '0'));
}

function normCell(v) {
  if (v === null || v === undefined) return '';
  if (v instanceof Date) return v.toISOString().slice(0, 10);
  if (typeof v === 'number') return v;
  return String(v).trim();
}

function parseDateCell(v) {
  if (v instanceof Date) return v.toISOString().slice(0, 10);
  if (typeof v === 'number') return serialToDate(v);
  const s = String(v).trim();
  if (/^\d{5}(\.\d+)?$/.test(s)) return serialToDate(parseFloat(s));   // 文本型序列号
  const m = s.match(/(\d{4})[\/\-年](\d{1,2})[\/\-月](\d{1,2})/);
  if (m) return m[1] + '-' + String(+m[2]).padStart(2, '0') + '-' + String(+m[3]).padStart(2, '0');
  return s || null;
}

(async () => {
  const outPath = process.argv[2];
  const kdocsUrl = process.argv[3] || URL_DEFAULT;
  const { chromium } = require('playwright');
  const result = { ok: false, fetched_at: new Date().toISOString(), source: kdocsUrl,
    sheet_names: [], songs_by_date: {}, error: null };

  const browser = await chromium.launch({ args: ['--mute-audio'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
    await page.goto(kdocsUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForTimeout(12000);                       // 等文档应用加载
    await page.keyboard.press('Escape');                    // 关协作弹窗
    await page.waitForTimeout(1000);

    const data = await page.evaluate(async (sheetIdx) => {
      const out = {};
      const api = window.WPSOpenApi;
      if (!api) throw new Error('WPSOpenApi 不存在（页面未加载完或结构变更）');
      const app = await api.EtApplication();
      const wb = await app.ActiveWorkbook;
      const sheets = await wb.Sheets;
      const count = await sheets.Count;
      out.sheet_names = [];
      for (let i = 1; i <= count; i++) out.sheet_names.push(await (await sheets.Item(i)).Name);
      if (sheetIdx > count) throw new Error('只有 ' + count + ' 个表，找不到第 ' + sheetIdx + ' 个');
      const sh = await sheets.Item(sheetIdx);
      const ur = await sh.UsedRange;
      const rows = await (await ur.Rows).Count;
      // 分块读 A:E（日期/歌曲名/点歌人/备注/拼好歌）
      const CH = 200;
      const grid = [];
      for (let r0 = 1; r0 <= rows; r0 += CH) {
        const r1 = Math.min(r0 + CH - 1, rows);
        const rg = await sh.Range('A' + r0 + ':E' + r1);
        const v = await rg.Value;
        for (const row of (v || [])) grid.push(row);
      }
      out.grid = grid;
      return out;
    }, SHEET_INDEX);

    result.sheet_names = data.sheet_names;
    if (data.sheet_names.length >= SHEET_INDEX)
      result.sheet_used = data.sheet_names[SHEET_INDEX - 1];

    // 归组：日期列向下传播（同日多歌只写在首行）
    let cur = null;
    for (const row of data.grid) {
      const cA = normCell(row[0]), cB = normCell(row[1]);
      if (cA !== '' && !/日期/.test(String(cA))) cur = parseDateCell(cA);
      if (!cur || cB === '') continue;
      if (cB === '歌曲名' || /日期/.test(String(cA))) continue;   // 表头
      if (!result.songs_by_date[cur]) result.songs_by_date[cur] = [];
      result.songs_by_date[cur].push({ name: String(cB), requester: normCell(row[2]) ? String(row[2]) : '', note: normCell(row[3]) ? String(row[3]) : '' });
    }
    result.days = Object.keys(result.songs_by_date).length;
    result.ok = result.days > 0;
    if (!result.ok) result.error = '解析后无任何日期数据（表结构可能变更）';
  } catch (e) {
    result.error = String(e && e.message || e).slice(0, 300);
  } finally {
    await browser.close().catch(() => {});
  }

  const fs = require('fs');
  fs.writeFileSync(outPath, JSON.stringify(result, null, 1), 'utf8');
  console.log(result.ok ? 'OK days=' + result.days : 'FAIL ' + result.error);
  process.exit(result.ok ? 0 : 2);
})();
