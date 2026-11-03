#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pv_to_z.py —— 把我们的 storyboard 翻译成 Vocaloid-Style MV Pipeline 的编排稿。

    这是「运行时接入」的那一半：产物是一份 `engine/timeline.js`，由**上游引擎自己**的
    `engine/core/main.js` 执行（`Z.setup(audio)` → `Z.SHOTS` → `window.renderFrame(t)`），
    再用上游自带的 `tools/render_frames.mjs` 逐帧抓图、NVENC 编码。

    我们贡献的是「这一首的编辑决定」：切点、每镜的强度、配色、立绘、唱词。
    上游贡献的是「怎么画」：8 个场景、JIZURA 版式层、WebGL2 后期（LUT/bloom/颗粒/手描き boil）。

用法：
    python tools/pv_to_z.py --job _tmp/mvg/art_out/_work --dst _tmp/mvg/zrun
    python tools/pv_to_z.py --job ... --dst ... --lyric-only     # 只刷新歌词与立绘

产出（全部落在 --dst）：
    engine/timeline.js        编排稿（Z.SHOTS / JIZURA plans / LOOKS / HUD / 竖排配置）
    analysis/lyrics_mv.lrc    唱词（JIZURA 的时间轴来源）
    assets/char/*.png         立绘（从 songcut.figart 素材库取，只复制不改原图）
    analysis/z_link.json      映射报告（镜数、场景分布、未落位的字段）

纪律：本脚本只写 --dst 里的四个东西，不碰 _ref/ 下任何文件（上游 clone 保持只读）。
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ------------------------------------------------------------------ 配色
# 片子的颜色来自我们自己的 palette（noir pack）。上游场景吃的是「渐变色标数组」，
# 所以这里把一份配色摊成 4 档亮度，供不同 section 使用。
INK = "#0A0D12"
PAPER = "#F2F6FA"
ACCENT = "#FFB067"
ACCENT2 = "#C8373A"


def _grad(top, bot):
    return [[0, top], [0.55, _mix(top, bot, 0.5)], [1, bot]]


def _hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb2hex(t):
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(v)))) for v in t)


def _mix(a, b, k):
    ra, rb = _hex2rgb(a), _hex2rgb(b)
    return _rgb2hex([ra[i] + (rb[i] - ra[i]) * k for i in range(3)])


# ------------------------------------------------------------------ section → 视觉档
# 我们的 section 名是 A1..A7 / B1..B4 / TITLE；能量靠 tier 与 section 前缀。
# 上游的 scene 只有 8 个，这里只用「不依赖外部图片素材」的三种：
#   illust   —— 渐变底 + 粒子 + 人物，主力
#   kanjiSlam—— 巨大汉字砸拍，只给全片最高能的 4 镜，用多了会腻
#   typeCard —— 标题卡
#   afterimage —— 尾卡
def scene_for(shot, is_title, is_tail, high_energy):
    if is_title:
        return "typeCard"
    if is_tail:
        return "illust"          # 尾镜有 28 秒，afterimage 是为 2 秒的残影写的，套上去 1.5 秒就黑
    has_art = bool((shot.get("fig") or {}).get("art"))
    # 高能镜进漩涡：converge 自带粒子与收白，不需要任何外部素材
    if high_energy and not has_art:
        return "converge"
    return "illust"


def args_for(shot, scene, title, artist, credit, art_name="", art_uri=""):
    """按场景生成 args。全部是纯数据（可 JSON 化），不含函数，便于复核。

    kanjiSlam / afterimage 的预设里写着上游自带的立绘（`assets/char/lookup_tear.png`、
    `REF_master.png`），模板工程的 assets/ 是空的（只有 .gitkeep）——不显式置 null 就会 404。
    所以这两处要么传我们的立绘，要么明确关掉。
    """
    tier = int(shot.get("tier") or 1)
    sec = str(shot.get("section") or "")
    fig = shot.get("fig") or {}
    art = fig.get("art") or {}

    if scene == "typeCard":
        return {"lines": [title], "size": 112, "fam": "minchoHeavy", "weight": 700,
                "ruleColor": ACCENT, "sub": artist, "lead": 0.3}

    if scene == "afterimage":
        # 尾卡：用她的立绘做剪影；没有立绘给 1x1 透明图（上游 demo 同款做法），不报错
        return {"text": title, "sub": artist.lower(), "credit": credit, "color": "#A88BFF",
                "char": ("assets/char/" + (art_name or os.path.basename(art["src"]))) if art_name or art.get("src") else art_uri}

    if scene == "converge":
        # 漩涡收白：whiteAt 给本镜末尾，正好在切点上炸白
        return {"seed": 7, "whiteAt": round(float(shot.get("t1") or 0), 3)}

    # ---- illust：靠 section 换底色，靠 tier 换粒子密度
    warm = sec.startswith("B")
    if warm:
        grad = _grad(_mix(INK, ACCENT2, 0.18), _mix(INK, ACCENT2, 0.55))
    elif tier >= 3:
        grad = _grad(_mix(INK, ACCENT, 0.12), _mix(INK, ACCENT, 0.42))
    else:
        grad = _grad(INK, _mix(INK, "#1B2A3A", 0.85))
    a = {"grad": grad, "vignette": 0.42 if tier <= 1 else 0.32}

    fx = []
    if tier <= 1:
        fx.append({"type": "dust", "n": 28, "color": "rgba(200,220,255,0.30)"})
    else:
        fx.append({"type": "dust", "n": 46, "color": "rgba(255,225,190,0.34)"})
        fx.append({"type": "glow", "x": 1420, "y": 300, "r": 520,
                   "color": ACCENT, "a": 0.16, "beat": 0.4})
    if warm:
        fx.append({"type": "embers", "n": 90, "color": ACCENT, "rise": 130,
                   "size": 2.8, "area": [0, 240, 1920, 1160], "wind": 0.12})
    a["fx"] = fx

    # 立绘：上游 drawChar 的 front:true 会把人物画到 S.fg（文字层之上），
    # 所以「字沉到她身后」是引擎原生行为；她身上的洞（发缝/羽翼）决定字能透出多少。
    art = fig.get("art") or {}
    if art.get("src"):
        h = 900 if tier <= 2 else 960
        a["chars"] = [{
            "img": "assets/char/" + os.path.basename(art["src"]),
            "front": True,                      # 文字穿到她身后靠这一行
            "x": 1180, "y": 1090, "h": h,
            "sway": 0.010, "breathe": 0.9,
            "in": {"dur": 0.55, "dy": 26, "ds": 0.03},
            "shadow": {"a": 0.28, "dx": 34, "dy": 0},
        }]
    return a


# ------------------------------------------------------------------ 主流程
def build(job_dir, dst, dur, title, artist, credit, sub, limit=None):
    sb_path = os.path.join(job_dir, "storyboard.json")
    if not os.path.exists(sb_path):
        raise SystemExit("找不到 %s" % sb_path)
    with open(sb_path, encoding="utf-8") as f:
        sb = json.load(f)
    shots = sb["shots"]
    lines = sb.get("lines") or []

    # ---- 唱词：JIZURA 的时间轴来源。空的 [] 行要剔掉（JIZURA 会当成空句）。
    lrc_src = os.path.join(job_dir, "lyric.lrc")
    lrc_out = os.path.join(dst, "analysis", "lyrics_mv.lrc")
    n_lrc = 0
    if os.path.exists(lrc_src):
        kept = []
        with open(lrc_src, encoding="utf-8-sig") as f:
            for ln in f:
                s = ln.rstrip("\n")
                if not s.strip():
                    continue
                if s.startswith("[") and "]" in s and not s.split("]", 1)[1].strip():
                    continue
                if s.startswith("[ti:") or s.startswith("[ar:"):
                    continue
                kept.append(s)
                n_lrc += 1
        os.makedirs(os.path.dirname(lrc_out), exist_ok=True)
        with open(lrc_out, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(kept) + "\n")

    # ---- 立绘：只复制，不改原图；同名覆盖前先比大小
    n_art = 0
    char_dir = os.path.join(dst, "assets", "char")
    os.makedirs(char_dir, exist_ok=True)
    for sh in shots:
        src = ((sh.get("fig") or {}).get("art") or {}).get("src")
        if not src or not os.path.exists(src):
            if src:
                print("[warn] 立绘不存在，跳过：%s" % src)
            continue
        tgt = os.path.join(char_dir, os.path.basename(src))
        if not (os.path.exists(tgt) and os.path.getsize(tgt) == os.path.getsize(src)):
            shutil.copy2(src, tgt)
        n_art += 1

    # ---- 镜头：尾部那镜（通常是 241s 到片尾）给 afterimage
    real = [s for s in shots if s.get("t1", 0) > s.get("t0", 0) + 0.05]
    if limit:
        a, b = limit
        real = [s for s in real if s["t0"] < b and s["t1"] > a]
    out = []
    hi = max((int(s.get("tier") or 1) for s in real), default=1)
    # 全片只用一张立绘（素材库里挑定的那张）；kanjiSlam / afterimage 也复用它，避免模板的 404 素材
    art_name = ""
    for sh in real:
        _a = ((sh.get("fig") or {}).get("art") or {}).get("src")
        if _a:
            art_name = os.path.basename(_a)
            break
    art_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    for k, sh in enumerate(real):
        is_title = sh.get("kind") == "title"
        # 区间预演（--shots）时最后一镜不是片尾，不能套尾镜/尾卡的待遇
        is_tail = (k == len(real) - 1) and not is_title and not limit
        high = int(sh.get("tier") or 1) >= hi and hi >= 3
        scene = scene_for(sh, is_title, is_tail, high)
        tier = int(sh.get("tier") or 1)
        has_art = bool((sh.get("fig") or {}).get("art"))
        # 有她的镜头用 centerFree 版式：字会被推到中线两侧，避开她的脸
        plan = "cf" if has_art else ("noir" if tier >= 3 else "main")
        trans = {"type": "xfade", "dur": 0.5} if tier <= 1 else (
            {"type": "slices", "dur": 0.3} if tier == 2 else {"type": "flash", "dur": 0.14, "color": ACCENT})
        if is_title or k == 0:
            trans = None
        row = {
            "id": "s%02d" % sh["i"],
            "t0": round(float(sh["t0"]), 3),
            "t1": round(float(sh["t1"]), 3),
            "scene": scene,
            "args": args_for(sh, scene, title, artist, credit, art_name, art_uri),
            "lyric": "none" if scene in ("typeCard",) else "front",
            "plan": plan,
            "post": {"boil": 0.0 if is_title else 1.0},
            "hud": False if scene in ("typeCard",) else True,
        }
        if trans:
            row["trans"] = trans
        if is_tail:
            # 尾镜 28 秒（音乐已经淡出）：给它一点余光，别让最后 20 秒黑掉
            row["args"].update({
                "grad": [[0, _mix(INK, "#141018", 1.0)], [0.55, _mix(INK, ACCENT, 0.30)], [1, _mix("#2A1B14", ACCENT, 0.45)]],
                "vignette": 0.18,
                "fx": [{"type": "dust", "n": 40, "color": "rgba(255,232,200,0.36)"},
                       {"type": "glow", "x": 960, "y": 1040, "r": 1000, "color": ACCENT, "a": 0.30}],
            })
            # 器乐尾没有唱词，只留一块渐变会空掉：让她站在那里收尾
            if art_name and not row["args"].get("chars"):
                row["args"]["chars"] = [{
                    "img": "assets/char/" + art_name, "front": True,
                    "x": 1180, "y": 1090, "h": 900, "sway": 0.008, "breathe": 0.7,
                    "in": {"dur": 1.2, "dy": 30, "ds": 0.04},
                    "shadow": {"a": 0.30, "dx": 34, "dy": 0},
                }]
                row["plan"] = "cf"
                row["post"] = {"boil": 1.0, "bloom": 0.13, "bloomThreshold": 0.90}
        if scene == "converge":
            # 漩涡本身已经很亮，别再叠全片级色散和 bloom
            row["post"] = {"boil": 1.0, "chroma": 0.22, "bloom": 0.26}
        elif has_art:
            # 她的脸/头发本来就是高光区，按全片阈值会整片泛白（五官糊成一团）：只给她的镜头压 bloom
            row["post"] = {"boil": 1.0, "bloom": 0.13, "bloomThreshold": 0.90, "chroma": 0.34}
        out.append(row)

    # ---- 尾卡：我们最后一镜往往是 20 多秒的器乐尾，直接挂 illust 会「唱完就黑」。
    # 这里从最后一镜末尾切出 7 秒，改挂 typeCard 收尾（标题 + 原唱 + 演唱）。
    if out and out[-1]["scene"] == "illust" and (out[-1]["t1"] - out[-1]["t0"]) > 10 and not limit:
        end = out[-1]["t1"]
        out[-1]["t1"] = round(end - 7.0, 3)
        out.append({
            "id": "s_end", "t0": round(end - 7.0, 3), "t1": end, "scene": "typeCard",
            "args": {"lines": [title], "size": 84, "fam": "minchoHeavy", "weight": 700,
                     "ruleColor": ACCENT, "sub": credit, "lead": 0.4},
            "lyric": "none", "plan": "main", "post": {"boil": 0.6, "bloom": 0.34, "grain": 0.05},
            "hud": False, "trans": {"type": "black", "dur": 0.8},
        })

    spec = {
        "meta": {"title": title, "sub": sub, "artist": artist, "credit": credit,
                 "dur": dur, "fps": 30, "src": os.path.basename(job_dir)},
        "shots": out,
    }

    js = TL_TMPL.replace("__SPEC__", json.dumps(spec, ensure_ascii=False, indent=1))
    tl_out = os.path.join(dst, "engine", "timeline.js")
    os.makedirs(os.path.dirname(tl_out), exist_ok=True)
    with open(tl_out, "w", encoding="utf-8", newline="\n") as f:
        f.write(js)

    # ---- 映射报告
    dist = {}
    for r in out:
        dist[r["scene"]] = dist.get(r["scene"], 0) + 1
    rep = {"shots": len(out), "scene": dist, "lrc_lines": n_lrc, "art_refs": n_art,
           "dur": dur, "out": os.path.relpath(tl_out, dst)}
    with open(os.path.join(dst, "analysis", "z_link.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    return rep


TL_TMPL = r"""/* engine/timeline.js —— 由 tools/pv_to_z.py 生成，请勿手改。
 *
 *  这份编排稿跑在 Vocaloid-Style MV Pipeline 自带的引擎上：
 *  engine/core/main.js 调 await Z.setup(audio) 拿到 Z.SHOTS，然后每帧调用
 *  window.renderFrame(t)（t = 秒）。场景、JIZURA 版式层、WebGL2 后期全部来自上游。
 *
 *  我们在这里只声明「这一首的编辑决定」：切点、镜头强度、配色、立绘、唱词。
 *  SPEC.shots 是 pv_to_z.py 从 storyboard.json 翻译出来的纯数据。
 */
(() => {
  'use strict';
  const Z = window.Z, D = Z.draw, E = Z.ease;
  const W = 1920, H = 1080;

  const SPEC = __SPEC__;

  Z.TITLE = SPEC.meta.title;
  Z.TITLE_SUB = SPEC.meta.sub;
  Z.ARTIST = SPEC.meta.artist;
  Z.CREDIT = SPEC.meta.credit;

  Z.setup = async (audio) => {
    const secs = audio.sections || [];
    // 段落的 label 全是 'section'（analyze_audio 的自动结果），直接拿 label 当 look 名会全部落空、
    // LOOKS 等于没写。这里按能量重新分档：安静段 / 主歌 / 副歌 / 极安静的开头。
    for (const s of secs) s.look = (s.start < 5 && (s.energy || 0) < 0.3) ? 'intro'
      : ((s.energy || 0) >= 0.74 ? 'chorus' : ((s.energy || 0) >= 0.6 ? 'pre' : 'verse'));
    Z.sectionAt = t => { let s = secs[0] || null; for (const x of secs) if (x.start <= t + 1e-6) s = x; return s; };

    // 我们自己的片子：低照度、颗粒明显、bloom 收敛（和 pv_engine 的 noir 一档）
    Z.LOOK_DEFAULT = { boil: 1.0, bloom: 0.24, bloomThreshold: 0.84, grain: 0.055, vignette: 0.34, lutMix: 0.10 };
    Z.LOOKS = {
      intro: { boil: 0.7, grain: 0.04, bloom: 0.24 },
      pre: { boil: 1.05, grain: 0.06, beatCA: 1.2 },
      verse: { boil: 0.95, grain: 0.06 },
      chorus: { boil: 1.15, bloom: 0.38, beatBloom: 0.12, beatCA: 1.6 },
      tail: { boil: 0.6, grain: 0.04, bloom: 0.28 },
    };

    // 全片只在「高能镜的入口」闪一次，其余交给场景自己的粒子
    Z.globalFX = (P, t) => {
      for (const sh of SPEC.shots) {
        if (sh.scene !== 'kanjiSlam') continue;
        const d = t - sh.t0;
        if (d >= 0 && d < 0.3) P.flash = Math.max(P.flash || 0, 0.55 * Math.exp(-d * 12));
      }
    };

    // JIZURA 唱词层：读 analysis/lyrics_mv.lrc，四个 plan 对应四种版式气质
    let lrc = '';
    try { const r = await fetch(Z.ROOT + 'analysis/lyrics_mv.lrc'); if (r.ok) lrc = await r.text(); } catch (e) { /* 无歌词 */ }
    if (!lrc.trim()) console.warn('analysis/lyrics_mv.lrc missing: rendering without the JIZURA lyric layer');
    lrc = lrc.replace(/^\ufeff/, '').replace(/^\[ti:.*\]\s*$/m, '');
    if (lrc.trim() && window.JZ) {
      JZ.setAudio({ duration: audio.duration, beats: audio.beats, energy: null, energyRate: 0 });
      const DENY = {
        // 与上游 demo 同一套 deny 名单：挡掉 UI 小玩意、恐怖向、满幅特效（后半段由我们的后期负责）
        layout: 'scatter ring wave labels pill zigzag arcTop gridCells bubble ticker searchBar chat notification ticket hanging wordCloud bounceLine elastic stickerBomb keycaps bubbles slotMachine flipBoard dotMatrix equalizer tape contents numbered poster ema ransom newspaper vinyl cassette bookSpine stampSheet calendar chochin routeMap stationSign noren tanzaku omikuji kakejiku shoji clapper warningLabel priceTag nameTag stickyNotes karuta cube flipCards accordion pile blocks balloons magnets tiles bulbs ledScroll billboard crowdBubbles crossword wordSearch puzzle dominoes burst fisheye origami zipper mosaicTiles stencil tyIndexTable tyStatCount typeSpecimen dictionary proofread magazine headlineDeck footnote ribbon flag pendulum halftoneBig tySquare tyRotBlock subtitleBar tile kaleido polaroid postcard letterPaper genkou panels splitScreen splitHalves wall cylinder cube shadowPlay glitchGrid',
        fx: 'panelWipe irisTrans doors blindsTrans splitSlide gridRepeat whiteFrame blackFrame strobe colorBars mirrorFlash starGlint focusLines speedLines halftone duotone kaleido bulge squash snapshot loopScroll crtOff tvStatic zoomStutter negativeRing edgeDetect hueShift posterize ditherBit rotateSnap invert bandInvert flash bloomFlash',
        decor: 'dots arrows shapes qrBlock triangleSpin checkerStrip confetti petals heartsStars kamon seigaiha asanoha hanabi chochin shimenawa sensu momiji namiGashira memphis zigzagRibbon polkaPatch stripeCircle starburst cursorClick windowChrome progressBar toggleSwitch notifBell likeCounter mediaControls volumeBars musicNotes bubbles dandelion vines cloudPuffs tapePieces highlightMark staple paperClip indexTabs punchHoles swatches spinner tally qrBlock bracketsJP loopArrows halfCircles decoCorners tyColophon dateStamp tyTypeScale tyTextRule dataColumns timecodeBar rainStreaks',
        cam: 'knCardFlip jelly bounce knJumpCut knTiltKick knReadPan knShearKick knRushIn',
        treat: 'boxed marker knWordPlate circled sticker splitColor rainbow chrome',
        enter: 'bubbles stickerPeel crumple loadingBar tokoroten',
        exit: 'balloonOff crumpleOut peelOff',
        bg: 'argyle tartan chevron houndstooth herringbone polka checker retroGrid eqBars kaleidoscope fireworks sunburst bigStripes isoCubes seigaiha asanoha tvBars splitDiag spiralArms squareTunnel moire',
        trans: 'knCornerSwing knStripSlam checker cubeTurn spinOut doorsOpen pixelate knStutterCut',
      };
      const enabled = {};
      for (const g of J.GROUP_KEYS) {
        enabled[g] = {}; const deny = new Set((DENY[g] || '').split(/\s+/).filter(Boolean));
        for (const k of J.order(g)) {
          const d = J.registry(g)[k] || {};
          enabled[g][k] = k === 'none' ? true : (!deny.has(k) && !/^hr[A-Z]/.test(k) && d.set !== 'horror');
        }
      }
      const common = {
        enabled, lyrics: lrc, fps: 30, res: 1080, aspect: '16:9', seed: 20261101, unify: true, typeset: true,
        extra: true, wa: false, horror: false,
        colors: { accentOn: true, accent: '#FFB067', ghostA: '#39C6E0', ghostB: '#C8373A' }, schemes: 'dark',
        fx: { motion: 0.68, glitch: 0.32, chroma: 0.5, decor: 0.34, density: 0.46, texture: 0.5, flash: false, koma: 12, hud: 'off', bgSwitch: 0.3 },
      };
      await JZ.addPlan('main', Object.assign({}, common, { style: 'noir', mood: 'emotional' }));
      await JZ.addPlan('cf', Object.assign({}, common, { style: 'noir', mood: 'emotional', centerFree: true }));
      await JZ.addPlan('noir', Object.assign({}, common, { style: 'noir', mood: 'glitch', fx: Object.assign({}, common.fx, { glitch: 0.6, chroma: 0.75 }) }));
    }

    await Promise.all(['900 100px "Zen Old Mincho"', '400 17px "DotGothic16"', '400 24px "Zen Kaku Gothic New"']
      .map(f => document.fonts.load(f, Z.TITLE + Z.ARTIST + Z.CREDIT + '0123456789：/.—')));

    // ---- 镜头表：post 里可能指向 Z.typo.post.*（函数不能 JSON 化，靠 postFn 名字挂回来）
    const S = [];
    for (const sh of SPEC.shots) {
      const row = { id: sh.id, t0: sh.t0, t1: sh.t1, scene: sh.scene, args: sh.args, lyric: sh.lyric, plan: sh.plan, hud: sh.hud };
      let post = sh.post || {};
      if (sh.postFn === 'kanjiSlam' && Z.typo && Z.typo.post && Z.typo.post.kanjiSlam) {
        post = S => Object.assign(Z.typo.post.kanjiSlam(S), post);
      }
      row.post = post;
      if (sh.trans) row.trans = sh.trans;
      S.push(row);
    }
    Z.SHOTS = S;
    window.__zankoMeta.duration = SPEC.meta.dur;

    // ---- HUD：角标（时间码/小节/段落），标题卡与巨字镜关掉
    const SEC = t => { const s = Z.sectionAt(t); return s ? String(s.name || s.group || '—') : '—'; };
    const lastBar = audio.bars.length ? audio.bars[audio.bars.length - 1].index : 0;
    Z.hud = (ctx, St) => {
      const t = St.t, fade = Z.clamp(t / 1.2) * Z.clamp((SPEC.meta.dur - 1.0 - t) / 0.8);
      if (fade <= 0) return;
      const bar = Math.max(0, St.clock.bar(t) + 1);
      const f = Math.floor(t * 30), tc = [Math.floor(t / 60) % 60, Math.floor(t) % 60, f % 30].map(n => String(n).padStart(2, '0')).join(':');
      ctx.save(); ctx.globalAlpha = 0.46 * fade; ctx.fillStyle = '#F4EFE6'; ctx.strokeStyle = '#F4EFE6'; ctx.lineWidth = 1;
      ctx.shadowColor = 'rgba(0,0,0,0.5)'; ctx.shadowBlur = 4; ctx.textBaseline = 'top';
      D.font(ctx, 17, 'dot', 400); ctx.textAlign = 'left';
      ctx.fillText(Z.TITLE + ' / ' + Z.ARTIST, 44, 36);
      ctx.beginPath(); ctx.moveTo(44, 60); ctx.lineTo(44 + 150 + 40 * St.clock.pulse(t, 6), 60); ctx.stroke();
      ctx.fillText('SEC.' + SEC(t), 44, 68);
      ctx.textAlign = 'right';
      ctx.fillText(tc, W - 44, 36);
      ctx.fillText('BAR ' + String(bar).padStart(3, '0') + ' / ' + String(lastBar).padStart(3, '0'), W - 44, 58);
      ctx.textBaseline = 'alphabetic'; ctx.textAlign = 'left';
      ctx.fillText('♩=' + (60 / (St.clock.spb || 0.5)).toFixed(1), 44, H - 38);
      const ib = St.clock.inBar(t);
      for (let i = 0; i < 4; i++) { const x = W - 44 - (4 - i) * 16; ctx.strokeRect(x, H - 52, 10, 10); if (i === ib) ctx.fillRect(x, H - 52, 10, 10); }
      ctx.globalAlpha = 0.3 * fade; ctx.fillRect(44, H - 30, (W - 88) * Z.clamp(t / SPEC.meta.dur), 1);
      ctx.restore();
    };

    Z.VERTICAL_CFG = {
      full: SPEC.shots.filter(s => s.scene !== 'illust').map(s => s.id),
      planMap: { main: 'v_main', cf: 'v_main', noir: 'v_noir' },
      title: Z.TITLE, sub: Z.TITLE_SUB, artist: Z.ARTIST, credits: [0, SPEC.meta.dur],
      schemes: 'dark', ambience: [10, 6, 18],
    };
  };
})();
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="storyboard → Vocaloid-Style MV Pipeline 编排稿")
    ap.add_argument("--job", required=True, help="我们的工作区（含 storyboard.json / lyric.lrc）")
    ap.add_argument("--dst", required=True, help="上游工程的根目录（engine/ 在这里）")
    ap.add_argument("--title", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--credit", default="")
    ap.add_argument("--sub", default="")
    ap.add_argument("--dur", type=float, default=0.0, help="成片时长（秒）；不给就取 storyboard.meta.dur")
    ap.add_argument("--shots", default="", help="只翻译某个区间，如 0:12（调试用）")
    a = ap.parse_args(argv)

    sb = os.path.join(a.job, "storyboard.json")
    meta = {}
    if os.path.exists(sb):
        with open(sb, encoding="utf-8") as f:
            meta = json.load(f).get("meta") or {}
    title = a.title or meta.get("title") or "UNTITLED"
    artist = a.artist or meta.get("vocal") or meta.get("artist") or ""
    credit = a.credit or ("Vocal %s / Song %s" % (meta.get("vocal", ""), meta.get("artist", ""))).strip(" /")
    sub = a.sub or (meta.get("artist") or "")
    dur = a.dur or float(meta.get("dur") or 0)
    limit = None
    if a.shots:
        p, q = a.shots.split(":")
        limit = (float(p), float(q))

    rep = build(a.job, a.dst, dur, title, artist, credit, sub, limit)
    print("[z] %d 镜：%s" % (rep["shots"], rep["scene"]))
    print("[z] 唱词 %d 行 · 立绘引用 %d 处 · 时长 %.2fs" % (rep["lrc_lines"], rep["art_refs"], rep["dur"]))
    print("[z] → %s" % os.path.join(a.dst, "engine", "timeline.js"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
