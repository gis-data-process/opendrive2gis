#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
preview.py —— 把转换结果画成预览图, 肉眼确认路网几何是否正常。
用法（在项目根目录执行）:
    python code/own/preview.py out/Town03.gpkg
    python code/own/preview.py --all        # 画 out/ 下全部 Town*.gpkg
"""
import os
import sys
import argparse
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
# Windows 下中文字体, 否则标题里的中文会变成方框
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.paths import OUT  # noqa: E402  项目根/out，与 cwd 无关
from matplotlib.patches import Patch
import geopandas as gpd
import pyogrio


def plot(gpkg, png=None):
    """读取转换图层并画平面预览；文件不含 Lane 时不能生成车道图。"""
    base = os.path.splitext(os.path.basename(gpkg))[0]
    lanes = gpd.read_file(gpkg, layer="Lane")
    ref = gpd.read_file(gpkg, layer="ReferenceLine")
    names = {l[0] for l in pyogrio.list_layers(gpkg)}
    sig = gpd.read_file(gpkg, layer="RoadSignal") if "RoadSignal" in names else None
    obj = gpd.read_file(gpkg, layer="RoadObject") if "RoadObject" in names else None

    fig, ax = plt.subplots(figsize=(11, 11))
    ax.set_facecolor("#f7f7f5")

    # 车道面按类型上色
    palette = {"driving": "#cfd8dc", "shoulder": "#e8dcc4", "sidewalk": "#d6e4d0",
               "parking": "#e2d3e8", "biking": "#d6e9ec", "restricted": "#efe0e0",
               "none": "#dfe3e6", "curb": "#e6e0d4", "median": "#dde6dd",
               "entry": "#e7d4d4", "exit": "#d4dee7", "bidirectional": "#cfd8dc"}
    for t, grp in lanes.groupby("lane_type"):
        if len(grp):
            grp.plot(ax=ax, color=palette.get(t, "#eceff1"),
                     edgecolor="#9aa5ad", linewidth=0.25, label=t)
    if obj is not None and len(obj):
        obj.plot(ax=ax, color="#f39c12", markersize=5, label="RoadObject")
    if sig is not None and len(sig):
        sig.plot(ax=ax, color="#e74c3c", markersize=6, label="RoadSignal")

    handles = [Patch(facecolor=palette.get(t, "#eceff1"), edgecolor="#9aa5ad",
                     label="%s  (%d)" % (t, int((lanes.lane_type == t).sum())))
               for t in sorted(lanes.lane_type.unique())]
    if sig is not None and len(sig):
        handles.append(plt.Line2D([], [], marker="o", color="w", markerfacecolor="#e74c3c",
                                  markersize=7, label="RoadSignal (%d)" % len(sig)))
    if obj is not None and len(obj):
        handles.append(plt.Line2D([], [], marker="o", color="w", markerfacecolor="#f39c12",
                                  markersize=6, label="RoadObject (%d)" % len(obj)))

    ax.legend(handles=handles, loc="upper right", fontsize=8, framealpha=0.9, ncol=2)
    ax.set_title("%s  —  %d roads / %d lanes   CRS: %s"
                 % (base, len(ref), len(lanes), lanes.crs.name if lanes.crs else "none"),
                 fontsize=12)
    ax.set_xlabel("X (m)"); ax.set_ylabel("Y (m)")
    ax.set_aspect("equal")
    out = png or os.path.join(os.path.dirname(gpkg), base + "_preview.png")
    Path(out).resolve().parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print("  -> %s" % out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("gpkg", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dir", default=None)
    args = ap.parse_args()
    if args.all:
        files = []
        for p in sorted(Path(args.dir or OUT).glob("*.gpkg")):
            if {"Lane", "ReferenceLine"} <= {name for name, _ in pyogrio.list_layers(p)}:
                files.append(p)
    elif args.gpkg:
        files = [args.gpkg]
    else:
        ap.error("指定 GPKG 或 --all")
    if not files:
        ap.error("没有可预览的转换产物")
    for path in files:
        plot(path)


if __name__ == "__main__":
    main()
