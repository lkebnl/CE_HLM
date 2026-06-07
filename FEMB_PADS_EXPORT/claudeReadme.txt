你现在要帮我做一个 PADS 设计数据解析项目，目标是把 FEMB 原理图和 PCB 的导出文件整理成统一的数据结构，供后续查询、可视化和大模型问答使用。

项目背景：

* 这是一个基于 PADS Designer / PADS Layout 的 FEMB 设计数字孪生项目。
* 项目书里强调：不要只依赖 PDF，要优先用 PADS 导出的结构化文件。
* 当前已经导出的文件有：

  * 原理图：`.kyn`、`.edn`、`.eds`、`.cce`、`.pdf`、`.xlsx`、`.txt`、`.frs`
  * PCB：`.asc`
* 目前只有一个 PCB ASCII 文件可用，先以它作为第一版 PCB 主数据源。

你的任务：

1. 先写一个“原理图 netlist 解析器”

   * 输入：`keyin_netlist.kyn`
   * 输出：

     * `nets.json`
     * `pins.json`
     * `components_pin_index.json`
     * `netlist_summary.csv`
   * 解析规则：

     * `%page=` 表示页面编号
     * `\NET_NAME\` 是网络名
     * 后面的 `\REFDES\-\PIN\` 是连接点
     * `*` 表示同一个 net 的续行

2. 再写一个“PCB ASCII 解析器”

   * 输入：`io1865-1f.asc` 或整理后的 `pcb_layout_ascii.asc`
   * 输出建议：

     * `pcb_components.json`
     * `pcb_parts.json`
     * `pcb_decals.json`
     * `pcb_nets.json`
     * `pcb_routes.json`
     * `pcb_vias.json`
     * `board_outline.json`
     * `layers.json`
   * 重点解析：

     * `*PART*`：器件位置、旋转、side、RefDes
     * `*PARTDECAL*`：封装/footprint
     * `*VIA*`：via 信息
     * `*ROUTE*` / `*SIGNAL*`：走线和网络
     * `*LINES*`：板框和机械线
     * `*RULES*`：规则信息（如果有）

3. 再写一个统一的数据融合层

   * 以 `RefDes` 为主键，把原理图、BOM、PCB、封装信息合并成统一结构
   * 建议输出：

     * `components.json`
     * `nets.json`
     * `pins.json`
     * `pcb_components.json`
     * `routing.json`
     * `schematic_objects.json`
   * 先做到最小可用版本：

     * 输入 `U12`，能查到它的 pin/net
     * 输入 `GND`，能查到连接到哪些器件和 pin
     * 输入某个 RefDes，能查到 PCB 上的位置

代码要求：

* 用 Python 编写
* 结构清晰，模块化
* 提供命令行入口，方便直接运行
* 处理 PADS ASCII 里的续行、转义符和不完整字段
* 文件编码按 ASCII/UTF-8 兼容处理
* 输出文件统一放到 `parsed/` 目录
* 遇到无法解析的字段，不要崩溃，保留原始值并做标记
* 先做第一版能跑通的 MVP，不要一开始追求完美

项目目录建议：

* `FEMB_PADS_EXPORT/01_schematic/`
* `FEMB_PADS_EXPORT/02_pcb/`
* `FEMB_PADS_EXPORT/03_netlist/`
* `parsed/`

请先输出：

1. 你对文件格式的理解
2. 你准备怎么解析 `.kyn`
3. 你准备怎么解析 `.asc`
4. 项目文件结构
5. 第一版代码实现
6. 如何运行
7. 如何验证输出是否正确

如果需要，你可以先只实现 `.kyn` 解析器，再实现 `.asc` 解析器。
