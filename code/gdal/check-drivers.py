"""检查当前 Python 环境的 GDAL 是否带 XODR(OpenDRIVE) 驱动。

用法（在项目根目录执行）:
    .venv\\Scripts\\python.exe code\\gdal\\check-drivers.py
"""
import sys

try:
    import pyogrio
except ImportError:
    print("未安装 pyogrio。请先在虚拟环境执行: pip install -r requirements.txt")
    sys.exit(1)

print("Python    :", sys.version.split()[0])
print("pyogrio   :", pyogrio.__version__)
print("GDAL      :", pyogrio.__gdal_version_string__)
print("GEOS      :", pyogrio.__geos_version_string__ if hasattr(pyogrio, "__geos_version_string__") else "?")

drivers = pyogrio.list_drivers()
hit = [d for d in drivers if "XODR" in d.upper() or "OPENDRIVE" in d.upper()]

print("驱动总数  :", len(drivers))
print("XODR 驱动 :", hit if hit else "❌ 不可用")

if hit:
    print("\n✅ 可以直接用 GDAL 官方驱动:")
    print("   ogr2ogr -f GPKG out.gpkg in.xodr -oo DISSOLVE_TIN=YES")
else:
    print("\n❌ 当前环境的 GDAL 编译时没有带 libOpenDRIVE(>=0.6.0), XODR 驱动未启用。")
    print("   是否可用取决于 GDAL 的编译选项，可使用带该驱动的自编译版本或 Docker 镜像。")
    print("   两条路:")
    print("   1) 用 code\\own\\xodr2gpkg.py（实现常用 OpenDRIVE 元素，不依赖 XODR 驱动）")
    print("   2) 用 Python 运行 code/gdal/xodr2gpkg_docker.py（官方声明支持 1.4，较新文件需实测）")
