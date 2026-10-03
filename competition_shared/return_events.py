"""只记录到达返航点并开始下降，不将返航请求、任务完成或普通降落当作成功。"""
import math


def matches_return_event(event, mission_id, uav_id, session_id=None, checksum=None):
    return (isinstance(event, dict) and event.get('phase') == 'return_descent'
            and event.get('mission_id') == mission_id and event.get('uav_id') == uav_id
            and isinstance(event.get('event_id'), str) and bool(event['event_id'])
            and (session_id is None or event.get('competition_session_id') == session_id)
            and (checksum is None or event.get('assignment_checksum') == checksum))


def competition_elapsed(state, received_at, now, mission_id):
    if (not isinstance(state, dict) or state.get('mission_id') != mission_id
            or state.get('running') is not True or not state.get('session_id')):
        return None
    try:
        elapsed = float(state['elapsed_seconds'])
        if not math.isfinite(elapsed) or elapsed < 0:
            return None
        return elapsed + max(0.0, now - received_at)
    except (KeyError, TypeError, ValueError):
        return None
