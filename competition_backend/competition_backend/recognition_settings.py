import json
import os
from pathlib import Path
import tempfile
import threading

from competition_shared.recognition import CATEGORY_CATALOG, validate_recognition_selection


class RecognitionSettings:
    def __init__(self, directory):
        self.path = Path(directory) / 'recognition_selection.json'
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.exists():
                return None
            try:
                return validate_recognition_selection(json.loads(self.path.read_text(encoding='utf-8')))
            except (ValueError, OSError) as error:
                raise ValueError('已保存的识别类别配置无效，请重新保存：%s' % error) from error

    def save(self, value):
        value = validate_recognition_selection(value)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='recognition-', suffix='.tmp', dir=str(self.path.parent))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return value

    def snapshot(self):
        return dict(catalog=CATEGORY_CATALOG, selection=self.read(),
                    topic_template='/uavN/competition/recognition_categories',
                    message_type='std_msgs/Int32MultiArray')
