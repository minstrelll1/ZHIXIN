# 程序 B 的 ROS 任务接口

地面端选择任务区域、GPS 或 XYZ 坐标系、飞行高度方案，以及“外部自主程序B（比赛）”，再点击规划与分派。GPS 模式使用遥测经纬度生成航线；GPS 与 XYZ 模式均使用分配的相对起飞点高度，不叠加 GPS 海拔。机载任务执行器负责接收、校验和落盘完整任务，先升到分配高度，稳定到达后发布程序 B 的固定 ROS 接口。

```text
/ground_mission_planner/vehicle_1/path_stage_1
```

> SU17 每台机载机的厂家 ROS 命名空间均为 `/uav1`。`uav_id` 字段才是竞赛中的逻辑编号，例如 UAV3。

该话题实际类型为 `std_msgs/Float64MultiArray`，`data` 按连续三元组排列：`[经度1, 纬度1, 相对高度1, 经度2, 纬度2, 相对高度2, ...]`。例如：`data: [103.8610, 30.7852, 0.5, 103.8611, 30.7853, 0.5]`。

降落点通过另一个 `std_msgs/Float64MultiArray` 话题发布，`data` 只包含 `[经度, 纬度, 任务相对高度]`：

```text
/ground_mission_planner/vehicle_1/jiangluodian
```

到达分配高度后，机载端先发布路径、降落点和 JSON 任务，最后在以下话题发布 `std_msgs/Int32` 启动模式。按程序 B 的协议，`0` 表示正常启动，`1` 表示异常启动；当前正常任务固定发布 `0`。程序 B 应先读取同一任务的 JSON，再根据启动模式决定如何处理，并使用 `mission_id` 和 `assignment_checksum` 去重：

```text
/ground_mission_planner/recon_start_mode
```

兼容保留的 JSON 任务说明如下：

```json
{
  "type": "external_mission",
  "uav_id": 3,
  "mission_id": "subject1-xxxx",
  "assignment_checksum": "...",
  "flight_profile": "competition",
  "coordinate_frame": "WGS84",
  "altitude_frame": "RELATIVE_TO_TAKEOFF",
  "target_altitude_m": 0.5,
  "relative_altitude_m": 0.5,
  "waypoints": [
    {"latitude": 30.7852, "longitude": 103.8610, "altitude_m": 0.5}
  ],
  
  "return_home": {"latitude": 30.7851, "longitude": 103.8609, "altitude_m": 0.5},
  "deadline_at": 1780000000.0,
  "return_battery_threshold": 0.25
}
```

实验室 XYZ 任务的 `coordinate_frame` 为 `ENU`。此时 `path_stage_1` 的连续三元组是 `[x米, y米, z米, ...]`，`jiangluodian` 是 `[x米, y米, z米]`；JSON 的 `waypoints` 使用 `x_m`、`y_m`、`z_m`。比赛 GPS 任务的 `coordinate_frame` 为 `WGS84`，兼容话题使用 `[经度, 纬度, 相对起飞点高度, ...]`。降落顺序不再通过 ROS 话题发送。内部任务的 `waypoints_wgs84` 仍按 `[纬度, 经度]` 存储；只有发给程序 B 的这两个数组话题按经度在前排列。JSON 继续使用具名字段 `latitude`、`longitude` 和 `altitude_m`，不按字段排列顺序解释坐标。

`altitude_frame` 固定为 `RELATIVE_TO_TAKEOFF`；`target_altitude_m`、`relative_altitude_m`、GPS 航点和 `return_home` 的 `altitude_m` 均为分配的相对起飞点高度。例如 UAV1 选择约 1 米方案时，这些值及两个数组话题的第三个值均为 `0.5`，无论 GPS 海拔是多少。经纬度仍为 WGS84。“约 2 米”方案的 UAV1～6 高度依次为 `1.5、2.0、2.5、1.5、2.0、2.5` 米，GPS 和 XYZ 模式使用同一组相对高度。旧任务中附带的 GPS 航点海拔不会覆盖选定高度；程序 B 需按这一相对高度协议读取。程序 B 收到任务后独占向 Prometheus 飞控发布后续航点、返航和降落指令，本工程不会再发布搜索阶段的 Prometheus 移动指令。

竞赛 GPS 一键起飞以无人机未解锁时持续记录、解锁后冻结的地面本地 Z 为起飞高度基准；如果机载任务程序启动时飞机已在空中且没有记录，使用飞控 `rel_alt` 从当前本地 Z 反推地面 Z。点击起飞时的空中位置不会重新成为起飞原点。本地 Z 可以带非零零点偏移，例如地面本地 Z 为 `0.4` 米时，任务相对高度 `1.5` 米对应本地 Z 目标 `1.9` 米；程序 B 两个 GPS 数组话题的第三个值仍为 `1.5` 米。进入厂商 `command_control` 后的初始悬停由厂商 `Init_Pos_Hover` 参数决定，此行为与竞赛任务选定高度分别处理。

更新机载程序后，缺少地面高度基准的旧 GPS 任务缓存不会自动恢复；需重新规划并分派任务。新缓存只在同次开机且保存地面基准时自动恢复。

当前界面规划的局部坐标约定为：`+X` 向地图上方（北），`+Y` 向地图左侧（西）。机载端按该约定转换为 WGS84：纬度随 `+X` 增大，经度随 `+Y` 增大而减小。正式比赛前必须用已知坐标点核验该方向与程序 B 的坐标约定一致。

程序 B 应向下列话题回报状态，以便地面界面显示实际任务阶段：

```text
/uav1/competition/external_status
```

同样使用 `std_msgs/String` JSON。例如：

```json
{"uav_id":3,"mission_id":"subject1-xxxx","phase":"executing","next_waypoint":4}
```

可用的 `phase`：`executing`、`completed`、`returning`、`landing`、`landed`。其中 `completed` 表示搜索航点完成；程序 B 随后应自行返航和降落。重启后，机载任务程序会重新发布尚未结束的外部任务；程序 B 应按相同 `mission_id` 和 `assignment_checksum` 去重，而不是从第一航点重复执行。

人工点击地面端“一键返航”时，机载执行器不会再向 Prometheus 发布返航移动指令，而是在对应 UAV 的以下话题发布一次 `std_msgs/Bool`，`data: true`。程序 B 只需订阅这个话题并负责返航、降落：

```text
/ground_mission_planner/vehicle_N/return_home
```

GPS 实飞任务的 `jiangluodian` 使用规划分派前获取并锁定的本机 GPS，发布 `[本机起飞点经度, 本机起飞点纬度, 本机任务相对高度]`。XYZ 固定方案使用选定出发点 `[X, Y, 本机任务相对高度]`；旧任务未附带降落点时兼容 `[0, 0, 本机任务相对高度]`。该点是返航点，最终下降、降落由程序 B 执行，第三个值不是触地高度。

其中 `N` 为机载 ROS 编号，例如 UAV1 使用 `/ground_mission_planner/vehicle_1/return_home`。该消息不附带任务编号或其他字段；程序 B 可按当前任务上下文处理重复的 `true` 消息。

## 全机队航点的返航路线

`/ground_mission_planner/vehicle_N/entry_path` 和 `/ground_mission_planner/vehicle_N/return_paths` 均为锁存的 `std_msgs/String` JSON，在成功升到任务高度后发布。

进场消息保持版本 1，终点为本机首个侦察点。返航消息升级为 `schema_version=2`：每架接收机收到六机全部规划航点的返航路线，**每条终点均为接收机自身的 `jiangluodian`，所有路线高度均为接收机任务高度**。`uav_id` 表示接收机；每项 `routes` 新增 `source_uav_id`，必须和来源机内从 1 开始的 `waypoint_index` 一起选择路线，不能只用数组下标。`landing_point` 为接收机降落点，`waypoint_counts_by_uav` 为各来源机航点数量。

例如 UAV1 消息中 `source_uav_id=2, waypoint_index=3` 表示从 UAV2 第 3 个侦察点返回 UAV1 降落点。GPS 仍为 `[经度,纬度,相对高度]`，XYZ 为 `[X,Y,Z]`。完整示例与时机见 [进场与全机队返航接口](external_transit_routes.md)。程序 B 的读取端需适配这些字段，本工程没有修改程序 B 源码。

## 固定目标记录

地面文件：`config/fixed_targets.json`。保留科目一最终赛事结果中去重排序后的**全部固定目标**，包括车辆、人员、工事及其他型号；移动目标不写入。固定目标之间的顺序与最终 `target-submission.json` 的 `features` 一致，经纬度取各目标的 `Point` 坐标。

自动更新由当前任务发布端执行，条件为本次科目一任务实际发出过起飞指令，且已进入 `completed` 或已确认全部结束的 `failed` 状态。飞行中、只有规划、科目二飞行和人工成果预览均不更新。读取最终成果与对应整理摘要，确认去重排序已完成且版本匹配后，原子替换这一份文件，不追加历史。成果尚未到齐时保留上一份；同一轮后续整理可补齐结果，直到开始下一任务或操作员修改文件。

赛后人工修正请编辑**当前任务发布端电脑**的该文件，保存为 UTF-8 JSON。可以修改坐标、型号、增删固定目标；`target_id` 须唯一，按赛事 `target-001` 格式编号；`target_category` 只能为 `固定`，`target_type` 与 `target_model` 沿用赛事结果。`category_id` 使用既有类别目录：车辆1～7 为 0～6，工事1～4 为 7～10，固定人员1～4 为 11～14；未知型号可填 `null`。旧版无 `target_category` 的工事记录仍按固定目标接受。`source_mission_id` 保留自动生成的科目一任务编号；没有来源任务的人工初始配置可留空。本机当前 `config/fixed_targets.json` 仅供联调，**坐标不是真实检测结果**，正式使用前应由下一次科目一结果覆盖或人工清空。示例：

```json
{
  "schema_version": 1,
  "coordinate_frame": "WGS84",
  "source_mission_id": "",
  "targets": [
    {
      "target_id": "target-001",
      "target_category": "固定",
      "target_type": "工事",
      "target_model": "工事2",
      "category_id": 8,
      "latitude": 33.8691414,
      "longitude": 113.7087948
    },
    {
      "target_id": "target-002",
      "target_category": "固定",
      "target_type": "人员",
      "target_model": "人员1",
      "category_id": 11,
      "latitude": 33.8644,
      "longitude": 113.7098
    }
  ]
}
```

人工修改后，同一轮飞行的后台整理不会覆盖修正；下一轮科目一飞行结束后正常生成新记录。普通地面端的副本会被发布端覆盖，应在发布端修改。互联配置同步及断线重连后的遥测互通都会读取发布端最新文件，即使无人机未连接也可同步。`tools/update_ground.ps1` 首次安装提供模板，后续保留本机已有记录。手动整目录覆盖代码时也应保留该文件。

科目一、科目二在“规划与分派”时将文件内容放入 `assign_task.fixed_targets`，六机使用同一份快照并纳入各自完整任务 SHA-256。已分派后再修改 JSON 不会改变正在执行的任务，下次分派才生效。缺失文件按空列表处理；格式或类别编号有误时规划会提示配置错误。

ROS 话题为 `/uavN/competition/fixed_targets`，N 是全机队编号 1～6（包括本地 ROS 编号固定为 1 的 P600）；消息类型 `std_msgs/String`，`queue_size=1`、`latch=True`。接收并校验分派任务后、发送 `task_received` 回执前发布，无需点击起飞。字符串内为上述 JSON 加上当前 `mission_id`、`uav_id`、`assignment_checksum`。`source_mission_id` 是目标记录来源，`mission_id` 是本次飞行，二者可能不同。

```bash
rostopic echo /uav3/competition/fixed_targets
```

程序 B 应按 `target_type`、`target_model` 或 `category_id` 区分固定目标类型，按命名字段 `latitude`、`longitude` 读取 WGS84 经纬度。晚启动的订阅者会收到锁存消息；重复分派同一报文会重新发布。新进程启动、空目标记录或旧版不带此字段的任务会发布空列表，接收方应清除旧目标。此话题只传递目标信息，不触发飞行或改变已有航线。需要同时更新地面代码、`competition_shared` 和机载竞赛执行器；无需新增 ROS 自定义消息类型。本工程未修改程序 B 源码。
