#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OpenDRIVE 常规路网 Python 入口。

在项目根目录执行 python code/roadnet/roadnet.py data/Town06.xodr --csv。
构建实现按车道、junction、节点、端点与线形整理拆分；build_net 的原参数保持兼容。
Link 坐标顺序与行驶方向一致，所有 Link 单向。掉头使用源 junction 路径，
两个对向节点保持独立，并在写出前检查进口→掉头→反向出口及回转弧形。
"""
from pathlib import Path
import sys

if __package__ in (None, ""):
    # 仅脚本直接运行时补项目 code 目录，包内模块全部使用显式相对导入。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roadnet.builder import build_net
from roadnet.cli import main
from roadnet.endpoints import _emit, _s_merge, _tail_blend, _tail_soft, _not_reverse
from roadnet.geometry import (_arc_len, _smoothstep, _resample, _walk, _fillet_at,
    _end_tangent, _gauss_smooth, smooth_line, _kink_deg, _norm_samples, _mid_index,
    _kink_near, _smooth_patch, _geom_points, _side, _really_crosses, _joint_kink,
    _tangent, _chord_sag, _out_dir, _directed_road_tangents, _crossing_count,
    _replacement_safe, _curve_turns)
from roadnet.lanes import (DRIVABLE, road_link, lane_directions, lane_stats,
    group_speed, road_type_of, is_connector_port, _offsets_to_points, _lane_arrays,
    _taper_weight, side_lane_groups, side_center_points)
from roadnet.junction import (_end_lane_links, _lane_chain, _partition_auxiliary_connectors,
                             _merge_turns, is_uturn_geometry)
from roadnet.topology import UF, _components
from roadnet.coincidence import _coincide, _mid_coincide
from roadnet.refinement import (_refine_auxiliary_joins, _straighten, _fit_turn,
                               _refine_connectors, _refine_straight_neighbours)


__all__ = ["build_net", "main"]


if __name__ == "__main__":
    main()
