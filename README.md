# Competition Development

## 当前新增模块

- `src/su17_image_transfer`：按ROS指令抓取SU17当前相机帧，并通过独立TCP通道发送到Windows地面端。具体启动与联调步骤见该模块的`README.md`。

“智信—2026”无人智能挑战赛独立算法开发工作区。

本目录与无人机源码分离：

```text
F:\Projects\ZhiXin\
├─ su17_experiment\          # 无人机复制出的源码，仅作参考和部署目标
└─ competition_development\  # 本工作区，开发任务与航线规划算法
```

## 打开工程

在安装 ROS Noetic 的 Ubuntu 20.04 开发机或 VS Code Remote SSH 会话中打开：

```text
competition_development.code-workspace
```

该工作区会同时显示：

- `competition_development`：可修改的竞赛算法工程；
- `su17_reference`：同级 `su17_experiment` 原始源码，用于查询接口。

团队新增代码统一放在本目录的 `src` 中，不直接修改 `../su17_experiment`。

## 首次构建

通过 VS Code 执行 `ROS: Build competition package`，或在 Ubuntu 终端执行：

```bash
cd competition_development
source /opt/ros/noetic/setup.bash
catkin_init_workspace src
catkin_make --only-pkg-with-deps su17_mission_planning
source devel/setup.bash
roslaunch su17_mission_planning mission_planner.launch
```

如果同级 `su17_experiment/devel/setup.bash` 存在，VS Code 构建任务会先加载它，使竞赛包能够逐步复用原工程已经编译的 ROS 消息与库。

## 当前算法包

`src/su17_mission_planning` 是任务与航线规划的起始包，目前带有一个直线插值基线节点，仅用于验证编译、参数和话题接口，不是最终比赛算法。

默认接口位于 `/uav1/competition`：

- 输入 `current_pose`：`geometry_msgs/PoseStamped`
- 输入 `goal`：`geometry_msgs/PoseStamped`
- 输出 `planned_path`：`nav_msgs/Path`
- 输出 `status`：`std_msgs/String`

竞赛背景及现有 SU17 代码地图见 `docs/competition_context.md`。
