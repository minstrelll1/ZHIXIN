# 程序 B 目标回传与赛事整理

实际入口为 `/uavN/target_scheduler/completed_targets`，消息为 `CompletedTargetArray`，内部 `CompletedTarget` 的字段顺序与程序 B 一致。在 `global_id` 后增加 `uint64 detection_count`、`bool tracking_success`。竞赛端的消息包为 `su17_image_transfer`，程序 B 为 `px4_north_camera`；二者线上的字段、顺序与 MD5 必须一致。

更新地面代码后，须重新部署并构建竞赛机载端；程序 B 也须使用已编译这两个字段的新消息版本。新旧 ROS 消息 MD5 不兼容，不能只更新一端。部署及启动命令仍使用 README 原有指令。

`cx/cy` 是相对图片左上角的归一化中心，`w/h` 是归一化宽高。绘框前分别乘实际图片宽、高。框不可用时保留原始 JSON 并回传未标框图片。JSON 记录 `bbox.units=normalized`、`rendered_bbox` 像素框、原始图像时间及选用照片时间。2 秒缓存及超时替代规则不变。

`detection_count` 是累计识别次数，不是动态轨迹点数，也不当作检测概率。`tracking_success` 原样保存：静态精定位完成后成功；动态第 40 个实际上报点才成功，不足 40 点不推定成功。原始 `score` 仍作为 `confidence` 保留。

同机同 ID 静目标仍采用最后一次反馈整条覆盖。发布端跨机同类静目标 10 米内去重，代表记录按“跟踪定位成功 → 识别次数更多 → 检测分数更高”选择。结果排序和超过 16 个目标时的保留也按此质量顺序。旧记录没有新增字段时按未成功、0 次处理，再比较原检测分数。

同类移动目标仍按轨迹相似性合并，选择一架的最长有效轨迹，最多取最早 40 个点；长度相同（40 点以上均按 40 比较）时按上述质量顺序选择。不会拼接不同无人机的点。动态轨迹时间采用 `localization_time`，避免图像时间相同而丢掉不同预测时刻的轨迹点；缺失时兼容图像时间。

赛事 `targetType/targetModel` 按类别 ID 映射：0～6 为“车辆/车辆1～7”，7～10 为“工事/工事1～4”，11～14 为“人员/人员1～4”，15～18 为“人员/运动的人员1～4”。没有有效编号时兼容 vehicle/building/solider/dsolider 名称及已整理的中文类型。原始 JSON 不改名、不覆盖历史数据。

新增质量字段保存在原始 JSON 和本地 `raw-targets.json`、`dedup-decisions.json` 中，赛事 JSON 保持既有字段结构；`confidence` 为原检测分数，不把识别次数伪装成 0～1 的概率。质量排序不保证单帧检测分数递减。
