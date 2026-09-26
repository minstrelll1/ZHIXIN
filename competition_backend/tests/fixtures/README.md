# PCL 测试样本

`pcl_xyz_three_points.bin` 是人工构造的三个 XYZ 点，不包含实际飞行数据。
用 PCL 1.10.0 的 `StaticRangeCoder::encodeCharVectorToStream` 和
`encodeIntVectorToStream` 编码，外层使用 `OctreePointCloudCompression` 的帧格式。

- 独立 I 帧，无颜色，保留每个点。
- 分辨率 1 米，点精度 0.01 米，包围盒 `[-1,-2,-3]` 到 `[1,0,-1]`。
- 八叉树 `[129]`，叶节点点数 `[2,1]`，XYZ 差分 `[1,2,3,4,5,6,7,8,9]`。
- C++ 参考解码所得 XYZ float32 字节 SHA-256：
  `322c8118e95729a2e89b360e4f548580460fc4d0b9ecf9a6c3d2c159cf58d6dd`。

上游实现：https://github.com/PointCloudLibrary/pcl/tree/pcl-1.10.0/io/include/pcl/compression
授权见项目 `third_party/PCL_LICENSE.txt`。
