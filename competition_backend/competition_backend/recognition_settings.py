"""科目一识别类别目录。选择只随本次任务发送，不读取历史保存文件。"""
from competition_shared.recognition import CATEGORY_CATALOG, optional_recognition_selection


class RecognitionSettings:
    def __init__(self, _directory=None):
        pass

    def snapshot(self):
        return dict(catalog=CATEGORY_CATALOG, selection=optional_recognition_selection(None),
                    topic_template='/uavN/competition/recognition_categories',
                    message_type='std_msgs/Int32MultiArray')
