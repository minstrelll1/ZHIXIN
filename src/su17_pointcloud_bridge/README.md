# GroundStation point-cloud bridge

Prometheus publishes the reduced OctoMap cloud twice: as a normal
`sensor_msgs/PointCloud2` topic and as a PCL Octree compressed topic ending in
`/compressed`. GroundStation commonly subscribes to the compressed topic. This
node decodes that PCL stream on the GroundStation computer and republishes the
normal XYZ `PointCloud2` topic used by
`tools/forward_groundstation_pointcloud.py`.

Build this package in the competition workspace on the ROS computer, then run:

```bash
roslaunch su17_pointcloud_bridge decompress_uav.launch uav_id:=3
python3 tools/forward_groundstation_pointcloud.py \
  --uav-topics '3=/uav3/octomap_point_cloud_centers/reduce_the_frequency' \
  --backend http://192.168.1.123:8000 --token "$POINTCLOUD_TOKEN" --hz 1
```

The node only subscribes to the GroundStation computer's existing ROS graph;
it does not open another aircraft or ROSBridge connection.
