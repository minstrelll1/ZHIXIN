"""只给无时间戳帧补一次时间，不改写相机已有时间戳。"""
class StampPolicy:
    def __init__(self, mode='preserve_or_receive'):
        if mode not in ('preserve_or_receive', 'require_source'):
            raise ValueError('图像时间戳模式无效')
        self.mode = mode
        self.last_generated = 0

    def resolve(self, source_ns, arrival_ns):
        if source_ns > 0:
            self.last_generated = max(self.last_generated, source_ns)
            return source_ns, 'source'
        if self.mode == 'require_source':
            raise ValueError('相机图像时间戳为空，已拒绝该帧')
        if arrival_ns <= 0:
            raise ValueError('ROS 时钟尚未就绪')
        self.last_generated = max(arrival_ns, self.last_generated + 1)
        return self.last_generated, 'receive'
