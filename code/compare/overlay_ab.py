#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
overlay_ab.py —— 把官方 GDAL 与自研的 LaneBorder 叠加画图，检查二维局部差异。

为什么只画 LaneBorder:
    官方把 Lane/RoadMark/RoadObject 输出成 TIN, 本机 pyogrio 读不了(GeometryError 1016)。
    LaneBorder 是普通线，适合目视叠加；线宽遮盖只能辅助比较，不能证明几何等价。

用法（在项目根目录执行）:
    python code/compare/overlay_ab.py
"""
import os
import sys
import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False
import matplotlib.pyplot as plt
import geopandas as gpd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.paths import OUT, OUT_GDAL  # noqa: E402

GDAL_DIR = OUT_GDAL
OWN_DIR = OUT


def load(name):
    """返回 (官方真实车道边界, 自研车道边界)。

    官方 LaneBorder 里 ID=0 的是 center lane 边界(每条 road 一条), 自研不输出, 要过滤掉。
    (不能用 Type=='none' 过滤: 部分真实车道的 Type 也是 none。)
    长度 > 100 km 的是病态线, 也要剔除, 否则画图时坐标直接爆掉。
    """
    g = gpd.read_file(os.path.join(GDAL_DIR, name + ".gpkg"), layer="LaneBorder")
    g = g[g["ID"] != 0]                              # 去掉 center lane 边界
    g = g[g.geometry.notna() & (g.geometry.length < 1e5)]
    o = gpd.read_file(os.path.join(OWN_DIR, name + ".gpkg"), layer="LaneBorder")
    if g.crs is None or o.crs is None or not g.crs.equals(o.crs, ignore_axis_order=True):
        raise ValueError("叠加图要求两套数据具有相同的已知 CRS")
    return g, o


def draw(ax, name, zoom=None, title=""):
    g, o = load(name)
    # 官方画粗一点放底层(蓝), 自研画细一点放上层(红), 重合处会呈现紫/红覆盖蓝
    g.plot(ax=ax, color="#1f77b4", linewidth=2.4, alpha=0.85)
    o.plot(ax=ax, color="#d62728", linewidth=0.9, alpha=0.95)
    ax.set_facecolor("#fafafa")
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_aspect("equal")
    if zoom:
        ax.set_xlim(zoom[0], zoom[1]); ax.set_ylim(zoom[2], zoom[3])
    if title:
        ax.set_title(title, fontsize=10)
    return len(g), len(o)


def main():
    fig, axes = plt.subplots(2, 2, figsize=(14, 13))

    # Town01: 全图 + 局部放大
    n1 = draw(axes[0][0], "Town01", title="Town01 全图叠加")
    g, o = load("Town01")
    x0, y0, x1, y1 = g.total_bounds
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    draw(axes[0][1], "Town01", zoom=(cx - 45, cx + 45, cy - 45, cy + 45),
         title="Town01 局部放大 (±45 m)")

    # Town03: 全图 + 局部放大
    n3 = draw(axes[1][0], "Town03", title="Town03 全图叠加")
    g3, o3 = load("Town03")
    x0, y0, x1, y1 = g3.total_bounds
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    draw(axes[1][1], "Town03", zoom=(cx - 45, cx + 45, cy - 45, cy + 45),
         title="Town03 局部放大 (±45 m)")

    handles = [
        plt.Line2D([], [], color="#1f77b4", linewidth=4, label="官方 GDAL XODR 驱动（底层，粗）"),
        plt.Line2D([], [], color="#d62728", linewidth=1.5, label="自研 xodr2gpkg.py（上层，细）"),
    ]
    fig.suptitle("车道边界二维叠加对比（线宽遮盖不代表几何完全一致）", y=0.985, fontsize=13)
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.96),
               ncol=2, fontsize=11, framealpha=0.95)
    fig.text(0.5, 0.91, f"Town01：官方 {n1[0]} / 自研 {n1[1]} 条　｜　Town03：官方 {n3[0]} / 自研 {n3[1]} 条",
             ha="center", fontsize=11)
    plt.tight_layout(rect=(0, 0, 1, 0.90))
    out = os.path.join(OUT, "AB_overlay.png")
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print("-> %s" % out)
    print("Town01: 官方 %d 条 / 自研 %d 条" % n1)
    print("Town03: 官方 %d 条 / 自研 %d 条" % n3)


if __name__ == "__main__":
    main()
