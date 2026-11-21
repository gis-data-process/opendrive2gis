#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读校验自研转换产物；缺失产物或失败检查以非零状态退出。

默认检查全部有源要素的图层。只导出部分图层时，用 --layers 指定相同集合。
几何统计采用平面长度/面积，不能代替道路通行规则和完整三维曲面的验证。
"""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import sqlite3
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
import pyogrio
from shapely import get_coordinates
from shapely.geometry import Point

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.paths import DATA, OUT  # noqa: E402
from own.xodr2gpkg import ALL_LAYERS, LAYER_FIELDS, parse_georeference  # noqa: E402


def source_record(el, exclude=()):
    """独立读取 XML 来源树，保留属性文本、有效节点文本和子节点顺序。"""
    return {"tag": el.tag, "attributes": dict(el.attrib), "text": (el.text or "").strip(),
            "children": [source_record(child) for child in el if child.tag not in exclude]}


def attribute_errors(root, name, frame):
    """逐要素核对源值和嵌套记录；不调用生成器的属性构造函数求期望值。

    源节点通过 road/段/lane 或 road/点下标关联，避免跨段重号、重复 point id
    被合并。NULL 与源缺失匹配，JSON 以解析后的结构比较，不依赖排版。
    """
    errors = []
    roads = {el.get("id"): el for el in root.findall("road")}
    junctions = {el.get("id"): el for el in root.findall("junction")}
    sections = {rid: sorted(road.findall("lanes/laneSection"), key=lambda e: float(e.get("s", "0")))
                for rid, road in roads.items()}
    seen = set()

    def issue(label):
        errors.append(label)

    def compare(row, field, expected, prefix, as_json=False):
        actual = row.get(field)
        if as_json:
            try:
                ok = json.loads(actual) == expected if isinstance(actual, str) else False
            except (ValueError, TypeError):
                ok = False
        elif expected is None:
            ok = pd.isna(actual)
        elif isinstance(expected, (int, float)):
            try:
                ok = not pd.isna(actual) and np.isclose(float(actual), expected, rtol=1e-12, atol=1e-12)
            except (ValueError, TypeError):
                ok = False
        else:
            ok = not pd.isna(actual) and str(actual) == expected
        if not ok:
            issue(prefix + "/" + field)

    def compare_source(row, el, prefix, exclude=(), attr_field="source_attributes_json", child_field="source_children_json"):
        compare(row, attr_field, dict(el.attrib), prefix, True)
        compare(row, child_field, source_record(el, exclude)["children"], prefix, True)

    def compare_records(row, el, mapping, prefix):
        for field, path in mapping.items():
            compare(row, field, [source_record(child) for child in el.findall(path)], prefix, True)

    def active(records, key, position):
        candidates = [el for el in records if float(el.get(key, "0")) <= position + 1e-9]
        return max(enumerate(candidates), key=lambda p: (float(p[1].get(key, "0")), p[0]))[1] if candidates else None

    def speed(row, road, lane, s, prefix):
        local = active(lane.findall("speed"), "sOffset", 0.0) if lane is not None else None
        rtype = active(road.findall("type"), "s", s)
        inherited = rtype.find("speed") if rtype is not None else None

        def value(el):
            if el is None:
                return None
            try:
                raw = float(el.get("max", "nan"))
            except ValueError:
                return None
            if not np.isfinite(raw) or raw < 0:
                return None
            unit = el.get("unit", "m/s").lower()
            if unit not in {"m/s", "km/h", "mph"}:
                issue(prefix + "/源限速单位")
                return None
            return raw * {"m/s": 1.0, "km/h": 1 / 3.6, "mph": .44704}[unit]

        expected = value(local)
        record, origin = local, "lane" if local is not None else None
        if expected is None and inherited is not None:
            expected, record, origin = value(inherited), inherited, "road"
        compare(row, "speed", expected, prefix)
        compare(row, "speed_max", record.get("max") if record is not None else None, prefix)
        compare(row, "speed_unit", record.get("unit") if record is not None else None, prefix)
        compare(row, "speed_source", origin, prefix)

    # 引用可位于其他 road；完整记录同时保留引用发生的位置。
    references = {"signal": {}, "object": {}}
    for road in roads.values():
        for kind, path in (("signal", "signals/signalReference"), ("object", "objects/objectReference")):
            for i, el in enumerate(road.findall(path)):
                references[kind].setdefault(el.get("id"), []).append({
                    "road_id": road.get("id"), "reference_index": i, "source": source_record(el)})

    for row in frame.to_dict("records"):
        prefix = name + "#" + str(len(seen))
        if name == "JunctionLink":
            junc = junctions.get(row.get("junction_id"))
            if junc is None:
                issue(prefix + "/源 junction 关联")
                continue
            matches = [el for el in junc.findall("connection") if el.get("id") == row.get("connection_id")]
            if len(matches) != 1:
                issue(prefix + "/源 connection 关联")
                continue
            conn = matches[0]
            compare(row, "junction_name", junc.get("name", ""), prefix)
            compare(row, "junction_type", junc.get("type"), prefix)
            compare_source(row, junc, prefix, ("connection",), "junction_attributes_json", "junction_children_json")
            compare_source(row, conn, prefix, attr_field="connection_attributes_json", child_field="connection_children_json")
            for field, attr in {"incoming_road": "incomingRoad", "connecting_road": "connectingRoad",
                                "linked_road": "linkedRoad", "connection_type": "type"}.items():
                compare(row, field, conn.get(attr) if field in ("linked_road", "connection_type") else conn.get(attr, ""), prefix)
            compare(row, "contact_point", conn.get("contactPoint", "start"), prefix)
            links = conn.findall("laneLink")
            inferred = not links
            compare(row, "is_inferred", int(inferred), prefix)
            key = (junc.get("id"), conn.get("id"), row.get("lane_link_index"), row.get("to_lane"))
            if key in seen:
                issue(prefix + "/重复关联键")
            seen.add(key)
            if inferred:
                compare(row, "lane_link_index", None, prefix)
                compare(row, "from_lane", None, prefix)
                compare(row, "source_attributes_json", {}, prefix, True)
                compare(row, "source_children_json", [], prefix, True)
            else:
                index = row.get("lane_link_index")
                if pd.isna(index) or int(index) != index or not 0 <= int(index) < len(links):
                    issue(prefix + "/laneLink 下标")
                    continue
                el = links[int(index)]
                compare(row, "from_lane", el.get("from"), prefix)
                compare(row, "to_lane", int(float(el.get("to"))), prefix)
                compare_source(row, el, prefix)
            continue

        rid = row.get("road_id")
        road = roads.get(rid)
        if road is None:
            issue(prefix + "/源 road 关联")
            continue
        compare(row, "road_name", road.get("name"), prefix)
        compare(row, "junction", road.get("junction"), prefix)
        compare(row, "road_rule", road.get("rule"), prefix)
        if name in ("ReferenceLine", "CenterLane"):
            s = 0.0
            key = (rid,)
            compare(row, "length", float(road.get("length")), prefix)
            compare_source(row, road, prefix, ("lanes", "signals", "objects"))
            for tag in ("predecessor", "successor"):
                el = road.find("link/" + tag)
                for suffix, attr in (("_id", "elementId"), ("_type", "elementType"), ("_contact_point", "contactPoint")):
                    compare(row, tag + suffix, el.get(attr) if el is not None else None, prefix)
            compare_records(row, road, {
                "road_types_json": "type", "road_link_json": "link", "lane_offsets_json": "lanes/laneOffset",
                "elevations_json": "elevationProfile/elevation", "lateral_profile_json": "lateralProfile",
                "signal_references_json": "signals/signalReference", "object_references_json": "objects/objectReference",
                "user_data_json": "userData",
            }, prefix)
            center = [{"lane_section": i, "s_start": float(sec.get("s", "0")),
                       "s_end": float(sections[rid][i + 1].get("s")) if i + 1 < len(sections[rid]) else float(road.get("length")),
                       "source": source_record(el)}
                      for i, sec in enumerate(sections[rid]) for el in sec.findall("center/lane")]
            compare(row, "center_lanes_json", center, prefix, True)
            speed(row, road, None, 0.0, prefix)
        elif name in ("Lane", "LaneBorder", "LaneCenter"):
            index = row.get("lane_section")
            if pd.isna(index) or int(index) != index or not 0 <= int(index) < len(sections[rid]):
                issue(prefix + "/laneSection 下标")
                continue
            sec = sections[rid][int(index)]
            s = float(sec.get("s", "0"))
            end = float(sections[rid][int(index) + 1].get("s")) if int(index) + 1 < len(sections[rid]) else float(road.get("length"))
            side, lid = row.get("side"), row.get("lane_id")
            matches = [el for el in sec.findall(str(side) + "/lane") if not pd.isna(lid) and int(float(el.get("id"))) == lid]
            if len(matches) != 1 or side not in ("left", "right"):
                issue(prefix + "/源 lane 关联")
                continue
            el = matches[0]
            key = (rid, int(index), side, int(lid))
            compare(row, "s_start", s, prefix)
            compare(row, "s_end", end, prefix)
            compare(row, "road_length", float(road.get("length")), prefix)
            compare(row, "single_side", sec.get("singleSide"), prefix)
            compare(row, "lane_section_attributes_json", dict(sec.attrib), prefix, True)
            compare(row, "lane_section_children_json", source_record(sec, ("left", "center", "right"))["children"], prefix, True)
            for field, attr in (("lane_type", "type"), ("level", "level"), ("direction", "direction")):
                compare(row, field, el.get(attr), prefix)
            compare_source(row, el, prefix)
            compare_records(row, el, {
                "widths_json": "width", "borders_json": "border", "speeds_json": "speed", "road_marks_json": "roadMark",
                "heights_json": "height", "materials_json": "material", "visibilities_json": "visibility",
                "accesses_json": "access", "rules_json": "rule", "lane_link_json": "link", "user_data_json": "userData",
            }, prefix)
            for tag in ("predecessor", "successor"):
                link = el.find("link/" + tag)
                compare(row, tag + "_lane", int(float(link.get("id"))) if link is not None else None, prefix)
            speed(row, road, el, s, prefix)
        else:
            kind = "signal" if name == "RoadSignal" else "object"
            path = "signals/signal" if kind == "signal" else "objects/object"
            index = row.get(kind + "_index")
            nodes = road.findall(path)
            if pd.isna(index) or int(index) != index or not 0 <= int(index) < len(nodes):
                issue(prefix + "/源点下标")
                continue
            el = nodes[int(index)]
            s = float(el.get("s", "0"))
            key = (rid, int(index))
            compare(row, kind + "_id", el.get("id"), prefix)
            compare(row, "road_length", float(road.get("length")), prefix)
            strings = {"name": "name", "type": "type", "subtype": "subtype", "dynamic": "dynamic", "orientation": "orientation"}
            numbers = {"s": "s", "t": "t", "z_offset": "zOffset", "pitch": "pitch", "roll": "roll", "height": "height", "width": "width"}
            if kind == "signal":
                strings.update({"country": "country", "country_revision": "countryRevision", "unit": "unit", "text": "text"})
                numbers.update({"h_offset": "hOffset", "value": "value"})
            else:
                numbers.update({"hdg": "hdg", "length": "length", "radius": "radius", "valid_length": "validLength"})
            for field, attr in strings.items():
                compare(row, field, el.get(attr), prefix)
            for field, attr in numbers.items():
                raw = el.get(attr)
                compare(row, field, float(raw) if raw not in (None, "") else None, prefix)
            compare_source(row, el, prefix)
            mapping = {"validity_json": "validity", "user_data_json": "userData"}
            if kind == "signal":
                mapping.update({"dependencies_json": "dependency", "references_json": "reference",
                                "positions_road_json": "positionRoad", "positions_inertial_json": "positionInertial"})
            else:
                mapping.update({"repeats_json": "repeat", "materials_json": "material", "parking_spaces_json": "parkingSpace",
                                "borders_json": "borders", "markings_json": "markings"})
                outlines = [child for child in el if child.tag == "outline"] + el.findall("outlines/outline")
                compare(row, "outlines_json", [source_record(child) for child in outlines], prefix, True)
            compare_records(row, el, mapping, prefix)
            compare(row, kind + "_references_json", references[kind].get(el.get("id"), []), prefix, True)
        rtype = active(road.findall("type"), "s", s)
        compare(row, "road_type", rtype.get("type") if rtype is not None else None, prefix)
        if key in seen:
            issue(prefix + "/重复关联键")
        seen.add(key)
    return errors


def src_counts(xodr):
    """从 XML 统计输出契约；全段零宽的占位车道不要求面/线要素。

    这里只统计 XML 记录，不调用几何构造，避免把生成器自己的错误当成期望值。
    border 记录是外边界距离，不能依据“无 width”认定源 XML 无效。
    """
    root = ET.parse(xodr).getroot()
    roads = root.findall("road")
    n_lane = n_zero = 0
    for road in roads:
        sections = sorted(road.findall("lanes/laneSection"), key=lambda e: float(e.get("s", "0")))
        for i, section in enumerate(sections):
            sa = float(section.get("s", "0"))
            sb = float(sections[i + 1].get("s")) if i + 1 < len(sections) else float(road.get("length"))
            length = sb - sa
            # 独立从 XML 取记录分界和多项式极值，不复用生成器的几何或计数。
            samples = {0.0, length}
            for el in section.findall(".//width") + section.findall(".//border"):
                start = float(el.get("sOffset", "0"))
                samples.add(min(max(start, 0), length))
                b, c, d = [float(el.get(a, "0")) for a in ("b", "c", "d")]
                for value in np.roots([3 * d, 2 * c, b]) if (b or c or d) else []:
                    if abs(value.imag) < 1e-10 and 0 < start + value.real < length:
                        samples.add(float(start + value.real))
            points = np.array(sorted(samples), dtype=float)

            def values(records):
                result = np.zeros(len(points))
                ordered = sorted(records, key=lambda e: float(e.get("sOffset", "0")))
                for j, point in enumerate(points):
                    # 只在末端取左记录；刚好从本段末端开始的 width 不代表本段有宽度。
                    active = [e for e in ordered if float(e.get("sOffset", "0")) < point
                              or (point < length and float(e.get("sOffset", "0")) == point)]
                    if not active:
                        continue
                    el = active[-1]
                    delta = point - float(el.get("sOffset", "0"))
                    a, b, c, d = [float(el.get(key, "0")) for key in ("a", "b", "c", "d")]
                    result[j] = a + delta * (b + delta * (c + delta * d))
                return result

            for tag in ("left", "right"):
                cumulative = np.zeros(len(points))
                for lane in sorted(section.findall(tag + "/lane"), key=lambda e: abs(int(e.get("id")))):
                    width = values(lane.findall("width")) if lane.find("width") is not None else np.abs(values(lane.findall("border"))) - cumulative
                    width = np.maximum(width, 0)
                    cumulative += width
                    if len(width) and width.max() >= 1e-6:
                        n_lane += 1
                    else:
                        n_zero += 1
    return {
        "ReferenceLine": len(roads), "CenterLane": len(roads),
        "Lane": n_lane, "LaneBorder": n_lane, "LaneCenter": n_lane,
        "RoadSignal": sum(len(r.findall("signals/signal")) for r in roads),
        "RoadObject": sum(len(r.findall("objects/object")) for r in roads),
        "JunctionLink": sum(len(c.findall("laneLink")) for j in root.findall("junction") for c in j.findall("connection")),
        "zero_width_lanes": n_zero,
    }


def lane_endpoint_errors(root, name, frame, tolerance=.01):
    """独立按源 XML 计算车道段首/末横断面，检测有效面中的错位末点与尖刺。

    不调用 build_lane_geometry、pick 或 lane_width_array；末端严格选择边界左侧的
    offset/width/border，仍在精确里程求值。planView 用解析 line/arc/paramPoly3 计算，
    极少量 spiral/poly3 仅用通用参考线解析取得坐标，横断面与记录归属独立计算。
    默认 XY 允许 1 cm：源 planView 的相邻记录有毫米级接缝残差，不能误报为米级尖刺。
    返回错误明细及最大 XY 误差，不把 laneSection 不连续的源数据强制缝合。
    """
    roads = {road.get("id"): road for road in root.findall("road")}
    section_cache, positions, cross_sections, reference_fallback = {}, {}, {}, {}
    offset = root.find("header/offset")
    ox, oy, rotation = (tuple(float(offset.get(key, "0")) for key in ("x", "y", "hdg"))
                        if offset is not None else (0.0, 0.0, 0.0))

    def polynomial(record, distance):
        """独立 Horner 求值，保留边界左记录在精确末端的多项式值。"""
        a, b, c, d = [float(record.get(key, "0")) for key in ("a", "b", "c", "d")]
        return a + distance * (b + distance * (c + distance * d))

    def value(records, key, position, left):
        allowed = [record for record in records
                   if float(record.get(key, "0")) < position
                   or (not left and float(record.get(key, "0")) == position)]
        if not allowed:
            return 0.0
        record = max(enumerate(allowed), key=lambda pair: (float(pair[1].get(key, "0")), pair[0]))[1]
        return polynomial(record, position - float(record.get(key, "0")))

    def reference(road, position):
        """在源 planView 精确里程取参考点和航向，不使用车道构造器的末点。"""
        cache_key = (road.get("id"), position)
        if cache_key in positions:
            return positions[cache_key]
        geometries = sorted(road.findall("planView/geometry"), key=lambda el: float(el.get("s")))
        geom = max((geom for geom in geometries if float(geom.get("s")) <= position),
                   key=lambda el: float(el.get("s")), default=geometries[0])
        distance = min(max(position - float(geom.get("s")), 0.0), float(geom.get("length")))
        x0, y0, heading = [float(geom.get(key)) for key in ("x", "y", "hdg")]
        child = next((el for el in geom if el.tag in ("line", "arc", "spiral", "poly3", "paramPoly3")), None)
        if child is None:
            raise ValueError("源 planView 缺少受支持的几何")
        if child.tag == "line":
            u, v, turn = distance, 0.0, 0.0
        elif child.tag == "arc":
            curvature = float(child.get("curvature"))
            turn = curvature * distance
            u, v = ((math.sin(turn) / curvature, (1 - math.cos(turn)) / curvature)
                    if abs(curvature) > 1e-12 else (distance, 0.0))
        elif child.tag == "paramPoly3":
            parameter = distance / float(geom.get("length")) if child.get("pRange", "arcLength").lower() == "normalized" else distance
            au, bu, cu, du = [float(child.get(key, "0")) for key in ("aU", "bU", "cU", "dU")]
            av, bv, cv, dv = [float(child.get(key, "0")) for key in ("aV", "bV", "cV", "dV")]
            u = au + parameter * (bu + parameter * (cu + parameter * du))
            v = av + parameter * (bv + parameter * (cv + parameter * dv))
            turn = math.atan2(bv + 2 * cv * parameter + 3 * dv * parameter ** 2,
                              bu + 2 * cu * parameter + 3 * du * parameter ** 2)
        else:
            from own.xodr2gpkg import Road
            rid = road.get("id")
            if rid not in reference_fallback:
                reference_fallback[rid] = Road(road, step=.1)
            sampled = reference_fallback[rid]
            x, y, _ = sampled.point_at(position)
            turn = math.atan2(float(np.interp(position, sampled.S, sampled.SIN)),
                              float(np.interp(position, sampled.S, sampled.COS)))
            result = (ox + x * math.cos(rotation) - y * math.sin(rotation),
                      oy + x * math.sin(rotation) + y * math.cos(rotation), turn + rotation)
            positions[cache_key] = result
            return result
        x = x0 + u * math.cos(heading) - v * math.sin(heading)
        y = y0 + u * math.sin(heading) + v * math.cos(heading)
        result = (ox + x * math.cos(rotation) - y * math.sin(rotation),
                  oy + x * math.sin(rotation) + y * math.cos(rotation), heading + turn + rotation)
        positions[cache_key] = result
        return result

    errors, maximum = [], 0.0
    for row in frame.itertuples(index=False):
        rid = str(row.road_id)
        road = roads.get(rid)
        if road is None:
            errors.append(rid + "/缺少源 road")
            continue
        if rid not in section_cache:
            section_cache[rid] = sorted(road.findall("lanes/laneSection"), key=lambda el: float(el.get("s")))
        sections = section_cache[rid]
        index = int(row.lane_section)
        if not 0 <= index < len(sections):
            errors.append(rid + "/laneSection 下标")
            continue
        section = sections[index]
        start = float(section.get("s"))
        end = float(sections[index + 1].get("s")) if index + 1 < len(sections) else float(road.get("length"))
        lid, side = int(row.lane_id), row.side
        key = (rid, index, side)
        if key not in cross_sections:
            group = sorted(section.findall(side + "/lane"), key=lambda lane: abs(int(lane.get("id"))))
            endpoints = {}
            for position, left in ((start, False), (end, True)):
                base = value(road.findall("lanes/laneOffset"), "s", position, left)
                cumulative = 0.0
                sign = 1 if side == "left" else -1
                for lane in group:
                    inner = base + sign * cumulative
                    widths = lane.findall("width")
                    width = (value(widths, "sOffset", position - start, left) if widths
                             else abs(value(lane.findall("border"), "sOffset", position - start, left)) - abs(inner))
                    width = max(width, 0.0)
                    outer = inner + sign * width
                    endpoints.setdefault(int(lane.get("id")), []).append((inner, (inner + outer) / 2, outer))
                    cumulative += width
            cross_sections[key] = endpoints
        expected = cross_sections[key].get(lid)
        if expected is None:
            errors.append(f"{rid}/{index}/{lid}/缺少源车道")
            continue
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            errors.append(f"{rid}/{index}/{lid}/缺少几何")
            continue
        for endpoint, position in enumerate((start, end)):
            x, y, heading = reference(road, position)
            inner, center, outer = expected[endpoint]

            def point(t):
                return Point(x - t * math.sin(heading), y + t * math.cos(heading))

            if name == "Lane":
                # 封口内外端和中点均应落在面边界；不把附近的纵向侧边当作正确末点。
                distance = max(point(inner).distance(geometry.boundary), point(outer).distance(geometry.boundary), point(center).distance(geometry.boundary))
            else:
                target = point(outer if name == "LaneBorder" else center)
                distance = Point(geometry.coords[0 if endpoint == 0 else -1][:2]).distance(target)
            maximum = max(maximum, distance)
            if distance > tolerance:
                errors.append(f"{rid}/{index}/{lid}/{'首' if endpoint == 0 else '末'}点偏差 {distance:.6f} m")
    return errors, maximum


def validate(xodr, gpkg, layers=None):
    """返回各项检查的布尔结果；任何错误都成为失败项而非静默跳过。

    CRS 与源 geoReference 比较；没有 geoReference 的局部坐标允许没有 CRS。
    图层无源要素时允许不建表，支持 sample 等没有信号和物体的合法文件。
    """
    selected = set(layers or ALL_LAYERS)
    root = ET.parse(xodr).getroot()
    expected = src_counts(xodr)
    source_crs, _ = parse_georeference(root)
    print(f"\n{Path(xodr).name} -> {Path(gpkg).name}")
    results = []

    def record(label, ok, detail=""):
        results.append(bool(ok))
        print(f"  [{'OK' if ok else 'FAIL'}] {label}{': ' + detail if detail else ''}")

    if not Path(gpkg).is_file():
        record("产物存在", False, str(gpkg))
        return results
    names = {name for name, _ in pyogrio.list_layers(gpkg)}
    frames = {name: pyogrio.read_dataframe(gpkg, layer=name) for name in selected & names}
    with sqlite3.connect(Path(gpkg).resolve().as_uri() + "?mode=ro", uri=True) as con:
        sql_types = {name: {row[1]: row[2].upper() for row in con.execute(f'PRAGMA table_info("{name}")')}
                     for name in frames}
    # sqlite3 的事务上下文不会关闭连接，Windows 下必须在后续读写前显式释放句柄。
    con.close()
    if frames:
        metadata = pyogrio.read_info(gpkg, layer=next(iter(frames)))["dataset_metadata"] or {}
        expected_metadata = {
            "xodr_document_json": source_record(root, ("road", "junction")),
            "xodr_roads_json": [source_record(el) for el in root.findall("road")],
            "xodr_junctions_json": [source_record(el) for el in root.findall("junction")],
        }
        metadata_errors = []
        for key, expected_value in expected_metadata.items():
            try:
                if json.loads(metadata.get(key, "")) != expected_value:
                    metadata_errors.append(key)
            except (ValueError, TypeError):
                metadata_errors.append(key)
        record("完整 XML 属性来源元数据", not metadata_errors, ", ".join(metadata_errors))

    for name in ALL_LAYERS:
        if name not in selected:
            continue
        count = len(frames[name]) if name in frames else 0
        # connection 没有 laneLink 时，生成器允许按连接路接触端车道导出。
        count_ok = count == expected[name]
        if name == "JunctionLink" and any(not c.findall("laneLink") for j in root.findall("junction") for c in j.findall("connection")):
            count_ok = count >= expected[name]
        record(name + " 要素数", count_ok, f"输出 {count} / 源 {expected[name]}")
        if name not in frames:
            continue
        df = frames[name]
        fields = LAYER_FIELDS[name]
        missing = sorted(set(fields) - set(df.columns))
        record(name + " 属性字段完整", not missing, ", ".join(missing) if missing else f"{len(fields)} 个属性字段")
        expected_types = {"string": "TEXT", "Float64": "REAL", "Int64": "INTEGER"}
        bad_types = [field for field, kind in fields.items()
                     if field in sql_types[name] and sql_types[name][field] != expected_types[kind]]
        record(name + " 属性类型", not missing and not bad_types, ", ".join(bad_types))
        errors = attribute_errors(root, name, df) if not missing else ["先补齐缺失字段"]
        record(name + " 源属性与记录保留", not errors,
               f"{len(errors)} 处不一致；" + ", ".join(errors[:5]) if errors else f"{len(df)} 个要素与 XML 对应")
        actual_crs = df.crs
        same_crs = ((actual_crs is None or actual_crs.name.startswith("Undefined")) if source_crs is None
                    else actual_crs is not None and actual_crs.equals(source_crs, ignore_axis_order=True))
        record(name + " CRS", same_crs)
        geom = df.geometry
        null = int(geom.isna().sum())
        empty = int(geom.is_empty.sum())
        invalid = int((~geom.is_valid).sum())
        record(name + " 几何", null == 0 and empty == 0 and invalid == 0,
               f"NULL {null} / 空 {empty} / 无效 {invalid}")
        coords = get_coordinates(geom.to_numpy(), include_z=True)
        xy_ok = bool(len(coords)) and np.isfinite(coords[:, :2]).all()
        z_values = coords[:, 2] if len(coords) else np.array([])
        # 2D 坐标的 Z 是 NaN；只有实际带 Z 的几何才要求 Z 全部有限。
        has_z = bool(any(g is not None and g.has_z for g in geom))
        finite = xy_ok and (not has_z or np.isfinite(z_values).all())
        record(name + " 坐标有限", finite)
        if name in ("Lane", "LaneBorder", "LaneCenter") and not missing:
            endpoint_errors, max_endpoint_error = lane_endpoint_errors(root, name, df)
            record(name + " 段端横断面与源记录一致", not endpoint_errors,
                   f"最大误差 {max_endpoint_error:.6f} m；异常 {len(endpoint_errors)} 处"
                   + ("；" + ", ".join(endpoint_errors[:3]) if endpoint_errors else ""))

    if "ReferenceLine" in frames and "length" in frames["ReferenceLine"]:
        ref = frames["ReferenceLine"]
        declared = ref["length"].astype(float).to_numpy()
        measured = ref.geometry.length.to_numpy()
        error = np.abs(measured - declared) / np.maximum(declared, 1e-12)
        record("参考线长度误差 < 1%", bool(len(error)) and np.isfinite(error).all() and error.max() < .01,
               f"最大 {float(error.max()) * 100:.5f}%" if len(error) else "无参考线")

    if "Lane" in frames and {"s_start", "s_end"} <= set(frames["Lane"].columns):
        lane = frames["Lane"]
        areas = lane.geometry.area.to_numpy()
        lengths = (lane.s_end - lane.s_start).to_numpy()
        average_width = areas / np.maximum(lengths, 1e-12)
        record("车道面面积与平均宽度", bool(len(areas)) and np.isfinite(areas).all()
               and (areas > 0).all() and (lengths > 0).all() and (average_width <= 30).all(),
               f"总面积 {float(np.nansum(areas)):.2f} m²")
        if "LaneCenter" in frames and {"road_id", "lane_id", "s_start", "s_end"} <= set(frames["Lane"].columns) & set(frames["LaneCenter"].columns):
            center = frames["LaneCenter"]
            keys = ["road_id", "lane_section", "side", "lane_id"]
            if not set(keys) <= set(lane.columns) & set(center.columns):
                keys = ["road_id", "lane_id", "s_start", "s_end"]
            pairs = lane.merge(center, on=keys, suffixes=("_lane", "_center"))
            # 使用边界也算在内的 buffer 覆盖，避免 contains 把端点判为面外。
            outside = [line.difference(poly.buffer(1e-5)).length for poly, line in zip(pairs.geometry_lane, pairs.geometry_center)
                       if poly is not None and line is not None]
            unmatched = len(pairs) != len(lane) or len(pairs) != len(center)
            failed = sum(length > 1e-4 for length in outside)
            record("LaneCenter 与 Lane 一一对应并落在面内", not unmatched
                   and bool(outside) and failed == 0,
                   f"超出 {failed} 条，最大 {max(outside):.6f} m" if outside else "无可比较几何")

    print(f"  合计 {sum(results)}/{len(results)} 项通过")
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("xodr", nargs="?")
    ap.add_argument("gpkg", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--layers", nargs="+", choices=ALL_LAYERS)
    ap.add_argument("--dir", default=OUT, help="批量输出所在目录")
    args = ap.parse_args()
    if args.all:
        if args.xodr or args.gpkg:
            ap.error("--all 不能同时指定单个输入/输出")
        pairs = [(p, Path(args.dir) / (p.stem + ".gpkg")) for p in sorted(Path(DATA).glob("Town*.xodr"))]
    elif args.xodr and args.gpkg:
        pairs = [(args.xodr, args.gpkg)]
    else:
        ap.error("指定 XODR 和 GPKG，或 --all")
    if not pairs:
        ap.error("没有待检查的 XODR")
    checks = []
    for src, dst in pairs:
        try:
            checks.extend(validate(src, dst, args.layers))
        except Exception as exc:
            print(f"[FAIL] {src}: {exc}", file=sys.stderr)
            checks.append(False)
    print(f"\n总计: {sum(checks)}/{len(checks)} 项通过")
    return 0 if checks and all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
