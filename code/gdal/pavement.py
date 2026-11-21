#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 Lane 面合成为二维展示路面：并集后执行 buffer(r).buffer(-r)。

闭运算会改变细小空隙和岛体，适用于可视化，不能作为车道拓扑或三维曲面。
半径使用源坐标单位；投影坐标为米时，默认 r=0.2 即 0.2 米。
"""
import argparse
import glob
import math
import os
from pathlib import Path
import sys

import geopandas as gpd
import numpy as np
from shapely import force_2d, get_coordinates, make_valid
from shapely.ops import unary_union

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.files import atomic_output  # noqa: E402


def _polygons(geom):
    """保留面部件；make_valid 的线状退化结果不作为路面。"""
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [part for child in getattr(geom, "geoms", []) for part in _polygons(child)]


def build(src, layer="Lane", r=0.2):
    """读取、修复并融合车道面，拒绝空输入和角度坐标，返回 (图层, 并集, 闭运算)。"""
    if not math.isfinite(r) or r < 0:
        raise ValueError("半径必须为有限非负数")
    frame = gpd.read_file(src, layer=layer)
    if frame.crs is not None and frame.crs.is_geographic:
        raise ValueError("路面闭运算需要平面坐标，请先投影")
    geometries = []
    for geom in frame.geometry:
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type not in ("Polygon", "MultiPolygon"):
            raise ValueError(f"{layer} 包含非面几何")
        if not np.isfinite(get_coordinates(geom)).all():
            raise ValueError("路面坐标含 NaN/Inf")
        planar = force_2d(geom)
        geometries.extend(_polygons(make_valid(planar) if not planar.is_valid else planar))
    if not geometries:
        raise ValueError("输入没有可用的车道面")
    union = unary_union(geometries)
    closed = union.buffer(r, join_style=2).buffer(-r, join_style=2) if r else union
    parts = _polygons(closed)
    if not parts or union.area <= 0:
        raise ValueError("未得到有面积的路面")
    out = gpd.GeoDataFrame({"src": [Path(src).name] * len(parts),
                            "parts": [len(parts)] * len(parts),
                            "area": [part.area for part in parts]}, geometry=parts, crs=frame.crs)
    return out, union, closed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("srcs", nargs="+")
    ap.add_argument("-r", "--radius", type=float, default=0.2)
    ap.add_argument("-l", "--layer", default="Lane")
    ap.add_argument("--dir", default=None)
    args = ap.parse_args()
    files = []
    for src in args.srcs:
        files.extend(sorted(glob.glob(src)) if any(c in src for c in "*?[") else [src])
    if not files:
        ap.error("没有匹配的输入文件")
    failed = False
    for src in dict.fromkeys(files):
        try:
            out, union, closed = build(src, args.layer, args.radius)
            dst = Path(args.dir or Path(src).parent) / (Path(src).stem + "_pave.gpkg")
            with atomic_output(dst) as temporary:
                out.to_file(temporary, layer="Pavement", driver="GPKG")
            print(f"{Path(src).stem}: 面积 {union.area:.1f} -> {closed.area:.1f}，变化 {(closed.area / union.area - 1) * 100:+.2f}%，{len(out)} 块 -> {dst}")
        except (ValueError, OSError) as exc:
            print(f"[FAIL] {src}: {exc}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
