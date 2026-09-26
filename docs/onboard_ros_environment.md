# 机载端启动：找不到 prometheus_msgs

如果 `onboard_task_executor.py` 在导入 `UAVCommand`、`UAVControlState`、`UAVState`
时报 `ModuleNotFoundError: No module named 'prometheus_msgs'`，任务节点尚未完成初始化。
同一份日志中图片发送节点可以正常启动，这并不代表任务节点已经连接地面端。

`prometheus_msgs` 是 SU17 工作区的 ROS 消息包。竞赛工作区的 Catkin 环境可能保存了旧的
工作区链；直接加载它的 `devel/setup.bash` 会回退环境变量，从而移除已加载的 SU17 路径。
加载顺序应为 ROS Noetic、SU17、竞赛工作区，后两个都使用 `--extend`。

## 已更新的启动脚本

`tools/start_onboard_stack.sh` 现在会保留已有 SU17 环境，并在运行 roslaunch 前导入上述三个
消息类型。导入失败则退出并显示诊断，不再启动后反复重启任务节点。
脚本从自身路径确定竞赛工作区；无人机编号支持 1～6，地面地址仍可通过第二个参数覆盖。
默认编号 N 对应 `192.168.1.(120+N)`，本地 ROS 命名空间仍为 `/uav1`。

脚本只读取 `~/su17_experiment/devel/setup.bash`，不修改或重新编译 `su17_experiment`。
Windows 工程文件更新不会自动更新机载 Ubuntu 上的副本，需要将新脚本同步到
`~/competition_development/tools/start_onboard_stack.sh`。

在 Ubuntu 上按固定机队配置检查，不访问 ROS/MAVROS，也不启动节点：

```bash
cd ~/competition_development
bash tools/start_onboard_stack.sh --model p600 --expect-uav-id 1 --direct --check
```

固定绑定检查通过后，直接启动竞赛节点：

```bash
bash tools/start_onboard_stack.sh --model p600 --expect-uav-id 1 --direct
```

认证令牌从 `tools/local_tokens.env` 的 `AUTH_TOKEN` 读取。若要严格读取飞控和 Prometheus 状态话题，去掉 `--direct`，但厂商 ROS/MAVROS 必须已经启动。

## 旧机载副本的即时修复

先在报错的竞赛 roslaunch 终端按 `Ctrl+C`，然后在 Ubuntu 执行。这里只修改竞赛启动脚本，
并留下 `.before_extend.bak` 备份；不会写入 SU17 工作区：

```bash
cd ~/competition_development
sed -i.before_extend.bak -E 's|^(source[[:space:]]+[^[:space:]]*/devel/setup\.bash)[[:space:]]*$|\1 --extend|' tools/start_onboard_stack.sh
source /opt/ros/noetic/setup.bash
source ~/su17_experiment/devel/setup.bash --extend
source ./devel/setup.bash --extend
python3 -c 'from prometheus_msgs.msg import UAVCommand, UAVControlState, UAVState; print("Prometheus 消息导入成功")'
```

最后一行成功后，重新执行原来的 `bash tools/start_onboard_stack.sh 3 ...` 启动命令。
这条即时修复只处理环境加载顺序；启动前自动检查和 `--check` 选项需要同步完整新版脚本。

如果仍报同样错误，可进一步只读检查已有生成文件及 Python 搜索路径：

```bash
ls ~/su17_experiment/devel/lib/python3/dist-packages/prometheus_msgs/msg/_UAVCommand.py
ls ~/su17_experiment/devel/lib/python3/dist-packages/prometheus_msgs/msg/_UAVControlState.py
ls ~/su17_experiment/devel/lib/python3/dist-packages/prometheus_msgs/msg/_UAVState.py
python3 -c 'import sys; print(sys.executable); print("\n".join(sys.path))'
```

文件不存在说明机载工作区的消息生成结果不齐全；文件存在但导入失败则要继续核对搜索路径
和实际 Python 解释器。不要仅凭 Windows 本地副本存在这些文件就判断机载端也已生成。
