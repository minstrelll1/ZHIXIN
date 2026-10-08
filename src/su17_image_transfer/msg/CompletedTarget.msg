std_msgs/Header header
string global_id
# 累计真实识别次数；跟踪/精定位是否成功。须与程序 B 同步编译消息。
uint64 detection_count
bool tracking_success
string target_type
time timestamp
time image_stamp
time localization_time
# cx/cy 为相对图片左上角的归一化中心，w/h 为归一化宽高。
float32 cx
float32 cy
float32 w
float32 h
float32 score
int32 category_id
float32 speed_mps
string category
bool is_moving
bool indoor_position
float64 east_m
float64 north_m
float64 up_m
float64 latitude_deg
float64 longitude_deg
float64 altitude_gps_m
