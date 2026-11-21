"""源几何约束的辅道接点、直行、转弯及联合候选整理。"""
from collections import defaultdict
from dataclasses import dataclass, field
import math

import numpy as np
from shapely.geometry import LineString
from shapely.ops import substring

from .coincidence import _coincide, _mid_coincide
from .junction import _merge_turns
from .geometry import (_arc_len, _chord_sag, _crossing_count, _curve_turns,
                       _directed_road_tangents, _end_tangent, _kink_deg,
                       _replacement_safe, _resample, _smoothstep, _tangent,
                       smooth_line)


def _refine_auxiliary_joins(links, source_lines):
    """全宽端独立的 aux 在共用收窄节点处沿主组切向收口，消除端部新增穿越。

    只修复源未穿过主组、输出收口却穿过主组的情况；以源末段作切向贝塞尔，
    首末节点保持固定，候选必须通过共同几何保护，并减少与主组的穿越次数。
    """
    for i, source in source_lines.items():
        if i not in links.index or links.at[i, "link_type"] != "aux":
            continue
        main_ids = links.index[(links.road_id == links.at[i, "road_id"]) & (links.link_type == "road")]
        for j in main_ids:
            main = links.at[j, "geometry"]
            if (_crossing_count(source, main) or not _crossing_count(links.at[i, "geometry"], main)):
                continue
            for at_start, column in ((False, "to_node"), (True, "from_node")):
                if links.at[i, column] != links.at[j, column]:
                    continue
                original = links.at[i, "geometry"]
                base = LineString(list(source.coords)[::-1]) if at_start else source
                target = LineString(list(main.coords)[::-1]) if at_start else main
                end = np.asarray(original.coords[0 if at_start else -1])
                target_tangent = _end_tangent(np.asarray(target.coords))
                if target_tangent is None:
                    continue
                distance = float(np.linalg.norm(end - np.asarray(base.coords[-1])))
                candidates = []
                for back in (max(6.0, distance * 2.0), max(12.0, distance * 4.0)):
                    back = min(back, .45 * base.length)
                    head = substring(base, 0.0, base.length - back)
                    point = np.asarray(head.coords[-1])
                    tangent = _tangent(base, base.length - back)
                    chord = float(np.linalg.norm(end - point))
                    u = np.linspace(0.0, 1.0, max(20, int(chord / .5) + 1))[:, None]
                    for fraction in (.2, .35, .5):
                        arm = chord * fraction
                        curve = ((1 - u) ** 3 * point + 3 * (1 - u) ** 2 * u * (point + arm * tangent)
                                 + 3 * (1 - u) * u ** 2 * (end - arm * target_tangent) + u ** 3 * end)
                        coords = list(head.coords) + [tuple(p) for p in curve[1:]]
                        if at_start:
                            coords.reverse()
                        coords[0], coords[-1] = original.coords[0], original.coords[-1]
                        candidate = LineString(coords)
                        if (_crossing_count(candidate, main) < _crossing_count(original, main)
                                and _replacement_safe(links, i, candidate)):
                            candidates.append(candidate)
                if candidates:
                    candidate = min(candidates, key=lambda g: g.hausdorff_distance(original))
                    links.at[i, "geometry"] = candidate
                    links.at[i, "length"] = round(candidate.length, 3)


def _straighten(links, max_ang=10.0, sag_ratio=0.04, max_sag=2.5,
                min_len=3.0, skip=(), source_lines=None, changed=None, pending=None):
    """把源车道群共线、前后有向 road 顺直的连接路换成节点间直线。

    使用吸附前的车道群中线（包含 laneOffset 和宽度），不能以参考线共线代替。
    有源几何时不再受输出鼓包的 4% 阈值限制，也可重检已贴线对象；共同护栏允许
    恢复本线与邻线在源中已有的交叉，避免先贴弯、再因交叉被禁止恢复直线。
    未提供源几何的旧调用保留起伏和 skip 限制。只改 geometry/length，不改节点或属性。
    pending 可收集已通过直行条件、但被穿越护栏拦住的节点弦，供联合邻线整理重检。
    """
    if not len(links):
        return 0
    inc = defaultdict(list)
    for i in links.index:
        inc[links.at[i, "from_node"]].append(i)
        inc[links.at[i, "to_node"]].append(i)

    n_changed = 0
    for i in list(links.index[links.link_type == "connector"]):
        if "_is_uturn" in links and links.at[i, "_is_uturn"]:
            continue
        g = links.at[i, "geometry"]
        p = np.asarray(g.coords, dtype=float)
        sag, Lc = _chord_sag(p)
        if Lc < min_len or len(p) == 2:
            continue
        u = (p[-1, :2] - p[0, :2]) / Lc
        if source_lines is not None:
            source = source_lines.get(i)
            if source is None or not source.is_simple:
                continue
            source_sag, source_chord = _chord_sag(source.coords)
            if source_sag > .02 or source_chord < min_len or source.length - source_chord > .02:
                continue
            source_u = (np.asarray(source.coords[-1]) - np.asarray(source.coords[0])) / source_chord
            if float(source_u @ u) < math.cos(math.radians(max_ang)):
                continue
        elif i in skip or sag <= .02 or sag > min(max_sag, sag_ratio * Lc):
            continue
        if _directed_road_tangents(links, i, (u, u), inc, max_ang) is None:
            continue
        chord = LineString([tuple(p[0, :2]), tuple(p[-1, :2])])
        if not _replacement_safe(links, i, chord, source_lines=source_lines):
            if pending is not None:
                pending[i] = chord
            continue

        links.at[i, "geometry"] = chord
        links.at[i, "length"] = round(Lc, 3)
        n_changed += 1
        if changed is not None:
            changed.add(i)
    return n_changed


def _fit_turn(source, a, b, t_in, t_out, arm_fractions=None):
    """源单向弧的切向约束三次贝塞尔拟合，控制点限制在两条端口射线的交点以内。

    仅给常规单次转弯兜底，不能拟合真实 S 弯、掉头或环岛。参数按源弧长取样，
    先平顺分摊节点偏移再求两个控制臂，保持源弧度而不把转弯换成弦。
    arm_fractions 给定时用交点射线的指定比例，供有空间限制的同类转弯作有限重试。
    """
    matrix = np.column_stack([t_in, t_out])
    if abs(float(np.linalg.det(matrix))) < 1e-6:
        return None
    rays = np.linalg.solve(matrix, b - a)
    if (rays <= 0).any():
        return None
    if arm_fractions is None:
        p = _resample(np.asarray(source.coords), .5)
        s = _arc_len(p)
        u = s / s[-1]
        w = _smoothstep(u)[:, None]
        target = p + (1 - w) * (a - p[0]) + w * (b - p[-1])
        b0, b1, b2, b3 = (1 - u) ** 3, 3 * (1 - u) ** 2 * u, 3 * (1 - u) * u ** 2, u ** 3
        base = (b0 + b1)[:, None] * a + (b2 + b3)[:, None] * b
        design = np.stack([b1[:, None] * t_in, -b2[:, None] * t_out], axis=-1).reshape(-1, 2)
        arms = np.linalg.lstsq(design, (target - base).reshape(-1), rcond=None)[0]
        arms = np.clip(arms, .2 * rays, .85 * rays)
    else:
        arms = np.asarray(arm_fractions) * rays
    u = np.linspace(0, 1, max(20, int(source.length / .5) + 1))[:, None]
    q = ((1 - u) ** 3 * a + 3 * (1 - u) ** 2 * u * (a + arms[0] * t_in)
         + 3 * (1 - u) * u ** 2 * (b - arms[1] * t_out) + u ** 3 * b)
    q[0], q[-1] = a, b
    return LineString(q)


def _refine_connectors(links, source_lines, sigma=1.5, changed=None, skip=(), crossing_limits=None):
    """校正源小弯和常规转弯的吸附鼓包，优先把两端偏移沿源弧长平顺分摊。

    入口/出口需有沿行驶方向衔接的普通 road，源切向与邻居偏差不超过 10°。
    真实 S 弯、变道、掉头、自交或大幅节点迁移不做整线拟合。单次转弯分摊失败时
    尝试受切向约束的源弧拟合；每个候选都检查源偏离、反向回摆、端点与穿越。
    至多两轮重检被邻线挡住的候选，成功的线不反复拟合；skip 保留已确认直线。
    联合整理可传入原几何的逐对穿越上限，避免临时放入的直线放宽后续转弯保护。
    返回实际改写条数。
    """
    incidence = defaultdict(list)
    for i in links.index:
        incidence[links.at[i, "from_node"]].append(i)
        incidence[links.at[i, "to_node"]].append(i)
    completed, modified = set(skip), set()
    candidates = list(links.index[links.link_type == "connector"])

    def candidates_of(warped, source, a, b, tangents, single_turn):
        """先试保源分摊与最小二乘拟合，空间不满足时按源偏离排序有限控制臂候选。"""
        yield warped
        if not single_turn:
            return
        fitted = _fit_turn(source, a, b, *tangents)
        if fitted is None:
            return
        yield fitted
        alternatives = []
        for left in np.linspace(.2, .85, 6):
            for right in np.linspace(.2, .85, 6):
                curve = _fit_turn(source, a, b, *tangents, arm_fractions=(left, right))
                alternatives.append((curve.hausdorff_distance(source), curve))
        for _, curve in sorted(alternatives, key=lambda item: item[0]):
            yield curve

    for _ in range(2):
        before = len(modified)
        for i in candidates:
            if i in completed or i not in source_lines or ("_is_uturn" in links and links.at[i, "_is_uturn"]):
                continue
            source = source_lines[i]
            if source.length < 3.0 or not source.is_simple:
                completed.add(i)
                continue
            p = _resample(np.asarray(source.coords), .5)
            source = LineString(p)
            turns = _curve_turns(source)
            if np.max(np.abs(turns), initial=0.0) > 25.0:
                completed.add(i)
                continue
            targets = (-_end_tangent(p[::-1]), _end_tangent(p))
            tangents = _directed_road_tangents(links, i, targets, incidence)
            if tangents is None:
                completed.add(i)
                continue
            t_in, t_out = tangents
            net_turn = math.degrees(math.atan2(t_in[0] * t_out[1] - t_in[1] * t_out[0], float(t_in @ t_out)))
            source_sag, source_chord = _chord_sag(p)
            slight = source_sag <= min(.3, .01 * source_chord) and np.abs(turns).sum() <= 15.0
            reverse = float(turns[turns > 0].sum() if net_turn < 0 else -turns[turns < 0].sum())
            single_turn = 15 <= abs(net_turn) <= 135 and abs(float(turns.sum()) - net_turn) <= 15 and reverse <= 5
            if not slight and not single_turn:
                completed.add(i)
                continue
            original = links.at[i, "geometry"]
            a, b = np.asarray(original.coords)[[0, -1]]
            delta = max(float(np.linalg.norm(a - p[0])), float(np.linalg.norm(b - p[-1])))
            if delta > .4 * source.length:
                completed.add(i)
                continue
            u = _arc_len(p) / source.length
            w = _smoothstep(u)[:, None]
            q = p + (1 - w) * (a - p[0]) + w * (b - p[-1])
            q[0], q[-1] = a, b
            warped = smooth_line(LineString(q), sigma)
            # 独立整理的小变化可省略；联合候选即使变化小，也须重新满足原穿越上限。
            if crossing_limits is None and warped.hausdorff_distance(original) <= .05:
                completed.add(i)
                continue
            for candidate in candidates_of(warped, source, a, b, tangents, single_turn):
                # 拟合误差和端口横移共同影响源偏离，允许不超过两倍实际偏移；
                # 同时保留长度、方向、无回摆和逐对象穿越护栏，不因图面顺滑放宽空间约束。
                if (candidate.hausdorff_distance(source) > max(2.0 * delta, .5)
                        or not .75 <= candidate.length / source.length <= 1.4):
                    continue
                if single_turn:
                    new_turns = _curve_turns(candidate)
                    new_reverse = float(new_turns[new_turns > 0].sum() if net_turn < 0 else -new_turns[new_turns < 0].sum())
                    if new_reverse > max(1.0, reverse + .5):
                        continue
                if not _replacement_safe(links, i, candidate, crossing_limits=crossing_limits):
                    continue
                links.at[i, "geometry"] = candidate
                links.at[i, "length"] = round(candidate.length, 3)
                modified.add(i)
                completed.add(i)
                break
        if len(modified) == before:
            break
    if changed is not None:
        changed.update(modified)
    return len(modified)


def _refine_straight_neighbours(links, source_lines, sigma=1.5, straightened=None, refined=None):
    """联合整理源直行与共节点的阻挡转弯，整组安全后才写回 geometry/length。

    源共线、有向邻居和源交叉规则由 _straighten 复用。只允许连接路邻居参与，
    不移动主路、辅道或不相关节点。临时放直后，邻线须按原逐对穿越上限拟合；
    最后与全部 Link 复核，失败丢弃整组，成功保留节点、来源及全部非长度属性。
    """
    straightened = set() if straightened is None else straightened
    refined = set() if refined is None else refined
    pending = {}
    _straighten(links, source_lines=source_lines, changed=straightened, pending=pending)
    connector_ids = set(links.index[links.link_type == "connector"])
    for i, chord in pending.items():
        if _replacement_safe(links, i, chord, source_lines=source_lines):
            links.at[i, "geometry"] = chord
            links.at[i, "length"] = round(chord.length, 3)
            straightened.add(i)
            continue
        endpoints = {links.at[i, "from_node"], links.at[i, "to_node"]}
        blockers, limits = set(), {}
        for j in links.index:
            if i == j or chord.disjoint(links.at[j, "geometry"]):
                continue
            pair = (min(i, j), max(i, j))
            existing = _crossing_count(links.at[i, "geometry"], links.at[j, "geometry"])
            source, other_source = source_lines.get(i), source_lines.get(j)
            if (source is not None and other_source is not None
                    and source.is_valid and source.is_simple
                    and other_source.is_valid and other_source.is_simple):
                existing = max(existing, _crossing_count(source, other_source))
            limits[pair] = existing
            if _crossing_count(chord, links.at[j, "geometry"]) > existing:
                blockers.add(j)
        if not blockers or any(
                j not in connector_ids or j not in source_lines or j in straightened
                or not endpoints.intersection({links.at[j, "from_node"], links.at[j, "to_node"]})
                for j in blockers):
            continue
        trial = links.copy(deep=True)
        trial.at[i, "geometry"] = chord
        trial.at[i, "length"] = round(chord.length, 3)
        turn_ids = set()
        _refine_connectors(trial, source_lines, sigma=sigma, changed=turn_ids,
                           skip=connector_ids - blockers, crossing_limits=limits)
        updated = turn_ids | {i}
        # 整组逐对上限取修改前次数，只有涉及源直行的配对可恢复源已有交叉。
        for j in updated:
            for k in links.index:
                if j == k:
                    continue
                pair = (min(j, k), max(j, k))
                existing = _crossing_count(links.at[j, "geometry"], links.at[k, "geometry"])
                if i in pair and j in source_lines and k in source_lines:
                    source, other_source = source_lines[j], source_lines[k]
                    if (source.is_valid and source.is_simple
                            and other_source.is_valid and other_source.is_simple):
                        existing = max(existing, _crossing_count(source, other_source))
                limits[pair] = existing
        safe = True
        for j in updated:
            candidate = trial.at[j, "geometry"]
            trial.at[j, "geometry"] = links.at[j, "geometry"]
            safe = _replacement_safe(trial, j, candidate, crossing_limits=limits)
            trial.at[j, "geometry"] = candidate
            if not safe:
                break
        if not safe:
            continue
        for j in updated:
            links.at[j, "geometry"] = trial.at[j, "geometry"]
            links.at[j, "length"] = trial.at[j, "length"]
        straightened.add(i)
        refined.update(turn_ids)


@dataclass
class RefinementStats:
    """记录各处理阶段，原始表排序与执行先后保持兼容。"""

    merged: int
    smoothed: int
    repeated_smoothing: int
    length_changes: list
    coincided: int
    mid_coincided: int
    straightened: set = field(default_factory=set)
    refined: set = field(default_factory=set)
    protected_uturns: set = field(default_factory=set)


def refine_geometry(links, node_xy, merge_turns=True, smooth=1.5, smooth_roads=False,
                    fillet=4.0, coincide=True, mid_coincide=True, straighten=True,
                    refine_connectors=True):
    """按既定阶段整理代表线；源掉头只做节点偏移分摊，保留回转弧，不进入普通处理链。"""
    # --- 5b. 同一转向的多条平行连接路只留最中间那条 ---
    n_merge = 0
    if merge_turns:
        links, n_merge = _merge_turns(links)
    source_lines = (links.pop("_source_geometry").dropna().to_dict()
                    if "_source_geometry" in links else {})
    uturn_ids = set(links.index[links["_is_uturn"]]) if "_is_uturn" in links else set()

    # --- 5c. 平滑(默认只平滑路口连接路, 主路几何保持原样) ---
    n_smooth = 0
    n_re = 0             # 一遍压不平、补了平滑的条数
    dlen = []
    if smooth > 0:
        idx = (links.index if smooth_roads
               else links.index[links.link_type == "connector"])
        for i in idx:
            if i in uturn_ids:
                continue
            g0 = links.at[i, "geometry"]
            g1 = smooth_line(g0, smooth)
            # 急弯一遍高斯压不平: 重采样点数按输入长度取, 平滑一收缩点就挤成
            # 微段, 源几何里的尖角也露出来(实测 Town06/799 残留 46°)。
            # 还有 >25° 折角就再平滑一遍, 最多 3 遍 —— 只有问题线多付长度代价。
            for _ in range(2):
                if _kink_deg(g1) <= 25.0:
                    break
                g1 = smooth_line(g1, smooth)
                n_re += 1
            if len(g1.coords) < 2:
                continue
            dlen.append(g1.length - g0.length)
            links.at[i, "geometry"] = g1
            links.at[i, "length"] = round(g1.length, 3)
            n_smooth += 1

    # --- 5d. 贴线重合合流: 连接路端部真穿越同节点其它 link 的,
    #      交点后改沿对方折线走到底(重合段), 视觉上从内侧并入, 不再斜穿 ---
    n_coincide = 0
    touched = set()          # 已贴线几何只有通过源形状、有向邻居及穿越护栏才允许再整理。
    if coincide:
        n_coincide = _coincide(links, node_xy, fillet=fillet, changed=touched, skip=uturn_ids)

    # --- 5e. 中段浅穿: 连接路中段扎进另一条线再出来(浅舌头), 换成贴线 ---
    n_mid = 0
    if coincide and mid_coincide:
        n_mid = _mid_coincide(links, changed=touched, skip=uturn_ids)

    # --- 5f. 源车道群共线、前后有向道路顺直时拉直，避免输出起伏阈值漏掉吸附鼓包 ---
    n_straight = 0
    straight_ids = set()
    if straighten:
        n_straight = _straighten(links, skip=touched, source_lines=source_lines,
                                changed=straight_ids)

    # --- 5g. 源小弯及常规转弯的节点偏移分摊，去掉两端局部 S 收口带出的回摆 ---
    curve_ids = set()
    if refine_connectors:
        _refine_connectors(links, source_lines, sigma=smooth, changed=curve_ids,
                           skip=straight_ids | uturn_ids)
    if straighten and refine_connectors:
        # 共节点的直行与转弯可能互相阻挡；只应用整组通过原穿越上限的临时候选。
        _refine_straight_neighbours(links, source_lines, sigma=smooth,
                                   straightened=straight_ids, refined=curve_ids)
        n_straight = len(straight_ids)
        curve_ids.difference_update(straight_ids)


    return links, RefinementStats(n_merge, n_smooth, n_re, dlen, n_coincide, n_mid,
                                  straight_ids, curve_ids, uturn_ids)
