"""整理同节点端部合流及环岛中段切角；受保护的源掉头路径不参与贴线。"""
from collections import defaultdict
import math

import numpy as np
from shapely.geometry import LineString, Point
from shapely.ops import substring

from .geometry import (_fillet_at, _geom_points, _joint_kink, _kink_near,
                       _really_crosses, _resample, _smooth_patch, _tangent)


def _coincide(links, node_xy, fillet=4.0, window=15.0, min_left=3.0,
              node_tol=1.0, changed=None, skip=()):
    """贴线重合合流(5d): 连接路端部若与同节点的其它 link 相交,
    在离节点最远的交点处切断自己的路径, 端部改沿对方折线走到底(重合段)。

    背景: 右转连接路按源数据落在直行 link 的外侧车道, 而节点取的是车道群
    中线 —— 连接路必须横穿直行线才能到节点(实测 Town03/1135: link 215 距
    节点 12.7 m 处斜穿 link 217)。把交点以后的一段改成与直行线重合, 就是
    "贴内侧合流": 视觉零交叉, 端点仍严格在节点上, 拓扑不变。

    护栏:
      - 只处理真穿越(交点两侧的点分居对方线两侧), 相切/搭接不动
      - 交点距节点 < node_tol 的不算(所有 link 本来就在节点相会)
      - 切完至少还剩 min_left 米自己的路径, 且交点须落在端部 window 米内
    返回改写的条数。
    """
    inc = defaultdict(list)
    for i in links.index:
        inc[links.at[i, "from_node"]].append(i)
        inc[links.at[i, "to_node"]].append(i)

    n_changed = 0
    for i in list(links.index[links.link_type == "connector"]):
        if i in skip:
            continue
        for end in ("tail", "head"):
            g = links.at[i, "geometry"]
            L = g.length
            if L < 2.0 * min_left + 1.0:
                break
            nd = links.at[i, "to_node" if end == "tail" else "from_node"]
            B = np.asarray(node_xy[nd], dtype=float)
            s0, s1 = ((max(L - window, 0.0), L) if end == "tail"
                      else (0.0, min(window, L)))
            if s1 - s0 < node_tol:
                continue
            sub = substring(g, s0, s1)
            best = None                       # (离节点距离, 对方行号, 交点)
            for j in inc[nd]:
                if j == i:
                    continue
                xg = links.at[j, "geometry"]
                for pt in _geom_points(sub.intersection(xg)):
                    p = np.asarray(pt.coords[0], dtype=float)
                    dn = float(np.hypot(*(p - B)))
                    if dn < node_tol:
                        continue
                    if not _really_crosses(g, xg, Point(p)):
                        continue
                    if best is None or dn > best[0]:
                        best = (dn, j, Point(p))
            if best is None:
                continue
            _, j, P = best
            xg = links.at[j, "geometry"]
            s_cut = float(g.project(P))
            if s_cut < min_left or L - s_cut < min_left:
                continue
            if end == "tail":
                # 尾段: 本线 A->P + 对方 P->B(首坐标必是 P, 末坐标必是 B)
                xpart = substring(xg, xg.project(P), xg.project(Point(B)))
                own = substring(g, 0.0, s_cut)
                a, b = list(own.coords), list(xpart.coords)
                b[0] = a[-1]        # 两边各自插值出的 P 差 ~1cm, 强制重合免生微段
                joint = len(a) - 1
                new = a + b
                back_len, fwd_len = s_cut, float(xpart.length)
            else:
                # 首段: 对方 A->P(首坐标必是 A) + 本线 P->尾
                xpart = substring(xg, xg.project(Point(B)), xg.project(P))
                own = substring(g, s_cut, L)
                a, b = list(xpart.coords), list(own.coords)
                a[-1] = b[0]        # 同上
                joint = len(a) - 1
                new = a + b[1:]
                back_len, fwd_len = float(xpart.length), L - s_cut
            ded, jmap = [new[0]], [0]        # jmap: new 下标 -> ded 下标
            for q in new[1:]:
                if abs(q[0] - ded[-1][0]) > 1e-9 or abs(q[1] - ded[-1][1]) > 1e-9:
                    ded.append(q)
                    jmap.append(jmap[-1] + 1)
                else:
                    jmap.append(jmap[-1])
            if len(ded) < 2:
                continue
            q = np.asarray(ded, dtype=float)
            joint_d = jmap[joint]
            if (0 < joint_d < len(q) - 1 and fillet > 0
                    and _joint_kink(q, joint_d) > 25.0):
                joint_pt = q[joint_d].copy()
                q = _fillet_at(q, joint_d,
                               min(fillet, 0.45 * back_len),
                               min(fillet, 0.45 * fwd_len))
                if _kink_near(q, joint_pt) > 25.0:
                    q = _smooth_patch(q, joint_pt)
            g2 = LineString(q)
            links.at[i, "geometry"] = g2
            links.at[i, "length"] = round(g2.length, 3)
            if changed is not None:
                changed.add(i)          # 后续仅按源形状、有向邻居及穿越护栏重检。
            n_changed += 1
    return n_changed


def _mid_coincide(links, max_span=45.0, max_ride=70.0, max_jump=35.0,
                  max_depth=3.5, shallow=40.0, steep=45.0, min_left=3.0,
                  ramp=8.0, exit_back=10.0, max_pass=2, changed=None, skip=()):
    """中段切角修正(5e): 连接路中段"扎进另一条 link 的弯里再切出去"(切角),
    改成贴着对方绕一段再从切向出弯 —— 视觉上不再斜穿对方。

    背景(Town03 junction 1469 环岛, link 254 = road 1608):
    环岛里同一条环带被好几条连接路共用, 源数据的参考线本来就互相叠着。
    我们按 laneSection 取"车道群中心线的中位数", 车道数在 laneSection 边界
    减少时(1608: s<17.75 有 -2/-3 两条, 之后只剩 -1, 即 -2 那条被并掉)
    中位线横跳半个车道, 于是 link 254 先扎进环道线里 1.7 m、再以 65° 切出去,
    看着就是右转线斜穿转盘。判据是"入口浅、出口陡": 真正的路口交叉两端都陡,
    只有切对方的弯才会一浅一陡。

    做法和端部合流同一思路，只是把“走到节点”换成“走到出弯点”：
        自己的前段 -> 入口处 ramp 米平滑压到对方线上 -> 沿对方走到末端前
        exit_back 米 -> 三次贝塞尔切向出弯, 落在自己的终点上, 到达方向沿用
        自己原来末端的切线。出弯点切线连续, 不需要圆角; 入口那几十度用
        _smooth_patch 抹平。

    护栏(只动"切角", 绝不动正常的路口交叉):
      - 只认真穿越, 相切/搭接(交集是线段)不算
      - 这条 link 与对方恰好 2 个交点, 跨度 <= max_span, 都不在端部 min_left 内
      - 入口交角 <= shallow(浅)、出口交角 >= steep(陡)
      - 舌头深度(中段到对方线的最大距离) <= max_depth
      - 对方从入口交点还要能再走 6..max_ride 米才有节点, 且它的末端离
        自己的终点 <= max_jump 米(超过就是两个不相干的弯, 别硬拽)
    返回改写的条数。
    """
    bbox = {i: links.at[i, "geometry"].bounds for i in links.index}
    cand = [i for i in links.index[links.link_type == "connector"] if i not in skip]
    n_changed = 0

    for _ in range(max_pass):
        n_pass = 0
        for i in cand:
            g = links.at[i, "geometry"]
            L = g.length
            if L < 2.0 * min_left + 1.0:
                continue
            bi = bbox[i]
            for j in links.index:
                if j == i:
                    continue
                bj = bbox[j]
                if (bj[2] < bi[0] or bj[0] > bi[2]
                        or bj[3] < bi[1] or bj[1] > bi[3]):
                    continue
                xg = links.at[j, "geometry"]
                if g.distance(xg) > 0.01:
                    continue
                pts = []
                for pt in _geom_points(g.intersection(xg)):
                    P = Point(np.asarray(pt.coords[0], dtype=float))
                    s = float(g.project(P))
                    if s < min_left or L - s < min_left:
                        continue
                    if not _really_crosses(g, xg, P):
                        continue
                    pts.append((s, P))
                if len(pts) != 2:
                    continue
                pts.sort(key=lambda t: t[0])
                (sa, Pa), (sb, Pb) = pts
                if sb - sa < 1.0 or sb - sa > max_span:
                    continue
                # 入口浅、出口陡 = 在切对方的弯
                angs = []
                for s, P in ((sa, Pa), (sb, Pb)):
                    va = _tangent(g, s)
                    vb = _tangent(xg, float(xg.project(P)))
                    angs.append(math.degrees(
                        math.acos(min(1.0, abs(float(va @ vb))))))
                if angs[0] > shallow or angs[1] < steep:
                    continue
                # 对方还要往前走多远才到节点
                xa = float(xg.project(Pa))
                ride = xg.length - xa
                if ride < 6.0 or ride > max_ride:
                    continue
                own_end = np.asarray(g.coords[-1], dtype=float)
                if float(np.hypot(*(own_end
                                    - np.asarray(xg.coords[-1], dtype=float))))                         > max_jump:
                    continue
                # 舌头深度: 中段离对方线的最大距离
                mid = substring(g, sa, sb)
                ns = max(2, int((sb - sa) / 0.5) + 1)
                dep = max(xg.distance(Point(mid.interpolate(t)))
                          for t in np.linspace(0.0, mid.length, ns))
                if dep > max_depth:
                    continue
                # ---- 组装: 前段 + 沿对方 + 贝塞尔出弯 ----
                de = min(exit_back, 0.45 * ride)
                p0 = np.asarray(xg.interpolate(xg.length - de).coords[0],
                                dtype=float)
                t0 = _tangent(xg, xg.length - de)
                t2 = _tangent(g, max(L - 1.0, 0.0))
                d = float(np.hypot(*(own_end - p0)))
                if d < 1.0:
                    continue
                c1 = p0 + t0 * 0.45 * d
                c2 = own_end - t2 * 0.45 * d
                ts = np.linspace(0.0, 1.0, max(int(d), 8))[:, None]
                bez = (((1 - ts) ** 3) * p0 + 3 * ((1 - ts) ** 2) * ts * c1
                       + 3 * (1 - ts) * ts ** 2 * c2 + ts ** 3 * own_end)
                head = list(substring(g, 0.0, sa).coords)
                arc = list(substring(xg, xa, xg.length - de).coords)
                arc[0] = head[-1]        # 两边插值出的 P 差 ~1cm, 强制重合
                new = head + arc[1:] + [tuple(p) for p in bez[1:]]
                keep = [new[0]]
                for q in new[1:]:
                    if (abs(q[0] - keep[-1][0]) > 1e-9
                            or abs(q[1] - keep[-1][1]) > 1e-9):
                        keep.append(q)
                if len(keep) < 4:
                    continue
                q = np.asarray(_resample(keep, 0.5), dtype=float)
                # 入口接缝: 自己的切线 -> 对方切线(实测 32°, 抹掉)
                q = _smooth_patch(q, np.asarray(head[-1], dtype=float),
                                  sigma=1.5, span=ramp)
                g2 = LineString(q)
                links.at[i, "geometry"] = g2
                links.at[i, "length"] = round(g2.length, 3)
                bbox[i] = g2.bounds
                if changed is not None:
                    changed.add(i)      # 后续仅按源形状、有向邻居及穿越护栏重检。
                n_changed += 1
                n_pass += 1
                break
        if not n_pass:
            break
    return n_changed
