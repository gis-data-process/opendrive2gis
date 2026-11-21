"""Python 单文件与批量命令入口；原参数、默认目录及输出约定保持兼容。"""
import argparse
import glob
import os
import sys

from common.paths import DATA, OUT
from .builder import build_net


def main():
    ap = argparse.ArgumentParser(
        description="OpenDRIVE → 城市路网 Link-Node 模型",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xodr", nargs="?", help="输入 .xodr 文件")
    ap.add_argument("-o", "--output", default=None, help="输出 .gpkg 路径")
    ap.add_argument("--all", action="store_true", help="处理 data/ 下全部 Town*.xodr")
    ap.add_argument("--step", type=float, default=1.0, help="参考线采样步长(米)")
    ap.add_argument("--snap", type=float, default=0.05,
                   help="节点合并容差(米), 默认 0.05")
    ap.add_argument("--min-len", type=float, default=0.1,
                   help="短于该长度的 link 丢弃(米)。CARLA 里有 0.46 m 这种超短连接路, "
                        "丢掉会把路网切块, 所以默认留着")
    ap.add_argument("--csv", action="store_true", help="同时导出 link/node 的 CSV")
    ap.add_argument("--lane-types", default="driving,bidirectional",
                   help="纳入的 lane/@type，逗号分隔；默认 driving,bidirectional，匝道可增加 onRamp/offRamp")
    ap.add_argument("--keep-connectors", action="store_true",
                   help="保留无有效车道方向的占位连接线，passable=0；使用独立节点，默认过滤")
    ap.add_argument("--no-shoulder-uturns", action="store_true",
                   help="关闭源路肩掉头例外；默认只保留能按实际行驶方向接通的路肩掉头，普通路肩仍过滤")
    ap.add_argument("--no-merge-turns", action="store_true",
                   help="不合并同一转向的平行连接路。默认合并: 起止节点相同的多条连接路 "
                        "只留最中间那条(它们的起止节点一样, 去掉不影响连通性)")
    ap.add_argument("--smooth", type=float, default=1.5,
                   help="普通连接路平滑强度，高斯 σ(米)，0 = 不平滑，默认 1.5；源掉头保留回转弧")
    ap.add_argument("--smooth-roads", action="store_true",
                   help="主路也一起平滑。默认只平滑路口连接路(主路保持 xodr 原始几何)")
    ap.add_argument("--fillet", type=float, default=4.0,
                   help="端部过渡的退距控制(米)，默认 4；按可用线段长度限制，非圆弧半径；"
                        "0 = 关闭该局部过渡")
    ap.add_argument("--no-lane-center", action="store_true",
                   help="使用道路基准中心线。默认各方向走自己的车道群中线，侧别按 RHT/LHT 决定")
    ap.add_argument("--lane-ramp", type=float, default=0.0,
                   help="端部把车道中线收回到道路中心线的渐变长度(米), 默认 0 = 不收拢。"
                        ">0 会让主路 link 在路口附近变弯, 一般不需要")
    ap.add_argument("--merge-r", type=float, default=12.0,
                   help="端部 S 形横向平移的目标半径(米), 默认 12。节点处有横向偏移时"
                        "用双圆弧滑到节点, 不再垂直于行驶方向补小尾巴(那样会出 90° 小钩); "
                        "0 = 关闭, 退回直线补尾 + 圆角")
    ap.add_argument("--no-coincide", action="store_true",
                   help="不把连接路端部与同节点其它 link 的真交叉改成贴线重合合流。"
                        "默认开启: 交点以后沿对方折线并入节点(如右转并进直行道), "
                        "视觉上不再斜穿")
    ap.add_argument("--no-mid-coincide", action="store_true",
                   help="不修连接路中段的切角: 默认把'扎进另一条 link 的弯里又切出去'"
                        "改成沿对方绕行到出弯点再切向出弯")
    ap.add_argument("--no-straighten", action="store_true",
                   help="关闭直行拉直；默认将源车道群共线且前后有向道路顺直的连接线换成节点间直线")
    ap.add_argument("--no-refine-connectors", action="store_true",
                   help="关闭源中线几何校正；默认平顺分摊节点偏移，消除小弯和常规转弯的接点回摆")
    ap.add_argument("--no-soft", action="store_true",
                   help="关闭端部浅贝塞尔过渡；默认同时处理厘米级偏移造成的短末段折角")
    ap.add_argument("--no-split-taper", action="store_true",
                   help="关闭渐变辅道及其独立端口拆分；默认收窄端接主组，"
                        "全宽端有源车道连接时单独建节点，混合连接路按车道组保留路径")
    args = ap.parse_args()
    if args.all and (args.xodr or args.output):
        ap.error("--all 不能同时指定输入或单个输出路径")
    lane_types = tuple(t.strip() for t in args.lane_types.split(",") if t.strip())

    # 默认输出落在项目 out/；显式相对路径按当前工作目录解析。
    data_dir = DATA
    out_dir = OUT

    if args.all:
        files = sorted(glob.glob(os.path.join(data_dir, "Town*.xodr")))
        if not files:
            sys.exit("data/ 下没有 Town*.xodr")
    elif args.xodr:
        files = [args.xodr]
    else:
        ap.error("给一个 .xodr 路径, 或用 --all")

    for path in files:
        out = args.output or os.path.join(
            out_dir, os.path.splitext(os.path.basename(path))[0] + "_net.gpkg")
        build_net(path, out, step=args.step, snap=args.snap,
                  min_len=args.min_len, csv=args.csv,
                  lane_types=lane_types,
                  keep_conn=args.keep_connectors,
                  merge_turns=not args.no_merge_turns,
                  smooth=args.smooth, smooth_roads=args.smooth_roads,
                  fillet=args.fillet,
                  lane_center=not args.no_lane_center,
                  lane_ramp=args.lane_ramp,
                  split_taper=not args.no_split_taper,
                  merge_r=args.merge_r,
                  coincide=not args.no_coincide,
                  mid_coincide=not args.no_mid_coincide,
                  straighten=not args.no_straighten,
                  soft=not args.no_soft,
                  refine_connectors=not args.no_refine_connectors,
                  shoulder_uturns=not args.no_shoulder_uturns)
        print()
