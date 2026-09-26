"""SU17 的 PCL XYZ 独立帧解码，不依赖 ROS 或本机 PCL 安装。

算法移植自 PCL 1.10.0（BSD）；版权声明见 third_party/PCL_LICENSE.txt。
仅支持 SU17 当前发送的无颜色 I 帧，拒绝依赖前帧的 P 帧。
"""
from __future__ import annotations

import bisect
import math
import struct

MARKER = b"<PCL-OCT-COMPRESSED>"
MAX_POINTS = 2_000_000
MAX_BYTES = 64 * 1024 * 1024


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError("PCL 压缩帧不完整")
        result = self.data[self.pos:self.pos + size]
        self.pos += size
        return result

    def unpack(self, fmt: str):
        return struct.unpack("<" + fmt, self.read(struct.calcsize("<" + fmt)))


def _range_decode(reader: Reader, count: int, integer: bool = False):
    if not 0 <= count <= MAX_BYTES:
        raise ValueError("PCL 解压数据长度超出限制")
    if integer:
        size, byte_size = reader.unpack("QB")
        if not 2 <= size <= MAX_POINTS + 2 or not 1 <= byte_size <= 8:
            raise ValueError("PCL 整数频率表无效")
        frequencies = [0] + [int.from_bytes(reader.read(byte_size), "little")
                               for _ in range(size - 1)]
        bits = 64
    else:
        frequencies = reader.unpack("257I")
        bits = 32
    mask = (1 << bits) - 1
    top, bottom = 1 << (bits - 8), 1 << (bits - 16)
    total = frequencies[-1]
    if (frequencies[0] != 0 or not 0 < total < bottom or
            any(a > b for a, b in zip(frequencies, frequencies[1:]))):
        raise ValueError("PCL 累积频率表无效")
    code = int.from_bytes(reader.read(bits // 8), "big")
    low, span = 0, mask
    output = [] if integer else bytearray()
    for _ in range(count):
        span //= total
        if span == 0:
            raise ValueError("PCL 熵编码区间无效")
        value = ((code - low) & mask) // span
        symbol = bisect.bisect_right(frequencies, value) - 1
        if not 0 <= symbol < len(frequencies) - 1:
            raise ValueError("PCL 熵编码符号无效")
        output.append(symbol)
        low = (low + frequencies[symbol] * span) & mask
        span = (span * (frequencies[symbol + 1] - frequencies[symbol])) & mask
        while True:
            if (low ^ ((low + span) & mask)) >= top:
                if span >= bottom:
                    break
                span = (-low) & (bottom - 1)
            code = ((code << 8) | reader.read(1)[0]) & mask
            low, span = (low << 8) & mask, (span << 8) & mask
    return output


def decode_xyz(data: bytes):
    """返回完整的小端 float32 XYZ 字节及压缩帧元数据；不接受残缺帧。"""
    if len(data) > MAX_BYTES:
        raise ValueError("PCL 压缩帧超过 64 MiB 限制")
    reader = Reader(data)
    if reader.read(len(MARKER)) != MARKER:
        raise ValueError("不是 PCL 八叉树压缩点云")
    frame_id, intra = reader.unpack("IB")
    if intra != 1:
        raise ValueError("暂不支持依赖前帧的 PCL P 帧，需要独立 I 帧")
    voxel, color, count, resolution, color_bits, precision, *bounds = reader.unpack("BBQdBd6d")
    if color != 0 or voxel not in (0, 1):
        raise ValueError("暂不支持带颜色或未知配置的 PCL 点云")
    if not 0 < count <= MAX_POINTS:
        raise ValueError("PCL 点数无效或超过 200 万点限制")
    if (not all(math.isfinite(v) for v in [resolution, precision] + bounds) or
            not 0 < resolution <= 1e6 or not 0 < precision <= resolution):
        raise ValueError("PCL 分辨率或坐标边界无效")
    lower, upper = bounds[:3], bounds[3:]
    if any(h <= l for l, h in zip(lower, upper)):
        raise ValueError("PCL 包围盒为空")
    epsilon = 2 ** -23
    max_voxels = max(2, *(math.ceil((h - l - epsilon) / resolution)
                          for l, h in zip(lower, upper)))
    depth = math.ceil(math.log2(max_voxels) - epsilon)
    if not 1 <= depth <= 32:
        raise ValueError("PCL 八叉树深度超出限制")
    side = (1 << depth) * resolution
    lower = [l - (side - (h - l)) / 2 if (side - (h - l)) / 2 > epsilon else l
             for l, h in zip(lower, upper)]
    tree_size, = reader.unpack("Q")
    if not 0 < tree_size <= min(MAX_BYTES, count * depth):
        raise ValueError("PCL 八叉树长度无效")
    tree = _range_decode(reader, tree_size)
    counts, differences = [], b""
    if not voxel:
        leaf_count, = reader.unpack("Q")
        if not 0 < leaf_count <= count:
            raise ValueError("PCL 叶节点数量无效")
        counts = _range_decode(reader, leaf_count, integer=True)
        if any(n <= 0 for n in counts) or sum(counts) != count:
            raise ValueError("PCL 叶节点点数与帧头不一致")
        difference_size, = reader.unpack("Q")
        if difference_size != count * 3:
            raise ValueError("PCL XYZ 差分数据长度与点数不一致")
        differences = _range_decode(reader, difference_size)
    if reader.pos != len(data):
        raise ValueError("PCL 帧后存在多余数据")

    # 与 PCL 保持相同的 float32 乘法及最终舍入。
    f32 = lambda v: struct.unpack("<f", struct.pack("<f", v))[0]
    precision = f32(precision)
    offsets = [f32(i * precision) for i in range(256)]
    body = bytearray(count * 12)
    tree_index = leaf_index = point_index = 0

    def visit(key, level):
        nonlocal tree_index, leaf_index, point_index
        if tree_index >= len(tree):
            raise ValueError("PCL 八叉树结构不完整")
        occupancy = tree[tree_index]
        tree_index += 1
        if occupancy == 0:
            raise ValueError("PCL 八叉树包含空分支")
        for child in range(8):
            if not occupancy & (1 << child):
                continue
            child_key = (key[0] * 2 + ((child >> 2) & 1),
                         key[1] * 2 + ((child >> 1) & 1), key[2] * 2 + (child & 1))
            if level > 1:
                visit(child_key, level - 1)
                continue
            if not voxel and leaf_index >= len(counts):
                raise ValueError("PCL 叶节点数量与八叉树不一致")
            n = 1 if voxel else counts[leaf_index]
            leaf_index += 1
            if point_index + n > count:
                raise ValueError("PCL 解压点数超出帧头")
            corner = [v * resolution + l for v, l in zip(child_key, lower)]
            for _ in range(n):
                if voxel:
                    point = [(v + .5) * resolution + l for v, l in zip(child_key, lower)]
                else:
                    index = point_index * 3
                    point = [corner[j] + offsets[differences[index + j]] for j in range(3)]
                try:
                    struct.pack_into("<fff", body, point_index * 12, *point)
                except (OverflowError, struct.error) as error:
                    raise ValueError("PCL 解压坐标超出 float32 范围") from error
                point_index += 1

    visit((0, 0, 0), depth)
    if tree_index != len(tree) or point_index != count or (not voxel and leaf_index != len(counts)):
        raise ValueError("PCL 八叉树、叶节点和点数校验不一致")
    return bytes(body), {"compression": "pcl_octree", "compression_frame_id": frame_id,
                         "source_points": count, "resolution": resolution,
                         "point_resolution": precision}
