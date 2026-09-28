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

|无人机|机载地址|图传网卡地址|地面互联地址|
|---|---|---|---|
|UAV1|192.168.1.202|192.168.1.230|192.168.2.202|
|UAV2|192.168.1.207|192.168.1.230|192.168.2.207|
|UAV3|192.168.1.212|192.168.1.230|192.168.2.212|
|UAV4|192.168.1.217|192.168.1.230|192.168.2.217|
|UAV5|192.168.1.222|192.168.1.230|192.168.2.222|
|UAV6|192.168.1.227|192.168.1.230|192.168.2.227|

各地面电脑连接图传模块的网卡设置为 `192.168.1.230/24`；地面互联网卡保持表中地址。

其他 P600 只替换 `-UavAddress`。SU17 使用实际机载地址，并将 `-Model p600` 改为 `-Model su17`。部署脚本不修改厂商工作区。

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

核对实机配置后使用上述命令；`-ConfirmLiveConfig` 表示操作员已确认配置，不代替定位、遥测、任务回执等预检。进入网页后选择本机地面终端编号和任务发布角色。更改启动参数时，先在旧后端终端按 `Ctrl+C`，再重新启动。

### 2. 机载厂商程序

P600：

```bash
source /opt/ros/noetic/setup.bash
source ~/p600_experiment/devel/setup.bash
roslaunch p600_experiment P600_outdoor_onboard.launch uav_id:=N
```

SU17：加载 `/opt/ros/noetic/setup.bash` 和 `~/su17_experiment/devel/setup.bash`，再启动厂商提供的 Prometheus/MAVROS 启动文件。

### 3. 机载竞赛程序

先检查固定绑定：

```bash
cd ~/competition_development
bash ./tools/start_onboard_stack.sh --model p600 --expect-uav-id N --direct --check
```

检查通过后启动（飞行控制关闭）：

```bash
bash ./tools/start_onboard_stack.sh --model p600 --expect-uav-id N --direct
```

确认允许竞赛程序控制飞行后启动：

```bash
bash ./tools/start_onboard_stack.sh --model p600 --expect-uav-id N --direct --enable-motion
```

SU17 将上述命令中的 `--model p600` 改为 `--model su17`；`N` 为 1～6 的无人机编号。

### 网页起飞操作

规划并分派 → 确认任务回执 → 一键起飞预检 → 确认起飞。更新后的机载程序会自动解锁并进入 COMMAND_CONTROL，稳定到达任务高度后才启动程序 B 或本工程航线。遥控器保持开启，接管操作沿用厂商流程；本工程停止输出，不自动抢回控制权。本次更新需重新部署机载端，部署和启动命令不变。
