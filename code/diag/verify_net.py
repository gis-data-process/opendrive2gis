"""独立只读校验 out/Town*_net.gpkg 的拓扑与几何质量。

检查连通分量、孤立节点、通行标志、首尾坐标、全部内部折角及正反向重叠。
默认要求一个弱连通分量；多片真实路网可显式指定 --components。
"""
# ---- 路径引导: 本文件在 code/diag/ 下, 把 code/ 加进 sys.path 后再取项目根常量 ----
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from common.paths import OUT  # noqa: E402

import glob
import argparse
import math
import os
import sys

import geopandas as gpd
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def max_turn_angle(geom, thresh_deg=25.0):
    """统计全部内部顶点的折角；不跳过短线和接入口，零长度段不参与角度计算。"""
    if geom is None or geom.is_empty or geom.geom_type != "LineString":
        return 0
    c = list(geom.coords)
    if len(c) < 3:
        return 0
    a = np.asarray(c, dtype=float)[:, :2]
    v1 = a[1:-1] - a[:-2]
    v2 = a[2:] - a[1:-1]
    n1 = np.linalg.norm(v1, axis=1)
    n2 = np.linalg.norm(v2, axis=1)
    ok = (n1 > 1e-9) & (n2 > 1e-9)
    cos = np.clip((v1 * v2).sum(1) / np.maximum(n1 * n2, 1e-12), -1, 1)
    ang = np.degrees(np.arccos(cos))
    ang = np.where(ok, ang, 0.0)
    return int((ang > thresh_deg).sum())


def check(path, expected_components=1):
    """只读校验 road 级图；数字化首末点须对应 from_node/to_node，无需方向标签。

    同一源 road 每方向只有一条主组；共享车道标识且反向端点对应时允许共线。
    失败返回 False，缺少表或必要字段抛出明确异常。
    """
    link = gpd.read_file(path, layer="Link")
    node = gpd.read_file(path, layer="Node")
    name = os.path.basename(path)
    link_fields = {"link_id", "road_id", "from_node", "to_node", "passable", "length", "oneway", "shared_lane_ids"}
    node_fields = {"node_id", "degree", "n_in", "n_out"}
    if not link_fields <= set(link) or not node_fields <= set(node):
        raise ValueError("Link/Node 缺少必要字段")
    if len(link) == 0 or len(node) == 0:
        print(f"{name}: FAIL 空路网")
        return False
    if (link.geometry.isna().any() or link.geometry.is_empty.any()
            or not link.geometry.is_valid.all() or node.geometry.isna().any()
            or node.geometry.is_empty.any()):
        print(f"{name}: FAIL 空或无效几何")
        return False
    if not (link.geometry.geom_type == "LineString").all() or not (node.geometry.geom_type == "Point").all():
        print(f"{name}: FAIL 几何类型错误")
        return False
    ids = set(node.node_id)
    if (not node.node_id.is_unique or not link.link_id.is_unique
            or not (set(link.from_node) | set(link.to_node)) <= ids):
        print(f"{name}: FAIL ID 重复或节点引用不存在")
        return False

    # 1) 弱连通分量（把 link 当无向边）
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in zip(link.from_node, link.to_node):
        union(a, b)
    comps = len({find(n) for n in node.node_id})

    # 2) 孤立节点
    deg = pd.concat([link.from_node, link.to_node]).value_counts()
    iso = int((node.node_id.map(lambda n: deg.get(n, 0)).fillna(0) == 0).sum())
    n_in = link.to_node.value_counts()
    n_out = link.from_node.value_counts()
    degrees_ok = all(r.degree == deg.get(r.node_id, 0)
                     and r.n_in == n_in.get(r.node_id, 0)
                     and r.n_out == n_out.get(r.node_id, 0) for _, r in node.iterrows())

    # 3) passable
    npass0 = int((link.passable == 0).sum()) if "passable" in link.columns else -1
    one_way_ok = bool((link.oneway == 1).all())

    # 4) 端点误差
    xy = dict(zip(node.node_id, zip(node.geometry.x, node.geometry.y)))
    err = 0.0
    for g, a, b in zip(link.geometry, link.from_node, link.to_node):
        c = list(g.coords)
        err = max(err, math.hypot(c[0][0] - xy[a][0], c[0][1] - xy[a][1]))
        err = max(err, math.hypot(c[-1][0] - xy[b][0], c[-1][1] - xy[b][1]))

    # 5) 折角
    kinks = int(link.geometry.apply(max_turn_angle).sum())

    nm = int((link.n_merged > 0).sum()) if "n_merged" in link.columns else 0
    total = link.geometry.length.sum()
    lengths_ok = np.isfinite(link["length"]).all() and np.allclose(
        link["length"], link.geometry.length, atol=0.001, rtol=0)
    coords_ok = np.isfinite(node.geometry.x).all() and np.isfinite(node.geometry.y).all()
    coords_ok = coords_ok and all(np.isfinite(np.asarray(g.coords)).all() for g in link.geometry)

    # 6) 对向重叠：每个 road 每个行驶方向只出一个主组，先排除同方向的 aux。
    # 两侧间距由坐标计算，不依赖额外方向字段或数据行排列顺序。
    pairs, tight, shared_pairs, duplicates = 0, 0, 0, 0
    gaps = []
    ordinary = link[link.link_type == "road"] if "link_type" in link else link
    for _, sub in ordinary.groupby("road_id"):
        if len(sub) > 2 or sub.duplicated(subset=["from_node", "to_node"]).any():
            duplicates += 1
        if len(sub) != 2:
            continue
        pairs += 1
        shared = [{value.strip() for value in str(raw).split(",") if value.strip()}
                  if pd.notna(raw) else set() for raw in sub.shared_lane_ids]
        lane_ids = [{value.strip() for value in str(raw).split(",") if value.strip()}
                    if pd.notna(raw) else set() for raw in sub.lane_ids] if "lane_ids" in sub else [set(), set()]
        arow, brow = sub.iloc[0], sub.iloc[1]
        shared_only = shared[0] and shared[0] == shared[1] == lane_ids[0] == lane_ids[1]
        reverse_nodes = arow.from_node == brow.to_node and arow.to_node == brow.from_node
        reverse_shape = (arow.geometry.hausdorff_distance(brow.geometry) < 1e-8
                         and np.linalg.norm(np.asarray(arow.geometry.coords[0]) - np.asarray(brow.geometry.coords[-1])) < 1e-8
                         and np.linalg.norm(np.asarray(arow.geometry.coords[-1]) - np.asarray(brow.geometry.coords[0])) < 1e-8)
        if shared_only and reverse_nodes and reverse_shape:
            shared_pairs += 1
            continue
        a, b = sub.geometry.tolist()
        # 5 m 以内的短路段：几个采样点，横向分不开也不影响看图，不计入
        if min(a.length, b.length) < 5.0:
            continue
        g = float(np.mean([line.interpolate(f, normalized=True).distance(other)
                           for line, other in ((a, b), (b, a)) for f in (0.25, 0.5, 0.75)]))
        gaps.append(g)
        if g < 0.5:
            tight += 1
    gap_med = float(np.median(gaps)) if gaps else 0.0

    ok = (comps == expected_components) and (iso == 0) and (npass0 == 0) and (err < 1e-9) \
        and (kinks == 0) and (tight == 0) and (duplicates == 0) and one_way_ok and degrees_ok and lengths_ok and coords_ok
    print(
        f"{name:22s} node={len(node):4d} link={len(link):4d} "
        f"comps={comps} iso={iso} pass0={npass0} "
        f"err={err:.2e} kink>25={kinks:3d} merged={nm:3d} "
        f"len={total/1000:.2f}km | 双向对={len(gaps):3d} 中位间距={gap_med:.2f}m "
        f"共享双向={shared_pairs:2d} 重复主组={duplicates:2d} 重叠={tight:2d} "
        f"oneway={'OK' if one_way_ok else 'FAIL'} degree={'OK' if degrees_ok else 'FAIL'} "
        f"length={'OK' if lengths_ok else 'FAIL'} {'OK' if ok else 'FAIL'}"
    )
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--dir", default=OUT)
    ap.add_argument("--glob", default="Town*_net.gpkg")
    ap.add_argument("--components", type=int, default=1)
    args = ap.parse_args()
    files = args.files or sorted(glob.glob(os.path.join(args.dir, args.glob)))
    if not files:
        ap.error("没有待检查的路网产物")
    if args.components < 1:
        ap.error("--components 必须为正整数")
    res = []
    for path in files:
        try:
            res.append(check(path, args.components))
        except Exception as exc:
            print(f"[FAIL] {path}: {exc}", file=sys.stderr)
            res.append(False)
    print("\nALL PASS" if all(res) else "\nSOME FAILED")
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
