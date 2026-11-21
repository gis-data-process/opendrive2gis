#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""为路网技术文章生成示意图和归档对比图。

在项目根目录调用 python code/diag/roadnet_article_figs.py。
默认写入 docs/figures；--output-dir 可另选目录。只读取源数据、正式结果和 ZIP，
归档中的 GPKG 在临时目录中打开，不执行归档脚本，也不覆盖正式 GIS 产物。
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle
import numpy as np
from shapely.geometry import LineString, Point
from shapely.ops import substring

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from roadnet import roadnet as net  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]
BLUE, TEAL, ORANGE = "#315ca8", "#17877e", "#c27a35"
RED, INK, MUTED, PALE = "#c65146", "#25364b", "#607187", "#d5deea"
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["font.size"] = 11


class Figures:
    """缓存归档数据和出处；生成的对比图按 road_id/车道组定位，避免跨版本误用 ID。"""

    def __init__(self, output, stack):
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)
        self.temp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="roadnet_figures_")))
        self.stack = stack
        self.packages, self.frames, self.sources = {}, {}, {}

    def archived(self, archive, member, layer="Link"):
        key = (archive, member, layer)
        if key not in self.frames:
            if archive not in self.packages:
                self.packages[archive] = self.stack.enter_context(zipfile.ZipFile(ROOT / "archive" / archive))
            raw = self.packages[archive].read(member)
            path = self.temp / (hashlib.sha256(raw).hexdigest() + ".gpkg")
            if not path.exists():
                path.write_bytes(raw)
            self.frames[key] = gpd.read_file(path, layer=layer)
            self.sources[f"{archive}:{member}:{layer}"] = hashlib.sha256(raw).hexdigest()
        return self.frames[key]

    def current(self, map_name, layer="Link"):
        path = ROOT / "out" / f"{map_name}_net.gpkg"
        key = (str(path), layer)
        if key not in self.frames:
            self.frames[key] = gpd.read_file(path, layer=layer)
            self.sources[f"out/{path.name}:{layer}"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return self.frames[key]

    def save(self, figure, name):
        path = self.output / (name + ".png")
        figure.savefig(path, dpi=180, facecolor="white", bbox_inches="tight", pad_inches=.18)
        plt.close(figure)
        print(path.name, flush=True)


def axes_panel(ax, title, xlim, ylim):
    """统一示意图的标题和留白，横纵轴只在需要量距时保留。"""
    ax.set_title(title, loc="left", fontsize=12.5, color=INK, pad=12)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.axis("off")


def arrow(ax, a, b, color=BLUE, width=2.2, size=14, style="-|>"):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle=style, mutation_scale=size,
                                linewidth=width, color=color, zorder=6))


def line(ax, points, color=BLUE, width=2.5, dashed=False):
    points = np.asarray(points)
    ax.plot(points[:, 0], points[:, 1], color=color, linewidth=width,
            linestyle="--" if dashed else "-", solid_capstyle="round")


def node(ax, point, label=None, color=INK, offset=(0, .55)):
    ax.plot(*point, "o", markersize=7, markerfacecolor="white", markeredgecolor=color,
            markeredgewidth=1.7, zorder=8)
    if label:
        ax.text(point[0] + offset[0], point[1] + offset[1], label, fontsize=10, color=color)


def curve(a, b, c, d, n=100):
    t = np.linspace(0, 1, n)[:, None]
    return (1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t ** 2 * c + t ** 3 * d


def fig_intro(figs):
    fig, axs = plt.subplots(2, 1, figsize=(7.4, 6.0))
    axes_panel(axs[0], "提取出来的线：位置能看见，连接关系还没有建好", (-1, 23), (-3, 6.5))
    axes_panel(axs[1], "常规路网：每条线有方向，每个接头有 Node", (-1, 23), (-3, 6.5))
    a, b = axs
    line(a, [(0, 0), (8, 0)], MUTED)
    line(a, [(11, 0), (22, 0)], MUTED)
    line(a, [(9, 1), (10, 1)], ORANGE)
    line(a, curve(np.array([10., 1]), np.array([13., 1]), np.array([13., 3]), np.array([13., 5])), ORANGE)
    a.text(7.5, -1.4, "线头之间有缝", color=RED)
    a.text(0, 2.6, "普通 road", color=MUTED)
    a.text(14, 2.6, "路口连接路", color=ORANGE)
    line(b, [(0, 0), (8, 0)], BLUE)
    line(b, [(8, 0), (11, 0)], ORANGE)
    line(b, [(11, 0), (22, 0)], BLUE)
    line(b, [(0, -1.8), (22, -1.8)], TEAL)
    for x in (3, 14):
        arrow(b, (x, 0), (x + 2, 0), BLUE)
        arrow(b, (x + 2, -1.8), (x, -1.8), TEAL)
    line(b, curve(np.array([8., 0]), np.array([13., 0]), np.array([13., 3]), np.array([13., 5])), ORANGE)
    arrow(b, (12.8, 3.3), (13, 4.5), ORANGE)
    for point, label in (((8, 0), "A"), ((11, 0), "B"), ((13, 5), "C")):
        node(b, point, label)
    b.text(8.2, -1.0, "A → B：直行", color=ORANGE, fontsize=10)
    b.text(14, 3.0, "A → C：转弯", color=ORANGE, fontsize=10)
    fig.tight_layout(h_pad=2.0)
    figs.save(fig, "roadnet-lines-to-network")


def fig_lane_groups(figs):
    fig, axs = plt.subplots(2, 1, figsize=(7.6, 5.7))
    ax = axs[0]
    axes_panel(ax, "先算全部车道的位置，再选择行驶车道的中线", (-8, 9.6), (-1.0, 4.4))
    bands = [(-7, 3.5, "车道 +3", TEAL), (-3.5, 3.5, "车道 +2", TEAL),
             (0, 2, "占位 +1", MUTED), (2, 3.5, "车道 −1", BLUE), (5.5, 3.5, "车道 −2", BLUE)]
    for x, width, label, color in bands:
        ax.add_patch(Rectangle((x, .6), width, 1.5, facecolor=color, alpha=.13, edgecolor=color))
        ax.text(x + width / 2, 1.33, label, ha="center", fontsize=9.5, color=color)
    ax.axvline(0, ymin=.12, ymax=.74, color=MUTED, linewidth=1.1, linestyle="--")
    ax.axvline(2, ymin=.12, ymax=.9, color=ORANGE, linewidth=1.7)
    ax.text(0, 3.0, "参考线", ha="center", fontsize=10, color=MUTED)
    ax.text(2, 3.7, "laneOffset 后的基准", ha="center", fontsize=10, color=ORANGE)
    ax.text(-7.5, 3.7, "左侧 ←", fontsize=9.5, color=TEAL)
    ax.text(7, 3.7, "→ 右侧", fontsize=9.5, color=BLUE)
    for x in (-5.25, -1.75, 3.75, 7.25):
        ax.plot(x, .65, "o", color=TEAL if x < 2 else BLUE, markersize=5)
    ax.text(-6, -.45, "占位车道不出 Link，但它的宽度仍参与定位", color=INK, fontsize=10.5)
    ax = axs[1]
    axes_panel(ax, "同一方向的多条车道，用位于中间的线代表", (0, 30), (-1.5, 5.5))
    for y in (0, 3.5):
        line(ax, [(0, y), (30, y)], PALE, 1.2)
    line(ax, [(0, 1.75), (30, 1.75)], BLUE, 3)
    arrow(ax, (11, 1.75), (16, 1.75), BLUE)
    ax.text(0, 4.4, "两条同宽车道的中心在 0 m 和 3.5 m", color=MUTED)
    ax.text(0, -1.0, "代表线在 1.75 m；lanes = 2", color=BLUE)
    fig.tight_layout(h_pad=1.5)
    figs.save(fig, "roadnet-lane-groups")


def fig_directions(figs):
    fig, axs = plt.subplots(4, 1, figsize=(7.3, 8.7))
    for ax, rule in zip(axs[:2], ("右侧通行 RHT", "左侧通行 LHT")):
        axes_panel(ax, rule, (0, 22), (-2.2, 3.6))
        line(ax, [(2, 0), (20, 0)], MUTED, 1.1, True)
        arrow(ax, (8, 0), (12, 0), MUTED, 1.4)
        ax.text(12.5, .1, "源里程 s 增大", color=MUTED, fontsize=9)
        reverse = rule.startswith("左")
        for y, label, forward in ((1.7, "左侧车道", reverse), (-1.7, "右侧车道", not reverse)):
            line(ax, [(2, y), (20, y)], BLUE if forward else TEAL)
            arrow(ax, (8 if forward else 12, y), (12 if forward else 8, y), BLUE if forward else TEAL)
            ax.text(2, y + .45, label, color=INK, fontsize=9.5)
    for ax, title, forward in zip(axs[2:], ("从连接路 start 进入", "从连接路 end 进入"), (True, False)):
        axes_panel(ax, title, (0, 22), (-1.1, 2.2))
        line(ax, [(3, 0), (19, 0)], ORANGE)
        node(ax, (3, 0), "源 start")
        node(ax, (19, 0), "源 end", offset=(-2.8, .55))
        arrow(ax, (8 if forward else 14, 0), (14 if forward else 8, 0), ORANGE)
        ax.text(3, -1.0, "输出点列按车从哪一端进入来排", color=MUTED, fontsize=10)
    fig.tight_layout(h_pad=1.3)
    figs.save(fig, "roadnet-direction-cases")


def fig_shared_lanes(figs):
    fig, axs = plt.subplots(3, 1, figsize=(7.2, 7.2))
    titles = ("左右各一条普通单向车道", "一条物理车道本身双向", "两条物理车道都允许双向")
    notes = ("两条单向 Link，分别走两侧的车道中线", "两条单向 Link，几何相同，坐标顺序相反",
             "按方向各聚合一条，共两条；每条 lanes = 2")
    for index, (ax, title, note) in enumerate(zip(axs, titles, notes)):
        axes_panel(ax, title, (0, 24), (-2.1, 3.0))
        if index == 0:
            for y, color, forward in ((1.1, TEAL, False), (-.2, BLUE, True)):
                line(ax, [(2, y), (21, y)], color)
                arrow(ax, (8 if forward else 14, y), (14 if forward else 8, y), color)
                node(ax, (2, y), color=color)
                node(ax, (21, y), color=color)
        else:
            line(ax, [(2, .4), (21, .4)], MUTED, 1.4)
            arrow(ax, (8, .65), (14, .65), BLUE)
            arrow(ax, (14, .15), (8, .15), TEAL)
            node(ax, (2, .4))
            node(ax, (21, .4))
            ax.text(2, 1.9, "箭头略错开方便看；实际 Link 共线", fontsize=9, color=MUTED)
        ax.text(2, -1.45, note, fontsize=10.5, color=INK)
    fig.tight_layout(h_pad=1.8)
    figs.save(fig, "roadnet-shared-lanes")


def fig_section_counts(figs):
    fig, axs = plt.subplots(2, 1, figsize=(7.5, 6.2))
    axes_panel(axs[0], "同一条车道跨段换号，不能数成三条", (0, 33), (-1, 3.5))
    for k, label in enumerate(("−3", "−2", "−1")):
        line(axs[0], [(k * 10 + 1, .8), (k * 10 + 10, .8)], BLUE, 3)
        axs[0].text(k * 10 + 5, 1.4, "车道 " + label, ha="center", color=BLUE)
    for x in (10.5, 20.5):
        axs[0].axvline(x, color=PALE, ymin=.1, ymax=.8, linestyle="--")
    axs[0].text(1, -.7, "沿 lane/link 追踪后：一直是 1 条车道", color=INK)
    axes_panel(axs[1], "同一方向先两条、后面三条，lanes 取同时存在的最大数", (0, 33), (-1, 5))
    for y in (1, 2.2):
        line(axs[1], [(1, y), (31, y)], BLUE)
    line(axs[1], [(17, 3.4), (31, 3.4)], ORANGE)
    arrow(axs[1], (11, 1), (15, 1), BLUE)
    axs[1].text(4, 4.3, "这一段 2 条", color=BLUE)
    axs[1].text(20, 4.3, "这一段 3 条", color=ORANGE)
    axs[1].text(1, -.5, "该方向 lanes = 3，不能把两段相加成 5", color=INK)
    fig.tight_layout(h_pad=2)
    figs.save(fig, "roadnet-section-counts")


def fig_node_cases(figs):
    fig, axs = plt.subplots(3, 1, figsize=(7.1, 7.8))
    axes_panel(axs[0], "两方向分开的普通道路：同一 road 端有两个 Node", (0, 24), (-2, 3))
    for y, color, forward in ((1.2, BLUE, True), (-.3, TEAL, False)):
        line(axs[0], [(1, y), (22, y)], color)
        arrow(axs[0], (6 if forward else 14, y), (10 if forward else 10, y), color)
        node(axs[0], (15, y), "各自的接头", color, offset=(.5, .1))
    axes_panel(axs[1], "道路倒着接：应连接行驶出口与入口", (0, 24), (-2, 3))
    line(axs[1], [(1, .4), (12, .4)], BLUE)
    line(axs[1], [(12, .4), (22, .4)], ORANGE)
    arrow(axs[1], (4, .4), (9, .4), BLUE)
    arrow(axs[1], (15, .4), (20, .4), ORANGE)
    node(axs[1], (12, .4), "共用 Node")
    axs[1].text(1, 2.2, "甲：源 start → end", color=BLUE, fontsize=10)
    axs[1].text(13, 2.2, "乙：源 end → start", color=ORANGE, fontsize=10)
    axs[1].text(1, -1.2, "两条源 end 相接，乙的输出点列要反过来", color=INK, fontsize=10)
    axes_panel(axs[2], "中段的平面交叉：没有源连接，不增加 Node", (0, 24), (-2, 3))
    line(axs[2], [(2, .2), (21, .2)], BLUE)
    line(axs[2], [(11, -1.7), (11, 2.4)], ORANGE)
    arrow(axs[2], (4, .2), (8, .2), BLUE)
    arrow(axs[2], (11, -1.2), (11, -.4), ORANGE)
    axs[2].text(13, 1.5, "可以交叉，不能从交点随意拐弯", fontsize=10, color=INK)
    fig.tight_layout(h_pad=2)
    figs.save(fig, "roadnet-node-cases")


def fig_node_position(figs):
    fig, axs = plt.subplots(2, 1, figsize=(7.4, 5.7))
    for ax, title, bad in zip(axs, ("把连接路的偏移也拿来平均", "优先用普通道路的端口定位置"), (True, False)):
        axes_panel(ax, title, (0, 32), (-2, 8))
        y = 1 if bad else 0
        line(ax, [(1, 0), (16, 0), (20, y)], BLUE)
        line(ax, curve(np.array([20., y]), np.array([23., 2]), np.array([24., 5]), np.array([29., 6])), ORANGE)
        node(ax, (20, y), "代表节点", offset=(-6, 2.3))
        ax.plot(20, 2, "x", markersize=8, color=ORANGE)
        ax.plot(20, 0, "x", markersize=8, color=BLUE)
        ax.text(2, -1.2, "主路末端被抬起来" if bad else "主路保持原位置，连接线调整接点", color=INK, fontsize=10.5)
        ax.text(22, 3.8, "连接路原端口", color=ORANGE, fontsize=10)
    fig.tight_layout(h_pad=2)
    figs.save(fig, "roadnet-node-position")


def fig_taper_rules(figs):
    """示意有独立源路径的渐变辅道；只分开计算车道，不代表一定输出额外 Link。"""
    fig, axs = plt.subplots(3, 1, figsize=(7.4, 8.0))
    x = np.linspace(0, 30, 120)
    for ax, title, grows in zip(axs[:2], ("有独立出口的分流：车道从窄到宽", "有独立进口的汇入：车道从宽到窄"), (True, False)):
        axes_panel(ax, title, (-1, 32), (-1.8, 10))
        width = 3.5 * net._smoothstep(x / 30 if grows else 1 - x / 30)
        ax.fill_between(x, 0, 7, color=BLUE, alpha=.08)
        ax.fill_between(x, 7, 7 + width, color=ORANGE, alpha=.15)
        line(ax, np.column_stack([x, np.full_like(x, 3.5)]), BLUE, 2.5)
        weight = net._taper_weight(x, width, 2)
        auxiliary = weight * (7 + width / 2) + (1 - weight) * 5.25
        # 接近消失的一端接主组，足够宽的一端保持独立节点；示意不代替源几何。
        end_weight = net._smoothstep((x if grows else 30 - x) / 8)
        auxiliary = 3.5 + end_weight * (auxiliary - 3.5)
        line(ax, np.column_stack([x, auxiliary]), ORANGE, 2.5)
        node(ax, (0, 3.5))
        node(ax, (30, 3.5))
        index = -1 if grows else 0
        node(ax, (x[index], auxiliary[index]))
        arrow(ax, (11, 3.5), (16, 3.5), BLUE)
        ax.text(1, -1.2, "稳定主组保持原线；全宽端有源连接才保留辅道", color=INK, fontsize=10)
    axes_panel(axs[2], "夹在两条稳定车道中间：保持聚合，不额外拆出重叠线", (-1, 32), (-1.4, 7))
    for y in (0, 4):
        line(axs[2], [(0, y), (30, y)], BLUE, 1.6)
    line(axs[2], [(0, 2), (30, 2)], ORANGE, 1.5, True)
    line(axs[2], [(0, 2), (30, 2)], BLUE, 2.8)
    axs[2].text(1, 5.8, "中间是渐变车道", color=ORANGE, fontsize=10)
    axs[2].text(1, -1.0, "代表线本来已走在中间，再拆会重复", color=INK, fontsize=10)
    fig.tight_layout(h_pad=2)
    figs.save(fig, "roadnet-taper-rules")


def fig_mixed_connections(figs):
    fig, axs = plt.subplots(2, 1, figsize=(7.4, 5.7))
    for ax, title, bad in zip(axs, ("只看连接 road：两组被挤到同一个节点", "沿车道连接追踪：主组和辅道各接各的"), (True, False)):
        axes_panel(ax, title, (0, 31), (-1.5, 7))
        for y, color in ((0, BLUE), (4, ORANGE)):
            line(ax, [(0, y), (8, y)], color)
            line(ax, [(23, y), (30, y)], color)
        line(ax, [(8, 0), (23, 0)], BLUE)
        line(ax, [(8, 4), (8, 0), (23, 4)] if bad else [(8, 4), (23, 4)], RED if bad else ORANGE)
        for p in ((8, 0), (23, 0), (23, 4)):
            node(ax, p)
        if not bad:
            node(ax, (8, 4))
        ax.text(0, 5.8, "主路和辅道都经过同一条 connecting road", color=INK, fontsize=10)
        ax.text(9, -1.1, "一个接头串错两组" if bad else "按首末车道组划分连接线", color=INK, fontsize=10)
        arrow(ax, (13, 0), (18, 0), BLUE)
        if not bad:
            arrow(ax, (13, 4), (18, 4), ORANGE)
    fig.tight_layout(h_pad=2)
    figs.save(fig, "roadnet-mixed-connections")


def fig_endpoint_methods(figs):
    """对比横向接点和节点后退的接线；后退修正直接调用浅贝塞尔，F 只是几何点。"""
    for relative in ("code/roadnet/endpoints.py", "code/roadnet/geometry.py"):
        figs.sources[relative] = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    fig, axs = plt.subplots(4, 1, figsize=(7.8, 10.2))
    axes_panel(axs[0], "横着补一小段：连接路末端出现直角钩", (-1, 31), (-1, 6))
    raw = [(0, 1.75), (30, 1.75), (30, 0)]
    line(axs[0], raw, RED)
    node(axs[0], (30, 0))
    axs[0].text(0, 4.5, "源线与代表节点差半个车道宽", color=INK, fontsize=10)
    axes_panel(axs[1], "有足够空间：在端部平缓横移到节点", (-1, 31), (-1, 6))
    points = np.column_stack([np.linspace(0, 30, 80), np.full(80, 1.75)])
    merged = net._s_merge(points, (30, 0))
    assert merged is not None
    line(axs[1], merged, BLUE)
    node(axs[1], (30, 0))
    axs[1].text(0, 4.5, "两段反向圆弧，端点和方向平顺衔接", color=INK, fontsize=10)
    # 末段局部放大：目标节点略在原末点后方、旁侧，调整量在浅贝塞尔允许范围内。
    source = np.column_stack([np.arange(29, dtype=float), np.ones(29)])
    target, old_end = np.array([26.9, .6]), source[-1]
    joined = net._tail_soft(source, target)
    assert joined is not None and np.diff(joined[:, 0]).min() > 0
    # 当前示意的回退点位于两个原采样点之间，返回点列第一处变化就是插入的 F。
    cut_index = next(i for i, (old, new) in enumerate(zip(source, joined))
                     if not np.allclose(old, new))
    cut = joined[cut_index]
    bounds = ((22.5, 29.7), (-1.2, 3.3))
    axes_panel(axs[2], "节点在原线头后方：保留 P 再接 Node 会折返", *bounds)
    line(axs[2], source, RED, 2.5)
    line(axs[2], [old_end, target], RED, 2.5)
    arrow(axs[2], (25.1, 1), (27.6, 1), RED)
    arrow(axs[2], old_end + .15 * (target - old_end),
          old_end + .85 * (target - old_end), RED)
    axs[2].plot(*old_end, "x", color=RED, markersize=6)
    axs[2].text(27.55, 1.6, "P：原末点", color=RED, fontsize=10)
    node(axs[2], target, "Node", offset=(-.45, -.9))
    axs[2].text(22.75, 2.55, "末段局部放大：先走到 P，再返回 Node", color=INK, fontsize=10)

    axes_panel(axs[3], "在 F 点开始改末段：沿浅贝塞尔向前到达 Node", *bounds)
    line(axs[3], [cut, old_end], MUTED, 1.4, True)
    line(axs[3], joined, BLUE, 2.7)
    arrow(axs[3], joined[-11], joined[-4], BLUE)
    axs[3].plot(*cut, "o", color=BLUE, markersize=4)
    axs[3].text(cut[0] - .75, cut[1] + .6, "F：改线起点", color=BLUE, fontsize=10)
    axs[3].plot(*old_end, "x", color=MUTED, markersize=6)
    axs[3].text(27.55, 1.6, "P：不再保留", color=MUTED, fontsize=10)
    node(axs[3], target, "Node", offset=(-.45, -.9))
    axs[3].text(22.75, 2.55, "F 之前保留；F 之后替换成蓝色过渡线", color=INK, fontsize=10)
    axs[3].text(22.75, -.95, "车辆沿蓝线向前走；F 不新增路网节点", color=INK, fontsize=10)
    fig.tight_layout(h_pad=2.0)
    figs.save(fig, "roadnet-endpoint-methods")


def fig_auxiliary_join(figs):
    fig, axs = plt.subplots(2, 1, figsize=(7.3, 5.6))
    for ax, title in zip(axs, ("收窄端直接追节点，接线可能穿过主组", "沿主组局部方向接入，减少端部额外交叉")):
        axes_panel(ax, title, (0, 30), (-2.0, 5.7))
        line(ax, [(1, 0), (29, 0)], BLUE)
        node(ax, (25, 0))
    line(axs[0], [(1, 3), (15, 3), (22, -.8), (25, 0)], RED)
    line(axs[1], curve(np.array([1., 3]), np.array([17., 3]), np.array([18., 0]), np.array([25., 0])), ORANGE)
    axs[0].text(1, 4.7, "主组：蓝线；辅道：红线", color=INK, fontsize=10)
    axs[1].text(1, -1.5, "只修源中未穿过主组、接点处理却新增穿越的情况", color=INK, fontsize=10)
    fig.tight_layout(h_pad=1.8)
    figs.save(fig, "roadnet-auxiliary-join")


def fig_smoothing(figs):
    fig, axs = plt.subplots(2, 1, figsize=(7.3, 5.9))
    original = np.array([(0, 0), (6, 0), (6.2, .1), (6.35, .8), (6.45, 1.1), (6.6, .9), (12, 2), (20, 2)])
    geometry = net.smooth_line(LineString(original), 1.5)
    for ax, title, points in ((axs[0], "点距不均匀：密集的小点把接缝挤在一起", original),
                              (axs[1], "先均匀采样，再平滑；首末两个节点固定", np.asarray(geometry.coords))):
        axes_panel(ax, title, (-1, 21), (-1.5, 4.4))
        line(ax, points, RED if ax is axs[0] else BLUE)
        ax.scatter(points[:, 0], points[:, 1], s=13, color=RED if ax is axs[0] else BLUE)
        node(ax, (0, 0), "A")
        node(ax, (20, 2), "B", offset=(-1, .6))
        ax.text(0, -1.2, "平滑强度按米控制，不能按顶点个数随意平均", color=INK, fontsize=10)
    fig.tight_layout(h_pad=2)
    figs.save(fig, "roadnet-smoothing")


def fig_short_links(figs):
    fig, axs = plt.subplots(2, 1, figsize=(7.4, 5.7))
    axes_panel(axs[0], "很短的边，也可能是两段道路之间唯一的连接", (0, 26), (-2, 3))
    line(axs[0], [(1, 0), (12, 0)], BLUE)
    line(axs[0], [(12, 0), (13, 0)], ORANGE, 5)
    line(axs[0], [(13, 0), (25, 0)], BLUE)
    node(axs[0], (12, 0))
    node(axs[0], (13, 0))
    axs[0].text(6, 2.0, "短连接边不能只因为短就删掉", color=INK)
    axs[0].text(1, -1.6, "示意图放大了短边；删除后左右两侧断开", color=MUTED, fontsize=10)
    axes_panel(axs[1], "端点迁移量比源线还长：两头各补线容易折返", (0, 26), (-2, 3))
    line(axs[1], [(12, 1), (14, 1)], RED, 3)
    line(axs[1], [(4, 0), (22, 0)], BLUE, 2.6)
    node(axs[1], (4, 0), "Node A")
    node(axs[1], (22, 0), "Node B", offset=(-3, .5))
    axs[1].text(10, 2.0, "源短线", color=RED)
    axs[1].text(1, -1.4, "这种退化情况用节点间直线兜底，并保留迁移量供复核", color=INK, fontsize=10)
    fig.tight_layout(h_pad=2)
    figs.save(fig, "roadnet-short-links")


def fig_preserved_shapes(figs):
    fig, axs = plt.subplots(3, 1, figsize=(7.3, 7.0))
    x = np.linspace(0, 30, 160)
    axes_panel(axs[0], "源线本来就是 S 弯：保留两个转弯方向", (-1, 31), (-3.5, 4.0))
    line(axs[0], np.column_stack([x, 2.2 * np.sin(2 * np.pi * x / 30)]), BLUE)
    line(axs[0], [(0, 0), (30, 0)], RED, 1.4, True)
    axs[0].text(1, 3.1, "不能因为首尾顺直就把中段拉平", color=INK, fontsize=10)
    axes_panel(axs[1], "真实掉头：弯得紧不等于算法错误", (-1, 31), (-1.5, 7.0))
    t = np.linspace(0, np.pi, 100)
    line(axs[1], np.column_stack([15 + 4 * np.sin(t), 2 - 2 * np.cos(t)]), ORANGE)
    line(axs[1], [(6, 0), (15, 0)], ORANGE)
    line(axs[1], [(15, 4), (6, 4)], ORANGE)
    arrow(axs[1], (8, 0), (12, 0), ORANGE)
    arrow(axs[1], (12, 4), (8, 4), ORANGE)
    axs[1].text(1, 6, "应回看源轨迹，不能拿小半径统计直接判错", color=INK, fontsize=10)
    axes_panel(axs[2], "转弯本来跨过直行路径：保留源交叉", (-1, 31), (-2, 5.5))
    line(axs[2], [(0, 0), (30, 0)], BLUE)
    line(axs[2], curve(np.array([5., -1.5]), np.array([15., -1.5]), np.array([17., 3]), np.array([28., 4])), ORANGE)
    axs[2].text(1, 4.7, "中段交叉处不自动增加 Node", color=INK, fontsize=10)
    fig.tight_layout(h_pad=1.8)
    figs.save(fig, "roadnet-preserve-shapes")


def fig_curve_biarc(figs):
    """用同半径反向圆弧展示横移，并单独标出曲率切换；图示不作为车辆设计轨迹。"""
    fig, axs = plt.subplots(2, 1, figsize=(7.8, 6.0),
                            gridspec_kw={"height_ratios": [1.5, 1]})
    radius, angle = 8.0, np.deg2rad(50.0)
    theta = np.linspace(0.0, angle, 100)
    first = np.column_stack([radius * np.sin(theta), radius * (1.0 - np.cos(theta))])
    middle = first[-1]
    second = middle + np.column_stack([
        radius * (np.sin(angle) - np.sin(angle - theta)),
        radius * (np.cos(angle - theta) - np.cos(angle))])
    start, finish = first[0], second[-1]
    ax = axs[0]
    axes_panel(ax, "双圆弧：先向一侧转，再把方向转回来", (-2, 15.5), (-2.2, 8.4))
    line(ax, [(-1.6, 0), start], MUTED, 1.5)
    line(ax, [finish, finish + (2, 0)], MUTED, 1.5)
    line(ax, first, BLUE, 3)
    line(ax, second, TEAL, 3)
    arrow(ax, first[30], first[46], BLUE)
    arrow(ax, second[55], second[72], TEAL)
    arrow(ax, (-1.6, 0), (-.1, 0), MUTED, 1.4)
    arrow(ax, finish + (.15, 0), finish + (1.8, 0), MUTED, 1.4)
    node(ax, start, "A", offset=(-.6, -.95))
    node(ax, middle, "M", offset=(-.5, .6))
    node(ax, finish, "B", offset=(-.3, -.95))
    ax.text(.6, 1.75, "第 1 段：向左转", color=BLUE, fontsize=10.5)
    ax.text(8.0, 3.35, "第 2 段：向右转", color=TEAL, fontsize=10.5)
    ax.text(8.0, 7.1, "到达方向与出发方向平行", color=INK, fontsize=10)
    arrow(ax, (14.7, 0), (14.7, finish[1]), ORANGE, 1.4, 12, "<->")
    ax.text(13.55, 2.0, "横向\n位移", color=ORANGE, fontsize=10, linespacing=1.5)
    ax.set_aspect("equal")

    ax = axs[1]
    half_length = radius * angle
    ax.hlines(1 / radius, 0, half_length, color=BLUE, linewidth=2.8)
    ax.hlines(-1 / radius, half_length, 2 * half_length, color=TEAL, linewidth=2.8)
    ax.axhline(0, color=PALE, linewidth=1)
    ax.axvline(half_length, color=MUTED, linestyle="--", linewidth=1)
    ax.set_xlim(-.4, 2 * half_length + .4)
    ax.set_ylim(-.21, .23)
    ax.set_yticks([-1 / radius, 0, 1 / radius], ["−1/R", "0", "+1/R"])
    ax.set_xticks([0, half_length, 2 * half_length], ["A", "M：拼接处", "B"])
    ax.set_title("方向连续，曲率在 M 处切换正负", loc="left", color=INK, fontsize=12)
    ax.set_xlabel("沿双圆弧的长度", color=MUTED, fontsize=10)
    ax.set_ylabel("曲率", color=MUTED, fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(h_pad=1.8)
    figs.save(fig, "roadnet-curve-biarc")


def fig_curve_quadratic(figs):
    """二次贝塞尔只替换折点两侧的小段，控制点取原折点；保留段与替换段分别上色。"""
    fig, ax = plt.subplots(figsize=(7.5, 5.4))
    axes_panel(ax, "二次贝塞尔：两个端点、一个控制点", (-1.5, 13), (-2.2, 9.8))
    start, control, finish = np.array([4., 0]), np.array([8., 0]), np.array([8., 4.])
    u = np.linspace(0.0, 1.0, 100)[:, None]
    points = (1 - u) ** 2 * start + 2 * (1 - u) * u * control + u ** 2 * finish
    line(ax, [(0, 0), start], BLUE, 2.5)
    line(ax, [finish, (8, 8.3)], BLUE, 2.5)
    line(ax, [start, control, finish], MUTED, 1.6, True)
    line(ax, points, ORANGE, 3)
    arrow(ax, (1.0, 0), (2.8, 0), BLUE)
    arrow(ax, (8, 5.4), (8, 7.3), BLUE)
    arrow(ax, points[42], points[58], ORANGE)
    node(ax, start, "P0", offset=(-.7, -.8))
    node(ax, finish, "P2", offset=(.55, .05))
    ax.plot(*control, "s", color=MUTED, markersize=6)
    ax.text(8.5, -.2, "P1：原折点\n控制点", color=MUTED, fontsize=10, linespacing=1.6)
    ax.text(.3, 1.2, "保留原线", color=BLUE, fontsize=10.5)
    ax.text(6.3, 5.8, "保留原线", color=BLUE, fontsize=10.5)
    ax.text(4.0, 2.8, "仅替换接缝", color=ORANGE, fontsize=10.5)
    ax.text(.1, 9.0, "P0→P1 给出出发方向；P1→P2 给出到达方向", color=INK, fontsize=10)
    ax.text(.1, -1.7, "虚线是原折角；曲线不会经过中间控制点 P1。", color=MUTED, fontsize=9.5)
    ax.set_aspect("equal")
    fig.tight_layout()
    figs.save(fig, "roadnet-curve-quadratic")


def fig_curve_displacement(figs):
    """直接调用平滑权重生成位移分摊示意；不加高斯平滑，避免混淆两种操作。"""
    fig, axs = plt.subplots(2, 1, figsize=(7.6, 6.4),
                            gridspec_kw={"height_ratios": [1, 1.3]})
    ax = axs[0]
    u = np.linspace(0, 1, 100)
    weight = net._smoothstep(u)
    ax.plot(u, weight, color=BLUE, linewidth=2.6)
    ax.plot([0, .13], [0, 0], color=MUTED, linewidth=1.2)
    ax.plot([.87, 1], [1, 1], color=MUTED, linewidth=1.2)
    ax.scatter([0, 1], [0, 1], color=BLUE, s=28, zorder=6)
    ax.set_xlim(-.02, 1.02)
    ax.set_ylim(-.06, 1.16)
    ax.set_title("三阶平滑权重：两端变化平缓，中间逐渐过渡", loc="left", color=INK, fontsize=12)
    ax.set_xlabel("u：沿源中线的长度比例", color=MUTED, fontsize=10)
    ax.set_ylabel("w(u)", color=MUTED, fontsize=10)
    ax.text(.08, .88, "w(u) = 3u² − 2u³", color=BLUE, fontsize=12)
    ax.grid(color="#edf1f6", linewidth=.7)
    ax.spines[["top", "right"]].set_visible(False)

    ax = axs[1]
    theta = np.linspace(0, np.pi / 2, 100)
    source = np.column_stack([9 * np.sin(theta), 9 * (1 - np.cos(theta))])
    length_fraction = net._arc_len(source) / net._arc_len(source)[-1]
    blend = net._smoothstep(length_fraction)[:, None]
    delta_start, delta_end = np.array([0., -2]), np.array([2., 0])
    moved = source + (1 - blend) * delta_start + blend * delta_end
    axes_panel(ax, "沿源弧分摊：固定两端，中间各点逐渐承担位移", (-1, 16.5), (-3.5, 11.5))
    line(ax, source, MUTED, 1.6, True)
    line(ax, moved, BLUE, 2.8)
    for index in (0, 22, 48, 72, 99):
        arrow(ax, source[index], moved[index], ORANGE, 1.1, 10)
    arrow(ax, moved[35], moved[48], BLUE, 1.8, 12)
    node(ax, moved[0], "A", offset=(-.5, -.95))
    node(ax, moved[-1], "B", offset=(.6, -.1))
    ax.text(.8, 9.0, "灰虚线：源弧", color=MUTED, fontsize=10.5)
    ax.text(.8, 7.5, "蓝线：分摊后的弧", color=BLUE, fontsize=10.5)
    ax.text(.8, 6.0, "橙箭头：各点的位移", color=ORANGE, fontsize=10.5)
    ax.set_aspect("equal")
    fig.tight_layout(h_pad=1.6)
    figs.save(fig, "roadnet-curve-displacement")


def fig_curve_gaussian(figs):
    """展示当前离散高斯核和两种尺度的平滑，源点列、首末点均一致。"""
    fig, axs = plt.subplots(2, 1, figsize=(7.7, 6.1))
    ax = axs[0]
    sigma, spacing = 1.5, .5
    distance = np.arange(-3 * sigma, 3 * sigma + spacing, spacing)
    kernel = np.exp(-.5 * (distance / sigma) ** 2)
    kernel /= kernel.sum()
    ax.bar(distance, kernel, width=.37, color=TEAL, alpha=.7)
    ax.plot(distance, kernel, color=TEAL, linewidth=1.6)
    ax.set_title("高斯权重：离当前位置越近，影响越大", loc="left", color=INK, fontsize=12)
    ax.set_xlabel("沿线距离 d（m）", color=MUTED, fontsize=10)
    ax.set_ylabel("归一化权重", color=MUTED, fontsize=10)
    ax.text(-4.1, .105, "σ = 1.5 m\n各权重之和为 1", color=TEAL, fontsize=10, linespacing=1.5)
    ax.grid(axis="y", color="#edf1f6", linewidth=.7)
    ax.spines[["top", "right"]].set_visible(False)

    ax = axs[1]
    x = np.linspace(0, 24, 180)
    y = .075 * x + .75 * np.sin(2.1 * x) * np.sin(np.pi * x / 24) ** 2
    source = LineString(np.column_stack([x, y]))
    for geometry, color, label, width in (
            (source, MUTED, "源示意线", 1.2),
            (net.smooth_line(source, .6), BLUE, "σ = 0.6 m", 2),
            (net.smooth_line(source, 1.5), TEAL, "σ = 1.5 m", 2.5)):
        points = np.asarray(geometry.coords)
        ax.plot(points[:, 0], points[:, 1], color=color, linewidth=width, label=label,
                linestyle="--" if geometry is source else "-")
    ax.scatter([0, 24], [y[0], y[-1]], color=INK, s=32, zorder=8)
    ax.set_title("同一条线：平滑尺度越大，局部起伏减弱越多", loc="left", color=INK, fontsize=12)
    ax.set_xlabel("X（m）", color=MUTED, fontsize=10)
    ax.set_ylabel("Y（m）", color=MUTED, fontsize=10)
    ax.set_xlim(-.5, 24.5)
    ax.set_ylim(-.7, 3.1)
    ax.legend(loc="upper left", frameon=False, fontsize=9, ncol=3)
    ax.text(11.8, -.42, "两端固定；纵轴放大以看清起伏", color=MUTED, fontsize=9)
    ax.grid(color="#edf1f6", linewidth=.7)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(h_pad=1.9)
    figs.save(fig, "roadnet-curve-gaussian")


def fig_curve_fit_selection(figs):
    """用示意控制点解释三次贝塞尔及候选顺序；代码出处记录当前函数的文件哈希。"""
    for relative in ("code/roadnet/refinement.py", "code/roadnet/endpoints.py",
                     "code/roadnet/geometry.py"):
        figs.sources[relative] = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    fig, axs = plt.subplots(2, 1, figsize=(7.8, 9.4),
                            gridspec_kw={"height_ratios": [1.1, 1]})
    ax = axs[0]
    axes_panel(ax, "三次贝塞尔：节点固定，控制点约束首末方向", (-2, 13), (-1.8, 12))
    start, first = np.array([1., 1.]), np.array([5.7, 1.])
    second, finish = np.array([9., 4.3]), np.array([9., 9.])
    intersection = np.array([9., 1.])
    line(ax, [start, intersection, finish], PALE, 1.3, True)
    line(ax, [start, first], BLUE, 1.8, True)
    line(ax, [second, finish], TEAL, 1.8, True)
    line(ax, [(-1.5, 1), start], BLUE, 2.5)
    line(ax, [finish, (9, 11.4)], TEAL, 2.5)
    arrow(ax, (-1.1, 1), (.7, 1), BLUE)
    arrow(ax, (9, 9.35), (9, 11.1), TEAL)
    points = curve(start, first, second, finish)
    line(ax, points, ORANGE, 3)
    arrow(ax, points[42], points[56], ORANGE)
    node(ax, start, "P0 = A", offset=(-1.9, -.9))
    node(ax, finish, "P3 = B", offset=(-2.8, .7))
    for point, label, color, offset in ((first, "P1", BLUE, (-.3, -.9)),
                                       (second, "P2", TEAL, (.5, -.1))):
        ax.plot(*point, "s", markersize=6, color=color, zorder=8)
        ax.text(point[0] + offset[0], point[1] + offset[1], label, color=color, fontsize=11)
    ax.plot(*intersection, "x", color=MUTED, markersize=6)
    ax.text(9.5, .7, "方向线交点", color=MUTED, fontsize=9.5)
    ax.text(2.9, -.65, "入口控制臂 h_in", color=BLUE, fontsize=10)
    ax.text(9.5, 6.5, "出口控制臂 h_out", color=TEAL, fontsize=10)
    ax.text(-1.4, 3.0, "沿入口方向出发", color=BLUE, fontsize=10)
    ax.text(4.7, 10.9, "沿出口方向到达", color=TEAL, fontsize=10)
    ax.text(-1.4, -1.6, "线形为示意；虚线为控制臂和方向线，方点为控制点。", color=MUTED, fontsize=9.5)
    ax.set_aspect("equal")

    ax = axs[1]
    axes_panel(ax, "整段转弯：按顺序检查，采用第一个通过的候选", (0, 12), (0, 13.7))
    choices = (("1. 沿源弧分摊位移 + 按配置平滑", "保留源弧，检查节点偏移后的形状", BLUE),
               ("2. 最小二乘三次贝塞尔", "首末方向固定，控制臂限制在交点以内", ORANGE),
               ("3. 36 组控制臂候选", "按离源弧的偏差从小到大检查", ORANGE),
               ("都不通过：保持本轮整理前的几何", "不接受新增的自交、回摆或穿越", MUTED))
    positions = (10.8, 7.8, 4.8, 1.8)
    for index, ((title, detail, color), y) in enumerate(zip(choices, positions)):
        ax.add_patch(FancyBboxPatch((.4, y), 8.6, 1.85, boxstyle="round,pad=.12",
                                   facecolor="#f5f7fb", edgecolor=color, linewidth=1.1))
        ax.text(.8, y + 1.15, title, color=color, fontsize=10.5)
        ax.text(.8, y + .45, detail, color=INK, fontsize=9.5)
        if index < 3:
            arrow(ax, (4.7, y - .2), (4.7, positions[index + 1] + 2.05), MUTED, 1.2, 12)
            ax.text(5.15, y - .72, "未通过", color=MUTED, fontsize=9)
            arrow(ax, (9.25, y + .92), (10.25, y + .92), TEAL, 1.3, 12)
            ax.text(10.45, y + .65, "通过\n采用", color=TEAL, fontsize=9.5, linespacing=1.4)
    ax.text(.45, .5, "源小弯只尝试第 1 项；真实 S 弯和掉头不走普通整线拟合。", color=MUTED, fontsize=9)
    fig.tight_layout(h_pad=2.0)
    figs.save(fig, "roadnet-curve-fit-selection")


def fig_crossing_limits(figs):
    fig, axs = plt.subplots(3, 1, figsize=(7.3, 7.0))
    for ax, title in zip(axs, ("源中没有交叉：不能新增", "源中已有一次交叉：核对两条源线后可恢复", "源中只有一次：不能把它变成两次")):
        axes_panel(ax, title, (0, 22), (-2.8, 3.0))
        line(ax, [(1, 0), (21, 0)], BLUE)
    line(axs[0], [(11, -.9), (11, .9)], RED)
    axs[0].text(1, -2.2, "缺少任一条源轨迹，也不能靠猜测放行", color=INK, fontsize=10)
    line(axs[1], [(11, -1.1), (11, 1.8)], TEAL)
    axs[1].text(1, -2.2, "恢复交叉不代表可以在交点换路", color=INK, fontsize=10)
    line(axs[2], [(7, -1.1), (7, 1.4), (15, 1.4), (15, -1.1)], RED)
    axs[2].text(1, -2.2, "要比较穿越次数，不能只比较穿过了哪些 Link", color=INK, fontsize=10)
    fig.tight_layout(h_pad=1.8)
    figs.save(fig, "roadnet-crossing-limits")


def fig_connectivity(figs):
    """用示意边区分弱连通与有向可达，避免把道路末端当作零度废点。"""
    fig, axs = plt.subplots(2, 1, figsize=(7.4, 5.9))
    axes_panel(axs[0], "连成一片：仍要按箭头判断能否到达", (0, 30), (-2.4, 4.0))
    for a, b in (((4, 0), (14, 0)), ((14, 0), (24, 0))):
        line(axs[0], [a, b], BLUE)
        arrow(axs[0], (a[0] + 3, 0), (b[0] - 3, 0), BLUE)
    for point, label in (((4, 0), "A"), ((14, 0), "B"), ((24, 0), "C")):
        node(axs[0], point, label, offset=(-.3, .7))
    axs[0].text(2, 2.7, "忽略方向：1 个分量；按方向：A 能到 C，C 回不到 A", color=INK, fontsize=10)
    axs[0].text(2, -1.8, "分量数量不能代替方向可达检查", color=MUTED, fontsize=10)
    axes_panel(axs[1], "没有边使用的节点，与正常道路末端分开处理", (0, 30), (-2.4, 4.0))
    line(axs[1], [(15, 0), (27, 0)], BLUE)
    arrow(axs[1], (19, 0), (23, 0), BLUE)
    node(axs[1], (5, 0), "度 = 0", color=RED, offset=(-1.6, .7))
    node(axs[1], (15, 0), "度 = 1", offset=(-1.6, .7))
    node(axs[1], (27, 0), "度 = 1", offset=(-1.6, .7))
    axs[1].text(2, 2.7, "degree = 入度 + 出度，统计实际写出的 Link", color=INK, fontsize=10)
    axs[1].text(2, -1.8, "左侧零度点可剔除；右侧仍有道路相接，保留末端节点", color=MUTED, fontsize=10)
    fig.tight_layout(h_pad=2)
    figs.save(fig, "roadnet-connectivity")


def select_row(frame, rid, lanes=None):
    rows = frame[frame.road_id.astype(str) == str(rid)]
    if lanes is not None:
        rows = rows[rows.lane_ids == lanes]
    if len(rows) != 1:
        raise ValueError(f"对比对象不唯一: road={rid}, lanes={lanes}, rows={len(rows)}")
    return rows.iloc[0]


def map_pair(figs, before, after, rid, name, titles, context=(), lanes=None,
             span=None, orient_horizontal=False, include_context=False, note=None):
    """相同窗口重画历史/当前几何；局部端部图按弧长截取，不缩放横向距离。"""
    a, b = select_row(before, rid, lanes), select_row(after, rid, lanes)
    ga, gb = a.geometry, b.geometry
    transform = lambda p: np.asarray(p)[:, :2]
    if span is not None:
        # 比较端部几何差，而不是取整条线的曲率最小点。
        scores = []
        for start in (True, False):
            part = substring(ga, 0 if start else max(0, ga.length - span), min(span, ga.length) if start else ga.length)
            next_part = substring(gb, 0 if start else max(0, gb.length - span), min(span, gb.length) if start else gb.length)
            scores.append(part.hausdorff_distance(next_part))
        start = scores[0] >= scores[1]
        ga = substring(ga, 0 if start else max(0, ga.length - span), min(span, ga.length) if start else ga.length)
        gb = substring(gb, 0 if start else max(0, gb.length - span), min(span, gb.length) if start else gb.length)
    if orient_horizontal:
        origin = np.asarray(gb.coords[0])[:2]
        v = np.asarray(gb.coords[-1])[:2] - origin
        v /= np.linalg.norm(v)
        normal = np.array([-v[1], v[0]])
        transform = lambda p: np.column_stack([(np.asarray(p)[:, :2] - origin) @ v,
                                               (np.asarray(p)[:, :2] - origin) @ normal])
    all_points = np.vstack([transform(ga.coords), transform(gb.coords)])
    if include_context:
        all_points = np.vstack([all_points] + [transform(row.geometry.coords)
                               for frame in (before, after) for crid, _ in context
                               for _, row in frame[frame.road_id.astype(str) == str(crid)].iterrows()])
    xmin, ymin = all_points.min(axis=0)
    xmax, ymax = all_points.max(axis=0)
    pad = max(1.4, .13 * max(xmax - xmin, ymax - ymin))
    bounds = (xmin - pad, xmax + pad, ymin - pad, ymax + pad)
    fig, axs = plt.subplots(2, 1, figsize=(7.2, 8.4 if ymax - ymin > xmax - xmin else 5.8))
    for ax, frame, main, title in zip(axs, (before, after), (ga, gb), titles):
        for crid, color in context:
            for _, row in frame[frame.road_id.astype(str) == str(crid)].iterrows():
                p = transform(row.geometry.coords)
                ax.plot(p[:, 0], p[:, 1], color=color, linewidth=1.6, alpha=.85)
        points = transform(main.coords)
        ax.plot(points[:, 0], points[:, 1], color=RED if frame is before else BLUE, linewidth=2.7)
        endpoints = (points[0 if start else -1],) if span is not None else (points[0], points[-1])
        for point in endpoints:
            node(ax, point)
        ax.set_title(title, loc="left", color=INK, fontsize=12.5, pad=12)
        ax.set_xlim(bounds[0], bounds[1])
        ax.set_ylim(bounds[2], bounds[3])
        ax.set_aspect("equal")
        ax.grid(color="#edf1f6", linewidth=.6)
        ax.tick_params(labelsize=9, colors=MUTED)
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_xlabel("沿道路方向距离（m）" if orient_horizontal else "X（m）", fontsize=10, color=MUTED)
        ax.set_ylabel("横向距离（m）" if orient_horizontal else "Y（m）", fontsize=10, color=MUTED)
    fig.tight_layout(h_pad=1.8)
    if note:
        fig.text(.06, -.025, note, color=MUTED, fontsize=9.5)
    figs.save(fig, name)


def fig_history_comparisons(figs):
    first = "2026-11-14-before-optimization.zip"
    old = figs.archived(first, "out/_preblend/futiancenter_net.gpkg")
    map_pair(figs, old, figs.current("futiancenter"), "4452", "roadnet-large-offset",
             ("归档：右转起步先横甩，再急弯回来", "当前：沿行驶方向平顺接入转弯"),
             context=(("4461", TEAL),), lanes="-1", note="福田 Road 4452；端部横移约 6.19 m。绿线为邻接道路。")
    old = figs.archived(first, "out/_presoft/futiancenter_net.gpkg")
    map_pair(figs, old, figs.current("futiancenter"), "1142", "roadnet-small-offset",
             ("归档：末点硬挪约 0.98 m，留下小折角", "当前：把位移摊在最后几米"),
             lanes="4,5,6,7", span=8, orient_horizontal=True,
             note="福田 Road 1142；同一源道路、同一车道组，端部按相同比例显示。")
    no_coincide = figs.archived(first, "out/_noc/Town03_net.gpkg")
    historical_after = figs.archived(first, "out/Town03_net.gpkg")
    map_pair(figs, no_coincide, historical_after, "1146", "roadnet-end-coincide",
             ("归档：合流线斜穿直行线后再接节点", "归档：交点之后沿邻线合流"),
             context=(("1165", TEAL),), lanes="1",
             note="Town03 Road 1146 / 1165；展示历史贴线步骤，最终结果还要核对源形状。")
    steep_before = figs.archived(first, "out/_noc/Town04_net.gpkg")
    steep_after = figs.archived(first, "out/Town04_net.gpkg")
    map_pair(figs, steep_before, steep_after, "1162", "roadnet-steep-coincide",
             ("归档：合流接缝的夹角很大", "归档：圆角后，再平滑局部接缝"),
             note="Town04 Road 1162；历史案例用于说明接缝处理，节点和转向关系不变。")
    no_mid = figs.archived(first, "out/_nomid/Town03_net.gpkg")
    map_pair(figs, no_mid, figs.current("Town03"), "1608", "roadnet-roundabout",
             ("归档：中段穿进环道，又斜着切出来", "当前：沿环道走一段，再顺着方向离开"),
             context=(("1624", TEAL),), note="Town03 Road 1608 / 1624；浅进、陡出、两次内部穿越的历史案例。")


def fig_auxiliary_nodes(figs):
    """对比辅道独立路径与同起终节点的重复边，后者仅显示稳定主组。"""
    fig, axs = plt.subplots(3, 1, figsize=(7.3, 7.3))
    titles = ("收窄端：辅道接入主组节点", "全宽端有源车道连接：辅道保留自己的节点",
              "两端都回到主组：省略辅道，只保留稳定主组")
    for ax, title in zip(axs, titles):
        axes_panel(ax, title, (0, 30), (-1.6, 5.5))
        line(ax, [(1, 0), (29, 0)], BLUE)
        node(ax, (22, 0))
    line(axs[0], curve(np.array([1., 3]), np.array([15., 3]), np.array([16., 0]), np.array([22., 0])), ORANGE)
    axs[0].text(1, -1.25, "几何先靠向相邻稳定车道，端部再与主组接头", color=INK, fontsize=10)
    line(axs[1], [(1, 3), (22, 3), (29, 4.5)], ORANGE)
    node(axs[1], (22, 3))
    arrow(axs[1], (23.5, 3.4), (27, 4.1), ORANGE)
    axs[1].text(1, -1.25, "主路和辅道继续各接各的路径", color=INK, fontsize=10)
    node(axs[2], (1, 0))
    axs[2].text(1, 3.5, "没有独立接点，不增加同方向 A → B 的重复边", color=INK, fontsize=10)
    axs[2].text(1, -1.25, "主组仍按稳定车道计算，渐变车道不加回来拉偏它", color=INK, fontsize=10)
    fig.tight_layout(h_pad=1.8)
    figs.save(fig, "roadnet-auxiliary-node-rules")


def fig_height_cases(figs):
    fig, axs = plt.subplots(2, 1, figsize=(7.4, 5.6))
    axes_panel(axs[0], "俯视图：两个端点在同一个 XY 位置", (-1, 28), (-1.3, 5))
    line(axs[0], [(1, 1), (13, 1)], BLUE)
    line(axs[0], [(13, 1), (25, 1)], ORANGE)
    node(axs[0], (13, 1))
    axs[0].text(1, 3.7, "只看平面距离，会误以为它们能接起来", color=INK, fontsize=10)
    axes_panel(axs[1], "侧视图：桥上与桥下的高程不同", (-1, 28), (-1.3, 7))
    line(axs[1], [(1, 1), (13, 1)], BLUE)
    line(axs[1], [(13, 5), (25, 5)], ORANGE)
    node(axs[1], (13, 1))
    node(axs[1], (13, 5))
    axs[1].text(1, -1.0, "坐标吸附同时看高程；源显式连接仍需另行核对", color=INK, fontsize=10)
    fig.tight_layout(h_pad=1.7)
    figs.save(fig, "roadnet-height-cases")


def fig_lane_section_boundary(figs):
    fig, axs = plt.subplots(2, 1, figsize=(7.4, 5.9))
    axes_panel(axs[0], "分界处参考基准和车道编号同时变化", (0, 32), (-2.5, 5))
    line(axs[0], [(1, -2), (16, -2)], TEAL)
    line(axs[0], [(16, 0), (31, 0)], TEAL)
    for x, label in ((1, "前段：有内侧占位车道"), (18, "后段：占位车道删去")):
        axs[0].text(x, 4.1, label, fontsize=9.5, color=INK)
    axs[0].axvline(16, color=PALE, ymin=.08, ymax=.86, linestyle="--")
    line(axs[0], [(1, 1.75), (15.8, 1.75), (16, 3.75)], RED)
    axs[0].text(1, -1.8, "把后段基准配上前段宽度，末点会凭空跳出去", color=RED, fontsize=10)
    axes_panel(axs[1], "分别按各段记录求值，真实车道中线保持连续", (0, 32), (-2.5, 5))
    line(axs[1], [(1, 1.75), (31, 1.75)], BLUE)
    axs[1].axvline(16, color=PALE, ymin=.08, ymax=.86, linestyle="--")
    axs[1].text(1, 3.2, "前段末点用前段记录，后段首点用后段记录", color=INK, fontsize=10)
    axs[1].text(1, -1.8, "真实不连续的源记录仍要单独处理，不能一律抹平", color=INK, fontsize=10)
    fig.tight_layout(h_pad=1.8)
    figs.save(fig, "roadnet-section-boundary")


def fig_taper_map(figs):
    """对比同节点重复辅道删除前后；两个窗口共享范围，主组坐标保持原样。"""
    old = figs.archived("2026-11-18-before-redundant-auxiliary.zip", "out/Pingshan_20250606_OD_net.gpkg")
    current = figs.current("Pingshan_20250606_OD")
    lane_path = ROOT / "out/Pingshan_20250606_OD.gpkg"
    lanes = gpd.read_file(lane_path, layer="Lane")
    figs.sources[f"out/{lane_path.name}:Lane"] = hashlib.sha256(lane_path.read_bytes()).hexdigest()
    rid = "208110035"
    chosen = lanes[lanes.road_id.astype(str) == rid]
    row = current[(current.road_id.astype(str) == rid) & (current.link_type == "road")].iloc[0]
    points = np.asarray(row.geometry.coords)[:, :2]
    origin = points[0]
    v = points[-1] - origin
    v /= np.linalg.norm(v)
    normal = np.array([-v[1], v[0]])
    def xy(p):
        p = np.asarray(p)[:, :2] - origin
        return np.column_stack([p @ v, p @ normal])
    plotted = []
    for _, lane in chosen.iterrows():
        if lane.geometry is not None:
            plotted.extend(xy(polygon.exterior.coords)
                           for polygon in getattr(lane.geometry, "geoms", [lane.geometry]))
    for frame in (old, current):
        plotted.extend(xy(record.geometry.coords)
                       for _, record in frame[frame.road_id.astype(str) == rid].iterrows())
    all_points = np.vstack(plotted)
    lower, upper = all_points.min(axis=0), all_points.max(axis=0)
    padding = np.array([2.0, 1.0])
    fig, axs = plt.subplots(2, 1, figsize=(7.3, 5.8))
    for ax, frame, title in zip(axs, (old, current), ("修改前：辅道绕出去，再回到主组同一对节点", "当前：省略重复辅道，主组路线保持原样")):
        for _, lane in chosen.iterrows():
            if lane.geometry is None:
                continue
            for polygon in getattr(lane.geometry, "geoms", [lane.geometry]):
                p = xy(polygon.exterior.coords)
                ax.fill(p[:, 0], p[:, 1], color=PALE, alpha=.55, edgecolor="white")
        for _, link_row in frame[frame.road_id.astype(str) == rid].iterrows():
            p = xy(link_row.geometry.coords)
            color = ORANGE if link_row.link_type == "aux" else BLUE
            ax.plot(p[:, 0], p[:, 1], color=color, linewidth=2.8)
            if link_row.link_type == "road":
                for point in (p[0], p[-1]):
                    node(ax, point)
        ax.set_title(title, loc="left", fontsize=12.5, color=INK, pad=12)
        ax.set_xlim(lower[0] - padding[0], upper[0] + padding[0])
        ax.set_ylim(lower[1] - padding[1], upper[1] + padding[1])
        ax.set_aspect("equal")
        ax.grid(color="#edf1f6", linewidth=.6)
        ax.set_xlabel("沿道路方向距离（m）", fontsize=10, color=MUTED)
        ax.set_ylabel("横向距离（m）", fontsize=10, color=MUTED)
        ax.tick_params(labelsize=9, colors=MUTED)
        ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(h_pad=1.8)
    figs.save(fig, "roadnet-taper-map")


def fig_source_crossing(figs):
    """用源中线、归档结果和当前线形解释交叉次数；叉号是几何交点，圆点才是 Node。

    Link 编号只用于图中标识，源组按 road_id、lane_ids 和行驶方向匹配。
    右侧局部图放大出口附近的浅穿越，避免整图把两条线的细小间距画成重合。
    """
    from roadnet.lanes import DRIVABLE
    from roadnet.source import prepare_source

    previous = figs.archived("2026-11-15-before-source-crossing-fix.zip",
                             "out/Town10HD_net.gpkg")
    current = figs.current("Town10HD")
    straight = select_row(current, "255", "-1,-2")
    turn = select_row(current, "382", "1")
    old_straight = select_row(previous, "255", "-1,-2")
    old_turn = select_row(previous, "382", "1")
    source_path = ROOT / "data/Town10HD.xodr"
    figs.sources[source_path.relative_to(ROOT).as_posix()] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source = prepare_source(ET.parse(source_path).getroot(), 1.0, DRIVABLE, True, 0.0, True)

    def source_line(record):
        """按正式 Link 的车道来源找吸附前中线，保留源里程对应的行驶方向。"""
        candidates = []
        lane_ids = {int(value) for value in record.lane_ids.split(",")}
        for direction in source.dirs_of(record.road_id):
            for group in source.groups[(record.road_id, direction)]:
                if set(group["lid"]) == lane_ids:
                    points = group["pts"] if direction > 0 else group["pts"][::-1]
                    candidates.append(LineString(points))
        if len(candidates) != 1:
            raise ValueError(f"源车道组不唯一：Road {record.road_id} / {record.lane_ids}")
        return candidates[0]

    source_straight, source_turn = source_line(straight), source_line(turn)
    stages = ((source_straight, source_turn, "1. 源车道群中线：中途穿越 1 次", BLUE, 1),
              (old_straight.geometry, old_turn.geometry, "2. 加工后：直行鼓包，中途穿越 0 次", RED, 0),
              (straight.geometry, turn.geometry, "3. 只拉直 Link 60：中途穿越 1 次", BLUE, 1))
    assert old_turn.geometry.equals_exact(turn.geometry, 1e-12)
    origin = np.asarray(straight.geometry.coords[0])[:2]
    direction = np.asarray(straight.geometry.coords[-1])[:2] - origin
    direction /= np.linalg.norm(direction)
    normal = np.array([-direction[1], direction[0]])

    def local(geometry):
        points = np.asarray(geometry.coords)[:, :2] - origin
        return np.column_stack([points @ direction, points @ normal])

    fig, axes = plt.subplots(3, 2, figsize=(11.5, 12.6),
                             gridspec_kw={"width_ratios": [1.7, 1]})
    for index, (first, second, title, color, expected) in enumerate(stages):
        assert net._crossing_count(first, second) == expected
        cross_points = [point for point in net._geom_points(first.intersection(second))
                        if min(first.project(point), first.length - first.project(point),
                               second.project(point), second.length - second.project(point)) > 1e-7]
        for ax, closeup in zip(axes[index], (False, True)):
            a, b = local(first), local(second)
            ax.plot(a[:, 0], a[:, 1], color=color, linewidth=2.5,
                    label=f"直行 Link {int(straight.link_id)} / Road 255")
            ax.plot(b[:, 0], b[:, 1], color=TEAL, linewidth=2.5,
                    label=f"转弯 Link {int(turn.link_id)} / Road 382")
            ax.set_xlim(25.0 if closeup else -2.0, 39.2 if closeup else 40.5)
            ax.set_ylim(-2.2 if closeup else -4.0, 1.15 if closeup else 12.0)
            ax.set_title("出口附近放大" if closeup else title, loc="left", color=INK, fontsize=12, pad=12)
            ax.grid(color="#edf1f6", linewidth=.7)
            ax.spines[["top", "right"]].set_visible(False)
            ax.tick_params(labelsize=9, colors=MUTED)
            ax.set_xlabel("沿直行方向距离（m）", fontsize=10, color=MUTED)
            ax.set_ylabel("横向距离（m）", fontsize=10, color=MUTED)
            for point in cross_points:
                position = local(point)[0]
                ax.plot(*position, "x", markersize=9, markeredgewidth=1.8, color=ORANGE, zorder=9)
            if index > 0:
                node(ax, a[-1])
                ax.annotate(f"Node {int(straight.to_node)}", a[-1],
                            xytext=(-36, 21), textcoords="offset points", fontsize=9, color=INK)
                if not closeup:
                    node(ax, a[0])
                    ax.annotate(f"Node {int(straight.from_node)}", a[0],
                                xytext=(-8, 18), textcoords="offset points", fontsize=9, color=INK)
            if not closeup:
                ax.legend(loc="lower left", frameon=True, fontsize=9)
                ax.add_patch(Rectangle((25.0, -2.2), 14.2, 3.35, fill=False,
                                       edgecolor=MUTED, linestyle="--", linewidth=1.0))
            else:
                if index == 0:
                    ax.text(.04, .08, "源转弯中线末点在另一条车道中心", transform=ax.transAxes,
                            fontsize=9, color=TEAL)
                elif index == 1:
                    ax.text(.04, .08, "红线位于绿线下方；仅在 Node 52 相接", transform=ax.transAxes,
                            fontsize=9, color=INK)
                else:
                    # 当前交点很浅；插图另放大横向距离，原坐标值仍按米显示。
                    detail = ax.inset_axes([.05, .38, .56, .48])
                    detail.plot(a[:, 0], a[:, 1], color=color, linewidth=1.8)
                    detail.plot(b[:, 0], b[:, 1], color=TEAL, linewidth=1.8)
                    for point in cross_points:
                        position = local(point)[0]
                        detail.plot(*position, "x", markersize=7, color=ORANGE)
                    detail.set_xlim(32.5, 35.2)
                    detail.set_ylim(-.028, .058)
                    detail.set_xticks([33, 34, 35])
                    detail.set_yticks([-.02, 0, .04])
                    detail.tick_params(labelsize=7)
                    detail.grid(color="#edf1f6", linewidth=.5)
                    detail.set_title("交点处再放大横向距离", fontsize=8, color=INK, pad=5)
                    ax.text(.04, .08, "叉号：中途交点；圆点：共同终点", transform=ax.transAxes,
                            fontsize=9, color=INK)
    fig.tight_layout(h_pad=2.0, w_pad=2.0, rect=(0, .035, 1, 1))
    fig.text(.08, .005, "绿色始终是 Link 67；源图未做端点对齐。几何交点不新增 Node，不开放两线间切换。",
             fontsize=9.5, color=MUTED)
    figs.save(fig, "town10hd-source-crossing-straightening")


def fig_joint_straightening(figs):
    """按归档几何展示单独拉直失败、联合调整成功；只改试算图，不修改正式结果。"""
    previous = figs.archived("2026-11-15-before-joint-connector-fix.zip", "out/Town10HD_net.gpkg")
    current = figs.current("Town10HD")
    before = {identifier: select_row(previous, road, lanes)
              for identifier, road, lanes in ((86, "736", "-1"), (88, "795", "-1"), (90, "832", "1"))}
    after = {identifier: select_row(current, road, lanes)
             for identifier, road, lanes in ((86, "736", "-1"), (88, "795", "-1"), (90, "832", "1"))}
    origin = np.asarray(after[86].geometry.coords[0])[:2]
    direction = np.asarray(after[86].geometry.coords[-1])[:2] - origin
    direction /= np.linalg.norm(direction)
    normal = np.array([-direction[1], direction[0]])

    def local(geometry):
        points = np.asarray(geometry.coords)[:, :2] - origin
        return np.column_stack([points @ direction, points @ normal])

    chord = LineString([before[86].geometry.coords[0], before[86].geometry.coords[-1]])
    stages = (
        ({key: record.geometry for key, record in before.items()},
         "1. 原结果：直行中段偏到一侧", RED, 0),
        ({86: chord, 88: before[88].geometry, 90: before[90].geometry},
         "2. 只拉直 86：与橙色 90 多出交叉", BLUE, 1),
        ({key: record.geometry for key, record in after.items()},
         "3. 同时调整 86 和 90：不再多出交叉", BLUE, 0),
    )
    assert before[88].geometry.equals_exact(after[88].geometry, 1e-12)
    fig, axes = plt.subplots(3, 2, figsize=(11.5, 12.2),
                             gridspec_kw={"width_ratios": [1.7, 1]})
    for index, (geometries, title, straight_color, expected) in enumerate(stages):
        assert net._crossing_count(geometries[86], geometries[90]) == expected
        assert net._crossing_count(geometries[86], geometries[88]) == 0
        crossings = [point for point in net._geom_points(geometries[86].intersection(geometries[90]))
                     if min(geometries[86].project(point), geometries[86].length - geometries[86].project(point),
                            geometries[90].project(point), geometries[90].length - geometries[90].project(point)) > 1e-7]
        for ax, closeup in zip(axes[index], (False, True)):
            for identifier, color in ((86, straight_color), (88, TEAL), (90, ORANGE)):
                points = local(geometries[identifier])
                ax.plot(points[:, 0], points[:, 1], color=color, linewidth=2.4,
                        label=f"Link {identifier} / Road {after[identifier].road_id}")
            straight = local(geometries[86])
            node(ax, straight[-1])
            ax.annotate("Node 72", straight[-1], xytext=(-36, 20),
                        textcoords="offset points", color=INK, fontsize=9)
            if not closeup:
                node(ax, straight[0])
                ax.annotate("Node 70", straight[0], xytext=(-12, 20),
                            textcoords="offset points", color=INK, fontsize=9)
            ax.set_title("出口附近放大" if closeup else title, loc="left", fontsize=12, color=INK, pad=12)
            ax.set_xlim(35 if closeup else -2, 47.5 if closeup else 50)
            ax.set_ylim(-.3 if closeup else -20, 2.35 if closeup else 6.5)
            ax.grid(color="#edf1f6", linewidth=.7)
            ax.spines[["top", "right"]].set_visible(False)
            ax.tick_params(labelsize=9, colors=MUTED)
            ax.set_xlabel("沿直行方向距离（m）", color=MUTED, fontsize=10)
            ax.set_ylabel("横向距离（m）", color=MUTED, fontsize=10)
            for point in crossings:
                position = local(point)[0]
                ax.plot(*position, "x", color=RED, markersize=9, markeredgewidth=1.8, zorder=9)
            if not closeup:
                ax.legend(loc="lower left", frameon=True, fontsize=9)
                ax.add_patch(Rectangle((35, -.3), 12.5, 2.65, fill=False,
                                       edgecolor=MUTED, linestyle="--", linewidth=1))
            elif index == 0:
                ax.text(.04, .92, "红色直行还在上方；橙线仅在终点接上", transform=ax.transAxes,
                        fontsize=9, color=INK, va="top")
            elif index == 1:
                # 约 6 cm 的橙线回摆需放大纵轴，才能看清与拟放入直线的新增穿越。
                detail = ax.inset_axes([.05, .43, .53, .38])
                for identifier, color in ((86, BLUE), (90, ORANGE)):
                    points = local(geometries[identifier])
                    detail.plot(points[:, 0], points[:, 1], color=color, linewidth=1.8)
                for point in crossings:
                    position = local(point)[0]
                    detail.plot(*position, "x", color=RED, markersize=7)
                detail.set_xlim(39.6, 43.2)
                detail.set_ylim(-.045, .09)
                detail.set_xticks([40, 41, 42, 43])
                detail.set_yticks([0, .06])
                detail.tick_params(labelsize=7)
                detail.grid(color="#edf1f6", linewidth=.5)
                detail.set_title("回摆处再放大横向距离", fontsize=8, color=INK, pad=5)
                ax.text(.04, .92, "试算方案未采用：叉号处是新交叉", transform=ax.transAxes,
                        fontsize=9, color=RED, va="top")
            else:
                ax.text(.04, .92, "橙线顺向接到 Node 72，避免穿过直行", transform=ax.transAxes,
                        fontsize=9, color=INK, va="top")
    fig.tight_layout(h_pad=2.0, w_pad=2.0, rect=(0, .035, 1, 1))
    fig.text(.08, .005, "绿色 Link 88 全程未改；蓝色 86 和橙色 90 同时通过检查后采用，节点保持原位。",
             color=MUTED, fontsize=9.5)
    figs.save(fig, "roadnet-town10-joint")


def fig_turn_refinement(figs):
    """同尺度展示源右转、旧接头和当前分摊结果，箭头均按车辆行驶方向绘制。

    用源 road 和车道组定位，核对源只有一次右转、旧几何多出左摆、当前采用位移分摊。
    A、B 代表固定的目标节点，P 是尚未对齐的源起点；仅生成配图，不写入路网。
    """
    from roadnet.lanes import DRIVABLE
    from roadnet.source import prepare_source

    previous = figs.archived("2026-11-15-before-connector-refinement.zip", "out/Town03_net.gpkg")
    current = figs.current("Town03")
    before = select_row(previous, "817", "1")
    after = select_row(current, "817", "1")
    source_path = ROOT / "data/Town03.xodr"
    figs.sources[source_path.relative_to(ROOT).as_posix()] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source = prepare_source(ET.parse(source_path).getroot(), 1.0, DRIVABLE, True, 0.0, True)
    matches = []
    for direction in source.dirs_of("817"):
        for group in source.groups[("817", direction)]:
            if set(group["lid"]) == {1}:
                points = group["pts"] if direction > 0 else group["pts"][::-1]
                matches.append(LineString(points))
    if len(matches) != 1:
        raise ValueError("Road 817 的源右转车道组不唯一")
    original = matches[0]
    target = np.asarray(after.geometry.coords)[[0, -1], :2]
    assert np.allclose(np.asarray(before.geometry.coords)[[0, -1], :2], target, atol=1e-12, rtol=0)

    # 核对正文所说的 3.5 m 起点横移和实际采用的候选，不把拟合示意误当作正式结果。
    points = net._resample(np.asarray(original.coords), .5)
    weights = net._smoothstep(net._arc_len(points) / net._arc_len(points)[-1])[:, None]
    aligned = points + (1 - weights) * (target[0] - points[0]) + weights * (target[1] - points[-1])
    aligned[0], aligned[-1] = target
    distributed = net.smooth_line(LineString(aligned), 1.5)
    assert distributed.hausdorff_distance(after.geometry) < 1e-8
    assert abs(float(np.linalg.norm(target[0] - points[0])) - 3.5) < 1e-8
    assert np.linalg.norm(target[1] - points[-1]) < 1e-8
    assert np.maximum(net._curve_turns(original), 0).sum() < .1
    assert np.maximum(net._curve_turns(before.geometry), 0).sum() > 10
    assert np.maximum(net._curve_turns(after.geometry), 0).sum() < 1

    incoming = points[1, :2] - points[0, :2]
    incoming /= np.linalg.norm(incoming)
    right = np.array([incoming[1], -incoming[0]])

    def local(geometry):
        """起点 A 为原点，横轴向进口右侧，纵轴沿进口方向；三幅统一旋转和比例。"""
        coords = np.asarray(geometry.coords)[:, :2] - target[0]
        return np.column_stack([coords @ right, coords @ incoming])

    fig, axes = plt.subplots(1, 3, figsize=(12.5, 5.8))
    stages = ((original, MUTED, "1. 源中线：只有一次右转"),
              (before.geometry, RED, "2. 旧结果：入口多摆了一下"),
              (after.geometry, BLUE, "3. 当前：逐渐右转到出口"))
    source_points = local(original)
    end = local(after.geometry)[-1]
    for index, (ax, (geometry, color, title)) in enumerate(zip(axes, stages)):
        if index:
            ax.plot(source_points[:, 0], source_points[:, 1], color=MUTED, linestyle="--",
                    linewidth=1.4, alpha=.65, label="源车道中线")
        stage_points = local(geometry)
        ax.plot(stage_points[:, 0], stage_points[:, 1], color=color, linewidth=2.7,
                label="源车道中线" if index == 0 else "旧结果" if index == 1 else "当前结果")
        for fraction in (.15, .46, .80):
            segment = substring(geometry, fraction * geometry.length, (fraction + .055) * geometry.length)
            positions = local(segment)
            arrow(ax, positions[0], positions[-1], color=color, width=2.0, size=12)
        node(ax, (0, 0), "A", offset=(-.45, .7))
        node(ax, end, "B", offset=(.35, .3))
        if index == 0:
            start = source_points[0]
            ax.plot(*start, "s", color=MUTED, markersize=5)
            ax.text(start[0] + .3, start[1] + .7, "P", fontsize=10, color=MUTED)
            ax.add_patch(FancyArrowPatch((0, 0), start, arrowstyle="<->", mutation_scale=12,
                                        linewidth=1.4, linestyle="--", color=MUTED))
            ax.text(start[0] / 2, -.9, "3.5 m", ha="center", fontsize=10, color=INK)
        elif index == 1:
            position = local(before.geometry.interpolate(before.geometry.length * .23))[0]
            ax.annotate("多出一次向左修正", position, xytext=(.1, 7.2), fontsize=9.5,
                        color=RED, arrowprops={"arrowstyle": "->", "color": RED, "lw": 1.2})
        ax.set_title(title, loc="left", fontsize=11.5, color=INK, pad=14)
        ax.set_xlim(-1.5, 15.3)
        ax.set_ylim(-2.1, 13.9)
        ax.set_aspect("equal")
        ax.grid(color="#edf1f6", linewidth=.6)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(labelsize=9, colors=MUTED)
        ax.set_xlabel("横向距离（m）", fontsize=9.5, color=MUTED)
        ax.set_ylabel("沿进口方向距离（m）", fontsize=9.5, color=MUTED)
        ax.legend(loc="lower right", fontsize=9, frameon=True)
    fig.tight_layout(w_pad=1.8, rect=(0, .10, 1, 1))
    fig.text(.04, .025,
             f"Town03 Link {int(after.link_id)} / Road 817；A = Node {int(after.from_node)}，B = Node {int(after.to_node)}；箭头表示行驶方向。",
             fontsize=9.5, color=MUTED)
    figs.save(fig, "roadnet-town03-turn")


def fig_town03_refinement(figs):
    """分别绘制 Town03 直行鼓包和右转摆动案例，两者采用各自所需的说明方式。"""
    old = figs.archived("2026-11-15-before-connector-refinement.zip", "out/Town03_net.gpkg")
    current = figs.current("Town03")
    map_pair(figs, old, current, "1426", "roadnet-town03-straight",
             ("修复前：直行两端偏移，中间留着鼓包",
              f"当前：Link {int(select_row(current, '1426', '2,3').link_id)} 是 38.61 m 直线"),
             lanes="2,3", orient_horizontal=True,
             note="Town03 Road 1426；同一方向的代表线，横向与纵向按相同比例显示。")
    fig_turn_refinement(figs)


def fig_parallel_turns(figs):
    """临时生成未合并版本与正式结果对照，记录源输入，不覆盖正式路网。"""
    source = ROOT / "data/Town06.xodr"
    figs.sources["data/Town06.xodr"] = hashlib.sha256(source.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="roadnet_unmerged_figure_") as folder:
        unmerged, _ = net.build_net(str(source), str(Path(folder) / "unmerged.gpkg"),
                                   merge_turns=False, verbose=False)
    current = figs.current("Town06")
    merged = current[current.junction_id == "72"]
    unmerged = unmerged[unmerged.junction_id == "72"]
    bounds = merged.total_bounds
    pad = 4
    fig, axs = plt.subplots(2, 1, figsize=(7.2, 6.9))
    for ax, frame, title in zip(axs, (unmerged, merged), ("同一批转向：保留全部平行连接路", "按入口 Node 和出口 Node 合并后")):
        for _, row in frame.iterrows():
            p = np.asarray(row.geometry.coords)
            ax.plot(p[:, 0], p[:, 1], color=ORANGE, linewidth=1.9)
            for point in (p[0], p[-1]):
                node(ax, point)
        ax.set_title(title, loc="left", color=INK, fontsize=12.5, pad=12)
        ax.set_xlim(bounds[0] - pad, bounds[2] + pad)
        ax.set_ylim(bounds[1] - pad, bounds[3] + pad)
        ax.set_aspect("equal")
        ax.axis("off")
    fig.tight_layout(h_pad=1.6)
    fig.text(.05, -.01, "Town06 junction 72；只合并同一起止节点的线，不把不同转向压成一条。", fontsize=9.5, color=MUTED)
    figs.save(fig, "roadnet-parallel-turns")


def fig_uturn_expression(figs):
    """用坪山源声明画道路和车道配对；线形为示意，编号及连接端从 XODR 读取。"""
    path = ROOT / "data/Pingshan_20250606_OD.xodr"
    root = ET.parse(path).getroot()
    connection = root.find("junction[@id='190']/connection[@id='1']")
    if connection is None or connection.get("contactPoint") != "start":
        raise ValueError("坪山 junction 190 connection 1 不符合本文的 start 接入示例")
    mapping = connection.find("laneLink")
    incoming_id, road_id = connection.get("incomingRoad"), connection.get("connectingRoad")
    road = root.find(f"road[@id='{road_id}']")
    if mapping is None or road is None:
        raise ValueError("坪山掉头示例缺少 connecting road 或 laneLink")
    lane_id = mapping.get("to")
    lane = road.find(f"lanes/laneSection/right/lane[@id='{lane_id}']")
    outgoing = road.find("link/successor")
    outgoing_lane = lane.find("link/successor") if lane is not None else None
    if outgoing is None or outgoing_lane is None:
        raise ValueError("坪山掉头示例缺少出口道路或出口车道声明")
    figs.sources[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()

    fig, axs = plt.subplots(2, 1, figsize=(7.6, 7.4),
                            gridspec_kw={"height_ratios": [1.05, 1]})
    ax = axs[0]
    axes_panel(ax, "源掉头路径：进口车道 → 连接车道 → 反向出口车道", (-1, 35), (-2.2, 9))
    line(ax, [(1, 0), (15, 0)], BLUE, 3)
    line(ax, [(15, 5), (1, 5)], TEAL, 3)
    theta = np.linspace(0, np.pi, 100)
    arc = np.column_stack([15 + 4 * np.sin(theta), 2.5 - 2.5 * np.cos(theta)])
    line(ax, arc, ORANGE, 3)
    arrow(ax, (4, 0), (10, 0), BLUE)
    arrow(ax, (10, 5), (4, 5), TEAL)
    arrow(ax, arc[38], arc[56], ORANGE)
    ax.text(1, 1.0, f"驶入：Road {incoming_id}\n车道 {mapping.get('from')}",
            color=BLUE, fontsize=10.5, linespacing=1.5)
    ax.text(1, 6.1, f"驶出：Road {outgoing.get('elementId')}\n车道 {outgoing_lane.get('id')}",
            color=TEAL, fontsize=10.5, linespacing=1.5)
    ax.text(21, 1.9, f"连接 Road {road_id}\n车道 {lane_id}\njunction = {road.get('junction')}",
            color=ORANGE, fontsize=10.5, linespacing=1.6)
    node(ax, (15, 0), "s = 0", offset=(-1.7, -1.0))
    node(ax, (15, 5), "s 末端", offset=(-1.7, .7))
    ax.text(1, -1.9, "线形为示意；编号和车道配对来自坪山源文件。", color=MUTED, fontsize=9.5)

    ax = axs[1]
    axes_panel(ax, "对照声明：laneLink 的 to 只到连接车道", (0, 32), (0, 7.8))
    records = ((5.9, BLUE, "junction / connection / laneLink",
                f"进口车道 {mapping.get('from')} → 连接车道 {lane_id}"),
               (3.7, ORANGE, "连接 road / link / successor",
                f"Road {road_id} → Road {outgoing.get('elementId')}（{outgoing.get('contactPoint')}）"),
               (1.5, TEAL, "连接 lane / link / successor",
                f"连接车道 {lane_id} → 出口车道 {outgoing_lane.get('id')}"))
    for y, color, title, detail in records:
        ax.add_patch(FancyBboxPatch((.4, y), 31.0, 1.5, boxstyle="round,pad=.15",
                                   facecolor="#f5f7fb", edgecolor=color, linewidth=1.1))
        ax.text(1.2, y + 1.02, title, color=color, fontsize=10.5)
        ax.text(1.2, y + .37, detail, color=INK, fontsize=11)
    fig.tight_layout(h_pad=1.8)
    figs.save(fig, "roadnet-uturn-expression")


def fig_uturn_extraction(figs):
    """画源关系、车道组和回转保护的先后步骤，对应当前单 connectingRoad 提取范围。"""
    fig, ax = plt.subplots(figsize=(7.5, 8.5))
    axes_panel(ax, "提取掉头：先确定连接关系，最后核对有向路径", (0, 11), (0, 18.2))
    steps = (("1. 查源关系", "incomingRoad、connectingRoad、laneLink", BLUE),
             ("2. 从接触端追到出口", "contactPoint、road/link、跨段车道配对", BLUE),
             ("3. 核对车道类型和方向", "路肩例外只接已有的同侧、同方向通行组", TEAL),
             ("4. 计算中线并接到固定节点", "点列沿实际行驶方向，起止为 Node A、B", TEAL),
             ("5. 保留回转弧，合并平行掉头", "同 junction、同起止节点保留一条代表线", ORANGE),
             ("6. 写出前核对进口 → 掉头 → 出口", "节点接续、回转形状和源路径都要对应", ORANGE))
    positions = np.linspace(15.9, 2.4, len(steps))
    for index, ((title, detail, color), y) in enumerate(zip(steps, positions)):
        ax.add_patch(FancyBboxPatch((.4, y), 10.2, 1.8, boxstyle="round,pad=.12",
                                   facecolor="#f5f7fb", edgecolor=color, linewidth=1.2))
        ax.text(.9, y + 1.17, title, color=color, fontsize=12)
        ax.text(.9, y + .47, detail, color=INK, fontsize=10.5)
        if index + 1 < len(positions):
            arrow(ax, (5.5, y - .18), (5.5, positions[index + 1] + 2.0), MUTED, width=1.4)
    ax.text(.45, .8, "无源掉头声明不补边；方向不匹配的路肩路径过滤。", color=MUTED, fontsize=10)
    fig.tight_layout()
    figs.save(fig, "roadnet-uturn-extraction")


def fig_uturn_rules(figs):
    """掉头连接独立节点；源路肩回转线只在实际行驶方向接通时保留。"""
    fig, axs = plt.subplots(3, 1, figsize=(7.4, 8.0))
    titles = ("有效掉头：A → B 是一条单向 connector",
              "没有源掉头声明：两个方向分别保留",
              "源路肩掉头：核实方向后，接入已有通行组")
    for ax, title in zip(axs, titles):
        axes_panel(ax, title, (0, 30), (-2.2, 7.0))
        line(ax, [(1, 0), (15, 0)], BLUE)
        line(ax, [(15, 4), (1, 4)], TEAL)
        arrow(ax, (4, 0), (10, 0), BLUE)
        arrow(ax, (10, 4), (4, 4), TEAL)
        node(ax, (15, 0), "A", offset=(-.4, -.95))
        node(ax, (15, 4), "B", offset=(-.4, .65))
    theta = np.linspace(0, np.pi, 100)
    arc = np.column_stack([15 + 4 * np.sin(theta), 2 - 2 * np.cos(theta)])
    line(axs[0], arc, ORANGE)
    arrow(axs[0], arc[38], arc[56], ORANGE)
    axs[0].text(20, 2.0, "掉头 Link", color=ORANGE, fontsize=10)
    axs[0].text(1, -1.8, "进口 → 掉头边 → 反向出口；A、B 仍是两个节点", color=INK, fontsize=10)
    axs[1].plot([15, 15], [0, 4], color=PALE, linestyle="--", linewidth=1.4)
    axs[1].text(17, 1.8, "位置近 ≠ 有返回边", color=MUTED, fontsize=10)
    axs[1].text(1, -1.8, "不能把两个节点合并，也不自动补出掉头", color=INK, fontsize=10)
    line(axs[2], arc, ORANGE)
    arrow(axs[2], arc[38], arc[56], ORANGE)
    axs[2].text(20, 2.0, "源路肩掉头", color=ORANGE, fontsize=10)
    axs[2].text(1, -1.8, "没有对应机动车方向时过滤；普通路肩、人行道仍不开放", color=INK, fontsize=9.5)
    fig.tight_layout(h_pad=1.8)
    figs.save(fig, "roadnet-uturn-rules")


def fig_pingshan_uturn(figs):
    """对照源掉头、归档中压窄的回转弧与当前有向路径，编号和长度均取自对应产物。"""
    lane_path = ROOT / "out/Pingshan_20250606_OD.gpkg"
    lanes = gpd.read_file(lane_path, layer="JunctionLink")
    figs.sources[f"out/{lane_path.name}:JunctionLink"] = hashlib.sha256(lane_path.read_bytes()).hexdigest()
    links = figs.current("Pingshan_20250606_OD")
    incoming = links[(links.road_id == "202310038") & (links.link_type == "road")].iloc[0]
    outgoing = links[(links.road_id == "202310008") & (links.link_type == "road")].iloc[0]
    candidates = links[(links.junction_id == "190") & (links.from_node == incoming.to_node)
                       & (links.to_node == outgoing.from_node)]
    if len(candidates) != 1:
        raise ValueError("坪山掉头的当前代表线不唯一，需先核对正式输出")
    uturn = candidates.iloc[0]
    before = figs.archived("2026-11-16-before-roadnet-modules.zip",
                           "out/Pingshan_20250606_OD_net.gpkg")
    old_candidates = before[(before.junction_id == "190") & (before.road_id == "194")
                            & (before.from_node == uturn.from_node) & (before.to_node == uturn.to_node)]
    if len(old_candidates) != 1:
        raise ValueError("坪山归档掉头的源道路和节点不匹配，需核对对比版本")
    old_uturn = old_candidates.iloc[0]
    origin = np.asarray(uturn.geometry.coords[0])[:2]
    tangent = net._end_tangent(np.asarray(incoming.geometry.coords))
    normal = np.array([-tangent[1], tangent[0]])
    def xy(points):
        points = np.asarray(points)[:, :2] - origin
        return np.column_stack([points @ tangent, points @ normal])
    main_parts = (substring(incoming.geometry, max(0, incoming.geometry.length - 12), incoming.geometry.length),
                  substring(outgoing.geometry, 0, min(12, outgoing.geometry.length)))
    sources = lanes[lanes.connecting_road.isin(["191", "194"])]
    if len(sources) != 2:
        raise ValueError("坪山掉头源 laneLink 数量变化，需核对对应案例")
    all_points = np.vstack([xy(uturn.geometry.coords), xy(old_uturn.geometry.coords)]
                          + [xy(g.coords) for g in main_parts]
                          + [xy(g.coords) for g in sources.geometry])
    low, high = all_points.min(axis=0) - 2.5, all_points.max(axis=0) + 2.5
    fig, axs = plt.subplots(3, 1, figsize=(7.5, 12))
    for ax, title in zip(axs, ("源声明：Road 191、194 是两条机动车掉头路径",
                              "归档：普通平滑和贴线把回转弧压窄",
                              f"当前：保留掉头弧；Link {int(incoming.link_id)} → {int(uturn.link_id)} → {int(outgoing.link_id)}")):
        for part, color in zip(main_parts, (BLUE, TEAL)):
            p = xy(part.coords)
            ax.plot(p[:, 0], p[:, 1], color=color, linewidth=2.2)
        ax.set_title(title, loc="left", color=INK, fontsize=12, pad=12)
        ax.set_xlim(low[0], high[0])
        ax.set_ylim(low[1], high[1])
        ax.set_aspect("equal")
        ax.grid(color="#edf1f6", linewidth=.6)
        ax.tick_params(labelsize=9, colors=MUTED)
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_xlabel("沿进口行驶方向距离（m）", color=MUTED, fontsize=10)
        ax.set_ylabel("横向距离（m）", color=MUTED, fontsize=10)
    for (_, row), color in zip(sources.iterrows(), (ORANGE, MUTED)):
        p = xy(row.geometry.coords)
        axs[0].plot(p[:, 0], p[:, 1], color=color, linewidth=2.4,
                    label=f"源 Road {row.connecting_road}")
        arrow(axs[0], p[len(p) // 2 - 1], p[len(p) // 2 + 1], color, size=12)
    axs[0].legend(loc="upper left", frameon=True, fontsize=9)
    for ax, record, color in ((axs[1], old_uturn, RED), (axs[2], uturn, ORANGE)):
        p = xy(record.geometry.coords)
        ax.plot(p[:, 0], p[:, 1], color=color, linewidth=2.8)
        arrow(ax, p[len(p) // 2 - 1], p[len(p) // 2 + 1], color)
        for part, road_color in zip(main_parts, (BLUE, TEAL)):
            point = xy([part.interpolate(3).coords[0], part.interpolate(7).coords[0]])
            arrow(ax, point[0], point[1], road_color)
        for point, identifier in ((p[0], record.from_node), (p[-1], record.to_node)):
            node(ax, point)
            ax.annotate(f"Node {int(identifier)}", point, xytext=(-52, -20),
                        textcoords="offset points", color=INK, fontsize=10)
        ax.text(.03, .52, f"掉头 Link {int(record.link_id)}\n{record.length:.3f} m\noneway = 1",
                transform=ax.transAxes, color=color, fontsize=10,
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": .9})
    fig.tight_layout(h_pad=2)
    fig.text(.08, -.01, "荔景南路 junction 190；两条源路径合成一条可达边，对向节点保持独立。", color=MUTED, fontsize=9.5)
    figs.save(fig, "roadnet-pingshan-uturn")


def fig_workflow(figs):
    """按当前模块调用顺序展示车道筛选、源掉头保护、线形整理与写出前后校验。"""
    stages = (
        ("1. 读取道路和车道", "参考线、分段宽度、junction 与源连接声明"),
        ("2. 筛选车道并确定方向", "每条 Link 单向；双向通行按两个方向表达"),
        ("3. 分主组、辅道及连接组", "计算代表线，追踪跨段换号的车道"),
        ("4. 合并端口，确定 Node", "按来源和方向接头；主路、辅道接点分别判断"),
        ("5. 生成 Link 并对齐端点", "普通线处理接头；源掉头沿弧长分摊节点偏移"),
        ("6. 分别合并普通转向与掉头", "同路口、同起止节点合并；保留全部源引用"),
        ("7. 整理普通连接线形状", "平滑、贴线、拉直与转弯；源掉头保留回转弧"),
        ("8. 省略重复辅道，核验并写出", "主组保留原线；重算编号、节点度数并检查路网"),
    )
    fig, ax = plt.subplots(figsize=(6.2, 10.7))
    ax.set_xlim(0, 10)
    ax.set_ylim(-.7, 13.5)
    ax.axis("off")
    for i, (title, detail) in enumerate(stages):
        y = 11.9 - i * 1.65
        ax.add_patch(FancyBboxPatch((.55, y), 8.9, 1.13,
                                   boxstyle="round,pad=.12,rounding_size=.07",
                                   facecolor="#edf3f8" if i % 2 == 0 else "#edf6f3",
                                   edgecolor="#bfcedd"))
        ax.text(5, y + .74, title, ha="center", fontsize=12, color=INK)
        ax.text(5, y + .3, detail, ha="center", fontsize=9.6, color=MUTED)
        if i < len(stages) - 1:
            arrow(ax, (5, y - .04), (5, y - .48), MUTED, 1.4, 12)
    figs.save(fig, "roadnet-article-workflow")


def fig_uturn_coverage(figs):
    """从独立覆盖报告画源候选和类型筛选数量；区分源路径条数与合并后的 Link 数。"""
    path = ROOT / "out/junction_uturn_coverage_report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    figs.sources[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    groups = (("坪山", ("Pingshan_20250606_OD",)),
              ("Town03", ("Town03",)),
              ("Town04", ("Town04",)),
              ("Town06", ("Town06",)),
              ("其余 8 份", tuple(name for name in report["maps"]
                                if name not in {"Pingshan_20250606_OD", "Town03", "Town04", "Town06"})))
    allowed, shoulders, filtered = [], [], []
    for _, names in groups:
        allowed.append(sum(report["maps"][name]["counts"]["default_motor_uturn_source_paths"] for name in names))
        shoulders.append(sum(report["maps"][name]["counts"]["retained_shoulder_uturn_source_paths"] for name in names))
        filtered.append(sum(report["maps"][name]["counts"]["remaining_filtered_reverse_source_paths"] for name in names))
    fig, ax = plt.subplots(figsize=(7.4, 5.3))
    positions = np.arange(len(groups))
    ax.barh(positions, allowed, height=.56, color=TEAL, label="默认允许的机动车源路径")
    ax.barh(positions, shoulders, left=allowed, height=.56, color=ORANGE, label="方向接通的源路肩掉头")
    ax.barh(positions, filtered, left=np.asarray(allowed) + np.asarray(shoulders), height=.56,
            color="#aebacb", label="没有匹配机动车方向")
    for index, (motor, shoulder, other) in enumerate(zip(allowed, shoulders, filtered)):
        total = motor + shoulder + other
        ax.text(total + .12, index, f"{total} 条源路径", va="center", fontsize=10, color=INK)
    ax.set_yticks(positions, [label for label, _ in groups], fontsize=11)
    ax.set_xticks(range(6))
    ax.set_xlim(0, 6.1)
    ax.invert_yaxis()
    ax.set_xlabel("进口、出口行驶夹角至少 145° 的源 connection 数", color=MUTED, fontsize=10)
    ax.set_title("12 份源数据：5 条源路径接通，合为 4 条掉头 Link", loc="left", color=INK, fontsize=13, pad=14)
    ax.grid(axis="x", color="#edf1f6", linewidth=.7)
    ax.set_axisbelow(True)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.legend(loc="upper right", frameon=False, fontsize=9)
    fig.subplots_adjust(left=.17, right=.96, top=.86, bottom=.27)
    fig.text(.08, .13, "坪山两条 driving 合为一条；Town03 保留两条路肩掉头，Town06 保留一条。", color=INK, fontsize=9.5)
    fig.text(.08, .075, "范围：238 个 junction、2,204 条 connection；福田另有 46 条缺源道路或出口，无法完整核实。", color=MUTED, fontsize=9)
    figs.save(fig, "roadnet-uturn-coverage")


def fig_shoulder_uturns(figs):
    """按正式输出重画三条方向接通的路肩掉头，源线和机动车接入范围分别显示。"""
    from own.xodr2gpkg import Road, lane_center_index, parse_header_offset
    import xml.etree.ElementTree as ET

    fig, axes = plt.subplots(3, 1, figsize=(7.5, 12.5))
    for ax, (name, rid) in zip(axes, (("Town03", "1202"), ("Town03", "1688"), ("Town06", "719"))):
        path = ROOT / "data" / (name + ".xodr")
        figs.sources[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        root = ET.parse(path).getroot()
        road = Road(root.find(f"road[@id='{rid}']"), step=.25, offset=parse_header_offset(root))
        source = next(iter(lane_center_index(road, with_z=False).values()))["pts"]
        links = figs.current(name)
        turn = select_row(links, rid)
        incoming = links[(links.to_node == turn.from_node) & (links.link_type == "road")].iloc[0]
        outgoing = links[(links.from_node == turn.to_node) & (links.link_type == "road")].iloc[0]
        origin = np.asarray(turn.geometry.coords[0])
        tangent = net._end_tangent(incoming.geometry.coords)
        normal = np.array([-tangent[1], tangent[0]])

        def xy(points):
            points = np.asarray(points)[:, :2] - origin
            return np.column_stack([points @ tangent, points @ normal])

        parts = (substring(incoming.geometry, max(0, incoming.geometry.length - 12), incoming.geometry.length),
                 substring(outgoing.geometry, 0, min(12, outgoing.geometry.length)))
        for part, color in zip(parts, (BLUE, TEAL)):
            points = xy(part.coords)
            line(ax, points, color)
            arrow_points = xy([part.interpolate(3).coords[0], part.interpolate(min(7, part.length)).coords[0]])
            arrow(ax, arrow_points[0], arrow_points[1], color)
        raw, points = xy(source), xy(turn.geometry.coords)
        ax.plot(raw[:, 0], raw[:, 1], "--", color=MUTED, linewidth=1.8, label="源路肩中线")
        ax.plot(points[:, 0], points[:, 1], color=ORANGE, linewidth=2.8, label="接入通行组后的掉头")
        for fraction in (.35, .65):
            pos = int(fraction * (len(points) - 1))
            arrow(ax, points[pos], points[min(pos + 2, len(points) - 1)], ORANGE)
        for point, identifier in ((points[0], turn.from_node), (points[-1], turn.to_node)):
            node(ax, point)
            offset = (-50, 12) if identifier == turn.from_node else (-45, -20)
            ax.annotate(f"Node {int(identifier)}", point, xytext=offset, textcoords="offset points", fontsize=9.5)
        ax.set_title(f"{name} Road {rid} → Link {int(turn.link_id)}；{turn.geometry.length:.3f} m",
                     loc="left", color=INK, fontsize=12, pad=12)
        # 窄掉头图把图例放到轴外，避免挡住上方节点和回转弧。
        ax.legend(loc="center left", bbox_to_anchor=(1.02, .55), frameon=False, fontsize=9)
        ax.set_aspect("equal")
        ax.grid(color="#edf1f6", linewidth=.6)
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_xlabel("沿进口方向距离（m）", color=MUTED, fontsize=10)
        ax.set_ylabel("横向距离（m）", color=MUTED, fontsize=10)
    fig.tight_layout(h_pad=2)
    figs.save(fig, "roadnet-shoulder-uturns")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs/figures")
    args = parser.parse_args()
    with ExitStack() as stack:
        figures = Figures(args.output_dir, stack)
        for generate in (fig_intro, fig_lane_groups, fig_directions, fig_shared_lanes,
                         fig_section_counts, fig_lane_section_boundary, fig_node_cases,
                         fig_node_position, fig_height_cases, fig_taper_rules,
                         fig_auxiliary_nodes, fig_mixed_connections, fig_endpoint_methods,
                         fig_auxiliary_join, fig_smoothing, fig_short_links, fig_preserved_shapes,
                         fig_curve_biarc, fig_curve_quadratic, fig_curve_displacement,
                         fig_curve_gaussian, fig_curve_fit_selection,
                         fig_crossing_limits, fig_connectivity, fig_history_comparisons,
                         fig_taper_map, fig_source_crossing, fig_joint_straightening, fig_town03_refinement,
                         fig_parallel_turns, fig_uturn_expression, fig_uturn_extraction,
                         fig_uturn_rules, fig_pingshan_uturn, fig_workflow,
                         fig_uturn_coverage, fig_shoulder_uturns):
            generate(figures)
        (figures.output / "roadnet-figure-sources.json").write_text(
            json.dumps({"source_sha256": figures.sources,
                        "note": "示意图解释处理规则；归档图只代表其历史阶段，当前结果按源 road 和车道组对齐。"},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
