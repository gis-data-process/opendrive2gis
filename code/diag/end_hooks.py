# -*- coding: utf-8 -*-
"""端部缺陷体检: 端部急弯(小钩)、自交、同一对 link 多次交叉、小夹角穿越。

用法:
    python code/diag/end_hooks.py                # 扫 out/*_net.gpkg
    python code/diag/end_hooks.py --dir out/_new # 扫别的目录
"""
import os, sys, glob, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import math
import numpy as np
import pyogrio
from shapely.strtree import STRtree
from shapely.geometry import MultiPoint, Point
import common.paths as _paths


def _resample_xy(c, ds=0.25):
    d = np.hypot(*np.diff(c, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(d)])
    if s[-1] <= ds:
        return c
    t = np.arange(0.0, s[-1] + 1e-9, ds)
    return np.column_stack([np.interp(t, s, c[:, 0]), np.interp(t, s, c[:, 1])])


def min_radius_near_ends(g, span=6.0, win=1.0):
    """两端各 span 米内的最小曲率半径(用三点外接圆), 没有弯就返回 inf"""
    c = np.asarray(g.coords, dtype=float)[:, :2]
    if len(c) < 4 or g.length < 3.0:
        return float("inf")
    p = _resample_xy(c, 0.25)
    step = max(int(round(win / 0.25)), 1)
    L = (len(p) - 1) * 0.25
    s_all = np.arange(len(p)) * 0.25
    mask = (s_all < span) | (s_all > L - span)
    rmin = float("inf")
    for i in range(step, len(p) - step):
        if not mask[i]:
            continue
        a, b, cc = p[i - step], p[i], p[i + step]
        ab = np.hypot(*(b - a))
        bc = np.hypot(*(cc - b))
        ca = np.hypot(*(a - cc))
        area2 = abs((b[0] - a[0]) * (cc[1] - a[1]) - (cc[0] - a[0]) * (b[1] - a[1]))
        if area2 < 1e-9:
            continue
        r = ab * bc * ca / (2.0 * area2)
        rmin = min(rmin, r)
    return rmin


def tangent_at(g, pt):
    s = g.project(pt)
    d = min(2.0, max(0.2, g.length * 0.1))
    a = g.interpolate(max(s - d, 0.0))
    b = g.interpolate(min(s + d, g.length))
    v = np.array([b.x - a.x, b.y - a.y])
    n = np.linalg.norm(v)
    return v / n if n > 1e-9 else np.array([1.0, 0.0])


def scan(path, r_hook=3.0, edge_m=2.0, ang_shallow=30.0):
    L = pyogrio.read_dataframe(path, layer="Link")
    ids = L["link_id"].values
    geoms = list(L.geometry.values)
    res = dict(n=len(geoms), hooks=0, rmin=float("inf"), hook_ids=[], overlap=0,
               multi=[], shallow=0, shallow_pairs=0, pairs=0)
    for k, g in enumerate(geoms):
        if g is None or g.length < 3.0:
            continue
        r = min_radius_near_ends(g)
        if r < res["rmin"]:
            res["rmin"] = r
        if r < r_hook:
            res["hooks"] += 1
            if len(res["hook_ids"]) < 6:
                res["hook_ids"].append((int(ids[k]), round(r, 2)))
    tree = STRtree(geoms)
    seen = set()
    for i, g in enumerate(geoms):
        if g is None or g.length < 2.0:
            continue
        for j in tree.query(g.buffer(0.05)):
            j = int(j)
            if j <= i or (i, j) in seen:
                continue
            seen.add((i, j))
            h = geoms[j]
            if h is None or h.length < 2.0:
                continue
            inter = g.intersection(h)
            if inter.is_empty:
                continue
            res["pairs"] += 1
            # 近共线重叠(交集含线)不算穿越, 单独统计
            if getattr(inter, "length", 0.0) > 1.0:
                res["overlap"] += 1
                continue
            pts = []
            if isinstance(inter, Point):
                pts = [inter]
            elif isinstance(inter, MultiPoint):
                pts = list(inter.geoms)
            elif hasattr(inter, "geoms"):
                pts = [x for x in inter.geoms
                       if isinstance(x, Point) and not getattr(x, "length", 0.0)]
            # 只留"内部穿越点"(离两条线端点都 > edge_m), 端部相接不算穿越
            inner = [q for q in pts
                     if min(g.project(q), g.length - g.project(q)) > edge_m
                     and min(h.project(q), h.length - h.project(q)) > edge_m]
            if len(inner) >= 2:
                if max(inner[k].distance(inner[l]) for k in range(len(inner))
                       for l in range(k + 1, len(inner))) > 1.5:
                    res["multi"].append((int(ids[i]), int(ids[j]), len(inner)))
            for q in inner:
                t1, t2 = tangent_at(g, q), tangent_at(h, q)
                a = math.degrees(math.acos(min(1.0, abs(float(np.dot(t1, t2))))))
                if a < ang_shallow:
                    res["shallow"] += 1
                    res["shallow_pairs"] += 1
                    break
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(_paths.OUT))
    ap.add_argument("--glob", default="*_net.gpkg")
    ap.add_argument("--skip", default="_")
    a = ap.parse_args()
    files = sorted(f for f in glob.glob(os.path.join(a.dir, a.glob))
                   if not os.path.basename(f).startswith(a.skip))
    tot = dict(hooks=0, links=0, multi=0, shallow=0, overlap=0)
    print("%-28s %5s %6s %8s %6s %8s %6s" %
          ("file", "links", "hook", "minR(m)", "multi", "shallow", "ovlp"))
    for f in files:
        r = scan(f)
        tot["links"] += r["n"]; tot["hooks"] += r["hooks"]
        tot["multi"] += len(r["multi"]); tot["shallow"] += r["shallow"]
        tot["overlap"] += r["overlap"]
        print("%-28s %5d %6d %8.2f %6d %8d %6d" %
              (os.path.basename(f)[:28], r["n"], r["hooks"], r["rmin"],
               len(r["multi"]), r["shallow"], r["overlap"]))
        if r["hook_ids"]:
            print("      tight ends:", r["hook_ids"])
        for m in r["multi"][:5]:
            print("      multi-cross %s <-> %s (%d pts)" % m)
    print("%-28s %5d %6d %8s %6d %8d %6d" %
          ("TOTAL", tot["links"], tot["hooks"], "", tot["multi"],
           tot["shallow"], tot["overlap"]))


if __name__ == "__main__":
    main()
