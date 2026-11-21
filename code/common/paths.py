# -*- coding: utf-8 -*-
"""项目统一路径常量。

目录布局（2026-11-01 重构后）：

    opendrive2shp/
    |-- code/           所有代码
    |   |-- common/     本文件所在：跨路线共用的路径/工具
    |   |-- own/        路线 A：自研 Python 解析
    |   |-- gdal/       路线 B：官方 GDAL XODR 驱动（Docker）
    |   |-- roadnet/    路线 A 的下游：Link-Node 路网生成
    |   |-- compare/    A/B 对比与产物整理
    |   |-- diag/       几何校验 / 专题插图工具
    |-- data/           *.xodr 源数据
    |-- out/            自研路线 + 路网 + 诊断图 的产物
    |-- out_gdal/       官方路线产物
    |-- docs/           专题文章

常量按本文件的位置定位项目目录。用户显式传入的相对输入/输出路径仍按
调用时的工作目录解析；只有默认数据和默认输出目录使用这些常量。

用法（各脚本开头两行）：

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from common.paths import ROOT, DATA, OUT, OUT_GDAL
"""
import os

# code/common/paths.py -> common -> code -> 项目根
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CODE = os.path.join(ROOT, "code")
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "out")
OUT_GDAL = os.path.join(ROOT, "out_gdal")
DOCS = os.path.join(ROOT, "docs")

# 虚拟环境解释器（Windows 布局；Linux/macOS 下回退到 bin/）
_venv_win = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
_venv_posix = os.path.join(ROOT, ".venv", "bin", "python")
VENV_PY = _venv_win if os.path.exists(_venv_win) else _venv_posix


def ensure(*dirs):
    """确保目录存在，返回第一个参数（方便链式使用）。"""
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    return dirs[0] if dirs else None


def town_xodrs():
    """data/ 下所有 Town*.xodr，按名字排序。"""
    import glob
    return sorted(glob.glob(os.path.join(DATA, "Town*.xodr")))
