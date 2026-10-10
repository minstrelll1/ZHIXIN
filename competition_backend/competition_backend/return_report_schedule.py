"""成功返航与比赛时间的上报节拍；不访问网络，不控制飞行。"""

import math


class ReturnReportSchedule:
    RETURN_DELAY = 3.0
    RETURN_INTERVAL = 2.0
    RETURN_COUNT = 10
    CLOCK_START = 1440.0
    CLOCK_END = 1530.0
    CLOCK_INTERVAL = 5.0

    def __init__(self):
        self.participants = frozenset()
        self.returned = {}
        self.return_next = None
        self.return_attempts = 0
        self.clock_slots = set()
        self.clock_next = None
        self.selected_strategy = None

    def set_participants(self, uav_ids):
        """固定本次起飞名单；遥测失联或先后降落不得缩减名单。"""
        ids = frozenset(int(uid) for uid in uav_ids)
        if not ids or not ids.issubset(range(1, 7)):
            raise ValueError('起飞参与名单必须是1～6号无人机的非空子集')
        if self.participants:
            if ids != self.participants:
                raise ValueError('本次起飞参与名单已固定，不能随在线状态改变')
            return False
        self.participants = ids
        return True

    def _all_returned_at(self):
        if self.participants and self.participants.issubset(self.returned):
            return max(self.returned[uid]['at'] for uid in self.participants)
        return None

    def observe(self, telemetry, mission_id, session_id, checksums, now):
        """只接受本任务、本比赛、本机分派版本的到点下降事件。"""
        added = []
        for key, item in (telemetry or {}).items():
            try:
                uid = int(key)
                if uid not in self.participants or uid in self.returned:
                    continue
                if not isinstance(item, dict):
                    item = vars(item)
                event = item.get('successful_return') or {}
                checksum = checksums.get(str(uid))
                if (event.get('phase') != 'return_descent'
                        or event.get('mission_id') != mission_id
                        or event.get('competition_session_id') != session_id
                        or event.get('uav_id') != uid
                        or not checksum or event.get('assignment_checksum') != checksum
                        or not event.get('event_id')):
                    continue
                age = float(event.get('age_seconds', 0))
                if not math.isfinite(age) or age < 0:
                    continue
                self.returned[uid] = dict(event_id=str(event['event_id']), at=now - age)
                added.append(uid)
            except (TypeError, ValueError, AttributeError):
                continue
        returned_at = self._all_returned_at()
        if returned_at is not None and self.return_next is None and not self.return_attempts:
            self.return_next = returned_at + self.RETURN_DELAY
        return added

    def due(self, now, elapsed):
        if not math.isfinite(elapsed):
            return []
        if self.selected_strategy is None:
            # 本次起飞机的最后一架到点下降即选中返航计划，等3秒再发送。
            # 同次轮询发现两个条件时，按最后一架事件年龄比较先后。
            returned_at = self._all_returned_at()
            clock_at = now - (elapsed - self.CLOCK_START) if elapsed >= self.CLOCK_START else None
            if returned_at is not None and (clock_at is None or returned_at < clock_at):
                self.selected_strategy = 'all_participants_returned'
            elif clock_at is not None:
                self.selected_strategy = 'competition_time'
        reasons = []
        if (self.selected_strategy == 'all_participants_returned'
                and self.return_next is not None and now >= self.return_next
                and self.return_attempts < self.RETURN_COUNT):
            self.return_attempts += 1
            reasons.append('all_participants_returned')
            self.return_next = (now + self.RETURN_INTERVAL
                                if self.return_attempts < self.RETURN_COUNT else None)
        if self.selected_strategy == 'competition_time' and self.CLOCK_START <= elapsed < self.CLOCK_END:
            slot = int((elapsed - self.CLOCK_START) // self.CLOCK_INTERVAL)
            if slot not in self.clock_slots and (self.clock_next is None or now >= self.clock_next):
                self.clock_slots.add(slot)
                self.clock_next = now + self.CLOCK_INTERVAL
                reasons.append('competition_time')
        # 只执行最先满足的一套计划；不集中补发因暂停或时间调整错过的节拍。
        return reasons

    def snapshot(self):
        return dict(participant_uav_ids=sorted(self.participants),
                    pending_return_uav_ids=sorted(self.participants.difference(self.returned)),
                    returned_uav_ids=sorted(self.returned),
                    selected_strategy=self.selected_strategy,
                    return_attempts=self.return_attempts, return_total=self.RETURN_COUNT,
                    clock_attempts=len(self.clock_slots),
                    clock_start_seconds=self.CLOCK_START, clock_end_seconds=self.CLOCK_END)
