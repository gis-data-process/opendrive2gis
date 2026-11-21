#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""三条转换流程的边界回归；使用临时文件，不覆盖项目内的正式产物。"""
import contextlib
import importlib.util
import io
import json
import math
from pathlib import Path
import subprocess
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from shapely.geometry import LineString, Point, Polygon

from own import xodr2gpkg as own
from own import validate as validator
from roadnet import roadnet as net


ROOT = Path(__file__).resolve().parent.parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = load_module("verify_regression", "code/diag/verify_net.py")
straight = load_module("straight_regression", "code/diag/straight.py")
tin = load_module("tin_regression", "code/gdal/tin2poly.py")
compare = load_module("compare_regression", "code/compare/compare.py")
report = load_module("report_regression", "code/compare/compare_ab.py")
merge_layers = load_module("merge_regression", "code/compare/mergelayers.py")
pavement = load_module("pavement_regression", "code/gdal/pavement.py")


def lane(lid=-1, kind="driving", width=4.0):
    return (f'<lane id="{lid}" type="{kind}" level="false">'
            f'<width sOffset="0" a="{width}" b="0" c="0" d="0"/></lane>')


def road_xml(length=20.0, lanes=None, geometry="<line/>", rid="1", x=0, z=0):
    lanes = lanes or f'<laneSection s="0"><center>{lane(0, "none", 0)}</center><right>{lane()}</right></laneSection>'
    return (f'<road id="{rid}" name="test" length="{length}" junction="-1">'
            f'<planView><geometry s="0" x="{x}" y="0" hdg="0" length="{length}">{geometry}</geometry></planView>'
            f'<elevationProfile><elevation s="0" a="{z}" b="0" c="0" d="0"/></elevationProfile>'
            f'<lanes>{lanes}</lanes></road>')


def document(roads=None):
    return ET.fromstring('<OpenDRIVE><header revMajor="1" revMinor="6">'
                         '<geoReference>+proj=utm +zone=49 +datum=WGS84</geoReference>'
                         '</header>' + (roads or road_xml()) + '</OpenDRIVE>')


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xodr_regression_")
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def source(self, root):
        src = self.dir / "test.xodr"
        ET.ElementTree(root).write(src, encoding="utf-8", xml_declaration=True)
        return src

    def convert(self, root, layers=None, **options):
        dst = self.dir / "test.gpkg"
        with contextlib.redirect_stdout(io.StringIO()):
            own.convert(str(self.source(root)), str(dst), layers=layers, **options)
        return dst

    def build(self, root, **options):
        return net.build_net(str(self.source(root)), str(self.dir / "net.gpkg"),
                             verbose=False, **options)

    def test_invalid_step_preserves_existing_file(self):
        dst = self.dir / "existing.gpkg"
        dst.write_bytes(b"existing result")
        with self.assertRaises(ValueError):
            own.convert(str(self.source(document())), str(dst), step=0)
        self.assertEqual(dst.read_bytes(), b"existing result")

    def test_failed_writer_preserves_existing_file(self):
        dst = self.dir / "existing.gpkg"
        dst.write_bytes(b"existing result")
        with patch.object(own.pyogrio, "write_dataframe", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                own.convert(str(self.source(document())), str(dst))
        self.assertTrue(dst.exists(), "写入失败不能删除旧文件")
        self.assertEqual(dst.read_bytes(), b"existing result")

    def test_default_output_goes_to_created_output_directory(self):
        src = self.source(document())
        out = self.dir / "new_out"
        with patch.object(own, "OUT", str(out), create=True), patch.object(sys, "argv", ["xodr2gpkg.py", str(src)]):
            with contextlib.redirect_stdout(io.StringIO()):
                own.main()
        self.assertTrue((out / "test.gpkg").is_file())

    def test_lane_speed_units_are_metres_per_second(self):
        for unit, value, expected in [("km/h", "36", 10), ("mph", "25", 11.176), ("m/s", "10", 10)]:
            with self.subTest(unit=unit):
                el = ET.fromstring(f'<lane><speed sOffset="0" max="{value}" unit="{unit}"/></lane>')
                self.assertAlmostEqual(own.lane_speed(el), expected)

    def test_lane_speed_uses_active_record(self):
        el = ET.fromstring('<lane><speed sOffset="0" max="36" unit="km/h"/>'
                          '<speed sOffset="10" max="72" unit="km/h"/></lane>')
        self.assertAlmostEqual(own.lane_speed(el, 15), 20)

    def test_road_speed_is_inherited_by_links(self):
        root = document()
        rtype = ET.SubElement(root.find("road"), "type", {"s": "0", "type": "town"})
        ET.SubElement(rtype, "speed", {"max": "25", "unit": "mph"})
        links, _ = self.build(root)
        self.assertIsNotNone(links.speed_kmh.iloc[0])
        self.assertAlmostEqual(links.speed_kmh.iloc[0], 40.2)

    def test_lane_speed_kmh_is_not_multiplied_twice(self):
        root = document()
        ET.SubElement(root.find("road/lanes/laneSection/right/lane"), "speed",
                      {"sOffset": "0", "max": "40", "unit": "km/h"})
        links, _ = self.build(root)
        self.assertEqual(links.speed_kmh.iloc[0], 40)

    def test_header_offset_rotates_and_translates_all_coordinates(self):
        root = document(road_xml(length=10))
        ET.SubElement(root.find("header"), "offset", {"x": "100", "y": "200", "z": "30", "hdg": str(math.pi / 2)})
        dst = self.convert(root, layers=["ReferenceLine"])
        line = pyogrio.read_dataframe(dst, layer="ReferenceLine").geometry.iloc[0]
        np.testing.assert_allclose(line.coords[0], [100, 200, 30], atol=1e-8)
        np.testing.assert_allclose(line.coords[-1], [100, 210, 30], atol=1e-8)

    def test_border_defined_lane_is_exported(self):
        root = document()
        el = root.find("road/lanes/laneSection/right/lane")
        el.remove(el.find("width"))
        ET.SubElement(el, "border", {"sOffset": "0", "a": "-4", "b": "0", "c": "0", "d": "0"})
        dst = self.convert(root, layers=["ReferenceLine", "Lane", "LaneBorder", "LaneCenter"])
        names = {x[0] for x in pyogrio.list_layers(dst)}
        self.assertIn("Lane", names)
        self.assertAlmostEqual(pyogrio.read_dataframe(dst, layer="Lane").geometry.area.sum(), 80)

    def test_signal_and_object_z_offsets(self):
        root = document(road_xml(z=10))
        for group, tag, height in [("signals", "signal", 5), ("objects", "object", 7)]:
            parent = ET.SubElement(root.find("road"), group)
            ET.SubElement(parent, tag, {"id": "1", "s": "3", "t": "0", "zOffset": str(height)})
        dst = self.convert(root, layers=["RoadSignal", "RoadObject"])
        for layer, expected in [("RoadSignal", 15), ("RoadObject", 17)]:
            self.assertEqual(pyogrio.read_dataframe(dst, layer=layer).geometry.iloc[0].z, expected)

    def test_lane_layers_preserve_section_identity_and_source_records(self):
        sections = (f'<laneSection s="0" singleSide="false"><right>{lane()}</right></laneSection>'
                    f'<laneSection s="10"><right>{lane()}</right></laneSection>')
        root = document(road_xml(lanes=sections))
        road = root.find("road")
        road.set("rule", "LHT")
        el = road.find("lanes/laneSection/right/lane")
        link = ET.SubElement(el, "link")
        ET.SubElement(link, "predecessor", {"id": "-2"})
        ET.SubElement(link, "successor", {"id": "-3"})
        ET.SubElement(el, "speed", {"sOffset": "0", "max": "36", "unit": "km/h"})
        ET.SubElement(el, "speed", {"sOffset": "5", "max": "72", "unit": "km/h"})
        ET.SubElement(el, "roadMark", {"sOffset": "0", "type": "solid", "color": "white"})
        ET.SubElement(el, "roadMark", {"sOffset": "5", "type": "broken", "color": "yellow"})
        el.set("customAttribute", "保留扩展属性")
        dst = self.convert(root, layers=["Lane", "LaneBorder", "LaneCenter"])
        required = {"road_name", "junction", "road_rule", "lane_section", "single_side",
                    "s_start", "s_end", "level", "speed", "speed_max", "speed_unit",
                    "predecessor_lane", "successor_lane", "widths_json", "speeds_json",
                    "road_marks_json", "source_attributes_json", "source_children_json"}
        for layer in ("Lane", "LaneBorder", "LaneCenter"):
            with self.subTest(layer=layer):
                df = pyogrio.read_dataframe(dst, layer=layer)
                self.assertFalse(required - set(df.columns), f"{layer} 缺字段: {required - set(df.columns)}")
                self.assertEqual(df.lane_section.tolist(), [0, 1])
                self.assertEqual(df.s_start.tolist(), [0, 10])
                self.assertEqual(df.s_end.tolist(), [10, 20])
                row = df.iloc[0]
                self.assertEqual(row.road_rule, "LHT")
                self.assertEqual(row.single_side, "false")
                self.assertEqual(row.predecessor_lane, -2)
                self.assertEqual(row.successor_lane, -3)
                self.assertAlmostEqual(row.speed, 10)
                self.assertEqual((row.speed_max, row.speed_unit), ("36", "km/h"))
                self.assertEqual([e["attributes"]["max"] for e in json.loads(row.speeds_json)], ["36", "72"])
                self.assertEqual([e["attributes"]["type"] for e in json.loads(row.road_marks_json)], ["solid", "broken"])
                self.assertEqual(json.loads(row.source_attributes_json)["customAttribute"], "保留扩展属性")

    def test_reference_layers_preserve_road_and_center_lane_attributes(self):
        root = document()
        road = root.find("road")
        road.set("rule", "RHT")
        road.set("customRoad", "扩展")
        link = ET.SubElement(road, "link")
        ET.SubElement(link, "predecessor", {"elementType": "junction", "elementId": "001"})
        ET.SubElement(link, "successor", {"elementType": "road", "elementId": "002", "contactPoint": "end"})
        for s, kind, speed in ((0, "town", "36"), (10, "rural", "72")):
            rtype = ET.SubElement(road, "type", {"s": str(s), "type": kind})
            ET.SubElement(rtype, "speed", {"max": speed, "unit": "km/h"})
        center = root.find("road/lanes/laneSection/center/lane")
        ET.SubElement(center, "roadMark", {"sOffset": "0", "type": "solid"})
        dst = self.convert(root, layers=["ReferenceLine", "CenterLane"])
        for layer in ("ReferenceLine", "CenterLane"):
            df = pyogrio.read_dataframe(dst, layer=layer)
            required = {"road_rule", "predecessor_id", "predecessor_type", "successor_id",
                        "successor_contact_point", "road_types_json", "center_lanes_json",
                        "source_attributes_json", "source_children_json"}
            self.assertFalse(required - set(df.columns), f"{layer} 缺字段: {required - set(df.columns)}")
            row = df.iloc[0]
            self.assertEqual((row.predecessor_id, row.predecessor_type), ("001", "junction"))
            self.assertEqual((row.successor_id, row.successor_contact_point), ("002", "end"))
            self.assertEqual([e["attributes"]["type"] for e in json.loads(row.road_types_json)], ["town", "rural"])
            self.assertEqual(json.loads(row.source_attributes_json)["customRoad"], "扩展")
            self.assertEqual(json.loads(row.center_lanes_json)[0]["source"]["children"][-1]["attributes"]["type"], "solid")

    def test_signals_preserve_optional_values_validity_and_cross_road_references(self):
        root = document(road_xml() + road_xml(rid="2", x=30))
        group = ET.SubElement(root.find("road"), "signals")
        sg = ET.SubElement(group, "signal", {
            "id": "001", "name": "限速", "s": "3", "t": "1", "type": "274", "subtype": "-1",
            "country": "China", "countryRevision": "2022", "orientation": "-", "dynamic": "no",
            "zOffset": "2", "hOffset": "0.1", "pitch": "0.2", "roll": "0.3", "height": "1.5",
            "width": "0.8", "value": "40", "unit": "km/h", "text": "40", "catalogId": "自定义"})
        ET.SubElement(sg, "validity", {"fromLane": "-3", "toLane": "-2"})
        ET.SubElement(sg, "validity", {"fromLane": "-1", "toLane": "-1"})
        ET.SubElement(group, "signal", {"id": "002", "s": "4", "t": "0"})
        refs = ET.SubElement(root.findall("road")[1], "signals")
        ref = ET.SubElement(refs, "signalReference", {"id": "001", "s": "0", "t": "0", "orientation": "+"})
        ET.SubElement(ref, "validity", {"fromLane": "1", "toLane": "2"})
        dst = self.convert(root, layers=["RoadSignal"])
        df = pyogrio.read_dataframe(dst, layer="RoadSignal")
        required = {"signal_index", "road_name", "junction", "country_revision", "orientation", "dynamic",
                    "z_offset", "h_offset", "pitch", "roll", "height", "width", "value", "unit", "text",
                    "validity_json", "signal_references_json", "source_attributes_json", "source_children_json"}
        self.assertFalse(required - set(df.columns), f"RoadSignal 缺字段: {required - set(df.columns)}")
        row = df.iloc[0]
        self.assertEqual((row.signal_id, row.orientation, row.dynamic), ("001", "-", "no"))
        self.assertEqual(row.country_revision, "2022")
        self.assertAlmostEqual(row.h_offset, .1)
        self.assertEqual((row.z_offset, row.height, row.width, row.value, row.unit), (2, 1.5, .8, 40, "km/h"))
        self.assertEqual(len(json.loads(row.validity_json)), 2)
        refs = json.loads(row.signal_references_json)
        self.assertEqual(refs[0]["road_id"], "2")
        self.assertEqual(refs[0]["source"]["children"][0]["attributes"]["toLane"], "2")
        self.assertEqual(json.loads(row.source_attributes_json)["catalogId"], "自定义")
        for name in ("orientation", "dynamic", "height", "value", "unit", "country_revision"):
            self.assertTrue(pd.isna(df.iloc[1][name]), name + " 源缺失值必须为空")

    def test_objects_preserve_dimensions_outline_and_references(self):
        root = document(road_xml() + road_xml(rid="2", x=30))
        group = ET.SubElement(root.find("road"), "objects")
        ob = ET.SubElement(group, "object", {"id": "A01", "s": "2", "t": "-1", "name": "停车位",
            "type": "parkingSpace", "subtype": "parallel", "dynamic": "no", "orientation": "+",
            "zOffset": ".5", "hdg": ".1", "pitch": ".2", "roll": ".3", "height": "1",
            "width": "2", "length": "5", "radius": ".4", "validLength": "10"})
        outline = ET.SubElement(ob, "outline", {"id": "0", "outer": "true"})
        for u, v in ((0, 0), (5, 0), (5, 2), (0, 2)):
            ET.SubElement(outline, "cornerLocal", {"u": str(u), "v": str(v), "z": "0", "height": "0"})
        ET.SubElement(ob, "repeat", {"s": "2", "length": "10", "distance": "5"})
        ET.SubElement(ob, "parkingSpace", {"access": "all"})
        refs = ET.SubElement(root.findall("road")[1], "objects")
        ET.SubElement(refs, "objectReference", {"id": "A01", "s": "0", "t": "0", "validLength": "8"})
        dst = self.convert(root, layers=["RoadObject"])
        df = pyogrio.read_dataframe(dst, layer="RoadObject")
        required = {"object_index", "subtype", "dynamic", "orientation", "z_offset", "hdg", "pitch", "roll",
                    "height", "width", "length", "radius", "valid_length", "outlines_json", "repeats_json",
                    "parking_spaces_json", "object_references_json", "source_attributes_json", "source_children_json"}
        self.assertFalse(required - set(df.columns), f"RoadObject 缺字段: {required - set(df.columns)}")
        row = df.iloc[0]
        self.assertEqual((row.length, row.width, row.height, row.radius, row.valid_length), (5, 2, 1, .4, 10))
        self.assertAlmostEqual(row.hdg, .1)
        self.assertEqual(len(json.loads(row.outlines_json)[0]["children"]), 4)
        self.assertEqual(json.loads(row.repeats_json)[0]["attributes"]["distance"], "5")
        self.assertEqual(json.loads(row.object_references_json)[0]["road_id"], "2")

    def test_all_null_speed_keeps_real_sqlite_type(self):
        dst = self.convert(document(), layers=["Lane", "LaneBorder", "LaneCenter"])
        with contextlib.closing(sqlite3.connect(dst)) as con:
            for name in ("Lane", "LaneBorder", "LaneCenter"):
                types = {row[1]: row[2] for row in con.execute(f'PRAGMA table_info("{name}")')}
                self.assertEqual(types.get("speed"), "REAL", name + " 空 speed 不能退化成 TEXT")
        for name in ("Lane", "LaneBorder", "LaneCenter"):
            self.assertTrue(pyogrio.read_dataframe(dst, layer=name).speed.isna().all())

    def test_junction_links_preserve_connection_and_lane_link_attributes(self):
        root = document()
        junc = ET.SubElement(root, "junction", {"id": "J01", "name": "路口", "type": "default"})
        ET.SubElement(junc, "controller", {"id": "C01", "type": "0", "sequence": "1"})
        con = ET.SubElement(junc, "connection", {"id": "001", "incomingRoad": "2",
            "connectingRoad": "1", "contactPoint": "start", "type": "default"})
        ET.SubElement(con, "laneLink", {"from": "-2", "to": "-1", "custom": "转向扩展"})
        dst = self.convert(root, layers=["JunctionLink"])
        df = pyogrio.read_dataframe(dst, layer="JunctionLink")
        required = {"junction_type", "junction_attributes_json", "junction_children_json",
                    "connection_attributes_json", "connection_children_json", "lane_link_index",
                    "is_inferred", "source_attributes_json", "source_children_json"}
        self.assertFalse(required - set(df.columns), f"JunctionLink 缺字段: {required - set(df.columns)}")
        row = df.iloc[0]
        self.assertEqual((row.junction_id, row.connection_id, row.junction_type), ("J01", "001", "default"))
        self.assertEqual(row.is_inferred, 0)
        self.assertEqual(row.lane_link_index, 0)
        self.assertEqual(json.loads(row.source_attributes_json)["custom"], "转向扩展")
        self.assertEqual(json.loads(row.junction_children_json)[0]["attributes"]["id"], "C01")
        self.assertEqual(json.loads(row.connection_attributes_json)["type"], "default")

    def test_validator_rejects_missing_attribute_field(self):
        root = document()
        dst = self.convert(root, layers=["LaneBorder"])
        df = pyogrio.read_dataframe(dst, layer="LaneBorder").drop(columns=["side"])
        pyogrio.write_dataframe(df, dst, layer="LaneBorder", driver="GPKG")
        with contextlib.redirect_stdout(io.StringIO()):
            checks = validator.validate(self.source(root), dst, layers=["LaneBorder"])
        self.assertFalse(all(checks), "几何正确也必须报告缺少 side 属性")

    def test_validator_rejects_changed_source_attribute(self):
        root = document()
        dst = self.convert(root, layers=["ReferenceLine"])
        df = pyogrio.read_dataframe(dst, layer="ReferenceLine")
        df.loc[0, "road_name"] = "错误名称"
        pyogrio.write_dataframe(df, dst, layer="ReferenceLine", driver="GPKG")
        with contextlib.redirect_stdout(io.StringIO()):
            checks = validator.validate(self.source(root), dst, layers=["ReferenceLine"])
        self.assertFalse(all(checks), "几何正确也必须报告错误的源属性值")

    def test_source_document_metadata_is_preserved_in_single_layer_output(self):
        root = document()
        root.set("customDocument", "完整保留")
        ET.SubElement(root.find("header"), "offset", {"x": "1", "y": "2", "z": "3", "hdg": "0"})
        ET.SubElement(root, "controller", {"id": "01", "name": "控制器"})
        dst = self.convert(root, layers=["LaneBorder"])
        metadata = pyogrio.read_info(dst, layer="LaneBorder")["dataset_metadata"] or {}
        self.assertIn("xodr_document_json", metadata)
        source = json.loads(metadata["xodr_document_json"])
        self.assertEqual(source["attributes"]["customDocument"], "完整保留")
        self.assertEqual(source["children"][1]["attributes"]["id"], "01")
        header = source["children"][0]
        self.assertEqual(header["children"][-1]["attributes"]["z"], "3")

    def test_invalid_numeric_attribute_preserves_existing_output(self):
        root = document()
        group = ET.SubElement(root.find("road"), "signals")
        ET.SubElement(group, "signal", {"id": "01", "s": "1", "t": "0", "height": "not-a-number"})
        dst = self.dir / "existing.gpkg"
        dst.write_bytes(b"existing result")
        with self.assertRaisesRegex(ValueError, "height"):
            own.convert(str(self.source(root)), str(dst), layers=["RoadSignal"])
        self.assertEqual(dst.read_bytes(), b"existing result")

    def test_validator_rejects_changed_nested_source_record(self):
        root = document()
        dst = self.convert(root, layers=["LaneBorder"])
        df = pyogrio.read_dataframe(dst, layer="LaneBorder")
        df.loc[0, "widths_json"] = "[]"
        pyogrio.write_dataframe(df, dst, layer="LaneBorder", driver="GPKG")
        with contextlib.redirect_stdout(io.StringIO()):
            checks = validator.validate(self.source(root), dst, layers=["LaneBorder"])
        self.assertFalse(all(checks), "必须发现宽度记录丢失，不能只检查字段存在")

    def test_validator_rejects_real_attribute_written_as_text(self):
        root = document()
        el = root.find("road/lanes/laneSection/right/lane")
        ET.SubElement(el, "speed", {"sOffset": "0", "max": "36", "unit": "km/h"})
        dst = self.convert(root, layers=["LaneBorder"])
        df = pyogrio.read_dataframe(dst, layer="LaneBorder")
        df["speed"] = df["speed"].astype(str)
        pyogrio.write_dataframe(df, dst, layer="LaneBorder", driver="GPKG")
        with contextlib.redirect_stdout(io.StringIO()):
            checks = validator.validate(self.source(root), dst, layers=["LaneBorder"])
        self.assertFalse(all(checks), "必须发现 REAL speed 被写为 TEXT")

    def test_layer_merge_preserves_nullable_field_types_and_source_metadata(self):
        src = self.convert(document(), layers=["LaneCenter"])
        dst = self.dir / "official.gpkg"
        official = gpd.GeoDataFrame({"ID": [1]}, geometry=[LineString([(0, 0), (20, 0)])], crs="EPSG:32649")
        pyogrio.write_dataframe(official, dst, layer="ReferenceLine", driver="GPKG",
                                dataset_metadata={"official_marker": "保留"})
        merge_layers.merge(src, dst, ["LaneCenter"])
        with contextlib.closing(sqlite3.connect(dst)) as con:
            types = {row[1]: row[2] for row in con.execute('PRAGMA table_info("LaneCenter")')}
            self.assertEqual(types["predecessor_lane"], "INTEGER", "空整数不能在合并后变为 REAL")
            self.assertEqual(types["successor_lane"], "INTEGER")
            self.assertEqual(types["speed"], "REAL")
        info = pyogrio.read_info(dst, layer="LaneCenter")
        self.assertEqual(info["dataset_metadata"]["official_marker"], "保留")
        self.assertIn("xodr_roads_json", info["dataset_metadata"])
        self.assertEqual(info["layer_metadata"]["xodr_attribute_schema"], "2")

    def test_spiral_turn_limit_handles_curvature_reversal(self):
        rd = own.Road(ET.fromstring(road_xml(length=10, geometry='<spiral curvStart="0.5" curvEnd="-0.5"/>')))
        heading = np.unwrap(np.arctan2(rd.SIN, rd.COS))
        self.assertLessEqual(float(np.max(np.abs(np.diff(heading)))), math.radians(5) + 1e-8)

    def test_poly3_uses_arc_length(self):
        rd = own.Road(ET.fromstring(road_xml(length=10, geometry='<poly3 a="0" b="1" c="0" d="0"/>')))
        np.testing.assert_allclose([rd.X[-1], rd.Y[-1]], [math.sqrt(50)] * 2, atol=1e-5)

    def test_narrow_width_profile_is_not_missed(self):
        root = document(road_xml(length=2))
        el = root.find("road/lanes/laneSection/right/lane")
        el.remove(el.find("width"))
        for start, a, b, c, d in [(0, 0, 0, 0, 0), (.2, 0, 0, 300, -2000), (.3, 1, 0, -300, 2000), (.4, 0, 0, 0, 0)]:
            ET.SubElement(el, "width", dict(zip(["sOffset", "a", "b", "c", "d"], map(str, [start, a, b, c, d]))))
        rd = own.Road(root.find("road"))
        items = own.build_lane_geometry(rd, 0, 2, root.find("road/lanes/laneSection"), False)
        self.assertEqual(len(items), 1)
        self.assertGreater(max(p[1] for p in items[0]["inner"]) - min(p[1] for p in items[0]["outer"]), .9)

    def test_junction_lane_follows_successor_renumbering(self):
        sections = (f'<laneSection s="0"><center>{lane(0,"none",0)}</center><right>'
                    '<lane id="-1" type="driving"><link><successor id="-2"/></link><width sOffset="0" a="4" b="0" c="0" d="0"/></lane>'
                    '</right></laneSection>'
                    f'<laneSection s="10"><center>{lane(0,"none",0)}</center><right>{lane()}'
                    '<lane id="-2" type="driving"><link><predecessor id="-1"/></link><width sOffset="0" a="4" b="0" c="0" d="0"/></lane></right></laneSection>')
        root = document(road_xml(lanes=sections))
        junc = ET.SubElement(root, "junction", {"id": "1"})
        con = ET.SubElement(junc, "connection", {"id": "0", "incomingRoad": "2", "connectingRoad": "1", "contactPoint": "start"})
        ET.SubElement(con, "laneLink", {"from": "-1", "to": "-1"})
        dst = self.convert(root, layers=["JunctionLink"])
        g = pyogrio.read_dataframe(dst, layer="JunctionLink").geometry.iloc[0]
        np.testing.assert_allclose(g.coords[-1][:2], [20, -6])

    def test_left_hand_traffic_right_lane_coordinates_follow_driving_direction(self):
        root = document()
        root.find("road").set("rule", "LHT")
        links, nodes = self.build(root)
        link = links.iloc[0]
        np.testing.assert_allclose(link.geometry.coords[0], (20, -2))
        np.testing.assert_allclose(link.geometry.coords[-1], (0, -2))
        xy = nodes.set_index("node_id").geometry
        self.assertEqual(tuple(link.geometry.coords[0]), tuple(xy[link.from_node].coords[0]))
        self.assertEqual(tuple(link.geometry.coords[-1]), tuple(xy[link.to_node].coords[0]))

    def test_link_output_uses_coordinate_order_without_direction_label(self):
        sections = f'<laneSection s="0"><left>{lane(1)}</left><right>{lane()}</right></laneSection>'
        for rule, right_start, left_start in (("RHT", 0, 20), ("LHT", 20, 0)):
            with self.subTest(rule=rule):
                root = document(road_xml(lanes=sections))
                root.find("road").set("rule", rule)
                links, nodes = self.build(root, csv=True)
                self.assertEqual(len(links), 2)
                self.assertNotIn("dir", links.columns, "方向由线坐标顺序表达，不能再输出标签")
                xy = nodes.set_index("node_id").geometry
                for lid, start in (("-1", right_start), ("1", left_start)):
                    link = links[links.lane_ids == lid].iloc[0]
                    expected_y = -2 if lid == "-1" else 2
                    np.testing.assert_allclose(link.geometry.coords[0], (start, expected_y))
                    np.testing.assert_allclose(link.geometry.coords[-1], (20 - start, expected_y))
                    self.assertEqual(tuple(link.geometry.coords[0]), tuple(xy[link.from_node].coords[0]))
                    self.assertEqual(tuple(link.geometry.coords[-1]), tuple(xy[link.to_node].coords[0]))
                disk = pyogrio.read_dataframe(self.dir / "net.gpkg", layer="Link")
                exported = pd.read_csv(self.dir / "net_link.csv", encoding="utf-8-sig")
                self.assertNotIn("dir", disk.columns)
                self.assertNotIn("dir", exported.columns)
                self.assertEqual(exported.from_node.tolist(), links.from_node.tolist())
                self.assertEqual(exported.to_node.tolist(), links.to_node.tolist())

    def test_junction_coordinate_order_follows_contact_end(self):
        sections = f'<laneSection s="0"><left>{lane(1)}</left><right>{lane()}</right></laneSection>'
        for contact, start, y in (("start", 0, -2), ("end", 20, 2)):
            with self.subTest(contact=contact):
                root = document(road_xml(lanes=sections))
                root.find("road").set("junction", "10")
                junction = ET.SubElement(root, "junction", {"id": "10"})
                connection = ET.SubElement(junction, "connection", {"id": "0", "incomingRoad": "2",
                    "connectingRoad": "1", "contactPoint": contact})
                ET.SubElement(connection, "laneLink", {"from": "-1", "to": "-1" if contact == "start" else "1"})
                links, nodes = self.build(root)
                self.assertEqual(len(links), 1)
                link = links.iloc[0]
                self.assertNotIn("dir", links.columns)
                np.testing.assert_allclose(link.geometry.coords[0], (start, y))
                np.testing.assert_allclose(link.geometry.coords[-1], (20 - start, y))
                xy = nodes.set_index("node_id").geometry
                self.assertEqual(tuple(link.geometry.coords[0]), tuple(xy[link.from_node].coords[0]))
                self.assertEqual(tuple(link.geometry.coords[-1]), tuple(xy[link.to_node].coords[0]))

    def _uturn_root(self, contact="start"):
        """手工构造同一 road 两方向的掉头：两个接头相距 8 m，转向为半径 4 m 的半圆。

        参考弧半径为 2 m，连接车道宽 4 m；start 走右侧，end 从左侧反向进入。
        两种输入都应把 (0,-4) 的进口接到 (0,4) 的出口，不能合并这两个节点。
        """
        sections = (f'<laneSection s="0"><left>{lane(1, width=8)}</left>'
                    f'<right>{lane(-1, width=8)}</right></laneSection>')
        root = document(road_xml(length=20, lanes=sections, x=-20))
        arm_link = ET.SubElement(root.find("road"), "link")
        ET.SubElement(arm_link, "successor", {"elementType": "junction", "elementId": "10"})
        connector_side = "right" if contact == "start" else "left"
        connector_id = -1 if contact == "start" else 1
        connector_sections = (f'<laneSection s="0"><{connector_side}>'
                              f'{lane(connector_id, width=4)}</{connector_side}></laneSection>')
        curvature = .5 if contact == "start" else -.5
        connector = ET.fromstring(road_xml(length=math.pi * 2, lanes=connector_sections,
            geometry=f'<arc curvature="{curvature}"/>', rid="2"))
        connector.set("junction", "10")
        connector.find("planView/geometry").set("y", "-2" if contact == "start" else "2")
        declaration = ET.SubElement(connector, "link")
        for tag in ("predecessor", "successor"):
            ET.SubElement(declaration, tag, {"elementType": "road", "elementId": "1", "contactPoint": "end"})
        lane_link = ET.SubElement(connector.find(f"lanes/laneSection/{connector_side}/lane"), "link")
        ET.SubElement(lane_link, "predecessor", {"id": "-1" if contact == "start" else "1"})
        ET.SubElement(lane_link, "successor", {"id": "1" if contact == "start" else "-1"})
        root.append(connector)
        junction = ET.SubElement(root, "junction", {"id": "10"})
        connection = ET.SubElement(junction, "connection", {"id": "0", "incomingRoad": "1",
            "connectingRoad": "2", "contactPoint": contact})
        ET.SubElement(connection, "laneLink", {"from": "-1", "to": str(connector_id)})
        return root

    def test_junction_uturn_connects_opposing_nodes_for_both_contact_ends(self):
        """遗漏掉头、接反方向或把两端吸成一个节点，都会破坏进口→掉头→出口。"""
        for contact in ("start", "end"):
            with self.subTest(contact=contact):
                links, nodes = self.build(self._uturn_root(contact), csv=True)
                self.assertEqual(len(links), 3)
                main = links[links.road_id == "1"]
                incoming = main[main.geometry.map(lambda g: g.coords[0][1] < 0)].iloc[0]
                outgoing = main[main.geometry.map(lambda g: g.coords[0][1] > 0)].iloc[0]
                uturn = links[links.road_id == "2"].iloc[0]
                self.assertEqual(incoming.to_node, uturn.from_node)
                self.assertEqual(uturn.to_node, outgoing.from_node)
                self.assertNotEqual(uturn.from_node, uturn.to_node)
                self.assertEqual((uturn.oneway, uturn.passable, uturn.link_type), (1, 1, "connector"))
                np.testing.assert_allclose(uturn.geometry.coords[0], (0, -4), atol=1e-10)
                np.testing.assert_allclose(uturn.geometry.coords[-1], (0, 4), atol=1e-10)
                self.assertGreater(uturn.geometry.length, 8.8, "默认整理后仍须保留掉头弧，不能只横接两节点")
                self.assertTrue(uturn.geometry.is_simple)
                xy = nodes.set_index("node_id").geometry
                self.assertEqual(tuple(uturn.geometry.coords[0]), tuple(xy[uturn.from_node].coords[0]))
                self.assertEqual(tuple(uturn.geometry.coords[-1]), tuple(xy[uturn.to_node].coords[0]))
                disk = pyogrio.read_dataframe(self.dir / "net.gpkg", layer="Link")
                exported = pd.read_csv(self.dir / "net_link.csv", encoding="utf-8-sig")
                self.assertEqual(disk.loc[disk.road_id == "2", "from_node"].iloc[0], incoming.to_node)
                self.assertEqual(exported.loc[exported.road_id == 2, "to_node"].iloc[0], outgoing.from_node)

    def test_junction_uturn_has_no_implicit_reverse_link(self):
        links, _ = self.build(self._uturn_root())
        uturn = links[links.road_id == "2"].iloc[0]
        reverse = links[(links.from_node == uturn.to_node) & (links.to_node == uturn.from_node)]
        self.assertTrue(reverse.empty, "只有一个源掉头方向时，不能凭空补出反向边")
        self.assertEqual(links.from_node.tolist().count(uturn.from_node), 1)

    def test_driving_uturn_expands_across_wide_lane_groups(self):
        """一般掉头接到宽车道组时也可横向展开，start/end 两种源方向都要保留回转与有向接续。"""
        for contact in ("start", "end"):
            with self.subTest(contact=contact):
                root = self._uturn_root(contact)
                for width in root.findall("road[@id='1']/lanes/laneSection/*/lane/width"):
                    width.set("a", "24")
                links, nodes = self.build(root)
                turn = links[links.road_id == "2"].iloc[0]
                incoming = links[(links.road_id == "1") & (links.lane_ids == "-1")].iloc[0]
                outgoing = links[(links.road_id == "1") & (links.lane_ids == "1")].iloc[0]
                self.assertEqual((turn.from_node, turn.to_node), (incoming.to_node, outgoing.from_node))
                np.testing.assert_allclose(turn.geometry.coords[0], (0, -12), atol=1e-8)
                np.testing.assert_allclose(turn.geometry.coords[-1], (0, 12), atol=1e-8)
                self.assertTrue(turn.geometry.is_simple)
                self.assertGreater(turn.geometry.length, 26.4)
                self.assertEqual(verify.max_turn_angle(turn.geometry), 0)
                self.assertEqual(len(links), 3, "横向展开不能额外增加反向掉头")
                points = nodes.set_index("node_id").geometry
                self.assertEqual(tuple(turn.geometry.coords[0]), tuple(points[turn.from_node].coords[0]))
                self.assertEqual(tuple(turn.geometry.coords[-1]), tuple(points[turn.to_node].coords[0]))

    def test_wide_driving_uturn_rejects_longitudinal_node_drag(self):
        """大位移仅允许横向接入；向前后拖拽源弧仍要报错并保留旧路网。"""
        root = self._uturn_root()
        for width in root.findall("road[@id='1']/lanes/laneSection/*/lane/width"):
            width.set("a", "24")
        root.find("road[@id='2']/planView/geometry").set("x", "5")
        destination = self.dir / "net.gpkg"
        destination.write_bytes(b"existing_network")
        with self.assertRaisesRegex(ValueError, "横向"):
            self.build(root)
        self.assertEqual(destination.read_bytes(), b"existing_network")

    def test_wide_driving_uturn_rejects_excessive_lateral_node_drag(self):
        """纯横向位移也有源弧比例上限，不能为很远的错误节点任意放大掉头。"""
        root = self._uturn_root()
        for width in root.findall("road[@id='1']/lanes/laneSection/*/lane/width"):
            width.set("a", "30")
        destination = self.dir / "net.gpkg"
        destination.write_bytes(b"existing_network")
        with self.assertRaisesRegex(ValueError, "迁移超过"):
            self.build(root)
        self.assertEqual(destination.read_bytes(), b"existing_network")

    def test_real_pingshan_20240507_wide_uturns_keep_fixed_nodes(self):
        """2024 坪山九条紧凑掉头需移到宽车道组节点；逐条验证展开后的端点、弧形和折角。"""
        from roadnet.endpoints import align_uturn_to_nodes
        from roadnet.junction import is_uturn_geometry
        from roadnet.source import prepare_source
        from roadnet.topology import build_topology

        path = ROOT / "data/Pingshan_20240507.xodr"
        if not path.exists():
            self.skipTest("缺少 2024 坪山源样例")
        source = prepare_source(ET.parse(path).getroot(), 1, net.DRIVABLE, True, 0, True)
        topology = build_topology(source, .05, False)
        node_baseline = {node_id: tuple(point) for node_id, point in topology.node_xy.items()}
        for road_id in ("457", "478", "651", "666", "781", "891", "1130", "1143", "1189"):
            with self.subTest(road_id=road_id):
                group = source.groups[(road_id, 1)][0]
                start, end = (topology.node_xy[topology.node_id[source.group_port(road_id, side, 1, 0)]]
                              for side in (0, 1))
                result = LineString(align_uturn_to_nodes(group["pts"], start, end))
                np.testing.assert_allclose(result.coords[0], start, atol=1e-8)
                np.testing.assert_allclose(result.coords[-1], end, atol=1e-8)
                self.assertTrue(result.is_simple)
                self.assertTrue(is_uturn_geometry(result.coords))
                self.assertEqual(verify.max_turn_angle(result), 0)
        self.assertEqual(node_baseline, {node_id: tuple(point) for node_id, point in topology.node_xy.items()})

    def test_junction_uturn_uses_connection_when_connector_incoming_link_is_absent(self):
        """junction 已明确进口及接触端时，连接路缺少重复的进口 road/link 不得断网。"""
        for contact in ("start", "end"):
            with self.subTest(contact=contact):
                root = self._uturn_root(contact)
                declaration = root.find("road[@id='2']/link")
                declaration.remove(declaration.find("predecessor" if contact == "start" else "successor"))
                # 端点故意相差 0.5 m，排除坐标吸附偶然掩盖源连接遗漏。
                geometry = root.find("road[@id='2']/planView/geometry")
                geometry.set("y", str(float(geometry.get("y")) + .5))
                links, _ = self.build(root)
                uturn = links[links.road_id == "2"].iloc[0]
                incoming = links[(links.road_id == "1") & (links.lane_ids == "-1")].iloc[0]
                outgoing = links[(links.road_id == "1") & (links.lane_ids == "1")].iloc[0]
                self.assertEqual(incoming.to_node, uturn.from_node, "源进口接点必须由 junction/connection 补齐")
                self.assertEqual(uturn.to_node, outgoing.from_node)

    def test_junction_uturn_keeps_source_arc_when_endpoints_already_match(self):
        """无节点迁移的半圆掉头不应被高斯平滑压成窄弧，直行与贴线开关也不能改它。"""
        for contact in ("start", "end"):
            with self.subTest(contact=contact):
                root = self._uturn_root(contact)
                connector = root.find("road[@id='2']")
                source = own.lane_center_index(own.Road(connector, step=1), with_z=False, contact_point=contact)
                expected = LineString(next(iter(source.values()))["pts"])
                links, _ = self.build(root)
                uturn = links[links.road_id == "2"].iloc[0]
                self.assertLess(uturn.geometry.hausdorff_distance(expected), 1e-8,
                    "掉头应保留源半圆，不能经过普通连接路平滑/切角链")
                self.assertAlmostEqual(uturn.geometry.length, expected.length, places=6)

    def test_junction_uturn_exposes_all_merged_source_connections(self):
        """两份源 connection 同方向合并后可追溯；不会因代表 road 的替换丢失掉头来源。"""
        root = self._uturn_root()
        duplicate = ET.fromstring(ET.tostring(root.find("road[@id='2']")))
        duplicate.set("id", "3")
        root.append(duplicate)
        connection = ET.fromstring(ET.tostring(root.find("junction/connection")))
        connection.set("id", "1")
        connection.set("connectingRoad", "3")
        root.find("junction").append(connection)
        links, _ = self.build(root)
        movements = links.attrs.get("junction_movements", [])
        turns = [item for item in movements if item["movement"] == "uturn"]
        self.assertEqual(len(turns), 2, "内部核验要保留被合并的全部源 connection")
        self.assertEqual({item["connecting_road"] for item in turns}, {"2", "3"})
        self.assertEqual(len({item["output_link_id"] for item in turns}), 1)
        self.assertTrue(all(item["directionally_connected"] for item in turns))
        self.assertFalse(links[links.link_type == "connector"].duplicated(["from_node", "to_node"]).any())

    def test_invalid_junction_uturn_preserves_existing_network(self):
        """不能保持回转弧时停止构建；不能写出退化直线或覆盖已经可用的路网。"""
        root = self._uturn_root()
        root.find("road[@id='2']/planView/geometry").set("y", "12")
        destination = self.dir / "net.gpkg"
        destination.write_bytes(b"existing_network")
        with self.assertRaisesRegex(ValueError, "掉头"):
            self.build(root)
        self.assertEqual(destination.read_bytes(), b"existing_network")

    def test_repeated_junction_connection_does_not_duplicate_uturn_or_source_report(self):
        root = self._uturn_root()
        root.find("junction").append(ET.fromstring(ET.tostring(root.find("junction/connection"))))
        links, _ = self.build(root)
        turns = links[links.link_type == "connector"]
        movements = [record for record in links.attrs["junction_movements"] if record["movement"] == "uturn"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(len(movements), 1, "同一 source connection 只能登记一次")

    def test_junction_without_uturn_declaration_does_not_create_one(self):
        root = self._uturn_root()
        root.remove(root.find("road[@id='2']"))
        root.find("junction").remove(root.find("junction/connection"))
        links, _ = self.build(root)
        self.assertEqual(len(links), 2)
        self.assertFalse((links.link_type == "connector").any(), "相邻对向节点不能代替源掉头声明")
        self.assertEqual(len(set(links.from_node) | set(links.to_node)), 4)

    def test_shoulder_uturn_keeps_directionally_connected_source_route(self):
        """路肩只对源掉头单独开放；两种接触端都须接到实际进口和反向出口。"""
        for contact in ("start", "end"):
            with self.subTest(contact=contact):
                root = self._uturn_root(contact)
                side = "right" if contact == "start" else "left"
                root.find(f"road[@id='2']/lanes/laneSection/{side}/lane").set("type", "shoulder")
                links, nodes = self.build(root, csv=True)
                self.assertEqual(len(links), 3, "有源声明且能接通的路肩掉头需要成为导航边")
                uturn = links[links.road_id == "2"].iloc[0]
                self.assertEqual((uturn.oneway, uturn.passable, uturn.link_type), (1, 1, "connector"))
                self.assertNotEqual(uturn.from_node, uturn.to_node)
                self.assertTrue(any((links.road_id == "1") & (links.to_node == uturn.from_node)))
                self.assertTrue(any((links.road_id == "1") & (links.from_node == uturn.to_node)))
                np.testing.assert_allclose(uturn.geometry.coords[0], (0, -4), atol=1e-8)
                np.testing.assert_allclose(uturn.geometry.coords[-1], (0, 4), atol=1e-8)
                self.assertGreater(uturn.geometry.length, 8.8)
                self.assertEqual(len(nodes), 4)
                self.assertEqual(len(pyogrio.read_dataframe(self.dir / "net.gpkg", layer="Link").columns), 18)

    def test_shoulder_uturn_can_be_disabled_without_enabling_ordinary_shoulders(self):
        root = self._uturn_root()
        root.find("road[@id='2']/lanes/laneSection/right/lane").set("type", "shoulder")
        links, _ = self.build(root, shoulder_uturns=False)
        self.assertEqual(len(links), 2)
        self.assertFalse((links.link_type == "connector").any())

    def test_shoulder_uturn_rejects_wrong_way_source_endpoint(self):
        """出口只存在反向机动车边时，不扩大普通road的方向来迁就路肩掉头。"""
        root = self._uturn_root()
        root.find("road[@id='2']/lanes/laneSection/right/lane").set("type", "shoulder")
        root.find("road[@id='1']/lanes/laneSection/left/lane").set("type", "shoulder")
        links, _ = self.build(root)
        self.assertEqual(len(links), 1)
        self.assertFalse((links.link_type == "connector").any())

    def test_shoulder_uturn_does_not_include_parallel_sidewalk_lane(self):
        root = self._uturn_root()
        section = root.find("road[@id='2']/lanes/laneSection/right")
        section.find("lane").set("type", "shoulder")
        section.append(ET.fromstring(lane(-2, "sidewalk", 3)))
        ET.SubElement(root.find("junction/connection"), "laneLink", {"from": "-1", "to": "-2"})
        links, _ = self.build(root)
        self.assertEqual(len(links[links.road_id == "2"]), 1, "只保留有源映射的路肩掉头")
        uturn = links[links.road_id == "2"].iloc[0]
        self.assertEqual((uturn.lane_ids, uturn.lanes), ("-1", 1))

    def test_ordinary_shoulder_connection_is_still_filtered(self):
        root = self.filtered_connector_case(kind="shoulder")
        links, _ = self.build(root)
        self.assertFalse((links.link_type == "connector").any(), "不能把全图路肩纳入通行图")

    def test_shoulder_uturn_does_not_create_self_loop_when_lane_center_nodes_coincide(self):
        """基准线模式把同road对向节点吸为一点时，不用路肩掉头制造自环。"""
        root = self._uturn_root()
        root.find("road[@id='2']/lanes/laneSection/right/lane").set("type", "shoulder")
        links, _ = self.build(root, lane_center=False)
        self.assertTrue(links[links.road_id == "2"].empty)

    def test_shoulder_uturn_follows_contact_end_across_lane_renumbering(self):
        """路肩不是按左右默认侧判方向；end接入仍沿前身追到另一编号，排除旁边none车道。"""
        root = self._uturn_root("end")
        connector = root.find("road[@id='2']")
        first = connector.find("lanes/laneSection")
        first.find("left/lane").set("type", "shoulder")
        second = ET.fromstring(ET.tostring(first))
        second.set("s", str(math.pi))
        shoulder = second.find("left/lane")
        shoulder.set("id", "2")
        shoulder.find("link/predecessor").set("id", "1")
        first.find("left/lane/link/successor").set("id", "2")
        second.find("left").insert(0, ET.fromstring(lane(1, "none", 0)))
        connector.find("lanes").append(second)
        root.find("junction/connection/laneLink").set("to", "2")
        links, _ = self.build(root)
        self.assertEqual(len(links[links.road_id == "2"]), 1)
        self.assertEqual(set(links.loc[links.road_id == "2", "lane_ids"].iloc[0].split(",")), {"1", "2"})

    def test_parallel_shoulder_uturns_merge_with_all_source_references(self):
        root = self._uturn_root()
        root.find("road[@id='2']/lanes/laneSection/right/lane").set("type", "shoulder")
        road = ET.fromstring(ET.tostring(root.find("road[@id='2']")))
        road.set("id", "3")
        root.append(road)
        connection = ET.fromstring(ET.tostring(root.find("junction/connection")))
        connection.set("id", "1")
        connection.set("connectingRoad", "3")
        root.find("junction").append(connection)
        links, _ = self.build(root)
        connectors = links[links.link_type == "connector"]
        self.assertEqual(len(connectors), 1)
        self.assertEqual(connectors.n_merged.iloc[0], 1)
        self.assertEqual({r["connecting_road"] for r in links.attrs["junction_movements"]}, {"2", "3"})

    def test_shoulder_uturn_inherits_source_speed_once(self):
        root = self._uturn_root()
        lane_element = root.find("road[@id='2']/lanes/laneSection/right/lane")
        lane_element.set("type", "shoulder")
        ET.SubElement(lane_element, "speed", {"sOffset": "0", "max": "36", "unit": "km/h"})
        links, _ = self.build(root)
        self.assertEqual(links.loc[links.road_id == "2", "speed_kmh"].iloc[0], 36)

    def test_shared_lane_types_do_not_duplicate_retained_shoulder_uturn(self):
        root = self._uturn_root()
        root.find("road[@id='2']/lanes/laneSection/right/lane").set("type", "shoulder")
        links, _ = self.build(root, lane_types={"driving", "shoulder"})
        self.assertEqual(len(links[links.road_id == "2"]), 1)

    @unittest.skipUnless(all((ROOT / "data" / name).is_file() for name in ("Town03.xodr", "Town06.xodr")),
                         "缺少 Town03/Town06 源样例")
    def test_real_shoulder_uturns_keep_existing_nodes_and_actual_traffic_directions(self):
        """三条有效源路肩回转弧需要跨过车道群的横向距离；逆行端候选继续过滤。"""
        for name, expected in (("Town03", {"1202", "1688"}), ("Town06", {"719"})):
            with self.subTest(map=name):
                destination = self.dir / (name + "_shoulder.gpkg")
                links, nodes = net.build_net(ROOT / "data" / (name + ".xodr"), destination, verbose=False)
                for rid in expected:
                    rows = links[links.road_id == rid]
                    self.assertEqual(len(rows), 1, f"{name} Road {rid} 路肩掉头没有提取")
                    row = rows.iloc[0]
                    self.assertEqual((row.passable, row.oneway), (1, 1))
                    self.assertNotEqual(row.from_node, row.to_node)
                    self.assertTrue(row.geometry.is_simple)
                    self.assertGreater(row.geometry.length, Point(row.geometry.coords[0]).distance(Point(row.geometry.coords[-1])) * 1.1)
                rejected = {"1487", "1961"} if name == "Town03" else {"571", "745", "760", "1155"}
                self.assertFalse(links.road_id.isin(rejected).any(), "逆行端路肩不能成为可导航掉头")
                self.assertEqual(len(nodes), 204 if name == "Town03" else 128)

    @unittest.skipUnless((ROOT / "data/Pingshan_20250606_OD.xodr").is_file(), "缺少 2025 坪山源样例")
    def test_pingshan_uturn_remains_navigable_after_parallel_merge(self):
        """两条源掉头合成一条道路级边，合并前后都保留进口主组到反向出口主组。"""
        for merge in (False, True):
            with self.subTest(merge_turns=merge):
                links, _ = net.build_net(str(ROOT / "data/Pingshan_20250606_OD.xodr"),
                    str(self.dir / "pingshan_uturn.gpkg"), verbose=False, merge_turns=merge)
                incoming = links[(links.road_id == "202310038") & (links.link_type == "road")].iloc[0]
                outgoing = links[(links.road_id == "202310008") & (links.link_type == "road")].iloc[0]
                turns = links[(links.junction_id == "190") & (links.from_node == incoming.to_node)
                              & (links.to_node == outgoing.from_node)]
                self.assertEqual(len(turns), 1 if merge else 2)
                self.assertEqual(set(turns.road_id), {"194"} if merge else {"191", "194"})
                self.assertTrue((turns.oneway == 1).all())
                self.assertTrue((turns.passable == 1).all())
                self.assertEqual(turns.n_merged.tolist(), [1] if merge else [0, 0])
                for geometry in turns.geometry:
                    self.assertGreater(geometry.length, Point(geometry.coords[0]).distance(Point(geometry.coords[-1])) * 1.2)
                    self.assertTrue(geometry.is_simple)

    def test_network_verifier_accepts_coordinate_direction_and_detects_overlap(self):
        sections = f'<laneSection s="0"><left>{lane(1)}</left><right>{lane()}</right></laneSection>'
        links, nodes = self.build(document(road_xml(lanes=sections)))
        path = self.dir / "net.gpkg"
        # 在旧实现上也移除标签，直接证明校验器无需方向文本即可校验有向图。
        disk = pyogrio.read_dataframe(path, layer="Link").drop(columns=["dir"], errors="ignore")
        pyogrio.write_dataframe(disk, path, layer="Link", driver="GPKG")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                valid = verify.check(path, expected_components=2)
        except ValueError as exc:
            self.fail(f"只按数字化方向输出的路网应可校验: {exc}")
        self.assertTrue(valid)
        for i, link in disk.iterrows():
            start_x = link.geometry.coords[0][0]
            disk.at[i, "geometry"] = LineString([(start_x, 0), (20 - start_x, 0)])
        collapsed_nodes = pyogrio.read_dataframe(path, layer="Node")
        collapsed_nodes.geometry = [Point(g.x, 0) for g in collapsed_nodes.geometry]
        pyogrio.write_dataframe(disk, path, layer="Link", driver="GPKG")
        pyogrio.write_dataframe(collapsed_nodes, path, layer="Node", driver="GPKG")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(verify.check(path, expected_components=2), "去掉方向标签后仍应发现对向线重叠")

    def test_spacing_diagnostic_uses_opposing_main_links_without_direction_label(self):
        sections = f'<laneSection s="0"><left>{lane(1)}</left><right>{lane()}</right></laneSection>'
        self.build(document(road_xml(lanes=sections)))
        path = self.dir / "net.gpkg"
        disk = pyogrio.read_dataframe(path, layer="Link").drop(columns=["dir"], errors="ignore")
        # aux 与主组同方向，不应使两侧主组的间距检查被跳过。
        auxiliary = disk.iloc[[0]].copy()
        auxiliary["link_type"] = "aux"
        auxiliary["link_id"] = 3
        disk = pd.concat([disk, auxiliary], ignore_index=True)
        pyogrio.write_dataframe(disk, path, layer="Link", driver="GPKG")
        try:
            gaps = straight.gaps(path)
        except KeyError as exc:
            self.fail(f"间距诊断仍依赖已移除的字段: {exc}")
        np.testing.assert_allclose(gaps, [4.0])

    def test_single_bidirectional_lane_becomes_two_opposite_one_way_links(self):
        for kind, direction in (("bidirectional", None), ("driving", "both"), ("bidirectional", "both")):
            for side in ("right", "left"):
                with self.subTest(kind=kind, direction=direction, side=side):
                    lid = -1 if side == "right" else 1
                    sections = f'<laneSection s="0"><{side}>{lane(lid, kind)}</{side}></laneSection>'
                    root = document(road_xml(lanes=sections))
                    el = root.find(f"road/lanes/laneSection/{side}/lane")
                    if direction:
                        el.set("direction", direction)
                    try:
                        links, nodes = self.build(root, csv=True)
                    except ValueError as exc:
                        self.fail(f"双向车道不应被默认过滤: {exc}")
                    self.assertEqual(len(links), 2)
                    self.assertEqual(len(nodes), 2, "共享同一条车道的两方向应共用端点节点")
                    self.assertEqual(links.oneway.tolist(), [1, 1])
                    self.assertEqual(links.lanes.tolist(), [1, 1])
                    self.assertEqual(links.lane_ids.tolist(), [str(lid), str(lid)])
                    self.assertEqual(links.shared_lane_ids.tolist(), [str(lid), str(lid)])
                    a, b = links.iloc[0], links.iloc[1]
                    self.assertEqual((a.from_node, a.to_node), (b.to_node, b.from_node))
                    np.testing.assert_allclose(a.geometry.coords, list(b.geometry.coords)[::-1])
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertTrue(verify.check(self.dir / "net.gpkg"), "共享双向车道的对向共线应合法")
                    self.assertEqual(straight.gaps(self.dir / "net.gpkg"), [], "合法共享车道不应被记成异常间距")
                    exported = pd.read_csv(self.dir / "net_link.csv", encoding="utf-8-sig")
                    self.assertEqual(exported.oneway.tolist(), [1, 1])

    def test_two_bidirectional_lanes_aggregate_to_two_links_without_duplicates(self):
        cases = (
            f'<left>{lane(1, "bidirectional")}</left><right>{lane(-1, "bidirectional")}</right>',
            f'<right>{lane(-1, "bidirectional")}{lane(-2, "bidirectional")}</right>',
            '<left><lane id="1" type="driving" direction="both"><width sOffset="0" a="4" b="0" c="0" d="0"/></lane></left>'
            '<right><lane id="-1" type="driving" direction="both"><width sOffset="0" a="4" b="0" c="0" d="0"/></lane></right>',
            f'<right>{lane(-1, "bidirectional")}'
            '<lane id="-2" type="driving" direction="both"><width sOffset="0" a="4" b="0" c="0" d="0"/></lane></right>',
        )
        for lanes in cases:
            with self.subTest(lanes=lanes):
                root = document(road_xml(lanes=f'<laneSection s="0">{lanes}</laneSection>'))
                for options in ({}, {"split_taper": False}):
                    links, nodes = self.build(root, **options)
                    self.assertEqual(len(links), 2, "应按两个方向聚合，不能按两车道各复制两个 Link")
                    self.assertEqual(len(nodes), 2)
                    self.assertEqual(links.oneway.tolist(), [1, 1])
                    self.assertEqual(links.lanes.tolist(), [2, 2])
                    expected = {int(el.get("id")) for el in root.findall("road/lanes/laneSection/.//lane")}
                    for row in links.itertuples():
                        self.assertEqual({int(v) for v in row.lane_ids.split(",")}, expected)
                        self.assertEqual({int(v) for v in row.shared_lane_ids.split(",")}, expected)
                    np.testing.assert_allclose(links.geometry.iloc[0].coords, list(links.geometry.iloc[1].coords)[::-1])
                    self.assertEqual(len({(r.from_node, r.to_node) for r in links.itertuples()}), 2)

    def test_shared_taper_lane_does_not_add_duplicate_direction_links(self):
        sections = ('<laneSection s="0"><right>' + lane(-1, "bidirectional") +
                    '<lane id="-2" type="bidirectional"><width sOffset="0" a="4" b="-.2" c="0" d="0"/></lane>'
                    '</right></laneSection>')
        links, _ = self.build(document(road_xml(lanes=sections)))
        self.assertEqual(len(links), 2, "共享车道 road 应每方向聚合一条，不再拆出重复 aux")
        self.assertEqual(links.lanes.tolist(), [2, 2])
        self.assertEqual(links.link_type.tolist(), ["road", "road"])

    def test_bidirectional_lane_joins_each_direction_capacity_and_speed_once(self):
        sections = f'<laneSection s="0"><left>{lane(1)}</left><right>{lane(-1)}{lane(-2, "bidirectional")}</right></laneSection>'
        root = document(road_xml(lanes=sections))
        for path, speed in (("left/lane", 72), ("right/lane[@id='-1']", 54), ("right/lane[@id='-2']", 36)):
            ET.SubElement(root.find("road/lanes/laneSection/" + path), "speed", {"sOffset": "0", "max": str(speed), "unit": "km/h"})
        links, _ = self.build(root)
        self.assertEqual(len(links), 2)
        forward = next(r for r in links.itertuples() if r.geometry.coords[0][0] < r.geometry.coords[-1][0])
        reverse = next(r for r in links.itertuples() if r.geometry.coords[0][0] > r.geometry.coords[-1][0])
        self.assertEqual({int(v) for v in forward.lane_ids.split(",")}, {-1, -2})
        self.assertEqual({int(v) for v in reverse.lane_ids.split(",")}, {1, -2})
        self.assertEqual((forward.lanes, reverse.lanes), (2, 2))
        self.assertEqual((forward.speed_kmh, reverse.speed_kmh), (36, 36))
        self.assertEqual((forward.shared_lane_ids, reverse.shared_lane_ids), ("-2", "-2"))

    def test_bidirectional_type_respects_explicit_lane_type_filter(self):
        sections = f'<laneSection s="0"><right>{lane(-1, "bidirectional")}</right></laneSection>'
        root = document(road_xml(lanes=sections))
        with self.assertRaises(ValueError):
            self.build(root, lane_types={"driving"})
        root.find("road/lanes/laneSection/right/lane").set("type", "sidewalk")
        root.find("road/lanes/laneSection/right/lane").set("direction", "both")
        with self.assertRaises(ValueError):
            self.build(root)

    def test_shared_lane_junction_links_deduplicate_repeated_contact_declarations(self):
        sections = f'<laneSection s="0"><right>{lane(-1, "bidirectional")}</right></laneSection>'
        root = document(road_xml(lanes=sections))
        root.find("road").set("junction", "10")
        junction = ET.SubElement(root, "junction", {"id": "10"})
        for i, contact in enumerate(("start", "end", "start")):
            connection = ET.SubElement(junction, "connection", {"id": str(i), "incomingRoad": "2",
                "connectingRoad": "1", "contactPoint": contact})
            ET.SubElement(connection, "laneLink", {"from": "-1", "to": "-1"})
        links, nodes = self.build(root)
        self.assertEqual(len(links), 2)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(links.oneway.tolist(), [1, 1])
        self.assertEqual(links.lane_ids.tolist(), ["-1", "-1"])
        np.testing.assert_allclose(links.geometry.iloc[0].coords, list(links.geometry.iloc[1].coords)[::-1])

    def test_shared_lane_direction_is_read_per_section(self):
        sections = (f'<laneSection s="0"><right>{lane(-1)}</right></laneSection>'
                    f'<laneSection s="10"><right>{lane(-1, "bidirectional")}</right></laneSection>')
        links, _ = self.build(document(road_xml(lanes=sections)))
        self.assertEqual(len(links), 2)
        reverse = next(r for r in links.itertuples() if r.geometry.coords[0][0] > r.geometry.coords[-1][0])
        point = reverse.geometry.interpolate(reverse.geometry.project(Point(15, -2)))
        self.assertAlmostEqual(point.y, -2)
        self.assertEqual(reverse.lanes, 1)

    def test_every_link_is_one_way_in_two_lane_road(self):
        sections = f'<laneSection s="0"><left>{lane(1)}</left><right>{lane()}</right></laneSection>'
        links, _ = self.build(document(road_xml(lanes=sections)))
        self.assertEqual(links.oneway.tolist(), [1, 1])

    def test_verifier_rejects_duplicate_forward_link_on_shared_lane(self):
        root = document()
        root.find("road/lanes/laneSection/right/lane").set("direction", "both")
        self.build(root)
        path = self.dir / "net.gpkg"
        links = pyogrio.read_dataframe(path, layer="Link")
        # 保留几何和节点引用正确，单独加入同向副本并重算节点度，确认检出重复边。
        extra = links.iloc[[0]].copy()
        extra["link_id"] = int(links.link_id.max()) + 1
        links = pd.concat([links, extra], ignore_index=True)
        nodes = pyogrio.read_dataframe(path, layer="Node")
        incoming, outgoing = links.to_node.value_counts(), links.from_node.value_counts()
        nodes["n_in"] = nodes.node_id.map(incoming).fillna(0).astype(int)
        nodes["n_out"] = nodes.node_id.map(outgoing).fillna(0).astype(int)
        nodes["degree"] = nodes.n_in + nodes.n_out
        pyogrio.write_dataframe(links, path, layer="Link", driver="GPKG")
        pyogrio.write_dataframe(nodes, path, layer="Node", driver="GPKG")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(verify.check(path), "共享双向车道允许对向共线，但不能允许重复同向 Link")

    def test_verifier_requires_all_links_to_be_one_way(self):
        self.build(document())
        path = self.dir / "net.gpkg"
        links = pyogrio.read_dataframe(path, layer="Link")
        links["oneway"] = 0
        pyogrio.write_dataframe(links, path, layer="Link", driver="GPKG")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(verify.check(path), "oneway=0 不再符合单向边契约")

    def test_shared_lane_curved_geometry_is_reversed_without_extra_links(self):
        root = document(road_xml(geometry='<arc curvature="0.01"/>'))
        root.find("road/lanes/laneSection/right/lane").set("direction", "both")
        for rule in ("RHT", "LHT"):
            with self.subTest(rule=rule):
                root.find("road").set("rule", rule)
                links, nodes = self.build(root)
                self.assertEqual(len(links), 2)
                self.assertEqual(len(nodes), 2)
                np.testing.assert_allclose(links.geometry.iloc[0].coords, list(links.geometry.iloc[1].coords)[::-1])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(verify.check(self.dir / "net.gpkg"))

    def test_explicit_reversed_lane_direction_uses_same_physical_lane(self):
        root = document()
        root.find("road/lanes/laneSection/right/lane").set("direction", "reversed")
        links, _ = self.build(root)
        self.assertEqual(len(links), 1)
        np.testing.assert_allclose(links.geometry.iloc[0].coords[0], (20, -2))
        np.testing.assert_allclose(links.geometry.iloc[0].coords[-1], (0, -2))

    def test_grade_separated_endpoints_are_not_joined(self):
        root = document(road_xml(length=10) + road_xml(length=10, rid="2", x=10, z=10))
        links, nodes = self.build(root)
        self.assertEqual(len(nodes), 4)
        self.assertNotEqual(links.to_node.iloc[0], links.from_node.iloc[1])

    def filtered_connector_case(self, kind="shoulder", width=4.0, paired=True):
        """两条普通双向路间放置无有效车道的连接路，复现端口传递合并。"""
        section = f'<laneSection s="0"><left>{lane(1)}</left><right>{lane()}</right></laneSection>'
        root = document(road_xml(rid="1", lanes=section) + road_xml(rid="4", x=-30, lanes=section))
        junction = ET.SubElement(root, "junction", {"id": "10"})
        specifications = (("2", -1), ("3", 1)) if paired else (("2", -1),)
        for rid, direction in specifications:
            side, lid = ("left", 1) if direction < 0 else ("right", -1)
            lanes = f'<laneSection s="0"><{side}>{lane(lid, kind, width)}</{side}></laneSection>'
            connector = ET.fromstring(road_xml(length=10, rid=rid, x=-10, lanes=lanes))
            connector.set("junction", "10")
            declared = ET.SubElement(connector, "link")
            ET.SubElement(declared, "predecessor", {"elementType": "road", "elementId": "4", "contactPoint": "end"})
            ET.SubElement(declared, "successor", {"elementType": "road", "elementId": "1", "contactPoint": "start"})
            root.append(connector)
            directions = (direction,) if paired else (-1, 1)
            for d in directions:
                ET.SubElement(junction, "connection", {"id": str(len(junction)),
                    "incomingRoad": "1" if d < 0 else "4", "connectingRoad": rid,
                    "contactPoint": "end" if d < 0 else "start"})
        return root

    def assert_opposing_ports_separate(self, links):
        """分开的左右机动车道保留各自节点与源端坐标，不能被占位端口牵引。"""
        for rid, column, x in (("1", "from_node", 0), ("4", "to_node", -10)):
            rows = links[links.road_id == rid]
            forward = rows[rows.lane_ids == "-1"].iloc[0]
            reverse = rows[rows.lane_ids == "1"].iloc[0]
            opposite = "to_node" if column == "from_node" else "from_node"
            self.assertNotEqual(forward[column], reverse[opposite], "过滤的连接路不能把对向端口并成节点")
            index = 0 if column == "from_node" else -1
            np.testing.assert_allclose(forward.geometry.coords[index], (x, -2), atol=1e-9)
            np.testing.assert_allclose(reverse.geometry.coords[-1 if index == 0 else 0], (x, 2), atol=1e-9)

    def test_filtered_connectors_cannot_merge_opposing_road_nodes(self):
        for kind, width in (("shoulder", 4), ("sidewalk", 4), ("driving", 0)):
            with self.subTest(kind=kind, width=width):
                links, nodes = self.build(self.filtered_connector_case(kind, width))
                self.assert_opposing_ports_separate(links)
                self.assertEqual((len(links), len(nodes)), (4, 8))

    def test_filtered_connector_with_two_contacts_cannot_bridge_directions(self):
        links, nodes = self.build(self.filtered_connector_case(paired=False))
        self.assert_opposing_ports_separate(links)
        self.assertEqual((len(links), len(nodes)), (4, 8))

    def test_kept_nonpassable_connectors_do_not_merge_passable_nodes(self):
        links, nodes = self.build(self.filtered_connector_case(), keep_conn=True)
        self.assert_opposing_ports_separate(links)
        diagnostics = links[links.passable == 0]
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual((len(links), len(nodes)), (6, 12))
        plain = links[links.passable == 1]
        self.assertTrue((set(diagnostics.from_node) | set(diagnostics.to_node)).isdisjoint(
            set(plain.from_node) | set(plain.to_node)), "占位线保留独立节点，不继承机动车通行关系")

    def test_lane_type_is_evaluated_per_section(self):
        sections = (f'<laneSection s="0"><right>{lane()}{lane(-2,"sidewalk")}</right></laneSection>'
                    f'<laneSection s="10"><right>{lane()}{lane(-2)}</right></laneSection>')
        rd = own.Road(ET.fromstring(road_xml(lanes=sections)))
        groups = net.side_lane_groups(rd, -1, {"driving"}, split=False)
        point = min(groups[0]["pts"], key=lambda p: abs(p[0] - 5))
        self.assertAlmostEqual(point[1], -2)

    def test_split_taper_does_not_duplicate_lane_capacity(self):
        """无独立接点的渐变车道不另出边，主组仍只统计稳定车道的容量和限速。"""
        sections = ('<laneSection s="0"><right>' + lane() + lane(-2) +
                    '<lane id="-3" type="driving"><width sOffset="0" a="4" b="-.2" c="0" d="0"/></lane>'
                    '</right></laneSection>')
        root = document(road_xml(lanes=sections))
        for entry in root.findall("road/lanes/laneSection/right/lane"):
            ET.SubElement(entry, "speed", {"sOffset": "0", "max": "36" if entry.get("id") == "-3" else "72", "unit": "km/h"})
        links, nodes = self.build(root, csv=True)
        self.assertFalse((links.link_type == "aux").any(), "同起终节点的弯曲辅道不增加独立路径")
        self.assertEqual(len(links), 1)
        main = links.iloc[0]
        self.assertEqual((main.lanes, main.lane_ids, main.speed_kmh), (2, "-1,-2", 72))
        np.testing.assert_allclose(np.asarray(main.geometry.coords)[:, 1], -4, atol=1e-10)
        self.assertEqual((tuple(main.geometry.coords[0]), tuple(main.geometry.coords[-1])), ((0, -4), (20, -4)))
        self.assertTrue((nodes.degree == 1).all(), "删除重复边后节点度数要重算")
        disk = pyogrio.read_dataframe(self.dir / "net.gpkg", layer="Link")
        exported = pd.read_csv(self.dir / "net_link.csv", encoding="utf-8-sig")
        self.assertEqual(len(disk), 1)
        self.assertEqual(len(exported), 1)
        self.assertEqual(set(exported), set(disk) - {"geometry"})

    def test_multiple_redundant_tapers_keep_each_direction_main_geometry(self):
        """两侧多个渐变车道都回到主组时只保留两条主线，不合并到整侧中位线。"""
        sides = []
        for side, tag in ((1, "left"), (-1, "right")):
            entries = lane(side) + lane(2 * side)
            for lid in (3 * side, 4 * side):
                entries += (f'<lane id="{lid}" type="driving">'
                            '<width sOffset="0" a="4" b="-.2" c="0" d="0"/></lane>')
            sides.append(f'<{tag}>' + entries + f'</{tag}>')
        sections = '<laneSection s="0">' + ''.join(sides) + '</laneSection>'
        for rule in ("RHT", "LHT"):
            for merge in (True, False):
                with self.subTest(rule=rule, merge_turns=merge):
                    root = document(road_xml(lanes=sections))
                    root.find("road").set("rule", rule)
                    links, nodes = self.build(root, merge_turns=merge)
                    self.assertEqual(len(links), 2, "四条同节点渐变辅道应全部省略")
                    self.assertTrue((links.link_type == "road").all())
                    self.assertTrue((links.lanes == 2).all())
                    self.assertTrue((links.oneway == 1).all())
                    self.assertEqual(len(nodes), 4)
                    self.assertTrue((nodes.degree == 1).all())
                    for _, row in links.iterrows():
                        side = 1 if row.lane_ids == "1,2" else -1
                        np.testing.assert_allclose(np.asarray(row.geometry.coords)[:, 1], side * 4, atol=1e-10)
                        direction = side if rule == "LHT" else -side
                        self.assertEqual(row.geometry.coords[0][0], 0 if direction > 0 else 20)
                        self.assertEqual(row.geometry.coords[-1][0], 20 if direction > 0 else 0)

    def auxiliary_junction_case(self, direction=1, rule="RHT", mixed=False, renumber=False):
        """辅道在行驶终端保持全宽，源 junction 将其接入独立或混合连接路。"""
        side = direction if rule == "LHT" else -direction
        tag = "left" if side > 0 else "right"
        lane_ids = [side * i for i in (1, 2, 3)]
        section = f'<laneSection s="0"><{tag}>' + ''.join(lane(lid) for lid in lane_ids) + f'</{tag}></laneSection>'
        source = ET.fromstring(road_xml(rid="1", lanes=section))
        source.set("rule", rule)
        width = source.find(f"lanes/laneSection/{tag}/lane[@id='{lane_ids[2]}']/width")
        width.set("a", "0" if direction > 0 else "4")
        width.set("b", ".2" if direction > 0 else "-.2")
        declared = ET.SubElement(source, "link")
        road_tag = "successor" if direction > 0 else "predecessor"
        ET.SubElement(declared, road_tag, {"elementType": "junction", "elementId": "10"})
        root = document(ET.tostring(source, encoding="unicode"))
        junction = ET.SubElement(root, "junction", {"id": "10"})
        contact = "start" if direction > 0 else "end"
        connection_tag = "predecessor" if direction > 0 else "successor"
        specs = [("2", lane_ids if mixed else lane_ids[:2], lane_ids if mixed else lane_ids[:2], 0)]
        if not mixed:
            specs.append(("3", [side], [lane_ids[2]], 8 * side))
        for rid, ids, targets, offset in specs:
            conn_section = f'<laneSection s="0"><{tag}>' + ''.join(lane(lid) for lid in ids) + f'</{tag}></laneSection>'
            connector = ET.fromstring(road_xml(rid=rid, x=20 if direction > 0 else -10,
                                               length=10, lanes=conn_section))
            connector.set("junction", "10")
            connector.set("rule", rule)
            if offset:
                ET.SubElement(connector.find("lanes"), "laneOffset", {"s":"0", "a":str(offset), "b":"0", "c":"0", "d":"0"})
            lk = ET.SubElement(connector, "link")
            ET.SubElement(lk, connection_tag, {"elementType":"road", "elementId":"1",
                "contactPoint":"end" if direction > 0 else "start"})
            c = ET.SubElement(junction, "connection", {"id":rid, "incomingRoad":"1",
                "connectingRoad":rid, "contactPoint":contact})
            for lid, target in zip(ids, targets):
                entry = connector.find(f"lanes/laneSection/{tag}/lane[@id='{lid}']")
                ET.SubElement(ET.SubElement(entry, "link"), connection_tag, {"id":str(target)})
                ET.SubElement(c, "laneLink", {"from":str(target), "to":str(lid)})
            if renumber:
                first = connector.find("lanes/laneSection")
                second = ET.SubElement(connector.find("lanes"), "laneSection", {"s":"5"})
                group = ET.SubElement(second, tag)
                for i in (1, 2, 3):
                    group.append(ET.fromstring(lane(side * i, "none", 0)))
                for lid in ids:
                    old_lane = first.find(f"{tag}/lane[@id='{lid}']")
                    new_lane = ET.fromstring(lane(lid + 3 * side))
                    ET.SubElement(old_lane.find("link"), "successor", {"id":new_lane.get("id")})
                    ET.SubElement(ET.SubElement(new_lane, "link"), "predecessor", {"id":str(lid)})
                    group.append(new_lane)
            root.append(connector)
        return root

    def test_full_width_auxiliary_junction_uses_its_own_node(self):
        for rule in ("RHT", "LHT"):
            for direction in (1, -1):
                with self.subTest(rule=rule, direction=direction):
                    root = self.auxiliary_junction_case(direction, rule)
                    links, nodes = self.build(root)
                    main = links[(links.road_id == "1") & (links.link_type == "road")].iloc[0]
                    auxiliary = links[links.link_type == "aux"].iloc[0]
                    branch = links[links.road_id == "3"].iloc[0]
                    through = links[links.road_id == "2"].iloc[0]
                    self.assertNotEqual(auxiliary.to_node, main.to_node, "辅道全宽出口不能共用主路节点")
                    self.assertEqual(auxiliary.from_node, main.from_node, "收窄汇入端仍保持主路连通")
                    self.assertEqual(branch.from_node, auxiliary.to_node)
                    self.assertEqual(through.from_node, main.to_node)
                    side = direction if rule == "LHT" else -direction
                    np.testing.assert_allclose(auxiliary.geometry.coords[-1], (20 if direction > 0 else 0, side * 10))
                    self.assertEqual(branch.snap_m, 0)
                    self.assertEqual(len(nodes[nodes.node_id == auxiliary.to_node]), 1)
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertTrue(verify.check(self.dir / "net.gpkg"))

    def test_mixed_connector_keeps_each_source_group_route(self):
        root = self.auxiliary_junction_case(mixed=True)
        links, nodes = self.build(root)
        main = links[(links.road_id == "1") & (links.link_type == "road")].iloc[0]
        auxiliary = links[links.link_type == "aux"].iloc[0]
        connectors = links[links.road_id == "2"]
        self.assertEqual(len(connectors), 2, "源混合连接路按主路/辅道入口分组，不能借共享节点串接")
        primary = connectors[connectors.lane_ids == "-1,-2"].iloc[0]
        branch = connectors[connectors.lane_ids == "-3"].iloc[0]
        self.assertEqual(primary.from_node, main.to_node)
        self.assertEqual(branch.from_node, auxiliary.to_node)
        self.assertNotEqual(primary.from_node, branch.from_node)
        self.assertEqual(sorted(connectors.lanes), [1, 2])
        self.assertTrue((connectors.oneway == 1).all())

    def test_mixed_connector_partition_follows_section_lane_renumbering(self):
        root = self.auxiliary_junction_case(mixed=True, renumber=True)
        links, _ = self.build(root)
        connectors = links[links.road_id == "2"]
        self.assertEqual(len(connectors), 2)
        auxiliary = links[links.link_type == "aux"].iloc[0]
        branch = connectors[connectors.from_node == auxiliary.to_node].iloc[0]
        self.assertEqual(set(branch.lane_ids.split(",")), {"-3", "-6"})
        self.assertEqual(branch.lanes, 1)
        np.testing.assert_allclose(np.asarray(branch.geometry.coords)[:, 1], -10, atol=1e-9)

    def test_partitioned_connector_speed_does_not_mix_reused_lane_ids(self):
        root = self.auxiliary_junction_case(mixed=True, renumber=True)
        connector = root.find("road[@id='2']")
        first, second = connector.findall("lanes/laneSection")
        group = second.find("right")
        for entry in list(group):
            lid = int(entry.get("id"))
            if lid in (-2, -3):
                group.remove(entry)
            elif lid <= -4:
                entry.set("id", str(lid + 2))
        for entry in first.findall("right/lane"):
            entry.find("link/successor").set("id", str(int(entry.get("id")) - 1))
        for section in (first, second):
            for entry in section.findall("right/lane"):
                if entry.get("type") != "driving":
                    continue
                auxiliary_id = -3 if section is first else -4
                ET.SubElement(entry, "speed", {"sOffset":"0", "max":"36" if int(entry.get("id")) == auxiliary_id else "72", "unit":"km/h"})
        links, _ = self.build(root)
        auxiliary = links[links.link_type == "aux"].iloc[0]
        connectors = links[links.road_id == "2"]
        branch = connectors[connectors.from_node == auxiliary.to_node].iloc[0]
        primary = connectors[connectors.from_node != auxiliary.to_node].iloc[0]
        self.assertEqual(primary.speed_kmh, 72, "重用 ID 的旧辅道限速不能混入新主路车道组")
        self.assertEqual(branch.speed_kmh, 36)

    def test_auxiliary_target_can_use_junction_lane_link(self):
        root = self.auxiliary_junction_case()
        lane_el = root.find("road[@id='3']/lanes/laneSection/right/lane")
        lane_el.remove(lane_el.find("link"))
        links, _ = self.build(root)
        auxiliary = links[links.link_type == "aux"].iloc[0]
        main = links[(links.road_id == "1") & (links.link_type == "road")].iloc[0]
        branch = links[links.road_id == "3"].iloc[0]
        self.assertEqual(branch.from_node, auxiliary.to_node)
        self.assertNotEqual(branch.from_node, main.to_node)

    def test_no_split_taper_keeps_one_group_node_contract(self):
        for options in ({"split_taper":False}, {"lane_center":False}):
            with self.subTest(options=options):
                links, _ = self.build(self.auxiliary_junction_case(), **options)
                self.assertFalse((links.link_type == "aux").any())
                self.assertEqual(len(links[links.road_id == "1"]), 1)

    @unittest.skipUnless((ROOT / "data/Town06.xodr").is_file(), "缺少 Town06 源样例")
    def test_town06_auxiliary_19_and_connector_122_share_separate_node(self):
        """真实 Road 15 辅道和 814 连接线保持源端点，主路继续接 799。"""
        links, nodes = net.build_net(str(ROOT / "data/Town06.xodr"), str(self.dir / "town06.gpkg"), verbose=False)
        road = links[links.road_id == "15"]
        main = road[road.link_type == "road"].iloc[0]
        auxiliary = road[road.link_type == "aux"].iloc[0]
        connector = links[links.road_id == "814"].iloc[0]
        through = links[links.road_id == "799"].iloc[0]
        self.assertNotEqual(auxiliary.to_node, main.to_node)
        self.assertEqual(auxiliary.to_node, connector.from_node)
        self.assertEqual(main.to_node, through.from_node)
        self.assertEqual(auxiliary.from_node, main.from_node)
        np.testing.assert_allclose(auxiliary.geometry.coords[-1], (123.87825809783939, -38.256990682096834))
        self.assertLessEqual(connector.snap_m, 1.75)
        self.assertEqual(main.snap_m, 0)
        xy = nodes.set_index("node_id").geometry
        self.assertAlmostEqual(xy[auxiliary.to_node].distance(xy[main.to_node]), 8.774748426595765, places=6)

    @unittest.skipUnless((ROOT / "data/futiancenter.xodr").is_file(), "缺少福田源样例")
    def test_partitioned_auxiliary_join_does_not_cross_its_main_group(self):
        """全宽端独立后，收窄端沿主组切向收口，不能把辅道接线拉过主路。"""
        source = ET.parse(ROOT / "data/futiancenter.xodr").getroot()
        root = ET.Element("OpenDRIVE")
        root.append(source.find("header"))
        for rid in ("3585", "3593", "10762"):
            root.append(source.find(f"road[@id='{rid}']"))
        root.append(source.find("junction[@id='10761']"))
        links, _ = self.build(root)
        main = links[(links.road_id == "3593") & (links.link_type == "road")].iloc[0]
        auxiliary = links[(links.road_id == "3593") & (links.link_type == "aux")].iloc[0]
        self.assertNotEqual(auxiliary.from_node, main.from_node)
        self.assertEqual(auxiliary.to_node, main.to_node)
        self.assertEqual(net._crossing_count(main.geometry, auxiliary.geometry), 0, "收窄汇入应贴向主组，不新增穿越")

    def test_neighbour_tangent_is_at_requested_node(self):
        links = pd.DataFrame([{"from_node": 1, "to_node": 2, "geometry": LineString([(0,0),(0,10),(10,10)])}])
        np.testing.assert_allclose(net._out_dir(links, 0, 1), [0, 1])

    def test_validator_rejects_wrong_projected_crs(self):
        root = document()
        for group, tag in [("signals", "signal"), ("objects", "object")]:
            ET.SubElement(ET.SubElement(root.find("road"), group), tag, {"id": "1", "s": "3", "t": "0"})
        dst = self.convert(root)
        for name, _ in pyogrio.list_layers(dst):
            df = pyogrio.read_dataframe(dst, layer=name).set_crs("EPSG:3857", allow_override=True)
            pyogrio.write_dataframe(df, dst, layer=name, append=False)
        with contextlib.redirect_stdout(io.StringIO()):
            checks = validator.validate(str(self.dir / "test.xodr"), str(dst))
        self.assertFalse(all(checks))

    def test_validator_accepts_absent_empty_signal_layers(self):
        dst = self.convert(document())
        with contextlib.redirect_stdout(io.StringIO()):
            checks = validator.validate(str(self.dir / "test.xodr"), str(dst))
        self.assertTrue(all(checks))

    def test_kink_check_covers_three_point_lines(self):
        self.assertEqual(verify.max_turn_angle(LineString([(0,0),(1,0),(1,1)])), 1)

    def test_kink_check_includes_end_vertices(self):
        self.assertEqual(verify.max_turn_angle(LineString([(0,0),(1,0),(1,1),(1,2),(1,3),(1,4),(1,5)])), 1)

    def test_verifier_empty_directory_fails(self):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "code/diag/verify_net.py"), "--dir", str(self.dir)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_missing_output_is_a_validation_failure(self):
        src = self.source(document())
        result = subprocess.run([sys.executable, "-B", str(ROOT / "code/own/validate.py"), str(src), str(self.dir / "absent.gpkg")], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_preview_batch_filters_roadnet_outputs(self):
        from own import preview
        self.convert(document())
        # 批处理目录内同时存在正常转换产物和 Link/Node 文件。
        self.build(document())
        rendered = []
        with patch.object(preview, "OUT", str(self.dir)), patch.object(sys, "argv", ["preview.py", "--all"]):
            with patch.object(preview, "plot", side_effect=lambda p: rendered.append(Path(p).name)):
                preview.main()
        self.assertEqual(rendered, ["test.gpkg"])

    def test_comparison_handles_null_geometries(self):
        dst = self.dir / "null.gpkg"
        gpd.GeoDataFrame({"value": [1, 2]}, geometry=[LineString([(0,0),(1,0)]), None], crs="EPSG:32649").to_file(dst, layer="LaneBorder")
        stats = compare.summarize(str(dst))
        self.assertEqual(stats["LaneBorder"]["n"], 2)
        self.assertEqual(stats["LaneBorder"]["len"], 1)

    def test_tin_null_geometry_keeps_attribute_row(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript('CREATE TABLE gpkg_geometry_columns(table_name TEXT,column_name TEXT,srs_id INT);'
                          'CREATE TABLE gpkg_spatial_ref_sys(srs_id INT,definition TEXT);'
                          'CREATE TABLE Lane(fid INTEGER,geom BLOB);'
                          'INSERT INTO gpkg_geometry_columns VALUES ("Lane","geom",0);'
                          'INSERT INTO gpkg_spatial_ref_sys VALUES (0,"undefined");'
                          'INSERT INTO Lane VALUES (7,NULL);')
        try:
            frame, _ = tin.convert(con, "Lane")
        except TypeError as exc:
            self.fail(f"合法 NULL 几何不能中断整层转换: {exc}")
        self.assertEqual(frame.fid.tolist(), [7])
        self.assertTrue(frame.geometry.isna().iloc[0])

    def test_tin_unknown_layer_preserves_destination(self):
        src = self.convert(document(), layers=["ReferenceLine"])
        dst = self.dir / "existing.gpkg"
        dst.write_bytes(b"old result")
        result = subprocess.run([sys.executable, "-B", str(ROOT / "code/gdal/tin2poly.py"), str(src), "NotARealLayer", "-o", str(dst)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(dst.exists(), "图层名错误不能删除目标文件")
        self.assertEqual(dst.read_bytes(), b"old result")

    def test_report_recomputes_conclusion_from_input(self):
        official = {"TownX": {"Lane": {"count": 1, "total_area": 100}, "ReferenceLine": {"count": 1, "total_length": 10}}}
        own_stats = {"TownX": {"Lane": {"count": 0, "total_area": 0}, "ReferenceLine": {"count": 1, "total_length": 10}}}
        with patch.object(report, "OWN", own_stats, create=True), patch.object(report, "GDAL", official, create=True), patch.object(report, "TOWNS", ["TownX"], create=True):
            md = report.build()
        conclusion = md.split("## 6. 结论")[-1]
        self.assertNotIn("8/8 完全一致", conclusion)
        self.assertNotIn("高度一致（偏差 ≤ 0.07%）", conclusion)
        self.assertIn("100.000", md)

    def test_layer_merge_failure_preserves_target(self):
        src = self.convert(document(), layers=["ReferenceLine", "LaneCenter"])
        dst = self.dir / "target.gpkg"
        gpd.GeoDataFrame({"name": ["original"]}, geometry=[Point(0, 0)], crs="EPSG:32649").to_file(dst, layer="Keep")
        original = dst.read_bytes()
        with patch.object(merge_layers.pyogrio, "write_dataframe", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                merge_layers.merge(src, dst, ["LaneCenter"])
        self.assertEqual(dst.read_bytes(), original)

    def test_layer_merge_is_idempotent_and_preserves_other_tables(self):
        src = self.convert(document(), layers=["ReferenceLine", "LaneCenter"])
        dst = self.dir / "target.gpkg"
        gpd.GeoDataFrame({"name": ["original"]}, geometry=[Point(0, 0)], crs="EPSG:32649").to_file(dst, layer="Keep")
        for _ in range(2):
            merge_layers.merge(src, dst, ["LaneCenter"])
        self.assertEqual(len(pyogrio.read_dataframe(dst, layer="LaneCenter")), 1)
        self.assertEqual(pyogrio.read_dataframe(dst, layer="Keep").name.tolist(), ["original"])

    def test_layer_merge_rejects_mismatched_crs(self):
        src = self.convert(document(), layers=["LaneCenter"])
        dst = self.dir / "target.gpkg"
        gpd.GeoDataFrame({"value": [1]}, geometry=[Point(0, 0)], crs="EPSG:3857").to_file(dst, layer="Keep")
        original = dst.read_bytes()
        with self.assertRaises(ValueError):
            merge_layers.merge(src, dst, ["LaneCenter"])
        self.assertEqual(dst.read_bytes(), original)

    def test_pavement_rejects_empty_input(self):
        src = self.dir / "empty.gpkg"
        gpd.GeoDataFrame({"value": [1]}, geometry=[None], crs="EPSG:32649").to_file(src, layer="Lane")
        with self.assertRaises(ValueError):
            pavement.build(src)

    def test_pavement_rejects_invalid_radius(self):
        src = self.convert(document(), layers=["Lane"])
        for radius in [-1, float("inf"), float("nan")]:
            with self.subTest(radius=radius), self.assertRaises(ValueError):
                pavement.build(src, r=radius)

    def test_tin_triangle_fixture_with_custom_geometry_column(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript('CREATE TABLE gpkg_geometry_columns(table_name TEXT,column_name TEXT,srs_id INT);'
                          'CREATE TABLE gpkg_spatial_ref_sys(srs_id INT,definition TEXT);'
                          'CREATE TABLE Lane(fid INTEGER,surface BLOB);'
                          'INSERT INTO gpkg_geometry_columns VALUES ("Lane","surface",0);'
                          'INSERT INTO gpkg_spatial_ref_sys VALUES (0,"undefined");')
        triangle = struct.pack("<BIII", 1, 1017, 1, 4)
        triangle += b"".join(struct.pack("<ddd", *p) for p in [(0,0,5),(4,0,5),(0,3,5),(0,0,5)])
        blob = b"GP\x00\x01" + struct.pack("<i", 0) + struct.pack("<BII", 1, 1016, 1) + triangle
        con.execute("INSERT INTO Lane VALUES (?,?)", (9, blob))
        frame, bad = tin.convert(con, "Lane")
        self.assertEqual(bad, 0)
        self.assertEqual(frame.fid.tolist(), [9])
        self.assertAlmostEqual(frame.geometry.iloc[0].area, 6)
        self.assertFalse(frame.geometry.iloc[0].has_z)

    def test_polygon_repair_finds_nested_multipolygon_parts(self):
        from shapely.geometry import GeometryCollection, MultiPolygon
        small = Polygon([(0,0),(1,0),(0,1)])
        large = Polygon([(10,0),(14,0),(10,3)])
        with patch.object(own, "make_valid", return_value=GeometryCollection([MultiPolygon([small, large]), LineString([(0,0),(1,1)])])):
            result, fixed = own.fix_polygon(Polygon([(0,0),(1,1),(0,1),(1,0)]))
        self.assertIsNotNone(result, "修复结果的嵌套 MultiPolygon 不能被丢弃")
        self.assertTrue(fixed)
        self.assertAlmostEqual(result.area, 6)

    def test_small_endpoint_snap_does_not_make_sharp_kink(self):
        points = [(0,0),(1,0),(2,0),(3,0),(4,0),(4.99,0),(5,0)]
        rows = []
        ids = {("1",0,1):1,("1",1,1):2}
        net._emit(rows, {"link_type":"road"}, "1", points, 1, 1, ids,
                  {1:(0,0),2:(5,.02)}, .1, 1, 1, 1)
        self.assertEqual(verify.max_turn_angle(rows[0]["geometry"]), 0)
        self.assertEqual(tuple(rows[0]["geometry"].coords[-1]), (5,.02))

    def test_short_road_endpoint_is_blended_without_kink(self):
        points = [(float(x),0) for x in np.linspace(0,5.5,8)]
        rows=[]
        ids = {("1",0,1):1,("1",1,1):2}
        net._emit(rows, {"link_type":"road"}, "1", points, 1, 1, ids,
                  {1:(0,.9),2:(5.5,0)}, .1, 1, 1, 1)
        self.assertEqual(verify.max_turn_angle(rows[0]["geometry"]), 0)

    def test_docker_command_preserves_open_options_and_paths(self):
        docker = load_module("docker_regression", "code/gdal/xodr2gpkg_docker.py")
        source = self.dir / "input folder" / "my road.xodr"
        source.parent.mkdir()
        source.write_text("<OpenDRIVE/>", encoding="utf-8")
        target = self.dir / "output folder" / "my road.gpkg"
        command = docker.build_command(source, target, ["-oo", "DISSOLVE_TIN=YES", "-oo", "EPSILON=0.5"], image="example/gdal:test")
        self.assertEqual(command[-4:], ["-oo", "DISSOLVE_TIN=YES", "-oo", "EPSILON=0.5"])
        self.assertIn("/input/my road.xodr", command)
        self.assertIn("/output/my road.gpkg", command)
        self.assertIn("--pull=never", command)

    def test_docker_failure_preserves_existing_file(self):
        docker = load_module("docker_failure_regression", "code/gdal/xodr2gpkg_docker.py")
        src = self.source(document())
        dst = self.dir / "existing.gpkg"
        dst.write_bytes(b"existing official result")
        with patch.object(docker.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)):
            self.assertEqual(docker.run(src, dst, []), 7)
        self.assertEqual(dst.read_bytes(), b"existing official result")

    def test_declared_junction_lane_without_geometry_is_rejected(self):
        root = document(road_xml(lanes='<laneSection s="0"><center>' + lane(0,"none",0) + '</center></laneSection>'))
        junction = ET.SubElement(root, "junction", {"id":"1"})
        connection = ET.SubElement(junction,"connection", {"id":"0","incomingRoad":"2","connectingRoad":"1","contactPoint":"start"})
        ET.SubElement(connection,"laneLink", {"from":"-1","to":"-1"})
        dst=self.dir/"existing.gpkg"
        dst.write_bytes(b"existing result")
        with self.assertRaises(ValueError):
            own.convert(str(self.source(root)),str(dst),layers=["ReferenceLine","JunctionLink"])
        self.assertEqual(dst.read_bytes(),b"existing result")

    def test_border_validator_ignores_zero_width_outer_border(self):
        root=document()
        right=root.find("road/lanes/laneSection/right")
        for el in list(right):right.remove(el)
        ET.SubElement(ET.SubElement(right,"lane", {"id":"-1","type":"driving"}),"border", {"sOffset":"0","a":"-4","b":"0","c":"0","d":"0"})
        ET.SubElement(ET.SubElement(right,"lane", {"id":"-2","type":"border"}),"border", {"sOffset":"0","a":"-4","b":"0","c":"0","d":"0"})
        source=self.source(root)
        self.assertEqual(validator.src_counts(source)["Lane"],1)

    def test_width_record_outside_section_is_not_extrapolated(self):
        root = document(road_xml(length=20))
        root.find("road/lanes/laneSection/right/lane/width").set("sOffset", "100")
        with self.assertRaises(ValueError):
            self.convert(root, layers=["Lane"])

    def test_overlay_rejects_different_coordinate_systems(self):
        official = self.dir / "official.gpkg"
        own_file = self.dir / "own.gpkg"
        gpd.GeoDataFrame({"ID": [-1]}, geometry=[LineString([(0,0),(10,0)])], crs="EPSG:3857").to_file(official, layer="LaneBorder")
        gpd.GeoDataFrame({"lane_id": [-1]}, geometry=[LineString([(0,0),(10,0)])], crs="EPSG:32649").to_file(own_file, layer="LaneBorder")
        with self.assertRaises(ValueError):
            report.boundary_overlay(official, own_file)

    def test_tin_batch_only_processes_original_town_files(self):
        src = self.convert(document(), layers=["ReferenceLine"])
        original = self.dir / "Town01.gpkg"
        original.write_bytes(src.read_bytes())
        derived = self.dir / "Town01_poly.gpkg"
        derived.write_bytes(b"previous derived result")
        extra = self.dir / "Town01_notes.gpkg"
        extra.write_bytes(b"unrelated result")
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", str(ROOT / "code/gdal/tin2poly.py"),
             "--batch", "--all", "--dir", str(self.dir)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(pyogrio.read_dataframe(derived, layer="ReferenceLine")), 1)
        self.assertEqual(extra.read_bytes(), b"unrelated result")
        self.assertFalse((self.dir / "Town01_poly_poly.gpkg").exists())

    def test_tin_batch_no_inputs_returns_failure(self):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "code/gdal/tin2poly.py"),
             "--batch", "--all", "--dir", str(self.dir)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_tin_batch_rejects_single_output_path(self):
        src = self.convert(document(), layers=["ReferenceLine"])
        (self.dir / "Town01.gpkg").write_bytes(src.read_bytes())
        dst = self.dir / "existing.gpkg"
        dst.write_bytes(b"existing result")
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "code/gdal/tin2poly.py"),
             "--batch", "--all", "--dir", str(self.dir), "-o", str(dst)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(dst.read_bytes(), b"existing result")


class LaneSectionBoundaryTests(unittest.TestCase):
    """车道段末端只能使用本段左极限横断面，不能混入下一段的偏移或宽度。"""

    def offset_change_road(self):
        """边界删去内侧占位车道并同步移动基准；真实行驶车道应连续保持 y=0..3.5。"""
        first = ('<laneSection s="0"><left>' + lane(1, "none", 3.5)
                 + '<lane id="2" type="driving"><link><successor id="1"/></link>'
                   '<width sOffset="0" a="3.5" b="0" c="0" d="0"/></lane></left></laneSection>')
        second = ('<laneSection s="10"><left><lane id="1" type="driving">'
                  '<link><predecessor id="2"/></link>'
                  '<width sOffset="0" a="3.5" b="0" c="0" d="0"/></lane></left></laneSection>')
        offsets = ('<laneOffset s="0" a="-3.5" b="0" c="0" d="0"/>'
                   '<laneOffset s="10" a="0" b="0" c="0" d="0"/>')
        return own.Road(ET.fromstring(road_xml(lanes=offsets + first + second)))

    def test_previous_section_end_uses_left_offset(self):
        road = self.offset_change_road()
        sa, sb, section = own.lane_sections(road)[0]
        items = own.build_lane_geometry(road, sa, sb, section, False)
        driving = next(item for item in items if item["id"] == 2)
        np.testing.assert_allclose(driving["inner"][-1], (10, 0, 0), atol=1e-12)
        np.testing.assert_allclose(driving["outer"][-1], (10, 3.5, 0), atol=1e-12)
        self.assertAlmostEqual(Polygon(driving["inner"] + driving["outer"][::-1]).area, 35)

    def test_compensated_offset_and_lane_renumbering_join_without_spike(self):
        road = self.offset_change_road()
        lines = own.lane_center_index(road, with_z=False)
        line = LineString(lines[("left", 2)]["pts"])
        self.assertAlmostEqual(line.length, 20, places=10)
        self.assertLess(max(abs(y - 1.75) for _, y in line.coords), 1e-10)

    def test_section_end_does_not_activate_width_record_starting_at_end(self):
        sections = ('<laneSection s="0"><right><lane id="-1" type="driving">'
                    '<width sOffset="0" a="3.5" b="0" c="0" d="0"/>'
                    '<width sOffset="10" a="9" b="0" c="0" d="0"/>'
                    '</lane></right></laneSection>'
                    '<laneSection s="10"><right>' + lane(-1, width=3.5) + '</right></laneSection>')
        road = own.Road(ET.fromstring(road_xml(lanes=sections)))
        sa, sb, section = own.lane_sections(road)[0]
        item = own.build_lane_geometry(road, sa, sb, section, False)[0]
        np.testing.assert_allclose(item["outer"][-1], (10, -3.5, 0), atol=1e-12)

    def test_section_end_does_not_activate_border_record_starting_at_end(self):
        sections = ('<laneSection s="0"><right>' + lane(-1, width=3.5)
                    + '<lane id="-2" type="driving">'
                      '<border sOffset="0" a="-7" b="0" c="0" d="0"/>'
                      '<border sOffset="10" a="-10.5" b="0" c="0" d="0"/>'
                      '</lane></right></laneSection>'
                      '<laneSection s="10"><right>' + lane(-1, width=3.5) + '</right></laneSection>')
        road = own.Road(ET.fromstring(road_xml(lanes=sections)))
        sa, sb, section = own.lane_sections(road)[0]
        item = own.build_lane_geometry(road, sa, sb, section, False)[1]
        np.testing.assert_allclose(item["outer"][-1], (10, -7, 0), atol=1e-12)

    def test_left_limit_evaluates_polynomial_at_exact_boundary(self):
        sections = ('<laneOffset s="0" a="0" b="0.2" c="0" d="0"/>'
                    '<laneOffset s="10" a="4" b="0" c="0" d="0"/>'
                    '<laneSection s="0"><right>' + lane() + '</right></laneSection>'
                    '<laneSection s="10"><right>' + lane() + '</right></laneSection>')
        road = own.Road(ET.fromstring(road_xml(lanes=sections)))
        sa, sb, section = own.lane_sections(road)[0]
        item = own.build_lane_geometry(road, sa, sb, section, False)[0]
        np.testing.assert_allclose(item["inner"][-1], (10, 2, 0), atol=1e-12)

    def test_samples_do_not_include_points_outside_section(self):
        road = self.offset_change_road()
        road.S = np.array([0, 10 - 5e-10, 10 + 5e-10, 20])
        left, right = road.s_samples(0, 10), road.s_samples(10, 20)
        self.assertEqual(left[-1], 10)
        self.assertEqual(right[0], 10)
        self.assertTrue((left <= 10).all() and (right >= 10).all())

    def test_samples_preserve_exact_endpoints_after_arithmetic_rounding(self):
        road = self.offset_change_road()
        road.S = np.array([0, np.nextafter(10.0, 0), 20])
        self.assertEqual(road.s_samples(0, 10)[-1], 10)
        self.assertEqual(road.s_samples(10, 20)[0], 10)

    def test_sub_nanometre_section_does_not_borrow_adjacent_points(self):
        road = self.offset_change_road()
        road.S = np.array([0, 10 - 5e-10, 10, 10 + 5e-10, 10 + 1e-9, 20])
        samples = road.s_samples(10, 10 + 5e-10)
        np.testing.assert_array_equal(samples, [10, 10 + 5e-10])

    def test_profile_record_does_not_activate_before_its_start(self):
        records = [(0, "old"), (1 + 5e-10, "next")]
        self.assertEqual(own.pick(records, 1), (0, "old"))

    @unittest.skipUnless((ROOT / "data/Town03.xodr").is_file(), "缺少 Town03 源样例")
    def test_town03_affected_driving_lane_ends_stay_in_source_cross_section(self):
        root = ET.parse(ROOT / "data/Town03.xodr").getroot()
        for rid, old_id, new_id, end, inner_t in (("1737", 3, 3, 2, -3.5), ("1566", 5, 2, 2, 0)):
            with self.subTest(road_id=rid):
                road = own.Road(root.find(f"road[@id='{rid}']"))
                sections = own.lane_sections(road)
                previous = own.build_lane_geometry(road, *sections[0], False)
                following = own.build_lane_geometry(road, *sections[1], False)
                a = next(item for item in previous if item["id"] == old_id)
                b = next(item for item in following if item["id"] == new_id)
                np.testing.assert_allclose(a["inner"][-1], b["inner"][0], atol=1e-8)
                np.testing.assert_allclose(a["outer"][-1], b["outer"][0], atol=1e-8)
                np.testing.assert_allclose(a["inner"][-1], road.point_at(end, inner_t), atol=1e-8)

    def test_independent_endpoint_validator_rejects_valid_spike(self):
        road = self.offset_change_road()
        root = document(ET.tostring(road.el, encoding="unicode"))
        frame = gpd.GeoDataFrame([dict(road_id="1", lane_section=0, side="left", lane_id=2,
                                      geometry=LineString([(0, 1.75), (9, 1.75), (10, 5.25)]))])
        errors, maximum = validator.lane_endpoint_errors(root, "LaneCenter", frame)
        self.assertTrue(errors, "有限、有效且落在旧车道面内的尖刺也应被源横断面校验检出")
        self.assertAlmostEqual(maximum, 3.5)

    def test_endpoint_validator_accepts_offset_compensation(self):
        road = self.offset_change_road()
        root = document(ET.tostring(road.el, encoding="unicode"))
        frame = gpd.GeoDataFrame([dict(road_id="1", lane_section=0, side="left", lane_id=2,
                                      geometry=LineString([(0, 1.75), (10, 1.75)]))])
        errors, maximum = validator.lane_endpoint_errors(root, "LaneCenter", frame)
        self.assertEqual(errors, [])
        self.assertEqual(maximum, 0)

    def test_endpoint_validator_checks_lane_polygon_caps(self):
        road = self.offset_change_road()
        root = document(ET.tostring(road.el, encoding="unicode"))
        frame = gpd.GeoDataFrame([dict(road_id="1", lane_section=0, side="left", lane_id=2,
                                      geometry=Polygon([(0, 0), (9, 0), (10, 3.5),
                                                        (10, 7), (9, 3.5), (0, 3.5)]))])
        errors, maximum = validator.lane_endpoint_errors(root, "Lane", frame)
        self.assertTrue(errors)
        self.assertGreater(maximum, .5)

    def test_endpoint_validator_accepts_curved_reference_geometry(self):
        root = document(road_xml(length=20, geometry='<arc curvature="0.02"/>'))
        road = own.Road(root.find("road"))
        item = own.build_lane_geometry(road, *own.lane_sections(road)[0], False)[0]
        frame = gpd.GeoDataFrame([dict(road_id="1", lane_section=0, side="right", lane_id=-1,
                                      geometry=LineString(item["outer"]))])
        errors, maximum = validator.lane_endpoint_errors(root, "LaneBorder", frame)
        self.assertEqual(errors, [])
        self.assertLess(maximum, 1e-10)

    def test_source_count_excludes_width_record_starting_at_section_end(self):
        root = document(road_xml(length=10))
        width = root.find("road/lanes/laneSection/right/lane/width")
        width.set("a", "0")
        parent = root.find("road/lanes/laneSection/right/lane")
        ET.SubElement(parent, "width", {"sOffset": "10", "a": "3.5", "b": "0", "c": "0", "d": "0"})
        with tempfile.TemporaryDirectory(prefix="lane_count_boundary_") as temp:
            source = Path(temp) / "test.xodr"
            ET.ElementTree(root).write(source, encoding="utf-8", xml_declaration=True)
            counts = validator.src_counts(source)
        self.assertEqual(counts["Lane"], 0)
        self.assertEqual(counts["zero_width_lanes"], 1)


class ConnectorGeometryTests(unittest.TestCase):
    """复现节点横移造成的直行鼓包、转弯回摆及几何整理的保护边界。"""

    def frame(self, source, geometry, incoming=(1, 0), outgoing=(1, 0)):
        """用两条有向普通路约束连接线；源中线独立于已收口的输出几何。"""
        a, b = np.asarray(geometry.coords)[[0, -1]]
        records = []
        for lid, kind, start, end, geom in (
                (1, "road", 1, 2, LineString([a - 10 * np.asarray(incoming), a])),
                (2, "connector", 2, 3, geometry),
                (3, "road", 3, 4, LineString([b, b + 10 * np.asarray(outgoing)]))):
            records.append(dict(link_id=lid, road_id=str(lid), link_type=kind,
                                from_node=start, to_node=end, lanes=2, oneway=1,
                                shared_lane_ids="-1", lane_ids="-1,-2", snap_m=1.75,
                                length=round(geom.length, 3), geometry=geom))
        return pd.DataFrame(records), {1: source}

    def straight_case(self, shift=0.0):
        source = LineString([(0, 1.75 + shift), (38.61, 1.75 + shift)])
        geometry = LineString([(0, shift), (8, 1.75 + shift),
                               (30, 1.75 + shift), (38.61, shift)])
        return self.frame(source, geometry)

    def turn_case(self, mirror=1.0):
        """源线为半径 10 m 的四分之一圆，错误输出在入口先摆回再转弯。"""
        t = np.linspace(0, math.pi / 2, 40)
        source = LineString(np.column_stack([10 * (1 - np.cos(t)), mirror * 10 * np.sin(t)]))
        geometry = LineString([(-3.5, 0), (-2, mirror * 1.0),
                               (1, mirror * 3.0), (3, mirror * 7.0),
                               (6, mirror * 9.0), (10, mirror * 10.0)])
        return self.frame(source, geometry, (0, mirror), (1, 0))

    def headings(self, geom):
        """按弧长独立采样测行驶航向，检查回摆而非只检查顶点折角。"""
        samples = [geom.interpolate(s).coords[0] for s in np.linspace(0, geom.length, 80)]
        vectors = np.diff(np.asarray(samples), axis=0)
        return np.unwrap(np.arctan2(vectors[:, 1], vectors[:, 0]))

    def test_source_straight_connector_with_lane_offset_is_straightened(self):
        links, sources = self.straight_case()
        net._straighten(links, source_lines=sources)
        self.assertEqual(list(links.at[1, "geometry"].coords), [(0, 0), (38.61, 0)])
        self.assertEqual(links.at[1, "length"], 38.61)

    def test_all_equivalent_straight_connectors_are_corrected(self):
        for shift in (0, 20, -35):
            with self.subTest(shift=shift):
                links, sources = self.straight_case(shift)
                net._straighten(links, source_lines=sources)
                self.assertEqual(list(links.at[1, "geometry"].coords), [(0, shift), (38.61, shift)])

    def test_source_straight_connector_can_be_rechecked_after_coincide(self):
        links, sources = self.straight_case()
        net._straighten(links, skip={1}, source_lines=sources)
        self.assertEqual(len(links.at[1, "geometry"].coords), 2)

    def test_straightening_requires_incoming_and_outgoing_road_directions(self):
        links, sources = self.straight_case()
        links.loc[0, ["from_node", "to_node"]] = [2, 1]
        links.at[0, "geometry"] = LineString(list(links.at[0, "geometry"].coords)[::-1])
        before = links.at[1, "geometry"]
        net._straighten(links, source_lines=sources)
        self.assertTrue(before.equals_exact(links.at[1, "geometry"], 0))

    def test_real_source_lane_change_is_not_straightened(self):
        links, sources = self.straight_case()
        sources[1] = LineString([(0, 1.75), (10, 1.75), (18, 2.65),
                                (28, 1.75), (38.61, 1.75)])
        links.at[1, "geometry"] = LineString([(0, 0), (10, 0), (18, .9), (28, 0), (38.61, 0)])
        before = links.at[1, "geometry"]
        net._straighten(links, source_lines=sources)
        net._refine_connectors(links, sources)
        self.assertTrue(before.equals_exact(links.at[1, "geometry"], 0))

    def test_new_crossing_blocks_straightening(self):
        links, sources = self.straight_case()
        links.loc[3] = dict(links.iloc[0], link_id=4, from_node=5, to_node=6,
                            geometry=LineString([(19, -.5), (19, .5)]), length=1.0)
        before = links.at[1, "geometry"]
        net._straighten(links, source_lines=sources)
        self.assertTrue(before.equals_exact(links.at[1, "geometry"], 0))

    def test_straightening_restores_only_crossings_present_in_both_sources(self):
        """贴线隐藏的源交叉可恢复；缺少对方源线或源中未交叉时仍拒绝。"""
        for other_source, expected_straight in (
                (LineString([(19, -2), (19, 2)]), True),
                (LineString([(19, -.5), (19, .5)]), False),
                (None, False)):
            with self.subTest(other_source=other_source):
                links, sources = self.straight_case()
                links.loc[3] = dict(links.iloc[1], link_id=4, from_node=5, to_node=6,
                                    geometry=LineString([(19, -.5), (19, .5)]), length=1.0)
                if other_source is not None:
                    sources[3] = other_source
                before = links.at[1, "geometry"]
                net._straighten(links, source_lines=sources)
                geometry = links.at[1, "geometry"]
                if expected_straight:
                    self.assertEqual(list(geometry.coords), [(0, 0), (38.61, 0)])
                else:
                    self.assertTrue(before.equals_exact(geometry, 0))

    def test_source_crossing_does_not_allow_an_extra_crossing_with_same_link(self):
        """源中只有一次交叉，恢复直线不能与同一条对方 Link 穿越两次。"""
        links, sources = self.straight_case()
        links.loc[3] = dict(links.iloc[1], link_id=4, from_node=5, to_node=6,
                            geometry=LineString([(12, -2), (12, .5), (24, .5), (24, -2)]),
                            length=17.0)
        sources[3] = LineString([(19, -2), (19, 2)])
        before = links.at[1, "geometry"]
        net._straighten(links, source_lines=sources)
        self.assertTrue(before.equals_exact(links.at[1, "geometry"], 0))

    def test_right_turn_refinement_removes_reverse_bulge(self):
        links, sources = self.turn_case()
        self.assertGreater(float(np.degrees(np.diff(self.headings(links.at[1, "geometry"]))).max()), 1)
        net._refine_connectors(links, sources)
        geom = links.at[1, "geometry"]
        self.assertLessEqual(float(np.degrees(np.diff(self.headings(geom))).max()), .1)
        self.assertEqual(tuple(geom.coords[0]), (-3.5, 0))
        self.assertEqual(tuple(geom.coords[-1]), (10, 10))
        self.assertGreater(geom.length, math.hypot(13.5, 10) + .5)

    def test_left_turn_uses_symmetric_refinement(self):
        links, sources = self.turn_case(-1)
        net._refine_connectors(links, sources)
        self.assertGreaterEqual(float(np.degrees(np.diff(self.headings(links.at[1, "geometry"]))).min()), -.1)

    def test_turn_refinement_preserves_topology_and_attributes(self):
        links, sources = self.turn_case()
        before = links.drop(columns=["geometry", "length"]).copy(deep=True)
        net._refine_connectors(links, sources)
        pd.testing.assert_frame_equal(before, links.drop(columns=["geometry", "length"]))
        self.assertTrue(links.at[1, "geometry"].is_simple)
        self.assertEqual(verify.max_turn_angle(links.at[1, "geometry"]), 0)
        self.assertAlmostEqual(links.at[1, "length"], links.at[1, "geometry"].length, delta=.001)

    def test_refinement_preserves_real_source_s_curve(self):
        points = [(0, 0), (8, 0), (13, 3), (20, 3), (27, 0), (38.61, 0)]
        source = LineString(points)
        geometry = LineString([(x, y - 1.75) for x, y in points])
        links, sources = self.frame(source, geometry)
        net._refine_connectors(links, sources)
        self.assertTrue(geometry.equals_exact(links.at[1, "geometry"], 0))

    def test_refinement_rejects_self_intersecting_source(self):
        source = LineString([(0, 0), (10, 10), (0, 10), (10, 0)])
        geometry = LineString([(0, -1.75), (10, 8.25), (0, 8.25), (10, -1.75)])
        links, sources = self.frame(source, geometry)
        net._refine_connectors(links, sources)
        self.assertTrue(geometry.equals_exact(links.at[1, "geometry"], 0))

    def test_crossing_guard_counts_multiple_crossings_with_same_link(self):
        line = LineString([(0, 0), (10, 0)])
        other = LineString([(2, -2), (2, 2), (8, 2), (8, -2)])
        self.assertEqual(net._crossing_count(line, other), 2)

    def test_crossing_guard_ignores_floating_point_tangency(self):
        line = LineString([(0, 0), (10, 0)])
        other = LineString([(0, 0), (5, -1e-12), (10, 0)])
        self.assertEqual(net._crossing_count(line, other), 0)

    @unittest.skipUnless((ROOT / "data/Town03.xodr").is_file(), "缺少 Town03 源样例")
    def test_town03_corrects_straight_and_all_sampled_right_turn_bulges(self):
        """固定真实问题路口作回归；生产代码不按 link_id/road_id 特判。"""
        with tempfile.TemporaryDirectory(prefix="town03_connector_regression_") as temp:
            links, nodes = net.build_net(str(ROOT / "data/Town03.xodr"),
                                         str(Path(temp) / "net.gpkg"), verbose=False,
                                         shoulder_uturns=False)
        self.assertEqual((len(links), len(nodes)), (287, 204))
        node_xy = nodes.set_index("node_id").geometry
        # 三处源左右车道均相距 3.5 m；过滤的路肩/人行道连接路不能把它们串成一个节点。
        for rid, end in (("73", 0), ("41", 0), ("62", 1)):
            with self.subTest(road_id=rid, end=end):
                rows = links[(links.road_id == rid) & (links.link_type == "road")]
                forward = rows[rows.lane_ids == "-1"].iloc[0]
                reverse = rows[rows.lane_ids == "1"].iloc[0]
                a = forward.from_node if end == 0 else forward.to_node
                b = reverse.to_node if end == 0 else reverse.from_node
                self.assertNotEqual(a, b)
                self.assertAlmostEqual(node_xy[a].distance(node_xy[b]), 3.5, places=8)
                self.assertEqual((forward.snap_m, reverse.snap_m), (0, 0))
        self.assertNotIn("_source_geometry", links)
        straight = links[links.link_id == 244].iloc[0]
        self.assertEqual(len(straight.geometry.coords), 2)
        self.assertEqual(straight.length, 38.61)
        for lid in (116, 177, 178, 181, 182, 215, 277, 280):
            with self.subTest(link_id=lid):
                geom = links[links.link_id == lid].geometry.iloc[0]
                angles = np.degrees(np.diff(self.headings(geom)))
                self.assertLessEqual(float(angles[angles > 0].sum()), .1)

    @unittest.skipUnless((ROOT / "data/Town10HD.xodr").is_file(), "缺少 Town10HD 源样例")
    def test_town10hd_keeps_source_straight_connector_through_coincide(self):
        """路口源直行线与转弯本来相交，贴线不能先把直线改弯再阻止恢复。"""
        with tempfile.TemporaryDirectory(prefix="town10hd_straight_regression_") as temp:
            links, nodes = net.build_net(str(ROOT / "data/Town10HD.xodr"),
                                         str(Path(temp) / "net.gpkg"), verbose=False)
        self.assertEqual((len(links), len(nodes)), (98, 74))
        straight = links[links.link_id == 60].iloc[0]
        self.assertEqual((straight.road_id, straight.junction_id, straight.lane_ids),
                         ("255", "189", "-1,-2"))
        self.assertEqual((straight.from_node, straight.to_node, straight.lanes,
                          straight.oneway, straight.snap_m), (46, 52, 2, 1, 0))
        self.assertEqual(len(straight.geometry.coords), 2)
        self.assertAlmostEqual(straight.geometry.length, 38.11, places=8)
        self.assertEqual(straight.length, 38.11)
        node_xy = nodes.set_index("node_id").geometry
        self.assertEqual(straight.geometry.coords[0], node_xy[46].coords[0])
        self.assertEqual(straight.geometry.coords[-1], node_xy[52].coords[0])
        turn = links[links.link_id == 67].geometry.iloc[0]
        self.assertEqual(net._crossing_count(straight.geometry, turn), 1)

    @unittest.skipUnless((ROOT / "data/Town10HD.xodr").is_file(), "缺少 Town10HD 源样例")
    def test_town10hd_unsmoothed_extra_crossing_still_blocks_straightening(self):
        """关闭平滑且关闭转弯整理时，邻线回摆使源一次交叉变为两次，不能放行。"""
        with tempfile.TemporaryDirectory(prefix="town10hd_unsmoothed_regression_") as temp:
            links, _ = net.build_net(str(ROOT / "data/Town10HD.xodr"),
                                     str(Path(temp) / "net.gpkg"), smooth=0,
                                     refine_connectors=False, verbose=False)
        geometry = links[links.link_id == 60].geometry.iloc[0]
        turn = links[links.link_id == 67].geometry.iloc[0]
        chord = LineString([geometry.coords[0], geometry.coords[-1]])
        self.assertGreater(len(geometry.coords), 2)
        self.assertEqual(net._crossing_count(chord, turn), 2)
        self.assertEqual(net._crossing_count(geometry, turn), 0)

    @unittest.skipUnless((ROOT / "data/Town10HD.xodr").is_file(), "缺少 Town10HD 源样例")
    def test_town10hd_straight_86_refines_neighbor_turns_without_new_crossings(self):
        """直行与相邻两处转弯联合整理，打破互相阻挡，但源中没有的交叉仍不得出现。"""
        with tempfile.TemporaryDirectory(prefix="town10hd_joint_connector_regression_") as temp:
            links, nodes = net.build_net(str(ROOT / "data/Town10HD.xodr"),
                                         str(Path(temp) / "net.gpkg"), verbose=False)
        self.assertEqual((len(links), len(nodes)), (98, 74))
        row = links[links.link_id == 86].iloc[0]
        self.assertEqual((row.road_id, row.lane_ids, row.from_node, row.to_node,
                          row.lanes, row.oneway, row.passable, row.snap_m),
                         ("736", "-1", 70, 72, 1, 1, 1, 1.75))
        self.assertEqual(len(row.geometry.coords), 2)
        self.assertAlmostEqual(row.geometry.length, 46.15, places=8)
        self.assertEqual(row.length, 46.15)
        for lid in (88, 90):
            with self.subTest(link_id=lid):
                turn = links[links.link_id == lid].geometry.iloc[0]
                self.assertEqual(net._crossing_count(row.geometry, turn), 0)
                self.assertTrue(turn.is_simple)
                self.assertGreater(len(turn.coords), 2)
                self.assertLess(float(np.degrees(np.diff(self.headings(turn))).clip(min=0).sum()), 1.0)

    def test_joint_straightening_preserves_an_unrelated_road_obstacle(self):
        """联合整理仅处理共节点连接线，不能修改或越过不相关的主路。"""
        links, sources = self.straight_case()
        links.loc[3] = dict(links.iloc[0], link_id=4, from_node=5, to_node=6,
                            geometry=LineString([(19, -.5), (19, .5)]), length=1.0)
        sources[3] = links.at[3, "geometry"]
        before = links.copy(deep=True)
        net._refine_straight_neighbours(links, sources)
        pd.testing.assert_frame_equal(links, before)

    def test_failed_joint_connector_candidate_preserves_the_whole_group(self):
        """邻接连接线无法沿源切向消除穿越时，临时直线和转弯修改均不得写回。"""
        links, sources = self.straight_case()
        other = LineString([(0, 0), (5, -1), (10, .5), (15, -1)])
        links.loc[3] = dict(links.iloc[1], link_id=4, from_node=2, to_node=5,
                            geometry=other, length=round(other.length, 3))
        sources[3] = other
        before = links.copy(deep=True)
        net._refine_straight_neighbours(links, sources)
        pd.testing.assert_frame_equal(links, before)

    @unittest.skipUnless((ROOT / "data/Town10HD.xodr").is_file(), "缺少 Town10HD 源样例")
    def test_town10hd_can_disable_straightening_and_refinement(self):
        """两个形状开关各自生效；关闭拉直保留旧贴线，关闭转弯校正仍可拉直。"""
        for options, expected_straight in (
                ({"straighten": False}, False),
                ({"straighten": False, "refine_connectors": False}, False),
                ({"refine_connectors": False}, True)):
            with self.subTest(options=options):
                with tempfile.TemporaryDirectory(prefix="town10hd_no_straight_regression_") as temp:
                    links, _ = net.build_net(str(ROOT / "data/Town10HD.xodr"),
                                             str(Path(temp) / "net.gpkg"), verbose=False, **options)
                geometry = links[links.link_id == 60].geometry.iloc[0]
                if expected_straight:
                    self.assertEqual(len(geometry.coords), 2)
                    self.assertAlmostEqual(geometry.length, 38.11, places=8)
                else:
                    chord = LineString([geometry.coords[0], geometry.coords[-1]])
                    self.assertGreater(geometry.hausdorff_distance(chord), 1.0)
                    self.assertGreater(geometry.length, 38.4)

    def test_refinement_does_not_bend_a_confirmed_straight_connector_again(self):
        links, sources = self.straight_case()
        points = list(links.at[1, "geometry"].coords)
        points[-1] = (38.61, 2)
        links.at[1, "geometry"] = LineString(points)
        links.at[2, "geometry"] = LineString([(38.61, 2), (48.61, 2)])
        changed = set()
        net._straighten(links, source_lines=sources, changed=changed)
        net._refine_connectors(links, sources, skip=changed)
        self.assertEqual(list(links.at[1, "geometry"].coords), [(0, 0), (38.61, 2)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
