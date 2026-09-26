cmake_minimum_required(VERSION 3.0.2)
project(su17_mission_planning)

add_compile_options(-std=c++14 -Wall -Wextra)

find_package(catkin REQUIRED COMPONENTS
  geometry_msgs
  nav_msgs
  roscpp
  std_msgs
)

catkin_package(
  CATKIN_DEPENDS geometry_msgs nav_msgs roscpp std_msgs
)

include_directories(${catkin_INCLUDE_DIRS})

add_executable(mission_planner_node src/mission_planner_node.cpp)
target_link_libraries(mission_planner_node ${catkin_LIBRARIES})

install(TARGETS mission_planner_node
  RUNTIME DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

install(DIRECTORY config launch
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
)
