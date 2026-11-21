#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""解析常用 OpenDRIVE 元素并写出八类 GIS 图层，默认写到 out/<输入名>.gpkg。

ReferenceLine / CenterLane 为参考线与含 laneOffset 的车道基准线；
Lane / LaneBorder / LaneCenter 为每个 laneSection 的车道面、外边界和中心线；
段末横断面取边界左侧有效记录，段首取右侧记录，几何端点仍使用精确里程。
RoadSignal / RoadObject 为位置锚点，不重建实体轮廓；
JunctionLink 沿接触端车道及跨段 predecessor/successor 关系生成转向轨迹。

支持 line、arc、spiral、poly3、paramPoly3，按步长、航向变化和属性分界采样。
header/offset 的平移旋转应用于全部坐标；CRS 来自 geoReference，不猜测缺失投影。
默认带 Z；--crossfall 仅用 superelevation 对 Z 做简化补偿，不是完整三维道路曲面。
make_valid 后保留最大面片，self_intersect 记录修复；此取舍可能丢失其他面片。
字段按统一 schema 写出，源缺失值为 NULL，完整嵌套属性以 JSON 保留。
没有源要素的层可不建表。XML/参数/声明车道无效或写入失败时抛错，旧 GPKG 保留。

用法（项目根目录）：
    python code/own/xodr2gpkg.py data/Town06.xodr --epsilon 0.5
    python code/own/xodr2gpkg.py --all
    python code/own/validate.py --all

依赖见 requirements.txt；只需 GDAL 的 GPKG 驱动，不依赖官方 XODR 驱动。
完整原理与支持边界见 docs/从opendrive中提取GIS图层的实现思路.md。
"""

import argparse
import json
import math
import os
import sys
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString, Polygon, Point
from shapely import make_valid
import pyogrio
import pyproj

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.files import atomic_output  # noqa: E402
from common.paths import DATA, OUT  # noqa: E402

ALL_LAYERS = ["ReferenceLine", "LaneBorder", "Lane", "RoadSignal", "RoadObject",
               "CenterLane", "LaneCenter", "JunctionLink"]

# 单段参考线允许的最大转角(弧度), 默认 5°
MAX_TURN = math.radians(5.0)

# 显式约定列类型，避免某个文件全为空时被 GDAL 推断为 TEXT。
# 原始 id/枚举/布尔声明保留文本，生成的车道号、下标用整数，物理量用 REAL。
ROAD_FIELDS = {
    "road_id": "string", "road_name": "string", "junction": "string",
    "road_type": "string", "length": "Float64", "road_rule": "string",
    "speed": "Float64", "speed_max": "string", "speed_unit": "string", "speed_source": "string",
    "predecessor_id": "string", "predecessor_type": "string", "predecessor_contact_point": "string",
    "successor_id": "string", "successor_type": "string", "successor_contact_point": "string",
    "road_types_json": "string", "road_link_json": "string", "lane_offsets_json": "string",
    "elevations_json": "string", "lateral_profile_json": "string", "center_lanes_json": "string",
    "signal_references_json": "string", "object_references_json": "string", "user_data_json": "string",
    "source_attributes_json": "string", "source_children_json": "string",
}
ROAD_CONTEXT_FIELDS = {
    "road_id": "string", "road_name": "string", "junction": "string",
    "road_type": "string", "road_rule": "string", "road_length": "Float64",
}
LANE_FIELDS = {
    **ROAD_CONTEXT_FIELDS,
    "lane_section": "Int64", "single_side": "string", "lane_id": "Int64", "side": "string",
    "lane_type": "string", "level": "string", "direction": "string", "s_start": "Float64", "s_end": "Float64",
    "speed": "Float64", "speed_max": "string", "speed_unit": "string", "speed_source": "string",
    "predecessor_lane": "Int64", "successor_lane": "Int64",
    "widths_json": "string", "borders_json": "string", "speeds_json": "string", "road_marks_json": "string",
    "heights_json": "string", "materials_json": "string", "visibilities_json": "string",
    "accesses_json": "string", "rules_json": "string", "lane_link_json": "string", "user_data_json": "string",
    "lane_section_attributes_json": "string", "lane_section_children_json": "string",
    "source_attributes_json": "string", "source_children_json": "string",
}
SIGNAL_ATTRIBUTES = {
    "name": ("name", "string"), "type": ("type", "string"), "subtype": ("subtype", "string"),
    "country": ("country", "string"), "country_revision": ("countryRevision", "string"),
    "dynamic": ("dynamic", "string"), "orientation": ("orientation", "string"),
    "s": ("s", "Float64"), "t": ("t", "Float64"), "z_offset": ("zOffset", "Float64"),
    "h_offset": ("hOffset", "Float64"), "pitch": ("pitch", "Float64"), "roll": ("roll", "Float64"),
    "height": ("height", "Float64"), "width": ("width", "Float64"), "value": ("value", "Float64"),
    "unit": ("unit", "string"), "text": ("text", "string"),
}
OBJECT_ATTRIBUTES = {
    "name": ("name", "string"), "type": ("type", "string"), "subtype": ("subtype", "string"),
    "dynamic": ("dynamic", "string"), "orientation": ("orientation", "string"),
    "s": ("s", "Float64"), "t": ("t", "Float64"), "z_offset": ("zOffset", "Float64"),
    "hdg": ("hdg", "Float64"), "pitch": ("pitch", "Float64"), "roll": ("roll", "Float64"),
    "height": ("height", "Float64"), "width": ("width", "Float64"), "length": ("length", "Float64"),
    "radius": ("radius", "Float64"), "valid_length": ("validLength", "Float64"),
}
SIGNAL_RECORDS = {
    "validity_json": "validity", "dependencies_json": "dependency", "references_json": "reference",
    "positions_road_json": "positionRoad", "positions_inertial_json": "positionInertial",
    "user_data_json": "userData",
}
OBJECT_RECORDS = {
    "validity_json": "validity", "outlines_json": "outline", "repeats_json": "repeat",
    "materials_json": "material", "parking_spaces_json": "parkingSpace", "borders_json": "borders",
    "markings_json": "markings", "user_data_json": "userData",
}
LAYER_FIELDS = {
    "ReferenceLine": ROAD_FIELDS,
    "CenterLane": ROAD_FIELDS,
    "Lane": {**LANE_FIELDS, "self_intersect": "Int64"},
    "LaneBorder": LANE_FIELDS,
    "LaneCenter": LANE_FIELDS,
    "RoadSignal": {
        **ROAD_CONTEXT_FIELDS, "signal_id": "string", "signal_index": "Int64",
        **{field: kind for field, (_, kind) in SIGNAL_ATTRIBUTES.items()},
        **{field: "string" for field in SIGNAL_RECORDS}, "signal_references_json": "string",
        "source_attributes_json": "string", "source_children_json": "string",
    },
    "RoadObject": {
        **ROAD_CONTEXT_FIELDS, "object_id": "string", "object_index": "Int64",
        **{field: kind for field, (_, kind) in OBJECT_ATTRIBUTES.items()},
        **{field: "string" for field in OBJECT_RECORDS}, "object_references_json": "string",
        "source_attributes_json": "string", "source_children_json": "string",
    },
    "JunctionLink": {
        "junction_id": "string", "junction_name": "string", "junction_type": "string",
        "connection_id": "string", "connection_type": "string", "incoming_road": "string",
        "connecting_road": "string", "linked_road": "string", "contact_point": "string",
        "from_lane": "string", "to_lane": "Int64", "lane_ids": "string", "lane_type": "string",
        "lane_link_index": "Int64", "is_inferred": "Int64",
        "junction_attributes_json": "string", "junction_children_json": "string",
        "connection_attributes_json": "string", "connection_children_json": "string",
        "source_attributes_json": "string", "source_children_json": "string",
    },
}


def json_text(value):
    """原始 XML 属性保留字符串和节点顺序；不把枚举或数字文本擅自规范化。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def xml_record(el, exclude=()):
    """把节点及嵌套扩展转为可追溯记录，忽略排版空白，保留有效文本。

    exclude 只过滤当前节点的直接子节点。road/junction 用它避免在每条要素中
    重复其他车道或 connection；单层导出的完整来源另存 GPKG 数据集元数据。
    """
    return {
        "tag": el.tag, "attributes": dict(el.attrib), "text": (el.text or "").strip(),
        "children": [xml_record(child) for child in el if child.tag not in exclude],
    }


def source_fields(el, exclude=()):
    """为要素保留全部直接属性与未排除的子树；源缺失节点用空列表表示。"""
    return {
        "source_attributes_json": json_text(dict(el.attrib)),
        "source_children_json": json_text(xml_record(el, exclude)["children"]),
    }


def records_json(el, path):
    return json_text([xml_record(child) for child in el.findall(path)])


def numeric_attribute(el, name, integer=False):
    """缺失数值保留 NULL，已声明但非法或非有限的值报错，不静默填 0。"""
    raw = el.get(name)
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{el.tag} {el.get('id', '')} 的 {name} 不是有效数值: {raw}") from exc
    if not math.isfinite(value) or (integer and not value.is_integer()):
        raise ValueError(f"{el.tag} {el.get('id', '')} 的 {name} 不是有效{'整数' if integer else '有限数值'}: {raw}")
    return int(value) if integer else value


def mapped_attributes(el, mapping):
    return {field: numeric_attribute(el, name) if kind == "Float64" else el.get(name)
            for field, (name, kind) in mapping.items()}


def road_context(road, s=0.0):
    """公共关联属性；road_type 为该里程的生效类型，不替代完整 type 记录。"""
    active = pick([(f(el, "s"), el) for el in road.el.findall("type")], s)
    return {
        "road_id": road.id, "road_name": road.el.get("name"), "junction": road.el.get("junction"),
        "road_type": active[1].get("type") if active else None,
        "road_rule": road.el.get("rule"), "road_length": road.length,
    }


def speed_fields(road, lane=None, s=0.0, ds=0.0):
    """起点限速与对应原始声明；已知车道值优先，空/未知车道值沿用既有 road 继承规则。"""
    lane_record = pick([(f(el, "sOffset"), el) for el in lane.findall("speed")], ds) if lane is not None else None
    road_type = pick([(f(el, "s"), el) for el in road.el.findall("type")], s)
    road_record = road_type[1].find("speed") if road_type else None
    local = lane_record[1] if lane_record else None
    value = speed_value(local)
    record, origin = local, "lane" if local is not None else None
    if value is None and road_record is not None:
        value, record, origin = speed_value(road_record), road_record, "road"
    return {
        "speed": value, "speed_max": record.get("max") if record is not None else None,
        "speed_unit": record.get("unit") if record is not None else None, "speed_source": origin,
    }


def road_fields(road):
    """道路线层保留上下游、完整 type/剖面及中心 lane 属性，不重复左右车道和点要素。"""
    el = road.el
    values = road_context(road)
    values.pop("road_length")
    values["length"] = road.length
    values.update(speed_fields(road))
    for tag in ("predecessor", "successor"):
        link = el.find("link/" + tag)
        values.update({
            tag + "_id": link.get("elementId") if link is not None else None,
            tag + "_type": link.get("elementType") if link is not None else None,
            tag + "_contact_point": link.get("contactPoint") if link is not None else None,
        })
    for field, path in {
        "road_types_json": "type", "road_link_json": "link", "lane_offsets_json": "lanes/laneOffset",
        "elevations_json": "elevationProfile/elevation", "lateral_profile_json": "lateralProfile",
        "signal_references_json": "signals/signalReference", "object_references_json": "objects/objectReference",
        "user_data_json": "userData",
    }.items():
        values[field] = records_json(el, path)
    values["center_lanes_json"] = json_text([
        {"lane_section": i, "s_start": sa, "s_end": sb, "source": xml_record(lane)}
        for i, (sa, sb, section) in enumerate(lane_sections(road)) for lane in section.findall("center/lane")
    ])
    values.update(source_fields(el, exclude=("lanes", "signals", "objects")))
    return values


def lane_fields(road, lane, section, index, sa, sb, side, lid):
    """三类车道层共享 road/段/lane 关联和来源记录，源缺失可选属性始终保留 NULL。"""
    values = {
        **road_context(road, sa), "lane_section": index, "single_side": section.get("singleSide"),
        "lane_id": lid, "side": side, "lane_type": lane.get("type"), "level": lane.get("level"),
        "direction": lane.get("direction"), "s_start": sa, "s_end": sb, **speed_fields(road, lane, sa),
    }
    for tag in ("predecessor", "successor"):
        link = lane.find("link/" + tag)
        values[tag + "_lane"] = numeric_attribute(link, "id", integer=True) if link is not None else None
    for field, tag in {
        "widths_json": "width", "borders_json": "border", "speeds_json": "speed", "road_marks_json": "roadMark",
        "heights_json": "height", "materials_json": "material", "visibilities_json": "visibility",
        "accesses_json": "access", "rules_json": "rule", "lane_link_json": "link", "user_data_json": "userData",
    }.items():
        values[field] = records_json(lane, tag)
    values["lane_section_attributes_json"] = json_text(dict(section.attrib))
    values["lane_section_children_json"] = json_text(xml_record(section, exclude=("left", "center", "right"))["children"])
    values.update(source_fields(lane))
    return values


def reference_index(root, path):
    """按目标 ID 汇集全文件引用，保留引用所在 road 和全部嵌套有效车道范围。"""
    result = {}
    for road in root.findall("road"):
        for i, el in enumerate(road.findall(path)):
            result.setdefault(el.get("id"), []).append({
                "road_id": road.get("id"), "reference_index": i, "source": xml_record(el),
            })
    return result


def point_fields(road, el, index, kind, references):
    """signal/object 保留直接属性、完整嵌套子树与跨 road 引用，几何仍为位置锚点。"""
    mapping = SIGNAL_ATTRIBUTES if kind == "signal" else OBJECT_ATTRIBUTES
    records = SIGNAL_RECORDS if kind == "signal" else OBJECT_RECORDS
    values = {
        **road_context(road, f(el, "s")), kind + "_id": el.get("id"), kind + "_index": index,
        **mapped_attributes(el, mapping), **source_fields(el),
        kind + "_references_json": json_text(references.get(el.get("id"), [])),
    }
    for field, path in records.items():
        nodes = el.findall(path)
        if kind == "object" and field == "outlines_json":
            nodes = [child for child in el if child.tag == "outline"] + el.findall("outlines/outline")
        values[field] = json_text([xml_record(child) for child in nodes])
    return values


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------

def f(el, attr, default=0.0):
    """安全读取浮点属性"""
    v = el.get(attr)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def poly(coeffs, ds):
    """OpenDRIVE 通用三次多项式  a + b*ds + c*ds^2 + d*ds^3"""
    a, b, c, d = coeffs
    return a + b * ds + c * ds * ds + d * ds ** 3


def coeffs_of(el):
    return (f(el, "a"), f(el, "b"), f(el, "c"), f(el, "d"))


def pick(items, s, left_limit=False):
    """
    从 [(s, value), ...] 中取出 s 之前最近的一项，默认包含 s，left_limit 时严格排除 s。
    记录生效边界不用绝对浮点容差，避免微小间距的下一记录提前覆盖当前段。
    返回 (s_item, value); 若 s 位于所有项之前则返回 None。
    """
    best = None
    best_s = -float("inf")
    for s_i, v in items:
        if (s_i < s if left_limit else s_i <= s) and s_i >= best_s:
            best_s, best = s_i, v
    return (best_s, best) if best is not None else None


# ----------------------------------------------------------------------------
# 平面几何: 单个 <geometry> 的局部 (u, v, 航向增量)
# ----------------------------------------------------------------------------

def spiral_profile(ks, ke, length, nstep=1024):
    """
    螺旋线: 曲率从 ks 线性变化到 ke, 因此航向角是二次函数
        h(p) = ks*p + (ke-ks)*p^2 / (2*L)
    u,v 需要对 cos(h)/sin(h) 积分, 无闭式解(Fresnel), 用梯形法数值积分。
    """
    if not math.isfinite(length) or length <= 0:
        raise ValueError("spiral 的 length 必须为正数")
    # 积分精度独立于输出步长；长螺旋线不能始终只积分 1024 次。
    nstep = max(nstep, int(math.ceil(length * max(abs(ks), abs(ke)) / 0.002)))
    ps = np.linspace(0.0, length, nstep + 1)
    h = ks * ps + (ke - ks) * ps ** 2 / (2.0 * length)
    du = 0.5 * (np.cos(h[1:]) + np.cos(h[:-1])) * np.diff(ps)
    dv = 0.5 * (np.sin(h[1:]) + np.sin(h[:-1])) * np.diff(ps)
    u = np.concatenate([[0.0], np.cumsum(du)])
    v = np.concatenate([[0.0], np.cumsum(dv)])
    return ps, u, v, h


def geometry_local(geom):
    """
    返回函数 local(p) -> (u, v, dh), p 为段内距离 [0, length]
    支持 line / arc / spiral / paramPoly3 / poly3
    """
    length = f(geom, "length")
    child = None
    for c in geom:
        if c.tag in ("line", "arc", "spiral", "paramPoly3", "poly3"):
            child = c
            break
    if child is None:
        raise ValueError("geometry 没有受支持的平面几何元素")

    tag = child.tag

    if tag == "line":
        return (lambda p: (p, 0.0, 0.0)), {}

    if tag == "arc":
        k = f(child, "curvature")
        if abs(k) < 1e-12:
            return (lambda p: (p, 0.0, 0.0)), {}
        def loc(p, k=k):
            dh = k * p
            return math.sin(dh) / k, (1.0 - math.cos(dh)) / k, dh
        return loc, {}

    if tag == "spiral":
        ks, ke = f(child, "curvStart"), f(child, "curvEnd")
        ps, us, vs, hs = spiral_profile(ks, ke, length)
        def loc(p):
            p = min(max(p, 0.0), length)
            return (float(np.interp(p, ps, us)),
                    float(np.interp(p, ps, vs)),
                    float(np.interp(p, ps, hs)))
        return loc, {}

    if tag == "paramPoly3":
        aU, bU, cU, dU = f(child, "aU"), f(child, "bU"), f(child, "cU"), f(child, "dU")
        aV, bV, cV, dV = f(child, "aV"), f(child, "bV"), f(child, "cV"), f(child, "dV")
        normalized = (child.get("pRange", "arcLength").lower() == "normalized")
        scale = (1.0 / length) if (normalized and length > 0) else 1.0
        def loc(p):
            q = p * scale
            u = aU + bU * q + cU * q * q + dU * q ** 3
            v = aV + bV * q + cV * q * q + dV * q ** 3
            du = (bU + 2 * cU * q + 3 * dU * q * q) * scale
            dv = (bV + 2 * cV * q + 3 * dV * q * q) * scale
            return u, v, math.atan2(dv, du)
        return loc, {}

    if tag == "poly3":
        # poly3 定义 v(u)，而传入的 p 是弧长：先积分 ds/du，再反查 u。
        # 直接令 u=p 会把 v=u、length=10 的线错误地画成长 14.14 米。
        a, b, c, d = f(child, "a"), f(child, "b"), f(child, "c"), f(child, "d")
        us = np.linspace(0.0, length, 4097)
        slopes = b + 2 * c * us + 3 * d * us ** 2
        rates = np.hypot(1.0, slopes)
        ss = np.concatenate([[0.0], np.cumsum(0.5 * (rates[:-1] + rates[1:]) * np.diff(us))])
        def loc(p):
            u = float(np.interp(p, ss, us))
            v = a + b * u + c * u * u + d * u ** 3
            dv = b + 2 * c * u + 3 * d * u * u
            return u, v, math.atan(dv)
        return loc, {}

    return (lambda p: (p, 0.0, 0.0)), {}


# ----------------------------------------------------------------------------
# 单条 road 的采样与查询
# ----------------------------------------------------------------------------

def parse_header_offset(root):
    """header/offset 定义局部惯性坐标到投影坐标的平移和绕 Z 轴旋转。"""
    el = root.find("header/offset")
    if el is None:
        return 0.0, 0.0, 0.0, 0.0
    values = tuple(f(el, name) for name in ("x", "y", "z", "hdg"))
    if not all(math.isfinite(v) for v in values):
        raise ValueError("header/offset 含非有限值")
    return values


def _profile_samples(records, start, end):
    """多项式记录的分界和段内极值点，防止短渐变被参考线等距采样漏掉。"""
    points = []
    records = sorted(records, key=lambda item: item[0])
    for i, (s0, co) in enumerate(records):
        s1 = records[i + 1][0] if i + 1 < len(records) else end
        if not start <= s0 <= end:
            continue
        points.append(s0)
        _, b, c, d = co
        roots = np.roots([3 * d, 2 * c, b]) if abs(d) + abs(c) > 0 else []
        for value in roots:
            if abs(float(np.imag(value))) < 1e-12:
                s = s0 + float(np.real(value))
                if s0 < s < min(s1, end):
                    points.append(s)
    return points


class Road:
    """一条 road 的参考线采样 + 高程 + 车道横断面查询"""

    def __init__(self, el, step=1.0, max_turn=MAX_TURN, offset=(0.0, 0.0, 0.0, 0.0)):
        """offset 来自同一 XODR 的 header；所有查询返回已应用偏移的坐标。"""
        if not math.isfinite(step) or step <= 0:
            raise ValueError("采样步长必须为有限正数")
        if not math.isfinite(max_turn) or max_turn < 0:
            raise ValueError("最大转角必须为有限非负数")
        self.el = el
        self.id = el.get("id")
        self.name = el.get("name", "")
        self.junction = el.get("junction", "-1")
        self.length = f(el, "length")
        self.step = step
        self.max_turn = max_turn
        self.offset = offset

        # 道路类型(取第一个)
        t = el.find("type")
        self.road_type = t.get("type", "") if t is not None else ""

        self._sample_reference_line(step)
        self._load_elevation()
        self._load_lateral()

    # -- 参考线 ------------------------------------------------------------
    def _sample_reference_line(self, step):
        pv = self.el.find("planView")
        if pv is None:
            raise ValueError("road %s 缺少 planView" % self.id)

        if not pv.findall("geometry"):
            raise ValueError(f"road {self.id} 的 planView 没有 geometry")
        if not math.isfinite(self.length) or self.length <= 0:
            raise ValueError(f"road {self.id} 的 length 必须为有限正数")
        extra = [0.0, self.length]
        for path in ("elevationProfile/elevation", "lateralProfile/superelevation", "lanes/laneOffset"):
            extra.extend(_profile_samples([(f(e, "s"), coeffs_of(e)) for e in self.el.findall(path)], 0, self.length))
        for sa, sb, lsec in lane_sections(self):
            extra.extend([sa, sb])
            for lane in lsec.findall("left/lane") + lsec.findall("right/lane"):
                for tag in ("width", "border"):
                    records = [(sa + f(e, "sOffset"), coeffs_of(e)) for e in lane.findall(tag)]
                    extra.extend(_profile_samples(records, sa, sb))
                extra.extend(sa + f(e, "sOffset") for e in lane.findall("speed"))
        extra = sorted(set(s for s in extra if 0 <= s <= self.length))

        S, X, Y, C, Sn = [], [], [], [], []
        ox, oy, _, rotation = self.offset
        for geom in sorted(pv.findall("geometry"), key=lambda e: f(e, "s")):
            s0, x0, y0, h0 = f(geom, "s"), f(geom, "x"), f(geom, "y"), f(geom, "hdg")
            L = f(geom, "length")
            if not all(math.isfinite(v) for v in (s0, x0, y0, h0, L)) or L <= 0:
                raise ValueError(f"road {self.id} 含无效的 geometry")
            loc, _ = geometry_local(geom)
            n = max(2, int(math.ceil(L / self.step)) + 1)
            # arc/spiral 用最大绝对曲率，不能使用首尾净转角（S 弯会相互抵消）。
            arc, spiral = geom.find("arc"), geom.find("spiral")
            bound = abs(f(arc, "curvature")) if arc is not None else (
                max(abs(f(spiral, "curvStart")), abs(f(spiral, "curvEnd"))) if spiral is not None else 0.0)
            if self.max_turn > 0 and bound > 0:
                n = max(n, int(math.ceil(L * bound / self.max_turn)) + 1)
            samples = sorted(set(np.linspace(0, L, n).tolist() + [s - s0 for s in extra if s0 < s < s0 + L]))
            if self.max_turn > 0 and (geom.find("poly3") is not None or geom.find("paramPoly3") is not None):
                # 多项式曲线检查每小段内部的航向，按需二分；保留原始记录的精确端点。
                for _ in range(20):
                    additions = []
                    for a, b in zip(samples, samples[1:]):
                        probe = np.linspace(a, b, 5)
                        angles = np.unwrap([loc(float(p))[2] for p in probe])
                        if float(np.ptp(angles)) > self.max_turn + 1e-12:
                            additions.append((a + b) / 2)
                    if not additions:
                        break
                    samples = sorted(set(samples + additions))
                else:
                    raise ValueError(f"road {self.id} 的多项式曲线无法满足转角约束")
            for p in samples:
                u, v, dh = loc(p)
                h = h0 + dh + rotation
                s = s0 + p
                if S and abs(s - S[-1]) < 1e-9:   # 段间重复点去重
                    continue
                S.append(s)
                x = x0 + u * math.cos(h0) - v * math.sin(h0)
                y = y0 + u * math.sin(h0) + v * math.cos(h0)
                X.append(ox + x * math.cos(rotation) - y * math.sin(rotation))
                Y.append(oy + x * math.sin(rotation) + y * math.cos(rotation))
                C.append(math.cos(h))
                Sn.append(math.sin(h))

        self.S = np.asarray(S, dtype=float)
        self.X = np.asarray(X, dtype=float)
        self.Y = np.asarray(Y, dtype=float)
        self.COS = np.asarray(C, dtype=float)
        self.SIN = np.asarray(Sn, dtype=float)
        if len(self.S) < 2 or not np.isfinite([self.S, self.X, self.Y]).all():
            raise ValueError(f"road {self.id} 没有有限的参考线几何")

    # -- 高程 --------------------------------------------------------------
    def _load_elevation(self):
        self.elevations = []
        ep = self.el.find("elevationProfile")
        if ep is not None:
            for e in ep.findall("elevation"):
                self.elevations.append((f(e, "s"), coeffs_of(e)))
        self.elevations.sort(key=lambda t: t[0])

    def elevation_at(self, s):
        co = pick(self.elevations, s)
        if co is None:
            return self.offset[2]
        s_i, c = co
        return poly(c, s - s_i) + self.offset[2]

    def _load_lateral(self):
        self.superelevations = []
        lp = self.el.find("lateralProfile")
        if lp is not None:
            for e in lp.findall("superelevation"):
                self.superelevations.append((f(e, "s"), coeffs_of(e)))
        self.superelevations.sort(key=lambda t: t[0])

    def superelevation_at(self, s):
        co = pick(self.superelevations, s)
        if co is None:
            return 0.0
        s_i, c = co
        return poly(c, s - s_i)

    # -- 查询 --------------------------------------------------------------
    def point_at(self, s, t=0.0, crossfall=False):
        """给定 (s, t) 返回惯性坐标 (x, y, z)。t 正方向为参考线左侧。"""
        s = min(max(s, 0.0), float(self.S[-1]))
        x = float(np.interp(s, self.S, self.X))
        y = float(np.interp(s, self.S, self.Y))
        # 航向用 cos/sin 插值后再还原, 避免 ±pi 跳变
        hdg = math.atan2(float(np.interp(s, self.S, self.SIN)),
                         float(np.interp(s, self.S, self.COS)))
        if t != 0.0:
            x += -t * math.sin(hdg)
            y += t * math.cos(hdg)
        z = self.elevation_at(s)
        if crossfall and t != 0.0:
            # 正超高 = 路面向右倾斜 = 右侧(-t)低、左侧(+t)高
            z += t * math.tan(self.superelevation_at(s))
        return x, y, z

    def s_samples(self, sa, sb):
        """返回 [sa, sb] 内部采样及精确端点，不以绝对容差借入相邻车道段。

        planView 加法可能使分界点偏差几个浮点 ULP；内部点按严格区间筛选，
        sa/sb 显式写入，避免接点取成区间外的里程或遗漏极短 laneSection。
        """
        if not len(self.S):
            return np.array([])
        sa = max(sa, float(self.S[0]))
        sb = min(sb, float(self.S[-1]))
        if sb <= sa:
            return np.array([])
        interior = self.S[(self.S > sa) & (self.S < sb)]
        return np.concatenate([[sa], interior, [sb]])


# ----------------------------------------------------------------------------
# 车道
# ----------------------------------------------------------------------------

def lane_widths(lane):
    """[(sOffset, (a,b,c,d)), ...] 按 sOffset 排序"""
    ws = []
    for w in lane.findall("width"):
        ws.append((f(w, "sOffset"), coeffs_of(w)))
    if not ws:
        ws.append((0.0, (0.0, 0.0, 0.0, 0.0)))
    ws.sort(key=lambda t: t[0])
    return ws


def width_at(ws, s, left_limit=False):
    """在精确段内里程求宽度；左极限仅影响记录选择，仍在原里程计算多项式。"""
    co = pick(ws, s, left_limit=left_limit)
    if co is None:                 # s 在第一个 sOffset 之前, 取第一个
        co = ws[0]
    s_i, c = co
    return poly(c, s - s_i)


def speed_value(el):
    """读取 OpenDRIVE speed 并统一为 m/s；undefined/no limit 保留为空。"""
    if el is None:
        return None
    value = f(el, "max", float("nan"))
    if not math.isfinite(value) or value < 0:
        return None
    unit = el.get("unit", "m/s").lower()
    factors = {"m/s": 1.0, "km/h": 1 / 3.6, "mph": 0.44704}
    if unit not in factors:
        raise ValueError(f"不支持的 speed 单位: {unit}")
    return value * factors[unit]


def lane_speed(lane, ds=0.0):
    """当前 laneSection 内 ds 处的车道限速，单位 m/s；没有记录时返回 None。"""
    record = pick([(f(e, "sOffset"), e) for e in lane.findall("speed")], ds)
    return speed_value(record[1]) if record else None


def road_speed(el, s=0.0):
    """道路 type/speed 的生效记录，作为未声明车道限速时的继承值（m/s）。"""
    record = pick([(f(e, "s"), e) for e in el.findall("type")], s)
    return speed_value(record[1].find("speed")) if record else None


def lane_width_array(lane, distances, inner, left_limit_end=False):
    """从 width 或 border 得到非负车道宽度。

    width 优先；border 是相对参考线的外边界距离，需减去已累计的内边界，
    不能把它当成独立车道宽。两种记录都没有时按零宽占位处理。
    left_limit_end 用于车道段闭合末端：末点取本段有效记录，不提前启用下一段记录。
    """
    tag = "width" if lane.find("width") is not None else "border"
    records = lane.findall(tag)
    if records:
        first = min(f(el, "sOffset") for el in records)
        if first > 1e-9 or first < 0:
            raise ValueError(f"lane {lane.get('id')} 的首个 {tag}/sOffset 必须为 0（相对 laneSection）")
    if lane.find("width") is not None:
        ws = lane_widths(lane)
        values = np.array([width_at(ws, d, left_limit=left_limit_end and i == len(distances) - 1)
                           for i, d in enumerate(distances)], dtype=float)
    elif lane.find("border") is not None:
        records = sorted([(f(e, "sOffset"), coeffs_of(e)) for e in lane.findall("border")], key=lambda x: x[0])
        values = np.abs([width_at(records, d, left_limit=left_limit_end and i == len(distances) - 1)
                         for i, d in enumerate(distances)]) - np.abs(inner)
    else:
        values = np.zeros(len(distances))
    if not np.isfinite(values).all():
        raise ValueError("车道宽度含 NaN/Inf")
    return np.clip(values, 0.0, None)


def build_lane_geometry(road, sa, sb, lsec, crossfall, min_width=1e-6):
    """
    生成一条 road 在 laneSection lsec 覆盖的 [sa, sb) 内的车道面与外边界。
    返回 [(属性dict, 内边界点列, 外边界点列), ...]

    注意: 必须由调用方按 laneSection 的**下标**传入对应 Interval,
    不能在函数内用 "浮点 s 距离 < 1e-6" 反查 laneSection —— 相邻 laneSection
    的 s 间距可能小于 1e-6(CARLA Town03 road 575 两段的间距是 9.99999969e-07),
    那样会把同一段匹配到两个区间上, 既重复出要素, 又让"渐灭过渡段"里 1e18 量级
    的 width 系数在错误的 ds 上求值, 算出 1e19 米宽的病态车道面。
    """
    lanes_el = road.el.find("lanes")
    if lanes_el is None or lsec is None:
        return []

    # 中心车道偏移 laneOffset
    offsets = []
    for lo in lanes_el.findall("laneOffset"):
        offsets.append((f(lo, "s"), coeffs_of(lo)))
    offsets.sort(key=lambda t: t[0])

    ss = road.s_samples(sa, sb)
    if not len(ss):
        return []

    out = []
    sec_len = max(sb - sa, 0.0)   # width 多项式的有效 ds 上限

    def off_at(s, left_limit=False):
        co = pick(offsets, s, left_limit=left_limit)
        if co is None:
            return 0.0
        s_i, c = co
        return poly(c, s - s_i)

    # 每个采样点处的中心车道偏移
    # 几何闭合末点位于 sb，但横断面属于 [sa,sb)：偏移/宽度取 sb 左极限。
    # 下一段首点独立取右侧记录，不能把下一段偏移与本段旧车道宽度混合。
    base_arr = np.array([off_at(s, left_limit=i == len(ss) - 1) for i, s in enumerate(ss)], dtype=float)
    n = len(ss)
    cum = {"v": np.zeros(n)}   # 内侧已累计的车道宽度(每个采样点一个值)

    def emit(group_tag, lid, lane, sign):
        """sign=+1 左侧(t 递增), sign=-1 右侧(t 递减)"""
        # <width> 的 @sOffset 是相对"本 laneSection 起点"的偏移, 不是绝对 s
        # (对比: laneOffset/@s、elevation/@s 才是相对 road 起点)
        # ds 再夹到本段长度内: 超高次多项式只在自己的极短区间内有效,
        # 越界求值会炸到天文数字。
        t_in = base_arr + sign * cum["v"]
        distances = np.clip(ss - sa, 0.0, sec_len)
        distances[0], distances[-1] = 0.0, sec_len
        w = lane_width_array(lane, distances, t_in, left_limit_end=True)
        t_mid = base_arr + sign * (cum["v"] + w / 2.0)   # 车道中心线: 再加半个车道宽
        cum["v"] = cum["v"] + w            # 累计到外侧
        t_out = base_arr + sign * cum["v"]
        if float(np.max(np.abs(t_out - t_in))) < min_width:
            return
        out.append({
            "lane": lane, "id": lid, "group": group_tag,
            "inner": [road.point_at(s, t, crossfall) for s, t in zip(ss, t_in)],
            "center": [road.point_at(s, t, crossfall) for s, t in zip(ss, t_mid)],
            "outer": [road.point_at(s, t, crossfall) for s, t in zip(ss, t_out)],
        })

    # 左侧: id 1,2,3... 由内向外, t 递增
    if lsec.find("left") is not None:
        left_ids = sorted(int(float(l.get("id"))) for l in lsec.findall("left/lane"))
        cum["v"] = np.zeros(n)
        for lid in left_ids:
            lane = _find_lane(lsec, "left", lid)
            if lane is not None:
                emit("left", lid, lane, +1)

    # 右侧: id -1,-2,-3... 由内向外, t 递减
    if lsec.find("right") is not None:
        right_ids = sorted((int(float(l.get("id")))
                            for l in lsec.findall("right/lane")), reverse=True)
        cum["v"] = np.zeros(n)
        for lid in right_ids:
            lane = _find_lane(lsec, "right", lid)
            if lane is not None:
                emit("right", lid, lane, -1)
    return out


def fix_polygon(g):
    """
    急弯内侧常出现"曲率半径 < 车道宽度"导致的边界自相交(打结),
    这是 OpenDRIVE 的固有几何退化(GDAL 驱动用 TIN 输出来规避)。
    这里用 make_valid 修复并保留面积最大的一块。
    返回 (几何, 是否修复过); 无法修复返回 (None, True)。
    """
    if g.is_valid:
        return g, False
    try:
        m = make_valid(g)
    except Exception:
        return None, True
    if m.geom_type == "Polygon":
        return m, True
    # make_valid 可能返回 GeometryCollection(MultiPolygon, LineString)，
    # 需要递归取面部件；只看第一层会误判为无法修复并丢掉整条车道。
    pending, polys = [m], []
    while pending:
        part = pending.pop()
        if part.geom_type == "Polygon":
            polys.append(part)
        else:
            pending.extend(getattr(part, "geoms", []))
    if not polys:
        return None, True
    return max(polys, key=lambda x: x.area), True


def _find_lane(lsec, group_tag, lid):
    grp = lsec.find(group_tag)
    if grp is None:
        return None
    for l in grp.findall("lane"):
        if int(float(l.get("id"))) == lid:
            return l
    return None


def lane_sections(road):
    """[(sa, sb, lsec), ...] 本 road 的所有 laneSection 区间(按 s 升序)"""
    lanes_el = road.el.find("lanes")
    if lanes_el is None:
        return []
    secs = sorted(((f(s, "s"), s) for s in lanes_el.findall("laneSection")),
                  key=lambda t: t[0])
    bounds = []
    for i, (sa, lsec) in enumerate(secs):
        sb = secs[i + 1][0] if i + 1 < len(secs) else road.length
        bounds.append((sa, sb, lsec))
    return bounds


def center_lane_points(road, with_z=True):
    """
    车道基准中心线 (CenterLane): 参考线叠加 <laneOffset> 后的实际道路中心线。
    所有左/右车道的横向位置都以它为 0 起算, 所以画标线/校验偏移时用它,
    而不是 ReferenceLine —— 两者在 CARLA 数据里常常差好几米(Town06 有 710 段非零 offset)。
    """
    lanes_el = road.el.find("lanes")
    offsets = []
    if lanes_el is not None:
        for lo in lanes_el.findall("laneOffset"):
            offsets.append((f(lo, "s"), coeffs_of(lo)))
    offsets.sort(key=lambda t: t[0])

    def off_at(s):
        co = pick(offsets, s)
        if co is None:
            return 0.0
        s_i, c = co
        return poly(c, s - s_i)

    pts = [road.point_at(s, off_at(s), False) for s in road.S]
    return pts if with_z else [(x, y) for x, y, _ in pts]


def lane_center_index(road, with_z=True, crossfall=False, contact_point="start"):
    """
    {(side, lane_id): {"lane": <Element>, "pts": [...]}, ...}
    key 是 contact_point 所在端的 lane ID；沿 predecessor/successor 追踪
    跨段重编号，点列按从接触端进入连接路的行驶方向排列。缺少连接声明时
    只沿相同 ID 延续；声明指向不存在的车道时终止，不凭 ID 拼出虚假轨迹。
    """
    if contact_point not in ("start", "end"):
        raise ValueError("contactPoint 必须为 start 或 end")
    sections = []
    for sa, sb, lsec in lane_sections(road):
        sections.append({(item["group"], item["id"]): item
                         for item in build_lane_geometry(road, sa, sb, lsec, crossfall)})
    if contact_point == "end":
        sections.reverse()
    if not sections:
        return {}
    idx = {}
    tag = "successor" if contact_point == "start" else "predecessor"
    for anchor, first in sections[0].items():
        key = anchor
        joined = []
        ids = []
        for i, items in enumerate(sections):
            item = items.get(key)
            if item is None:
                break
            pts = item["center"] if with_z else [(x, y) for x, y, _ in item["center"]]
            if contact_point == "end":
                pts = pts[::-1]
            if joined and pts and np.linalg.norm(np.asarray(joined[-1]) - np.asarray(pts[0])) < 1e-8:
                pts = pts[1:]
            joined.extend(pts)
            ids.append(item["id"])
            if i == len(sections) - 1:
                break
            link = item["lane"].find("link/" + tag)
            if link is not None:
                lid = int(link.get("id"))
                key = ("left" if lid > 0 else "right", lid)
            # 没有声明的旧格式，只允许相同 ID；如果下一段明确声明另一前身则停止。
            elif key in sections[i + 1]:
                reverse = "predecessor" if tag == "successor" else "successor"
                back = sections[i + 1][key]["lane"].find("link/" + reverse)
                if back is not None and int(back.get("id")) != item["id"]:
                    break
        idx[anchor] = {"lane": first["lane"], "pts": joined, "lane_ids": ids}
    return idx


# ----------------------------------------------------------------------------
# 主转换
# ----------------------------------------------------------------------------

def parse_georeference(root):
    """从 <header><geoReference> 读取 proj-string, 转成 pyproj CRS"""
    h = root.find("header")
    if h is None:
        return None, ""
    gr = h.find("geoReference")
    if gr is None or not (gr.text or "").strip():
        return None, ""
    proj_str = gr.text.strip()
    try:
        crs = pyproj.CRS.from_proj4(proj_str)
        return crs, proj_str
    except Exception as e:
        raise ValueError(f"geoReference 解析失败: {e}") from e


def convert(xodr_path, gpkg_path, step=1.0, layers=None, with_z=True,
            crossfall=False, max_turn=MAX_TURN):
    """转换选定图层；无效输入或写入失败抛异常，完整成功后才替换旧 GPKG。"""
    if not math.isfinite(step) or step <= 0:
        raise ValueError("采样步长必须为有限正数")
    if not math.isfinite(max_turn) or max_turn < 0:
        raise ValueError("最大转角必须为有限非负数")
    if os.path.realpath(xodr_path) == os.path.realpath(gpkg_path):
        raise ValueError("输出路径不能覆盖输入 XODR")
    layers = layers or ALL_LAYERS
    if not set(layers) <= set(ALL_LAYERS):
        raise ValueError("包含不支持的输出图层")
    tree = ET.parse(xodr_path)
    root = tree.getroot()
    crs, proj_str = parse_georeference(root)
    offset = parse_header_offset(root)
    signal_refs = reference_index(root, "signals/signalReference")
    object_refs = reference_index(root, "objects/objectReference")

    ref_rows, border_rows, lane_rows = [], [], []
    sig_rows, obj_rows = [], []
    center_rows, lane_center_rows, link_rows = [], [], []
    n_road = 0
    n_fixed = [0]   # 被修复的自相交车道面数量
    roads = {}      # road_id -> Road, JunctionLink 要按连接路 id 反查

    for el in root.findall("road"):
        road = Road(el, step=step, max_turn=max_turn, offset=offset)
        n_road += 1
        roads[road.id] = road
        road_attrs = road_fields(road) if {"ReferenceLine", "CenterLane"} & set(layers) else None

        # ---- ReferenceLine ----
        if "ReferenceLine" in layers:
            pts = [road.point_at(s, 0.0, False) for s in road.S]
            geom = LineString(pts if with_z else [(x, y) for x, y, _ in pts])
            ref_rows.append({**road_attrs, "geometry": geom})

        # ---- CenterLane: 参考线 + laneOffset ----
        if "CenterLane" in layers:
            pts = center_lane_points(road, with_z)
            if len(pts) >= 2:
                center_rows.append({**road_attrs, "geometry": LineString(pts)})

        # ---- Lane / LaneBorder / LaneCenter ----
        lanes_el = el.find("lanes")
        if lanes_el is not None and ({"Lane", "LaneBorder", "LaneCenter"} & set(layers)):
            for section_index, (sa, sb, lsec) in enumerate(lane_sections(road)):
                for item in build_lane_geometry(road, sa, sb, lsec, crossfall):
                    lane = item["lane"]
                    attrs = lane_fields(road, lane, lsec, section_index, sa, sb, item["group"], item["id"])
                    inner, outer = item["inner"], item["outer"]
                    if len(inner) < 2:
                        continue
                    ring = list(inner) + list(reversed(outer))
                    if not with_z:
                        ring = [(x, y) for x, y, _ in ring]
                    if len(ring) >= 4 and "Lane" in layers:
                        geom, fixed = fix_polygon(Polygon(ring))
                        if geom is None:
                            continue
                        if fixed:
                            n_fixed[0] += 1
                        lane_rows.append({
                            **attrs,
                            "self_intersect": int(fixed),
                            "geometry": geom,
                        })
                    if "LaneBorder" in layers:
                        g = LineString(outer if with_z else [(x, y) for x, y, _ in outer])
                        border_rows.append({**attrs, "geometry": g})
                    # LaneCenter: 与 Lane 面一一对应(同 road/同 laneSection 段)
                    if "LaneCenter" in layers:
                        c = item["center"]
                        if len(c) >= 2:
                            lane_center_rows.append({
                                **attrs,
                                "geometry": LineString(
                                    c if with_z else [(x, y) for x, y, _ in c]),
                            })

        # ---- RoadSignal ----
        if "RoadSignal" in layers:
            for i, sg in enumerate(el.findall("signals/signal")):
                p = road.point_at(f(sg, "s"), f(sg, "t"), False)
                p = (p[0], p[1], p[2] + f(sg, "zOffset"))
                sig_rows.append({
                    **point_fields(road, sg, i, "signal", signal_refs),
                    "geometry": Point(p if with_z else p[:2]),
                })

        # ---- RoadObject ----
        if "RoadObject" in layers:
            for i, ob in enumerate(el.findall("objects/object")):
                p = road.point_at(f(ob, "s"), f(ob, "t"), False)
                p = (p[0], p[1], p[2] + f(ob, "zOffset"))
                obj_rows.append({
                    **point_fields(road, ob, i, "object", object_refs),
                    "geometry": Point(p if with_z else p[:2]),
                })

    # ---- JunctionLink: 交叉口转向线 ----
    # junction/connection/laneLink: from = 进口路的车道, to = 连接路的车道。
    # 转向线取连接路上该车道(id=to)的中心轨迹, 一条 laneLink 一条线。
    if "JunctionLink" in layers:
        cache = {}
        for j in root.findall("junction"):
            jid, jname = j.get("id"), j.get("name", "")
            for conn in j.findall("connection"):
                cid = conn.get("id")
                inc = conn.get("incomingRoad", "")
                cr_id = conn.get("connectingRoad", "")
                cp = conn.get("contactPoint", "start")
                cache_key = (cr_id, cp)
                if cache_key not in cache:
                    road = roads.get(cr_id)
                    cache[cache_key] = (lane_center_index(road, with_z, crossfall, cp)
                                       if road is not None else {})
                idx = cache[cache_key]
                links = [(i, ll.get("from"), ll.get("to"), ll) for i, ll in enumerate(conn.findall("laneLink"))]
                if links and not idx:
                    raise ValueError(f"junction {jid} connection {cid} 声明了 laneLink，但连接路 {cr_id} 没有可用车道")
                if not links:      # 没有 laneLink 的连接路: 全部车道都算转向线
                    links = [(None, None, str(k[1]), None) for k in idx]
                for link_index, frm, to, ll in links:
                    try:
                        lid = int(float(to))
                    except (TypeError, ValueError):
                        continue
                    ent = idx.get(("left" if lid > 0 else "right", lid))
                    if ent is None or len(ent["pts"]) < 2:
                        raise ValueError(f"junction {jid} connection {cid} 的连接车道 {lid} 无法追踪")
                    link_rows.append({
                        "junction_id": jid, "junction_name": jname,
                        "junction_type": j.get("type"), "connection_id": cid, "connection_type": conn.get("type"),
                        "incoming_road": inc, "connecting_road": cr_id,
                        "linked_road": conn.get("linkedRoad"), "contact_point": cp,
                        "from_lane": frm, "to_lane": lid,
                        "lane_ids": ",".join(str(i) for i in ent["lane_ids"]),
                        "lane_type": ent["lane"].get("type", ""),
                        "lane_link_index": link_index, "is_inferred": int(ll is None),
                        "junction_attributes_json": json_text(dict(j.attrib)),
                        "junction_children_json": json_text(xml_record(j, exclude=("connection",))["children"]),
                        "connection_attributes_json": json_text(dict(conn.attrib)),
                        "connection_children_json": json_text(xml_record(conn)["children"]),
                        **(source_fields(ll) if ll is not None else {
                            "source_attributes_json": json_text({}), "source_children_json": json_text([])}),
                        "geometry": LineString(ent["pts"]),
                    })

    # ---- 写出 GPKG ----
    written = {}
    groups = [("ReferenceLine", ref_rows), ("LaneBorder", border_rows),
              ("Lane", lane_rows), ("RoadSignal", sig_rows), ("RoadObject", obj_rows),
              ("CenterLane", center_rows), ("LaneCenter", lane_center_rows),
              ("JunctionLink", link_rows)]
    if n_road == 0 or not any(rows for name, rows in groups if name in layers):
        raise ValueError("输入没有可导出的要素；旧产物未修改")
    # 文件级来源只存一次：含 header/controller、完整 road 与 junction，单层导出也可核对来源。
    # 要素字段仍保留可查询属性及其直接来源，不要求用户靠元数据查询日常属性。
    metadata = {
        "xodr_attribute_schema": "2",
        "xodr_document_json": json_text(xml_record(root, exclude=("road", "junction"))),
        "xodr_roads_json": json_text([xml_record(el) for el in root.findall("road")]),
        "xodr_junctions_json": json_text([xml_record(el) for el in root.findall("junction")]),
    }
    with atomic_output(gpkg_path) as temporary:
        for name, rows in groups:
            if name not in layers or not rows:
                continue
            fields = LAYER_FIELDS[name]
            df = pd.DataFrame(rows).reindex(columns=[*fields, "geometry"])
            for field, kind in fields.items():
                df[field] = df[field].astype(kind)
            gdf = gpd.GeoDataFrame(df, geometry="geometry", crs=crs)
            pyogrio.write_dataframe(gdf, temporary, layer=name, driver="GPKG", append=False,
                                    dataset_metadata=metadata if not written else None,
                                    layer_metadata={"xodr_attribute_schema": "2"})
            written[name] = len(rows)

    print("输入 : %s" % xodr_path)
    print("坐标 : %s" % (proj_str if proj_str else "(无 geoReference, 输出为局部笛卡尔坐标)"))
    print("道路 : %d 条, 采样步长 %.2f m" % (n_road, step))
    for k, v in written.items():
        print("  %-14s %d 个要素" % (k, v))
    if n_fixed[0]:
        print("  注: %d 个车道面因急弯自相交已自动修复(见 self_intersect 字段)" % n_fixed[0])
    print("输出 : %s" % gpkg_path)
    return written


def main():
    ap = argparse.ArgumentParser(
        description="把 OpenDRIVE (.xodr) 转换为 GeoPackage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n  python xodr2gpkg.py demo.xodr\n"
               "  python xodr2gpkg.py demo.xodr -o out.gpkg --epsilon 0.5 --layers Lane")
    ap.add_argument("xodr", nargs="?", help="输入 .xodr 文件")
    ap.add_argument("--all", action="store_true", help="转换 data/Town*.xodr")
    ap.add_argument("-o", "--output", default=None, help="输出 .gpkg 路径，默认 out/<输入名>.gpkg")
    ap.add_argument("--epsilon", type=float, default=1.0,
                    help="参考线采样步长(米), 越小越精细, 默认 1.0")
    ap.add_argument("--max-turn", type=float, default=math.degrees(MAX_TURN),
                    help="单段参考线最大转角(度), 弯道上自动加密采样, 默认 5")
    ap.add_argument("--layers", nargs="+", choices=ALL_LAYERS, default=None,
                    help="要输出的图层, 默认全部")
    ap.add_argument("--no-z", action="store_true", help="输出 2D 几何(不带高程)")
    ap.add_argument("--crossfall", action="store_true",
                    help="把超高(superelevation)计入车道面高程")
    args = ap.parse_args()

    if args.all and (args.xodr or args.output):
        ap.error("--all 不能同时指定输入或单个输出路径")
    if args.all:
        from pathlib import Path
        files = sorted(Path(DATA).glob("Town*.xodr"))
    elif args.xodr:
        files = [args.xodr]
    else:
        ap.error("指定输入 XODR 或 --all")
    if not files:
        ap.error("data/ 下没有 Town*.xodr")
    try:
        for path in files:
            if not os.path.isfile(path):
                raise ValueError(f"找不到输入文件: {path}")
            out = args.output or os.path.join(OUT, os.path.splitext(os.path.basename(path))[0] + ".gpkg")
            convert(path, out, step=args.epsilon, layers=args.layers,
                    with_z=not args.no_z, crossfall=args.crossfall,
                    max_turn=math.radians(args.max_turn))
    except (ET.ParseError, ValueError, OSError) as e:
        sys.exit(f"转换失败: {e}")


if __name__ == "__main__":
    main()
