# SU17按需图片传输

该包实现可靠的按需图片链路：另一个ROS模块发出抓拍指令，机载节点取得相机的最新一帧，压缩成JPEG，先原子写入机载硬盘，再通过独立TCP通道发送到Windows地面端。到任务第19分钟时，机载端与地面端按图片文件名核对并补传缺失图片；如果此时仍断网，会定期重试。

本模块不修改`su17_experiment`，也不复用Prometheus的55555、55556和8889端口。默认图片端口为`56010`。

## 接口

默认使用新的双话题配对接口：

- 目标图片：`/uav1/image_transfer/target_image`（`sensor_msgs/Image`）
- 目标坐标：`/uav1/image_transfer/target_coordinate`（`sensor_msgs/NavSatFix`）
- 任务控制：`/uav1/image_transfer/mission_control`（`std_msgs/String`）
- 传输状态：`/uav1/image_transfer/status`（JSON格式的`std_msgs/String`）

图片进程和坐标进程必须给同一次目标事件填写相同或足够接近的`header.stamp`；默认允许相差`0.5`秒。节点只在图片与坐标配对成功后保存一次图片，在图片底部增加中文“目标经度”和“目标纬度”信息栏，再沿用原有落盘、TCP发送和补传流程。坐标使用WGS84十进制度：`latitude`范围为`[-90, 90]`，`longitude`范围为`[-180, 180]`。

每架无人机使用自己的命名空间，例如UAV2对应：

- `/uav2/image_transfer/target_image`
- `/uav2/image_transfer/target_coordinate`

旧的相机抓拍接口仍可通过启动参数`input_mode:=capture_request`临时启用：

- 相机输入：`/uav1/gimbal/image_original`（`sensor_msgs/Image`）
- 抓拍服务：`/uav1/image_transfer/capture`（`std_srvs/Trigger`）
- 抓拍指令：`/uav1/image_transfer/capture_request`（`std_msgs/String`）

正式算法使用双话题配对接口；图片消息本身就是保存触发，不再额外发送`capture_request`。任务控制Topic继续支持`start`、`start:<任务编号>`和`sync`。旧抓拍服务及抓拍Topic仅在`input_mode:=capture_request`兼容模式下启用。

## 六机ID隔离

每台无人机只启动一个发送节点，并通过`uav_id`生成自己的命名空间：

| 无人机 | 目标图片Topic | 目标坐标Topic | 任务控制Topic |
|---|---|---|---|
| UAV1 | `/uav1/image_transfer/target_image` | `/uav1/image_transfer/target_coordinate` | `/uav1/image_transfer/mission_control` |
| UAV2 | `/uav2/image_transfer/target_image` | `/uav2/image_transfer/target_coordinate` | `/uav2/image_transfer/mission_control` |
| UAV3 | `/uav3/image_transfer/target_image` | `/uav3/image_transfer/target_coordinate` | `/uav3/image_transfer/mission_control` |
| UAV4 | `/uav4/image_transfer/target_image` | `/uav4/image_transfer/target_coordinate` | `/uav4/image_transfer/mission_control` |
| UAV5 | `/uav5/image_transfer/target_image` | `/uav5/image_transfer/target_coordinate` | `/uav5/image_transfer/mission_control` |
| UAV6 | `/uav6/image_transfer/target_image` | `/uav6/image_transfer/target_coordinate` | `/uav6/image_transfer/mission_control` |

例如在2号机上启动：

```bash
roslaunch su17_image_transfer onboard_image_sender.launch \
  uav_id:=2 ground_host:=地面端IP
```

该节点只监听`/uav2/image_transfer/...`，不会接收UAV1或其他编号的目标图片、目标坐标及任务控制消息，从而避免六机之间串用数据。

地面接收器默认只接受1～6号机，并保存为：

```text
received_images/
├─ UAV1/<任务编号>/...
├─ UAV2/<任务编号>/...
├─ UAV3/<任务编号>/...
├─ UAV4/<任务编号>/...
├─ UAV5/<任务编号>/...
└─ UAV6/<任务编号>/...
```

TCP报文元数据和JPEG文件名也包含`uav_id`。缺少ID或ID不在1～6范围内时，地面端拒绝保存。

## 可靠传输参数

```yaml
enable_offline_recovery: true
reconcile_after_minutes: 19.0
reconcile_retry_sec: 30.0
cache_root: /home/amov/competition_development/image_cache
```

- `enable_offline_recovery`：是否启用“先落机载硬盘再发送”和文件名补传。
- `reconcile_after_minutes`：从任务`start`指令开始计时，默认第19分钟核对。
- `reconcile_retry_sec`：即时发送失败后自动核对，以及核对时网络仍中断的再次尝试间隔。
- `cache_root`：机载图片持久缓存根目录。

图片与坐标配对回调会在返回前完成中文标注、JPEG编码和机载落盘；网络发送在后台执行。断网后进入短暂的联网抑制期，期间的新图片仍会正常落盘，不会因每张图片等待TCP超时而堵塞。图片进入`pending_recovery`后，机载端会按`reconcile_retry_sec`自动尝试清单核对；网络恢复后无需人工发送`sync`。无论此前是否自动补传成功，第19分钟仍会执行一次最终全量核对。

## 1. Windows地面端

在PowerShell中运行：

```powershell
cd F:\Projects\ZhiXin\competition_development
python .\src\su17_image_transfer\ground\ground_image_receiver.py --bind 0.0.0.0 --port 56010 --output .\received_images
```

六机模式可以显式写出白名单：

```powershell
python .\src\su17_image_transfer\ground\ground_image_receiver.py --bind 0.0.0.0 --port 56010 --output .\received_images --uav-ids 1,2,3,4,5,6
```

也可以使用项目提供的启动脚本：

```powershell
.\tools\start_ground_receiver.ps1
```

首次使用时，Windows可能询问是否允许Python通过防火墙。只允许当前无人机使用的专用/私有网络，不要向公网开放该端口。

验证监听端口：

```powershell
Get-NetTCPConnection -LocalPort 56010 -State Listen
```

## 2. 将独立工程部署到机载电脑

工程应位于机载电脑的`/home/amov/competition_development`。在机载Ubuntu终端中构建：

如果已经配置SSH，可以先在Windows项目根目录一次性上传这个包：

```powershell
.\tools\deploy_image_transfer.ps1 -UavAddress 192.168.1.88
```

将地址替换为机载电脑的真实IP。脚本只复制新包，不会修改`su17_experiment`。

然后在机载Ubuntu终端中构建：

```bash
cd /home/amov/competition_development
source /opt/ros/noetic/setup.bash
source /home/amov/su17_experiment/devel/setup.bash
catkin_make --only-pkg-with-deps su17_image_transfer
source devel/setup.bash
```

确认真实相机话题存在：

```bash
rostopic info /uav1/gimbal/image_original
```

启动发送端，将`192.168.1.123`替换为Windows在无人机网络中的真实IP：

```bash
roslaunch su17_image_transfer onboard_image_sender.launch ground_host:=192.168.1.123
```

## 3. 开始一次科目一任务

在任务真正开始时发布任务编号；第19分钟从这条消息到达时开始计算：

```bash
rostopic pub -1 /uav1/image_transfer/mission_control \
  std_msgs/String "data: 'start:subject1-run-001'"
```

如果不指定编号，可让节点自动生成：

```bash
rostopic pub -1 /uav1/image_transfer/mission_control \
  std_msgs/String "data: 'start'"
```

节点启动时也会自动创建一个任务，但正式比赛建议显式发送`start:<任务编号>`，确保19分钟的起点准确。

## 4. 发出抓拍指令

服务方式：

```bash
rosservice call /uav1/image_transfer/capture
```

话题方式（字符串作为本次图片的唯一编号）：

```bash
rostopic pub -1 /uav1/image_transfer/capture_request std_msgs/String "data: 'manual-test-001'"
```

查看发送状态：

```bash
rostopic echo /uav1/image_transfer/status
```

成功后，地面端生成：

```text
received_images/
└─ UAV1/
   └─ subject1-run-001/
      ├─ subject1-run-001_UAV01_000001_时间.jpg
      └─ subject1-run-001_UAV01_000001_时间.json
```

机载端同时保留：

```text
/home/amov/competition_development/image_cache/
└─ subject1-run-001/
   ├─ subject1-run-001_UAV01_000001_时间.jpg
   └─ subject1-run-001_UAV01_000001_时间.json
```

## 5. 其他ROS模块调用

Python服务调用示例：

```python
import rospy
from std_srvs.srv import Trigger

capture = rospy.ServiceProxy("/uav1/image_transfer/capture", Trigger)
response = capture()
if response.success:
    rospy.loginfo("capture queued: %s", response.message)
else:
    rospy.logwarn("capture rejected: %s", response.message)
```

如果调用模块已经生成事件编号，直接发布字符串指令即可：

```python
from std_msgs.msg import String

publisher.publish(String(data="target-event-0001-frame-001"))
```

## 6. 补传

第19分钟会自动执行：机载端发送当前任务的全部`.jpg`文件名，地面端返回缺失文件名，机载端只补传缺少的图片，并再次核对直到无缺失。

如果第19分钟之前出现`pending_recovery`，机载端也会按`reconcile_retry_sec`自动核对。地面接收器恢复后，下一次重试会自动补传，无需人员登录机载端操作。第19分钟的最终核对仍然保留。

联调时无需等待19分钟，可以手动触发：

```bash
rostopic pub -1 /uav1/image_transfer/mission_control \
  std_msgs/String "data: 'sync'"
```

相关状态：

- `cached`：Topic回调已经把图片安全写入机载硬盘。
- `sent`：地面端已经保存并返回ACK。
- `pending_recovery`：即时发送失败，等待补传。
- `reconcile_complete`：清单核对和补传完成。
- `reconcile_retry_pending`：核对失败，将按参数继续重试。

## 常见问题

- `camera has not published an image yet`：相机话题名称错误或相机节点没有启动。
- `latest camera frame is ... old`：相机停止更新，默认拒绝发送超过2秒的旧帧。
- `Connection refused`：地面接收器未启动、IP错误或56010端口被防火墙拦截。
- Topic发出后地面没有图片：监听`/uav1/image_transfer/status`；出现`cached`说明机载文件安全，出现`sent`才表示地面已保存。
- 相机发布的是`CompressedImage`：启动时增加`compressed_input:=true`，并将`image_topic`设置为对应压缩话题。
