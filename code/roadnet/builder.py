"""按源数据、车道组、拓扑、Link、几何和写出阶段构建道路级有向路网。"""
import math
import os
import xml.etree.ElementTree as ET

import pandas as pd
from shapely.geometry import LineString

from .endpoints import EndpointStats, _emit
from .junction import group_references, is_uturn_geometry, movement_report
from .lanes import DRIVABLE, lane_stats, group_speed, road_type_of
from .output import print_summary, write_network
from .refinement import _refine_auxiliary_joins, refine_geometry
from .source import prepare_source
from .topology import build_topology, summarize_nodes


def emit_links(source, topology, step, min_len, keep_conn, fillet, merge_r, soft):
    """源车道组按实际行驶方向生成 Link；掉头来源与保护标记只在内存保留。"""
    roads, center, side_pts = source.roads, source.center, source.groups
    conn_flag, conn_junc = source.connector_flags, source.connector_junctions
    auxiliary_ends = source.auxiliary_ends
    dirs_of, side_of, group_port = source.dirs_of, source.side_of, source.group_port
    node_id, node_xy = topology.node_id, topology.node_xy
    lane_types = source.lane_types
    endpoints = EndpointStats()
    # --- 5. 生成 link ---
    rows = []
    auxiliary_sources = {}
    n_nodrv = 0          # 因为没有类型允许的车道而丢掉的 road 数
    n_aux = 0            # 辅道渐变单独出的 link 数
    aux_roads = set()
    for rid, rd in roads.items():
        pts = center[rid]
        L, R, _ = lane_stats(rd.el, lane_types)
        rtype = road_type_of(rd.el)
        is_conn = conn_flag[rid]
        junc = conn_junc.get(rid, rd.junction if rd.junction != "-1" else "-1")
        name = rd.name
        base = {
            "road_id": rid, "road_name": name, "junction_id": junc,
            "link_type": "connector" if is_conn else "road",
            "road_type": rtype,
        }
        # 每个行驶方向聚合对应车道群；共享车道可使两个方向使用同一几何。
        # 端口坐标取自同一份 side_pts, 端点因此天然落在节点上 —— 直路不会被
        # 在两端掰回中心线。路口的贴合交给连接路(第 5c 步)。
        # 普通 road 第 0 组为主路、其余为 aux；连接 road 所有分组都保持 connector 类型。
        groups_of = lambda d: (side_pts.get((rid, d))
                              or [{"pts": pts, "lid": [], "shared_lid": [], "taper": 0}])

        def emit_groups(d, nlanes, oneway, passable):
            nonlocal n_aux
            for gi, g in enumerate(groups_of(d)):
                shoulder = bool(g.get("shoulder_uturn"))
                if shoulder:
                    target_nodes = [node_id[group_port(*target)] for target in g["endpoint_targets"]]
                    if target_nodes[0] == target_nodes[1]:
                        # 使用基准线时对向节点可能重合；路肩掉头不能退化成同节点自环。
                        for report in source.shoulder_uturn_report:
                            if report.get("endpoint_targets") == g["endpoint_targets"] and report["connecting_road"] == rid:
                                report.update(status="filtered", reason="opposing_nodes_coincident")
                        continue
                speed = group_speed(rd.el, side_of(rid, d),
                                    set(lane_types) | {"shoulder"} if shoulder else lane_types,
                                    g["lid"], direction=d, lane_membership=g.get("lane_membership"),
                                    direction_override=d if shoulder else None)
                b = dict(base, speed_kmh=round(speed * 3.6, 1) if speed is not None else None,
                         link_type=(base["link_type"] if gi == 0 or is_conn else "aux"),
                         shared_lane_ids=",".join(str(lid) for lid in g["shared_lid"]))
                # 保留各类 Link 吸附前的有向车道群中线，随代表线合并保留。
                # 拉直时须同时核对本线与邻线的源交叉；此内部字段不写入输出。
                b["_source_geometry"] = LineString(g["pts"] if d > 0 else g["pts"][::-1])
                b["_junction_refs"] = (g.get("junction_refs") or group_references(source.references_by_road.get(rid, ()), d, g)
                                      if is_conn and passable else ())
                b["_is_uturn"] = bool(is_conn and passable and b["_junction_refs"]
                                      and is_uturn_geometry(b["_source_geometry"].coords))
                row_index = len(rows)
                if gi and not is_conn:
                    n_aux += 1
                    aux_roads.add(rid)
                _emit(rows, b, rid, g["pts"], d,
                      g.get("lanes", nlanes),
                      node_id, node_xy, min_len, oneway=oneway, step=step,
                      fillet=fillet, passable=passable,
                      lane_ids=",".join(str(x) for x in g["lid"]),
                      merge_r=merge_r, soft=soft,
                      endpoint_ports=(group_port(rid, 0, d, gi), group_port(rid, 1, d, gi)),
                      stats=endpoints, preserve_curve=b["_is_uturn"],
                      allow_uturn_expansion=shoulder)
                if (len(rows) > row_index and gi and not is_conn
                        and any((rid, end, d, gi) in auxiliary_ends for end in (0, 1))):
                    auxiliary_sources[row_index] = LineString(g["pts"] if d > 0 else g["pts"][::-1])

        if is_conn:
            if not any(side_pts.get((rid, d)) for d in dirs_of(rid)) and not keep_conn:
                n_nodrv += 1
                continue          # 严格模式：没有类型允许的车道时不输出可通行路径
            nlanes = max(L + R, 1)
            for d in dirs_of(rid):
                if side_pts.get((rid, d)):
                    emit_groups(d, nlanes, 1, 1)
                elif keep_conn:
                    emit_groups(d, 1, 1, 0)
        else:
            for d in dirs_of(rid):
                if side_pts.get((rid, d)):
                    emit_groups(d, max(L + R, 1), 1, 1)
            if not dirs_of(rid):
                n_nodrv += 1     # 类型不在允许集合的 road 不进路网

    links = pd.DataFrame(rows)
    if len(links) == 0:
        raise ValueError("没有生成任何 link, 检查输入文件")
    links["n_merged"] = 0   # 该转向被合并掉几条(0 = 本来就一条)
    if auxiliary_sources:
        _refine_auxiliary_joins(links, auxiliary_sources)


    return links, endpoints, (n_nodrv, n_aux, aux_roads)


def _drop_redundant_auxiliary_links(links):
    """省略同 road、同方向、同起终节点的渐变辅道，保留主组几何与属性。

    必须存在唯一的普通主组，且通行标志一致；连接路和独立节点的辅道不参与。
    在线形整理后调用，避免删掉辅助几何后改变主组或邻接连接线的处理结果。
    返回保留原行索引的表及删除记录，供整理索引、节点度数和构建摘要继续使用。
    """
    main_rows = {}
    for i, row in links[links.link_type == "road"].iterrows():
        key = row.road_id, row.from_node, row.to_node
        main_rows.setdefault(key, []).append(i)
    drop, removed = [], []
    for i, row in links[links.link_type == "aux"].iterrows():
        matches = main_rows.get((row.road_id, row.from_node, row.to_node), ())
        if len(matches) != 1:
            continue
        main = links.loc[matches[0]]
        if row.oneway != main.oneway or row.passable != main.passable:
            continue
        drop.append(i)
        removed.append({"road_id": row.road_id, "lane_ids": row.lane_ids,
                        "main_lane_ids": main.lane_ids,
                        "from_node": int(row.from_node), "to_node": int(row.to_node)})
    return links.drop(index=drop) if drop else links, removed


def build_net(xodr_path, gpkg_path, step=1.0, snap=0.05, min_len=0.1,
              csv=False, verbose=True, lane_types=DRIVABLE, keep_conn=False,
              merge_turns=True, smooth=1.5, smooth_roads=False, fillet=4.0,
              lane_center=True, lane_ramp=0.0, split_taper=True, merge_r=12.0,
              coincide=True, mid_coincide=True, straighten=True, soft=True,
              refine_connectors=True, shoulder_uturns=True):
    """构建二维单向 Node/Link，复用源车道计算并在写出前核验源掉头有向连接。

    参数和返回值兼容原入口。双向车道按两个行驶方向聚合，节点按有效组建立；
    掉头由 junction 源关系确定，保留弧形和独立对向节点，不推断未声明掉头。
    返回的 links.attrs["junction_movements"] 记录全部源 connection 与合并后的 Link。
    shoulder_uturns 默认保留源中能接入实际行驶方向的路肩掉头；普通路肩仍按 lane_types 筛选。
    无独立路径且与本 road 主组共用起终节点的 aux 不写出，主组代表线及源属性不变。
    GPKG 和每份 CSV 独立原子替换，写入失败保留各自旧文件。
    """
    if not math.isfinite(step) or step <= 0:
        raise ValueError("采样步长必须为有限正数")
    for name, value in (("snap", snap), ("min_len", min_len), ("smooth", smooth),
                        ("fillet", fillet), ("lane_ramp", lane_ramp), ("merge_r", merge_r)):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} 必须为有限非负数")
    if os.path.realpath(xodr_path) == os.path.realpath(gpkg_path):
        raise ValueError("输出不能覆盖输入 XODR")
    root = ET.parse(xodr_path).getroot()
    source = prepare_source(root, step, lane_types, lane_center, lane_ramp, split_taper,
                            shoulder_uturns=shoulder_uturns)
    topology = build_topology(source, snap, keep_conn)
    links, endpoints, emission = emit_links(source, topology, step, min_len, keep_conn,
                                            fillet, merge_r, soft)
    links, refinements = refine_geometry(links, topology.node_xy, merge_turns, smooth,
        smooth_roads, fillet, coincide, mid_coincide, straighten, refine_connectors)
    links, redundant_auxiliaries = _drop_redundant_auxiliary_links(links)
    links.attrs["redundant_auxiliaries"] = redundant_auxiliaries
    auxiliaries = links[links.link_type == "aux"]
    emission = emission[0], len(auxiliaries), set(auxiliaries.road_id)
    links.insert(0, "link_id", range(1, len(links) + 1))
    links.attrs["geometry_refinement"] = {
        "straightened_link_ids": sorted(int(links.at[i, "link_id"]) for i in refinements.straightened),
        "refined_link_ids": sorted(int(links.at[i, "link_id"]) for i in refinements.refined),
    }
    # 整理记录仍按删除前的行索引查找；映射到新 Link ID 后恢复连续索引，兼容调用方。
    links = links.reset_index(drop=True)
    links.attrs["junction_movements"] = movement_report(links, source)
    links.attrs["shoulder_uturns"] = source.shoulder_uturn_report
    links = links.drop(columns=["_junction_refs", "_is_uturn"])
    nodes, components, _, _, isolated = summarize_nodes(links, topology)
    write_network(links, nodes, source.crs, gpkg_path, csv)
    if verbose:
        print_summary(source, topology, links, nodes, components, isolated, endpoints,
                      refinements, emission, xodr_path, gpkg_path, smooth)
    return links, nodes
