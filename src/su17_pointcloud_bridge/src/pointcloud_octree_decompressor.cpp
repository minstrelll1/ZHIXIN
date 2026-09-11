#include <pcl/compression/octree_pointcloud_compression.h>
#include <pcl_conversions/pcl_conversions.h>
#include <ros/ros.h>
#include <sensor_msgs/PointCloud2.h>

#include <sstream>
#include <string>

class PointCloudOctreeDecompressor {
 public:
  PointCloudOctreeDecompressor() : nh_(), private_nh_("~") {
    private_nh_.param<std::string>(
        "input_topic", input_topic_,
        "/uav1/octomap_point_cloud_centers/reduce_the_frequency/compressed");
    private_nh_.param<std::string>(
        "output_topic", output_topic_,
        "/uav1/octomap_point_cloud_centers/reduce_the_frequency");
    private_nh_.param<int>("queue_size", queue_size_, 1);
    publisher_ = nh_.advertise<sensor_msgs::PointCloud2>(output_topic_, queue_size_);
    subscriber_ = nh_.subscribe(input_topic_, queue_size_,
                                 &PointCloudOctreeDecompressor::callback, this);
    ROS_INFO("正在解码压缩点云：%s -> %s", input_topic_.c_str(),
             output_topic_.c_str());
  }

 private:
  void callback(const sensor_msgs::PointCloud2::ConstPtr& message) {
    if (message->data.empty()) {
      ROS_WARN_THROTTLE(5.0, "压缩点云消息没有数据");
      return;
    }
    try {
      std::string bytes(message->data.begin(), message->data.end());
      std::stringstream stream(bytes);
      pcl::PointCloud<pcl::PointXYZ>::Ptr cloud(new pcl::PointCloud<pcl::PointXYZ>);
      decoder_.decodePointCloud(stream, cloud);
      sensor_msgs::PointCloud2 output;
      pcl::toROSMsg(*cloud, output);
      output.header = message->header;
      publisher_.publish(output);
      ROS_INFO_THROTTLE(5.0, "已从 %s 解码 %zu 个点", input_topic_.c_str(), cloud->size());
    } catch (const std::exception& error) {
      ROS_WARN_THROTTLE(5.0, "点云解压失败：%s", error.what());
    }
  }

  ros::NodeHandle nh_;
  ros::NodeHandle private_nh_;
  ros::Subscriber subscriber_;
  ros::Publisher publisher_;
  pcl::io::OctreePointCloudCompression<pcl::PointXYZ> decoder_;
  std::string input_topic_;
  std::string output_topic_;
  int queue_size_;
};

int main(int argc, char** argv) {
  ros::init(argc, argv, "pointcloud_octree_decompressor");
  PointCloudOctreeDecompressor node;
  ros::spin();
  return 0;
}
