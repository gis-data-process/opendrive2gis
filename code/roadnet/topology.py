"""由有效车道组端口建立节点，再统计真实输出边的度数和弱连通分量。"""
from collections import defaultdict, Counter
from dataclasses import dataclass
import math

import numpy as np
import pandas as pd
from shapely.geometry import Point

from .lanes import is_connector_port


class UF:
    def __init__(self):
        self.p = {}

    def add(self, x):
        self.p.setdefault(x, x)

    def find(self, x):
        self.add(x)
        r = x
        while self.p[r] != r:
            r = self.p[r]
        while self.p[x] != r:      # 路径压缩
            self.p[x], x = r, self.p[x]
        return r

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


@dataclass
class Topology:
    """源端口到输出节点的映射；坐标和来源同属于一次构建，禁止跨任务复用。"""

    node_id: dict
    node_xy: dict
    node_spread: dict
    node_junc: dict
    port_count: int
    declared_merges: int


def build_topology(source, snap, keep_conn):
    """先按距离与高程吸附，再按有效源方向合并；过滤连接方向不能桥接普通路两侧。"""
    roads, center = source.roads, source.center
    conn_flag, conn_dir = source.connector_flags, source.connector_directions
    side_pts, auxiliary_ends = source.groups, source.auxiliary_ends
    dirs_of, group_port = source.dirs_of, source.group_port
    # --- 3. 端口并查集: 坐标容差 + <link> 声明 ---
    # 主组端口按 **(路, 端, 行驶方向)** 拆；有源连接的全宽辅道再追加组下标。
    # 双向路的每一端有两个方向端口, 分别落在
    # 正向/反向车道群的中心线上。这样正向/反向 link 从头到尾是分开的两条,
    # 端点就是它自己的端点 —— 不必为了"接上节点"把直路在两端掰回中心线。
    # 拓扑连通改为让路口里的连接路去贴合这些端口(第 5c 步的平滑负责抹平)。
    ports, port_z, topology_ports, shoulder_ports = {}, {}, set(), set()
    for rid, pts in center.items():
        for d in dirs_of(rid):
            groups = side_pts.get((rid, d)) or []
            if not groups and not (keep_conn and conn_flag[rid]):
                continue
            for gi, group in enumerate(groups or [{"pts":pts}]):
                for end, index in ((0, 0), (1, -1)):
                    key = group_port(rid, end, d, gi)
                    if key in ports:
                        continue       # 收窄或无独立源连接的 aux 端口仍复用主组。
                    ports[key] = np.asarray(group["pts"][index], dtype=float)
                    port_z[key] = roads[rid].elevation_at(0 if end == 0 else roads[rid].length)
                    if groups:
                        topology_ports.add(key)
                    if group.get("shoulder_uturn"):
                        shoulder_ports.add(key)

    uf = UF()
    for p in ports:
        uf.add(p)

    # 3a 只合并有效车道方向；占位参考线即使重合，也不能桥接两侧通行端口。
    # 被过滤的连接路曾在出 Link 前用参考线占位，传递合并了相距 3.5 m 的对向车道。
    # 网格索引将候选范围限制到邻格；高程差不能靠 2D 吸附消掉。
    grid = defaultdict(list)
    cell = max(snap, 1e-6)
    for key, xy in ports.items():
        if key not in topology_ports or key in shoulder_ports:
            continue
        gx, gy = (int(math.floor(v / cell)) for v in xy)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid[(gx + dx, gy + dy)]:
                    if (np.linalg.norm(xy - ports[other]) <= snap
                            and abs(port_z[key] - port_z[other]) <= max(snap, 0.1)):
                        uf.union(key, other)
        grid[(gx, gy)].append(key)

    # 3b <link> 声明: 不管相距多远, 声明相接就是同一个节点(路口内的偏移在这里被吃掉)。
    # 方向要"出口接入口": 我的 mine 端接对方的 theirs 端时
    #     端号不同(一头起点一头终点) -> 内部方向符号相同
    #     端号相同                   -> 内部方向符号相反
    # 两边都为有效车道方向才合并；keep_conn 的 passable=0 占位端口保留独立节点。
    n_decl = 0
    for rid, rd in roads.items():
        lk = source.road_links[rid]
        for tag, mine in (("predecessor", 0), ("successor", 1)):
            e = lk.get(tag)
            if not e or e[1] != "road" or e[0] not in roads:
                continue
            theirs = 0 if e[2] == "start" else 1
            for d in dirs_of(rid):
                d_o = d if mine != theirs else -d
                own_groups = side_pts.get((rid, d)) or []
                other_groups = side_pts.get((e[0], d_o)) or []
                for gi, group in enumerate(own_groups):
                    mine_port = group_port(rid, mine, d, gi)
                    if group.get("shoulder_uturn"):
                        # 路肩回转线只接已核实的机动车端口；不参加距离吸附，不传递合并对向组。
                        target_port = group_port(*group["endpoint_targets"][mine])
                        if target_port in topology_ports:
                            uf.union(mine_port, target_port)
                            n_decl += 1
                        continue
                    target_gi = (group["end_target_groups"][mine]
                                 if conn_flag[rid] and "end_target_groups" in group else 0)
                    target_port = group_port(e[0], theirs, d_o, target_gi)
                    if not conn_flag[rid]:
                        # road→road 声明没有逐车道映射时，独立辅道不能兜底接到主组。
                        if len(mine_port) > 3:
                            continue
                        if sum(not g.get("shoulder_uturn") for g in other_groups) > 1 and conn_flag[e[0]]:
                            continue
                    if mine_port in topology_ports and target_port in topology_ports:
                        uf.union(mine_port, target_port)
                        n_decl += 1

    # --- 4. 节点坐标 = 组内端口质心 ---
    # 端口所属 junction: 连接路取自己的 junction; 普通路看该端 <link> 是否指向 junction
    port_junc = {}
    for port in ports:
        rid, end = port[:2]
        rd = roads[rid]
        own = rd.junction if rd.junction != "-1" else "-1"
        lk = source.road_links[rid]
        e = lk.get("predecessor" if end == 0 else "successor")
        port_junc[port] = e[0] if (e and e[1] == "junction") else own

    groups = defaultdict(list)
    for p in ports:
        groups[uf.find(p)].append(p)
    node_id, node_xy, node_spread, node_junc = {}, {}, {}, {}
    # 新路肩端口附着已有组，编号排序仍以原端口为准，避免无关 Node 全局重新编号。
    order = lambda item: min([p for p in item[1] if p not in shoulder_ports] or item[1])
    for i, (_, members) in enumerate(sorted(groups.items(), key=order), start=1):
        # 节点坐标优先取"普通路段"端口: 路口内偏移是连接路自己造成的,
        # 让短的连接路去贴合主路, 别把主路端点拽进路口(那样路段长度会失真)
        arr = np.array([ports[m] for m in members])
        plain = np.array([ports[m] for m in members
                          if not is_connector_port(m, roads, conn_dir)])
        xy = plain.mean(axis=0) if len(plain) else arr.mean(axis=0)
        for m in members:
            node_id[m] = i
        node_xy[i] = (float(xy[0]), float(xy[1]))
        # 组内坐标离散度, 用来评估"这个节点把线拉歪了多少"
        node_spread[i] = float(np.linalg.norm(arr - xy, axis=1).max()) if len(arr) else 0.0
        js = [port_junc[m] for m in members if port_junc.get(m, "-1") != "-1"]
        node_junc[i] = js[0] if js else "-1"


    return Topology(node_id, node_xy, node_spread, node_junc, len(ports), n_decl)


def summarize_nodes(links, topology):
    """长度整理后按输出 Link 重新计数；只剔除零度点，不删除正常道路末端。"""
    node_xy, node_junc, node_spread = topology.node_xy, topology.node_junc, topology.node_spread
    # --- 6. 节点度数 + 连通分量 ---
    deg_in = defaultdict(int)
    deg_out = defaultdict(int)
    adj = defaultdict(set)
    for _, r in links.iterrows():
        deg_out[r.from_node] += 1
        deg_in[r.to_node] += 1
        adj[r.from_node].add(r.to_node)
        adj[r.to_node].add(r.from_node)

    nodes = pd.DataFrame([{
        "node_id": i,
        "junction_id": node_junc[i],
        "degree": deg_in[i] + deg_out[i],
        "n_in": deg_in[i],
        "n_out": deg_out[i],
        "geometry": Point(node_xy[i]),
    } for i in sorted(node_xy)])
    nodes["spread"] = [round(node_spread[i], 3) for i in nodes.node_id]
    # 度=0 的节点没有任何 link(源路被过滤掉了), 留在表里只会污染导航数据
    n_iso = int((nodes.degree == 0).sum())
    if n_iso:
        nodes = nodes[nodes.degree > 0].reset_index(drop=True)

    # 分量只统计"进了 Node 表"的节点, 已剔除的孤立节点不算一块
    comp = _components(adj, sorted(nodes.node_id))
    n_comp = len(set(comp.values())) if len(comp) else 0
    comp_size = max(Counter(comp.values()).values()) if comp else 0


    return nodes, comp, n_comp, comp_size, n_iso


def _components(adj, node_list):
    """弱连通分量: 返回 {node: 分量号}(纯 BFS, 不引 networkx)"""
    comp = {}
    cid = 0
    for s in node_list:
        if s in comp:
            continue
        cid += 1
        stack = [s]
        comp[s] = cid
        while stack:
            v = stack.pop()
            for w in adj.get(v, ()):
                if w not in comp:
                    comp[w] = cid
                    stack.append(w)
    return comp
