cmake_minimum_required(VERSION 3.0.2)
project(su17_pointcloud_bridge)

find_package(catkin REQUIRED COMPONENTS roscpp sensor_msgs pcl_ros pcl_conversions)

catkin_package()

include_directories(${catkin_INCLUDE_DIRS})

add_executable(pointcloud_octree_decompressor src/pointcloud_octree_decompressor.cpp)
target_compile_features(pointcloud_octree_decompressor PRIVATE cxx_std_14)
target_link_libraries(pointcloud_octree_decompressor ${catkin_LIBRARIES})

install(TARGETS pointcloud_octree_decompressor
  RUNTIME DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION})
