# 科目一成果上报

参赛队名：北方自控智群队。

任务发布端进入网页 → 科目一成果上报 → 选择已回传任务，或选择整理好的 JSON 文件 → 生成并校验 → 下载核对 → 确认上报赛事方。

接口：`POST http://192.168.1.199:8001/api/v1/public/recognition-results`，免登录，不发送 AuthToken / PeerToken。请求采用 `multipart/form-data`，唯一文件参数名为 `file`，文件名 `target-submission.json`，UTF-8 编码。

坐标为 WGS84 `[经度, 纬度]`。固定目标使用 Point 和 timestamp；移动目标使用 LineString、trackPoints、trackStartTime、trackEndTime，至少两个轨迹点。时间含 ISO 8601 时区，类型型号使用 targetType / targetModel。

附件说明的 Array[3] 与示例二维坐标不一致；按附件 JSON 模板及“经度、纬度”的明确说明提交二维坐标。动态示例中的 vehicleModel 按正式字段表和 JSON 模板统一为 targetModel。

任务发布端将本次启动后的成果放在 `received_images/年-月-日_时-分-秒_微秒/`。其中 `UAV1`～`UAV6` 存放各机原始 JPG 和同名目标 JSON；`subject1_submissions/<任务编号>/` 存放持续更新的 `target-submission.json`、`images/`、`raw-targets.json`、`dedup-decisions.json` 和 `submission-format.json`；`subject1_reports/` 存放冻结的上报 JSON、人工导入的整理记录和赛事接口回执。上报状态接口会返回本次成果目录。

仅任务发布端汇总各终端在本次程序启动后收到的新回传；首次发现某个任务的新文件时，会补齐该任务在对端已有的文件。旧任务若没有新回传，不会自动进入本次成果目录。同一具体类型的固定目标在 10 米内合并，合并组内任意两点距离都须不超过 10 米；移动目标比较有时间重叠的轨迹，允许最多 3 秒的上报偏移，要求位置接近且运动方向一致。缺少可靠时间或经纬度的目标不会强行合并。静目标合并后按跟踪定位成功、识别次数、原检测分数的优先级选取代表。移动目标选择最长的单机轨迹并取最早 40 点，不拼接不同飞机的轨迹，长度相同时按上述质量优先级裁决。最终按质量优先级排序，最多保留赛事允许的 16 个目标；原检测分数仍保留在 confidence。原始回传不删除，合并和超额舍弃记录在中间文件中。人工导入的 JSON 在冻结前同样去重、排序并校验。

上传的是生成并核对后的固定文件，不会在确认上报时重新生成。无有效目标、字段不符、XYZ 未转换为有效 WGS84 或轨迹不足时会提示；未进入文件的目标数量需人工核对。

选择已回传任务只包含本机已经收到或同步到的同一 mission_id 结果，不代表六架无人机都已回传完毕。未自动合并不同任务，不自动重复上报。HTTP 成功响应仅表示接口已响应，业务是否受理以赛事回执为准；超时先向赛事方确认是否已收到。

此接口未提供图片上传，imagePath 不会上传对应图片。图片保存在本地提交包的 images 目录，待赛事方明确图片提交方式。

本次去重与归档由统一启动入口 `tools/start_ground.ps1`（含 `智信竞赛.exe`）启用，只涉及地面端；旧的 `tools/start_competition_backend_tcp.ps1` 直启方式尚不启用本次按启动时间归档。六台地面电脑都应更新后再进行跨机同步。未向赛事地址发送测试数据。

新版回传字段、类别映射和去重规则见 [completed_targets 协议](completed_targets_protocol.md)。子区域话题见 [任务子区域协议](task_region_protocol.md)。

## 赛事编号、型号与本地整理记录

正式文件在去重、质量排序和最多16个目标筛选完成后，依次使用 `target-001`、`target-002`…作为 `features[].id`。每次生成按本次最终顺序编号；原始机载目标 ID 不修改。`submission-format.json` 的 `id_mapping` 保存源 ID 与赛事 ID 对照，源 ID 重名时结合 `raw-targets.json` 和 `dedup-decisions.json` 查看机号与合并记录。冻结上报文件旁的 `<摘要>.format.json` 保存对应整理记录；摘要绑定正文，避免混用其他版本。

赛事 `targetModel` 将“运动的人员1～4”转换成“人员1～4”，`targetType` 为“人员”、`targetCategory` 仍为“移动”。内部识别类别和机载话题不改。图片路径继续指向真实文件，不因赛事目标编号转换而失效。

`indoorTargets` 是早期程序自定义的排除记录，不是赛事模板字段，实际还可能包含轨迹不足的目标。正式 JSON 不再包含该字段；未进入赛事结果的源 ID、原因及局部坐标保存在 `submission-format.json` 的 `excluded_targets`，原始回传 JSON/JPG 仍保留。人工导入旧文件时同样移出该字段，网页继续显示未纳入数量。

## 目标时间来源

- 静目标 `properties.timestamp` 使用外部程序 B 的原始 `timestamp`（机载回传元数据保存为 `image_stamp`）；B 的 timestamp 为零时机载使用原始 image_stamp。静目标重复回传后使用最后一整条反馈的时间，与坐标来自同一条记录。
- 动目标每个 `trackPoints[].timestamp` 优先使用 `localization_time`，无有效值时使用原始目标/图像时间。轨迹起止时间取保留轨迹的首、末点。
- 输出使用 UTC、毫秒精度、ISO 8601 `Z` 时区，例如 `2026-10-09T06:00:00.123Z` 等于北京时间 `2026-10-09T14:00:00.123+08:00`。`metadata.createdAt` 才是地面电脑生成文件的时间。
- 2秒图像替代仅改变原始元数据中的 `selected_image_stamp`，不改目标源时间。不会用接收时间、上传时间或替代图片时间伪造发现时间。旧 paired_topics/capture_request 记录使用原始相机 `source_stamp_sec/source_stamp_nsec`。
- 源时间为零或格式不合法且没有有效源时间可替代时，记录保存在本地，并在排除记录中说明原因；动目标缺少时间的点不会成为有效轨迹点。

格式合法不代表绝对时间正确。赛前需把机载与地面电脑校准到同一标准时间源，并确认相机 Header 与机载 ROS 使用同一真实时间基准（实机不使用仿真时间）。当前程序没有自动 NTP 校时功能；竞赛用时及其人工修改不会校准系统 UTC。若机载仍停留在1970年等错误日期，需要先校时，不能由地面把接收时间当作目标发生时间修正。
