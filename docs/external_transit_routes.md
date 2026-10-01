# 程序 B 进场、逐航点返航接口

## 生效范围与时机

- 覆盖 3m、5m、10m、100m、200m、竞赛、大连南山坡全部固定场景及其可选出发点，共 13 组固定方案；支持各场景允许的 GPS / XYZ 坐标系及全部高度选项。
- 进场起点、每条返航路线终点、降落点统一为界面选定的出发点：区域右下角、操场中央或大连固定起飞点，不再使用各机实际起飞位置作为降落点。
- 点击“规划与分派”：两类航线随该机任务通过现有机地通信发送、校验和缓存；此时不在新 ROS 话题发布。
- 点击“一键起飞”，机载执行器确认到达任务高度后：发布进场路线、返航路线集合、原侦察航点和降落点，然后发布 `recon_start_mode=0`。起飞失败或取消时不发布新任务航线。
- 一键起飞后保留原有任务锁定，不允许分派覆盖正在执行的任务。

## 新增话题

以下 N 是本机 ROS 无人机编号，范围 1～6。两个话题均为 `std_msgs/String`，内容为 UTF-8 JSON，锁存最新消息，供后启动的订阅者读取。消息是任务数据，不是速度或飞控控制指令。

| 话题 | 用途 |
| --- | --- |
| `/ground_mission_planner/vehicle_N/entry_path` | 选定出发点至第一个侦察航点的路线 |
| `/ground_mission_planner/vehicle_N/return_paths` | 每个侦察航点各一条返回选定出发点的路线 |

共同字段：`schema_version=1`、`mission_id`、`uav_id`、`assignment_checksum`、`plan_sha256`、`coordinate_frame`、`coordinate_order`、`altitude_frame`。

- GPS：`coordinate_frame="WGS84"`，`coordinate_order="longitude_latitude_relative_altitude"`，每点为 `[经度, 纬度, 相对起飞点高度米]`。
- XYZ：`coordinate_frame="ENU"`，`coordinate_order="x_y_z"`，每点为 `[X米, Y米, 任务高度米]`，沿用原任务 XYZ 坐标约定。
- 高度始终使用该架无人机实际选定的 `target_altitude_m`，不叠加 GPS 海拔；`altitude_frame="RELATIVE_TO_TAKEOFF"`。
- `entry_path` 消息的 `path` 是点数组，含出发点和首个侦察航点。
- `return_paths` 消息的 `routes` 是对象数组，每项为 `{"waypoint_index":1,"path":[...]}`。编号从 1 开始，与原侦察航点顺序一一对应，每条含对应侦察点和选定出发点。
- 中间点只是进返场转折点，不新增扫描动作或侦察点编号。路线末点仍为任务高度，降落由控制程序执行。

XYZ 内容示意（坐标仅解释协议，不用于飞行）：

```json
{
  "schema_version": 1,
  "mission_id": "示例任务",
  "uav_id": 1,
  "assignment_checksum": "当前任务校验值",
  "plan_sha256": "固定航线校验值",
  "coordinate_frame": "ENU",
  "coordinate_order": "x_y_z",
  "altitude_frame": "RELATIVE_TO_TAKEOFF",
  "path": [[0, 0, 1.5], [1, 0, 1.5], [1, 1, 1.5]]
}
```

返航消息使用相同共同字段，将 `path` 换为：

```json
"routes": [
  {"waypoint_index": 1, "path": [[1, 1, 1.5], [1, 0, 1.5], [0, 0, 1.5]]},
  {"waypoint_index": 2, "path": [[2, 1, 1.5], [1, 0, 1.5], [0, 0, 1.5]]}
]
```

程序 B 需要自行接入这两个新话题；本次没有修改程序 B 源码。不同 ROS 话题到达顺序并非原子事务：接收方应缓存消息，核对 `mission_id` 和 `assignment_checksum` 属于同一任务、数据齐全且收到启动信号后再使用。锁存消息可能是上一任务，不能仅以“收到话题”作为开始飞行依据。

原 `path_stage_1`、`jiangluodian` 仍为 `Float64MultiArray`，GPS 顺序仍为经度、纬度、相对高度，XYZ 顺序不变，降落点仍只含三个数值。

## 返航按钮

任务发布端点击“返航”后勾选本次任务的无人机；普通地面端只能选择本机配对无人机。界面逐机显示发送结果，一机失败不阻止其他机；不会自动重发。“已发送”表示发出请求，不代表已落地。

- 本工程自主控制：飞到选定出发点后发送降落指令。旧任务无选定出发点数据时兼容原实际起飞点返航。
- 外部程序 B：继续在 `/ground_mission_planner/vehicle_N/return_home` 发布 `std_msgs/Bool` 的 `true`。程序 B 负责从已接收的路线集合选择对应路线并完成返航、降落。
- 遥控器已接管时保留原有自动控制退出逻辑。

## 固定规划

文件：`competition_backend/competition_backend/external_transit_prepared.json`。赛前计算多边形内可见图最短路径，进场路线到首个侦察点，返航路线为对应最短进场路线的反向。

区域内航段不得穿越凹边界；出发点在区域外时只允许起始进场连接段在外部，进入区域后不再离开；返航只允许最后返回区域外出发点的连接段离开。约束为整个任务区域，允许经过其他无人机的子区。运行时校验固定文件、边界、航点和坐标转换，不重复求解。

这是几何边界规划，没有加入建筑物、动态障碍或多机避碰。程序 B 从追踪位置等任意位置返航时，还须自行连接到选用的航点返航路线；不能假定任意当前位置与路线首点之间的直线一定在区域内。六机共用选定降落点，实际降落间隔由执行程序或操作人员安排。

更新区域或侦察航点后，在项目目录预生成：

```powershell
.\competition_backend\.venv\Scripts\python.exe .\tools\prepare_transit_routes.py
```

本次需更新地面程序和竞赛机载程序，部署、构建、启动命令保持原样。未修改 P600 / SU17 厂家源码、目标检测程序或程序 B。未执行实飞验证。

查看 UAV1 的新话题：

```bash
rostopic echo /ground_mission_planner/vehicle_1/entry_path
rostopic echo /ground_mission_planner/vehicle_1/return_paths
```
