# 地面端可移植部署

## 一键部署

在目标 Windows 电脑打开 PowerShell，在希望保存项目的目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/minstrelll1/ZHIXIN/codex/portable-ground-deployment/tools/bootstrap_ground.ps1'))) -Destination (Join-Path (Get-Location) 'competition_development')"
```

`bootstrap_ground.ps1` 会下载代码和仓库内的 MediaMTX，使用项目根目录计算所有本地路径，并安装 `competition_backend` 的 Python 依赖。目标目录可以在任意盘符，不能要求 `F:\Projects\ZhiXin` 这样的固定路径。已有目录仅更新代码时使用 README 中的 `-SkipInstall` 命令：按文件校验后只下载变化内容，保留令牌、机队配置、日志、飞行记录、接收图片和现有第三方软件。旧版电脑需先执行一次在线增量更新入口。

地面端支持 Python 3.9～3.12。没有兼容版本时，脚本会尝试用 `winget` 安装 Python 3.11；如果电脑没有 `winget`，先安装 Python 3.11 再执行。

## 令牌

首次执行脚本会提示输入 AuthToken 和 PeerToken，并写入项目目录下的 `tools\local_tokens.ps1`。该文件已加入 `.gitignore`，不会进入 GitHub、部署包或日志。后续执行会保留已有文件。六个地面端使用同一个 PeerToken；机载端和对应地面端使用同一个 AuthToken。不要把令牌写入命令行历史或提交到版本库。

因此，一键部署包含令牌配置流程，但令牌值仍必须由部署人员在目标电脑上输入或以安全方式提供，GitHub 不保存令牌。

## 启动

```powershell
cd .\competition_development
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\start_ground.ps1
```

浏览器打开 `http://127.0.0.1:8000/`，选择本机地面终端编号和任务发布角色。终端编号、机型以及机地 IP 由仓库中的固定配置绑定。

## MediaMTX 和端口

默认端口已经写在 `config/fleet.json`，六台地面电脑可以使用相同的端口号，因为每台电脑分别监听：

|用途|默认端口|端口所在位置|
|---|---:|---|
|网页后端|8000|本机 Windows|
|任务/遥测 TCP|56100|本机 Windows，机载端连接|
|图片回传 TCP|56010|本机 Windows，机载端连接|
|MediaMTX RTSP|8554|本机 Windows，读取机载 RTSP|
|MediaMTX WebRTC|8891|本机 Windows，网页播放|
|ROSBridge/点云|9090|机载端或点云中继|

正常部署不需要额外确认端口。启动网页时选择的终端会从 `config/fleet.json` 读取本终端的端口和视频地址；MediaMTX 会根据模板自动生成 `ground_runtime\mediamtx_uavN.yml`。只有端口被其他程序占用或网络策略要求改端口时，才编辑 `config/fleet.json` 中对应终端的 `web_port`、`task_port`、`image_port`、`video_webrtc_port`、`rosbridge_port`，并确保机载配置使用同一个任务端口和图片端口。RTSP 源端口 `8554` 必须与无人机相机地址一致。

启动失败时先检查：

```powershell
Get-NetTCPConnection -LocalPort 8000,56100,56010,8554,8891 -State Listen -ErrorAction SilentlyContinue
```

不要把六台电脑的端口改成不同值来“避让”；它们位于不同电脑，保持固定映射更便于机载和地面配置一致。

## 机载端部署

地面电脑到无人机的首次 SSH 公钥配置和机载部署仍使用 `六机六地面端快速部署与启动.txt` 中的相对项目命令。机载厂商工作区（`p600_experiment` 或 `su17_experiment`）属于厂商代码，不上传、不修改；竞赛代码通过 `tools\deploy_onboard_stack.ps1` 同步并在机载端构建。

## 弱网和无人机重启后的恢复

保持地面端运行时，无人机重启后会重新检查开机标识、厂商心跳和基础连接，再自动启动本次开机的五个业务程序。SSH 超时、连接重置或回包不完整不会禁止本次开机的自动启动；地面端持续重试，恢复后先读取机载记录，再继续尚未执行的步骤。重试间隔逐步增加到最多 8 秒，单次普通 SSH 请求最多等待 45 秒；网络完全不可达时无法完成连接，链路恢复后继续。

每个程序通过机载持久记录、进程身份和启动锁核对，启动中途断线或回包丢失不会重复创建业务进程。自动补启动只适用于新版地面端创建的本次开机启动批次，并在机载端再次确认飞控已连接且未解锁。已经启动过后崩溃、人工停止、人工启动的已有会话，以及旧版无法确认进度的启动批次不会被自动重新拉起；真实指令配置错误仍在程序日志中显示，修正后可人工启动。

SSH 固定使用该无人机配置的图传网卡地址；管理脚本和回包压缩传输，状态轮询每个程序分批读取最多 8 KiB 日志，启动回执不携带日志正文。历史日志不删除，后续轮询继续读取。

此项修复需要各地面电脑更新并重启一次地面端服务。`tools/remote_programs.py` 由地面端在 SSH 请求时传送执行，不需要重新构建机载 ROS 工作区或修改厂商程序。竞赛任务分派、起飞、航点执行和外部程序 B 的接口保持不变。

## 发布要求

推送 GitHub 前只提交源代码、配置模板、部署脚本和 `third_party\mediamtx`；不要提交 `tools\local_tokens.ps1`、`tools\local_tokens.env`、`.venv`、日志、图片、bag 或飞行记录。MediaMTX 可随仓库发布，单个 Windows 可执行文件小于 GitHub 普通文件限制；PrometheusGroundStation 厂商软件不纳入本仓库，按厂商方式单独安装。
