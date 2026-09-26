# 地面端可移植部署

## 一键部署

在目标 Windows 电脑打开 PowerShell，在希望保存项目的目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/minstrelll1/ZHIXIN/codex/portable-ground-deployment/tools/bootstrap_ground.ps1'))) -Destination (Join-Path (Get-Location) 'competition_development')"
```

`bootstrap_ground.ps1` 会下载代码和仓库内的 MediaMTX，使用项目根目录计算所有本地路径，并安装 `competition_backend` 的 Python 依赖。目标目录可以在任意盘符，不能要求 `F:\Projects\ZhiXin` 这样的固定路径。已有目录再次执行时只更新代码，保留令牌、日志、飞行记录和接收图片。

没有 Python 3.8 以上版本时，脚本会尝试用 `winget` 安装 Python 3.11；如果电脑没有 `winget`，先安装 Python 3.11 或更高版本再执行。

## 令牌

首次执行脚本会提示输入 AuthToken 和 PeerToken，并写入项目目录下的 `tools\local_tokens.ps1`。该文件已加入 `.gitignore`，不会进入 GitHub、部署包或日志。后续执行会保留已有文件。六个地面端使用同一个 PeerToken；机载端和对应地面端使用同一个 AuthToken。不要把令牌写入命令行历史或提交到版本库。

因此，一键部署包含令牌配置流程，但令牌值仍必须由部署人员在目标电脑上输入或以安全方式提供，GitHub 不保存令牌。

## 启动

```powershell
cd .\competition_development
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\start_ground.ps1
```

浏览器打开 `http://127.0.0.1:8000/`，选择本机地面终端编号和任务发布角色。终端编号、机型以及机地 IP 由仓库中的固定配置绑定。

## 机载端部署

地面电脑到无人机的首次 SSH 公钥配置和机载部署仍使用 `六机六地面端快速部署与启动.txt` 中的相对项目命令。机载厂商工作区（`p600_experiment` 或 `su17_experiment`）属于厂商代码，不上传、不修改；竞赛代码通过 `tools\deploy_onboard_stack.ps1` 同步并在机载端构建。

## 发布要求

推送 GitHub 前只提交源代码、配置模板、部署脚本和 `third_party\mediamtx`；不要提交 `tools\local_tokens.ps1`、`tools\local_tokens.env`、`.venv`、日志、图片、bag 或飞行记录。MediaMTX 可随仓库发布，单个 Windows 可执行文件小于 GitHub 普通文件限制；PrometheusGroundStation 厂商软件不纳入本仓库，按厂商方式单独安装。
