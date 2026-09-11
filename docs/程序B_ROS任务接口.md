# 程序 B 的 ROS 任务接口

地面端规划时，在“任务控制程序”选择“外部自主程序B（比赛）”。任务区和降落区各输入四个点：GPS 模式直接输入纬度、经度，XYZ 模式额外填写 XYZ 转 GPS 基准纬度、经度和海拔。地面端据此为每架无人机生成独立的 WGS84 航线。机载任务执行器仍负责接收、校验和落盘完整任务，并先把无人机升到分配高度；稳定到达后，发布程序 B 的固定 ROS 接口。

```text
/ground_mission_planner/vehicle_1/path_stage_1
```

> SU17 每台机载机的厂家 ROS 命名空间均为 `/uav1`。`uav_id` 字段才是竞赛中的逻辑编号，例如 UAV3。

该话题实际类型为 `std_msgs/Float64MultiArray`，`data` 按连续三元组排列：`[纬度1, 经度1, 高度1, 纬度2, 经度2, 高度2, ...]`。例如：`data: [30.7852, 103.8610, 140.0, 30.7853, 103.8611, 140.0]`。

降落点通过另一个 `std_msgs/Float64MultiArray` 话题发布，`data` 为 `[纬度, 经度, 高度, 降落顺序]`：

```text
/ground_mission_planner/vehicle_1/jiangluodian
```

到达分配高度后，机载端在以下话题发布 `std_msgs/Int32` 值 `1`，通知程序 B 开始侦察：

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
  "coordinate_frame": "WGS84",
  "target_altitude_m": 140.0,
  "relative_altitude_m": 40.0,
  "waypoints": [
    {"latitude": 30.7852, "longitude": 103.8610, "altitude_m": 140.0}
  ],

  "return_home": {"latitude": 30.7851, "longitude": 103.8609, "altitude_m": 100.0},
  "deadline_at": 1780000000.0,
  "return_battery_threshold": 0.25
}
```

`target_altitude_m` 和每个航点的 `altitude_m` 是地面端使用比赛区域原点 GPS 海拔加分配相对高度计算得到的绝对高度；`relative_altitude_m` 保留原始的相对起飞高度，供程序 B 交叉核对。程序 B 收到任务后独占向 Prometheus 飞控发布后续航点、返航和降落指令，本工程不会再发布搜索阶段的 Prometheus 移动指令。

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
