"""按源车道定义计算方向、车道群中线、渐变车道和限速。"""
import numpy as np

from own.xodr2gpkg import (coeffs_of, lane_sections, lane_speed, lane_width_array,
                           pick, poly, road_speed, f)
from .geometry import _smoothstep


DRIVABLE = frozenset({"driving", "bidirectional"})


def road_link(el):
    """{'predecessor'|'successor': (element_id, element_type, contact_point)}"""
    out = {}
    lk = el.find("link")
    if lk is None:
        return out
    for tag in ("predecessor", "successor"):
        e = lk.find(tag)
        if e is not None:
            out[tag] = (e.get("elementId"), e.get("elementType", "road"),
                        e.get("contactPoint", "start"))
    return out


def lane_directions(lane, rule, side):
    """返回车道可行驶的源里程方向；共享双向声明同时进入两个方向集合。

    side 为左 +1/右 −1，standard 沿 road 规则对应的默认方向，reversed 反转。
    type=bidirectional 或 direction=both 均表示共享车道，重复声明不会加倍计数。
    本函数仅解析方向，不扩大 lane_types 所允许的车道类型。
    """
    declaration = lane.get("direction", "standard").strip().lower()
    if lane.get("type") == "bidirectional" or declaration == "both":
        return (1, -1)
    default = side if rule == "LHT" else -side
    if declaration in ("", "standard"):
        return (default,)
    if declaration == "reversed":
        return (-default,)
    raise ValueError(f"lane {lane.get('id')} 的 direction 无法识别: {declaration}")


def lane_stats(el, types=DRIVABLE):
    """(左侧车道数, 右侧车道数, 限速 m/s)。车道数跨段取最大，限速取已知最小值。

    默认纳入 driving/bidirectional；这里按物理侧统计，不重复数共享车道。
    车道未声明限速时继承 road/type/speed。一个 road
    不按限速变化拆边，故最小限速代表保守的整段通行代价。
    """
    lanes_el = el.find("lanes")
    if lanes_el is None:
        return 0, 0, None
    L = R = 0
    spd = None
    for lsec in lanes_el.findall("laneSection"):
        left = lsec.findall("left/lane")
        right = lsec.findall("right/lane")
        L = max(L, sum(1 for l in left if l.get("type") in types))
        R = max(R, sum(1 for l in right if l.get("type") in types))
        for l in left + right:
            if l.get("type") in types:
                s = lane_speed(l)
                if s is None:
                    s = road_speed(el, f(lsec, "s"))
                if s is not None:
                    spd = s if spd is None else min(spd, s)
    return L, R, spd


def group_speed(el, side, types, lane_ids, direction=None, lane_membership=None,
                direction_override=None):
    """该方向/车道组沿 road 的最小已知限速（m/s），包含记录中途变化。

    车道的 speed 优先于道路 type/speed；仅检查当前段可通行的车道，
    不把对向车道或已经拆出的辅道限速混入主组。
    direction 给定时从两侧筛选该方向的车道，共享车道在每个方向仅计一次。
    lane_membership 将连接路限速限制到对应的跨段轨迹，避免重用车道 ID 混入其他组。
    direction_override 仅用于经源 junction 核实的路肩掉头；路肩方向以接触端为准。
    """
    sections = sorted(el.findall("lanes/laneSection"), key=lambda e: f(e, "s"))
    speeds = []
    tags = ("left", "right") if direction is not None else ("left" if side > 0 else "right",)
    for i, lsec in enumerate(sections):
        sa = f(lsec, "s")
        sb = f(sections[i + 1], "s") if i + 1 < len(sections) else f(el, "length")
        for tag in tags:
            for lane in lsec.findall(tag + "/lane"):
                if lane.get("type") not in types or (lane_ids and int(lane.get("id")) not in lane_ids):
                    continue
                if lane_membership is not None and (i, int(lane.get("id"))) not in lane_membership:
                    continue
                source_directions = ((direction_override,) if direction_override is not None and lane.get("type") == "shoulder"
                                     else lane_directions(lane, el.get("rule", "RHT"), 1 if tag == "left" else -1))
                if direction is not None and direction not in source_directions:
                    continue
                positions = {sa}
                positions.update(sa + f(e, "sOffset") for e in lane.findall("speed") if sa + f(e, "sOffset") < sb)
                positions.update(f(e, "s") for e in el.findall("type") if sa < f(e, "s") < sb)
                for s in positions:
                    value = lane_speed(lane, s - sa)
                    if value is None:
                        value = road_speed(el, s)
                    if value is not None:
                        speeds.append(value)
    return min(speeds) if speeds else None


def road_type_of(el):
    t = el.find("type")
    return t.get("type", "") if t is not None else ""


def is_connector_port(port, roads, conn_dir):
    """该端口是否属于 junction 内部的 connecting road"""
    rid = port[0]
    return rid in conn_dir or (rid in roads and roads[rid].junction != "-1")


def _offsets_to_points(road, t):
    """横向偏移序列 -> 平面点列(与 road.S 对齐)。t 已含 laneOffset 基准。"""
    S = road.S
    Sf = float(S[-1])
    sc = np.clip(S, 0.0, Sf)
    hdg = np.arctan2(np.interp(sc, S, road.SIN), np.interp(sc, S, road.COS))
    x = np.interp(sc, S, road.X) - t * np.sin(hdg)
    y = np.interp(sc, S, road.Y) + t * np.cos(hdg)
    return list(zip(x.tolist(), y.tolist()))


def _lane_arrays(road, side, lane_membership=None, direction_override=None):
    """把某一侧所有车道展开成"逐采样点"数组。

    返回 (base, lanes):
        base  —— 道路中心线(参考线 + laneOffset)的横向偏移, 与 road.S 对齐
        lanes —— {lane_id: {"w": 宽度, "mid": 相对中心线的车道中心偏移,
                            "type": 类型, "directions": 方向位掩码(+s=1, −s=2, 双向=3)}}
                 车道在某个采样点不存在(不在该 laneSection 里)时对应位置为 nan
    lane_membership 指定 (laneSection 下标, lane ID) 集合时，只筛选类型参与组线；
    横断面累积仍计入全部物理车道，不能删除内侧车道后重新计算位置。
    direction_override 只赋给已经限定轨迹的 shoulder，由源 junction 接触端确定行驶方向。
    """
    S = road.S
    n = len(S)
    lanes_el = road.el.find("lanes")
    offs = []
    if lanes_el is not None:
        for lo in lanes_el.findall("laneOffset"):
            offs.append((f(lo, "s"), coeffs_of(lo)))
        offs.sort(key=lambda t: t[0])

    def off_at(s):
        co = pick(offs, s)
        if co is None:
            return 0.0
        s_i, c = co
        return poly(c, s - s_i)

    base = np.array([off_at(s) for s in S], dtype=float)
    lanes = {}
    if lanes_el is None:
        return base, lanes

    tag = "left" if side > 0 else "right"
    secs = lane_sections(road)
    for i, (sa, sb, lsec) in enumerate(secs):
        last = i == len(secs) - 1
        # 分界点归后一段；精确上下界避免把相邻微米区间误当成同一段。
        m = (S >= sa) & ((S <= sb) if last else (S < sb))
        if not m.any():
            continue
        ss = S[m]
        sec_len = max(sb - sa, 0.0)
        # 由内向外: 左侧 id 1,2,3...; 右侧 id -1,-2,-3...
        cum = np.zeros(len(ss))
        for lane in sorted(lsec.findall("%s/lane" % tag),
                           key=lambda l: abs(int(float(l.get("id"))))):
            # <width>/@sOffset 相对 laneSection 起点, ds 夹在本段长度内
            ds = np.clip(ss - sa, 0.0, sec_len)
            w = lane_width_array(lane, ds, base[m] + side * cum)
            mid = cum + w / 2.0            # 车道中心线
            cum = cum + w
            lid = int(float(lane.get("id")))
            d = lanes.setdefault(lid, {"w": np.full(n, np.nan),
                                       "mid": np.full(n, np.nan),
                                       "type": np.full(n, None, dtype=object),
                                       "directions": np.zeros(n, dtype=np.uint8)})
            d["w"][m] = w
            d["mid"][m] = mid
            d["type"][m] = (lane.get("type") if lane_membership is None or (i, lid) in lane_membership else None)
            directions = ((direction_override,) if direction_override is not None and lane.get("type") == "shoulder"
                          and lane_membership is not None and (i, lid) in lane_membership
                          else lane_directions(lane, road.el.get("rule", "RHT"), side))
            d["directions"][m] = (1 if 1 in directions else 0) | (2 if -1 in directions else 0)
    return base, lanes


def _taper_weight(S, w, w_ref=2.0):
    """渐变车道的"独立性"权重 g(s): 宽度够(w >= w_ref)处为 1, 收到 0 的那一端为 0。

    只在"窄于 w_ref 的那一段"里做平滑过渡 —— 车道还宽的时候绝不拉偏它的中心线,
    收窄的最后十几米才并回直行组中线, 画出来就是一条汇入(或分流)曲线。
    """
    n = len(S)
    if n < 2:
        return np.zeros(n)
    S0, Sf = float(S[0]), float(S[-1])
    wv = np.nan_to_num(w, nan=0.0)
    narrow = ~(wv >= w_ref)
    if not narrow.any():
        return np.ones(n)
    if narrow[0] and not narrow[-1]:
        i = int(np.argmax(~narrow))                  # 第一个足够宽的采样点
        s_b = float(S[max(i - 1, 0)])
        return _smoothstep((S - S0) / max(s_b - S0, 1e-6))
    if narrow[-1] and not narrow[0]:
        i = n - 1 - int(np.argmax((~narrow)[::-1]))  # 最后一个足够宽的采样点
        s_a = float(S[min(i + 1, n - 1)])
        return 1.0 - _smoothstep((S - s_a) / max(Sf - s_a, 1e-6))
    # 两端都窄(中间宽)或全程都窄: 退回按宽度比例, 只保证偏移不出谱
    return np.clip(wv / max(w_ref, 1e-6), 0.0, 1.0)


def side_lane_groups(road, side, types=DRIVABLE, ramp=0.0, split=True,
                     w_ref=2.0, w_die=0.4, direction=None, lane_membership=None,
                     direction_override=None):
    """某一方向的"车道群中线", 按辅道/直行拆成 1~2 条。

    side = -1  右侧车道(id<0)；side = +1 左侧车道(id>0)。行驶方向由
    road/@rule、lane/@direction 和 junction/contactPoint 决定。
    direction 给定时从两侧取该方向可用车道并聚合，共享双向车道同时可用于两个方向。
    存在共享双向车道时不拆 aux，保证每 road 每方向只出一个主组，避免重复边。
    lane_membership 为连接路分组的跨段车道轨迹，只限制参与中线的成员，不改变物理偏移。
    direction_override 仅供已确认源关系的路肩掉头使用，不改变普通 road 或其他车道方向。

    每个 s 处取该组 types 车道中心横向偏移的**中位数**(中间那条车道),
    不是平均值 —— 车道宽度不均时平均值会被宽车道拽偏。
    该侧没有可通行车道的位置, 横向偏移退回道路中心线(含 laneOffset)。

    拆分规则(split=True):
        直行组 = 全程都够宽(宽 >= w_die)的车道   -> 取中位数中线
        渐变组 = 某处细到 w_die 以下、别处宽过 0.5 m 的车道 —— 每条单独一组,
                 几何用自己的车道中线, 但在收窄端用 smoothstep 并回直行组中线。
    渐变车道必须整条 road 都在才拆(只存在一段的端点会落在路中间, 会产生悬挂节点)。
    拆不开(只剩一条组 / 没有渐变车道)时退回原来的"整侧中位数"行为。

    返回 [{"pts": 点列, "lid": [车道 id], "taper": 0/1}, ...]，第一组为主组；
    end_lids/end_wide_lids 分别为源首末端有效/保持独立宽度的车道 ID，供拓扑配对。
    """
    S = road.S
    n = len(S)
    if n == 0:
        return []
    base, lanes = _lane_arrays(road, side, lane_membership, direction_override)
    sign = 1.0 if side > 0 else -1.0
    if direction is not None:
        # 两侧偏移统一成相对 laneOffset 的有符号值；先保留默认侧的原有 ID 顺序。
        _, opposite = _lane_arrays(road, -side, lane_membership, direction_override)
        for values in lanes.values():
            values["mid"] *= side
        for values in opposite.values():
            values["mid"] *= -side
        lanes.update(opposite)
        sign = 1.0
    cands = []
    for lid, values in lanes.items():
        allowed = np.isin(values["type"], list(types)) & np.isfinite(values["w"])
        if direction is not None:
            allowed &= (values["directions"] & (1 if direction > 0 else 2)) != 0
        values["mid"] = np.where(allowed, values["mid"], np.nan)
        values["allowed"] = allowed
        if np.any(allowed & (values["w"] > 0.05)):
            cands.append(lid)
    if not cands:
        return []
    shared_ids = [lid for lid in cands if np.any(lanes[lid]["allowed"] & (lanes[lid]["directions"] == 3)
                                                & (lanes[lid]["w"] > 0.05))]
    if direction is not None and (shared_ids or (any(lid > 0 for lid in cands) and any(lid < 0 for lid in cands))):
        split = False

    # 渐变候选: 整条 road 都在、某处细到 w_die 以下、别处又宽过 0.5 m
    cand_taper = []
    if split and len(cands) >= 2:
        for lid in cands:
            w = lanes[lid]["w"]
            if not np.isfinite(w).all() or not lanes[lid]["allowed"].all():
                continue                     # 只想拆"整条都在"的车道
            if np.max(w) > 0.5 and np.min(w) < w_die:
                cand_taper.append(lid)
    # 只拆位于车道群"一端"的渐变车道: 若内外两侧都有稳定车道, 直行组的中位数
    # 本来就穿过这条渐变车道(实测 301210053 的 -2 夹在 -1 与 -3 之间),
    # 再拆出来会和直行线重叠 —— 那种情况保持原来的整侧中位数。
    persist = [l for l in cands if l not in cand_taper]
    taper = []
    for lid in cand_taper:
        inner = [l for l in persist if abs(l) < abs(lid)]
        outer = [l for l in persist if abs(l) > abs(lid)]
        if not (inner and outer):
            taper.append(lid)
    main_ids = [l for l in cands if l not in taper]
    if not taper or not main_ids:
        taper, main_ids = [], list(cands)

    arr = np.vstack([lanes[l]["mid"] for l in main_ids])
    mid_main = np.full(n, np.nan)
    ok = np.isfinite(arr).any(axis=0)
    if ok.any():
        mid_main[ok] = np.nanmedian(arr[:, ok], axis=0)
    t_main = np.where(np.isfinite(mid_main),
                      base + sign * np.nan_to_num(mid_main), base)

    # 端部 ramp: 把横向偏移平滑压回 0, 端点坐标因此不变。
    # 偏移幅度要按"这条 road 够不够长"自动缩放 —— smoothstep 的最大斜率是
    # 1.5*offset/r, 若直接把 15 m 的 ramp 套在 6 m 长的路上, 斜率冲到 45°,
    # 折角检测会立刻报几十处。要求斜率 <= tan(20°)=0.36 且两端各占 r:
    #   r >= 4.2*offset, L >= 8.4*offset  ->  offset <= L/8.4
    # 放不下的短路就按比例缩小偏移(最多缩到两条线几乎贴住, 但绝不长折角)。
    Sf = float(S[-1])
    L = Sf - float(S[0])
    dev = t_main - base
    offmax = float(np.max(np.abs(dev))) if n else 0.0
    if offmax > 1e-6 and L > 1e-6:
        if ramp > 0:
            k = min(1.0, L / (8.4 * offmax))
            r = min(ramp, 0.5 * L)
            u = S - float(S[0])
            w = _smoothstep(u / r) * _smoothstep((L - u) / r)
        else:
            k, w = 1.0, np.ones(n)   # ramp=0: 整条都偏移, 端点靠 _emit 补线接
        # 缩放后连 0.5 m 都拉不开的(几米长的短路段), 分离没有意义,
        # 只会把 4~5 个采样点拧出折角 —— 干脆保持中心线
        # 路肩掉头必须保留窄车道的真实中线，不能把不足半米的偏移压回参考基准。
        t_main = base + w * k * dev if direction_override is not None or k * offmax >= 0.5 else base
    else:
        t_main = base

    # lane ID 可以跨段增减，容量按每个位置同时有效的车道数求最大，不能数 ID 并集。
    capacity = max(int(np.sum([lanes[l]["allowed"][i] and lanes[l]["w"][i] > 0.05
                              for l in main_ids])) for i in range(n))
    groups = [{"pts": _offsets_to_points(road, t_main), "lid": main_ids,
               "taper": 0, "lanes": capacity, "shared_lid": [lid for lid in main_ids if lid in shared_ids]}]
    for lid in taper:
        w = lanes[lid]["w"]
        mid = np.nan_to_num(lanes[lid]["mid"])
        dev_own = sign * mid
        # 合并目标 = 相邻常驻车道的中心(不是直行组中位线)。车从渐变车道并出去,
        # 实际并入的是紧挨着的那条车道, 滑到它的中心线上才像一次真实的合流/分流。
        # (实测 202310008 / 301210014: 旧目标是两车道分界, 辅道线整段骑在车道带外)
        inner = [l for l in persist if abs(l) < abs(lid)]
        outer = [l for l in persist if abs(l) > abs(lid)]
        adj = max(inner, key=abs) if inner else min(outer, key=abs)
        dev_adj = sign * np.nan_to_num(lanes[adj]["mid"])
        # 参考宽度跟着车道自己的宽度走: 本来就窄的车道(1 m 级的)得早点并线
        ref = max(min(w_ref, 0.6 * float(np.max(w))), 2.0 * w_die)
        g = _taper_weight(S, w, ref)
        # 全程 = g*自身中心 + (1-g)*相邻车道中心, 不再往节点方向拉 —— 旧做法
        # (端部 ramp 回 dev_main) 实测会把整条辅道压出车道带(over +3~5 m)。
        # 收窄端到主组代表线仍可能有半车道宽残距，由端部收口处理；全宽且有源连接的
        # 辅道端使用自身端口，不能为了共用节点再把它拉回主组。
        dev_aux = g * dev_own + (1.0 - g) * dev_adj
        groups.append({"pts": _offsets_to_points(road, base + dev_aux),
                       "lid": [lid], "taper": 1, "lanes": 1, "shared_lid": [lid] if lid in shared_ids else []})
    for group in groups:
        # 端点车道必须按该端所在段与实际宽度判断，不能用跨段 ID 并集配 road/link。
        group["end_lids"] = tuple(tuple(lid for lid in group["lid"]
                                       if lanes[lid]["allowed"][index] and lanes[lid]["w"][index] > .05)
                                  for index in (0, -1))
        group["end_wide_lids"] = tuple(tuple(lid for lid in group["lid"]
                                            if lanes[lid]["allowed"][index] and lanes[lid]["w"][index] > w_die)
                                       for index in (0, -1))
    return groups


def side_center_points(road, side, types=DRIVABLE, ramp=0.0, split=True):
    """只取主(直行)组的中线点列 —— 兼容旧调用; 要拆辅道请用 side_lane_groups"""
    groups = side_lane_groups(road, side, types, ramp, split=split)
    return groups[0]["pts"] if groups else []
