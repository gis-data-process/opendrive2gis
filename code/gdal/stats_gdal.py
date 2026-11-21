#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
stats_gdal.py —— 在 GDAL 容器里统计 gpkg 各图层的要素数 / 总长度 / 总面积。

为什么必须在容器里跑:
    官方 XODR 驱动把 Lane / RoadMark / RoadObject / RoadSignal 输出成 TIN 几何,
    写入时注册了非标准扩展 gpkg_geom_TIN, 本机 pyogrio 读这些层会直接抛错。
    TIN 的 GetArea() 也不返回有效值, 必须逐个三角形求和。

用法(容器内):
    python3 stats_gdal.py <目录> <输出json>
"""
import glob
import json
import math
import os
import sys

from osgeo import ogr, gdal

gdal.UseExceptions()
ogr.UseExceptions()

# 单条线长度超过这个值就判定为病态几何(多项式越界求值炸出来的)
OUTLIER_LEN = 100_000.0


def _try(g, names):
    """GDAL 3.14 的 python binding 把 GetLength()/GetArea() 改成了 Length()/Area(),
    不同镜像版本名字不一样, 这里逐个试, 全失败返回 None。"""
    if g is None:
        return None
    for n in names:
        a = getattr(g, n, None)
        if a is None:
            continue
        try:
            v = a() if callable(a) else a
            return float(v)
        except Exception:
            continue
    return None


def area_of(g):
    """TIN 要拆成三角形逐个求和; 普通 Polygon / MultiPolygon 直接取面积"""
    if g is None:
        return 0.0
    name = g.GetGeometryName()
    if name.startswith("TIN"):
        total = 0.0
        for i in range(g.GetGeometryCount()):
            sub = g.GetGeometryRef(i)
            if sub is not None:
                total += area_of(sub)
        return total
    if name.startswith("MULTI") or name.startswith("GEOMETRYCOLLECTION"):
        total = 0.0
        for i in range(g.GetGeometryCount()):
            sub = g.GetGeometryRef(i)
            if sub is not None:
                total += area_of(sub)
        return total
    # 非面几何(Point/LineString)的 Area() 会告警并返回 0, 这是正常的
    v = _try(g, ("GetArea", "Area", "area"))
    return v if v is not None else 0.0


def _walk_points(g):
    """迭代出几何里所有点的 (x, y, z)"""
    if g is None:
        return
    n = g.GetGeometryName()
    if n in ("POINT", "POINTZ", "POINT25D"):
        p = g.GetPoint_2D() if g.GetCoordinateDimension() == 2 else g.GetPoint()
        yield (p[0], p[1], p[2] if len(p) > 2 else 0.0)
        return
    for i in range(g.GetGeometryCount()):
        sub = g.GetGeometryRef(i)
        if sub is not None:
            for pt in _walk_points(sub):
                yield pt
    # LineString / LinearRing 的点不在 GetGeometryCount() 里
    if g.GetGeometryCount() == 0 and g.GetPointCount() > 0:
        dim = g.GetCoordinateDimension()
        for i in range(g.GetPointCount()):
            p = g.GetPoint_2D(i) if dim == 2 else g.GetPoint(i)
            yield (p[0], p[1], p[2] if len(p) > 2 else 0.0)


def length_of(g):
    if g is None:
        return 0.0
    name = g.GetGeometryName()
    if name.startswith("TIN"):
        return 0.0
    v = _try(g, ("Length", "GetLength", "length"))
    if v is not None:
        return v
    # 兜底: 手工累加折线长度
    total = 0.0
    for geom in ([g] if g.GetGeometryCount() == 0
                 else [g.GetGeometryRef(i) for i in range(g.GetGeometryCount())]):
        pts = list(_walk_points(geom))
        for a, b in zip(pts, pts[1:]):
            total += math.hypot(b[0] - a[0], b[1] - a[1])
    return total


def scan(path):
    ds = ogr.Open(path, 0)
    if ds is None:
        return None
    out = {}
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayerByIndex(i)
        name = lyr.GetName()
        n = lyr.GetFeatureCount()
        total_len = 0.0
        total_area = 0.0
        clean_len = 0.0
        n_outlier = 0
        max_len = 0.0
        gtype = ""
        zmin, zmax = None, None
        lyr.ResetReading()
        for feat in lyr:
            g = feat.GetGeometryRef()
            if g is None:
                continue
            if not gtype:
                gtype = g.GetGeometryName()
            L = length_of(g)
            total_len += L
            total_area += area_of(g)
            # 病态几何判定: 单条线超过 100 km。CARLA 单条路最长不过 1 km,
            # 超过这个量级一定是多项式越界求值炸出来的(如 Town03 road 575)
            if L > OUTLIER_LEN:
                n_outlier += 1
            else:
                clean_len += L
            max_len = max(max_len, L)
            # Z 范围: 手工遍历, 避开 GetEnvelope3D 在各版本间的差异
            for _, _, z in _walk_points(g):
                if z is not None:
                    zmin = z if zmin is None else min(zmin, z)
                    zmax = z if zmax is None else max(zmax, z)
        out[name] = {
            "count": n,
            "geom_type": gtype,
            "total_length": total_len,
            "total_length_clean": clean_len,
            "length_outliers": n_outlier,
            "max_length": max_len,
            "total_area": total_area,
            "z_min": zmin,
            "z_max": zmax,
        }
    ds = None
    return out


def main():
    d = sys.argv[1]
    out_json = sys.argv[2]
    result = {}
    for p in sorted(glob.glob(os.path.join(d, "*.gpkg"))):
        key = os.path.splitext(os.path.basename(p))[0]
        result[key] = scan(p)
        print("  %-12s %s" % (key, {k: v["count"] for k, v in (result[key] or {}).items()}))
    with open(out_json, "w") as f:
        json.dump(result, f, indent=1)
    print("-> %s" % out_json)


if __name__ == "__main__":
    main()
