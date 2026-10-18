"""按任务保存发布端实际下发的类别；供所有地面结果整理入口读取。"""
import json
import os
from pathlib import Path
import re
import tempfile

from .recognition import optional_recognition_selection, validate_recognition_selection


def _path(image_root, mission_id):
    if (not isinstance(mission_id, str) or not mission_id.startswith('subject1-')
            or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', mission_id)):
        raise ValueError('无效的科目一任务编号')
    return Path(image_root) / 'mission_reporting_selections' / (mission_id + '.json')


def save_reporting_selection(image_root, mission_id, selection):
    target = _path(image_root, mission_id)
    document = dict(schema_version=1, mission_id=mission_id,
                    recognition_selection=optional_recognition_selection(selection))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=mission_id+'-', suffix='.part', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def load_reporting_selection(image_root, mission_id):
    # 历史任务/非标准旧任务没有快照时不猜测当前界面或另一任务的选择。
    if not isinstance(mission_id, str) or not mission_id.startswith('subject1-'):
        return optional_recognition_selection(None)
    path = _path(image_root, mission_id)
    try:
        document = json.loads(path.read_text(encoding='utf8'))
    except FileNotFoundError:
        return optional_recognition_selection(None)
    if (not isinstance(document, dict) or document.get('schema_version') != 1
            or document.get('mission_id') != mission_id):
        raise ValueError('任务人员类别快照与本次任务不一致')
    return validate_recognition_selection(document.get('recognition_selection'))
