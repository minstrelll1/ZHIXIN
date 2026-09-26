cmake_minimum_required(VERSION 3.0.2)
project(su17_competition_executor)

find_package(catkin REQUIRED COMPONENTS rospy std_msgs prometheus_msgs)
catkin_python_setup()
catkin_package()

catkin_install_python(PROGRAMS
  scripts/onboard_task_executor.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

install(DIRECTORY launch/
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}/launch
)
