# OpenDRIVE GIS 提取与常规路网生成

## 1. 项目用途与文档分工

本项目将 OpenDRIVE 转为 GIS 图层，并构建常规有向路网。本文是脚本使用说明，集中介绍环境、调用和传参、输入输出、字段及验收方法。docs 下三篇是可独立阅读的技术文章，按原理、处理流程、真实案例和注意事项组织：

1. [从 OpenDRIVE 中提取 GIS 图层的实现思路](docs/从opendrive中提取GIS图层的实现思路.md)：自研参考线、车道横断面、八类图层和校验。
2. [基于 GDAL 官方驱动从 OpenDRIVE 中提取 GIS 图层](docs/基于GDAL官方驱动从OpenDRIVE中提取GIS图层.md)：驱动、Docker、TIN 与二维整理。
3. [基于 OpenDRIVE 路网生成宏观 Link 路网图层](docs/基于车道中心线和junction%20line生成常规路网图层.md)：方向、Node/Link、辅道接点、直行与转弯整理，结合历史问题逐类图解。

当前 A/B 指标见 [COMPARE_GDAL.md](COMPARE_GDAL.md)。原始输入位于 data/，生成文件分别位于 out/ 和 out_gdal/。

Git 仓库包含代码、回归测试、三篇技术文章、配图和公众号排版 HTML。源地图、虚拟环境、转换产物和历史归档保留在本地；README 中的 out/、out_gdal/、archive/ 路径对应本地文件。克隆后先安装依赖，再将自己的 XODR 放到 data/，或在调用时指定文件路径。

## 2. 目录与 Python 环境

### 2.1. 目录职责

| 目录 | 内容 |
|---|---|
| code/own | 自研 XML 解析、八类图层生成、只读校验和预览 |
| code/gdal | 官方驱动入口、TIN 整理、可选路面合成 |
| code/roadnet | 复用 own 解析的二维 road 级有向图 |
| code/common | 默认路径和输出文件保护 |
| code/compare | A/B 统计、叠加和中心线合并 |
| code/diag | 路网校验、局部几何诊断和数学配图 |
| data | XODR 源数据及保留的原始 sample 对照 |
| out | 自研 GPKG、路网 GPKG/CSV、预览和验证摘要 |
| out_gdal | 原始官方 GPKG、二维 _poly 整理版及统计 |
| docs/figures | 三篇技术文章的流程、原理与案例配图 |
| archive | 优化前文件和已清理实验的可恢复备份 |

默认目录按脚本位置定位项目根目录，显式相对路径按调用目录解析。迁移到其他机器时复制项目和数据，再创建对应环境。

### 2.2. 选择解释器与安装依赖

使用 Python 3.11 或更新版本，依赖固定在 [requirements.txt](requirements.txt)。已有 .venv 可以直接使用；新机器在项目根目录执行：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

下文的 `.venv/Scripts/python.exe` 就是项目虚拟环境的 Python 解释器，直接运行它即可，无须先激活环境。若终端或 IDE 已选择该解释器，可把命令前缀换成 `python`。macOS/Linux 改用 `.venv/bin/python`。

所有示例默认在项目根目录运行，即当前目录下能看到 code、data 和 requirements.txt。

### 2.3. 路网代码的模块分工

`code/roadnet/roadnet.py` 保留为兼容入口，负责接入原来的命令和函数导入。具体处理按职责拆到同目录的模块中：

| 模块 | 职责 |
|---|---|
| __init__.py / roadnet.py / cli.py | 公开 `build_net()`、兼容原入口、解析单文件与批量参数 |
| builder.py | 按顺序组织读取、拓扑、Link、线形处理、核验和写出 |
| source.py | 读取 XODR，准备方向、车道组及有效源连接 |
| lanes.py | 车道类型、行驶方向、宽度中线、限速和渐变车道 |
| junction.py | connection/laneLink、连接组、掉头识别、转向合并和来源核验 |
| topology.py | 合并端口、生成 Node、统计度数和连通分量 |
| geometry.py | 点列、弧长、采样、切向、平滑和穿越判断 |
| endpoints.py | 端点接到 Node；掉头按源弧分摊节点位移 |
| coincidence.py / refinement.py | 合流贴线、拉直、普通转弯和联合邻线整理 |
| output.py | GPKG/CSV 文件保护、写出与构建摘要 |

底层算法模块不导入入口或构建模块；端点处理统计归属本次构建。调整 junction 规则时可直接查看 junction.py，调整线形时查看对应的几何模块。

## 3. Python 脚本如何调用和传参

### 3.1. 通用格式

```text
Python解释器 脚本路径 输入文件 -o 输出文件 --参数名 参数值 --开关
```

以自研转换为例：

```powershell
.venv/Scripts/python.exe code/own/xodr2gpkg.py data/Town06.xodr -o out/Town06.gpkg --epsilon 0.5 --no-z
```

| 命令部分 | 含义 |
|---|---|
| .venv/Scripts/python.exe | 使用哪个 Python |
| code/own/xodr2gpkg.py | 运行哪个脚本 |
| data/Town06.xodr | 位置参数：输入文件 |
| -o out/Town06.gpkg | 指定输出路径 |
| --epsilon 0.5 | 带值参数：采样步长 0.5 m |
| --no-z | 布尔开关：出现即启用，后面不跟 true/false |

路径含空格时用引号包住，例如 `"data/my map.xodr"`。多个图层用空格分隔：`--layers ReferenceLine Lane LaneCenter`；车道类型用逗号分隔：`--lane-types driving,onRamp,offRamp`。

每个脚本的参数以 `--help` 为准，例如：

```powershell
.venv/Scripts/python.exe code/own/xodr2gpkg.py --help
.venv/Scripts/python.exe code/roadnet/roadnet.py --help
.venv/Scripts/python.exe code/gdal/tin2poly.py --help
```

### 3.2. 自研 GIS 图层：xodr2gpkg.py

单文件转换：

```powershell
.venv/Scripts/python.exe code/own/xodr2gpkg.py data/Town06.xodr -o out/Town06.gpkg --epsilon 0.5 --max-turn 5
```

省略 -o 时写入 `out/<输入名>.gpkg`；省略 --layers 时生成八类有源图层。输入没有 signal/object 时，相应层可以省略。

只取部分图层，或批量处理 data/Town*.xodr：

```powershell
.venv/Scripts/python.exe code/own/xodr2gpkg.py data/Town06.xodr -o out/Town06_lines.gpkg --layers ReferenceLine LaneCenter JunctionLink
.venv/Scripts/python.exe code/own/xodr2gpkg.py --all
```

| 参数 | 默认 | 说明 |
|---|---|---|
| 输入 XODR | 必填，或使用 --all | 单个文件路径 |
| -o / --output | out/同名.gpkg | 单文件输出；不能与 --all 同用 |
| --epsilon | 1 m | 正数，控制参考线采样步长 |
| --max-turn | 5° | 弯道加密阈值；0 关闭该约束 |
| --layers | 八类全部 | 空格分隔图层名称，详见 --help |
| --no-z | 关闭 | 输出二维几何 |
| --crossfall | 关闭 | 简化超高补偿，边界见自研篇 |
| --all | 关闭 | 批量处理 data/Town*.xodr，不是所有任意文件 |

八类图层统一使用属性契约版本 2，字段数不含 fid/geom：

| 图层 | 属性字段数 | 主要内容 |
|---|---:|---|
| ReferenceLine / CenterLane | 各 27 | 道路、上下游、起点限速、分段 type/剖面和中心 lane 属性 |
| Lane | 37 | 车道段、连接、限速、宽度、标线、扩展和修复标记 |
| LaneBorder / LaneCenter | 各 36 | 与 Lane 相同的车道段关联和公共属性 |
| RoadSignal | 35 | 分类、朝向、动态状态、姿态、尺寸、值/单位、有效车道和引用 |
| RoadObject | 35 | 分类、姿态、尺寸、轮廓、重复实例、停车位和引用 |
| JunctionLink | 21 | 路口/connection/laneLink、车道重编号和声明/推导标识 |

完整字段按数据含义分组见[第 4.4 节](#44-自研八类图层完整字段)，实际 SQLite 列类型和缺失值见本地 `out/attribute_report.json`。分段、多条和自定义属性保留为 JSON；源未给出的可选值为 NULL，空数值列仍保持 REAL/INTEGER。文件级元数据保存完整解析后的 XML 属性来源。validate.py 会直接对照 XODR 校验字段、类型、属性值和嵌套记录：

```powershell
.venv/Scripts/python.exe code/own/validate.py data/Town06.xodr out/Town06.gpkg
```

读取输出属性和完整来源的示例：

```python
import json
import pyogrio

signals = pyogrio.read_dataframe("out/Town06.gpkg", layer="RoadSignal")
validity = json.loads(signals.iloc[0]["validity_json"])
info = pyogrio.read_info("out/Town06.gpkg", layer="ReferenceLine")
source_roads = json.loads(info["dataset_metadata"]["xodr_roads_json"])
```

### 3.3. 常规路网：roadnet.py

单文件和批量调用：

```powershell
.venv/Scripts/python.exe code/roadnet/roadnet.py data/Town06.xodr -o out/Town06_net.gpkg --step 1 --snap 0.05 --csv
.venv/Scripts/python.exe code/roadnet/roadnet.py --all --csv
```

省略 -o 时写入 `out/<输入名>_net.gpkg`，包含 Link/Node 两层。--csv 同时生成同名前缀的 _link.csv 和 _node.csv。

每条 Link 的数字化方向与行驶方向一致：首坐标对应 from_node，末坐标对应 to_node。双向道路分别输出两条按各自行驶方向排列坐标的 Link；GPKG/CSV 不再提供 dir 字段。显示方向箭头时直接使用线坐标顺序，构图时按 `from_node → to_node` 建边即可。

所有 Link 的 oneway=1。默认识别 type=bidirectional 和允许类型中的 direction=both：一条共享双向车道生成两条反向共线 Link；同一 road 的两条共享双向车道仍按方向聚合为两条主 Link，每条 lanes=2，不生成 4 条。shared_lane_ids 记录共享车道来源，校验允许其合法对向共线并拒绝同向重复边。显式 --lane-types 会替换普通车道允许类型集合；路肩掉头例外由下述独立规则和开关控制。

主路和辅道按各自车道组建立端口。全宽辅道有源连接声明时使用独立节点，连接路按 lane/link 或 junction/laneLink 接到对应组；收窄端保持主组连通。混合连接路必要时分组，因此启用 aux 后，某条 connecting road 可以输出多条不同端口组合的 connector。

无独立路径、且与同一 road 的主组共用起终节点的 aux，默认不写出。稳定主组的几何、车道数和限速保持原来的分组结果；省略辅道后，Link 编号、Node 入度和出度及 CSV 同步重算。有独立接点或转向的辅道继续保留，判断与前后对比见[路网篇第 5.2 节](docs/基于车道中心线和junction%20line生成常规路网图层.md#52-辅道两端都回到主组就只保留主组线)。`--no-split-taper` 会重新聚合整侧车道，可能改变主组位置，与默认省略重复 aux 的处理不同。

有效的 junction 掉头路径同样输出为单向 connector，连接进口节点与反向出口节点；多条同起止节点掉头按转向合并规则聚合。代码保留源回转弧，掉头不进入普通平滑、贴线、拉直或转弯拟合。写出前核对已明确源两端的有效掉头能从进口接到反向出口、节点独立且弧形有效。2025 版坪山 junction 190 当前保留 Link 164，导航链为 Link 76→164→36。

默认同时保留能按实际行驶方向接通的源路肩掉头：需要明确 junction/connection/laneLink、完整路肩轨迹和出口 lane/link，源进出口方向接近反向，且两端能映射到同侧同方向的已有通行车道组。路肩回转弧沿横向展开后接到该组节点，普通 road 不增加路肩或逆行方向。人行道、普通路肩连接、缺少源声明、出口逆行及同节点自环继续过滤。可加 `--no-shoulder-uturns` 关闭这条例外。OpenDRIVE 表达、提取流程与效果验收见[路网篇第 10 节](docs/基于车道中心线和junction%20line生成常规路网图层.md#10-掉头连线)。

连接路几何整理以源车道群和有向邻居为依据。端点过渡的原理见[路网篇第 6 节](docs/基于车道中心线和junction%20line生成常规路网图层.md#6-曲线连接)，直行拉直与转弯校正见[第 8 节](docs/基于车道中心线和junction%20line生成常规路网图层.md#8-直行鼓包与转弯凸起处理)。

| 参数 | 默认 | 说明 |
|---|---|---|
| --step | 1 m | 参考线采样步长 |
| --snap | 0.05 m | 端点吸附容差 |
| --min-len | 0.1 m | 最短 Link；增大可能丢失真实短连接 |
| --lane-types | driving,bidirectional | 逗号分隔允许的车道类型 |
| --keep-connectors | 关闭 | 保留无有效车道方向的 passable=0 占位线；独立节点不参与通行端口合并 |
| --no-shoulder-uturns | 关闭 | 关闭路肩掉头例外；默认只保留能按实际行驶方向接通的源路肩掉头 |
| --csv | 关闭 | 同时输出属性 CSV |
| --smooth | 1.5 m | 普通连接路平滑强度；0 关闭；受保护掉头不参与 |
| --no-merge-turns | 关闭 | 保留平行转向线 |
| --no-straighten | 关闭 | 禁用源共线连接路的直行拉直 |
| --no-refine-connectors | 关闭 | 禁用源形状偏移分摊与转弯鼓包校正 |
| --smooth-roads | 关闭 | 将平滑扩大到普通道路；默认只处理 connector |
| --fillet | 4 m | 端部圆角退距控制；0 关闭 |
| --merge-r | 12 m | S 形端部收口目标半径；0 关闭该链 |
| --no-lane-center | 关闭 | 使用含 laneOffset 的基准线；不拆辅道端口 |
| --lane-ramp | 0 m | 方向车道群端部向基准线收拢的范围 |
| --no-split-taper | 关闭 | 禁用渐变辅道及对应独立端口拆分 |
| --no-coincide / --no-mid-coincide | 关闭 | 禁用端部/中段贴线整理 |
| --no-soft | 关闭 | 禁用小偏移浅贝塞尔收口 |
| --all | 关闭 | 批量处理 data/Town*.xodr，不能与单输入或 -o 同用 |

输出均为二维。GPKG 的 Link CSV 不含 geometry；Node CSV 额外包含 x/y。图层字段完整清单见第 4 节。改变参数后应重新校验，不能沿用默认参数的验收结论。

关闭可选几何整理，检查基本拓扑与源线的示例：

```powershell
.venv/Scripts/python.exe code/roadnet/roadnet.py data/Town06.xodr -o out/Town06_basic_net.gpkg --no-merge-turns --smooth 0 --fillet 0 --merge-r 0 --no-coincide --no-mid-coincide --no-straighten --no-refine-connectors --no-soft
```

上述命令仍会将线首末坐标接到对应 Node；它不会关闭必要的端点处理。

也可以在自己的 Python 脚本或 IDE 中调用。下面的示例同样在项目根目录运行；IDE 已将 code 设为导入路径时，可省略 `sys.path.insert`：

```python
from pathlib import Path
import sys

sys.path.insert(0, str(Path("code").resolve()))
from roadnet import build_net

links, nodes = build_net(
    "data/Pingshan_20250606_OD.xodr",
    "out/Pingshan_20250606_OD_net.gpkg",
    step=1.0,
    snap=0.05,
    csv=True,
    verbose=False,
)

for movement in links.attrs["junction_movements"]:
    if movement["movement"] == "uturn":
        print(movement)
```

`build_net()` 会写文件，并返回 Link、Node 两张表。Python 使用关键字传参，例如 `--step 0.5` 对应 `step=0.5`，`--no-merge-turns` 对应 `merge_turns=False`，`--no-straighten` 对应 `straighten=False`，`--no-shoulder-uturns` 对应 `shoulder_uturns=False`。省略的参数沿用默认值。

`links.attrs["junction_movements"]` 保存源 connection、laneLink、合并后的 Link ID、进口/出口 Link 及连接状态。常见状态如下，具体连接还可查看 `source_ends_resolved` 和 `directionally_connected`：

| status | 含义 |
|---|---|
| connected | 进口→连接线→出口的有向路径已接通 |
| unresolved_source_ends | 源两端尚不能完整核实，未确认为已接通路径 |
| missing_source_road | 声明的连接 road 不在可解析源道路中 |
| filtered_lane_type | 该方向没有允许的车道组，未输出通行路径 |
| filtered_length_or_unmapped_group | 因最短长度或车道组映射未输出，需结合源数据和参数排查 |

多条源路径合并时仍各有一条来源记录；这些记录只在返回表的 attrs 中，不增加 GPKG/CSV 字段。完整车道几何和属性继续查看自研 JunctionLink。

`links.attrs["shoulder_uturns"]` 另记录路肩掉头的保留或过滤原因及目标端口。`retained` 表示保留，`already_allowed` 表示已被显式 --lane-types 纳入；`filtered` 配合 reason 区分无匹配行驶方向、车道轨迹不完整、缺出口、源形状不是回转弧或对向节点重合。原始 shoulder 类型保留在 XODR 和自研车道层，不改写为 driving。

### 3.4. 官方转换：xodr2gpkg_docker.py

```powershell
.venv/Scripts/python.exe code/gdal/check-drivers.py
.venv/Scripts/python.exe code/gdal/xodr2gpkg_docker.py data/Town06.xodr out_gdal/Town06.gpkg -oo DISSOLVE_TIN=YES -oo EPSILON=0.5
```

两个位置参数依次是输入 XODR 和可省略的输出 GPKG，默认写 `out_gdal/<输入名>.gpkg`。每个 GDAL 打开选项各带一个 -oo，KEY=VALUE 是一个完整参数。

`--image 镜像标签或digest` 指定 Docker 镜像；`--dry-run` 只显示将执行的命令。此 Python 脚本内部调用 Docker，实际转换要求 Docker 已运行且本地已有带 XODR 驱动的镜像，不自动下载。

默认镜像为 ghcr.io/osgeo/gdal:ubuntu-full-latest，也可通过 GDAL_DOCKER_IMAGE 环境变量设置。复现时使用已确认的标签或 digest。本机已安装 XODR 驱动时可直接调用 GDAL：

```text
ogr2ogr -f GPKG output.gpkg input.xodr -oo DISSOLVE_TIN=YES -oo EPSILON=0.5
```

--dry-run 示例不启动容器：

```powershell
.venv/Scripts/python.exe code/gdal/xodr2gpkg_docker.py data/Town06.xodr --image ghcr.io/osgeo/gdal:ubuntu-full-latest -oo EPSILON=0.5 --dry-run
```

### 3.5. 整理已有官方文件：tin2poly.py 与 mergelayers.py

单文件可选全部层或指定层：

```powershell
.venv/Scripts/python.exe code/gdal/tin2poly.py out_gdal/Town06.gpkg --all
.venv/Scripts/python.exe code/gdal/tin2poly.py out_gdal/Town06.gpkg Lane RoadMark -o out_gdal/Town06_surfaces.gpkg
```

默认输出同目录 `<输入名>_poly.gpkg`。`--all` 表示文件内全部图层；Lane、RoadMark 等层名是位置参数，不加 --layers。

批量整理和合并中心线是两次独立调用：

```powershell
.venv/Scripts/python.exe code/gdal/tin2poly.py --batch --all --dir out_gdal
.venv/Scripts/python.exe code/compare/mergelayers.py
```

--batch 选择目录内的原始 Town*.gpkg，跳过 _poly 等派生文件；--dir 省略时使用项目 out_gdal。批量不接受 -o。mergelayers 默认把 out/Town*.gpkg 的 CenterLane、LaneCenter、JunctionLink 并入同名 _poly 文件，需两套文件都已存在。

也可指定一对文件与要合并的层：

```powershell
.venv/Scripts/python.exe code/compare/mergelayers.py out/Town06.gpkg out_gdal/Town06_poly.gpkg -l LaneCenter JunctionLink
```

追加 `--dry` 可先检查并打印计划。整理过程不启动 Docker；官方六层降为二维，合并的自研线层保留来源 Z。

### 3.6. 可选路面展示：pavement.py

```powershell
.venv/Scripts/python.exe code/gdal/pavement.py out_gdal/Town06_poly.gpkg -l Lane -r 0.2
```

输入是一个或多个 GPKG；-l 指定来源面层，-r 指定非负缓冲半径（默认 0.2，平面单位），--dir 指定输出目录。默认写到输入同目录的 `<输入名>_pave.gpkg`，包含 Pavement 层。闭运算会修改窄缝和边界，只用于展示。

### 3.7. 校验、预览、比较与文章配图

| 脚本 | 调用参数 | 输出/行为 |
|---|---|---|
| own/validate.py | XODR GPKG，或 --all；部分导出须加同样的 --layers | 只读校验，失败返回非零 |
| own/preview.py | GPKG，或 --all；--dir 指定批量输入目录 | 同目录生成 _preview.png |
| diag/verify_net.py | 一个或多个路网 GPKG；--components 指定预期分量 | 不传文件时检查 out/Town*_net.gpkg |
| compare/compare_ab.py | --own-dir、--official-dir、-o 均可选 | 重算 Town 报告和两目录 stats.json |
| compare/compare.py | 官方 GPKG 自研 GPKG | 单文件对的比较简表 |
| diag/roadnet_article_figs.py | --output-dir 可选，默认 docs/figures | 重画路网文章示意图及归档对比图，并记录来源 SHA256 |

```powershell
.venv/Scripts/python.exe code/own/validate.py data/Town06.xodr out/Town06.gpkg
.venv/Scripts/python.exe code/own/validate.py data/Town06.xodr out/Town06_lines.gpkg --layers ReferenceLine LaneCenter JunctionLink
.venv/Scripts/python.exe code/own/preview.py out/Town06.gpkg
.venv/Scripts/python.exe code/diag/verify_net.py out/Pingshan_20250606_OD_net.gpkg --components 3
.venv/Scripts/python.exe code/compare/compare_ab.py --own-dir out --official-dir out_gdal -o COMPARE_GDAL.md
.venv/Scripts/python.exe code/compare/compare.py data/sample_gdal.gpkg out/sample_own.gpkg
```

路网文章配图的调用：

```powershell
.venv/Scripts/python.exe -X utf8 -B code/diag/roadnet_article_figs.py
.venv/Scripts/python.exe -X utf8 -B code/diag/roadnet_article_figs.py --output-dir out/roadnet_article_figures
```

该脚本没有位置参数，读取项目内的源 XODR、当前正式 GPKG 和归档 ZIP。`--output-dir` 指定图片输出目录；省略时更新 docs/figures 中由该脚本生成的图片，同时写入 roadnet-figure-sources.json。归档 GPKG 和未合并转向的对照版本在临时目录中读取或生成，结束后清理，不覆盖正式 GIS/路网结果。复现对比图需要保留对应 archive、data 和 out 文件。

## 4. 输入、输出和完整路网字段

### 4.1. 默认输入输出关系

| 操作 | 输入 | 默认输出 |
|---|---|---|
| 自研提取 | data/<名称>.xodr | out/<名称>.gpkg，八类有源 GIS 图层 |
| 路网生成 | data/<名称>.xodr | out/<名称>_net.gpkg，Node/Link |
| 路网 CSV | 与路网生成相同，增加 --csv | out/<名称>_net_link.csv、out/<名称>_net_node.csv |
| 官方驱动转换 | XODR | out_gdal/<名称>.gpkg，原始官方图层 |
| TIN 整理 | 官方 GPKG | 输入同目录的 <名称>_poly.gpkg |
| 图层合并 | 自研 GPKG＋对应 _poly.gpkg | 更新目标 _poly.gpkg，默认加入三类中心线 |
| 预览 | 自研 GPKG | 输入同目录的 <名称>_preview.png |
| Pavement | 面层所在 GPKG | 输入同目录的 <名称>_pave.gpkg |
| A/B 比较 | out/＋out_gdal/ | COMPARE_GDAL.md 和两目录 stats.json |

自研八类层的几何为 ReferenceLine/CenterLane/LaneBorder/LaneCenter/JunctionLink：LineStringZ，Lane：PolygonZ，RoadSignal/RoadObject：PointZ；--no-z 后改为二维。官方原始层通常包含 LineStringZ 和 TINZ，具体以 GPKG 元数据为准。

--all 在自研提取和路网脚本中只选择 data/Town*.xodr；TIN 整理的 --all 表示一个文件内所有图层，--batch 才表示多个文件。单文件调用可使用任意名称；含空格路径加引号。

### 4.2. Link：17 个属性字段

| 字段 | 含义 |
|---|---|
| link_id | 本次构建的唯一 Link ID |
| road_id | 保留的源 road ID |
| road_name | 源道路名称 |
| junction_id | 路口 ID，普通路通常为 −1 |
| link_type | road / connector / aux |
| road_type | 源 road 类型 |
| speed_kmh | 该方向与车道组的最小已知限速，未知为 NULL |
| shared_lane_ids | 本 Link 使用的共享双向车道 ID，逗号分隔 |
| from_node | 行驶起点节点，与首坐标一致 |
| to_node | 行驶终点节点，与末坐标一致 |
| lanes | 沿段同时有效的组内车道数最大值 |
| oneway | 固定为 1；反向通行通过另一条 Link 表达 |
| passable | 有效车道组为 1；显式保留的非通行占位线为 0 |
| lane_ids | 该组源车道 ID，逗号分隔；可包含跨段重编号 |
| snap_m | 源首末端到最终节点的最大迁移距离 |
| length | 最终二维几何长度，单位与平面坐标一致，已知数据为米 |
| n_merged | 同节点转向聚合时省略的连接线数量 |

字段数不含 GPKG 自动生成的 fid 和 geom。lane_ids 属于保留的代表线，不是全部被合并连接路的清单。不同组的混合连接可保留同一 road_id 的多条 connector。

### 4.3. Node：6 个属性字段

| 字段 | 含义 |
|---|---|
| node_id | 本次构建的唯一 Node ID |
| junction_id | 端口来源的 junction ID |
| degree | 入度＋出度 |
| n_in | 以此节点为终点的 Link 数量 |
| n_out | 以此节点为起点的 Link 数量 |
| spread | 合并前各源端口相对代表坐标的最大距离 |

Node CSV 额外包含 x、y；Link CSV 不含几何。Node/Link ID 在重新构建后可能变化，跨版本关联优先使用源 road、车道组和端口位置。机器报告提供相关旧新 ID 映射。

### 4.4. 自研八类图层完整字段

#### 4.4.1. 类型与缺失值

原始 ID、类型、朝向、动态状态和布尔声明用 TEXT；道路长度、s/t、尺寸、角度和统一后的 speed 用 REAL；车道号、段下标和修复/推导标记用 INTEGER。源缺失的可选属性保持 NULL，明确给出的空字符串保留为空字符串；不会为了填满表格猜测 country、orientation、尺寸或默认限速。

每层都有 source_attributes_json 和 source_children_json，分别保留对应 XML 节点的全部直接属性和嵌套记录。已有规范属性提供独立列，自定义属性及多条子记录通过 JSON 保留。数值声明非法或含 NaN/Inf 时转换报错，旧输出保留。

#### 4.4.2. ReferenceLine / CenterLane：各 27 个字段

| 类别 | 完整字段 | 含义 |
|---|---|---|
| 道路 | road_id、road_name、junction、road_type、length、road_rule | road_type 取起点生效类型；rule 为源声明 |
| 限速 | speed、speed_max、speed_unit、speed_source | 统一值与原始代表记录 |
| 上游 | predecessor_id、predecessor_type、predecessor_contact_point | road/link/predecessor |
| 下游 | successor_id、successor_type、successor_contact_point | road/link/successor |
| 分段与剖面 | road_types_json、road_link_json、lane_offsets_json、elevations_json、lateral_profile_json | 完整道路类型、连接和剖面记录 |
| 中心车道与引用 | center_lanes_json、signal_references_json、object_references_json | 各段 center lane 属性及本 road 的引用 |
| 扩展与来源 | user_data_json、source_attributes_json、source_children_json | 扩展属性及 road 来源；source_children 不重复 lanes/signals/objects |

CenterLane 仍表示 laneOffset 后的整条车道基准线。lane id=0 的逐段标线、连接和扩展信息保留在 center_lanes_json 中。

#### 4.4.3. Lane / LaneBorder / LaneCenter：公共 36 个字段

| 类别 | 完整字段 | 含义 |
|---|---|---|
| 道路关联 | road_id、road_name、junction、road_type、road_rule、road_length | road_type 取本车道段起点生效类型 |
| 段和车道 | lane_section、single_side、lane_id、side、lane_type、level、direction、s_start、s_end | 段标识、源车道属性及有效里程 |
| 限速 | speed、speed_max、speed_unit、speed_source | 本段起点限速；完整变化另存 JSON |
| 相邻车道 | predecessor_lane、successor_lane、lane_link_json | 源车道连接声明，不根据编号猜测 |
| 分段属性 | widths_json、borders_json、speeds_json、road_marks_json、heights_json、materials_json、visibilities_json、accesses_json、rules_json | 全部 width/border/speed/roadMark/height/material/visibility/access/rule 节点 |
| 段来源 | lane_section_attributes_json、lane_section_children_json | laneSection 直接属性及非 left/center/right 的扩展节点 |
| 扩展与车道来源 | user_data_json、source_attributes_json、source_children_json | 本 lane 全部直接属性与完整子树 |

Lane 额外包含 self_intersect（INTEGER，0/1），因此共 37 个属性字段。三层使用同一段关联键，LaneBorder 已补齐 road_name/junction/level/speed/s_start/s_end 等字段。

#### 4.4.4. RoadSignal：35 个字段

| 类别 | 完整字段 | 含义 |
|---|---|---|
| 关联 | road_id、road_name、junction、road_type、road_rule、road_length、signal_id、signal_index | 道路关联及 signal 来源下标 |
| 分类和状态 | name、type、subtype、country、country_revision、dynamic、orientation | countryRevision 映射为 country_revision；dynamic/orientation 保留原文 |
| 位置和姿态 | s、t、z_offset、h_offset、pitch、roll | s/t/zOffset 为米，姿态角为弧度；hOffset 是相对道路航向的偏角 |
| 尺寸和显示 | height、width、value、unit、text | value 按源值，单位由 unit 表示 |
| 关联子记录 | validity_json、dependencies_json、references_json、positions_road_json、positions_inertial_json、signal_references_json | 完整有效车道范围、依赖、位置声明及全文件对该 signal 的引用 |
| 扩展与来源 | user_data_json、source_attributes_json、source_children_json | signal 的扩展和完整来源 |

signalReference 没有另建点要素，以免与实体 signal 重复计数；signal_references_json 保留引用发生的 road、节点下标和全部属性。仅有引用而没有定义的记录仍可在文件级来源元数据中查到。信号几何继续按 s/t/zOffset 生成锚点，positionRoad/positionInertial 子记录保留属性，不改变当前锚点算法。

#### 4.4.5. RoadObject：35 个字段

| 类别 | 完整字段 | 含义 |
|---|---|---|
| 关联 | road_id、road_name、junction、road_type、road_rule、road_length、object_id、object_index | 道路关联及 object 来源下标 |
| 分类和状态 | name、type、subtype、dynamic、orientation | 源分类和状态声明 |
| 位置和姿态 | s、t、z_offset、hdg、pitch、roll | 位置为米，姿态为弧度；hdg 保留源相对航向 |
| 尺寸 | height、width、length、radius、valid_length | 保留源尺寸及 validLength，单位米 |
| 子记录 | validity_json、outlines_json、repeats_json、materials_json、parking_spaces_json、borders_json、markings_json、object_references_json | 轮廓角点、重复实例、材质、停车位、边界、标记及跨 road 引用 |
| 扩展与来源 | user_data_json、source_attributes_json、source_children_json | object 的扩展和完整来源 |

outlines_json 同时覆盖旧版 object/outline 与新版 object/outlines/outline，角点和子属性全部保留。保留轮廓和 repeat 属性不表示已经重建实体几何；RoadObject 仍是一个定义对应一个锚点。

#### 4.4.6. JunctionLink：21 个字段

| 类别 | 完整字段 | 含义 |
|---|---|---|
| 路口和连接 | junction_id、junction_name、junction_type、connection_id、connection_type、incoming_road、connecting_road、linked_road、contact_point | 路口及 connection 的源声明 |
| 转向车道 | from_lane、to_lane、lane_ids、lane_type、lane_link_index、is_inferred | 进口车道、连接路接触端车道、经过的车道编号及声明/推导标识 |
| 来源 | junction_attributes_json、junction_children_json、connection_attributes_json、connection_children_json、source_attributes_json、source_children_json | junction/connection/laneLink 全部属性；junction_children 不重复 connection |

junction_children_json 保留 controller、priority、surface、userData 等非 connection 子记录；connection_children_json 保存全部 laneLink 及扩展记录。

## 5. 检查输出与已知限制

### 5.1. 推荐验收调用

单文件转换后，使用同一源 XODR 和同一图层集合校验。路网默认检查 out/Town*_net.gpkg，其他文件可显式传入：

```powershell
.venv/Scripts/python.exe code/own/validate.py --all
.venv/Scripts/python.exe code/diag/verify_net.py
.venv/Scripts/python.exe code/diag/verify_net.py out/Pingshan_20250606_OD_net.gpkg --components 3
.venv/Scripts/python.exe code/diag/verify_net.py out/synth_net.gpkg --components 2
.venv/Scripts/python.exe code/test_regressions.py
```

verify_net.py 可接收多个位置参数；--dir 和 --glob 控制批量目录及匹配模式，--components 指定每个输入预期的弱连通分量。校验脚本只读，失败返回非零退出码。

回归测试使用临时文件，不覆盖正式结果。无需源地图的测试可直接运行；依赖 Town03、Town06、Town10HD、坪山或福田样例的案例，缺少对应文件时会显示 skipped，补齐 data/ 下的样例后自动执行。

批量预览和比较示例：

```powershell
.venv/Scripts/python.exe code/own/preview.py --all --dir out
.venv/Scripts/python.exe code/compare/compare_ab.py --own-dir out --official-dir out_gdal -o COMPARE_GDAL.md
.venv/Scripts/python.exe code/compare/overlay_ab.py
```

overlay_ab.py 读取默认 Town01/Town03，生成 out/AB_overlay.png。compare.py 的位置参数顺序为“官方文件 自研文件”。diag/end_hooks.py 可通过 --dir 和 --glob 扫描路网的接缝、曲率与交叉；这些是专题诊断，不能替代源通行关系检查。

### 5.2. 当前复核结果

最终复核（2026-11-20）：151 项回归通过；13 份临时重建路网与对应正式文件的几何、属性一致，14 套路网 CSV 与 GPKG 一致。13 份自研 GIS 共通过 790/794 项检查，其中八份 Town 为 504/504；福田和 2024 版坪山各有两项既有车道面检查失败。12 份既有路网通过独立检查；2024 版坪山两份路网仍有 8 处超过 25° 的折角，未计为通过。详细结果见本地 `out/final_review_report.json`。

151 项回归全部通过，包括掉头、路肩、渐变辅道、多条重复辅道和两种通行规则。此次更新福田、2025 版坪山的默认路网，并另存 2024 版坪山的简化结果；共省略 49 条重复 aux。保留下来的每条 Link 几何与源属性、Node 编号和坐标均逐项对照一致，有向节点对集合不变，节点度数及 CSV 已重算。

此前已验收的 12 份路网继续按下表的预期分量通过独立检查。2024 版坪山另存结果有两个弱连通分量，继承原来四条普通道路 Link 中段的 8 个超过 25° 的折角，整网折角检查仍未通过，详见本地专项报告。

此前的自研 GIS 综合结果为 729/731，八张 Town 为 504/504；所有源属性检查通过，报告覆盖 92 个图层、45,296 个要素。本次修改路网代码，这组 GIS 结果沿用既有报告。

| 数据 | Node | Link | 预期弱连通分量 |
|---|---:|---:|---:|
| Town01 | 88 | 124 | 1 |
| Town02 | 64 | 88 | 1 |
| Town03 | 204 | 289 | 1 |
| Town04 | 186 | 282 | 1 |
| Town05 | 182 | 306 | 1 |
| Town06 | 128 | 150 | 1 |
| Town07 | 199 | 278 | 1 |
| Town10HD | 74 | 98 | 1 |
| 坪山（2025） | 170 | 181 | 3 |
| 福田 | 683 | 814 | 1 |
| sample | 16 | 20 | 1 |
| synth | 4 | 2 | 2 |

2024 版坪山原文件被占用，简化结果保存为本地 `out/Pingshan_20240507_aux_simplified_net.gpkg`：Node 616、Link 717，较原来的 739 条省略 22 条重复 aux。旧文件继续保留。2025 版坪山省略 6 条，福田省略 21 条；Town06 的独立辅道 Link 19 和 Node 48 保持原样。

Town06 的 Road 15 辅道 Link 19 与 Road 814 的 Link 123（前次 Link 122）共用上方 Node 48，主组为 Node 47；原 Node 44 随构建重新编号。两组端点相距约 8.775 m。辅道分组曾使 Link 数量由 145 增至 149；本次再增加一条有效路肩掉头，总计 150 条。

Town03 前次对向端口修复保留：原 Node 249 分为 194/195，217 分为 173/172，145 分为 127/128，间距均为 3.5 m；Road 1426 的当前 Link 245（前次 Link 244）仍为 38.61 m 直线，已修复右转和 laneSection 边界保持正确。

Town10HD 的 Link 60（Road 255）已恢复为 38.11 m 两点直线，端部 1.332 m 鼓包消除；Town04 的同类 Link 264 恢复为 54.89 m 直线。保护逻辑核对本线与邻线的源轨迹，只允许恢复源中已有交叉。Town10HD Link 75 和 Town06 Link 142 原已共线，本次进一步简化为两点直线。三份路网的节点、拓扑、完整字段和其余属性均保持一致，CSV 同步核对。

Town10HD 的 Link 86（Road 736）通过联合邻线整理恢复为 46.15 m 两点直线，邻接 Link 90 同时调整转弯弧形；与 Link 88、90 均没有新增内部交叉。联合候选只在 --no-straighten 和 --no-refine-connectors 均未指定时执行，并保持修改前的逐对穿越上限。

模块拆分和掉头保护曾改变 2025 版坪山两条 Link 的几何。原 Link 170（Road 194，当前 Link 164）的掉头弧长由 28.912 m 变为 30.477 m，避免被普通平滑和贴线处理压窄；原 Link 171（Road 198，当前 Link 165）按既有整理规则同步调整，长 34.420 m。本次省略重复辅道时，两条线的几何和节点保持一致，只有顺序编号更新。

掉头覆盖扫描 12 份源数据的 238 个 junction、2,204 条 connection，按源进出口精确方向筛出 12 条返回候选。当前 5 条源路径可接入通行网，合为 4 条掉头 Link：2025 版坪山两条 driving 路径合为 Link 164；Town03 Road 1202→Link 225（Node 53→55，14.992 m）、Road 1688→Link 262（Node 90→103，15.523 m）；Town06 Road 719→Link 114（Node 97→100，18.552 m）。后三条源类型仍为 shoulder，通过定向例外保留。其余 7 条候选的机动车方向不匹配，继续过滤。

新增三条路肩掉头无内部穿越，Node 坐标和原有全部 Link 几何、源属性均不变；只有 Link 顺序编号、Node 度数和部分来源离散度随新增边更新。其他 10 份路网完全一致。120°～145° 的机动车急转也都已输出；145° 用于回转候选和弧形保护，不是普通连接线的输出门槛。

### 5.3. 需要单独解释的结果

福田源文件缺少 CRS，部分 junction 声明引用不存在的连接车道，完整 JunctionLink 导出会明确失败。目前正式 GIS 仅包含七类层，54/56 综合检查通过：14 条 LaneCenter 存在面内覆盖失败，7 处退化面封口与源横断面不符。属性完整不能替代这些几何检查。

福田另有 46 条机动车连接声明无法完整核实：其中引用源中不存在的 Road 3033、3517，或没有出口 road/link。它们不能算作掉头覆盖已完整确认。掉头专项目前以单条 connectingRoad 为单位；本组数据没有同一 junction 内的串联 connector，也没有 direct junction，尚不能凭这组结果证明此类输入已获掉头专项支持。

原始官方结果为已有产物，本次整理没有重新调用 Docker 或更换官方驱动版本。TIN 转二维后，竖直实体可成为空面；异常长线与面融合失败会单独报告。

自研 --crossfall 仅做简化超高补偿；RoadSignal/Object 是锚点；Lane 自交修复保留最大面片，可能损失其他部分。road 级弱连通不能证明变道、掉头或车辆权限完整。详细处理边界在三篇技术文章中说明。

### 5.4. 机器报告

| 文件 | 内容 |
|---|---|
| `out/final_review_report.json` | 最终复核：环境、回归、重建与正式结果对照、字段和 CSV、GIS 检查、已知限制及清理清单 |
| `out/validation_summary.json` | 综合验收与当前产物数量 |
| `out/attribute_report.json` | GIS 字段类型、NULL、源属性核对 |
| `out/connector_refinement_report.json` | 直行与转弯形状整理记录 |
| `out/lane_section_fix_report.json` | 车道段边界左右极限修复 |
| `out/node_merge_fix_report.json` | 过滤连接方向修复及该次 ID 映射 |
| `out/auxiliary_node_fix_report.json` | 辅道端口、混合连接分组及该次路网哈希 |
| `out/redundant_auxiliary_report.json` | 重复辅道删除、保留线逐字段及几何对照、Link 编号映射、另存结果与既有折角 |
| `out/source_crossing_fix_report.json` | 源交叉保护修复、12 份路网复核及该次产物哈希 |
| `out/junction_uturn_report.json` | 12 份 XODR 的掉头专项复核、源类型与合并结果、实际有向导航链 |
| `out/junction_uturn_coverage_report.json` | 独立复核掉头覆盖：精确源航向、逐车道路径、0.25 m 疑点采样、类型过滤和未能核实的源记录 |
| `out/joint_connector_fix_report.json` | 共节点直行与转弯联合整理、12 份复核及该次产物哈希 |
| `out/roadnet_modules_report.json` | 模块拆分与掉头保护、12 份逐字段/几何对照、源 connection 记录及该阶段路网哈希 |
| `out/shoulder_uturn_report.json` | 源路肩掉头定向例外、保留/过滤原因、三条新路径及 12 份对照、当前产物哈希 |
| [COMPARE_GDAL.md](COMPARE_GDAL.md) | 真实 GPKG 重算的 A/B 指标 |

这些 JSON 报告保留在本地，不随核心内容提交到 Git 仓库。修复报告保存各次修改时的哈希；最新路网以重复辅道报告及综合摘要为准。路肩掉头、模块拆分等报告保存对应阶段的产物。GIS 源文件、正式图层和官方原始结果按各自用途保留。

## 6. 文件保护与历史恢复

### 6.1. 写入保护

输出先写同目录临时文件，完成后才替换目标。被 QGIS 占用时关闭相关数据源后重试；不要先删除旧层或旧文件。GPKG 与两张 CSV 分别保护，不构成跨文件事务。

显式输入输出的相对路径以调用目录为准；默认目录按脚本位置定位项目根。脚本可迁移到其他机器，无需修改硬编码工作区路径。

### 6.2. 归档与恢复

修改前产物和清理过的历史实验保存在 archive/ 的 ZIP 中，均有完整性校验。主要归档包括首次整理、连接路形状、laneSection 边界、过滤端口节点、辅道节点和文章整理、源交叉保护、联合连接线整理，以及模块拆分和掉头弧形保护前的版本。

恢复时先解压到新目录，按清单选择文件，避免覆盖当前结果。详细内容和恢复说明见本地 `archive/README.md`；历史 ZIP 不随核心内容提交到 Git 仓库。
