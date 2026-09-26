# 科目三：Windows 接收 GroundStation 已有点云

## 本次确认的部署

| 设备或编号 | 用途 |
| --- | --- |
| Windows `192.168.1.123` | PrometheusGroundStation 和竞赛网页后端 |
| Ubuntu `192.168.1.88:9090` | 机载 ROSBridge |
| `/uav1/octomap_point_cloud_centers/reduce_the_frequency/compressed` | 抓包确认的机载点云话题 |
| 网页 UAV3 | 本机对应的竞赛编号，与 ROS 命名空间分开设置 |

数据路径：机载 ROSBridge → GroundStation 原有 TCP 连接 → Windows 本机复制接收包 →
TCP/WebSocket 重组 → PCL XYZ 解压 → 竞赛后端缓存/PCD → 科目三三维点云。

接收器使用 Windows 原始套接字，因此启动终端需要管理员权限。不需要安装 Npcap、
Wireshark、Windows ROS 或 PCL，也不需要在机载端运行新的解压或 HTTP 转发节点。
它不会发送新的 ROSBridge 订阅或向机载端建立连接。

本次不修改、不部署任何 `su17_experiment` 文件。
此前额外启动的 `decompress_uav.launch` 和 `forward_groundstation_pointcloud.py`
可在各自终端按 `Ctrl+C` 停止；机载原有 SU17、雷达、定位等进程继续保持原配置。

## 启动

1. Windows 中打开 PrometheusGroundStation，连接飞机并开启点云显示。
2. 如果旧竞赛后端正在运行，先在对应终端按 `Ctrl+C`。新脚本遇到端口占用会提示退出。
3. **以管理员身份打开 PowerShell**，执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "F:\Projects\ZhiXin\competition_development\tools\start_groundstation_pointcloud.ps1"
```
脚本已内置固定的 AuthToken 和 PeerToken，不需要再输入；令牌不会输出到终端日志。

终端编号不再作为启动参数传入。启动网页后只选择本机地面终端编号；机型、机载 IP、地面 IP、RTSP 和 ROS 话题均来自固定机队配置。六台 P600 的固定映射见 `config/fleet.json`，任务发布端才能修改并同步。

这次联调入口会在同一个地面终端中启动竞赛后端、点云接收、MediaMTX 主相机视频服务和图片接收服务。启动后请保持该 PowerShell 窗口运行。

主相机默认从 `rtsp://192.168.1.99:1234/test.sdp` 拉流，网页 UAV3 视频地址为 `http://127.0.0.1:8891/uav3/`。如果现场 RTSP 地址不同，可在启动命令末尾增加 `-VideoRtspSource "rtsp://实际地址"`。
启动脚本会拒绝重复的旧 `forward_groundstation_pointcloud.py`、重复 MediaMTX 进程和已占用的图片接收端口，避免同一无人机的点云或图像被两条链路同时转发。
完整竞赛启动仍可使用 `start_competition_backend_tcp.ps1`，增加以下参数以启用同一接收模块：

```powershell
-LocalUavId 3 -PointCloudSource groundstation_shared -PointCloudRelayUavIds "3" -PointCloudTopics "3=/uav1/octomap_point_cloud_centers/reduce_the_frequency/compressed" -PointCloudCaptureLocalIp "192.168.1.123" -PointCloudCaptureRemoteIp "192.168.1.88" -PointCloudCaptureUavId 3
```

保留此 Windows 后端终端。浏览器打开 <http://127.0.0.1:8000/>，选择科目三，点击 UAV3 卡片。
收到完整有效帧后，右侧显示可旋转、平移和缩放的点云，以及原始点数、显示点数和更新时间。
点云只读查看不需要规划或起飞。

若设备地址或 ROS 话题编号改变，由任务发布端修改 `config/fleet.json`，同步后重启地面和机载竞赛程序；不再使用临时 IP 或 ROS 编号覆盖参数。

## 接收状态与排查

浏览器直接查看 <http://127.0.0.1:8000/api/v1/pointcloud/status>，或在另一个临时 PowerShell 查询：

```powershell
(Invoke-RestMethod http://127.0.0.1:8000/api/v1/pointcloud/status).capture
```

机地全链路实时通信和重复链路检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/communication/status | ConvertTo-Json -Depth 12
```

该状态覆盖任务/遥测 TCP、图片回传 TCP、Prometheus TCP/UDP、点云、云台 RTSP、地面本机 WebRTC 以及六台地面电脑之间的汇聚链路。`duplicate_check.status=warning` 或 `duplicates` 非空时，说明点云或视频存在重复配置/活动连接；`status=ok` 表示没有检测到重复点云或视频链路。Windows TCP 统计由管理员进程读取，Prometheus UDP 字节数需由对应中继通过 `/api/v1/traffic/report` 上报。

- `running=true`：Windows 网卡接收已启动，尚不能单独证明有有效点云。
- `packets` 增长：收到指定 IP/9090 端口的数据包。
- `messages` 增长：已恢复完整 WebSocket JSON 消息，可能包含其他 ROS 话题。
- `decoded_frames` 增长：目标话题已经解压并存入网页后端。
- `error`：权限、网卡地址或解压错误，网页会显示同样的提示。
- `gap_resets`：发生缺包并已丢弃对应残帧，等待之后的完整独立帧。
- `skipped_frames`：解压来不及时跳过排队的旧帧，保留最新帧以限制内存和延迟。

如果 `packets=0`，先确认 GroundStation 正在显示点云，以及所选网卡、机载地址和端口正确。
如果有数据包和消息但没有有效点云，核对 GroundStation 是否订阅上述 `/uav1/.../compressed` 话题。
若提示需要管理员权限，请关闭此次后端后从管理员 PowerShell 启动。
若浏览器提示拒绝连接且 8000 没有监听，先看后端终端的启动错误。

默认每秒保存一次 PCD，最多保留 300 帧，每帧默认显示/保存不超过 20,000 点；
原始点数来自完整解压校验。最近保存的 PCD 可从
<http://127.0.0.1:8000/api/v1/pointcloud/3/pcd> 下载。
超过 5 秒没有新帧时，网页明确提示保留上一帧，不把静态旧点云显示为正常更新。

## 本次抓包验证与实现范围

本次 `pointcloud.pcapng` 恢复出 30 条完整压缩点云消息：

- 主地图：15 帧，每帧 18,608～19,185 点。
- 膨胀占据地图：15 帧，每帧 27,645～27,728 点。
- 30 帧的八叉树、叶节点点数、XYZ 差分长度和最终消费字节数均通过校验。
- 结尾 17,520 字节属于不完整帧，不会作为点云提交。
- 首帧完整 XYZ 与使用 PCL 官方静态熵解码代码的 C++ 参考程序逐字节相同。

网页本次选择主地图，不把两类地图混成同一帧。解码支持当前 SU17 发布的无颜色独立 I 帧；
其他颜色配置、依赖前帧的 P 帧、超过 200 万点或不完整的帧会明确报错。
这是对 PCL 压缩结果的解码，不能消除编码前已有的有损量化。
实时抓取仅支持 IPv4 明文 WebSocket；不解密 WSS，也不重组 IPv4 分片。

离线再次验证抓包（不启动服务、不向机载端发送数据）：

```powershell
.\competition_backend\.venv\Scripts\python.exe -B .\tools\inspect_groundstation_pointcloud.py --pcap "$env:TEMP\ZhiXinPointCloudCapture\pointcloud.pcapng"
```

仅开发验证时，可以显式增加 `--replay-backend http://127.0.0.1:18000` 向独立测试后端回放。
测试后端应设置 `COMPETITION_ADAPTER=sim`、匹配的话题映射和独立数据目录；
如果启用了接收令牌，通过环境变量 `COMPETITION_POINTCLOUD_INGEST_TOKEN` 提供。
回放保留原 ROS 时间戳，网页始终显示“历史抓包回放”，不能作为实时接收成功的证据。

单元测试（使用人工构造样本，不包含实际飞行数据）：

```powershell
cd F:\Projects\ZhiXin\competition_development\competition_backend
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_groundstation_capture.py -v
```
