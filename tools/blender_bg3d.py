# -*- coding: utf-8 -*-
"""在 Blender 里渲一段「可当 MV 背景」的三维镜头，输出图片序列。

定位
----
本文件**只能**由 Blender 执行（`blender -b -P`），不参与当前仓库的 Python 包。
它和 tools/bg3d_seq.py 是同一件事的两个产出端：都吐 PNG 序列，编排稿的
`shot.bg3d` 只认序列，不认是谁渲的 —— 所以 Blender 始终是可选项：
本机没装 Blender 也能出片（用 pv_bg 的程序化背景，或 bg3d_seq 的合成序列）。

用法
----
    "C:/Program Files/Blender Foundation/Blender 4.2/blender.exe" -b \
        -P tools/blender_bg3d.py -- \
        --out _tmp/b3d_real --sec 6 --fps 12 --w 960 --h 540 --style ink

    # 再进管线：
    python tools/pv_prepare.py ... --bg3d _tmp/b3d_real --bg3d-fps 12

约定（改这里之前先想清楚下游）
------------------------------
  · 文件命名 %05d.png 从 1 开始 —— 与渲染器 seqPath 的 %05d 逐位对得上；
    Blender 的 frame_start 会从 1 起算，所以 frame_start 必须写 1，不要写 0。
  · 帧率写进场景（scene.render.fps）**并且**在命令行用 --fps 声明，
    两边不一致时下游按 --fps 取帧，会整体错位 —— 所以这里禁止两者不同。
  · 输出必须是不带透明通道的整幅画面（film_transparent=False），
    本层是「场景」，透明底该由 pv_bg 的遮罩去做，不在这里做。

风格
----
  ink     : 深蓝夜 + 霓虹地面网格 + 缓慢推轨（默认，配文字 PV 最稳）
  paper   : 米白纸感 + 墨线（EEVEE + 卡通描边），配浅底排版
  void    : 纯黑 + 单色光，配大字冲击段
"""
import argparse
import math
import os
import sys

import bpy


def parse():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--sec", type=float, default=6.0)
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--w", type=int, default=960)
    ap.add_argument("--h", type=int, default=540)
    ap.add_argument("--style", default="ink", choices=["ink", "paper", "void"])
    ap.add_argument("--dolly", type=float, default=1.6, help="相机推轨距离（米）")
    ap.add_argument("--seed", type=int, default=20261103)
    return ap.parse_args(argv)


def wipe():
    """清空默认场景（-b 模式下会带出 Cube/Camera/Light）。"""
    bpy.ops.wm.read_factory_settings(use_empty=True)


def mat(name, rgb, emit=0.0, rough=0.6):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (rgb[0], rgb[1], rgb[2], 1)
        bsdf.inputs["Roughness"].default_value = rough
        if emit > 0:
            for key in ("Emission Color", "Emission"):
                if key in bsdf.inputs:
                    bsdf.inputs[key].default_value = (rgb[0], rgb[1], rgb[2], 1)
                    break
            if "Emission Strength" in bsdf.inputs:
                bsdf.inputs["Emission Strength"].default_value = emit
    return m


def build(style, seed):
    """搭一个「够用的三维背景」：地面 + 几组柱/环 + 一盏主光 + 一台推轨相机。"""
    pal = {
        "ink": dict(floor=(0.02, 0.02, 0.05), bars=[(0.10, 0.55, 0.75),
                                                   (0.95, 0.62, 0.20),
                                                   (0.85, 0.25, 0.42)],
                    key=(1.0, 0.92, 0.80), world=0.02, emit=2.5),
        "paper": dict(floor=(0.92, 0.90, 0.85), bars=[(0.85, 0.35, 0.25),
                                                      (0.20, 0.35, 0.55),
                                                      (0.35, 0.30, 0.28)],
                      key=(1.0, 0.98, 0.92), world=0.35, emit=0.4),
        "void": dict(floor=(0.01, 0.01, 0.01), bars=[(0.90, 0.90, 0.95),
                                                     (0.55, 0.30, 0.90),
                                                     (0.20, 0.60, 0.60)],
                     key=(1.0, 1.0, 1.0), world=0.01, emit=4.0),
    }[style]

    # 地面
    bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 0, 0))
    floor = bpy.context.object
    floor.data.materials.append(mat("floor", pal["floor"], rough=0.35))

    # 柱阵：三组不同进深，给视差一个真实的来源（而不是画上去的假视差）
    for k, (x, y, dz, h, w) in enumerate((
            (-3.2, 3.0, 0.0, 2.6, 0.55),
            (0.0, 6.0, 0.0, 3.4, 0.75),
            (3.6, 9.5, 0.0, 4.2, 1.05))):
        bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, dz + h / 2))
        ob = bpy.context.object
        ob.scale = (w, w, h)
        ob.data.materials.append(mat("bar%d" % k, pal["bars"][k], emit=pal["emit"]))

    # 环（转起来才有三维感）
    bpy.ops.mesh.primitive_torus_add(location=(0, 5.0, 2.4),
                                     rotation=(math.pi / 2, 0, 0),
                                     major_radius=1.5, minor_radius=0.06)
    ring = bpy.context.object
    ring.data.materials.append(mat("ring", pal["bars"][1], emit=pal["emit"] * 1.4))
    return ring


def main():
    a = parse()
    wipe()
    ring = build(a.style, a.seed)

    sc = bpy.context.scene
    sc.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in \
        [i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items] \
        else "BLENDER_EEVEE"
    sc.render.film_transparent = False
    sc.render.resolution_x = a.w
    sc.render.resolution_y = a.h
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    sc.render.image_settings.compression = 60

    n = max(1, int(round(a.sec * a.fps)))
    sc.render.fps = a.fps
    sc.frame_start = 1
    sc.frame_end = n
    os.makedirs(a.out, exist_ok=True)
    sc.render.filepath = os.path.join(a.out, "")

    # 世界底色（world 亮度决定「这是夜间还是纸面」）
    w = bpy.data.worlds.new("W")
    sc.world = w
    w.use_nodes = True
    bg = w.node_tree.nodes.get("Background")
    if bg:
        v = {"ink": (0.02, 0.03, 0.06), "paper": (0.90, 0.88, 0.84),
             "void": (0.005, 0.005, 0.008)}[a.style]
        bg.inputs[0].default_value = (v[0], v[1], v[2], 1)
        bg.inputs[1].default_value = {"ink": 0.35, "paper": 0.8, "void": 0.15}[a.style]

    # 相机：从 y=-2 缓慢推到 y=-2+dolly，略微抬高 —— 单一运动，别抢歌词
    cam_data = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Cam", cam_data)
    sc.collection.objects.link(cam)
    sc.camera = cam
    cam.location = (0.0, -2.0, 1.55)
    cam_data.lens = 42
    cam.keyframe_insert("location", frame=1)
    cam.location = (0.35, -2.0 + a.dolly, 2.05)
    cam.rotation_euler = (math.radians(76), 0, math.radians(-2.2))
    cam.keyframe_insert("location", frame=n)
    cam.keyframe_insert("rotation_euler", frame=n)
    cam.rotation_euler = (math.radians(80), 0, 0)
    cam.keyframe_insert("rotation_euler", frame=1)

    # 环自转（匀速、确定性：线性插值，不要缓动）
    for fc in ring.animation_data.action.fcurves if ring.animation_data else []:
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"
    ring.rotation_euler = (math.pi / 2, 0, 0)
    ring.keyframe_insert("rotation_euler", frame=1)
    ring.rotation_euler = (math.pi / 2, 0, math.radians(220))
    ring.keyframe_insert("rotation_euler", frame=n)

    # 主光
    light = bpy.data.lights.new("Key", type="AREA")
    light.energy = {"ink": 900, "paper": 1400, "void": 600}[a.style]
    light.size = 4.0
    lo = bpy.data.objects.new("Key", light)
    lo.location = (2.5, 1.5, 5.0)
    lo.rotation_euler = (math.radians(38), 0, math.radians(28))
    sc.collection.objects.link(lo)

    print("[bg3d] %s %dx%d @ %dfps × %d 帧 → %s" % (a.style, a.w, a.h, a.fps, n, a.out))
    bpy.ops.render.render(animation=True)
    # 自检：帧数必须对得上，少一帧下游就会在尾部静默停在最后一帧
    got = len([f for f in os.listdir(a.out) if f.lower().endswith(".png")])
    print("[bg3d] 完成：%d 帧（声明 %d）" % (got, n))
    if got < n:
        print("[bg3d] 警告：帧数不足，编排稿里的 n 要按实际值写")
    return 0


if __name__ == "__main__":
    sys.exit(main())
