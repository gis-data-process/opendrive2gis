#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
tin2poly.py —— 把官方 GDAL XODR 驱动输出的 TIN 图层，转成 QGIS 里能正常用的真多边形。

为什么需要它
    官方驱动把 Lane / RoadMark / RoadSignal / RoadObject 全部输出成 TIN(三角网)。
    本机 pyogrio 读 TIN 会抛 "Geometry type is not supported: 1016"；就算 QGIS 能读，
    也会把每个三角面的边都描一遍，车道面看起来就是一堆杂乱三角网。
    本脚本直接解析 GeoPackage 里的 TIN 二进制，把同一个要素的三角面并成整块多边形。

用法（都在项目根目录执行）
    python code/gdal/tin2poly.py out_gdal/Town03.gpkg Lane
    python code/gdal/tin2poly.py out_gdal/Town03.gpkg Lane RoadMark
    python code/gdal/tin2poly.py out_gdal/Town03.gpkg --all          # 6 层全搬，跟官方一一对应
    python code/gdal/tin2poly.py out_gdal/Town03.gpkg Lane -o out_gdal/Town03_lane.gpkg
    python code/gdal/tin2poly.py --batch --all --dir out_gdal     # 批量整理原始 Town 文件

    --all 选择文件内全部图层；--batch 才选择目录中的多个原始文件。
    图层名是位置参数，默认输出同目录 _poly；批量不接受单个 -o。

输出
    默认写到 <输入目录>/<输入名>_poly.gpkg，属性列原样保留。
    几何降为 2D（三角网并集带 Z 容易出无效几何，而车道面在 QGIS 里主要是看平面形状）。

    --all 会处理官方驱动的全部图层：TIN 层（Lane / RoadMark / RoadObject / RoadSignal）
    转成 Polygon，其余层（ReferenceLine / LaneBorder 本来就是 LINESTRING）原样搬过去，
    整理版保留原始属性和 fid，但线也会降为 2D。需要三维几何时读取原始文件。
"""
import argparse
import os
import re
import sqlite3
import struct
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import CRS
from shapely import wkb as shapely_wkb
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely import force_2d

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.files import atomic_output  # noqa: E402
from common.paths import OUT_GDAL  # noqa: E402

# TIN 的 WKB 类型号：16/1016 = TIN / TIN Z；patch 3/17/1003/1017 = Polygon / Triangle 及其 Z 变体
TIN_TYPES = (16, 1016)
PATCH_TYPES = (3, 17, 1003, 1017)
# GPKG blob 头 8 字节之后是 envelope，长度由 flags 的 bit1~3 决定
ENV_WIDTH = [0, 32, 48, 48, 64]


def parse_tin(blob):
    """把一条 GPKG 几何二进制解成 [三角形顶点列表, ...]，只取 x/y。"""
    body = wkb_body(blob)
    off = 0

    bo = "<" if body[off] == 1 else ">"
    off += 1
    gtype, = struct.unpack(bo + "I", body[off:off + 4])
    off += 4
    if gtype not in TIN_TYPES:
        raise ValueError("不是 TIN 几何（type=%d），请用普通图层" % gtype)
    ncoord = 3 if gtype >= 1000 else 2   # Z 变体每点 3 个 double，读少了后面全错位

    npat, = struct.unpack(bo + "I", body[off:off + 4])
    off += 4

    tris = []
    for _ in range(npat):
        pbo = "<" if body[off] == 1 else ">"
        off += 1
        ptype, = struct.unpack(pbo + "I", body[off:off + 4])
        off += 4
        if ptype not in PATCH_TYPES:
            raise ValueError("未知的 TIN patch 类型 %d" % ptype)
        nring, = struct.unpack(pbo + "I", body[off:off + 4])
        off += 4
        rings = []
        for _ in range(nring):
            npt, = struct.unpack(pbo + "I", body[off:off + 4])
            off += 4
            pts = []
            for _ in range(npt):
                vals = struct.unpack(pbo + "d" * ncoord, body[off:off + 8 * ncoord])
                off += 8 * ncoord
                pts.append((vals[0], vals[1]))
            rings.append(pts)
        if rings:
            tris.append(rings[0])
    return tris


def wkb_body(blob):
    """剥掉 GPKG 的 8 字节头 + envelope，返回纯 WKB（非 TIN 层原样搬运时用）。"""
    if blob is None or len(blob) < 8 or blob[:2] != b"GP":
        raise ValueError("不是有效的 GeoPackage 几何")
    envelope = (blob[3] >> 1) & 7
    if envelope >= len(ENV_WIDTH):
        raise ValueError("不支持的 GeoPackage envelope")
    body = blob[8 + ENV_WIDTH[envelope]:]
    if len(body) < 5:
        raise ValueError("GeoPackage WKB 不完整")
    return body


def layer_rows(con, layer):
    """从 GPKG 元数据取得真实几何列名；属性列含 fid，按原值保留。"""
    metadata = con.execute(
        "select column_name,srs_id from gpkg_geometry_columns where table_name=?", (layer,)
    ).fetchone()
    if metadata is None:
        raise ValueError(f"源文件没有图层: {layer}")
    geom_column, srs_id = metadata

    def quoted(name):
        return '"' + name.replace('"', '""') + '"'

    cols = [r[1] for r in con.execute("PRAGMA table_info(" + quoted(layer) + ")")]
    attrs = [c for c in cols if c != geom_column]
    query = "select " + ",".join(quoted(c) for c in attrs + [geom_column]) + " from " + quoted(layer)
    return attrs, con.execute(query).fetchall(), get_crs(con, srs_id)


# 单条车道边界超过 1000 km 必是官方驱动的病态产物（Town03 road 575 那条 5.77e11 m）。
# 留着会让 QGIS「缩放至图层」飞出地球，搬运时丢掉并计数。
BAD_LEN = 1e6


def copy_plain(con, layer):
    """复制普通几何为 2D；保留 fid/属性，病态长线置 NULL 并计数。"""
    attr_cols, rows, crs = layer_rows(con, layer)
    geoms, bad = [], 0
    for r in rows:
        blob = r[-1]
        if blob is None:
            geoms.append(None)
            continue
        try:
            g = shapely_wkb.loads(wkb_body(blob))
        except Exception:
            geoms.append(None)
            bad += 1
            continue
        if g is not None and not g.is_empty and g.length > BAD_LEN:
            geoms.append(None)
            bad += 1
            continue
        geoms.append(force_2d(g))
    df = pd.DataFrame([r[:-1] for r in rows], columns=attr_cols)
    return gpd.GeoDataFrame(df, geometry=geoms, crs=crs), bad


def list_layers(con):
    return [(t, gt) for t, gt in con.execute(
        "select table_name, geometry_type_name from gpkg_geometry_columns")]


def get_crs(con, srs_id):
    row = con.execute(
        "select definition from gpkg_spatial_ref_sys where srs_id=?", (srs_id,)
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return CRS.from_wkt(row[0])
    except Exception:
        return None


def convert(con, layer):
    """逐要素融合 TIN；源 NULL 保留 NULL，解析错误计数并保留属性占位。"""
    attr_cols, rows, crs = layer_rows(con, layer)

    geoms, bad = [], 0
    for r in rows:
        blob = r[-1]
        if blob is None:
            geoms.append(None)
            continue
        try:
            triangles = parse_tin(blob)
        except (ValueError, IndexError, struct.error):
            geoms.append(None)
            bad += 1
            continue
        polys = []
        for t in triangles:
            if len(t) < 3:
                continue
            # 退化三角形（重复点 / 零面积）会让 union_all 抛
            # "Scale factor must be non-zero"，必须在合并前剔掉
            ring = [t[0]]
            for p in t[1:]:
                if p != ring[-1]:
                    ring.append(p)
            if len(ring) < 3:
                continue
            poly = Polygon(ring)
            # 注意：invalid 的三角形要直接丢掉，别 buffer(0) 修补后再塞进 union_all，
            # 那样反而会触发 GEOS "Scale factor must be non-zero"
            if not poly.is_valid or poly.is_empty or poly.area <= 0:
                continue
            polys.append(poly)

        if not polys:
            geoms.append(Polygon())
            continue
        try:
            g = unary_union(polys)
        except Exception:
            # 官方驱动偶尔会吐病态要素（Town03 RoadMark fid 5060：14,280 个三角面、
            # 坐标量级 3.35e7 m，与那条 5.77e11 m 的 LaneBorder 同源），GEOS 直接抛错。
            # 这类要素没有可用几何，置空并计数。
            geoms.append(Polygon())
            bad += 1
            continue
        if not g.is_valid and not g.is_empty:
            try:
                g = g.buffer(0)
            except Exception:
                pass
        geoms.append(g)

    df = pd.DataFrame([r[:-1] for r in rows], columns=attr_cols)
    gdf = gpd.GeoDataFrame(df, geometry=geoms, crs=crs)
    return gdf, bad


def convert_file(src, layers=None, all_layers=False, out=None):
    """整理单个 GPKG，按所选层生成同目录 _poly 文件；失败保留已有输出。

    all_layers 表示复制全部空间层，layers 表示指定层名。两者只能选择一种；
    普通层也降为二维，属性和 fid 保留。此函数不调用 Docker，也不合并中心线。
    """
    source = Path(src)
    out = out or source.with_name(source.stem + "_poly.gpkg")
    if not source.is_file():
        raise ValueError(f"源文件不存在: {source}")
    if source.resolve() == Path(out).resolve():
        raise ValueError("输出不能覆盖源文件")
    if all_layers and layers:
        raise ValueError("--all 不能同时指定图层名")
    con = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        known = dict(list_layers(con))
        if all_layers:
            wanted = list(known.items())
        elif layers:
            if any(t not in known for t in layers):
                raise ValueError("图层不存在: " + ",".join(t for t in layers if t not in known))
            wanted = [(t, known[t]) for t in dict.fromkeys(layers)]
        else:
            raise ValueError("给图层名，或用 --all 处理全部")
        if not wanted:
            raise ValueError("源文件没有几何图层")
        with atomic_output(out) as temporary:
            for layer, gtype in wanted:
                gdf, bad = convert(con, layer) if gtype == "TIN" else copy_plain(con, layer)
                gdf.to_file(temporary, layer=layer, driver="GPKG", mode="w")
                print(f"{layer:14s} {len(gdf):6d} 要素  {gtype}  异常 {bad} / 空 {int(gdf.geometry.is_empty.sum())} / NULL {int(gdf.geometry.isna().sum())}")
        print(f"输出: {out}")
    finally:
        con.close()
    return Path(out)


def main():
    ap = argparse.ArgumentParser(description="将官方 GPKG 的 TIN 整理为二维面")
    ap.add_argument("src", nargs="?", help="单个输入 GPKG")
    ap.add_argument("layers", nargs="*", help="要转的图层名；或用 --all 处理全部")
    ap.add_argument("-a", "--all", action="store_true", help="处理文件内全部图层")
    ap.add_argument("-o", "--out", default=None, help="单文件输出，默认同目录 <输入名>_poly.gpkg")
    ap.add_argument("--batch", action="store_true", help="配合 --all，批量处理目录中的 Town 原始 GPKG")
    ap.add_argument("--dir", default=None, help="批量输入目录，默认 out_gdal")
    a = ap.parse_args()
    if a.batch:
        if a.src or a.layers or a.out:
            ap.error("--batch 不能同时指定单个输入、图层名或 -o")
        if not a.all:
            ap.error("批量整理请同时指定 --all，复制每个文件的全部图层")
        files = [p for p in sorted(Path(a.dir or OUT_GDAL).glob("Town*.gpkg"))
                 if re.fullmatch(r"Town\d{2}[A-Z]*", p.stem)]
        if not files:
            ap.error("批量目录中没有 Town 原始 GPKG")
    elif a.src:
        if a.dir:
            ap.error("--dir 只用于 --batch；单文件输出请用 -o")
        files = [Path(a.src)]
    else:
        ap.error("指定输入 GPKG，或使用 --batch --all")
    failed = False
    for src in files:
        try:
            convert_file(src, a.layers, a.all, a.out)
        except (ValueError, OSError, sqlite3.Error) as exc:
            print(f"[FAIL] {src.name}: {exc}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
