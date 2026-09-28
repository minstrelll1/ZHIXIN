# 竞赛系统最新部署与启动

## 一、部署

### 1. 地面端首次部署

在目标 Windows 电脑希望保存项目的父目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/minstrelll1/ZHIXIN/codex/portable-ground-deployment/tools/bootstrap_ground.ps1'))) -Destination (Join-Path (Get-Location) 'competition_development')"
```

首次部署会安装 Python 依赖，检查并使用仓库内的 MediaMTX，并提示在本机填写 AuthToken、PeerToken。

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
