#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""make_figs.py —— 生成文档配图（PNG）。

给 docs/基于GDAL官方驱动从OpenDRIVE中提取GIS图层.md 的 4.3 节配两张示意图：

  1. offset-vs-radius.png    横向偏移 t 与曲率半径 R 的三态对比（t<R / t=R / t>R）
  2. lane-triangulation.png  自相交条带切成三角面后，面片各自合法但互相叠压
  3. width-taper.png         6.1 节：1 µm 内宽度归零的 S 曲线 + 系数量级对比

为什么用 matplotlib 直接画，而不是先做 SVG 再转 PNG
    venv 里没有 cairosvg / svglib，为一个配图任务去引 cairo 原生依赖不划算。
    matplotlib 已装（3.11.2），且这两张图的几何全是参数方程算出来的 ——
    改半径、改分段数只需改常量，比手调 SVG 路径可靠。

几何要点（别改错）
    内侧边界 = 半径 (R - t) 的同心弧。**R - t 取负值会自动翻转到圆心另一侧**，
    这正是 t > R 产生自相交的原因，所以代码里直接用负半径，不要取 abs。
    图 2 故意用 t > R：正常条带本来就没问题，切三角面看不出意义；只有自相交条带
    切完才暴露出"面片各自合法、整片互相叠压"这个真问题。

用法（项目根目录执行）
    python code/diag/make_figs.py

输出
    docs/figures/offset-vs-radius.png
    docs/figures/lane-triangulation.png
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")  # 无 GUI 后端，必须在 pyplot 之前切

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Polygon
from shapely.geometry import Polygon as SPoly
from shapely.ops import unary_union

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.paths import DOCS, ensure  # noqa: E402

# 中文字体（Windows 自带；Linux/macOS 需另配）
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

FIG_DIR = os.path.join(DOCS, "figures")

# 配色（与在线 SVG 示意图保持一致）
GRAY_LINE = "#5F5E5A"   # 参考线
GRAY_HINT = "#B4B2A9"   # 半径虚线 / 曲率中心
GRAY_TEXT = "#5F5E5A"
INK = "#2C2C2A"


def arc(cx, cy, r, th0, th1, n=240):
    """圆弧采样点。r 可以为负 —— 负半径 = 翻到圆心另一侧。"""
    th = np.linspace(th0, th1, n)
    return cx + r * np.cos(th), cy + r * np.sin(th)


def fig_offset_tristate(path):
    """图 1：内侧边界有效半径 = R - t 的三种状态。"""
    R = 60.0
    # 用一段 140° 的弧，不用整半圆：整半圆时两条封口线都落在 y=0 上会重合成一条直线，
    # 反而看不出 t > R 的自相交；用部分弧，两条封口线就会呈 X 形交叉。
    TH0, TH1 = np.deg2rad(160.0), np.deg2rad(20.0)
    CASES = [
        # (圆心 x, 偏移 t, 填充, 描边, 公式, 说明)
        (0.0, 25.0, "#E1F5EE", "#0F6E56", "t < R", "条带正常\n合法多边形"),
        (160.0, 60.0, "#FAEEDA", "#854F0B", "t = R", "边界收缩成\n一个尖点"),
        (320.0, 85.0, "#FCEBEB", "#A32D2D", "t > R", "偏移线翻转\n条带自相交"),
    ]

    fig, ax = plt.subplots(figsize=(11.5, 4.0), dpi=200)

    # 图例
    ax.plot([-72, -52], [82, 82], color=GRAY_LINE, lw=2.5, solid_capstyle="round")
    ax.text(-47, 82, "参考线（曲率半径 R）", va="center", fontsize=11, color=INK)
    ax.plot([78, 98], [82, 82], color=INK, lw=2.0, solid_capstyle="round")
    ax.text(103, 82, "车道内侧边界（偏移 t）", va="center", fontsize=11, color=INK)
    ax.plot(255, 82, marker="o", ms=4.5, color=GRAY_HINT)
    ax.text(262, 82, "曲率中心 O", va="center", fontsize=11, color=INK)

    for cx, t, fc, ec, formula, note in CASES:
        ox, oy = arc(cx, 0.0, R, TH0, TH1)            # 参考线
        ix, iy = arc(cx, 0.0, R - t, TH0, TH1)        # 内侧边界：同心弧（负半径自动翻转）

        # 条带 = 外弧正向 + 内弧反向 闭合。
        # t > R 时两条封口线呈 X 形交叉 —— 这就是"自相交"，用 edgecolor 描出来。
        ax.fill(np.r_[ox, ix[::-1]], np.r_[oy, iy[::-1]],
                facecolor=fc, edgecolor=ec, lw=1.4, zorder=2)
        ax.plot(ox, oy, color=GRAY_LINE, lw=2.5, solid_capstyle="round", zorder=3)
        if abs(R - t) > 1e-9:                          # t = R 时内边界退化为一点，不画
            ax.plot(ix, iy, color=ec, lw=2.0, solid_capstyle="round", zorder=3)

        ax.plot(cx, 0.0, marker="o", ms=4.5, color=GRAY_HINT, zorder=4)
        ax.plot([cx, cx], [0.0, R], color=GRAY_HINT, lw=1.0, ls="--", zorder=1)
        ax.text(cx - 8, 12, "R", ha="right", va="center", fontsize=12, color=GRAY_TEXT)

        # t 双向箭头：从参考线顶点指到内侧边界顶点
        ax.annotate("", xy=(cx, R - t), xytext=(cx, R),
                    arrowprops=dict(arrowstyle="<->", color=GRAY_TEXT, lw=1.0,
                                    shrinkA=0, shrinkB=0), zorder=5)
        ax.text(cx + 6, R - t / 2, "t", ha="left", va="center",
                fontsize=12, color=GRAY_TEXT)

        ax.text(cx, -48, formula, ha="center", va="center", fontsize=14.5,
                color=INK)
        ax.text(cx, -68, note, ha="center", va="center", fontsize=11.5,
                color=GRAY_TEXT, linespacing=1.6)

    ax.set_aspect("equal")
    ax.set_xlim(-80, 400)
    ax.set_ylim(-78, 92)
    ax.axis("off")
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# 图 2 配色
C1, C2, EDGE = "#E4F6EF", "#B9E4D0", "#0F6E56"   # 三角面（深浅交替）
EDGE_SOFT = "#63B79A"                              # 左图面片描边：淡一点才不被交叉线糊住
F_ORANGE, EDGE_O = "#FAE0C8", "#854F0B"            # 右图第二片面片（换个色系才分得清）
C_RED = "#A32D2D"                                  # 翻转到另一侧的偏移线
OV, OV_FILL = "#534AB7", "#B7B0E8"                 # 重叠区


def _triangles(P, Q, N):
    """每段四边形 [P_i, P_i+1, Q_i+1, Q_i] 沿 P_i–Q_i+1 对角线切成两个三角面。"""
    tris = []
    for i in range(N):
        tris.append((i, np.vstack([P[i], P[i + 1], Q[i + 1]])))
        tris.append((i, np.vstack([P[i], Q[i + 1], Q[i]])))
    return tris


def fig_triangulation(path):
    """图 2：**自相交**条带切成三角面 —— 面片各自合法，整片互相叠压。

    为什么不用 t < R 的正常条带：那种条带本来就没毛病，切完三角面一片干净，
    既看不出"为什么要切"，也看不出"切完还剩什么问题"。
    换成 t > R，四边形变成蝴蝶形（顶点顺序被偏移线翻转），切成两个三角面后
    每片都是合法三角形，但两片在平面上**重叠** —— 这才是 2D 多边形装不下、
    必须交给带 Z 的 TIN 的那个理由。

    参数怎么选
        t 比 R 大得多时内边界缩成小圈，所有三角面都汇聚到圆心，画出来是爆炸状星芒，
        读不出东西。取 t ≈ 1.6R（r = -35）让翻转后的内弧还有足够半径，
        既看得出来回翻转，又不至于糊成一团。
    """
    R, t, N = 60.0, 95.0, 4          # t > R：内侧边界翻到圆心另一侧（半径 -35）
    r = R - t                        # 刻意保留负值，负半径才会翻转
    TH0, TH1 = np.deg2rad(160.0), np.deg2rad(20.0)

    th = np.linspace(TH0, TH1, N + 1)
    P = np.c_[R * np.cos(th), R * np.sin(th)]        # 参考线（曲率半径 R 一侧）
    Q = np.c_[r * np.cos(th), r * np.sin(th)]        # 偏移后的边界，翻到圆心下方
    tris = _triangles(P, Q, N)

    # 面片两两求交，找出真正叠压的组合（共享边相交面积为 0，会被阈值滤掉）
    spolys = [SPoly(pts) for _, pts in tris]
    overlaps = []
    for a in range(len(spolys)):
        for b in range(a + 1, len(spolys)):
            inter = spolys[a].intersection(spolys[b])
            if inter.area > 5.0:
                overlaps.append(inter)
    ov_union = unary_union(overlaps) if overlaps else None

    fig = plt.figure(figsize=(10.8, 4.6), dpi=200)
    # 左右两轴等高，宽度按各自数据范围的长宽比分，aspect=equal 时才不留白边
    gs = fig.add_gridspec(1, 2, width_ratios=[1.24, 1.14], wspace=0.08)
    ax1, ax2 = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])

    # ---------- 左：整条自相交条带的三角剖分 ----------
    # 面片描边用淡绿：这里三角面互相穿插，用深色描边会糊成一团乱麻，
    # 对比度留给下面两条粗边界弧。
    for k, (_, pts) in enumerate(tris):
        ax1.add_patch(Polygon(pts, facecolor=(C1 if k % 2 == 0 else C2),
                              edgecolor=EDGE_SOFT, lw=0.7, zorder=2))
    # 两条边界单独描粗：一上一下（红色那条翻到了圆心下方），和 图 4-1 第三格对得上
    ax1.plot(P[:, 0], P[:, 1], color=EDGE, lw=2.4, zorder=4, solid_capstyle="round")
    ax1.plot(Q[:, 0], Q[:, 1], color=C_RED, lw=2.4, zorder=4, solid_capstyle="round")
    # 叠压区只淡淡铺一层，细节交给右图，避免左图糊成一片
    if ov_union is not None:
        for g in getattr(ov_union, "geoms", [ov_union]):
            xy = np.asarray(g.exterior.coords)
            ax1.add_patch(Polygon(xy, facecolor=OV_FILL, alpha=0.15,
                                  edgecolor=OV, lw=0.7, zorder=3))

    # 翻转后一段的跨度几乎等于整条带（内弧被翻到圆心另一侧），圈不出"某一小段"，
    # 所以不画定位圈，左右两图各说一件事，对应关系交给图注。
    ax1.set_aspect("equal")
    ax1.set_xlim(-74, 74)
    ax1.set_ylim(-46, 70)
    ax1.axis("off")
    ax1.text(0.5, -0.05, "沿弧长分段，每段切 2 个三角面",
             transform=ax1.transAxes, ha="center", va="center",
             fontsize=13.5, color=INK)

    # ---------- 右：其中一段，四边形 → 两个三角面 ----------
    quad = np.vstack([P[0], P[1], Q[1], Q[0]])
    t1 = np.vstack([P[0], P[1], Q[1]])
    t2 = np.vstack([P[0], Q[1], Q[0]])

    # 两片用不同色系，否则读者看不出这里是"两片"叠着，只当一片被切成两半
    ax2.add_patch(Polygon(t2, facecolor=F_ORANGE, edgecolor=EDGE_O, lw=1.1, zorder=2))
    ax2.add_patch(Polygon(t1, facecolor=C1, edgecolor=EDGE, lw=1.1, zorder=3))

    ov = SPoly(t1).intersection(SPoly(t2))
    for g in getattr(ov, "geoms", [ov]):
        if g.area <= 1e-9:
            continue
        xy = np.asarray(g.exterior.coords)
        ax2.add_patch(Polygon(xy, facecolor=OV_FILL, alpha=0.9, edgecolor=OV,
                              lw=1.0, hatch="////", zorder=5))

    ax2.scatter(quad[:, 0], quad[:, 1], s=30, color=INK, zorder=7)

    xmin, xmax = quad[:, 0].min(), quad[:, 0].max()
    ymin, ymax = quad[:, 1].min(), quad[:, 1].max()
    pad_x, pad_y = (xmax - xmin) * 0.12, (ymax - ymin) * 0.16
    ax2.set_aspect("equal")
    ax2.set_xlim(xmin - pad_x, xmax + pad_x)
    ax2.set_ylim(ymin - pad_y, ymax + pad_y)
    ax2.axis("off")

    cx, cy = np.asarray(ov.representative_point().coords)[0]
    ax2.annotate("两片面片重叠", xy=(cx, cy), xytext=(xmax + pad_x * 0.2, ymax - 10),
                 fontsize=12, color=OV, ha="right", va="center",
                 arrowprops=dict(arrowstyle="->", color=OV, lw=1.1,
                                 shrinkA=4, shrinkB=4))
    ax2.text(0.5, -0.05, "取其中一段：切成两片，仍然叠压",
             transform=ax2.transAxes, ha="center", va="center",
             fontsize=13.5, color=INK)

    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def fig_width_taper(path):
    """图 6-1：1 µm 内宽度归零的曲线形状 + 系数随区间长度的爆炸。

    归一化形式 w = a·(1 - 3u² + 2u³)（u = ds/L），就是图形学里的 smoothstep：
    u=0 时 w=a、斜率 0；u=1 时 w=0、斜率 0。系数由两个边界条件唯一确定：
        c = -3a/L²,  d = 2a/L³
    形状与 L 无关，但系数按 L 的三次方倒数涨 —— 这就是 road 575 那个 1e18 的来源。
    """
    a = 3.5

    def coef(L):
        return -3.0 * a / L ** 2, 2.0 * a / L ** 3

    def wide(ds, L):
        u = ds / L
        return a * (1.0 - 3.0 * u ** 2 + 2.0 * u ** 3)

    fig, axes = plt.subplots(1, 2, figsize=(11.4, 4.2), dpi=160,
                             gridspec_kw=dict(width_ratios=[1.15, 1.0]))

    ax = axes[0]
    L = 1e-6
    ds = np.linspace(0.0, L, 500)
    ax.plot(ds * 1e6, wide(ds, L), color="#E24B4A", lw=2.4, zorder=3)
    ax.axhline(0, color=GRAY_HINT, lw=0.8)
    ax.set_xlim(-0.03, 1.03)
    ax.set_ylim(-0.2, 3.85)
    ax.set_xlabel("ds：距 laneSection 起点的距离（微米）", fontsize=9.5)
    ax.set_ylabel("车道宽 w（米）", fontsize=9.5)
    ax.set_title("a  lane −2 的宽度：1 微米内从 3.5 m 收到 0", fontsize=10)
    for u, txt, dx, dy in ((0.0, "3.5 m", 0.07, 0.08), (0.5, "1.75 m", 0.02, 0.3),
                           (1.0, "≈0", -0.2, 0.3)):
        ax.plot(u, wide(u * L, L), "o", color="#185FA5", ms=4.5, zorder=4)
        ax.annotate(txt, (u, wide(u * L, L)), xytext=(u + dx, wide(u * L, L) + dy),
                    fontsize=8.5, color=INK)
    ax.annotate("两端斜率都是 0\n（与相邻段平滑衔接，不留折角）",
                xy=(0.96, 0.16), xytext=(0.24, 1.0), fontsize=8.5, color=GRAY_TEXT,
                arrowprops=dict(arrowstyle="->", color=GRAY_TEXT, lw=0.8))
    ax.grid(alpha=0.2, ls=":")

    ax = axes[1]
    c30, d30 = coef(30.0)
    c1, d1 = coef(1e-6)
    vals = [abs(c30), abs(d30), abs(c1), abs(d1)]
    ax.bar(range(4), vals, color=[GRAY_HINT, GRAY_HINT, "#E24B4A", "#E24B4A"], width=0.6)
    ax.set_yscale("log")
    ax.set_ylim(1e-6, 1e21)
    ax.set_xticks([0.5, 2.5])
    ax.set_xticklabels(["L = 30 m\n正常渐变", "L = 1 µm\n这段渐灭"], fontsize=9)
    ax.set_yticks([1e-6, 1e-2, 1e2, 1e6, 1e10, 1e14, 1e18])
    ax.set_ylabel("系数的绝对值（对数轴）", fontsize=9.5)
    ax.set_title("b  同样的 S 曲线，系数差 22 个数量级", fontsize=10)
    for i, (v, nm) in enumerate(zip(vals, ["c", "d", "c", "d"])):
        ax.annotate("%s = %.1e" % (nm, v), (i, v), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=8, color=INK)
    ax.grid(alpha=0.2, ls=":", axis="y")

    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor="white")


def main():
    ensure(FIG_DIR)
    targets = [
        (fig_offset_tristate, "offset-vs-radius.png"),
        (fig_triangulation, "lane-triangulation.png"),
        (fig_width_taper, "width-taper.png"),
    ]
    for fn, name in targets:
        p = os.path.join(FIG_DIR, name)
        fn(p)
        print(f"  {os.path.relpath(p, DOCS)}  {os.path.getsize(p) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
