科目二 UAV3 程序B接口核对资料
来源提交：f6a3fcf178a4a3511dbbd983993bf689c6f57b89
导出日期：2026-10-15（Asia/Shanghai）

先打开 uav3_program_b_review.html，内含完整原文；或者读取 uav3_program_b_messages.json。
ground_to_onboard_assignment.json 与 ground_to_onboard_takeoff.json 是地面到机载的完整附录，不是直接发给B的控制消息。
publisher_reference.py.txt 是当前接口函数原文摘录；SHA256SUMS.txt 用于核对包内文件。

1. 本资料是固定规划与既有离线模拟任务的接口核对样本，不是真实飞行日志。导出只调用当前发布函数并捕获假ROS消息。

2. 样例全局UAV编号G=3、本机ROS编号L=3（P600示例）。SU17等绑定可能L=1；区域、航线、降落点、external_mission和return_home话题用L，类别/计时/任务状态/成功返航话题用G。JSON中的uav_id仍为全局编号3。

3. 所有std_msgs/String消息的data是JSON字符串。为了阅读，本文件事件data已解析；ros_message保留实际String.data字符串。MultiArray包含默认空layout；不能把包装对象整体当作ROS String.data发送。

4. 区域外环是16个不同顶点加重复首点闭合，共17项，非凸、无孔洞；不能替换成包围盒或凸包，否则会重新与UAV5重叠。

5. 坐标为WGS84。区域顶点为[经度,纬度]；path_stage_1、entry_path、return_paths和jiangluodian为[经度,纬度,相对高度米]。高度53米相对UAV3自己的起飞点，不是海拔。

6. path_stage_1只有6个点、18个数。B按已确认逻辑执行1→2→3→4→5→6→6→5→4→3→2→1，每次扫描30秒，转折点6共60秒。当前external_mission不含scan_passes或scan_execution_order；两趟需要B已有行为，不能把6点列表当作只执行一次的完整任务。

7. waypoint_actions.index从0开始（0至5）；return_paths.routes[].waypoint_index从1开始。return_paths用(source_uav_id,waypoint_index)联合定位。

8. return_paths为六机5/6/6/6/6/7个点共36条路线，全部返回接收机UAV3自己的降落点。UAV3正常两趟结束在本机第1点，应选择source_uav_id=3、waypoint_index=1。不是从第6点直接返场，也不是返回源飞机的起降点。

9. entry_path是到第1个扫描航点的进场折线。折线中的中间顶点是通行点，不是额外扫描航点；return_paths同理。

10. 当前样例external_mission.speed_mps=0.3，来自测试节点search_speed。正式值来自机载配置，初始化还受max_speed限制。场景规划按5m/s水平、6m/s爬升估时，但这两个速度不通过本次B控制消息强制设置；请作者核对B是否使用speed_mps。

11. 当前样例deadline_at=1792044639.7734976为Unix秒，是既有起飞命令中的示例截止时刻。840秒仅为规划预算，并未下发为B强制超时；return_battery_threshold=0.3代表30%。

12. 55米扫描半径、840秒规划上限、6m/s爬升、scan_passes=2等在地面到机载assign_task中存在，但当前external_mission未透传这些字段。禁止把附录assign_task误认为B实际收到了全部规划字段。

13. 航点和子区边界来自固定规划；实际进返场与降落点根据各机本次开机首次有效未解锁GPS重接。这里的UAV3模拟降落点为纬度33.86450507756048、经度113.70563，不是现场实际位置。

14. mission_id、assignment_checksum、plan_sha256、截止时间、比赛计时快照、状态时间和缓存路径是样例值；真实每次分派按本次任务核对。部分不带任务号的数组/启动话题是原接口，请B协调同批次带编号JSON的接收。所有列出发布器均锁存；跨话题实际收到的先后不能只依赖发布调用顺序。

15. 本样例识别类别为空数组，代表本次离线任务未选类别；正式内容来自页面选项。competition_time承接已有比赛计时快照，可能running=true；规划或起飞不等价于重新从零计时。

16. successful_return初始发布{}，是未有成功返航事件，不是成功。后续状态由原只读监测或B回报生成。return_home的Bool=true只在收到返航请求时发布，分派和起飞交接阶段不会顺带发送。

17. 子区域无面积重叠不代表扫描圆或所有航段不能跨子区分界；原规划允许扫描圆边缘交叠。进场分离、赛方边界5米安全距离和禁扫区约束已保留。

18. 任务交接与返航请求的消息原文在本资料中完整列出，包括36条返航线；计时和task_status是动态辅助话题，本资料给出接受任务及起飞交接时快照，不伪造后续飞行遥测序列。

六个固定扫描航点
序号,经度,纬度,相对起飞高度(m)
1,113.70431208229463,33.862600605591204,53
2,113.70479840224931,33.86327676899837,53
3,113.70600468711197,33.86351187095896,53
4,113.70671795637882,33.863809382858115,53
5,113.70671795637882,33.862916847160655,53
6,113.7088694477029,33.866247418328506,53

消息发布调用顺序

接受规划与分派后
1. /uav3/competition/recognition_categories  [std_msgs/Int32MultiArray]
2. /ground_mission_planner/vehicle_3/task_region  [std_msgs/String]
3. /uav3/competition/competition_time  [std_msgs/String]
4. /uav3/competition/successful_return  [std_msgs/String]
5. /uav3/competition/task_status  [std_msgs/String]

起飞成功后交给B
1. /ground_mission_planner/vehicle_3/entry_path  [std_msgs/String]
2. /ground_mission_planner/vehicle_3/return_paths  [std_msgs/String]
3. /ground_mission_planner/vehicle_3/path_stage_1  [std_msgs/Float64MultiArray]
4. /ground_mission_planner/vehicle_3/jiangluodian  [std_msgs/Float64MultiArray]
5. /uav3/competition/external_mission  [std_msgs/String]
6. /ground_mission_planner/recon_start_mode  [std_msgs/Int32]
7. /uav3/competition/task_status  [std_msgs/String]
8. /uav3/competition/task_status  [std_msgs/String]

条件分支：之后收到人工返航请求（不在起飞交接时发布）
1. /ground_mission_planner/vehicle_3/return_home  [std_msgs/Bool]
2. /uav3/competition/task_status  [std_msgs/String]
