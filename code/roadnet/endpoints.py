"""道路及连接线两端向节点对齐；掉头按源弧独立保护，统计归属一次构建。"""
from dataclasses import dataclass, field
import math

import numpy as np
from shapely.geometry import LineString, Point

from .geometry import _arc_len, _end_tangent, _fillet_at, _joint_kink, _smoothstep


@dataclass
class EndpointStats:
    """本次构建的端部过渡计数，实例随 build_net 创建，构建之间不共享可变状态。"""

    blends: int = 0
    paths: dict = field(default_factory=dict)

    def note(self, kind, link_type):
        key = link_type, kind
        self.paths[key] = self.paths.get(key, 0) + 1


def align_uturn_to_nodes(points, start, end, allow_expansion=False):
    """保持源回转形状，将首末节点偏移沿弧长分摊；不能满足形状条件时明确失败。

    只用于已由源车道与 junction 识别的掉头。权重两端导数为零，保留源端部方向；
    不采用普通连接路的两点退化兜底，防止丢失掉头几何而仍被误报为可导航。
    普通掉头位移不超过源弧长的 40% 时沿用原对齐；超过时只允许受约束的横向展开。
    allow_expansion 为已核实的路肩掉头显式启用相同保护。展开仍受源弧长的 80% 上限、
    回转方向、自交及内部折角约束，不接受纵向拖拽来迁就错误接点。
    """
    p = np.asarray(points, dtype=float).copy()
    a, b = np.asarray(start, dtype=float), np.asarray(end, dtype=float)
    distance = _arc_len(p)
    length = float(distance[-1])
    delta = max(float(np.linalg.norm(a - p[0])), float(np.linalg.norm(b - p[-1])))
    # 紧凑掉头从内侧车道移到多车道组中心，可能仅横向位移就超过原弧长的 40%。
    # 此时使用已有展开保护；普通小位移的几何保持原算法，不能直接放开全部迁移。
    expansion = allow_expansion or delta > .4 * length
    if length <= 0 or delta > (.8 if expansion else .4) * length:
        raise ValueError("掉头端点迁移超过源弧可接受范围，请检查 junction 源连接")
    if expansion:
        tangents = (-_end_tangent(p[::-1], .2), _end_tangent(p, .2))
        if any(tangent is None or abs(float(offset @ tangent)) > max(.25, .1 * float(np.linalg.norm(offset)))
               for offset, tangent in zip((a - p[0], b - p[-1]), tangents)):
            raise ValueError("掉头节点迁移不是横向接入，请检查源行驶方向")
    weight = _smoothstep(distance / length)[:, None]
    q = p + (1 - weight) * (a - p[0]) + weight * (b - p[-1])
    q[0], q[-1] = a, b
    g = LineString(q)
    span = max(.05, min(.5, .02 * g.length))
    incoming, outgoing = -_end_tangent(q[::-1], span=span), _end_tangent(q, span=span)
    chord = float(np.linalg.norm(b - a))
    if (not g.is_valid or not g.is_simple or not np.isfinite(q).all()
            or incoming is None or outgoing is None
            or float(incoming @ outgoing) > math.cos(math.radians(145))
            or g.length < max(1.1 * chord, .6 * length)):
        raise ValueError("掉头弧形无法在固定节点下保持，请检查源车道和节点位置")
    if expansion and any(_joint_kink(q, i) > 25 for i in range(1, len(q) - 1)):
        raise ValueError("掉头横向接入产生过大折角，请检查源车道和节点位置")
    return q


def _not_reverse(mid, end, nxt):
    """
    end -> mid -> nxt 是否"顺": 两段夹角 >120° 说明对齐后要掉头, 那就直接挪端点。
    mid = 原端点, end = 节点坐标, nxt = 端点后的第一个点。
    """
    v1 = (mid[0] - end[0], mid[1] - end[1])
    v2 = (nxt[0] - mid[0], nxt[1] - mid[1])
    n1 = math.hypot(*v1)
    n2 = math.hypot(*v2)
    if n1 < 1e-9 or n2 < 1e-9:
        return True
    return (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2) > -0.5


def _s_merge(p, node, r_target=12.0, max_frac=0.55, samples=48):
    """端部: 用 S 形(双圆弧)横向平移把端点收到节点上, 取代"垂直小尾巴"。

    为什么需要它: 节点坐标取的是该侧**多车道组中心线**, 而连接路只有 1 条车道、
    自然端点在组中心外侧半个车道宽(常 1.75 m)。原来的处理是垂直于行驶方向补
    一段直线到节点, 再磨圆 -> 末端出现 90° 小钩/回头包, 同一对 link 会被切两次。
    这里改成: 往回退 trim 米, 用两段半径 r 的反向圆弧横向滑到节点, 切向连续、
    不过冲, 端点严格等于节点坐标。

    返回新的点列(末点 = node) 或 None(纵向偏差为主 / 空间不够, 交给直线补尾)。
    """
    p = np.asarray(p, dtype=float)
    if len(p) < 4:
        return None
    L = float(_arc_len(p)[-1])
    if L < 4.0:
        return None
    t = _end_tangent(p, span=2.5)
    if t is None:
        return None
    nl = np.array([-t[1], t[0]])                 # 左法向
    v = np.asarray(node, dtype=float) - p[-1]
    lat = float(v @ nl)
    lon = float(v @ t)
    d = math.hypot(lat, lon)
    if d < 1e-9 or abs(lat) < 0.6:
        return None                              # 纯纵向偏差/小偏移: 直线接长更自然
    if abs(lat) / d < 0.8:
        # 斜着补尾(角度 < 53°)只是轻微折角, 磨圆就够; 强行 S 化会在端点附近
        # 划出一道"透镜形"的双重交叉。只处理几乎垂直的钩。
        return None
    # S 形的纵向跨度 F ≈ 2*sqrt(|lat|*r); 半径随偏移量放大, 免得小偏移也拉一大段
    r_use = max(6.0, min(r_target, 4.0 * abs(lat)))
    trim = 2.0 * math.sqrt(abs(lat) * r_use) - lon
    trim = min(max(trim, 0.0), max_frac * L)
    if trim < 1.5:
        return None
    s = _arc_len(p)
    target = s[-1] - trim
    if target <= 1e-6:
        return None
    j = int(np.searchsorted(s, target))
    j = min(max(j, 1), len(p) - 1)
    seg = s[j] - s[j - 1]
    u = (target - s[j - 1]) / seg if seg > 1e-12 else 0.0
    Q = p[j - 1] + u * (p[j] - p[j - 1])
    head = np.vstack([p[:j], [Q]])
    tq = _end_tangent(head, span=1.5)
    if tq is None:
        return None
    nq = np.array([-tq[1], tq[0]])
    W = np.asarray(node, dtype=float) - Q
    F = float(W @ tq)
    lat2 = float(W @ nq)
    if F <= 0.5 or abs(lat2) < 0.2:
        return None
    phi = 2.0 * math.atan(abs(lat2) / F)
    if phi > math.radians(85.0):
        return None
    r = abs(lat2) / (2.0 * (1.0 - math.cos(phi)))
    if r > 60.0:                                 # 极缓: 不如直接直线补
        return None
    if r < max(3.0, 2.0 * abs(lat)):             # 不够平缓: 补出来的弯比小尾巴还难看
        return None
    # 数值积分双圆弧
    sign = 1.0 if lat2 > 0 else -1.0
    ds = (2.0 * r * phi) / samples
    ang = math.atan2(tq[1], tq[0])
    pos = Q.copy()
    pts = [pos.copy()]
    for k in range(samples):
        frac = (k + 0.5) / samples
        ang += sign * (1.0 / r) * ds * (1.0 if frac < 0.5 else -1.0)
        pos = pos + ds * np.array([math.cos(ang), math.sin(ang)])
        pts.append(pos.copy())
    arc = np.array(pts)
    err = np.asarray(node, dtype=float) - arc[-1]     # 把离散残差摊到整段, 免得末端折一下
    arc = arc + err[None, :] * np.linspace(0.0, 1.0, len(arc))[:, None]
    arc[-1] = np.asarray(node, dtype=float)
    out = np.vstack([head, arc[1:]])
    # 去掉重合点
    ded = [out[0]]
    for q in out[1:]:
        if np.hypot(*(q - ded[-1])) > 1e-9:
            ded.append(q)
    return np.array(ded)


def _tail_blend(p, node):
    """端部**大横向偏移**的兜底: 从节点沿连接路自己的行驶方向出发, 贝塞尔并入线上。

    什么时候轮到它: 宽主干道(福田 5+3 车道)的节点取车道组中线, 转向连接路从
    最外车道出发, 端点与节点几乎纯侧向差(实测 link 392: |lat|=6.19 m, 纵向
    前进 0.11 m)。_s_merge 的双圆弧要 2*sqrt(|lat|*r) ≈ 17 m 的直线进深, 转向
    连接路起步就弯, 回退太深后切向已转走 -> 拒收(r>60); 落到"垂直补线+磨圆"
    就是先朝正东甩 6 m 再用 R≈4 m 急弯拐回来, 图上一个包。
    这里改成: 出发切向直接取连接路端点自己的行驶方向(与同节点直行 link 平行,
    视觉上是"从路口顺着方向出去再摆进转向车道"), 用三次贝塞尔并入源曲线,
    两端切向 G1, 端点严格 = 节点。

    p: 点列, 行走方向朝向自然端点(p[-1] = 离节点最近的端点)。
    返回新点列(末点 = node)或 None(判据不满足/贝塞尔质量差, 交给原逻辑)。
    """
    p = np.asarray(p, dtype=float)
    if len(p) < 8:
        return None
    L = float(_arc_len(p)[-1])
    if L < 8.0:
        return None
    t_arr = _end_tangent(p, span=2.5)            # 行走方向到达自然端点的切向
    if t_arr is None:
        return None
    t0 = -t_arr                                  # 终系(节点->远端)的出发切向
    A_ = np.asarray(node, dtype=float)
    W = A_ - p[-1]
    nl0 = np.array([-t0[1], t0[0]])
    lat = float(W @ nl0)
    lon = float(W @ t0)
    d = math.hypot(lat, lon)
    if abs(lat) < 1.0 or d < 1e-9:
        return None
    if abs(lat) / d < 0.55:
        return None                              # 以纵向为主: 直线补尾更自然
    if lon < -2.0:
        return None                              # 自然端点越过节点太多: 交给原逻辑
    # 并入点 R: 沿源曲线搜索"切向 ≈ 节点->R 方向"的位置 —— 这时候贝塞尔是
    # 单弯(出发切向、到达切向都对着弦), 没有中段回摆。并入太浅(刚离开端点)
    # 时源切向还没转过去, 贝塞尔必须先甩向侧面再扭回来, 又是一个 S。
    # 窗口必须收紧: 只做**端部**过渡, 吃掉太多源曲线会把长弧拉成直斜线
    # (福田 junction 400 的 70 m 转向弧就是这么被抹平的): 离端点
    # max(5, |lat|) ~ min(35%L, 2.2|lat|+8)。
    s = _arc_len(p)
    lo = max(int(np.searchsorted(s, s[-1] - min(0.35 * L, 2.2 * abs(lat) + 8.0))), 1)
    hi = min(int(np.searchsorted(s, s[-1] - max(5.0, abs(lat)))), len(p) - 2)
    best_j = None
    best_err = 1e9
    for j in range(lo, hi + 1):
        Rj = p[j]
        vR = p[j - 1] - Rj                       # 终系(节点->远端)下 R 点切向
        nR = float(np.hypot(*vR))
        nAR = float(np.linalg.norm(Rj - A_))
        if nR < 1e-9 or nAR < 2.0:
            continue
        tR = vR / nR
        err = math.degrees(math.acos(np.clip(
            float(tR @ ((Rj - A_) / nAR)), -1.0, 1.0)))
        if err < best_err:
            best_j, best_err = j, err
    if best_j is None:
        return None
    j = best_j
    R_ = p[j]
    vR = p[j - 1] - p[j]
    tR = vR / float(np.hypot(*vR))
    C = float(np.linalg.norm(R_ - A_))
    if C < 2.0:
        return None
    c = min(0.45 * C, max(2.0, 0.8 * abs(lat)))  # 控制臂长
    # 三次贝塞尔: A_(切向 t0) -> R_(切向 tR)
    P0, P1, P2, P3 = A_, A_ + t0 * c, R_ - tR * c, R_
    n = max(16, int(C / 0.5))
    ts = np.linspace(0.0, 1.0, n)[:, None]
    bez = ((1 - ts) ** 3 * P0 + 3 * (1 - ts) ** 2 * ts * P1
           + 3 * (1 - ts) * ts ** 2 * P2 + ts ** 3 * P3)
    # 质量闸: 不许中途往回走(沿出发切向投影), 转弯半径别太急
    fwd = np.diff(bez, axis=0)
    step = np.linalg.norm(fwd, axis=1)
    if (step < 1e-9).any():
        return None
    u = fwd / step[:, None]
    if float((u @ t0).min()) < -0.15:
        return None
    ang = np.arccos(np.clip((u[:-1] * u[1:]).sum(axis=1), -1.0, 1.0))
    rad = 0.5 * (step[:-1] + step[1:]) / np.maximum(ang, 1e-9)
    if float(rad.min()) < max(3.0, 0.6 * abs(lat)):
        return None
    # 不许把源曲线"抹平": 贝塞尔离**被替换的那一段**的距离不能远超本来的横向
    # 偏移, 否则就是拿一条弦去截长弧(junction 400 的 70 m 转向弧踩过)
    src = LineString(p[j:])
    dev = max(src.distance(Point(q)) for q in bez[:: max(1, len(bez) // 40)])
    if dev > max(1.3 * abs(lat), 3.0):
        return None
    out = np.vstack([p[:j + 1], bez[::-1][1:]])  # 终系下 R -> ... -> node
    ded = [out[0]]
    for q in out[1:]:
        if np.hypot(*(q - ded[-1])) > 1e-9:
            ded.append(q)
    return np.array(ded) if len(ded) >= 2 else None


def _tail_soft(p, node, max_d=1.2):
    """端部小偏移的柔和收口：末段切向贝塞尔，最大默认 1.2 m。

    什么时候轮到它: 节点是**多端口合并后的代表坐标**, 同一条 road 的两端/对向 link
    的端口常差 0.6~1.2 m。_s_merge 对这么小的偏移多半拒收(trim<1.5 或 phi 太大),
    _tail_blend 又要求 |lat|>=1.0; 落到兜底就是"补 0.9 m 垂直直线 + fillet", 而
    fillet 半径受 min(4, 0.9*d) 限制(≈0.8 m), 末端残留 10~40° 折角 —— 图上就是
    "长直主路末端外翻一下"。福田实测 57 个 road 端走这条路。

    这里: 从端点回退 back 米取起点 F(更早的源线原样保留), 三次贝塞尔 F -> node;
    出发切向 = 源线在 F 的切向, 到达切向 = 弦方向(与路口里后续 link 顺接), 单弯
    不回摆。闸: 源线在 F 已朝节点走、不回头、R>=1.5 m、偏离被替换段 <= max(2d,1.2)。

    p: 点列, 行驶方向朝向自然端点(p[-1] 离节点最近)。
    至少三个点且总长 >=3 m；厘米级偏移引起大折角时也允许处理。
    返回新点列(末点 = node)或 None(不及格, 交给原兜底)。
    """
    p = np.asarray(p, dtype=float)
    if len(p) < 3:
        return None
    L = float(_arc_len(p)[-1])
    if L < 3.0:
        return None
    A_ = np.asarray(node, dtype=float)
    W = A_ - p[-1]
    d = float(np.hypot(*W))
    if d < 1e-6 or d > max_d:
        return None
    back = min(max(3.0 * d, 2.0), 0.4 * L, 8.0)
    if back < 1.2:
        return None
    s = _arc_len(p)
    target = s[-1] - back
    if target <= 1e-6:
        return None
    j = int(np.searchsorted(s, target))
    j = min(max(j, 1), len(p) - 1)
    seg = s[j] - s[j - 1]
    u = (target - s[j - 1]) / seg if seg > 1e-12 else 0.0
    F = p[j - 1] + u * (p[j] - p[j - 1])
    head = np.vstack([p[:j], [F]])
    tF = _end_tangent(head, span=min(1.5, 0.4 * back))
    if tF is None:
        return None
    chord = A_ - F
    C = float(np.hypot(*chord))
    if C < 1.5:
        return None
    if float(tF @ (chord / C)) < 0.2:            # 源线在 F 处不是朝节点走: 回头
        return None
    tA = chord / C                               # 到达切向: 弦方向, 与后续 link 顺接
    c = min(0.45 * C, max(0.8, 1.1 * d))
    P0, P1, P2, P3 = F, F + tF * c, A_ - tA * c, A_
    n = max(14, int(C / 0.4))
    ts = np.linspace(0.0, 1.0, n)[:, None]
    bez = ((1 - ts) ** 3 * P0 + 3 * (1 - ts) ** 2 * ts * P1
           + 3 * (1 - ts) * ts ** 2 * P2 + ts ** 3 * P3)
    fwd = np.diff(bez, axis=0)
    step = np.linalg.norm(fwd, axis=1)
    if (step < 1e-9).any():
        return None
    uu = fwd / step[:, None]
    if float((uu @ tF).min()) < 0.0:             # 不许中途往回走
        return None
    ang = np.arccos(np.clip((uu[:-1] * uu[1:]).sum(axis=1), -1.0, 1.0))
    rad = 0.5 * (step[:-1] + step[1:]) / np.maximum(ang, 1e-9)
    if float(rad.min()) < 1.5:
        return None
    src = LineString(p[j:])
    dev = max(src.distance(Point(q)) for q in bez[:: max(1, len(bez) // 24)])
    if dev > max(2.0 * d, 1.2):                  # 别拿弦去截源线
        return None
    out = np.vstack([head, bez[1:]])
    ded = [out[0]]
    for q in out[1:]:
        if np.hypot(*(q - ded[-1])) > 1e-9:
            ded.append(q)
    return np.array(ded) if len(ded) >= 2 else None


def _emit(rows, base, rid, pts, direction, nlanes, node_id, node_xy,
          min_len, oneway, step, passable, fillet=4.0, lane_ids="",
          merge_r=12.0, soft=True, endpoint_ports=None, stats=None, preserve_curve=False,
          allow_uturn_expansion=False):
    """按行驶方向生成 Link，首末坐标分别接到 from_node/to_node。

    direction 仅用于内部将源点列排序：+1 沿参考线方向，−1 反向，不写出方向标签。
    lane_ids: 这条 link 用了哪些车道(如 "-1,-2"), 辅道渐变 link 靠它区分。
    endpoint_ports 为源 s 首末端的车道组端口；未提供时兼容主组的三元端口。
    stats 为本次构建统计；preserve_curve 只对已确认的源掉头启用固定节点的弧形保护。
    allow_uturn_expansion 限定为源路肩掉头接入通行车道群所需的横向弧形展开。
    """
    stats = EndpointStats() if stats is None else stats
    seq = [tuple(p) for p in (pts if direction > 0 else pts[::-1])]
    # 源里程递减方向需要反转点列，同时交换首末节点的端号，保持数字化方向一致。
    endpoint_ports = endpoint_ports or ((rid, 0, direction), (rid, 1, direction))
    a = node_id[endpoint_ports[0 if direction > 0 else 1]]
    b = node_id[endpoint_ports[1 if direction > 0 else 0]]
    A, B = node_xy[a], node_xy[b]
    d0 = math.hypot(seq[0][0] - A[0], seq[0][1] - A[1])
    d1 = math.hypot(seq[-1][0] - B[0], seq[-1][1] - B[1])

    ded = [seq[0]]
    for p in seq[1:]:
        if abs(p[0] - ded[-1][0]) > 1e-9 or abs(p[1] - ded[-1][1]) > 1e-9:
            ded.append(p)
    if len(ded) < 2:
        return
    L0 = float(LineString(ded).length)

    # 首尾必须落在节点坐标上 -> 整网严格首尾相接。偏移的处理分三种:
    #   小偏移(<tol)  直接替换端点, 视觉无缝
    #   大偏移        保留原几何, 在端部补一段直线到节点(长度真实, 路况才准)
    #   补线会折返     (节点在原端点的"后方") 退回替换, 免得画出回头路
    tol = max(step, 1e-6)
    snap0, snap1 = d0, d1                     # 原始吸附量, 报告用
    if preserve_curve:
        # 源掉头已经确认；只分摊节点位移，不能用两点直线或普通平滑压掉回转弧。
        geom = align_uturn_to_nodes(ded, A, B, allow_expansion=allow_uturn_expansion).tolist()
        tail_a = tail_b = False
    elif max(d0, d1) > L0:
        # 吸附量比整条线还长: 几何已经被节点"吃掉", 两头各补一段直线会在
        # 中段撞出折角。这种几米长的极短 link 直接连直线, 连通性一样。
        geom = [A, B]
        tail_a = tail_b = False
    else:
        # 横向偏移优先用 S 形横向平移收进去(不打垂直小钩, 见 _s_merge 说明)
        # 两端都先试双圆弧; 仍没收进去的那一端, 若偏移以侧向为主, 再用
        # _tail_blend 沿行驶方向贝塞尔并入 —— 但**每条线只做一端**且挑偏移
        # 大的那端: 两端各吃掉 35% 弧长会把短连接线的中段源曲线也抹直
        # (福田 junction 400 的 37~41 m 转向线踩过)。
        if merge_r > 0:
            _ltype = base.get("link_type", "?")
            if d1 > tol:
                m = _s_merge(ded, B, r_target=merge_r)
                if m is not None:
                    ded, d1 = m, 0.0
                    stats.note("smerge", _ltype)
            if d0 > tol:
                m = _s_merge(ded[::-1], A, r_target=merge_r)
                if m is not None:
                    ded, d0 = m[::-1], 0.0
                    stats.note("smerge", _ltype)
            if d0 > tol or d1 > tol:
                if d1 >= d0:
                    m = _tail_blend(ded, B)
                    if m is not None:
                        ded, d1 = m, 0.0
                        stats.blends += 1
                        stats.note("blend", _ltype)
                    elif soft:
                        # 小偏移(0.15~1.2 m)交给浅贝塞尔: 兜底的"补 0.9 m 直线 +
                        # fillet"磨不平, 末端会留 10~40° 外翻折角
                        m = _tail_soft(ded, B)
                        if m is not None:
                            ded, d1 = m, 0.0
                            stats.note("soft", _ltype)
                else:
                    m = _tail_blend(ded[::-1], A)
                    if m is not None:
                        ded, d0 = m[::-1], 0.0
                        stats.blends += 1
                        stats.note("blend", _ltype)
                    elif soft:
                        m = _tail_soft(ded[::-1], A)
                        if m is not None:
                            ded, d0 = m[::-1], 0.0
                            stats.note("soft", _ltype)
            # 兜底之外还有一类漏网的: 偏移 <= tol(默认 1 m) 的端被当成"小偏移"
            # 直接替换末点(geom[-1] = B), 没进任何平滑链。1 m 相对 24 m 宽的主路
            # 不算小 —— 末点被硬拽 1 m, 末段留下 10~40° 折角(福田 112 条 10~20°,
            # 5 条 >20°), 而 fillet 只在"补直线"分支触发, 这里根本不磨。
            # 给这类端也接浅贝塞尔, 下限 0.15 m 免得动到真正的数值噪声。
            # 偏移虽小，若最后一个采样段更短，覆盖端点仍会形成大折角。
            # 按实际末段折角触发收口，不能只看 0.15 m 的绝对阈值。
            def sharp_snap(points, node):
                if len(points) < 3:
                    return False
                u = np.asarray(points[-2]) - np.asarray(points[-3])
                v = np.asarray(node) - np.asarray(points[-2])
                norm = np.linalg.norm(u) * np.linalg.norm(v)
                return norm > 1e-12 and float(u @ v) / norm < math.cos(math.radians(25))

            if soft and merge_r > 0 and 1e-6 < d1 <= tol and (d1 > tol * 0.15 or sharp_snap(ded, B)):
                m = _tail_soft(ded, B, max_d=tol)
                if m is not None:
                    ded, d1 = m, 0.0
                    stats.note("soft", _ltype)
            if soft and merge_r > 0 and 1e-6 < d0 <= tol and (d0 > tol * 0.15 or sharp_snap(ded[::-1], A)):
                m = _tail_soft(ded[::-1], A, max_d=tol)
                if m is not None:
                    ded, d0 = m[::-1], 0.0
                    stats.note("soft", _ltype)
        geom = [A]
        tail_a = d0 > tol and _not_reverse(ded[0], A, ded[1])
        if tail_a:
            geom.extend(ded)
        else:
            geom.extend(ded[1:])
        tail_b = d1 > tol and _not_reverse(ded[-1], B, ded[-2])
        if tail_b:
            geom.append(B)
        else:
            geom[-1] = B
        if merge_r > 0:
            if d0 > tol:
                stats.note("tail" if tail_a else "repl", _ltype)
            if d1 > tol:
                stats.note("tail" if tail_b else "repl", _ltype)

    ded = [geom[0]]
    for p in geom[1:]:
        if abs(p[0] - ded[-1][0]) > 1e-9 or abs(p[1] - ded[-1][1]) > 1e-9:
            ded.append(p)
    if len(ded) < 2:
        return
    # 补线段和曲线之间是直角, 磨圆它 —— 原曲线其余部分不动
    if fillet > 0:
        L = float(_arc_len(ded)[-1])
        if tail_b:
            ded = _fillet_at(ded, len(ded) - 2,
                             min(fillet, 0.45 * (L - d1)),
                             min(fillet, 0.9 * d1))
        if tail_a:
            ded = _fillet_at(ded, 1,
                             min(fillet, 0.9 * d0),
                             min(fillet, 0.45 * (L - d0)))
    g = LineString(ded)
    if g.length < min_len:
        return
    rows.append(dict(base,
                     from_node=a, to_node=b, lanes=nlanes, oneway=oneway,
                     passable=passable,
                     lane_ids=lane_ids,
                     snap_m=round(max(snap0, snap1), 3),
                     length=round(g.length, 3), geometry=g))
