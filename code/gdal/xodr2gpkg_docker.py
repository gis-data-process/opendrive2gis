#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通过已有 Docker 镜像调用官方 XODR 驱动，结构化传参并保护旧 GPKG。

用法：python xodr2gpkg_docker.py 输入.xodr [输出.gpkg] [-oo KEY=VALUE ...]
默认输出 out_gdal/<输入名>.gpkg；不会自动拉取镜像。
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.files import atomic_output  # noqa: E402
from common.paths import OUT_GDAL  # noqa: E402

DEFAULT_IMAGE = "ghcr.io/osgeo/gdal:ubuntu-full-latest"


def build_command(source, target, options, image=DEFAULT_IMAGE):
    """生成参数列表，路径含空格和 KEY=VALUE 都作为一个参数，不经过 shell 重解释。

    当前入口专用于 GPKG；单文件可原子替换，Shapefile 的多个伴随文件不走此入口。
    Docker 的两个挂载目录分别只读输入、可写临时输出。
    """
    source, target = Path(source).resolve(), Path(target).resolve()
    if not source.is_file():
        raise ValueError(f"输入不存在: {source}")
    if source == target:
        raise ValueError("输出不能覆盖源文件")
    if target.suffix.lower() != ".gpkg":
        raise ValueError("输出必须是 .gpkg；其他格式请直接使用 ogr2ogr")
    if any(value in ("-f", "-update", "-append", "-overwrite") for value in options):
        raise ValueError("此入口创建完整 GPKG，不接受 -f/-update/-append/-overwrite")
    return ["docker", "run", "--rm", "--pull=never",
            "-v", f"{source.parent}:/input:ro", "-v", f"{target.parent}:/output",
            image, "ogr2ogr", "-f", "GPKG", f"/output/{target.name}",
            f"/input/{source.name}", *options]


def run(source, target, options, image=DEFAULT_IMAGE, dry=False):
    """Docker 成功且临时文件完整时替换目标；非零退出时旧结果保持原样。"""
    command = build_command(source, target, options, image)
    if dry:
        print(subprocess.list2cmdline(command))
        return 0
    try:
        with atomic_output(target) as temporary:
            command = build_command(source, temporary, options, image)
            result = subprocess.run(command, check=False)
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, command)
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    print(f"输出: {Path(target).resolve()}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("source")
    ap.add_argument("--image", default=os.environ.get("GDAL_DOCKER_IMAGE", DEFAULT_IMAGE))
    ap.add_argument("--dry-run", action="store_true", help="仅显示命令，不启动 Docker")
    args, options = ap.parse_known_args()
    source = Path(args.source)
    # 不把 target 声明为可选位置参数，避免 argparse 将未知 -oo 后的
    # KEY=VALUE 误认成输出文件。剩余参数的首项只有不以 - 开头才是目标。
    explicit = options.pop(0) if options and not options[0].startswith("-") else None
    target = Path(explicit) if explicit else Path(OUT_GDAL) / (source.stem + ".gpkg")
    try:
        return run(source, target, options, args.image, args.dry_run)
    except (ValueError, OSError) as exc:
        print(f"转换失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
