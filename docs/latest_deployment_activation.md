# 竞赛系统最新部署与启动

## 一、部署

### 1. 地面端首次部署

在目标 Windows 电脑希望保存项目的父目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/minstrelll1/ZHIXIN/codex/portable-ground-deployment/tools/bootstrap_ground.ps1'))) -Destination (Join-Path (Get-Location) 'competition_development')"
```

首次部署会安装 Python 依赖，检查并使用仓库内的 MediaMTX，并提示在本机填写 AuthToken、PeerToken。

### 2. 地面端仅更新代码或配置

在项目父目录执行，不重新安装 Python、MediaMTX 或令牌：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\competition_development\tools\bootstrap_ground.ps1" `
  -Destination ".\competition_development" `
  -SkipInstall
```

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

|无人机|机载地址|地面地址|
|---|---|---|
|UAV1|192.168.1.202|192.168.1.121|
|UAV2|192.168.1.207|192.168.1.122|
|UAV3|192.168.1.212|192.168.1.123|
|UAV4|192.168.1.217|192.168.1.124|
|UAV5|192.168.1.222|192.168.1.125|
|UAV6|192.168.1.227|192.168.1.126|

其他 P600 只替换 `-UavAddress`。SU17 使用实际机载地址，并将 `-Model p600` 改为 `-Model su17`。部署脚本不修改厂商工作区。

## 二、启动

### 1. 地面端

```powershell
cd .\competition_development
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\start_ground.ps1
```

浏览器打开：

```text
http://127.0.0.1:8000/
```

进入网页后选择本机地面终端编号和任务发布角色。

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
