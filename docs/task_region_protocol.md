# 任务子区域边界话题

点击“规划与分派”，地面将每架无人机自己的子区域随任务发送；机载完成任务校验后立即发布，随后返回任务已确认。无需点击一键起飞，适用于本工程自主控制和外部程序 B。该话题只提供区域，不触发飞行。

- 话题：`/ground_mission_planner/vehicle_N/task_region`，N 为机载本地 ROS 无人机编号。
- 类型：`std_msgs/String`，`data` 是 UTF-8 JSON。
- 锁存最后一条，后启动的程序也能收到；新任务覆盖起飞前旧消息。起飞后仍禁止覆盖任务。
- 每架收到自己的区域；`uav_id` 是机队任务编号，`mission_id` 和 `assignment_checksum` 对应该次分派。
- GPS：`coordinate_frame=WGS84`，顶点为 `[经度,纬度]`；XYZ：`coordinate_frame=ENU`，顶点为 `[x,y]`（米），与该任务本地航点一致。
- `polygons` 支持多块区域。`outer` 为按边界顺序排列的闭合外环，`holes` 为闭合内孔洞；首尾顶点相同。扣除区与边界相接时表现为外环凹口，完全在内部时表现为孔洞。
- 边界是任务分区边界，不是已经内缩 5 米的航线。侦察航点与进返场安全间距规则保持不变。
- `relative_altitude_m` 为该机本次分配的相对起飞点高度，不是 GPS 海拔。

示例（示意坐标，不是固定许昌分区）：

```json
{"schema_version":1,"uav_id":4,"mission_id":"subject1-example","assignment_checksum":"本次任务SHA-256","coordinate_frame":"WGS84","coordinate_order":"longitude_latitude","relative_altitude_m":52.0,"altitude_frame":"RELATIVE_TO_TAKEOFF","polygons":[{"outer":[[113.9100,34.1380],[113.9110,34.1380],[113.9110,34.1390],[113.9100,34.1390],[113.9100,34.1380]],"holes":[]}]}
```

查看：

```bash
rostopic echo /ground_mission_planner/vehicle_4/task_region
```
