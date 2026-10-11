# 程序 B 进场与全机队航点返航接口

## 范围与发布时间

覆盖 3m、5m、10m、100m、200m、竞赛、大连南山坡、许昌试飞场地（小）、科目一比赛使用场景及可选出发点，共 15 组固定方案，支持允许的 GPS / XYZ 坐标系和全部高度选项。

- 每架接收机收到 UAV1～6 **全部规划航点**的返航路线，包括当次未连接飞机的规划点。各机仍只执行自己的侦察任务，进场目的地为自己的第一个侦察航点。
- 所有路线返回**接收机自己的降落点**。例如 UAV1 收到的 UAV2 第 3 个航点路线，从该航点返回 UAV1 降落点；UAV2 收到的同一来源航点路线返回 UAV2 降落点。
- 所有路线高度使用**接收机的任务相对高度**，不是来源机高度。
- GPS 实飞时，许昌小场景与科目一比赛场景的降落点取本次开机首次有效且未解锁的GPS记录；其他场景取本次规划分派前取得的各机自身 GPS；规划后保持固定，不是点击起飞时重新采样。XYZ 固定方案沿用选定出发点。离线 GPS 预览使用固定出发点示意，不代替实飞 GPS。
- 点击“规划与分派”：航线随任务发送至竞赛机载执行器，校验、缓存；此时不发布新航线话题。
- 点击“一键起飞”并成功到达任务高度：发布进场、返航路线、侦察航点和降落点，最后发布 `recon_start_mode=0`。起飞未成功时不发布新航线；起飞后不允许新分派覆盖机载任务。

## 话题与字段

两个话题均为 `std_msgs/String`，`data` 是 UTF-8 JSON，锁存最后一条消息。N 为本机 ROS 编号；消息内 `uav_id` 为接收机的竞赛逻辑编号。

| 话题 | 内容 | JSON 版本 |
| --- | --- | --- |
| `/ground_mission_planner/vehicle_N/entry_path` | 接收机降落点至接收机第一个侦察航点 | `schema_version=1`，不变 |
| `/ground_mission_planner/vehicle_N/return_paths` | 六机全部航点至接收机降落点的路线集合 | `schema_version=2` |

共同字段为 `mission_id`、`uav_id`、`assignment_checksum`、`plan_sha256`、`coordinate_frame`、`coordinate_order`、`altitude_frame`。

- GPS：`coordinate_frame="WGS84"`，`coordinate_order="longitude_latitude_relative_altitude"`，每点 `[经度, 纬度, 相对起飞点高度米]`。
- XYZ：`coordinate_frame="ENU"`，`coordinate_order="x_y_z"`，每点 `[X米, Y米, 任务高度米]`，沿用任务局部坐标约定。
- `altitude_frame="RELATIVE_TO_TAKEOFF"`。所有路线点高度为接收机的 `target_altitude_m`，不叠加 GPS 海拔；末点高度不是触地高度，下降和降落由程序 B 执行。
- 返航消息 `route_scope="all_uav_waypoints"`；`landing_point` 与该机 `jiangluodian` 的三个数值相同。
- `waypoint_counts_by_uav` 给出各来源机规划航点数量。
- `routes` 按 `source_uav_id` 升序、再按 `waypoint_index` 升序排列。`source_uav_id` 为来源机编号 1～6；`waypoint_index` 为该来源机航点编号，从 1 开始。
- 唯一键是 **`(source_uav_id, waypoint_index)`**，不能仅凭 `waypoint_index` 或数组下标选择路线。
- 每条 `path` 包含来源航点、必要的区域内转折点、接收机降落点。中间转折点不新增扫描动作。

UAV1 接收的 XYZ 返航消息示意（仅展示两条，真实消息包含六机全部航点；示例坐标不可用于飞行）：

```json
{
  "schema_version": 2,
  "mission_id": "示例任务",
  "uav_id": 1,
  "assignment_checksum": "当前任务校验值",
  "plan_sha256": "固定航线校验值",
  "coordinate_frame": "ENU",
  "coordinate_order": "x_y_z",
  "altitude_frame": "RELATIVE_TO_TAKEOFF",
  "route_scope": "all_uav_waypoints",
  "landing_point": [0, 0, 1.5],
  "waypoint_counts_by_uav": {"1":18,"2":11,"3":8,"4":8,"5":13,"6":15},
  "routes": [
    {"source_uav_id":1,"waypoint_index":1,"path":[[1,1,1.5],[1,0,1.5],[0,0,1.5]]},
    {"source_uav_id":2,"waypoint_index":3,"path":[[2,1,1.5],[1,0,1.5],[0,0,1.5]]}
  ]
}
```

各场景和出发点的实际航点数量不同，以消息中的数量为准。

程序 B 需接入新版 `schema_version=2` 和来源编号；此次未修改程序 B 源码。升级后的竞赛机载端仍兼容旧的版本 1 单机任务，但旧缓存不会自动变成全机队路线，更新后需重新规划与分派。地面端与竞赛机载端均需更新，部署、构建、启动命令不变。

多个 ROS 话题不是原子事务。程序 B 应核对 `mission_id` 和 `assignment_checksum`，确认同一任务的数据齐全且收到启动信号后使用；锁存消息可能来自旧任务。原 `path_stage_1` 和 `jiangluodian` 仍为 `Float64MultiArray`，分别承载本机侦察航点与降落点。

## 几何来源与返航请求

### 100m、200m、竞赛 1km 场景的 5 米边界间距

这三个场景的两种出发点均使用重新生成的固定侦察方案。侦察点、相邻侦察航段、区域内进场航段和返航航段距整个任务区域外边界至少 5 米；不是距六机子区之间的分界线 5 米。侦察航线按 5.03 米内缩，进返场航线按 5.02 米内缩，额外间距用于抵消坐标舍入误差，适用于全部高度和 GPS / XYZ 表达。

保留原方案的扫描密度，对近边点进行内移、补齐覆盖缺口，再改善航点顺序。树林和湖泊内部仍按原有地类排除规则免侦察；外侧 5 米边界带没有从目标覆盖面积中删除，通过内侧侦察点的覆盖半径覆盖。凹边界的必要转折点也写入实际下发的航点，避免只在预览中绕弯、实际发出的相邻点直线却穿出安全区。任务估时计入这些下发点。

**固定起降点连接段例外：**起降点若位于边界上、距边界不足 5 米或区域外，保持该实际起降点不变，使用通往内缩区域的可见连接段；只有这段无法满足 5 米。进入内缩区域后保持间距，返航仅最后返回该起降点的连接段例外。不会把近边侦察航点当成例外。

新消息包含 `boundary_clearance_m: 5.0` 与 `endpoint_clearance_policy: "takeoff_landing_connector_only"`。原 3m、5m、10m、大连场景不启用这一新约束。

### 许昌试飞场地（小）的内部扣除区

许昌场景使用固定 WGS84 外边界与起飞点，内部使用用户给定的六点禁飞扣除区；见 [许昌场地坐标与固定方案](xuchang_small_scene.md)。六机侦察点、相邻航段、进场和全机队返航路线均距**外边界与内部扣除区边界**至少 5 米，扣除区完全不作为侦察目标。当前六区详见许昌固定方案；UAV4东侧六点区域已交给UAV3，六机共75个侦察点。高度按接收机编号保持不变；侦察半径为45米。绕行转折点写入实际下发的路线，不依赖预览图推测路径。

许昌实飞使用每架飞机本次开机首次有效且未解锁的GPS记录，重连不改变。UAV1、UAV2、UAV4、UAV5分别使用北侧、东侧进入后南绕、西北、西南固定通道；重接起降点与通道，并对进场和全部返航路线避让其他已连接无人机落点至少2.5米。不受阻的通道保持原样，冲突航段增加绕行，不重排侦察点。按接收机保存全部机队航点的返航通道，三维预览与实际下发使用同一组路径。UAV3、UAV6也检查完整路线的落点避让。若实际起降点处在扣除区或不足 5 米的边界缓冲区，在发送任何机载任务之前拒绝整次分派；落点相互过近或遮挡侦察点也拒绝，不静默降低间距。详见[避让案例图](xuchang_boot_home_example.png)。

固定侦察方案由 `tools/prepare_clearance_plans.py` 赛前生成，保存在 `competition_backend/competition_backend/competition_clearance_prepared.json`，运行时只读；区域内进返场几何仍保存于下述文件，GPS 实际起降点变化时按相同 5 米规则重接。更新后应在起飞前重新规划与分派，旧任务不会在飞行中自动替换。

固定文件为 `competition_backend/competition_backend/external_transit_prepared.json`，保存全部场景各机的原始进返场几何。地面端组合六机几何；GPS 实飞再依据接收机实测起飞点，重接全机队航点的返航路线，不改变侦察点。

路线约束为整个任务区域，允许经过其他飞机子区。区域内航段不得穿越凹边界；出发点在区域外时，进场只允许初始连接段在区域外，返航只允许最后连接段离开区域。未加入建筑物、动态障碍或多机避碰；程序 B 从任意追踪位置返航时，应自行连接到选定路线的首点。

地面“返航”按钮继续发送 `/ground_mission_planner/vehicle_N/return_home`，类型 `std_msgs/Bool`、值 `true`；程序 B 选择路线并执行返航、下降和降落。上述路线扩展不会自动发出返航指令。

查看 UAV1 话题：

```bash
rostopic echo /ground_mission_planner/vehicle_1/entry_path
rostopic echo /ground_mission_planner/vehicle_1/return_paths
```

区域或原始侦察航点变化后，在项目目录预生成固定几何：

```powershell
.\competition_backend\.venv\Scripts\python.exe .\tools\prepare_clearance_plans.py
.\competition_backend\.venv\Scripts\python.exe .\tools\prepare_xuchang_small_plan.py
.\competition_backend\.venv\Scripts\python.exe .\tools\prepare_transit_routes.py
.\competition_backend\.venv\Scripts\python.exe .\tools\build_plans_3d.py
```
