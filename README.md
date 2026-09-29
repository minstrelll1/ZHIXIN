# 竞赛系统最新部署与启动

## 一、部署

### 1. 地面端首次部署

在目标 Windows 电脑希望保存项目的父目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/minstrelll1/ZHIXIN/codex/portable-ground-deployment/tools/bootstrap_ground.ps1'))) -Destination (Join-Path (Get-Location) 'competition_development')"
```

首次部署会安装 Python 依赖，检查并使用仓库内的 MediaMTX，并提示在本机填写 AuthToken、PeerToken。

#### 使用 U 盘首次部署

将当前电脑项目中的以下内容复制到 U 盘，保留 `competition_development` 目录结构，再复制到目标电脑：

```text
competition_development/
├─ competition_backend/   排除 .venv、data、__pycache__
├─ competition_shared/
├─ config/
├─ src/
├─ tools/                 包含 local_tokens.ps1、local_tokens.env
├─ third_party/
├─ docs/
└─ README.md
```

不复制飞行记录、接收图片、日志、运行缓存及厂商工作区。目标电脑安装 Python 3.9～3.12（建议 3.11，并加入 PATH），在项目父目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\competition_development\tools\install_ground_station.ps1"
```

该命令创建本机 Python 环境并安装依赖；MediaMTX 和令牌使用已复制的文件。安装依赖仍需网络，完全离线时需另备 Python 安装包和依赖包。完成后按下方“地面端”命令启动。

### 2. 地面端仅更新代码

先关闭地面后端，在项目父目录执行。仅下载变化的文件，不重新安装 Python、MediaMTX；保留本机令牌、机队配置和数据：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\competition_development\tools\bootstrap_ground.ps1" `
  -Destination ".\competition_development" `
  -SkipInstall
```

旧版电脑首次启用增量更新时，改用以下命令一次；之后继续使用上面的本地命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/minstrelll1/ZHIXIN/codex/portable-ground-deployment/tools/bootstrap_ground.ps1'))) -Destination (Join-Path (Get-Location) 'competition_development') -SkipInstall"
```

更新失败详情自动保存在 `competition_development\ground_logs\update_*.log`。下载并校验全部成功后才替换文件。完成后重新启动地面后端并刷新网页。

#### 使用 U 盘更新代码

先关闭目标电脑的地面后端。在项目父目录执行，`E:` 替换为实际 U 盘盘符：

```powershell
$usbSource = "E:\competition_development"
$localProject = Join-Path (Get-Location) "competition_development"

foreach ($projectPath in @($usbSource, $localProject)) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectPath "tools\start_ground.ps1") -PathType Leaf)) {
        throw "项目目录不存在或不完整：$projectPath；请核对 U 盘盘符和当前所在目录。"
    }
}

New-Item -ItemType Directory -Force -Path "$localProject\ground_logs" | Out-Null

robocopy "$usbSource" "$localProject" /E /XJ /R:1 /W:1 /XD .git .venv __pycache__ data .runtime ground_runtime ground_logs flight_records received_images pointcloud_records position_tests onboard_source_backup .codex_backup* /XF fleet.json local_tokens.ps1 local_tokens.env mediamtx.exe auto.key auto.crt *.pyc *.bag *.log /TEE /LOG:"$localProject\ground_logs\usb_update.log"

if ($LASTEXITCODE -ge 8) { throw "USB 更新失败，请查看 ground_logs\usb_update.log" }
```

跳过未变化的文件，保留本机 Python 环境、MediaMTX、令牌、机队配置和采集数据。普通代码更新无需重复安装依赖；依赖清单发生变化时再运行安装命令。完成后重新启动地面后端并刷新网页。

### 3. 机载端部署

在地面端项目父目录执行对应无人机命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\competition_development\tools\deploy_onboard_stack.ps1" `
  -UavAddress "192.168.1.202" `
  -Model p600 `
  -SyncConfig
```

P600 机载地址：

|无人机|机载地址|地面图传网卡地址|地面互联地址|
|---|---|---|---|
|UAV1|192.168.1.202|192.168.1.230|192.168.2.202|
|UAV2|192.168.1.207|192.168.1.230|192.168.2.207|
|UAV3|192.168.1.212|192.168.1.230|192.168.2.212|
|UAV4|192.168.1.217|192.168.1.230|192.168.2.217|
|UAV5|192.168.1.222|192.168.1.230|192.168.2.222|
|UAV6|192.168.1.227|192.168.1.230|192.168.2.227|

各独立图传网络的地面网卡设为 `192.168.1.230/24`；地面互联网卡保持表中地址。

其他 P600 只替换 `-UavAddress`。SU17 机载地址为 `192.168.1.88`，地面图传网卡同样为 `192.168.1.230`，并将 `-Model p600` 改为 `-Model su17`。部署脚本不修改厂商工作区。

## 二、启动

### 1. 地面端

```powershell
cd .\competition_development
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\start_ground.ps1 -ConfirmLiveConfig
```

浏览器打开：

```text
http://127.0.0.1:8000/
```

进入网页后选择本机地面终端编号和任务发布角色。

### 2. 机载竞赛程序

确认允许竞赛程序控制飞行后启动：

```bash
cd ~/competition_development
bash ./tools/start_onboard_stack.sh --model p600 --expect-uav-id 1 --direct --enable-motion
```

SU17 将 `--model p600` 改为 `--model su17`。以下机载命令以 UAV1 为例，其他无人机将 `--expect-uav-id 1` 和 `uav_id:=1` 中的编号替换为本机编号 1～6。

### 3. 外部程序 B（聂天常）

```bash
source ~/recon_ws/devel/setup.bash
roslaunch px4_north_camera p600_gx40_position_pid_reconnaissance.launch \
  uav_id:=1 \
  flight_mode:=outdoor_small_range
```

### 4. 目标检测程序（边疆）

```bash
source ~/SpireCV_bj/src/spirecv-ros/devel/setup.bash
roslaunch spirecv_ros uav_yolo26_botsort_geolocation.launch uav_id:=1
```

### 网页起飞操作

规划并分派 → 确认任务回执 → 一键起飞预检 → 确认起飞。更新后的机载程序会自动解锁并进入 COMMAND_CONTROL，稳定到达任务高度后才启动程序 B 或本工程航线。遥控器保持开启，接管操作沿用厂商流程；本工程停止输出，不自动抢回控制权。本次更新需重新部署机载端，部署和启动命令不变。


### 自动启动前的首次 SSH 配置

新版在首次连接提示 `Permission denied` 时会自动弹出 SSH 授权窗口。输入一次机载 Ubuntu 密码，即可自动安装公钥并继续启动；密码不进入网页、不保存。取消后可点击网页“重新连接并启动”。每台地面电脑与每架无人机只需授权一次。

也可在项目目录手动执行（UAV1 示例）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_onboard_ssh.ps1 -UavAddress 192.168.1.202
```

按提示输入机载 Ubuntu 密码；以后启动不用再输入。

### 双击启动

1. 完成首次部署后，在项目目录双击 `智信竞赛.exe`；自动启动地面后端并用默认浏览器打开竞赛网页。重复双击复用已运行的后端。
2. 选择本地地面终端编号并确认。SSH 免密码登录可用时，自动启动对应无人机的竞赛机载程序、目标检测程序和自主飞行指令程序；编号随终端切换，UAV3 使用 `uav_id:=3`。
3. 点击“机地网络配置”右侧的程序名称查看打印内容。红色表示未启动、连接失败或异常，绿色表示对应 ROS 节点已响应（地面端表示网页服务运行中）。关闭网页不停止后台或机载程序。

目标检测使用 `~/SpireCV_bj/src/spirecv-ros/devel/setup.bash`；程序 B 使用 `~/recon_ws/devel/setup.bash`，启动参数为 `flight_mode:=outdoor_small_range`。这两个工作空间需已在无人机上安装。

启动管理脚本由地面端通过 SSH 自动传送。此次新增程序 B 的 `/uavN/target_scheduler/completed_targets` 话题兼容，需要按上面的机载更新命令同步竞赛机载代码；不修改目标检测、程序 B 或厂商工作空间。U 盘复制时同时复制根目录的 `智信竞赛.exe`。原有命令行启动方式仍可使用。


### 停止程序

- 关闭地面端：点击“竞赛程序地面端”→“停止程序”；或双击项目目录的 `stop_ground.cmd`，等待显示退出核验结果。关闭浏览器只关闭页面。
- 关闭机载端、目标检测或程序 B：点击对应程序名称→“停止程序”。停止并核验该程序及子进程；失败会明确提示，手动停止后不会自动重新拉起，可按原启动指令人工启动。
- 关闭地面端不会同时关闭机载程序。地面退出记录：`ground_logs/ground_stop.log`。

命令行关闭地面端（项目目录执行）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\stop_ground.ps1
```


### 科目一结果上报

任务发布端点击“科目一成果上报”，选择已回传任务或 JSON 文件 → 生成并校验 → 下载核对 → 确认上报。参赛队名默认“北方自控智群队”。接口采用 UTF-8 JSON 文件的 form-data 上传；图片另存本地，不自动上报。详见 [科目一上报说明](docs/subject1_reporting.md)。


### 科目一识别类别

展开“科目一识别类别” → 选择类别数和目标 → 保存类别 → 规划并分派。机载话题 `/uavN/competition/recognition_categories`（`std_msgs/Int32MultiArray`，保留最后一条）。本功能需同步更新竞赛机载端，原部署命令不变。详见 [类别编号与接入说明](docs/recognition_categories.md)。
