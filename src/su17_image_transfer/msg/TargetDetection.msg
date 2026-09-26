# 原始相机图像的 header，必须复制原帧时间戳，不能填写推理完成时间。
std_msgs/Header header
# 可选：本次回传请求编号、目标跟踪编号。留空请求编号由节点生成。
string request_id
string target_id
# 原始图像像素坐标：左上角为原点，x 向右，y 向下；不是归一化坐标。
float64 center_x
float64 center_y
float64 width
float64 height
# 目标位置：WGS84 十进制度，高度单位米；高度基准通过 extra_json 约定。
float64 target_longitude
float64 target_latitude
float64 target_altitude
# 置信度 0～1；目标类型使用算法约定的字符串。
float64 confidence
string target_type
# 可选 JSON 对象，例如型号、运动状态、高度基准等；最多 64 KiB。
string extra_json
