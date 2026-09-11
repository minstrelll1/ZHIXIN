cmake_minimum_required(VERSION 3.0.2)
project(su17_image_transfer)

find_package(catkin REQUIRED COMPONENTS
  cv_bridge
  message_filters
  rospy
  sensor_msgs
  std_msgs
  std_srvs
)

catkin_python_setup()

catkin_package(
  CATKIN_DEPENDS cv_bridge message_filters rospy sensor_msgs std_msgs std_srvs
)

catkin_install_python(PROGRAMS
  scripts/onboard_image_sender.py
  ground/ground_image_receiver.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

install(DIRECTORY config launch
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
)
