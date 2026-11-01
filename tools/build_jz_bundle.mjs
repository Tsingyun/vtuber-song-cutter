/*! build_jz_bundle.mjs —— 从上游 src/ 重建 renderer/pv/vendor/jizura_engine.js
 *
 * 为什么需要这个脚本（2026-11-01 补）
 * -----------------------------------
 * 2026-10-28 当初是把 bundle 直接拷进来的，而 `renderer/pv/vendor/*.js` 被 .gitignore
 * 排除 —— 于是换一台机器 clone 下来，JIZURA 通道就永久缺失，且 README 指向的这个脚本
 * 根本不存在（审计发现的隐患 1）。这里把它补齐，让 bundle 变成「可复现产物」。
 *
 * 复刻的是什么规则
 * ----------------
 * 上游 build.py 的拼接方式是 `sorted(glob('src/*.js'))` 后 `'\n'.join(...)`，
 * 再把 `@VERSION@` 换成 VERSION 文件内容。我们只做两处删减：
 *   · 去掉 `12_ui.js`（浏览器编辑器界面，我们不跑 UI）
 *   · 不拼 `vendor/mp4-muxer.min.js`（导出走我们自己的链路；11_export.js 里对
 *     Muxer 的引用是惰性的，不会被调用，所以不影响加载）
 * 再在最前面加一行署名头（MIT 要求保留版权声明）。
 *
 * 用法：
 *   node tools/build_jz_bundle.mjs                  # 用默认 _ref/JIZURA
 *   node tools/build_jz_bundle.mjs <JIZURA仓库> [--check]
 *     --check  只比对，不写盘；不一致时退出码 1（可用于 CI / 换机自检）
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.dirname(HERE);
const DEFAULT_SRC = path.join(ROOT, '_ref', 'JIZURA');
const OUT = path.join(ROOT, 'renderer', 'pv', 'vendor', 'jizura_engine.js');

const argv = process.argv.slice(2);
const checkOnly = argv.includes('--check');
const srcArg = argv.find(a => !a.startsWith('--'));
const srcRepo = path.resolve(srcArg || DEFAULT_SRC);

const EXCLUDE = new Set(['12_ui.js']);

function die(msg) {
  console.error('[jz-bundle] ' + msg);
  process.exit(2);
}

if (!fs.existsSync(srcRepo)) die('找不到上游仓库: ' + srcRepo + '（clone https://github.com/852wa/JIZURA 到 _ref/JIZURA）');
const srcDir = path.join(srcRepo, 'src');
if (!fs.existsSync(srcDir)) die('找不到 src/: ' + srcDir);

const version = fs.readFileSync(path.join(srcRepo, 'VERSION'), 'utf8').trim();

/* 排序必须与上游 build.py 一致（sorted glob），否则 02b_lang 之类会排到 03 后面 */
const files = fs.readdirSync(srcDir)
  .filter(f => f.endsWith('.js') && !EXCLUDE.has(f))
  .sort();

if (!files.length) die('src/ 里没有可拼接的 .js');

const header = `/*! JIZURA 字面 v${version} engine (MIT, (c) 2026 hakoniwa, github.com/852wa) — see JIZURA_LICENSE.txt */\n`;

let body = files.map(f => fs.readFileSync(path.join(srcDir, f), 'utf8')).join('\n');
/* @VERSION@ 在 12_ui/body.html 之外也出现在 src 里（HUD 与导出文件名），上游同样做全局替换 */
body = body.split('@VERSION@').join(version);

const out = header + body;

const sha = s => crypto.createHash('sha256').update(s, 'utf8').digest('hex').slice(0, 16);

/* git 在入库时会把 CRLF 归一成 LF（见 `git add` 的 warning），所以「换机器后自检」
 * 必须按行尾归一比较，否则内容相同也会报不一致。真正写盘时仍按上游原样输出。 */
const norm = s => s.replace(/\r\n/g, '\n');

if (checkOnly) {
  if (!fs.existsSync(OUT)) die('产物不存在: ' + OUT);
  const cur = fs.readFileSync(OUT, 'utf8');
  const same = norm(cur) === norm(out);
  console.log(`[jz-bundle] 上游 ${version} / ${files.length} 文件 / ${out.length} 字节`);
  console.log(`[jz-bundle] 现有 ${cur.length} 字节  sha=${sha(norm(cur))}`);
  console.log(`[jz-bundle] 重建 ${out.length} 字节  sha=${sha(norm(out))}`);
  console.log(same ? '[jz-bundle] 一致 ✓（按行尾归一比较）' : '[jz-bundle] 不一致 ✗');
  process.exit(same ? 0 : 1);
}

fs.mkdirSync(path.dirname(OUT), { recursive: true });
fs.writeFileSync(OUT, out, 'utf8');
console.log(`[jz-bundle] ${path.relative(ROOT, OUT)}  ${out.length} 字节  sha=${sha(out)}  (上游 ${version} / ${files.length} 文件)`);
