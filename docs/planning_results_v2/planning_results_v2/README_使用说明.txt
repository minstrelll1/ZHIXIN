planning_results_v2 — 按2026-10-17修改方案重新优化的三场景交接包

快速使用
1. 双击 planning_preview.html：独立离线3D预览，内嵌最终JSON及返航目录，不需网络或原项目。
   可切换三场景、六机、上下层，旋转缩放、俯视、时间播放；“查看返航”可观察回升和接入。
2. 阅读 planning_report.txt 和 revision_diff.json/CSV：完整方案、逐项v1-v2差异与未覆盖缺口。
3. 审查 exclusions_review.png、terrain_mask_review.png：新树林连接与旧地类疑点分别显示。
4. 建议把整个 planning_results_v2.zip 交还原竞赛程序开发对话，而不只交JSON或截图。

本轮变化
- 三场景物理upper半径50m、lower半径30m，按新参数重新求解点数、点位、点序和分区候选。
- 新树林以8个用户点与原场界编号8/9/10推导补点整体替换旧大地块，池塘仍为2017影像草案。
- 实际发送扫描数组逐段直接连接均需满足5m约束，不依赖未下发的辅助拐点。
- 双层任务下层结束先原位升到本机upper，再直连选定upper点接入返航目录；回升和接入全计时。
- 返航回升2m/s只是本轮规划暂估，非用户确认；另给1/2/3m/s敏感性。
- 逐次删除零贡献扫描点、重排、复算，再利用释放预算补点最多3轮并再次删除；不一次性批量删。

状态
主JSON三场景保持draft。几何与名义参数核验、现有话题映射核验、实际飞行条件分别报告。
所有实际数组直连与目录几何检查通过，并不代表当前程序已导入、ROS已运行或程序B已实飞验证。
池塘边界、新树林Q连接、局部历史森林归属、真实六机GPS、暂定高度/下降和返航回升参数仍需核对。
本包没有修改、连接、部署或控制地面端、机载端、外部程序B，没有发送任何飞行指令。

核心交付
planning_result.json / inputs/03_规划输出.schema.json：主结果保持原严格schema，ros_topics=null。
planning_result.geojson：完整外界/子区/豁免/强禁扫/禁飞/点/逐段轨迹/未覆盖几何。
waypoints.csv：每层唯一点；scan_visits.csv：实际访次，A末点双扫描完整保留。
planner.py / planner_config.json：确定性求解、统一参数与可复现生成时间。
inputs/02_规划输入_v2.json / merge_inputs.py：新版合并输入及构建代码，使用新source_inputs_sha256。
inputs/v1/：原始输入、原结果及原禁扫候选作版本记录；inputs/revision/：修改意见包原样文件。
exclusions_draft.json / exclusions_notes.txt：新树林用户点与推导补点、池塘来源和状态。
terrain_mask_audit.json：主保留旧地类和局部修正敏感性两套完整几何；不删无关森林、水体。
terrain_sensitivity_results.json：固定新航点在两种mask下的精确覆盖，非另一份重优化方案。
return_catalog.json / return_catalog.schema.json：每场景每recipient的完整返航目录。
return_selections.json：正常结束所选来源机、1起索引、点ID、回升、接入及完整路线。
topic_mapping.json / migration_notes.txt：现有已知话题名、实际展平数组、单/双层适配边界。
return_climb_sensitivity.json：1/2/3m/s回升时间、完整总时间、余量和1380秒判断。
validation_report.json：独立主Schema、几何、扫描数组直连、计时、冗余、CSV/GeoJSON核验。
return_catalog_validation_report.json：目录条数、起终点、recipient高度、全线及topic映射独立核验。
validator_tamper_report.json / return_catalog_negative_tests.json：主方案与目录的负例检测证据。
candidates/：coverage_priority和more_reserve两份完整候选、返航选择与冗余审计；后者另有独立JSON验证。
candidate_comparison.json / feasibility_bounds.json：候选比较、求解前的面积上界和时间下界。
redundancy_audit.json：每次删点前后机时、准确贡献面积、增补轮次及最终逐点贡献。
reproducibility_report.json：实际全量再优化及目录/预览重建的19文件SHA256比较。
input_reproducibility_report.json：仅以原始来源重新合并新输入/Q补点/mask审计的4文件一致性。
preview_validation_report.json / preview_evidence/：断网file://交互测试与实际截图。

接口要点
A物理upper/lower各机都把自己的唯一层放到path_stage_1，path_stage_2=[]，B正逆两趟。
不要把UAV4～6的唯一物理lower只放stage2，不要生成只有一层的task.layers数组。
B/C upper→lower各一趟；各层发送唯一点数组，不把execution_order展开后再重复。
主schema无需扩展；真实话题信息放独立topic_mapping.json，不往ros_topics:null字段塞字典。
返航目录的离线catalog_schema_version=1与运行话题schema_version=2不是同一种版本。
A目录适配仍需原程序实现明确legacy映射；主离线文件不声称已能被地面端直接导入。
真实mission_id、assignment_checksum由未来分派程序生成，不在本包制造。

复算（Python3.8～3.11；本次实际Python3.8.10。Windows可用python替代python3）
python3 -m pip install -r requirements.txt
python3 planner.py
python3 build_return_catalog.py
python3 validate_plan.py
python3 validate_return_catalog.py
python3 build_report.py
python3 build_preview.py
python3 test_validator_tamper.py
python3 test_return_catalog_tamper.py
python3 reproduce.py

脚本默认路径相对脚本所在目录，不需要原Prometheus仓库或临时依赖目录。
如需从旧输入和修改包重新合并，可先运行 python3 merge_inputs.py；边界审查图可由
python3 build_exclusions_review_v2.py 重建。手工来源及Q推导均已显式保存，不依赖隐藏状态。
planner_config.json的生成时间被持久化，便于同依赖同输入的字节级复现。参数由新版input
和planner.py中的算法设置共同确定，不通过修改快照伪造算法行为。

可选浏览器测试（打开预览本身不需要安装这些）
npm install --no-save playwright@1.48.2
npx playwright install chromium
node preview_browser_check.cjs

若选用更多时间余量候选，需将该候选JSON与对应return_selections一起作为输入重建返航目录、
映射、GeoJSON/CSV及预览后再核验；不得将备用扫描点与主方案目录混用。

坐标与单位
WGS84，经纬度不是GCJ02/BD09。局部[北,西,上]米；waypoints_wgs84为[纬度,经度]；
GeoJSON、B数组为[经度,纬度,相对高度]，Z不是海拔。所有面积/距离在固定原投影米制平面计算。
圆每四分圆128段；A正逆不增加几何覆盖，上下层不相加冒充一层100%。
每机1380s，规划尽量≤1350s。双层结束于upper高度的home上方，A结束于自身固定层高度，不含落地下降。

哈希与打包
python3 package_results.py 核对全部原始输入和修改包来源哈希，生成SHA256SUMS.json/txt，
再在结果目录上一级创建planning_results_v2.zip及planning_results_v2.zip.sha256。
ZIP内目录为planning_results_v2/；压缩内容逐项校验；哈希清单自身及外部ZIP不参与自引用。
排除__pycache__、.pyc、node_modules、虚拟环境。原planning_results.zip和旧结果目录保留。
