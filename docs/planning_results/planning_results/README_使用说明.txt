planning_results — 三场景离线规划完整交接包

先看什么
1. 双击 planning_preview.html，可独立、完全离线打开，已内嵌最终JSON。
   支持三场景切换、六机筛选、上下层/扫描圆/分区/禁区/航线/编号、旋转缩放、俯视和时间播放。
   默认XYZ真实比例；Z夸张模式有明确标签。无需原竞赛程序、网络、服务端或安装依赖。
2. 阅读 planning_report.txt 的各机时间和覆盖缺口、候选比较、待确认条件。
3. 查看 exclusions_review.png 和 exclusions_notes.txt，审查两个决赛强禁扫候选区。
4. 原程序开发对话建议接收整个 planning_results.zip。

状态和使用边界
- 决赛1、决赛2：draft。两个真实强禁扫边界尚未确认，使用2017影像候选多边形。
- 许昌2：validated仅指输入固定分区、名义GPS及暂定参数下的离线几何/时间核验。
- 三场景均不是实飞指令或实飞许可。没有修改、连接、部署地面端/机载端/程序B。
- 真实每机起降GPS、暂定高度和2m/s下降必须后续实机核对；动态避碰和未知耗时未验证。
- 原单层任务和旧3D不能直接导入此多层交接格式，必须后续适配。

关键文件
planning_result.json：推荐候选，严格匹配 inputs/03_规划输出.schema.json。
planning_result.geojson：完整几何、独立航点、每段实际移动线及全部未覆盖区域。
waypoints.csv：每层唯一点；scan_visits.csv：实际扫描访次，A转折点的两次扫描完整保留。
mission_summary.csv：每机每层点数、面积、覆盖、冗余和机时。
validate_plan.py / validation_report.json：从最终文件独立复算，非仅Schema验证。
planner.py / planner_config.json：确定性规划器、持久化生成时间和完整参数快照。
exclusions_draft.json：人工像素圈定的明确固定输入，非已确认边界。
feasibility_bounds.json：求解前的理想面积上界、点数/时间下界。
candidate_comparison.json：两种预算下的分区、候选点规模及指标。
candidates/coverage_priority.json：推荐完整候选，每机目标≤1350s。
candidates/more_reserve.json：更多余量完整候选，每机目标≤1230s。
candidates/more_reserve_validation.json：备用候选独立JSON验证（无主CSV/GeoJSON交叉核验）。
boundary_sensitivity.json：候选边界±10m时固定当前点位的几何覆盖及违规圆盘数量。
reproducibility_report.json：重新运行规划器的关键输出逐文件SHA256一致性。
preview_validation_report.json / preview_evidence：实际Chromium离线检查、截图。
test_validator_tamper.py / validator_tamper_report.json：8种错误注入均应被独立验证器拒绝。
inputs/：用户提供的全部8份原始交接文件，原样保存。

本次实际验证环境
Python 3.8.10，numpy 1.24.4，Shapely 2.0.6，jsonschema 4.23.0，Pillow 10.4.0。
推荐在Python 3.10环境安装 requirements.txt 中锁定依赖以便跨平台重现。
开发者的临时依赖目录不属于本包，也不是运行脚本所需的固定路径。

复算命令（在解压目录打开终端；Windows可将python3替换为python）
python3 -m pip install -r requirements.txt
python3 planner.py
python3 validate_plan.py
python3 build_report.py
python3 build_preview.py
python3 test_validator_tamper.py

planner.py从本包inputs/02_规划输入.json及exclusions_draft.json重新优化全部候选，
使用相对脚本的默认路径；无需原项目仓库。planner_config.json保留首次生成时间，
相同依赖/输入/代码的重新运行会保留同一生成时间以便字节级比较。
算法参数由planner.py明确版本化，planner_config.json只是精确运行快照，不能仅编辑
快照来改算法；修改算法或边界后必须重跑规划、验证、报告和预览，并更新哈希。
若仅重导出已有候选可用 python3 planner.py --reuse-candidates。

重建候选边界审查图（可选，不必为使用规划结果运行）
python3 build_exclusions_review.py
此脚本保留手工像素顶点，按输入配准重建边界及审查图；这些候选仍须真实边界确认。

浏览器验证（可选，打开HTML本身不需任何依赖）
npm install --no-save playwright@1.48.2
npx playwright install chromium
node preview_browser_check.cjs
检查以file://且offline模式打开，测试切换/筛选/播放/缩放/JSON导入，输出实际截图及哈希。

几何/时间注意
局部坐标[北,西,上]米；waypoints_wgs84=[纬度,经度]；GeoJSON=[经度,纬度]。
GeoJSON三维轨迹的第三维为相对各机起点高度，不是WGS84椭球高/海拔。
A每机只属于固定一层，scan_passes=2，顺序P1…PN,PN…P1。
B/C每机upper→lower，每层scan_passes=1；层间先原位下降，再水平转移。
所有中间辅助点仅飞行，不触发扫描。六机各自1380s，返回到降落点上方，不含最后落地。
每层全场覆盖单独计算，不能把上下层相加，也不能因A重复扫描把覆盖率翻倍。

哈希和打包
SHA256SUMS.json与SHA256SUMS.txt包含输入、所有交付代码/数据/报告/截图的SHA256。
为避免自引用，两个哈希清单自身不列入清单，ZIP不放在自己的内部；每项规则写在JSON中。
python3 package_results.py 会核对原始输入7项校验，刷新交付哈希，在结果目录的上一级生成
planning_results.zip和planning_results.zip.sha256，并对压缩内容逐项复验。
ZIP解压根目录为planning_results/。打包排除__pycache__、.pyc、node_modules及本地测试缓存。
