# SU17 目标图片回传

默认流程：持续接收相机原图 → 在内存中保留最近 0.5 秒画面 → 算法发布 `CompletedTargetArray` → 按每个目标的 `timestamp` 精确匹配原帧 → 在原图上画目标框 → 保存 JPEG 和 JSON → 经 TCP 回传地面。

图片只增加绿色目标框，不加坐标文字、底部信息栏或其他标签；保持原图宽高，不主动缩放。JPEG 默认质量为 80，因此不是无损原始图像。坐标、置信度、类型及扩展信息通过配套 JSON 回传和保存。

只修改 competition_development 的独立图片包，不修改 su17_experiment，不添加竞赛主页图片列表或预览。原有 TCP 56010、AuthToken 鉴权、地面目录、ACK、磁盘缓存、断网补传和地面端汇总继续沿用。

## 1. 输入接口

以编队编号 UAV3、厂商相机 ROS 编号 1 为例：

| 内容 | 话题 | 类型 |
|---|---|---|
| 相机原图 | /uav1/gimbal/image_original | sensor_msgs/Image |
| 算法结果 | /target_stcheduler/complemend_targets（可通过 `completed_target_topic` 修改） | su17_image_transfer/CompletedTargetArray |
| 图片任务控制 | /uav3/image_transfer/mission_control | std_msgs/String |
| 回传状态 | /uav3/image_transfer/status | JSON 格式的 std_msgs/String |

算法和回传节点订阅同一个相机源。算法不需要再次发送整张图片。uav_id 为编队编号 1～6，local_ros_uav_id 为相机话题编号，两个参数独立。单独修改编队编号不会修改厂商相机话题。

### 帧缓存

- frame_cache_sec 默认 0.5，camera_fps 默认 30，容量为 ceil(时长 × 帧率)，默认最多 15 帧。
- 每帧按原始 header.stamp.secs/nsecs 保存整数纳秒索引，另用单调时钟记录接收时间并淘汰超过窗口的帧；相机停止更新时也清理过期缓存。
- 相机回调只缓存消息引用和匹配请求；绘框、编码和落盘在后台线程执行，网络发送仍由独立线程处理。
- 不生成、插值或强制修改相机帧率，也不按算法结果频率采样。30 FPS 必须由相机源提供；若源帧率高于配置，容量上限会缩短可回查时间。可用 rostopic hz 核实。
- 连续原始帧只在短时内存中滚动保留，只有算法选中的带框图片沿用原有磁盘缓存。已匹配且排队处理的帧保留引用至处理完成。
- **精确匹配时间戳，不找最近帧，也不回退到最新帧。**若结果先于相机回调到达，最多再等待一个缓存窗口；仍找不到则报告 frame_not_found。
- 算法应在原帧到达回传节点后的 0.5 秒内提交结果。复制 header.stamp，不要使用推理完成时间或把时间戳转成浮点秒后重建。非空 header.frame_id 也必须与原图一致。

### CompletedTarget 消息

定义位于 msg/CompletedTarget.msg 和 msg/CompletedTargetArray.msg。一个数组可以携带多个目标；节点逐目标匹配并逐目标回传图片。

`CompletedTarget.timestamp` 是匹配原图的时间戳；只有它为零时才回退到兼容字段 `image_stamp`。`CompletedTarget.header.stamp` 是结果发布时间，不能用于匹配相机帧；`header.frame_id` 为 `wgs84`，不与相机 frame_id 比较。

| 字段 | 含义 |
|---|---|
| timestamp/image_stamp | 原图采集时间戳，必须复制相机原帧时间戳 |
| global_id、target_type/category | 目标 ID 和类别 |
| cx、cy、w、h | 原始图像像素框 |
| score、category_id、speed_mps、is_moving | 识别与运动信息 |
| indoor_position、east_m/north_m/up_m | 室内局部 XYZ；室外时无效 |
| latitude_deg/longitude_deg/altitude_gps_m | 室外 WGS84；室内时无效 |

框必须对应原始图像尺寸。如果算法使用缩放或补边后的输入，发布前需将框坐标映射回原图。部分越界的框按图像边缘裁剪，完全越界的框拒绝回传；原始框和实际绘制角点都记录在 JSON 中。

同一原帧的不同目标可分别发布消息，每次在原图副本上绘制本次目标框；之前的框不会残留到下一张回传图片。此接口不将多条结果自动合并成一张多框图片，也不按 request_id 自动去重。

## 2. 算法侧发布示例

在已有算法 ROS 节点中初始化 Publisher，并在推理完成后调用以下函数。坐标、类型等应传入真实算法结果。image_msg 必须是本次推理对应的原始图像消息。

```python
import rospy
from su17_image_transfer.msg import CompletedTargetArray, CompletedTarget
import json

# 放在已有节点 rospy.init_node(...) 之后。
result_pub = rospy.Publisher(
    "/target_stcheduler/complemend_targets", CompletedTargetArray, queue_size=10
)

def publish_target(image_msg, box, position, confidence, target_type,
                   target_id="", moving=False):
    result = CompletedTarget()
    result.header.stamp = rospy.Time.now()  # 结果发布时间
    result.header.frame_id = "wgs84"
    result.global_id = str(target_id)
    result.target_type = target_type
    result.timestamp = image_msg.header.stamp  # 用原图时间戳匹配缓存
    result.cx, result.cy, result.w, result.h = box
    result.score = confidence
    result.is_moving = moving
    result.indoor_position = False
    result.latitude_deg, result.longitude_deg, result.altitude_gps_m = position
    result_pub.publish(CompletedTargetArray(targets=[result]))
```

算法包需声明对 su17_image_transfer 的消息依赖，运行前加载更新后的 competition_development/devel/setup.bash。C++ 节点可包含 su17_image_transfer/TargetDetection.h，字段含义相同。

## 3. 构建和启动

将更新后的整个 src/su17_image_transfer 包同步到机载 ~/competition_development/src/，包括新增 msg 文件夹。此次新增自定义消息，需要重新构建独立图片包：

```bash
cd ~/competition_development
source /opt/ros/noetic/setup.bash
source ~/su17_experiment/devel/setup.bash --extend
catkin_make --only-pkg-with-deps su17_image_transfer
source devel/setup.bash
rosmsg show su17_image_transfer/TargetDetection
```

上述流程只读取原有 SU17 环境，不编译或修改其源代码。ROS 依赖包括 message_generation、message_runtime、cv_bridge；不再需要中文字体或 Pillow 来标注图片。

原有一键机载启动脚本包含此 launch，默认会切换到新接口。单独联调图片节点时（不要与已运行的一键程序重复启动）：

```bash
roslaunch su17_image_transfer onboard_image_sender.launch \
  uav_id:=3 local_ros_uav_id:=1 ground_host:=192.168.1.123 \
  auth_token:="$AUTH_TOKEN"
```

AUTH_TOKEN 使用已配置的固定 AuthToken，地面图片接收器需使用同一个值。相机话题不是默认名称时指定 image_topic:=实际话题；若消息类型为 CompressedImage，同时指定 compressed_input:=true。压缩输入也先解码、画框，再编码 JPEG。

地面仍使用现有地面启动命令，不需要增加相册或预览服务。独立接收器程序仍为 ground/ground_image_receiver.py，默认 TCP 端口 56010。一键地面程序已启动时不要再启动第二个接收器。

## 4. 保存格式和传输状态

地面目录：received_images/UAV3/<图片任务编号>/<文件名>.jpg 和同名 .json。

机载持久缓存：/home/amov/competition_development/image_cache/<图片任务编号>/。

JSON 保留原有任务、序号、相机话题、源时间戳、图片宽高等字段，并增加：

- image_stamp：原图秒与纳秒；detection_received_at_unix_ns：算法消息接收时间。
- bbox：原始中心和宽高；rendered_bbox：实际绘制角点，最大角点为闭区间。
- target_id、target_type、confidence、target_longitude、target_latitude、target_altitude。
- coordinate_system 为 WGS84；camera_frame_id、detection_frame_id、detection_topic。
- extra：解析后的扩展 JSON 对象，不覆盖无人机编号、令牌等协议字段。

新增字段随现有 TCP 元数据发送，在机载、地面和地面端汇总时保留；存入磁盘的 JSON 不包含 AuthToken。

```bash
rostopic echo /uav3/image_transfer/status
```

| 状态 | 含义 |
|---|---|
| waiting_for_frame | 已收到算法结果，等待完全相同时间戳的相机原帧 |
| frame_not_found | 缓存窗口内无法找到原帧，没有改用其他画面 |
| detection_rejected | 字段无效、frame_id 不一致、框完全越界或处理失败 |
| processing_queue_full | 待匹配或绘框队列已满，请求尚未落盘，需要上游处理 |
| cached | 带框 JPEG 和元数据已写入机载硬盘 |
| queued | 已进入原有 TCP 发送队列 |
| sent | 地面已保存并返回成功 ACK |
| pending_recovery | 图片已落盘，即时发送失败，等待补传 |
| reconcile_complete | 文件名清单核对和补传完成 |
| reconcile_retry_pending | 核对失败，等待下次重试 |

未匹配原帧或绘框前被拒绝的请求没有持久图片，不能靠断网补传恢复。默认绘框队列长度沿用 queue_size: 10，相机输入 30 FPS 不代表能够持续进行每秒 30 张图片的编码、磁盘写入和网络回传。

## 5. 任务控制与离线补传

竞赛模式下节点启动时不再自动创建图片任务。机载竞赛执行器收到地面端“一键起飞”指令后，会向图片任务控制话题发送 `start:<任务编号>`，图片任务从该时刻开始计时。手工联调仍可显式发送 start 指令；如需旧行为，可将 `start_on_node_start` 设置为 `true`。

```bash
rostopic pub -1 /uav3/image_transfer/mission_control \
  std_msgs/String "data: 'start:subject1-run-001'"
```

图片落盘后若 TCP 发送失败，保留磁盘缓存，默认即时发送抑制 5 秒；后台清单核对尝试间隔为 30 秒，第 19 分钟仍进行一次定时核对。机载发送当前任务 JPG 文件名，地面返回缺失文件名，机载补发完整图片和元数据，再次核对。每张补传图片默认最多尝试 3 次。

```bash
rostopic pub -1 /uav3/image_transfer/mission_control std_msgs/String "data: 'sync'"
```

补传仍针对当前图片任务，不会自动遍历全部历史任务。文件名清单核对行为未修改。

## 6. 兼容模式与测试

显式 input_mode:=paired_topics 保留旧图片/坐标双话题入口；input_mode:=capture_request 保留旧抓拍服务和指令入口。这两种模式均不再添加坐标文字，只有默认 timestamped_detection 模式使用新增算法目标框接口。

在具备 Python、NumPy 和 OpenCV 的环境运行：

```bash
python3 -m unittest discover -s src/su17_image_transfer/test -p 'test_*.py' -v
```

测试覆盖 30 FPS 缓存淘汰、纳秒精确匹配、跨话题乱序、过期拒绝、目标框裁剪、原帧不被修改、同帧多目标隔离、后台处理、真实 JPEG 编解码以及 TCP 断网补传的元数据完整性。ROS 边界在单元测试中使用桩；真实 ROS 编译、相机帧率和算法延迟需在机载环境验证。

