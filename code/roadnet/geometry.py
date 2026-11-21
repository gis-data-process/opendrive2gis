"""路网点列、弧长、切向、平滑和穿越校验；本模块不读取源文件或写出图层。"""
import math

import numpy as np
from shapely.geometry import LineString


def _arc_len(p):
    """累计弧长"""
    p = np.asarray(p, dtype=float)
    if len(p) < 2:
        return np.zeros(len(p))
    d = np.hypot(np.diff(p[:, 0]), np.diff(p[:, 1]))
    return np.concatenate([[0.0], np.cumsum(d)])


def _smoothstep(x):
    """0..1 三阶平滑(两端导数为 0, 接上去不会出现折角)"""
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _resample(pts, ds):
    """按弧长等距重采样, 首尾点不动"""
    p = np.asarray(pts, dtype=float)
    if len(p) < 2:
        return p
    s = _arc_len(p)
    L = s[-1]
    if L <= 1e-9:
        return p
    n = max(int(round(L / ds)), 1)
    t = np.linspace(0.0, L, n + 1)
    q = np.column_stack([np.interp(t, s, p[:, 0]), np.interp(t, s, p[:, 1])])
    q[0], q[-1] = p[0], p[-1]
    return q


def _walk(p, i, dist):
    """从顶点 i 沿折线走 dist(负 = 往回走)。

    返回 (落点, 前缀末下标, 后缀首下标): 落点落在 p[a] 与 p[b] 之间,
    前缀取 p[:a], 后缀取 p[b:], 两者都不含被替换的那一段。
    """
    if dist >= 0:
        d, idx = float(dist), i
        while True:
            nxt = idx + 1
            if nxt >= len(p):
                return p[idx], idx, idx
            seg = float(np.hypot(*(p[nxt] - p[idx])))
            if seg >= d:
                u = d / seg if seg > 0 else 0.0
                return p[idx] + u * (p[nxt] - p[idx]), idx, nxt
            d -= seg
            idx = nxt
    d, idx = float(-dist), i
    while True:
        prv = idx - 1
        if prv < 0:
            return p[idx], idx, idx
        seg = float(np.hypot(*(p[prv] - p[idx])))
        if seg >= d:
            u = d / seg if seg > 0 else 0.0
            return p[idx] + u * (p[prv] - p[idx]), prv, idx
        d -= seg
        idx = prv


def _fillet_at(p, i, t_back, t_fwd, samples=8):
    """顶点 i 两侧各退弧长 t_back / t_fwd, 中间用二次贝塞尔过渡(两端相切, 不过冲)。

    只用在"补到节点的直线"与"原曲线"相交那个直角上, 原曲线其余部分不动。
    退让长度要小于相邻那一段的实有长度, 否则会把节点端点一起吃掉。
    """
    p = np.asarray(p, dtype=float)
    if i <= 0 or i >= len(p) - 1 or min(t_back, t_fwd) <= 1e-6:
        return p
    p0, a, _ = _walk(p, i, -t_back)      # 落在 p[a] 与 p[a+1] 之间
    p2, _, b = _walk(p, i, +t_fwd)       # 落在 p[b-1] 与 p[b] 之间
    if a >= b or np.hypot(*(p2 - p0)) < 1e-6:
        return p
    ctrl = p[i]
    mid = [(1 - u) ** 2 * p0 + 2 * u * (1 - u) * ctrl + u ** 2 * p2
           for u in np.linspace(0.0, 1.0, samples + 2)[1:-1]]
    out = np.vstack([p[:a + 1], [p0], mid, [p2], p[b:]])
    ded = [out[0]]
    for q in out[1:]:
        if np.hypot(*(q - ded[-1])) > 1e-9:
            ded.append(q)
    return np.array(ded)


def _end_tangent(p, span=2.0):
    """端点处的单位切向(沿行驶方向), 用端点往回 span 米的方向估计"""
    p = np.asarray(p, dtype=float)
    if len(p) < 2:
        return None
    s = _arc_len(p)
    L = float(s[-1])
    if L < 1e-9:
        return None
    target = max(L - span, 0.0)
    j = int(np.searchsorted(s, target))
    j = min(max(j, 0), len(p) - 2)      # p[j] 在端点上游约 span 米处
    v = p[-1] - p[j]
    n = math.hypot(*v)
    if n < 1e-9:
        v = p[-1] - p[0]
        n = math.hypot(*v)
        if n < 1e-9:
            return None
    return v / n


def _gauss_smooth(p, sigma, ds):
    """一维高斯沿弧长平滑, 两端做反射延拓 + 残差回填, 保证首尾点不动"""
    p = np.asarray(p, dtype=float)
    if sigma <= 0 or len(p) < 3:
        return p
    r = max(int(np.ceil(3.0 * sigma / ds)), 1)
    if r >= len(p):
        r = len(p) - 1
    k = np.exp(-0.5 * (np.arange(-r, r + 1) * ds / sigma) ** 2)
    k /= k.sum()
    head = 2.0 * p[0] - p[r:0:-1]            # 反射延拓, 避免端点被拉平
    tail = 2.0 * p[-1] - p[-2:-r - 2:-1]
    pad = np.vstack([head, p, tail])
    q = np.column_stack([np.convolve(pad[:, 0], k, "same"),
                         np.convolve(pad[:, 1], k, "same")])[r:r + len(p)]
    # 延拓会带来一点端点漂移, 用 smoothstep 衰减把残差还回去
    s = _arc_len(q)
    L = s[-1]
    q += _smoothstep(1.0 - s / max(3.0 * sigma, 1e-6))[:, None] * (p[0] - q[0])
    if L > 0:
        q += _smoothstep(1.0 - (L - s) / max(3.0 * sigma, 1e-6))[:, None] * (p[-1] - q[-1])
    q[0], q[-1] = p[0], p[-1]
    return q


def smooth_line(g, sigma, ds=0.5):
    """LineString -> 等距重采样 + 高斯平滑, 首尾不变"""
    p = np.asarray(g.coords, dtype=float)
    if sigma <= 0:
        return g
    return LineString(_gauss_smooth(_resample(p, ds), sigma, ds))


def _kink_deg(g):
    """折线内部最大折角(度)。忽略首尾各 1 个内部顶点(补线接口, 不是真折角)。"""
    c = np.asarray(g.coords, dtype=float)[:, :2]
    if len(c) < 5:
        return 0.0
    v1 = c[1:-1] - c[:-2]
    v2 = c[2:] - c[1:-1]
    cos = np.clip((v1 * v2).sum(1)
                  / np.maximum(np.linalg.norm(v1, axis=1)
                               * np.linalg.norm(v2, axis=1), 1e-12), -1, 1)
    inner = np.degrees(np.arccos(cos))[1:-1]
    return float(inner.max()) if len(inner) else 0.0


def _norm_samples(g, n=11):
    """按弧长归一化取 n 个点, 用来比较两条线"是不是同一束"的左右次序"""
    return np.array([g.interpolate(i / (n - 1.0), normalized=True).coords[0]
                     for i in range(n)])


def _mid_index(geoms, n=11):
    """一组同起止节点的平行线里, 取"最中间"那条的下标"""
    if len(geoms) <= 2:
        return len(geoms) // 2
    smp = np.array([_norm_samples(g, n) for g in geoms])      # (m, n, 2)
    flat = smp.reshape(-1, 2)
    c = flat - flat.mean(axis=0)
    w, v = np.linalg.eigh(c.T @ c)                            # 主轴 = 走向
    e1 = v[:, int(np.argmax(w))]
    e2 = np.array([-e1[1], e1[0]])                            # 法向
    off = (smp - smp.mean(axis=(0, 1))).reshape(-1, 2) @ e2
    lat = off.reshape(len(geoms), n).mean(axis=1)             # 每条线的平均侧向偏移
    order = np.argsort(lat)
    return int(order[len(order) // 2])


def _kink_near(q, pt, span=10.0):
    """pt 弧长 ±span 米内的最大折角(度)"""
    q = np.asarray(q, dtype=float)
    s = _arc_len(q)
    j = int(np.argmin(np.hypot(q[:, 0] - pt[0], q[:, 1] - pt[1])))
    lo = max(int(np.searchsorted(s, s[j] - span)), 1)
    hi = min(int(np.searchsorted(s, s[j] + span)), len(q) - 2)
    worst = 0.0
    for k in range(lo, hi + 1):
        v1, v2 = q[k] - q[k - 1], q[k + 1] - q[k]
        n1, n2 = float(np.hypot(*v1)), float(np.hypot(*v2))
        if n1 < 1e-12 or n2 < 1e-12:
            continue
        worst = max(worst, math.degrees(math.acos(
            np.clip(float(v1 @ v2) / (n1 * n2), -1.0, 1.0))))
    return worst


def _smooth_patch(q, pt, sigma=1.0, span=8.0, thresh=25.0, max_pass=4):
    """绕 pt ±span 米局部高斯平滑, 压大折角; 每遍后复测, 压平即停。

    大角度 T 穿(交叉角 80° 上下)光靠 _fillet_at 的贝塞尔磨不平:
    控制点取折角顶点, 在出线弯曲时切线不闭合, 磨完仍剩 ~50° 折角。
    这里窗口首末点固定, 拼回原折线不生新缝; 平滑只往折角楔形内侧收,
    不会再压到对方线上。"""
    q = np.asarray(q, dtype=float)
    for _ in range(max_pass):
        if _kink_near(q, pt) <= thresh:
            break
        s = _arc_len(q)
        j = int(np.argmin(np.hypot(q[:, 0] - pt[0], q[:, 1] - pt[1])))
        lo = max(int(np.searchsorted(s, s[j] - span)), 0)
        hi = min(int(np.searchsorted(s, s[j] + span)), len(q) - 1)
        if hi - lo < 4:
            break
        seg = _gauss_smooth(_resample(q[lo:hi + 1], 0.5), sigma, 0.5)
        q = np.vstack([q[:lo + 1], seg[1:-1], q[hi:]])
    return q


def _geom_points(g):
    """交集中抽出所有点; 交集是线段(搭接/重合)不算交叉, 返回空。"""
    if g.is_empty:
        return []
    if g.geom_type == "Point":
        return [g]
    if g.geom_type == "MultiPoint":
        return list(g.geoms)
    if hasattr(g, "geoms"):
        out = []
        for h in g.geoms:
            out.extend(_geom_points(h))
        return out
    return []


def _side(p, a, b):
    """点 p 在有向线 a->b 的哪一侧: >0 左, <0 右"""
    return ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]))


def _really_crosses(g, xg, P, span=0.8):
    """P 是 g 与 xg 的交点: 看 g 上 P 两侧 span 米的点是否分居 xg 两侧。

    相切/擦边(同侧)不算 —— aux 汇入线本来就与直行线相切收敛, 不能当交叉动。
    """
    s = g.project(P)
    sx = xg.project(P)
    a1 = np.asarray(g.interpolate(max(s - span, 0.0)).coords[0], dtype=float)
    a2 = np.asarray(g.interpolate(min(s + span, g.length)).coords[0], dtype=float)
    b1 = np.asarray(xg.interpolate(max(sx - span, 0.0)).coords[0], dtype=float)
    b2 = np.asarray(xg.interpolate(min(sx + span, xg.length)).coords[0], dtype=float)
    if float(np.hypot(*(b2 - b1))) < 1e-9:
        return False
    return _side(a1, b1, b2) * _side(a2, b1, b2) < 0


def _joint_kink(p, i):
    """顶点 i 处的折角(度)"""
    v1 = p[i] - p[i - 1]
    v2 = p[i + 1] - p[i]
    n1 = float(np.hypot(*v1))
    n2 = float(np.hypot(*v2))
    if n1 < 1e-12 or n2 < 1e-12:
        return 0.0
    return float(np.degrees(np.arccos(np.clip(float(v1 @ v2) / (n1 * n2),
                                              -1.0, 1.0))))


def _tangent(g, s, d=0.5):
    """g 在弧长 s 处的单位切线方向"""
    p0 = np.asarray(g.interpolate(max(s - d, 0.0)).coords[0], dtype=float)
    p1 = np.asarray(g.interpolate(min(s + d, g.length)).coords[0], dtype=float)
    v = p1 - p0
    n = float(np.hypot(*v))
    return v / n if n > 1e-12 else np.array([1.0, 0.0])


def _chord_sag(p):
    """点列相对"首尾连线"的最大偏离(米), 同时返回弦长。已共线时 sag≈0。"""
    p = np.asarray(p, dtype=float)[:, :2]
    if len(p) < 3:
        return 0.0, float(np.hypot(*(p[-1] - p[0])))
    a, b = p[0], p[-1]
    ab = b - a
    n = float(np.hypot(*ab))
    if n < 1e-9:
        return 0.0, 0.0
    # 点到直线距离 = |叉积| / |ab|
    cross = (p[1:-1, 0] - a[0]) * ab[1] - (p[1:-1, 1] - a[1]) * ab[0]
    return float(np.abs(cross).max() / n), n


def _out_dir(links, i, node):
    """link i 在 node 端、指向 link 内部(离开 node)的单位切向"""
    c = np.asarray(links.at[i, "geometry"].coords, dtype=float)
    if links.at[i, "from_node"] != node:
        c = c[::-1]                       # 让起点就是 node
    tangent = _end_tangent(c[::-1], 2.0)
    return -tangent if tangent is not None else None


def _directed_road_tangents(links, i, targets, incidence, max_ang=10.0):
    """找入口 road→connector、出口 connector→road 的切向，与目标方向偏差不超过门槛。

    targets 为沿行驶方向的首末切向；仅在相应节点取局部方向，排除反向相邻道路。
    """
    tangents = []
    for node, column, sign, target in (
            (links.at[i, "from_node"], "to_node", -1.0, targets[0]),
            (links.at[i, "to_node"], "from_node", 1.0, targets[1])):
        if target is None:
            return None
        choices = []
        for j in incidence[node]:
            if links.at[j, "link_type"] != "road" or links.at[j, column] != node:
                continue
            tangent = _out_dir(links, j, node)
            if tangent is None:
                continue
            tangent = sign * tangent
            angle = math.degrees(math.acos(np.clip(float(tangent @ target), -1.0, 1.0)))
            choices.append((angle, tangent))
        if not choices:
            return None
        angle, tangent = min(choices, key=lambda item: item[0])
        if angle > max_ang:
            return None
        tangents.append(tangent)
    return tangents


def _crossing_count(g, other, tolerance=1e-7):
    """数两线的内部真实穿越；兼容混合交集，排除共享端点、重合及浮点相切。

    比较次数而非只比较对象 ID，避免一条对方 Link 从一次穿越增加成两次却被漏过。
    tolerance 为局部侧别距离容差（米），仅消除数值噪声，不放过实际浅穿越。
    """
    count = 0
    for point in _geom_points(g.intersection(other)):
        s, t = g.project(point), other.project(point)
        if min(s, g.length - s, t, other.length - t) <= tolerance:
            continue
        a1 = np.asarray(g.interpolate(max(s - .8, 0.0)).coords[0])
        a2 = np.asarray(g.interpolate(min(s + .8, g.length)).coords[0])
        b1 = np.asarray(other.interpolate(max(t - .8, 0.0)).coords[0])
        b2 = np.asarray(other.interpolate(min(t + .8, other.length)).coords[0])
        distance = float(np.linalg.norm(b2 - b1))
        if distance <= tolerance:
            continue
        side1, side2 = _side(a1, b1, b2) / distance, _side(a2, b1, b2) / distance
        if min(side1, side2) < -tolerance and max(side1, side2) > tolerance:
            count += 1
    return count


def _replacement_safe(links, i, candidate, source_lines=None, crossing_limits=None):
    """接点几何替换护栏：端点固定、无自交/大折角，不新增无来源的穿越。

    默认以当前两线的内部穿越次数为上限。已确认源共线的拉直调用可传入源线，
    同时核对两条有效、无自交的源轨迹，允许恢复被贴线隐藏的源交叉；每对线仍
    不得超过当前或源中已有的最大次数。缺少任一源线时维持原保护。
    crossing_limits 为联合候选按原始几何预先确定的逐对上限，优先于临时几何次数，
    防止先暂放直线产生的交叉被邻接转弯当成可以保留的既有交叉。
    """
    original = links.at[i, "geometry"]
    p = np.asarray(candidate.coords)
    if (not candidate.is_valid or not candidate.is_simple or not np.isfinite(p).all()
            or not np.array_equal(p[0], original.coords[0])
            or not np.array_equal(p[-1], original.coords[-1])):
        return False
    for k in range(1, len(p) - 1):
        if _joint_kink(p, k) > 25.0:
            return False
    for j in links.index:
        if j == i:
            continue
        other = links.at[j, "geometry"]
        if candidate.disjoint(other):
            continue
        crossings = _crossing_count(candidate, other)
        existing = _crossing_count(original, other)
        pair = (min(i, j), max(i, j))
        if crossing_limits is not None and pair in crossing_limits:
            existing = crossing_limits[pair]
        elif crossings > existing and source_lines is not None:
            source, other_source = source_lines.get(i), source_lines.get(j)
            if (source is not None and other_source is not None
                    and source.is_valid and source.is_simple
                    and other_source.is_valid and other_source.is_simple):
                existing = max(existing, _crossing_count(source, other_source))
        if crossings > existing:
            return False
    return True


def _curve_turns(geometry):
    """等距采样的有符号转角（度）；正为左转，用累计反向转角识别鼓包与真实 S 弯。"""
    p = _resample(np.asarray(geometry.coords), .5)
    v = np.diff(p, axis=0)
    v = v[np.linalg.norm(v, axis=1) > 1e-9]
    if len(v) < 2:
        return np.array([], dtype=float)
    return np.degrees(np.diff(np.unwrap(np.arctan2(v[:, 1], v[:, 0]))))
