# su17_mission_planning

SU17 竞赛任务规划与航线规划包。

当前节点是接口联调基线：收到当前位置和目标点后，按 `step_size` 生成一条直线路径。后续算法应在保持输入输出契约稳定的前提下，逐步替换为地图约束、威胁/代价建模、多机冲突消解和动态重规划。

## 快速测试

启动节点：

```bash
roslaunch su17_mission_planning mission_planner.launch uav_id:=1
```

发布当前位置和目标点：

```bash
rostopic pub -1 /uav1/competition/current_pose geometry_msgs/PoseStamped \
  "{header: {frame_id: map}, pose: {position: {x: 0, y: 0, z: 2}, orientation: {w: 1}}}"

rostopic pub -1 /uav1/competition/goal geometry_msgs/PoseStamped \
  "{header: {frame_id: map}, pose: {position: {x: 10, y: 5, z: 3}, orientation: {w: 1}}}"
```

查看结果：

```bash
rostopic echo /uav1/competition/planned_path
```
