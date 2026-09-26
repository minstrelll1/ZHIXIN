cmake_minimum_required(VERSION 3.0.2)
project(su17_image_transfer)

find_package(catkin REQUIRED COMPONENTS
  cv_bridge
  message_filters
  message_generation
  rospy
  sensor_msgs
  std_msgs
  std_srvs
)

catkin_python_setup()

add_message_files(FILES TargetDetection.msg CompletedTarget.msg CompletedTargetArray.msg)
generate_messages(DEPENDENCIES std_msgs)

catkin_package(
  CATKIN_DEPENDS cv_bridge message_filters message_runtime rospy sensor_msgs std_msgs std_srvs
)

catkin_install_python(PROGRAMS
  scripts/onboard_image_sender.py
  scripts/image_stamp_adapter.py
  ground/ground_image_receiver.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

install(DIRECTORY config launch
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
)
