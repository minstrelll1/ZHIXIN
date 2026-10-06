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
