# P600 / SU17 共用竞赛程序

适用：六套独立机地链路，每台地面电脑直连一架飞机；机队可以混用 P600 和 SU17。

## 配置与身份

`config/fleet.json` 是六架飞机共用的配置。当前六架 P600 的机载 IP、机地直连网卡、地面互联 IP、RTSP 地址和终端编号已经固定；P600 的机地直连网卡默认是 `192.168.1.230`，地面互联默认是 `192.168.2.202`～`.227`。SU17 的机载 IP 固定为 `192.168.1.88`，地面机地网卡统一使用 `192.168.1.230`。无人机编号、飞控 `MAV_SYS_ID`、Prometheus 消息的 `uav_id` 和 `/uavN` 必须一致。

网页首页的“机队配置”入口可修改、导入、导出配置。保存使用 PeerToken 验证，令牌不保存在浏览器或导出的 JSON 中。配置修改后明确显示“等待重启”，执行任务或收到已解锁遥测时拒绝修改。已有返航功能不受等待重启的状态限制。

各地面端、机载端必须使用同一份配置。连接时校验固定的 UAV 编号、机型和 ROS 命名空间，拒绝串机；`device_id` 只作为诊断信息上报，不再作为人工配对或启动阻塞条件。更换飞机或机型后仍需由任务发布端修改固定配置并重启相关程序。

| 字段 | 用途 |
| --- | --- |
| `model` | `p600` 或 `su17`，每架单独选择 |
| `onboard_host` | 当前地面电脑能访问的机载 IP |
| `ground_host` | 当前飞机能访问的配对地面机地网卡 IP；P600 默认为 192.168.1.230，SU17 使用现场的 192.168.1.230 |
| `peer_host` | 其他地面电脑能访问的地面互联 IP；P600 默认为 192.168.2.202～.227，可与机地网卡不同 |
| `device_id` | 机载上报的诊断标识，不再作为人工启动绑定条件 |
| `video_rtsp_source` | 本架相机的实际 RTSP 地址，不按机型猜测 |
| `pointcloud_source` | 默认 `groundstation_shared`，复用 GroundStation 接收流；`onboard` 为直接订阅，二者择一 |
| `max_speed_mps` | 经实际核验的机载速度上限，默认 1 m/s；起飞前另读飞控 `MPC_XY_VEL_MAX`，取更低限制 |
| `max_distance_m` | 航点到起飞锚点的允许距离；还须满足厂商程序原有围栏 |

## 地面端

在 Windows PowerShell 中执行一条命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "F:\Projects\ZhiXin\competition_development\tools\start_ground.ps1"
```

不再使用 `-GroundTerminalId`、`-LocalIp`、`-OnboardIp` 或 `-RosUavId` 临时覆盖参数。启动后在网页只选择本机地面终端编号和是否为任务发布端；机型、机载 IP、地面 IP、RTSP 和端口均从固定机队配置自动读取。

仅核对配置、不启动服务：在命令末尾加 `-CheckOnly`。IP 未定时可启动网页配置页；未配置相机地址时不启动 MediaMTX，也不显示虚假的可用视频地址。默认不解除真机起飞锁；确认实际配置后使用 `-ConfirmLiveConfig`，网页仍保留起飞预检与二次确认。

复用 GroundStation 点云且已经配置机地网卡时，需管理员 PowerShell。GroundStation 应打开对应飞机的点云显示。直接 ROSBridge 模式需要机载端已有服务和对应话题；不要同时运行旧的独立点云转发器。

原有固定 AuthToken/PeerToken 已迁移到本机 `tools/local_tokens.ps1`；机载使用 `tools/local_tokens.env`。它们已加入忽略规则，通用地面打包工具不打包这些本地认证文件。复制程序给新的地面电脑时另行配置这两个环境变量或本地文件。

## 机载端

源程序依然放在 `~/competition_development`。厂商程序先按原来的方式启动，竞赛程序只读取对应厂商工作区已有的 ROS 环境。两种 `UAVState` 消息的 MD5 不同，所以在各自机载电脑上加载各自版本，再转换为统一 JSON/TCP，地面端不混载两种 ROS 消息。

首次或代码更新后，仅编译竞赛包：

```bash
cd ~/competition_development
bash tools/build_onboard.sh --model p600
 bash tools/start_onboard_stack.sh --model p600 --expect-uav-id 1 --direct --check
```

SU17 将 `p600` 换为 `su17`。编译产物分别保存到 `build_p600/devel_p600` 和 `build_su17/devel_su17`。不会编译或改动厂商源码。如果厂商目录不是默认的 `~/<机型>_experiment`，先设置 `COMPETITION_VENDOR_WORKSPACE` 为实际绝对路径。

严格核验可以使用不带 `--direct` 的 `--check`，它会读取飞控参数、消息版本和 Prometheus 状态话题；不会发送飞行指令。若厂商 ROS 尚未发布状态话题，可使用 `--direct --check`，只按固定机队配置检查并跳过 ROS/MAVROS 访问。固定 IP、UAV 编号和机型已写入机队配置，同一份 `fleet.json` 上传到所有使用中的机载端即可；设备标识不需要再填回配置。

普通启动用于遥测、任务接收、图片回传检查，飞行控制默认关闭：

```bash
bash tools/start_onboard_stack.sh --model p600 --expect-uav-id 1 --direct
```

完成配置和地面联调后，需要执行飞行任务时再加 `--enable-motion`。`--direct` 按已经固化的 `fleet.json` 启动，不等待 `/uavN/prometheus/state`；`--expect-uav-id` 必须填写，用于选择本机固定编号。若要严格确认飞控实际编号，去掉 `--direct`，但厂商 ROS/MAVROS 必须已经发布状态话题。

Windows 部署工具现在支持选择机型，例如：

```powershell
.\tools\deploy_onboard_stack.ps1 -UavAddress "实际机载IP" -Model p600 -SyncConfig
```

该工具只上传 `competition_development` 内的竞赛包、共用模块、启动文件和认证文件，然后在独立竞赛目录编译。未加 `-SyncConfig` 时保留机载已有的机队配置；首次部署才自动复制配置。不启动飞行或厂商程序。

## 各功能的数据接口

| 功能 | 统一接口与运行条件 |
| --- | --- |
| 遥测 / 起飞 / 返航 / 任务缓存 | `/uavN/prometheus/state`、`control_state`、`command`；保留任务校验和、断线恢复、低电量与时间限制 |
| 相机原始图像 | `/uavN/gimbal/image_original` |
| 算法与回传缓存共用图像 | `/uavN/competition/image_stamped`；有效源时间戳保持不变，零时间戳补为适配器接收时刻 |
| 算法目标结果 | `/target_stcheduler/complemend_targets`，`CompletedTargetArray`，与已对接消息字段一致 |
| 图片回传与科目一提交 | 仍按图像时间戳精确匹配，缓存 0.5 秒/30fps 最多 15 帧；画目标框、保存 JPEG/元数据与提交 JSON，保留断线补传 |
| 点云 | `/uavN/octomap_point_cloud_centers/reduce_the_frequency/compressed`；共用原有解码与网页显示链路 |
| 视频 | 每架配置实际 RTSP，地面 MediaMTX 转为浏览器视频；浏览器远程访问地面网页时替换本机回环地址 |
| 外部飞行算法 B | `/uavN/competition/external_mission`、`external_status`；路径 `/ground_mission_planner/vehicle_N/path_stage_1`、降落点 `/ground_mission_planner/vehicle_N/jiangluodian`；GPS 模式为 `[经度,纬度,相对起飞点高度]`，XYZ 模式为 `[x,y,z]`；GPS 不叠加海拔 |

必须让检测算法订阅 `image_stamped` 并原样回传其 `image.header.stamp`。只让回传节点订阅适配图像，而算法继续使用零时间戳原图，无法完成匹配。可以通过算法启动参数或 ROS remap 切换订阅，不需要改动厂商源码。接收时刻不是相机曝光时刻；若算法要求严格的曝光时间，应选择 `require_source`，并使用能提供原始时间戳的相机驱动。

地图任务的 `LOCAL_NORTH_WEST` 航点需要同时带 WGS84 经纬度。机载程序在起飞时记录 GPS/RTK 与本地 ENU 锚点，转换航点后保存；不会把地图原点当成飞控原点。此模式高度相对起飞位置计算。室内原有 ENU 任务的坐标语义保持不变。GPS 无效或定位源为室内 SLAM 时拒绝地图任务。

悬停扫描任务在到达航点后保持设定时长，默认还要求算法明确确认扫描完成。内部执行器发布：

```json
{"uav_id":3,"mission_id":"实际任务编号","waypoint_index":0,"request_id":"本次扫描唯一编号","duration_s":10}
```

请求话题 `/uav3/competition/scan/request`，类型 `std_msgs/String`；扫描算法在 `/uav3/competition/scan/status` 回传同样的 `uav_id`、`mission_id`、`request_id` 以及 `"state":"completed"`。`failed` 或超时会停止推进航点。旧扫描回执不能完成新航点；`timed_hover` 仅适合测试等待，不证明实际光学扫描已经完成。

外部 B 的 JSON 任务包含坐标系、航速、扫描模式、每航点动作和扫描时长；比赛可使用 WGS84 经纬高或 XYZ/ENU，实验室使用本地 ENU 的 `x_m、y_m、z_m`。实验室区域沿用比赛多边形形状并按比例缩放到最大 3m × 3m，实际区域不固定为 3m × 3m。内部任务的 `waypoints_wgs84` 仍按 `[纬度, 经度]` 存储；程序 B 的两个 GPS 数组话题统一按 `[经度, 纬度, 相对起飞点高度]` 发布。JSON 的 WGS84 航点和返航点使用具名字段 `latitude`、`longitude`、`altitude_m`。旧的 `Float64MultiArray` 路径只作兼容输出，不能单独表达扫描动作。外部 B 必须处理 JSON 中的动作并报告进度。当前拷贝的厂商目录不包含完整的 `recon_ws`/`paotou_ws` 算法工程，其具体扫描/避障/投放行为需要与该算法实机联调。

## 验证范围

本次在 Windows 执行后端、TCP 混合机型路由、设备冲突、配置保存、图像绘框/JPEG/补传、任务协议、坐标转换和扫描回执测试；ROS 边界使用测试替身，不把这些测试等同于实飞。

尚需真实 P600/SU17 验证：ROS 编译及话题订阅、相机帧率、算法时间戳回传、实际点云帧、RTSP 地址、GPS/ENU 方向、飞控限速、扫描算法确认和外部 B 行为。现有固定区域规划仍保留预览流程，不在这次机型适配中自动开启实飞分派。


