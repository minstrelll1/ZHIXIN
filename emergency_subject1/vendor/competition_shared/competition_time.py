"""规划时随任务下发的比赛计时快照（与电脑系统时间无关）。"""

import math


def normalize_competition_time(value, mission_id):
    if not isinstance(value, dict):
        raise ValueError("比赛计时快照必须是对象")
    if type(value.get("running")) is not bool or type(value.get("synchronized")) is not bool:
        raise ValueError("比赛计时运行及同步状态无效")
    elapsed = value.get("elapsed_seconds")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or not 0 <= elapsed <= 366 * 86400:
        raise ValueError("比赛已用时间无效")
    if not isinstance(mission_id, str) or not mission_id or len(mission_id) > 128:
        raise ValueError("比赛计时任务编号无效")
    result = dict(schema_version=1, mission_id=mission_id, running=value["running"],
                  synchronized=value["synchronized"], elapsed_seconds=float(elapsed))
    if value["running"]:
        session = value.get("session_id")
        revision = value.get("revision")
        publisher = value.get("publisher_terminal_id")
        updated_by = value.get("updated_by")
        if not isinstance(session, str) or not 1 <= len(session) <= 128:
            raise ValueError("比赛计时会话编号无效")
        if type(revision) is not int or revision < 0:
            raise ValueError("比赛计时修订号无效")
        if type(publisher) is not int or publisher not in range(1, 7):
            raise ValueError("比赛计时发布终端无效")
        if type(updated_by) is not int or updated_by not in range(1, 7):
            raise ValueError("比赛计时修改终端无效")
        result.update(session_id=session, revision=revision,
                      publisher_terminal_id=publisher, updated_by=updated_by)
    else:
        result.update(session_id="", revision=0, publisher_terminal_id=None, updated_by=None)
    return result
