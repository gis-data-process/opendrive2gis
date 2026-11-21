"""诊断: 普通 road 的 link 直线度。

指标 sag = 各点到"首尾连线"的最大垂距 / 线长。
    sag < 0.01  基本是直线
    sag > 0.03  肉眼可见的弯

用法:
    python code/diag/straight.py out/Town01_net.gpkg
"""

import sys
import numpy as np
import geopandas as gpd


def sag(g):
    p = np.asarray(g.coords, dtype=float)
    a, b = p[0], p[-1]
    ab = b - a
    L = float(np.hypot(*ab))
    if L < 5.0 or len(p) < 3:
        return None
    t = (p - a) @ ab / (L * L)
    proj = a + t[:, None] * ab
    d = np.hypot(*(p - proj).T)
    return float(d.max() / L)


def gaps(path):
    """同 road 两侧主组 Link 的中段间距；排除 aux 及合法共享车道反向共线。

    行驶方向由各线坐标顺序表达，不读取方向标签；对两侧对称取样。
    """
    df = gpd.read_file(path, layer="Link")
    if "link_type" in df:
        df = df[df.link_type == "road"]
    out = []
    for _, sub in df.groupby("road_id"):
        if len(sub) != 2:
            continue
        if "shared_lane_ids" in sub and "lane_ids" in sub:
            shared = [{v.strip() for v in str(raw).split(",") if v.strip()}
                      if isinstance(raw, str) else set() for raw in sub.shared_lane_ids]
            ids = [{v.strip() for v in str(raw).split(",") if v.strip()}
                   if isinstance(raw, str) else set() for raw in sub.lane_ids]
            first, second = sub.iloc[0], sub.iloc[1]
            if (shared[0] and shared[0] == shared[1] == ids[0] == ids[1]
                    and first.from_node == second.to_node and first.to_node == second.from_node
                    and first.geometry.hausdorff_distance(second.geometry) < 1e-8):
                continue
        a, b = sub.geometry.tolist()
        if min(a.length, b.length) < 5.0:
            continue
        out.append(float(np.mean([line.interpolate(f, normalized=True).distance(other)
                                  for line, other in ((a, b), (b, a)) for f in (0.25, 0.5, 0.75)])))
    return out


def report(path, tag):
    df = gpd.read_file(path, layer="Link")
    df = df[df.link_type == "road"]
    s = df.geometry.map(sag).dropna()
    if len(s) == 0:
        print("%-28s 无有效样本" % tag)
        return
    g = gaps(path)
    print("%-28s n=%-4d 中位=%.4f  p90=%.4f  最大=%.4f  >0.03的条数=%d"
          "  | 双向对=%d 间距中位=%.2fm 最小=%.2fm 重叠=%d"
          % (tag, len(s), s.median(), s.quantile(0.9), s.max(),
             int((s > 0.03).sum()), len(g),
             float(np.median(g)) if g else 0.0, min(g) if g else 0.0,
             int(sum(x < 0.5 for x in g))))


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        report(arg, arg.split("/")[-1])
