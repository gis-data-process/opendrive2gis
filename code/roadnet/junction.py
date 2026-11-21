"""junction 源连接、车道组绑定、掉头识别与转向来源核验。

掉头只是已有 source connection 的通行形状，不从对向端点距离推断新增路径。
内部记录保留全部被合并的 connection，输出 Link 表仍沿用固定属性契约。
"""
from collections import defaultdict
from dataclasses import dataclass
import math

import numpy as np

from .geometry import _arc_len, _end_tangent, _mid_index
from .lanes import lane_directions, road_link, side_lane_groups
from own.xodr2gpkg import lane_sections


@dataclass(frozen=True)
class ConnectionRef:
    """一条明确的 junction 源声明；lane_links 保留进口车道到连接路车道的映射。"""

    junction_id: str
    connection_id: str
    incoming_road: str
    connecting_road: str
    contact_point: str
    lane_links: tuple

    @property
    def end(self):
        return 0 if self.contact_point == "start" else 1

    @property
    def direction(self):
        return 1 if self.end == 0 else -1

    @property
    def key(self):
        return (self.junction_id, self.connection_id, self.incoming_road,
                self.connecting_road, self.contact_point)


def collect_connections(root, roads):
    """一次读取 junction 声明，并补齐能唯一确定的连接路进口端。

    road/link 已有明确的 road 目标时不猜测位置；缺少重复声明时，仅从 incomingRoad
    指向本 junction 的唯一端确定接点。不存在的 road 和未知出口不制造通行关系。
    不修改 XML 原节点，补充信息只在本次构建使用。
    """
    references, seen = [], set()
    directions, junctions, by_road = defaultdict(set), {}, defaultdict(list)
    declarations = {rid: road_link(road.el) for rid, road in roads.items()}
    for junction in root.findall("junction"):
        for connection in junction.findall("connection"):
            rid = connection.get("connectingRoad")
            if not rid:
                continue
            contact = connection.get("contactPoint", "start")
            if contact not in ("start", "end"):
                raise ValueError(f"junction {junction.get('id')} connection {connection.get('id')} contactPoint 无效: {contact}")
            reference = ConnectionRef(junction.get("id", ""), connection.get("id", ""),
                connection.get("incomingRoad", ""), rid, contact,
                tuple((int(item.get("from")), int(item.get("to"))) for item in connection.findall("laneLink")))
            directions[rid].add(reference.direction)
            junctions.setdefault(rid, reference.junction_id)
            if reference.key not in seen:
                references.append(reference)
                by_road[rid].append(reference)
                seen.add(reference.key)
            if rid not in roads or reference.incoming_road not in roads:
                continue
            tag = "predecessor" if reference.end == 0 else "successor"
            existing = declarations[rid].get(tag)
            if existing is not None and existing[1] == "road":
                if existing[0] != reference.incoming_road:
                    raise ValueError(f"junction {reference.junction_id} connection {reference.connection_id} 与 road {rid} 的 {tag} 声明冲突")
                continue
            incoming = roads[reference.incoming_road]
            endpoints = []
            for source_tag, source_end in (("predecessor", 0), ("successor", 1)):
                linked = road_link(incoming.el).get(source_tag)
                if linked and ((linked[1] == "junction" and linked[0] == reference.junction_id)
                               or (linked[1] == "road" and linked[0] == rid)):
                    endpoints.append(source_end)
            if len(endpoints) == 1:
                declarations[rid][tag] = (reference.incoming_road, "road",
                                         "start" if endpoints[0] == 0 else "end")
            elif len(endpoints) > 1:
                raise ValueError(f"junction {reference.junction_id} connection {reference.connection_id} 的进口端不唯一")
    return references, directions, junctions, dict(by_road), declarations


def is_uturn_geometry(points, min_angle=145.0):
    """识别源车道回转弧，用于保护几何；不据此判断或新增车辆通行权限。

    首末行驶方向近似相反且弧长明显大于端点距离，才属于回转候选。
    用短端部窗口，避免紧凑掉头因长切向窗口被误判成普通转弯。
    """
    p = np.asarray(points, dtype=float)[:, :2]
    if len(p) < 3 or not np.isfinite(p).all():
        return False
    length = float(_arc_len(p)[-1])
    if length <= 0:
        return False
    span = max(.05, min(.5, .02 * length))
    before = -_end_tangent(p[::-1], span=span)
    after = _end_tangent(p, span=span)
    return bool(before is not None and after is not None
                and float(before @ after) <= math.cos(math.radians(min_angle))
                and length > 1.1 * float(np.linalg.norm(p[-1] - p[0])))


def group_references(references, direction, group):
    """按接触端车道 ID 将源 connection 绑定到相应组，分组换号后仍按端部 ID 配对。"""
    result = []
    for reference in references:
        if reference.direction != direction:
            continue
        lane_ids = set(group.get("end_lids", (group["lid"], group["lid"]))[reference.end])
        if not reference.lane_links or lane_ids.intersection(to for _, to in reference.lane_links):
            result.append(reference)
    return tuple(result)


def movement_report(links, source):
    """写出前核对源掉头的进口、出口和几何，返回全部 connection 的诊断记录。

    对有效且两端已明确的掉头，必须能接到指定源道路组、两个节点分开并保留回转弧。
    缺少源路或出口声明的部分输入只报告 unresolved，不猜测缺失连接。
    """
    report = []
    covered = set()
    # 每个源 connection 只查其进口/出口节点，避免逐条复制和扫描整张 Link 表。
    incoming_index, outgoing_index = defaultdict(list), defaultdict(list)
    for _, item in links[links.passable == 1].iterrows():
        incoming_index[(item.road_id, item.to_node)].append(int(item.link_id))
        outgoing_index[(item.road_id, item.from_node)].append(int(item.link_id))
    for _, row in links.iterrows():
        for reference in row.get("_junction_refs", ()):
            covered.add(reference.key)
            start = source.road_links.get(reference.connecting_road, {}).get(
                "predecessor" if reference.end == 0 else "successor")
            finish = source.road_links.get(reference.connecting_road, {}).get(
                "successor" if reference.end == 0 else "predecessor")
            incoming = incoming_index.get((reference.incoming_road, row.from_node), [])
            outgoing = (outgoing_index.get((finish[0], row.to_node), [])
                        if finish and finish[1] == "road" else [])
            resolved = bool(start and start[1] == "road" and finish and finish[1] == "road"
                            and reference.incoming_road in source.roads and finish[0] in source.roads)
            connected = resolved and bool(incoming) and bool(outgoing)
            is_uturn = bool(row.get("_is_uturn", False))
            if is_uturn and row.passable and resolved:
                if not connected or row.from_node == row.to_node or not is_uturn_geometry(row.geometry.coords):
                    raise ValueError(f"junction {reference.junction_id} connection {reference.connection_id} 掉头连通或弧形检查失败")
                if not row.geometry.is_valid or not row.geometry.is_simple:
                    raise ValueError(f"junction {reference.junction_id} connection {reference.connection_id} 掉头几何无效")
            report.append({
                "junction_id": reference.junction_id, "connection_id": reference.connection_id,
                "incoming_road": reference.incoming_road, "connecting_road": reference.connecting_road,
                "contact_point": reference.contact_point, "lane_links": list(reference.lane_links),
                "movement": "uturn" if is_uturn else "turn",
                "output_link_id": int(row.link_id), "output_road_id": row.road_id,
                "from_node": int(row.from_node), "to_node": int(row.to_node),
                "directionally_connected": bool(connected), "source_ends_resolved": resolved,
                "incoming_link_ids": incoming,
                "outgoing_link_ids": outgoing,
                "length_m": float(row.geometry.length),
                "status": "connected" if connected else "unresolved_source_ends",
            })
    for reference in source.references:
        if reference.key in covered:
            continue
        rd = source.roads.get(reference.connecting_road)
        points = source.center.get(reference.connecting_road)
        report.append({
            "junction_id": reference.junction_id, "connection_id": reference.connection_id,
            "incoming_road": reference.incoming_road, "connecting_road": reference.connecting_road,
            "contact_point": reference.contact_point, "lane_links": list(reference.lane_links),
            "movement": "uturn" if points is not None and is_uturn_geometry(points) else "turn",
            "output_link_id": None, "directionally_connected": False,
            "status": ("missing_source_road" if rd is None else
                       "filtered_lane_type" if not source.groups.get((reference.connecting_road, reference.direction))
                       else "filtered_length_or_unmapped_group"),
        })
    return report


def _end_lane_links(root, road, end, other_id):
    """该 road 接触端的自身车道 ID → 相邻 road 车道 ID；lane/link 优先，junction 作补充。"""
    sections = lane_sections(road)
    if not sections:
        return {}
    section = sections[0 if end == 0 else -1][2]
    tag = "predecessor" if end == 0 else "successor"
    result = {}
    for side in ("left", "right"):
        for lane in section.findall(side + "/lane"):
            linked = lane.find("link/" + tag)
            if linked is not None:
                result[int(lane.get("id"))] = int(linked.get("id"))
    for connection in root.findall("junction/connection"):
        if (connection.get("connectingRoad") == road.el.get("id")
                and connection.get("incomingRoad") == other_id
                and connection.get("contactPoint", "start") == ("start" if end == 0 else "end")):
            for linked in connection.findall("laneLink"):
                result.setdefault(int(linked.get("to")), int(linked.get("from")))
    return result


def _lane_chain(road, end, lane_id):
    """从接触端沿 lane/link 跟踪跨段重编号，返回 (段下标, lane ID) 轨迹与两个端点 ID。

    没有声明时仅沿相同 ID 延续；下一段明确指定另一前身时停止，不凭 ID 拼接。
    """
    sections = [{int(lane.get("id")): lane for side in ("left", "right")
                 for lane in section.findall(side + "/lane")}
                for _, _, section in lane_sections(road)]
    order = list(range(len(sections))) if end == 0 else list(range(len(sections) - 1, -1, -1))
    tag = "successor" if end == 0 else "predecessor"
    reverse = "predecessor" if end == 0 else "successor"
    members, endpoints = set(), [None, None]
    for position, index in enumerate(order):
        lane = sections[index].get(lane_id)
        if lane is None:
            break
        members.add((index, lane_id))
        if index == 0:
            endpoints[0] = lane_id
        if index == len(sections) - 1:
            endpoints[1] = lane_id
        if position == len(order) - 1:
            break
        linked = lane.find("link/" + tag)
        if linked is not None:
            lane_id = int(linked.get("id"))
        else:
            next_lane = sections[order[position + 1]].get(lane_id)
            back = next_lane.find("link/" + reverse) if next_lane is not None else None
            if back is not None and int(back.get("id")) != lane_id:
                break
    return members, endpoints


def retain_shoulder_uturns(source):
    """把能按实际方向接通的源路肩掉头加入连接组，返回保留/过滤原因。

    例外仅用于完整 junction/laneLink 路径；普通 road 的类型与方向保持原筛选结果。
    路肩的接入端映射到同侧最近的已有通行车道组。目标方向不存在、轨迹不完整、
    中间含人行道或接点不唯一时过滤；不为单向道路制造反向车道，不修改源 XML。
    """
    reports, partitions = [], {}
    threshold = math.cos(math.radians(145))

    def endpoint_lanes(road, end):
        sections = lane_sections(road)
        if not sections:
            return {}
        section = sections[0 if end == 0 else -1][2]
        return {int(lane.get("id")): lane for side in ("left", "right")
                for lane in section.findall(side + "/lane")}

    def target_group(road_id, end, direction, lane_id):
        """显式通行车道直接配组；源路肩仅配同侧同方向最近一组，等距歧义不兜底。"""
        lane = endpoint_lanes(source.roads[road_id], end).get(lane_id)
        if lane is None:
            return None
        groups = source.groups.get((road_id, direction), [])
        choices = []
        for index, group in enumerate(groups):
            if group.get("shoulder_uturn"):
                continue
            lids = group["end_lids"][end] if index == 0 else group["end_wide_lids"][end]
            if lane_id in lids:
                return index
            if lane.get("type") != "shoulder":
                continue
            side = 1 if lane_id > 0 else -1
            if direction not in lane_directions(lane, source.roads[road_id].el.get("rule", "RHT"), side):
                continue
            distances = [abs(abs(lid) - abs(lane_id)) for lid in lids if (lid > 0) == (lane_id > 0)]
            if distances:
                choices.append((min(distances), index))
        if not choices:
            return None
        choices.sort()
        if len(choices) > 1 and choices[0][0] == choices[1][0]:
            return None
        return choices[0][1]

    for reference in source.references:
        rid, end, direction = reference.connecting_road, reference.end, reference.direction
        road = source.roads.get(rid)
        if road is None:
            continue
        lanes = endpoint_lanes(road, end)
        shoulder_ids = {to for _, to in reference.lane_links
                        if to in lanes and lanes[to].get("type") == "shoulder"}
        if not shoulder_ids:
            continue
        declarations = source.road_links[rid]
        start = declarations.get("predecessor" if end == 0 else "successor")
        finish = declarations.get("successor" if end == 0 else "predecessor")
        if (not start or not finish or start[1] != "road" or finish[1] != "road"
                or start[0] != reference.incoming_road
                or start[0] not in source.roads or finish[0] not in source.roads):
            continue
        incoming_end, outgoing_end = (0 if link[2] == "start" else 1 for link in (start, finish))
        incoming_direction = direction if end != incoming_end else -direction
        outgoing_direction = direction if 1 - end != outgoing_end else -direction
        incoming, outgoing = source.roads[start[0]], source.roads[finish[0]]
        a = incoming_direction * np.array([incoming.COS[0 if incoming_end == 0 else -1],
                                            incoming.SIN[0 if incoming_end == 0 else -1]])
        b = outgoing_direction * np.array([outgoing.COS[0 if outgoing_end == 0 else -1],
                                            outgoing.SIN[0 if outgoing_end == 0 else -1]])
        if float(a @ b) > threshold:
            continue
        for from_id, to_id in reference.lane_links:
            if to_id not in shoulder_ids:
                continue
            report = {"junction_id": reference.junction_id, "connection_id": reference.connection_id,
                      "connecting_road": rid, "contact_point": reference.contact_point,
                      "from_lane": from_id, "to_lane": to_id,
                      "status": "filtered", "reason": "incomplete_lane_chain"}
            reports.append(report)
            members, endpoints = _lane_chain(road, end, to_id)
            if any(lid is None for lid in endpoints):
                continue
            sections = lane_sections(road)
            if any(next(lane for side in ("left", "right")
                        for lane in sections[index][2].findall(side + "/lane")
                        if int(lane.get("id")) == lid).get("type") != "shoulder"
                   for index, lid in members):
                report["reason"] = "lane_chain_not_shoulder"
                continue
            terminal = endpoint_lanes(road, 1 - end).get(endpoints[1 - end])
            terminal_link = terminal.find("link/successor" if end == 0 else "link/predecessor") if terminal is not None else None
            if terminal_link is None:
                report["reason"] = "missing_outgoing_lane_link"
                continue
            outgoing_id = int(terminal_link.get("id"))
            in_group = target_group(start[0], incoming_end, incoming_direction, from_id)
            out_group = target_group(finish[0], outgoing_end, outgoing_direction, outgoing_id)
            if in_group is None or out_group is None:
                report["reason"] = "no_matching_traffic_direction"
                continue
            old_groups = source.groups.get((rid, direction), [])
            if any(to_id in group.get("end_lids", ((), ()))[end] for group in old_groups):
                report.update(status="already_allowed", reason="included_by_lane_types")
                continue
            targets = [None, None]
            targets[end] = (start[0], incoming_end, incoming_direction, in_group)
            targets[1 - end] = (finish[0], outgoing_end, outgoing_direction, out_group)
            key = rid, direction, tuple(targets)
            partition = partitions.setdefault(key, {"members": set(), "references": {}, "reports": []})
            partition["members"].update(members)
            partition["references"][reference.key] = reference
            partition["reports"].append(report)

    for (rid, direction, targets), partition in partitions.items():
        road = source.roads[rid]
        side = direction if road.el.get("rule", "RHT") == "LHT" else -direction
        groups = side_lane_groups(road, side, {"shoulder"}, split=False, direction=direction,
                                  lane_membership=partition["members"], direction_override=direction)
        group = groups[0] if groups else None
        points = group["pts"] if group is not None else []
        if not groups or not is_uturn_geometry(points):
            for report in partition["reports"]:
                report["reason"] = "source_not_uturn_curve"
            continue
        group.update(shoulder_uturn=True, lane_membership=partition["members"],
                     endpoint_targets=targets, junction_refs=tuple(partition["references"].values()))
        source.groups.setdefault((rid, direction), []).append(group)
        for report in partition["reports"]:
            report.update(status="retained", reason="directed_shoulder_uturn",
                          endpoint_targets=targets)
    return reports


def _partition_auxiliary_connectors(root, roads, side_pts, conn_flag, lane_types, lane_ramp, road_links=None):
    """将接入全宽辅道的连接线绑定到该车道组；混合连接路按两端来源分组。

    只有源 lane/link 或 junction/laneLink 指向辅道时才建立独立端口，收窄端继续
    复用主组。连接路跨段跟踪车道重编号，按整条轨迹分组，防止主路与辅道被串接。
    返回需独立的 (road_id, end, direction, group_index)，内部元数据不写入输出字段。
    """
    separated = set()
    for (rid, direction), groups in list(side_pts.items()):
        if not conn_flag[rid] or not groups:
            continue
        road = roads[rid]
        declarations = (road_links or {}).get(rid, road_link(road.el))
        labels, references = [{}, {}], [None, None]
        for tag, end in (("predecessor", 0), ("successor", 1)):
            link = declarations.get(tag)
            if not link or link[1] != "road" or link[0] not in roads:
                continue
            other_end = 0 if link[2] == "start" else 1
            other_direction = direction if end != other_end else -direction
            other_groups = side_pts.get((link[0], other_direction), [])
            if len(other_groups) < 2:
                continue
            target = {lid: gi for gi, group in enumerate(other_groups)
                      for lid in (group["end_lids"][other_end] if gi == 0 else group["end_wide_lids"][other_end])}
            linked = _end_lane_links(root, road, end, link[0])
            own_ids = set(groups[0]["end_lids"][end])
            labels[end] = {lid: target[other_lid] for lid, other_lid in linked.items()
                           if lid in own_ids and other_lid in target}
            references[end] = (link[0], other_end, other_direction)
        if not any(gi > 0 for endpoint in labels for gi in endpoint.values()):
            continue
        partitions = defaultdict(set)
        covered = set()
        section_count = len(lane_sections(road))
        for end in (0, 1):
            index = 0 if end == 0 else section_count - 1
            for lid in groups[0]["end_lids"][end]:
                if (index, lid) in covered:
                    continue
                members, endpoints = _lane_chain(road, end, lid)
                key = tuple(labels[e].get(endpoints[e], 0) for e in (0, 1))
                partitions[key].update(members)
                covered.update(members)
        replacement = []
        for key, members in sorted(partitions.items()):
            side = direction if road.el.get("rule", "RHT") == "LHT" else -direction
            result = side_lane_groups(road, side, lane_types, lane_ramp, split=False,
                                      direction=direction, lane_membership=members)
            if not result:
                continue
            group = result[0]
            group["end_target_groups"] = key
            group["lane_membership"] = members
            replacement.append(group)
            for end, gi in enumerate(key):
                if gi > 0 and references[end] is not None:
                    separated.add((*references[end], gi))
        if replacement:
            side_pts[(rid, direction)] = replacement
    return separated


def _merge_turns(links):
    """同一个转向只留最中间的一条。

    CARLA 的 junction 里, 一个"进口路 -> 出口路"的转向常被拆成好几条连接路
    (每条一个车道对), 画出来就是一束平行线。它们的起止节点完全相同, 去掉
    多余的不会破坏连通性, 只是让图面从"一束"变成"一条"。
    返回 (新 DataFrame, 合并掉的条数)。
    """
    groups = defaultdict(list)
    for i, r in links[links.link_type == "connector"].iterrows():
        groups[(r.junction_id, r.from_node, r.to_node, bool(r.get("_is_uturn", False)))].append(i)

    drop, n_merged = [], 0
    for _, idxs in sorted(groups.items(), key=lambda kv: min(kv[1])):
        if len(idxs) < 2:
            continue
        keep = idxs[_mid_index([links.at[i, "geometry"] for i in idxs])]
        # n_merged = 合并掉的条数(该转向原有 n_merged + 1 条平行连接路)
        links.at[keep, "n_merged"] = len(idxs) - 1
        links.at[keep, "lanes"] = max(links.at[i, "lanes"] for i in idxs)
        if "_junction_refs" in links:
            references = {}
            for index in idxs:
                for reference in links.at[index, "_junction_refs"]:
                    references[reference.key] = reference
            links.at[keep, "_junction_refs"] = tuple(references.values())
        drop.extend(i for i in idxs if i != keep)
        n_merged += len(idxs) - 1
    if drop:
        links = links.drop(index=drop).reset_index(drop=True)
    return links, n_merged
