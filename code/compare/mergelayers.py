#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""将自研中心线层并入现有 GPKG；同名层整体替换，其他表保留。

默认：out/Town*.gpkg -> out_gdal/Town*_poly.gpkg。
先校验 CRS，再在目标的数据库备份上修改；全部成功后才替换原文件。
"""
import argparse
import os
from pathlib import Path
import sqlite3
import sys

import pyogrio
from pyproj import CRS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.files import atomic_output  # noqa: E402
from common.paths import OUT, OUT_GDAL  # noqa: E402

EXTRA_LAYERS = ["CenterLane", "LaneCenter", "JunctionLink"]


def merge(src, dst, layers, dry=False):
    """备份目标后替换选定图层，失败保留目标；不自动重投影来源几何。

    同一文件的全部空间图层须具有相同 CRS（局部坐标均为 None）。
    SQLite backup 同时复制属性表和元数据，避免直接拷贝遗漏 WAL 中的内容。
    读取后按来源声明恢复可空整数/数值类型，保留层级来源元数据及 xodr 属性来源。
    """
    source, target = Path(src).resolve(), Path(dst).resolve()
    if source == target:
        raise ValueError("来源与目标不能是同一文件")
    if not source.is_file() or not target.is_file():
        raise ValueError("来源和目标 GPKG 必须已存在")
    src_layers = {str(row[0]) for row in pyogrio.list_layers(source)}
    dst_layers = {str(row[0]) for row in pyogrio.list_layers(target)}
    todo = list(dict.fromkeys(name for name in layers if name in src_layers))
    if not todo:
        raise ValueError(f"来源没有可合并图层: {', '.join(layers)}")
    source_info = {name: pyogrio.read_info(source, layer=name) for name in todo}
    frames = {name: pyogrio.read_dataframe(source, layer=name) for name in todo}
    for name, frame in frames.items():
        # 含 NULL 的 INTEGER 会被读取为 float64，必须按驱动声明恢复，不能从值猜类型。
        for field, dtype in zip(source_info[name]["fields"], source_info[name]["dtypes"]):
            kind = str(dtype)
            if kind.startswith("int"):
                frame[field] = frame[field].astype("Int" + kind[3:])
            elif kind.startswith("uint"):
                frame[field] = frame[field].astype("UInt" + kind[4:])
            elif kind.startswith("float"):
                frame[field] = frame[field].astype("Float" + kind[5:])
            elif kind == "object":
                frame[field] = frame[field].astype("string")
    target_crs = []
    for name, geometry in pyogrio.list_layers(target):
        if geometry is not None:
            value = pyogrio.read_info(target, layer=name)["crs"]
            target_crs.append(CRS.from_user_input(value) if value else None)
    for name, frame in frames.items():
        for crs in target_crs:
            same = frame.crs is None and crs is None
            if frame.crs is not None and crs is not None:
                same = frame.crs.equals(crs, ignore_axis_order=True)
            if not same:
                raise ValueError(f"{name} 的 CRS 与目标不一致，请先明确坐标转换")
    if dry:
        print(f"[dry] {source.name} -> {target.name}: {', '.join(todo)}")
        return {}
    target_metadata = pyogrio.read_info(target, layer=next(iter(dst_layers)))["dataset_metadata"] or {}
    for info in source_info.values():
        # 文件同时含官方层与自研层，保留目标元数据，仅同步自研属性来源键。
        target_metadata.update({key: value for key, value in (info["dataset_metadata"] or {}).items()
                                if key.startswith("xodr_")})
    with atomic_output(target) as temporary:
        source_db = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
        backup_db = sqlite3.connect(temporary)
        try:
            source_db.backup(backup_db)
        finally:
            backup_db.close()
            source_db.close()
        for name, frame in frames.items():
            pyogrio.write_dataframe(frame, temporary, layer=name, driver="GPKG", append=False,
                                    dataset_metadata=target_metadata,
                                    layer_metadata=source_info[name]["layer_metadata"])
    return {name: (len(frame), name in dst_layers) for name, frame in frames.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src", nargs="?")
    ap.add_argument("dst", nargs="?")
    ap.add_argument("-l", "--layers", nargs="+", default=EXTRA_LAYERS)
    ap.add_argument("--dry", action="store_true", help="只检查并打印，不写文件")
    args = ap.parse_args()
    if args.src:
        source = Path(args.src)
        pairs = [(source, Path(args.dst) if args.dst else Path(OUT_GDAL) / (source.stem + "_poly.gpkg"))]
    else:
        pairs = [(src, Path(OUT_GDAL) / (src.stem + "_poly.gpkg"))
                 for src in sorted(Path(OUT).glob("Town*.gpkg"))
                 if not src.stem.endswith("_net")]
    if not pairs:
        ap.error("没有可处理的来源，请先生成自研 GPKG")
    failed = False
    for src, dst in pairs:
        try:
            result = merge(src, dst, args.layers, args.dry)
            for name, (count, replaced) in result.items():
                print(f"{src.name} -> {dst.name}: {name} {count} 个要素{'（替换）' if replaced else '（新增）'}")
        except (ValueError, OSError, sqlite3.Error) as exc:
            print(f"[FAIL] {src.name}: {exc}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
