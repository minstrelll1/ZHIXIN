# SU17 Competition Onboard Executor

该 ROS1 节点运行在每架无人机的机载计算机上。在六地面电脑架构中，每架机只主动连接厂家分配给
它的那台 Windows 地面电脑 `56100` 端口；它不会直接连接本次任务的主控电脑，跨电脑转发由地面
后端完成。Windows 不需要安装 ROS。UAV1 使用以下接口：

厂家为每架 SU17 提供的机内 ROS 命名空间都固定为 `/uav1`。本节点将两个编号分开处理：

- `uav_id`：六机系统中的逻辑编号，随飞机分别设置为 1～6，用于地面任务、TCP 和图片归档。
- `local_ros_uav_id`：机内底层 ROS 编号，SU17 固定设置为 1；状态读取和控制命令使用 `/uav1`。

- 订阅：`/uav1/competition/high_level_command` (`std_msgs/String` JSON)
- 发布：`/uav1/competition/task_status` (`std_msgs/String` JSON，latched)
- 发布：`/uav1/prometheus/command` (`prometheus_msgs/UAVCommand`)
- 读取：`/uav1/prometheus/state`、`/uav1/prometheus/control_state`
- TCP：任务、ACK、状态和遥测；断线后自动重连

## 任务分派闭环

收到 `assign_task` 后，节点会验证 UAV ID、任务编号、ENU 坐标、弓字航点和目标高度，然后原子保存到：

```text
/home/amov/competition_development/mission_cache/uav1/assigned_task.json
```

地面端会为完整任务计算 SHA-256。机载端会重新计算并核对；文件及目录元数据写入后执行 `fsync`，只有指纹一致且保存成功才回复：

```json
{"state":"task_received","uav_id":1,"mission_id":"...","task_assignment_acked":true,"assignment_checksum":"..."}
```

地面端起飞预检会同时核对 `mission_id` 和 `assignment_checksum`，所以旧任务 ACK 或不完整任务都不能通过。节点默认在启动终端打印完整任务 JSON；若只显示摘要，可增加 `log_full_task:=false`。

## 安全模式启动

首次测试保持运动禁用。该模式只接收、校验、保存任务并回复 ACK，不向飞控发送运动命令：

```bash
roslaunch su17_competition_executor onboard_task_executor.launch \
  uav_id:=1 local_ros_uav_id:=1 transport:=tcp ground_host:=192.168.1.230 ground_port:=56100 \
  enable_motion:=false
```

检查：

```bash
rostopic echo /uav1/competition/high_level_command
rostopic echo /uav1/competition/task_status
cat /home/amov/competition_development/mission_cache/uav1/assigned_task.json
```

## 运动模式

只有完成无桨测试、定位/控制状态检查以及场地坐标标定后，才可改为：

```bash
roslaunch su17_competition_executor onboard_task_executor.launch \
  uav_id:=1 local_ros_uav_id:=1 transport:=tcp ground_host:=192.168.1.230 ground_port:=56100 \
  enable_motion:=true max_distance_from_home_m:=10.0
```

运动流程支持 `takeoff`、`execute_task` 和 `return_home`。航点使用绝对 ENU 坐标；规划区域的 `(0,0)` 必须与无人机定位坐标原点一致。实验室默认最大航点距离为 10m，正式 1km 场地必须经过标定后显式调整该限制。

每架机独立衔接起飞与任务：若 `execute_task` 在抬升线程结束前到达，节点会先排队保存，不会以“执行器忙”拒绝；该机在自己的目标高度稳定后立即飞向第一个航点，不等待其他无人机。未稳定到目标高度时禁止开始水平航线。

悬停扫描的漂移保护使用两级限制。默认软容差为水平 0.25 m、垂直 0.20 m，软范围内的位置抖动不会中断扫描；硬容差为水平 0.50 m、垂直 0.30 m，超过硬容差后默认允许 3 秒恢复，持续超限才结束当前扫描并停止航线。连接、解锁、里程计有效、控制权和 failsafe 检查仍然保留。需要调整时，可在 launch 中设置 `scan_position_tolerance_m`、`scan_altitude_tolerance_m`、`scan_hard_position_tolerance_m`、`scan_hard_altitude_tolerance_m` 和 `scan_drift_grace_seconds`。
