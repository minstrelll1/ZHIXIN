# SU17 Competition Backend

六架 SU17 的竞赛任务编排后端。它负责规划、预检、起飞确认、任务启动和返航触发，不直接在地面网络上连续发送姿态或电机控制量。

## 当前闭环

```text
Web/地面端
  -> POST /plan                    区域六等分、生成弓字航线；在线飞机才接收分派
  -> 六机分别回传 task_received    确认已保存自己的子任务
  -> POST /takeoff/prepare         六机预检并生成短时确认令牌
  -> POST /takeoff/confirm         同时下发六个不同目标高度
  -> 每架机到达自己的高度后立即下发 execute_task，不等待其他飞机
  -> 任务完成 / 比赛超时 / 低电量
  -> 下发 return_home + land_after_return
```

后端通过 WebSocket `/ws/status` 每 0.5 秒推送全局任务和六机状态。所有状态迁移会追加到 `data/events.jsonl`，最新快照写入 `data/mission_snapshot.json`。

## 安全边界

- 默认 `COMPETITION_ADAPTER=sim`，只记录指令，不连接真机。
- 真机模式要求 `safety.production_config_confirmed=true`，否则预检拒绝起飞。
- 起飞使用“预检 + 15 秒有效确认令牌”两步接口，前端可以把它呈现为确认弹窗。
- 科目一、科目二支持任意凸四边形任务区和任意凸四边形降落区。网页可选择 `XYZ / ENU（米）` 或 `GPS（纬度、经度）`，两种坐标系都必须输入并可编辑两个区域的四个边界点；默认值已内置。
- 求解器在竞赛环境默认使用 `150m` 航线间距和 `5m` 转弯半径，在实验室环境默认使用 `0.5m` 和 `0.1m`；两组值都可在网页修改。算法搜索扫描方向，对每个候选方向用动态规划把连续覆盖条带分给六架无人机。目标先最小化最长单机飞行距离，再以总机队距离打破并列；距离包含进出降落区和带转弯半径的换线代价。
- 实验室高度依次为 `0.5、1.5、0.5、1.0、1.5、1.0m`；竞赛高度依次为 `40、50、40、45、50、45m`。
- 起飞确认前可以重复规划。每次规划都会生成新的 `mission_id`、废止旧确认令牌，并向本次在线的飞机重新发送任务；每架参与执行的飞机都必须针对新 `mission_id` 重新确认。
- 规划接口会先发送 `assign_task` 和完整任务的 SHA-256。机载执行器必须在校验、保存并 `fsync` 后向 `/uavN/competition/task_status` 发布 `task_received`、相同 `mission_id` 和相同 `assignment_checksum`，否则该机不能通过起飞预检。
- 分布式六地面端模式下，如果没有任何新鲜无人机遥测，规划接口仍会生成并展示完整六机结果，但 `active_uav_ids` 为空：不申请地面端控制租约、不下发 `assign_task`，起飞预检固定失败。这次规划仅供检查航线；无人机重新连接后再次点击规划，才会向已连接的飞机分派任务。
- 上述“仅规划”能力只改地面端的任务选择和页面展示，没有改变 TCP/ROS 分派报文或机载回执协议；已经部署支持 `task_received` 回执的机载执行器不需要因本项重新部署。
- 真机控制采用高层 ROS 指令。每架机必须部署机载任务执行器，订阅 `/uavN/competition/high_level_command`，并独立实现断网、低电和任务超时返航。地面后端断线不能成为飞行安全策略的唯一执行者。
- 示例高度和科目分工只是框架占位值，实飞前必须按赛场、地理围栏和飞机间隔重新标定。

## 安装与模拟启动

建议在 Python 虚拟环境中运行：

```powershell
cd F:\Projects\ZhiXin\competition_development\competition_backend
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m competition_backend.api
```

默认地址：`http://127.0.0.1:8000`，接口文档：`http://127.0.0.1:8000/docs`。

## API 顺序

规划科目一：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/plan `
  -ContentType application/json -Body '{"subject":"subject1"}'
```

## 任意四边形规划接口

前端的“规划并分派”会直接提交以下字段。四个点必须按边界顺时针或逆时针顺序输入，且区域必须凸；GPS 模式使用 `纬度、经度`，XYZ 模式使用 `X、Y（米）`。GPS 模式以任务区的 `P1` 作为本地 ENU 换算原点；XYZ 模式使用 `gps_origin` 将航线转成程序 B 所需的经纬度。

```json
{
  "subject": "subject1",
  "controller_mode": "external",
  "flight_profile": "competition",
  "gps_origin": {"latitude": 31.2304, "longitude": 121.4737, "altitude_m": 30},
  "search_area": {
    "coordinate_mode": "xyz",
    "points": [[0, 0], [900, 0], [1050, 600], [100, 500]],
    "lane_spacing_m": 150,
    "turn_radius_m": 5
  },
  "landing_area": {
    "coordinate_mode": "xyz",
    "points": [[30, -30], [180, -30], [180, 120], [30, 120]]
  }
}
```

`controller_mode` 与坐标选择相互独立：本工程自主控制收到本地 ENU 航点；外部程序 B 收到同一条规划航线转换出的 WGS84 三元组 `[纬度, 经度, 海拔]`，以及降落点 `[纬度, 经度, 海拔, 降落顺序]`。

### 为什么会出现 XYZ 与 GPS 的转换

航线间距、转弯半径、距离比较和多机分区都必须在“米”这个平面单位中计算。GPS 的纬度、经度是角度，且经度对应的实际距离随纬度变化，所以规划器会先把 GPS 四点投影为局部 ENU 米制坐标；这一步是为了正确求解几何距离，并不是要求用户改用 XYZ。

如果选择本工程自主控制并输入 XYZ，航点会一直保留为本地 ENU，不需要转 GPS。只有外部程序 B 的固定接口要求 WGS84 `[纬度, 经度, 海拔]` 时，后端才会把求解出的 XYZ 航点转换回 GPS；GPS 输入模式也会在完成米制规划后转换回 WGS84 下发给程序 B。

模拟六机遥测（UAV ID 依次改为 1 到 6）：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/sim/uavs/1/telemetry `
  -ContentType application/json `
  -Body '{"connected":true,"armed":true,"odom_valid":true,"control_state":2,"battery_percentage":0.9,"position":[0,0,0],"velocity":[0,0,0]}'
```

起飞预检：

```powershell
$ready = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/takeoff/prepare
```

确认起飞：

```powershell
$body = @{ token = $ready.confirmation_token } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/takeoff/confirm `
  -ContentType application/json -Body $body
```

手动要求全体返航：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/return `
  -ContentType application/json -Body '{"reason":"manual"}'
```

## 六台 Windows 地面电脑的分布式真机模式（推荐）

六台电脑运行完全相同的前后端，每台通过厂家配置好的图数传链路只直连一架无人机。例如
`ground-uav3` 的 `56100` 端口只接受 UAV3，网页也只公开本机 UAV3 的 MediaMTX 视频。六台电脑之间
通过交换机上的 HTTP 接口互传遥测、任务和图片清单，不修改网卡、图数传或厂家设置。

每台电脑后台持续同步六架无人机的遥测（连接状态、位置、速度、电量和任务状态），网页显示的在线集合就是这份共享缓存。任意一台电脑点击“规划”时直接读取缓存，只为当前已连接的 UAV 对应地面节点取得
30 秒互斥主控租约；主控在线期间每 5 秒续租，可阻止两台电脑同时发起飞行命令。随后主控把六份
规划结果保留在页面中，但只把已连接 UAV 的任务分别送到对应电脑，再由每台电脑现有的 TCP 链路
转发给自己的无人机。未连接的 UAV 标记为“仅规划，本次不执行”，不会阻塞实验室单机或部分多机联调。
主控任务状态会同步到其余电脑，因此六台网页都能看到规划航线、六机位置、速度和执行阶段；每台
网页仍只能播放本机无人机视频。

每台电脑的图片接收器只接收本机 UAV 的图片，先保存到本机 `received_images/UAVN`。点击规划的
主控电脑每 5 秒从其余电脑读取图片清单，自动下载缺失的 JPG/JSON，并核对大小与 SHA-256；写入时
使用临时文件、`fsync` 和原子改名。断网不会删除源图片，恢复后会继续补传。

先由厂家给出六台地面电脑在交换机上的地址，然后在所有电脑上使用同一份节点表和同一
`PeerToken`。下面仅以占位地址表示，必须替换 `GROUND_PC_N_IP`：

```powershell
cd F:\Projects\ZhiXin\competition_development
$AuthToken = "与六台机载执行器一致的长随机值"
$PeerToken = "仅六台地面电脑共享的另一个长随机值"
$Peers = "1=http://GROUND_PC_1_IP:8000;2=http://GROUND_PC_2_IP:8000;3=http://GROUND_PC_3_IP:8000;4=http://GROUND_PC_4_IP:8000;5=http://GROUND_PC_5_IP:8000;6=http://GROUND_PC_6_IP:8000"

# UAV1 对应电脑；其他五台只把 1 改成各自的 2～6。
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\tools\start_competition_backend_tcp.ps1" `
  -LocalUavId 1 `
  -GroundNodeId "ground-uav1" `
  -GroundPeers $Peers `
  -AuthToken $AuthToken `
  -PeerToken $PeerToken `
  -ImageAuthToken $AuthToken `
  -RecordingSshHosts "1=厂家配置的UAV1机载SSH地址" `
  -ConfirmLiveConfig
```

该脚本现在会自动启动三项服务：MediaMTX、本机 UAV 图片接收器、竞赛 Web 后端。MediaMTX 默认从
`F:\Applications\mediamtx_v1.20.1_windows_amd64` 读取 `mediamtx.yml`；若它已经运行，脚本复用
现有进程。脚本自己启动的辅助进程会在后端退出时一并停止。日志写入 `ground_logs`。可分别使用
`-DisableMediaMtx` 或 `-DisableImageReceiver` 禁用自动启动。

每台电脑的 `mediamtx.yml` 只需配置自己的路径名 `uavN`，网页默认读取
`http://127.0.0.1:8891/uavN/`。例如 UAV2 对应电脑应配置 `paths.uav2`，不要在一台电脑配置六路视频。8891 用于避开 PrometheusGroundStation 使用的 UDP 8889。

完整的逐机启动模板见 `docs/six_ground_computers_setup.txt`。

## 旧版单电脑 TCP 模式说明

当前启动脚本默认启用上面的分布式模式；旧版 `tcp` 适配器仍保留在代码中供兼容和测试使用。

```powershell
cd F:\Projects\ZhiXin\competition_development
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\tools\start_competition_backend_tcp.ps1"
```

后端监听网页端口 `8000` 和六机TCP端口 `56100`。每架机主动连接地面固定IP `192.168.1.123`，在同一连接上传遥测和任务状态、接收任务与控制命令。图片仍使用独立的 `56010` 端口。

搜索规划始终生成 UAV1～UAV6 的完整结果；`COMPETITION_ACTIVE_UAV_IDS` / `-ActiveUavIds`
只限制本次实际连接、任务分派、预检和控制范围。比如 `-ActiveUavIds "1"` 时页面仍展示六机规划，
但只有 UAV1 收到任务和运动命令，其余五机标记为“仅规划，本次不执行”。

科目一、科目二的网页始终提交任务区与降落区各四个边界点。XYZ 模式采用 `+X` 向北、`+Y` 向西的
本地米制坐标；GPS 模式采用纬度、经度，并以任务区 P1 作为换算原点。页面会按实验室或竞赛环境载入对应的
四边形、航线间距和转弯半径默认值，也可直接修改。规划结果会展示四边形边界、每机条带数、路径长度与降落顺序。

实验室高度方案（`flight_profile=lab`）不执行起飞前最低电量阈值检查；竞赛高度方案仍要求电量
不低于配置的 `preflight_battery_min`。两种方案都保留飞行中的低电量自动返航保护，默认返航阈值
为30%。

确认起飞后，规划图会把TCP遥测中的实时 `position[X,Y,Z]` 叠加到规划航线上：实线表示规划
航线、虚线表示浏览器本次任务累计的实际XY轨迹、圆点和标签表示无人机当前XY坐标，箭头表示
实时 `velocity[X,Y]` 的方向和水平合速度。轨迹按任务
编号隔离，新规划会清空旧轨迹；浏览器刷新也会清空页面内轨迹，但机载rosbag记录不受影响。

六机状态卡片均可点击。详情弹窗左侧显示该机主摄视频，右上显示六机完整规划、实际位置、轨迹和
速度矢量，右下显示所选无人机的XYZ位置、XYZ速度、XY合速度、执行阶段和遥测延迟。按关闭按钮
或 Esc 可退出。视频和控制TCP相互独立；关闭弹窗会停止浏览器加载当前视频。

浏览器不能直接嵌入 PrometheusGroundStation 的 Qt 窗口，也不能原生播放 RTSP/RTMP。需要把同一
主摄画面提供为 HTTP MJPEG、浏览器可播放的视频文件流，或可嵌入的 HTTP 视频页面，然后启动时
配置 `-VideoSources`。各机用分号分隔，例如：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\tools\start_competition_backend_tcp.ps1" `
  -ActiveUavIds "1,2" `
  -VideoSources "1=http://192.168.1.88:8080/stream?topic=/uav1/gimbal/image_original;2=http://192.168.1.89:8080/stream?topic=/uav2/gimbal/image_original"
```

上述 `/stream?topic=...` 是 ROS `web_video_server` 的常见 HTTP MJPEG 地址格式，只有对应机载服务
已经启动时才有效。未配置或视频断流不会影响遥测、任务分派和飞行控制。

如需共享令牌，两端必须使用完全相同的值：Windows脚本使用 `-AuthToken`，机载launch使用 `auth_token:=...`。

## 科目三点云接收与网页展示

RViz 是独立的可视化客户端，本身不会把渲染画面或点云自动转发给网页。当前机载启动脚本已经启动
`rosbridge_websocket`（默认 `9090`），地面后端通过它订阅 ROS `sensor_msgs/PointCloud2`。
原始 FAST-LIO 话题 `/uavN/mid_point_cloud_centers` 可能很大，不建议在竞赛链路上按原始频率传输。
Prometheus 机载通信模块已有 `ReduceTheFrequency` 链路：在 UAV 通信对象创建后，它会把
`/uavN/octomap_point_cloud_centers` 缓存并以约 1 Hz 发布到
`/uavN/octomap_point_cloud_centers/reduce_the_frequency`。地面端应优先订阅这个降频后的普通
`PointCloud2` 话题；它仍然保留标准 XYZ 字段，网页端可以直接解析。

launch 文件中的 `rviz_port=8890` 目前不是可用的点云传输接口：通信桥源码没有启用该参数，也没有
实现 RViz socket。Prometheus 的控制/状态链路使用 UDP 8889、TCP 55555/55556；点云仍应通过
机载 `rosbridge_websocket` 9090（或另行实现的压缩中继）传输。RViz 本身是显示端，不会把点云
“转发”给网页端。

### 复用 PrometheusGroundStation 已有点云链路（默认）

`groundstation_shared` 是默认模式。竞赛后端不会连接机载或本机的 rosbridge `9090`，所以不会生成第二个随机
本地端口，也不会产生第二份机载到地面的点云流量。GroundStation 已建立的点云会话是唯一空中链路；
`tools/forward_groundstation_pointcloud.py` 只订阅 GroundStation 电脑已有的本地 ROS 话题，并通过已在使用的
网页后端端口 `8000` 写入网页缓存和 PCD 存储。

地面后端无需填写机载点云 IP：

```powershell
-PointCloudSource groundstation_shared `
-PointCloudRelayUavIds "3" `
-PointCloudIngestToken "与 AuthToken 相同的值"
```

在 PrometheusGroundStation 所在电脑、且已有该 ROS 话题的终端运行一次，例如 UAV3：

```bash
python3 tools/forward_groundstation_pointcloud.py \
  --uav-topics '3=/uav3/octomap_point_cloud_centers/reduce_the_frequency' \
  --backend http://127.0.0.1:8000 \
  --token '与 PointCloudIngestToken 相同的值' \
  --hz 1
```

Prometheus 的 GroundStation 通常订阅的是带 `/compressed` 后缀的 PCL Octree
话题。该消息不是网页端可以直接解析的普通 XYZ `PointCloud2`。在同一台
GroundStation 电脑上先启动本工程提供的 ROS 解压节点（不修改 SU17 源码）：

```bash
roslaunch su17_pointcloud_bridge decompress_uav.launch uav_id:=3
```

节点把 `/uav3/.../compressed` 解码并发布为普通的
`/uav3/octomap_point_cloud_centers/reduce_the_frequency`；随后运行上面的
`forward_groundstation_pointcloud.py`，订阅这个无 `/compressed` 后缀的话题。
这样仍复用 GroundStation 已建立的唯一机载会话，网页端收到的是标准 XYZ 点云。

这不是机载部署项，也不会打开 9090 的第二个客户端连接。它要求 GroundStation 本机 ROS 图中已有该
`PointCloud2` 话题；如果 GroundStation 不提供本机 ROS 话题，需要在它的点云接收模块中调用同一
`POST /api/v1/pointcloud/{uav_id}/ingest` 接口，不能由网页后端直接复用另一个进程的 10628 TCP 会话。

### 旧本机 rosbridge 中继（兼容）

仅在已经运行独立本机 rosbridge 中继时使用 `groundstation_relay`。它仍会新建一个本机 WebSocket 客户端，
因此不满足“与 10628 使用同一会话”的要求。

### 直接连接机载 rosbridge（兼容模式）

没有 GroundStation 本机中继时，才配置机载 ROSBridge 地址：

```powershell
$env:COMPETITION_POINTCLOUD_SOURCE = "onboard"
$env:COMPETITION_POINTCLOUD_ROSBRIDGE_HOSTS = "1=192.168.1.88;2=192.168.1.89"
$env:COMPETITION_POINTCLOUD_TOPICS = "1=/uav1/octomap_point_cloud_centers/reduce_the_frequency;2=/uav2/octomap_point_cloud_centers/reduce_the_frequency"
$env:COMPETITION_POINTCLOUD_ROSBRIDGE_PORT = "9090"
```

后端按每架机约 1 Hz 保存降采样后的二进制 PCD，默认保留每架机最近 300 帧，目录为
`pointcloud_records/UAVN/`。接口 `/api/v1/pointcloud/status` 返回接收状态，
`/api/v1/pointcloud/{uav_id}/latest` 返回网页点云，`/api/v1/pointcloud/{uav_id}/pcd` 下载最近 PCD。

如果未启动 Prometheus 的 UAV 通信对象（因此没有 `ReduceTheFrequency` 节点），才使用原始话题
或直接配置 `/uavN/octomap_point_cloud_centers`。后端仍会通过 rosbridge 的 `throttle_rate`、
单帧 XYZ 抽样上限和 PCD 保存间隔限流；这些措施不能替代机载端的降频，因为在 rosbridge 之前
原始 ROS 消息仍会占用机载 CPU 和 ROS 内存。
科目三的点云只显示在各无人机实时详情窗口，并替换该窗口原有的规划航线图；支持左键旋转、右键或中键平移、滚轮缩放和一键重置。科目一、科目二继续显示原有规划航线图。

## 每架无人机的机地分项通信量

每张无人机状态卡和详情面板显示该机的机地总实时速度，单位为 Mbps。点击后会同时看到云台视频、图片回传、RViz 点云、Prometheus、任务/遥测和其他机地通信的分项速度，以及活动端口明细。已知端口包括：9090（ROSBridge 点云）、1234/8554（云台 RTSP）、55555/55556（Prometheus TCP）、8889（Prometheus UDP）、56100（任务/遥测）和 56010（图片回传）。

TCP 字节由 Windows 全进程扩展统计读取，并按无人机机载 IP 和摄像头 RTSP 地址归属；使用 GroundStation 本机点云中继时，点云采集器会额外上报回环 WebSocket payload，计入 RViz 点云和总量。

启动参数使用 UAV_ID=机载IP，例如：

    -UavTrafficHosts "3=192.168.1.88"

start_ground_node.ps1 在 UavSshHost 为 IPv4 时会自动使用同一个地址，所以 UAV3 的常用启动方式无需额外填写此参数。Windows 读取全进程 TCP 字节计数需要管理员权限；请以“管理员身份运行”的 PowerShell 启动地面端。未提升权限时，网页会显示“需管理员启动”。

Prometheus UDP 8889 无法由 Windows TCP API 取得字节数；要把该 UDP 分项也纳入精确总量，需要让 GroundStation 或抓包采集器调用 TrafficMonitor.record_application_bytes 上报字节数。当前未上报时会显示为 0，不会伪造数据。交换机上其他设备的流量仍不计入无人机总量。

GroundStation 或其他本地采集器可以向 POST /api/v1/traffic/report 上报：

    {"uav_id": 3, "category": "prometheus", "received_bytes": 1280, "sent_bytes": 0}

上报值会按下一次采样换算为 Mbps，并同时加入 Prometheus 分项和该 UAV 的总量；需要鉴权时设置 COMPETITION_TRAFFIC_REPORT_TOKEN，并在请求中携带 X-Traffic-Token。

启动脚本在绑定网页端口前会检查并停止仍残留的本工程后端进程，然后等待端口释放；若端口由其他程序占用，会报告进程和 PID，不会强制结束该程序。

## 网页飞行数据采集

指挥台提供“开始采集”“停止并生成报告”“打开报告”和“删除机载备份”按钮。采集沿用
`tools/record_su17_flight.ps1`：机载端低内存记录诊断话题，停止后校验 bag、导出 CSV、复制到
`flight_records` 并生成交互式 MID360 HTML。图像和点云不会进入 bag。

网页流程不会接受或保存机载密码。必须先为运行后端的 Windows 用户配置 SSH 密钥；后端使用
`BatchMode=yes`，密钥不可用时立即拒绝开始采集。UAV1 默认 SSH 地址为 `192.168.1.88`。
多机地址可在启动时配置，例如：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\tools\start_competition_backend_tcp.ps1" `
  -ActiveUavIds "1,2" `
  -RecordingSshHosts "1=192.168.1.88,2=192.168.1.89"
```

网页停止采集后，机载备份默认保留。只有 PowerShell 流程返回成功且 HTML 已生成时，界面才启用
删除按钮；点击按钮后直接删除本次经过路径安全检查的机载备份。

## ROS 真机模式（兼容保留）

在能够访问六机 ROS Master/桥接话题的 Ubuntu 计算机上：

```bash
source /opt/ros/noetic/setup.bash
source /home/amov/su17_experiment/devel/setup.bash
export COMPETITION_ADAPTER=ros
export COMPETITION_CONFIG=/path/to/competition.production.json
python3 -m competition_backend.api
```

ROS 接口：

- 输入：`/uavN/prometheus/state`
- 输入：`/uavN/prometheus/control_state`
- 输入：`/uavN/competition/task_status`
- 输出：`/uavN/competition/high_level_command` (`std_msgs/String` JSON)

机载执行器收到 `assign_task` 后，应按 `mission_id` 保存其中的 `task.bounds_m`、`task.waypoints_m` 与 `target_altitude_m`，成功后回传 `task_received`。收到 `execute_task` 后才开始沿已保存航点飞行。

## 测试

```powershell
cd F:\Projects\ZhiXin\competition_development\competition_backend
python -m pip install -e ".[test]"
python -m unittest discover -s tests -v
```
