# 科目一成果上报

参赛队名：北方自控智群队。

任务发布端进入网页 → 科目一成果上报 → 选择已回传任务，或选择整理好的 JSON 文件 → 生成并校验 → 下载核对 → 确认上报赛事方。

接口：`POST http://192.168.1.199:8001/api/v1/public/recognition-results`，免登录，不发送 AuthToken / PeerToken。请求采用 `multipart/form-data`，唯一文件参数名为 `file`，文件名 `target-submission.json`，UTF-8 编码。

坐标为 WGS84 `[经度, 纬度]`。固定目标使用 Point 和 timestamp；移动目标使用 LineString、trackPoints、trackStartTime、trackEndTime，至少两个轨迹点。时间含 ISO 8601 时区，类型型号使用 targetType / targetModel。

附件说明的 Array[3] 与示例二维坐标不一致；按附件 JSON 模板及“经度、纬度”的明确说明提交二维坐标。动态示例中的 vehicleModel 按正式字段表和 JSON 模板统一为 targetModel。

上传的是生成并核对后的固定文件，不会在确认上报时重新生成。文件及接口回执保存于 `received_images/subject1_reports`。无有效目标、字段不符、XYZ 未转换为有效 WGS84 或轨迹不足时会提示；未进入文件的目标数量需人工核对。

选择已回传任务只包含本机已经收到或同步到的同一 mission_id 结果，不代表六架无人机都已回传完毕。未自动合并不同任务，不自动重复上报。HTTP 成功响应仅表示接口已响应，业务是否受理以赛事回执为准；超时先向赛事方确认是否已收到。

此接口未提供图片上传，imagePath 不会上传对应图片。图片保存在本地提交包的 images 目录，待赛事方明确图片提交方式。

本功能只更新地面端，无需重新构建或部署机载端。未向赛事地址发送测试数据。
