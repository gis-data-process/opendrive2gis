"""路网文件写出与构建摘要；每个目标文件独立原子替换，失败保留旧文件。"""
from collections import Counter
import os

import geopandas as gpd
import numpy as np
import pyogrio

from common.files import atomic_output


def write_network(links, nodes, crs, gpkg_path, csv=False):
    """成功写完两个图层再替换 GPKG；CSV 单独保护，固定属性不包含内部掉头来源对象。"""
    node_xy = {int(row.node_id): (row.geometry.x, row.geometry.y) for _, row in nodes.iterrows()}
    # --- 7. 写出 ---
    lg = gpd.GeoDataFrame(links, geometry="geometry", crs=crs)
    ng = gpd.GeoDataFrame(nodes, geometry="geometry", crs=crs)
    with atomic_output(gpkg_path) as temporary:
        pyogrio.write_dataframe(ng, temporary, layer="Node", driver="GPKG", append=False)
        pyogrio.write_dataframe(lg, temporary, layer="Link", driver="GPKG", append=False)

    if csv:
        base = os.path.splitext(gpkg_path)[0]
        with atomic_output(base + "_link.csv") as temporary:
            lg.drop(columns="geometry").to_csv(temporary, index=False, encoding="utf-8-sig")
        with atomic_output(base + "_node.csv") as temporary:
            ng.drop(columns="geometry").assign(
                x=[node_xy[i][0] for i in ng.node_id],
                y=[node_xy[i][1] for i in ng.node_id],
            ).to_csv(temporary, index=False, encoding="utf-8-sig")


    return lg, ng


def print_summary(source, topology, links, nodes, components, isolated, endpoint_stats,
                  refinement_stats, emission_stats, xodr_path, gpkg_path, smooth):
    """输出一次构建的诊断，统计只读自本次结果，不依赖模块级计数器。"""
    comp = components
    n_comp = len(set(comp.values()))
    comp_size = max(Counter(comp.values()).values()) if comp else 0
    n_iso = isolated
    endpoints, refinements = endpoint_stats, refinement_stats
    n_nodrv, n_aux, aux_roads = emission_stats
    n_merge, n_smooth, n_re = refinements.merged, refinements.smoothed, refinements.repeated_smoothing
    dlen, n_coincide, n_mid = refinements.length_changes, refinements.coincided, refinements.mid_coincided
    n_straight, curve_ids = len(refinements.straightened), refinements.refined
    print("输入 : %s" % xodr_path)
    print("道路 : %d 条(其中连接路 %d)" % (len(source.roads), len(source.connector_directions)))
    print("节点 : %d 个(端口 %d, 按 <link> 声明合并 %d 次)" %
          (len(nodes), topology.port_count, topology.declared_merges))
    print("Link : %d 条, 总长 %.1f km" % (len(links), links.length.sum() / 1000))
    for t, g in links.groupby("link_type"):
        print("        %-10s %5d 条  %8.1f km" % (t, len(g), g.length.sum() / 1000))
    n_bad = int((links.passable == 0).sum())
    if n_bad:
        print("       其中 %d 条是路口转向路径但该方向没有允许车道(passable=0)" % n_bad)
    if n_nodrv:
        print("       丢掉 %d 条 road: 没有类型允许的可通行车道" % n_nodrv)
    if n_aux:
        print("辅道 : %d 条渐变辅道单独出 link(link_type=aux), 涉及 %d 条 road"
              % (n_aux, len(aux_roads)))
    redundant_auxiliaries = links.attrs.get("redundant_auxiliaries", [])
    if redundant_auxiliaries:
        print("       省略 %d 条同 road 同起终节点的重复辅道，主组路线不变"
              % len(redundant_auxiliaries))
    dangling = int((nodes.degree == 1).sum())
    print("连通 : 弱连通分量 %d 个(最大 %d 个节点)" % (n_comp, comp_size))
    print("       悬挂节点(度=1) %d 个 —— 路网末端, 正常" % dangling)
    if n_iso:
        print("       剔除孤立节点 %d 个(所在 road 没出 link, 已不写进 Node 表)" % n_iso)
    if n_comp > 1:
        # 断成几块时直接报出是哪些 road, 省得再去翻 xodr
        sizes = Counter(comp.values())
        top = max(sizes.values())
        small = [c for c, n in sizes.items() if n < top]
        link_components = links.from_node.map(comp)
        for c in sorted(small, key=lambda c: -sizes[c])[:5]:
            ids = sorted(set(links.loc[link_components == c, "road_id"]),
                         key=lambda x: int(x) if x.isdigit() else 0)
            print("       ⚠️ 小分量 %d: %d 节点, road %s"
                  % (c, sizes[c], ids[:8]))
    sm = links.snap_m.values
    print("修正 : link 端点被节点拽动 中位 %.3f m, 90分位 %.3f m, 最大 %.3f m"
          % (np.median(sm), np.percentile(sm, 90), sm.max()))
    for t, g in links.groupby("link_type"):
        print("        %-10s 端点修正 中位 %.3f m, 最大 %.3f m"
              % (t, np.median(g.snap_m), g.snap_m.max()))
    if n_merge:
        print("合并 : 同一转向去掉 %d 条平行连接路, 只留最中间那条" % n_merge)
    if n_smooth:
        dl = np.array(dlen) if dlen else np.array([0.0])
        print("平滑 : %d 条(σ=%.1f m), 长度变化 中位 %+.2f m / 最大 %+.2f m"
              % (n_smooth, smooth, np.median(dl), dl[np.argmax(np.abs(dl))]))
    if n_re:
        print("       其中 %d 处急弯一遍压不平, 补平滑(最多 3 遍, 有少量长度收缩)"
              % n_re)
    if n_coincide:
        print("合流 : %d 条连接路端部斜穿改为贴线重合(交点后沿对方线并入节点)" % n_coincide)
    if n_mid:
        print("贴线 : %d 处中段切角(扎进对方弯里又切出去)改为沿对方绕行后切向出弯"
              % n_mid)
    if endpoints.blends:
        print("并入 : %d 处端部大横向偏移改为沿行驶方向贝塞尔并入(双圆弧拒收的兜底)"
              % endpoints.blends)
    if endpoints.paths:
        for _t in ("road", "connector", "aux"):
            _parts = ["%s:%d" % (k, v) for (tt, k), v in sorted(endpoints.paths.items())
                      if tt == _t]
            if _parts:
                print("端部 : %-10s %s  (端数; tail=补直线, repl=挪端点)"
                      % (_t, "  ".join(_parts)))
    if n_straight:
        print("直行 : %d 条源共线连接路改为节点间直线(有向邻居顺直、无新增穿越)"
              % n_straight)
    if curve_ids:
        print("曲线 : %d 条连接路按源中线和有向邻居消除接点回摆" % len(curve_ids))
    print("输出 : %s (Node + Link)" % gpkg_path)

    movements = links.attrs.get("junction_movements", [])
    uturns = {item["output_link_id"] for item in movements
              if item["movement"] == "uturn" and item["output_link_id"] is not None}
    if uturns:
        print("掉头 : %d 条单向连接线，保留源回转弧与独立对向节点" % len(uturns))
