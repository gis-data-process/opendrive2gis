"""交叉验证: 官方 GDAL XODR 驱动 vs 自研 xodr2gpkg.py

用法（在项目根目录执行）:
    .venv\\Scripts\\python.exe code/compare/compare.py data/sample_gdal.gpkg data/sample_own.gpkg
"""
import sys
import argparse
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compare_ab import scan  # noqa: E402


# 简表与完整报告共用统计口径；TIN 通过 SQLite/WKB 读取。
CANDIDATES = ["ReferenceLine", "LaneBorder", "Lane", "RoadMark", "RoadSignal", "RoadObject"]


def summarize(path):
    """汇总存在的比较图层，TIN 也参与统计；读取失败抛错，不能当作缺层跳过。"""
    if not os.path.isfile(path):
        raise ValueError(f"输入文件不存在: {path}")
    return {name: {"n": values["count"], "type": values["geom_type"],
                   "null": values["null"], "len": values["total_length"],
                   "area": values["total_area"]}
            for name, values in scan(path).items() if name in CANDIDATES}


def show(tag, d):
    print(f"\n=== {tag} ===")
    print(f"{'图层':<15}{'要素':>6}{'总长度(m)':>16}{'总面积(m2)':>16}")
    tl = ta = 0.0
    for k, v in d.items():
        if v.get("n", 0) == 0:
            print(f"{k:<15}{'—':>6}   {v.get('err','')}")
            continue
        print(f"{k:<15}{v['n']:>6}{v['len']:>16.2f}{v['area']:>16.2f}")
        tl += v["len"]
        ta += v["area"]
    print(f"{'合计':<15}{'':>6}{tl:>16.2f}{ta:>16.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("official")
    ap.add_argument("own")
    args = ap.parse_args()
    a, b = args.official, args.own
    da, db = summarize(a), summarize(b)
    show("官方 GDAL XODR 驱动", da)
    show("自研 xodr2gpkg.py", db)

    print("\n=== 差异 ===")
    keys = sorted(set(da) | set(db))
    print(f"{'图层':<15}{'要素(官方/自研)':>22}{'长度偏差':>14}{'面积偏差':>14}")
    for k in keys:
        va, vb = da.get(k, {"n": 0}), db.get(k, {"n": 0})
        if va.get("n", 0) == 0 or vb.get("n", 0) == 0:
            print(f"{k:<15}{va.get('n',0):>10}/{vb.get('n',0):<11}{'—':>14}{'—':>14}")
            continue
        dl = (vb["len"] - va["len"]) / va["len"] * 100 if va["len"] else 0.0
        dar = (vb["area"] - va["area"]) / va["area"] * 100 if va["area"] else 0.0
        print(f"{k:<15}{va['n']:>10}/{vb['n']:<11}{dl:>13.2f}%{dar:>13.2f}%")


if __name__ == "__main__":
    main()
