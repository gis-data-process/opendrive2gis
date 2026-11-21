#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""读取两套现有 GPKG，重新计算 A/B 指标并生成报告；不复用写死的历史结论。

默认比较 data/Town*.xodr 对应的 out/ 和 out_gdal/ 产物。
官方 TIN 在本机按三角形解析：面积是三角形平面面积之和，不是跨要素并集。
"""
import argparse
from datetime import datetime, timezone, timedelta
import json
import math
import os
from pathlib import Path
import sqlite3
import sys

import numpy as np
import pyogrio
from shapely.geometry import Polygon
from shapely import get_coordinates

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.files import atomic_output  # noqa: E402
from common.paths import DATA, ROOT, OUT, OUT_GDAL  # noqa: E402
from gdal.tin2poly import parse_tin  # noqa: E402

# build 可接受隔离统计输入，供回归用例使用；导入模块不读取文件、不写产物。
OWN = {}
GDAL = {}
TOWNS = []
OUTLIER_LEN = 100_000.0


def rel(value, baseline):
    """以官方值为基准计算百分比；零基准返回 None。"""
    return (value - baseline) / baseline * 100 if baseline else None


def fmt_pct(value, tolerance=.5):
    """总量偏差的报告标记；阈值仅用于二维统计，不表示几何等价。"""
    if value is None:
        return "—"
    mark = "✅" if abs(value) <= tolerance else "❌"
    return f"{value:+.4f}% {mark}"


def scan(path):
    """同一口径统计普通几何和 TIN，保留 NULL/空面及病态线的计数。

    TIN 的 total_area 为逐三角形面积之和；Polygon 为二维轮廓面积。
    未读取和统计失败必须显式报错，不能按缺失图层悄悄跳过。
    """
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f"产物缺失: {path}")
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    result = {}
    try:
        metadata = con.execute("select table_name,column_name,geometry_type_name from gpkg_geometry_columns").fetchall()
        for name, column, kind in metadata:
            if kind == "TIN":
                def quoted(value):
                    return '"' + value.replace('"', '""') + '"'
                blobs = con.execute(f"select {quoted(column)} from {quoted(name)}").fetchall()
                area = 0.0
                null = empty = bad = 0
                for (blob,) in blobs:
                    if blob is None:
                        null += 1
                        continue
                    triangles = parse_tin(blob)
                    subtotal = sum(Polygon(points).area for points in triangles if len(points) >= 3)
                    if not math.isfinite(subtotal):
                        bad += 1
                    else:
                        area += subtotal
                    empty += int(subtotal == 0)
                result[name] = dict(count=len(blobs), geom_type="TIN", total_length=0.0,
                                    total_length_clean=0.0, total_area=area, length_outliers=0,
                                    null=null, empty=empty, invalid=bad, z_min=None, z_max=None)
            else:
                frame = pyogrio.read_dataframe(path, layer=name)
                lengths = frame.geometry.length.to_numpy()
                finite_lengths = lengths[np.isfinite(lengths)]
                coords = get_coordinates(frame.geometry.to_numpy(), include_z=True)
                z = coords[:, 2] if len(coords) else np.array([])
                z = z[np.isfinite(z)]
                result[name] = dict(
                    count=len(frame), geom_type=kind,
                    total_length=float(np.nansum(lengths)),
                    total_length_clean=float(finite_lengths[finite_lengths <= OUTLIER_LEN].sum()),
                    total_area=float(frame.geometry.area.sum()),
                    length_outliers=int(np.sum(finite_lengths > OUTLIER_LEN)),
                    max_length=float(finite_lengths.max()) if len(finite_lengths) else 0,
                    null=int(frame.geometry.isna().sum()), empty=int(frame.geometry.is_empty.sum()),
                    invalid=int((~frame.geometry.is_valid & ~frame.geometry.isna()).sum()),
                    z_min=float(z.min()) if len(z) else None, z_max=float(z.max()) if len(z) else None,
                )
    finally:
        con.close()
    return result


def boundary_overlay(official, own, buffer_m=1.0):
    """重新计算车道边界在官方缓冲区内的覆盖率；范围容差不能证明三维几何等价。"""
    a = pyogrio.read_dataframe(official, layer="LaneBorder")
    b = pyogrio.read_dataframe(own, layer="LaneBorder")
    if not math.isfinite(buffer_m) or buffer_m <= 0:
        raise ValueError("覆盖缓冲距离必须为有限正数")
    same_crs = a.crs is None and b.crs is None
    if a.crs is not None and b.crs is not None:
        same_crs = a.crs.equals(b.crs, ignore_axis_order=True)
    if not same_crs:
        raise ValueError("两套车道边界的 CRS 不一致，不能直接计算覆盖率")
    if a.crs is not None and a.crs.is_geographic:
        raise ValueError("覆盖比较需要平面坐标")
    a = a[(a.ID != 0) & a.geometry.notna() & (a.geometry.length < OUTLIER_LEN)]
    b = b[b.geometry.notna() & (b.geometry.length > 0)]
    union = a.geometry.buffer(buffer_m).union_all()
    coverage = (b.geometry.intersection(union).length / b.geometry.length).to_numpy()
    return dict(official=len(a), own=len(b), covered=int(np.sum(coverage >= .999)),
                ratio=float(np.mean(coverage >= .999) * 100) if len(b) else 0,
                buffer_m=buffer_m)


def build(own=None, official=None, overlays=None, names=None):
    """依据本次统计输入生成结论；缺失数据在明细中展示并计入失败。"""
    own = OWN if own is None else own
    official = GDAL if official is None else official
    names = sorted(names or TOWNS or (set(own) | set(official)))
    if not names:
        raise ValueError("没有 A/B 统计数据")
    overlays = overlays or {}
    lines = [
        "# OpenDRIVE 自研与 GDAL 产物 A/B 对比",
        "",
        "## 1. 本次比较范围与口径",
        "",
        f"- 报告生成日期：{datetime.now(timezone(timedelta(hours=8))).date().isoformat()}。",
        "- 读取本次已有 GPKG 重新计算；不会沿用历史 stats.json 的结论。",
        "- 长度和面积为二维统计；TIN 面积按各要素三角形求和，Polygon 面积按轮廓计算。",
        "- 总量偏差表的标记阈值为 0.5%，仅辅助检查统计，不是局部几何或通行规则验收阈值。",
        "- GDAL 原始结果保留原来的驱动版本和转换参数，本次没有重新调用 Docker。",
        "- LaneBorder 官方额外含 id=0 的基准边界；Signal/Object 的点与 TIN 面不是可互换表示。",
        "",
        "## 2. 要素数量",
        "",
        "| 数据 | 图层 | 官方 | 自研 | 结论 |",
        "|---|---|---:|---:|---|",
    ]
    required = ["ReferenceLine", "Lane", "RoadSignal", "RoadObject"]
    matches = {layer: 0 for layer in required}
    for name in names:
        for layer in required + ["LaneBorder", "RoadMark"]:
            a = official.get(name, {}).get(layer, {})
            b = own.get(name, {}).get(layer, {})
            an, bn = a.get("count", 0), b.get("count", 0)
            available = name in official and name in own
            if layer in ("ReferenceLine", "Lane"):
                available = available and bool(a) and bool(b)
            if layer in required and available and an == bn:
                matches[layer] += 1
            note = "未统计" if not available else "一致" if an == bn else ("定义范围不同" if layer == "LaneBorder" else "自研未实现" if layer == "RoadMark" else "❌ 不一致")
            lines.append(f"| {name} | {layer} | {an} | {bn} | {note} |")

    metrics = {}
    for index, layer, key, label in [(3, "ReferenceLine", "total_length", "参考线总长（m）"),
                                     (4, "Lane", "total_area", "车道面总面积（m²）"),
                                     (5, "LaneBorder", "total_length_clean", "车道边界总长（m，剔除 >100 km）")]:
        lines += ["", f"## {index}. {label}", ""]
        if layer == "LaneBorder":
            lines += ["两侧数量定义不同：本表保留官方 ID=0 边界，百分比不作同定义图层的通过/失败判定。", ""]
        lines += ["| 数据 | 官方 | 自研 | 相对偏差 |", "|---|---:|---:|---|"]
        values = []
        for name in names:
            av = official.get(name, {}).get(layer, {}).get(key, 0)
            bv = own.get(name, {}).get(layer, {}).get(key, 0)
            value = rel(bv, av)
            if value is not None:
                values.append(abs(value))
            formatted = f"{value:+.4f}%" if layer == "LaneBorder" and value is not None else fmt_pct(value)
            lines.append(f"| {name} | {av:,.2f} | {bv:,.2f} | {formatted} |")
        metrics[layer] = max(values) if values else None

    lines += ["", "### 5.1. 车道边界覆盖", "",
              "此项只比较二维边界在 1 m 缓冲区内的覆盖率，不将不足覆盖解释成已经确认的裁剪策略。",
              "", "| 数据 | 官方有效边界 | 自研 | 覆盖 ≥99.9% 的线 | 达标比例 |", "|---|---:|---:|---:|---:|"]
    for name in names:
        data = overlays.get(name)
        if data:
            lines.append(f"| {name} | {data['official']} | {data['own']} | {data['covered']} | {data['ratio']:.2f}% |")
        else:
            lines.append(f"| {name} | — | — | — | 未计算 |")
    lines += ["", "## 6. 结论", "", "| 检查 | 本次结果 |", "|---|---|"]
    for layer in required:
        mark = "✅" if matches[layer] == len(names) else "❌"
        lines.append(f"| {layer} 要素数 | {mark} {matches[layer]}/{len(names)} 一致 |")
    for layer in ["ReferenceLine", "Lane"]:
        value = metrics[layer]
        formatted = fmt_pct(value).lstrip("+")
        lines.append(f"| {layer} 总量最大绝对偏差 | {formatted} |")
    bad = sum(own.get(name, {}).get(layer, {}).get("length_outliers", 0) for name in names for layer in ("ReferenceLine", "LaneBorder"))
    lines.append(f"| 自研病态长线 | {'✅' if bad == 0 else '❌'} {bad} 条 |")
    lines += ["", "总量一致只支持二维形状相近的判断；不能证明信号物体表示、局部车道归属、",
              "高程曲面或导航通行规则完全一致。进一步检查应使用源 XML、回归用例和路网校验。",
              "", "## 7. 重现", "", "```powershell",
              ".venv/Scripts/python.exe code/compare/compare_ab.py --own-dir out --official-dir out_gdal -o COMPARE_GDAL.md",
              "```", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--own-dir", default=OUT)
    ap.add_argument("--official-dir", default=OUT_GDAL)
    ap.add_argument("-o", "--output", default=os.path.join(ROOT, "COMPARE_GDAL.md"))
    args = ap.parse_args()
    names = [p.stem for p in sorted(Path(DATA).glob("Town*.xodr"))]
    if not names:
        ap.error("data/ 下没有 Town*.xodr")
    own = {name: scan(Path(args.own_dir) / (name + ".gpkg")) for name in names}
    official = {name: scan(Path(args.official_dir) / (name + ".gpkg")) for name in names}
    overlays = {name: boundary_overlay(Path(args.official_dir) / (name + ".gpkg"),
                                       Path(args.own_dir) / (name + ".gpkg")) for name in names}
    for folder, stats in [(args.own_dir, own), (args.official_dir, official)]:
        with atomic_output(Path(folder) / "stats.json") as temporary:
            Path(temporary).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    md = build(own, official, overlays, names)
    with atomic_output(args.output) as temporary:
        Path(temporary).write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
