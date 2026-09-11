#include <algorithm>
#include <cmath>
#include <string>

#include <geometry_msgs/PoseStamped.h>
#include <nav_msgs/Path.h>
#include <ros/ros.h>
#include <std_msgs/String.h>

class MissionPlannerNode {
 public:
  MissionPlannerNode() : private_nh_("~") {
    private_nh_.param<std::string>("frame_id", frame_id_, "map");
    private_nh_.param("step_size", step_size_, 1.0);
    step_size_ = std::max(step_size_, 0.01);

    current_pose_sub_ = nh_.subscribe("current_pose", 1,
                                      &MissionPlannerNode::currentPoseCallback, this);
    goal_sub_ = nh_.subscribe("goal", 1, &MissionPlannerNode::goalCallback, this);
    path_pub_ = nh_.advertise<nav_msgs::Path>("planned_path", 1, true);
    status_pub_ = nh_.advertise<std_msgs::String>("status", 1, true);

    publishStatus("waiting_for_current_pose");
    ROS_INFO("Mission planner baseline ready in frame '%s'", frame_id_.c_str());
  }

 private:
  void currentPoseCallback(const geometry_msgs::PoseStamped::ConstPtr& message) {
    current_pose_ = *message;
    if (!has_current_pose_) {
      has_current_pose_ = true;
      publishStatus("ready");
    }
  }

  void goalCallback(const geometry_msgs::PoseStamped::ConstPtr& goal) {
    if (!has_current_pose_) {
      ROS_WARN_THROTTLE(2.0, "Cannot plan before receiving current_pose");
      publishStatus("rejected_no_current_pose");
      return;
    }

    if (!current_pose_.header.frame_id.empty() && !goal->header.frame_id.empty() &&
        current_pose_.header.frame_id != goal->header.frame_id) {
      ROS_ERROR("Cannot plan between frames '%s' and '%s' without a transform",
                current_pose_.header.frame_id.c_str(), goal->header.frame_id.c_str());
      publishStatus("rejected_frame_mismatch");
      return;
    }

    const double dx = goal->pose.position.x - current_pose_.pose.position.x;
    const double dy = goal->pose.position.y - current_pose_.pose.position.y;
    const double dz = goal->pose.position.z - current_pose_.pose.position.z;
    const double distance = std::sqrt(dx * dx + dy * dy + dz * dz);
    const int segment_count = std::max(1, static_cast<int>(std::ceil(distance / step_size_)));

    nav_msgs::Path path;
    path.header.stamp = ros::Time::now();
    path.header.frame_id = !goal->header.frame_id.empty()
                               ? goal->header.frame_id
                               : (!current_pose_.header.frame_id.empty()
                                      ? current_pose_.header.frame_id
                                      : frame_id_);
    path.poses.reserve(static_cast<std::size_t>(segment_count + 1));

    for (int index = 0; index <= segment_count; ++index) {
      const double ratio = static_cast<double>(index) / segment_count;
      geometry_msgs::PoseStamped waypoint;
      waypoint.header = path.header;
      waypoint.pose.position.x = current_pose_.pose.position.x + ratio * dx;
      waypoint.pose.position.y = current_pose_.pose.position.y + ratio * dy;
      waypoint.pose.position.z = current_pose_.pose.position.z + ratio * dz;
      waypoint.pose.orientation = goal->pose.orientation;
      path.poses.push_back(waypoint);
    }

    path_pub_.publish(path);
    publishStatus("path_ready");
    ROS_INFO("Published baseline path with %zu waypoints", path.poses.size());
  }

  void publishStatus(const std::string& value) {
    std_msgs::String message;
    message.data = value;
    status_pub_.publish(message);
  }

  ros::NodeHandle nh_;
  ros::NodeHandle private_nh_;
  ros::Subscriber current_pose_sub_;
  ros::Subscriber goal_sub_;
  ros::Publisher path_pub_;
  ros::Publisher status_pub_;
  geometry_msgs::PoseStamped current_pose_;
  bool has_current_pose_{false};
  std::string frame_id_;
  double step_size_{1.0};
};

int main(int argc, char** argv) {
  ros::init(argc, argv, "mission_planner");
  MissionPlannerNode node;
  ros::spin();
  return 0;
}
