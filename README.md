# 竞赛系统最新部署与启动

## 一、部署

### 1. 地面端首次部署

在目标 Windows 电脑希望保存项目的父目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/minstrelll1/ZHIXIN/codex/portable-ground-deployment/tools/bootstrap_ground.ps1'))) -Destination (Join-Path (Get-Location) 'competition_development')"
```

首次部署会安装 Python 依赖，检查并使用仓库内的 MediaMTX，并提示在本机填写 AuthToken、PeerToken。若没有兼容的 Python，脚本先尝试 `winget`；未安装 `winget` 或安装失败时，自动从 Python 官网下载并校验安装包，为当前用户安装 Python 3.11。此时无需单独安装 `winget`，但电脑需要能访问 Python 官网和 Python 依赖下载源。

若复制某个文件时被短暂占用，部署脚本会自动重试。持续拒绝访问时，错误会列出来源、目标及哪一侧无法读取；核对对应路径的权限和 Windows 安全中心“保护历史”后，重新执行同一条命令即可继续，已有本机令牌与机队配置会保留。

#### 使用 U 盘首次部署

在原电脑的项目父目录执行，`E:` 改为 U 盘盘符：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\competition_development\tools\prepare_ground_usb.ps1" -Destination "E:\competition_development"
```

它将代码、`智信竞赛.exe` 和 MediaMTX 复制到 U 盘并校验；不复制本机令牌、虚拟环境、日志和飞行数据。在目标电脑打开 PowerShell，进入希望保存项目的父目录，执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "E:\competition_development\tools\bootstrap_ground_impl.ps1" -FromUsb
```

该命令直接从 U 盘复制代码，提示填写本机 AuthToken、PeerToken，自动检查或安装 Python 并安装地面依赖；已有本机令牌、机队配置和数据会保留。**不访问 GitHub**；缺少 Python 或依赖时，首次安装仍需能访问 Python 官网或软件包下载源。完成后按下方“地面端”命令启动。

### 2. 地面端仅更新代码

先关闭地面后端，在项目父目录执行。仅下载变化的文件，不重新安装 Python、MediaMTX；保留本机令牌、机队配置和数据：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\competition_development\tools\bootstrap_ground.ps1" `
  -Destination ".\competition_development" `
  -SkipInstall
```

所有已部署电脑也可统一使用下面这一条在线更新命令；它每次加载最新版更新器，旧版电脑无需分两步升级：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/minstrelll1/ZHIXIN/codex/portable-ground-deployment/tools/bootstrap_ground.ps1'))) -Destination (Join-Path (Get-Location) 'competition_development') -SkipInstall"
```

每次上传代码后，等待 GitHub Actions 中“发布地面增量更新清单”成功，再让其他电脑更新。更新器优先读取独立发布清单，按固定提交与文件哈希只下载变化文件，正常更新不调用 GitHub API，也不需要 GitHub 令牌。清单发布有延迟时读取的是上一已发布版本，以终端显示的版本号为准。

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

New-Item -ItemType Directory -Force -Path "$localProject\config" | Out-Null
if (-not (Test-Path "$localProject\config\onboard_programs.json")) { Copy-Item "$usbSource\config\onboard_programs.json" "$localProject\config\onboard_programs.json" }
robocopy "$usbSource" "$localProject" /E /XJ /R:1 /W:1 /XD .git .venv __pycache__ data .runtime ground_runtime ground_logs flight_records received_images pointcloud_records position_tests onboard_source_backup .codex_backup* /XF fleet.json onboard_programs.json local_tokens.ps1 local_tokens.env mediamtx.exe auto.key auto.crt *.pyc *.bag *.log /TEE /LOG:"$localProject\ground_logs\usb_update.log"

if ($LASTEXITCODE -ge 8) { throw "USB 更新失败，请查看 ground_logs\usb_update.log" }
```

跳过未变化的文件，保留本机 Python 环境、MediaMTX、令牌、机队配置和采集数据。普通代码更新无需重复安装依赖；依赖清单发生变化时再运行安装命令。完成后重新启动地面后端并刷新网页。

### 3. 机载端部署

在地面端项目父目录执行对应无人机命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\competition_development\tools\deploy_onboard_stack.ps1" -UavAddress "192.168.1.202" -Model p600 -SyncConfig
```
首次连接时，按终端提示核对主机指纹，并输入机载登录密码或本机密钥口令。


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

#### 自动配置双网口 IP

在目标地面电脑上，先接好 USB-C／USB 转网口的图传线，以及内置网口到交换机的网线。以管理员身份打开 PowerShell，进入项目目录，运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\configure_ground_network.ps1
```

按提示输入本机地面终端编号 1～6。脚本把外置 USB 图传网口设为 `192.168.1.230/24`，把内置交换机网口设为上表对应的 `192.168.2.x/24`；两个专用网口均无默认网关，不修改其他网卡。也可直接加 `-TerminalId 2` 指定编号。先查看选择结果而不修改网卡时加 `-Preview`。若电脑有多个 USB 或内置以太网卡，脚本会停止并列出名称，再以 `-ExternalAdapterName "外置网卡名" -InternalAdapterName "内置网卡名"` 明确指定。修改前的设置保存在 `ground_logs\network_before_*.json`。

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

启动时自动检查本机地面互联防火墙规则，双击和命令行启动均适用。首次缺少规则时，在 Windows 管理员授权提示中选择“是”；已有正确规则时不再申请权限。仅允许 `config/fleet.json` 中六个地面互联 IP 访问本机网页端口（默认 TCP 8000）和 Ping，公用网络也适用。配置失败不阻断启动，详情见 `ground_logs/firewall.log`；六台电脑分别更新并启动即可，无需逐台粘贴规则命令。

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
  flight_mode:=outdoor
```

### 4. 目标检测程序（边疆）

```bash
source ~/SpireCV_bj/src/spirecv-ros/devel/setup.bash
roslaunch spirecv_ros uav_yolo26_botsort_geolocation.launch uav_id:=1
```

### 网页起飞操作

规划并分派 → 确认任务回执 → 一键起飞预检 → 确认起飞。更新后的机载程序会自动解锁并进入 COMMAND_CONTROL，稳定到达任务高度后才启动程序 B 或本工程航线。遥控器保持开启，接管操作沿用厂商流程；本工程停止输出，不自动抢回控制权。本次更新需重新部署机载端，部署和启动命令不变。


### 自动启动前的首次 SSH 配置

程序先使用本机竞赛专用密钥，再尝试已有 SSH 公钥授权；均未授权时才提示输入机载 Ubuntu 密码，最多三次。密码不保存，不询问旧私钥口令。授权成功后继续启动；以后连接同一无人机无需再输入。原机载部署命令也会先完成这一步。

UAV4 首次授权（项目目录执行）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_onboard_ssh.ps1 -UavAddress 192.168.1.217
```

### 六台电脑任意换机：一次性公钥预授权

1. 每台地面电脑在项目目录执行，将生成的 `ground_ssh_keys` 文件夹通过 U 盘汇集到一台电脑。同名文件是同一公钥，只保留一份；不要复制 `.ssh` 私钥。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_onboard_ssh.ps1 -ExportPublicKeyDirectory .\ground_ssh_keys
```

2. 汇集好六台电脑的 `.pub` 文件后，在该电脑上逐架连接无人机并运行以下命令。每架只执行一次，将地址替换为 `.202`、`.207`、`.212`、`.217`、`.222`、`.227` 对应的实际机载地址；SU17 使用其实际地址。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_onboard_ssh.ps1 -UavAddress 192.168.1.217 -PublicKeyDirectory .\ground_ssh_keys
```

如提示密码，输入该无人机 `amov` 用户的 Ubuntu 登录密码。脚本追加汇集的公钥，保留已有授权，并验证当前电脑的专用密钥登录。完成六架授权后，这六台电脑可互换无人机使用，无需再逐对输入密码。新增电脑、重装系统或更换密钥后，重新导出并预授权新增公钥；无需重新编译机载程序。

若普通 `ssh amov@机载IP` 也无法登录，先解决用户名、密码或 SSH 服务配置；程序不能绕过服务器身份认证。授权脚本不会启动、停止或重启飞行程序。

### 双击启动

1. 完成首次部署后，在项目目录双击 `智信竞赛.exe`；自动启动地面后端并用默认浏览器打开竞赛网页。重复双击复用已运行的后端。
2. 选择本地地面终端编号并确认，可以先开地面端、后给无人机上电。地面端持续等待 SSH 就绪，识别到本次开机尚未启动业务程序后，自动启动竞赛机载端、目标检测和自主飞行指令程序；编号由 `{uav_id}` 随所选终端替换。首次 SSH 授权按弹窗完成一次。
3. 点击“机地网络配置”右侧的程序名称查看打印内容。红色表示未启动、连接失败或异常，绿色表示对应 ROS 节点已响应（地面端表示网页服务运行中）。关闭网页不停止后台或机载程序。

无人机是否重新开机以 Linux `boot_id` 判断，不使用断网时长或系统日期推断。机载端先保存本次开机的整批启动记录，再启动程序；同次开机的 LQ10 断线重连、SSH 响应丢失、地面程序重开都只恢复监控和增量日志。首次连接时若已有业务程序运行，也只接管监控。启动失败、程序崩溃或人工停止后不会自动重试；核对日志后，可主动点击“重新连接并启动”。无人机真正重新开机后才允许新一批自动启动。自动连接不发送任务、解锁或起飞命令，三个程序就绪后仍由操作员规划分派并点击一键起飞。

三个机载程序的启动指令在本机 `config/onboard_programs.json` 的 `commands` 中配置：`onboard` 为竞赛机载端，`detection` 为目标检测，`flight` 为程序 B。`{uav_id}` 和 `{model}` 自动替换为所选编号和机型；指令在机载 `~/competition_development` 目录执行，所引用工作空间须已安装。

每次开机自动启动或操作员主动启动时重新读取配置。修改后，在对应程序打印窗口点击“停止程序”，核验退出后点击“启动程序”；只启动该机载程序，其他程序不受影响。增量更新保留本机启动配置。全部场景的规划、分派和起飞均不匹配校验程序 B 的 `flight_mode`；外部模式的规划航速只用于估时，不下发为本工程飞行限速。一键起飞仍检查程序 B 是否正常运行。

启动管理脚本由地面端通过 SSH 自动传送；本次开机与断线区分功能只需更新地面端并重开，不需要为此重新编译机载代码。此次新增程序 B 的 `/uavN/target_scheduler/completed_targets` 话题兼容，需要按上面的机载更新命令同步竞赛机载代码；不修改目标检测、程序 B 或厂商工作空间。U 盘复制时同时复制根目录的 `智信竞赛.exe`。原有命令行启动方式仍可使用。


三个机载程序的地面日志各自只保留最近一次程序启动的内容，保存在 `ground_logs/programs/uavN_onboard.log`、`uavN_detection.log`、`uavN_flight.log`；新一次启动自动替换对应地面副本，断线重连和地面端重开继续同步同一次日志。每行加北京时间的“地面接收”时间，原始打印中的机载时间保留；补读历史时接收时间不是实际发生时间。机载原始 `console.log` 继续保留，不清理。更新后重启地面后端生效，无需重新部署或编译三个机载程序。

程序打印窗口每秒自动读取新日志；“加载更多日志”用于分批补看尚未显示的历史，不会启动或重启程序。 “查找内容”搜索已加载打印，不区分大小写；支持高亮、匹配数量、上一处／下一处，以及 Enter／Shift+Enter 跳转。未加载的历史需先点“加载更多日志”。

### 启动与停止程序

- 关闭地面端：点击“竞赛程序地面端”→“停止程序”；或双击项目目录的 `stop_ground.cmd`，等待显示退出核验结果。关闭浏览器只关闭页面。
- 关闭机载端、目标检测或程序 B：点击对应程序名称→“停止程序”。依次中断、终止、强制结束对应进程树并核验 ROS 节点退出；未确认退出会报错。手动停止后不会自动拉起；点击同一窗口的“启动程序”，重新读取 `config/onboard_programs.json` 并只启动该程序。已运行时不重复启动。
- 关闭地面端不会同时关闭机载程序。地面退出记录：`ground_logs/ground_stop.log`。

命令行关闭地面端（项目目录执行）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\stop_ground.ps1
```


### 科目一结果上报

任务发布端点击“科目一成果上报”，选择已回传任务或 JSON 文件 → 生成并校验 → 下载核对 → 确认上报。参赛队名默认“北方自控智群队”。接口采用 UTF-8 JSON 文件的 form-data 上传；图片另存本地，不自动上报。详见 [科目一上报说明](docs/subject1_reporting.md)。


### 科目一识别类别

展开“科目一识别类别” → 选择 0～19 类并勾选目标 → 直接规划并分派。每次打开默认 0 类，不单独保存；0 类也不阻断任务。机载话题 `/uavN/competition/recognition_categories`（`std_msgs/Int32MultiArray`，保留最后一条）。本功能需同步更新竞赛机载端，原部署命令不变。详见 [类别编号与接入说明](docs/recognition_categories.md)。

### 程序 B 实时业务数据

机载竞赛程序只读采集程序 B 的当前目标、上次识别、调度/机动/云台/循线状态及侦察点序号，在网页‘实时任务详情 → 实时数据’展示。需将最新程序 B 的三个消息编译到机载 ~/recon_ws 后更新竞赛机载端。详见 [程序 B 遥测接入](docs/program_b_telemetry.md)。
