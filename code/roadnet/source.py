"""解析 XODR 道路、采样源几何并准备方向及车道组；不写出结果。"""
from dataclasses import dataclass, field

from own.xodr2gpkg import Road, center_lane_points, parse_georeference, parse_header_offset
from .junction import collect_connections, _partition_auxiliary_connectors, retain_shoulder_uturns
from .lanes import lane_directions, side_lane_groups


@dataclass
class SourceContext:
    """本次构建的源数据与车道组，节点和几何阶段共享同一份采样与端部定义。"""

    root: object
    crs: object
    roads: dict
    center: dict
    connector_flags: dict
    connector_directions: dict
    connector_junctions: dict
    directions: dict
    groups: dict
    auxiliary_ends: set
    references: list
    references_by_road: dict
    road_links: dict
    lane_types: object
    shoulder_uturn_report: list = field(default_factory=list)

    def dirs_of(self, road_id):
        return self.directions[road_id]

    def side_of(self, road_id, direction):
        return direction if self.roads[road_id].el.get("rule", "RHT") == "LHT" else -direction

    def group_port(self, road_id, end, direction, group_index=0):
        """主组使用三元接点；混合连接组、独立辅道或路肩掉头追加组号，避免端口串接。"""
        groups = self.groups.get((road_id, direction), [])
        if group_index < len(groups) and groups[group_index].get("shoulder_uturn"):
            return road_id, end, direction, group_index
        if group_index and (self.connector_flags[road_id]
                or (road_id, end, direction, group_index) in self.auxiliary_ends):
            return road_id, end, direction, group_index
        return road_id, end, direction


def prepare_source(root, step, lane_types, lane_center, lane_ramp, split_taper,
                   shoulder_uturns=True):
    """先过滤有效车道与方向，再准备节点阶段所用的端口来源；显式 junction 关系作补充。"""
    crs, _ = parse_georeference(root)
    offset = parse_header_offset(root)
    roads, center = {}, {}
    for element in root.findall("road"):
        rid = element.get("id")
        road = Road(element, step=step, offset=offset)
        points = center_lane_points(road, with_z=False)
        if len(points) >= 2:
            roads[rid], center[rid] = road, points
    references, conn_dir, conn_junc, references_by_road, road_links = collect_connections(root, roads)
    # --- 2b. 每条路要出 link 的行驶方向 + 各自车道群的中心线 ---
    conn_flag = {rid: (rid in conn_dir or rd.junction != "-1")
                 for rid, rd in roads.items()}
    road_directions, shared_directions = {}, {}
    for rid, rd in roads.items():
        directions, shared = set(), set()
        for section in rd.el.findall("lanes/laneSection"):
            for tag, side in (("left", 1), ("right", -1)):
                for lane in section.findall(tag + "/lane"):
                    if lane.get("type") not in lane_types:
                        continue
                    ds = lane_directions(lane, rd.el.get("rule", "RHT"), side)
                    directions.update(ds)
                    if len(ds) == 2:
                        shared.update(ds)
        # 维持普通 road 既有的右侧优先次序，让不含新声明的数据 ID/几何保持稳定。
        first = -1 if rd.el.get("rule", "RHT") == "LHT" else 1
        road_directions[rid] = [d for d in (first, -first) if d in directions]
        shared_directions[rid] = shared

    def dirs_of(rid):
        """内部方向符号：+1 沿源 road 的里程递增方向，−1 沿里程递减方向。"""
        if conn_flag[rid]:
            # contactPoint 决定既有连接方向；共享车道增加另一方向，用集合去重重复声明。
            return sorted(set(conn_dir.get(rid, {1})) | shared_directions[rid], reverse=True)
        return road_directions[rid]

    def side_of(rid, direction):
        """LHT 右侧沿源里程递减方向行驶；连接路按接触端及道路规则找侧别。"""
        return direction if roads[rid].el.get("rule", "RHT") == "LHT" else -direction

    side_pts = {}
    for rid, rd in roads.items():
        for d in dirs_of(rid):
            # 一个方向聚合一组车道；共享车道无需额外副本，也不拆出重复 aux。
            groups = side_lane_groups(rd, side_of(rid, d), lane_types, lane_ramp,
                                      split=split_taper and not conn_flag[rid], direction=d)
            if not lane_center and groups:
                ids = list(dict.fromkeys(lid for group in groups for lid in group["lid"]))
                shared = list(dict.fromkeys(lid for group in groups for lid in group["shared_lid"]))
                endpoint_ids = tuple(tuple(dict.fromkeys(lid for group in groups for lid in group["end_lids"][end]))
                                     for end in (0, 1))
                groups = [{"pts": center[rid], "lid": ids, "shared_lid": shared,
                           "taper": 0, "lanes": sum(group["lanes"] for group in groups),
                           "end_lids": endpoint_ids, "end_wide_lids": endpoint_ids}]
            side_pts[(rid, d)] = groups

    auxiliary_ends = (_partition_auxiliary_connectors(root, roads, side_pts, conn_flag, lane_types, lane_ramp,
                                                     road_links=road_links)
                      if lane_center and split_taper else set())


    directions = {rid: dirs_of(rid) for rid in roads}
    source = SourceContext(root, crs, roads, center, conn_flag, conn_dir, conn_junc,
                           directions, side_pts, auxiliary_ends, references,
                           references_by_road, road_links, lane_types)
    if shoulder_uturns:
        # 普通车道组和辅道先定；路肩掉头只接这些已有行驶方向，不扩大普通路肩通行范围。
        source.shoulder_uturn_report = retain_shoulder_uturns(source)
    return source
