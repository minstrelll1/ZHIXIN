科目二六机不同高度：程序B全流程核对包
2026-10-15T15:15:41.975878+08:00

先打开index.html；每架完整内容在uavN_messages.json，总文件all_six_uav_messages.json。
本次只导出资料，无实机分派，无起飞，不改执行逻辑或接口。

UAV1: 高度59.0m；规划半径61.0m；5个点；36条返场线
UAV2: 高度56.0m；规划半径58.0m；6个点；36条返场线
UAV3: 高度53.0m；规划半径55.0m；6个点；36条返场线
UAV4: 高度50.0m；规划半径52.0m；6个点；36条返场线
UAV5: 高度47.0m；规划半径50.0m；6个点；36条返场线
UAV6: 高度44.0m；规划半径45.0m；7个点；36条返场线

1. 当前网页支持科目二比赛使用场景(subject2_actual)与六机不同高度(around54m)。选场景时页面自动选择科目二和around54m；GPS与操场中央固定。控制模式为external。

2. 这是当前源码及固定规划的完整离线接口样例，不是真实分派或飞行抓包。六机起降点使用操场中央附近间隔10米的模拟点；绝不能将这些示例降落点当作现场实际降落点。

3. 六架P600样例的全局编号G与本机ROS编号L均为1至6。每架独立ROS环境中的公共/ground_mission_planner/recon_start_mode不能合并为一个共享控制总线。自定义ROS话题覆盖、机载已部署版本及B实际订阅仍须以现场为准。

4. 每个uavN_messages.json含所有阶段的消息、ROS类型、话题、调用顺序和ros_message原文；data为便于阅读的解析值。std_msgs/String的实际data是JSON字符串；MultiArray的layout为dim=[]、data_offset=0。

5. 所有列出的ROS发布器均latch=true；task_status队列10，其余1。调用顺序不保证跨话题接收顺序，B应按mission_id和assignment_checksum配对JSON；数组和启动信号本身没有任务号。

6. 规划与分派阶段发布识别类别、task_region、比赛计时、successful_return={}及task_status。此时不会发布扫描航点/进返场航线/启动B信号。相同分派重传只重发计时、区域和task_received。

7. 点击一键起飞先经地面预检/确认，将takeoff发到机载执行器。机载成功起飞后依次发布entry_path、return_paths、path_stage_1、jiangluodian、external_mission、recon_start_mode=0。起飞失败不会交给B执行。不是按下按钮便立刻发送航点。

8. 坐标为WGS84；task_region顶点[经度,纬度]，三个路径接口及jiangluodian为[经度,纬度,相对起飞高度米]。external_mission航点使用latitude/longitude/altitude_m字段。高度不是绝对海拔。地面assign_task中的waypoints_wgs84则是[纬度,经度]，不要混用。

9. 六机高度59/56/53/50/47/44m，对应规划扫描半径61/58/55/52/50/45m，航点数5/6/6/6/6/7。地面规划的半径不会通过当前external_mission自动设置B定位器半径。

10. path_stage_1仅包含单趟航点。按用户已确认的B行为执行1→…→N→N→…→1，每次访问扫描30秒，N处共60秒。external_mission不含scan_passes或scan_execution_order；不能据此误认为收到两趟展开航点或自动设置了两趟模式。

11. waypoint_actions.index从0开始；return_paths.routes[].waypoint_index从1开始。每架接收36条返场线，覆盖六架的全部扫描点，所有路线终点均为接收机自己的降落点。正常两趟结束选择(source_uav_id=本机编号,waypoint_index=1)。

12. entry_path/return_paths的中间折线顶点仅用于通行，不是额外扫描点。固定扫描点和子区边界不会因任务发布者位置平移；进返场线、降落点在分派时根据各机本次开机首次有效未解锁GPS重接并验证。

13. 六个任务子区无面积重叠；边界可能相接。UAV3子区为非凸多边形，必须保留完整顶点与孔洞语义，不可取凸包或包围盒。扫描圆边缘重叠与子区面积重叠是两件事。

14. 速度重要差异：场景估时用水平5m/s、爬升6m/s；task未设置speed_mps，当前external_mission回退到执行器search_speed。默认launch为0.30m/s，启动时又受max_speed限制；本包实际捕获值0.3。未读六架现场ROS参数，不能认定实机覆盖值。B作者应核对是否使用该字段，不能将规划估时速度当作已下发飞行速度。

15. 840秒是科目二规划预算（爬升+两趟扫描/航行+返回降落点上方），不是external_mission强制超时字段。当前科目二通用任务时长默认1140秒，deadline_at继承takeoff命令。本包附录保留实际离线测试生成的截止时刻，不能用它作为现场时限。

16. return_battery_threshold=0.3是30%；B是否按该阈值及deadline_at返航取决于B实现。扫描半径、6m/s爬升、840秒预算、两趟说明在地面assign_task规划元数据内，不代表B的external_mission收到这些字段。

17. mission_id、assignment_checksum、plan_sha256、比赛计时、deadline_at、事件ID和状态时间都是本次离线样例值。识别类别样例[]代表未选类别；现场从页面选择生成。完整地面assign_task/takeoff附录不是直接发给B的消息。

18. competition_time在接受任务后及执行期间约每秒发布，继承全队比赛时钟，不等于本机起飞后时间。包中只给快照，不能预先列出未来所有动态消息。task_status按事件发布；异常/拒绝/人工接管状态也可能出现，详见发布函数摘录。

19. return_home Bool=true只在执行器收到返航请求时发布；正常两趟结束的自动返场由B已有逻辑负责，不保证会再收到Bool。返航请求只发信号，不重新发布整包航点。

20. successful_return初始{}不是成功；后续事件表示已到返航点并开始下降，不等于已落地。这里通过B携带当前mission_id和assignment_checksum的return_descent反馈捕获该分支；另外已有只读Land+飞控降落状态监测也可生成该事件。

21. 外部模式逐点飞行、扫描与降落由B执行。竞赛执行器不会再逐航点发送独立扫描指令。图像启动消息属于图像程序，Prometheus起飞命令属于飞控，均不是B任务接口，未混入B消息列表。

22. 当前工作区onboard_programs.json已有其他修改：flight启动命令含flight_mode:=outdoor_small_range、reconnaissance_mode:=2、sweep_yaw_speed_deg_s:=-20。它是B启动配置，不是选择科目二后自动下发的ROS消息；本次不更改此配置。现场已运行模式可能与文件不同，检查快照详见live_readonly_check.json。
