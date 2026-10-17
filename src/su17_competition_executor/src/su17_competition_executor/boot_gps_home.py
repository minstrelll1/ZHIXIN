"""记录本次开机首次有效、未解锁 GPS。只采集，不校时、不改变飞行。"""
import json
import math
from pathlib import Path


class BootGpsHome:
    def __init__(self, path, boot_id):
        self.path, self.boot_id, self.home = Path(path), str(boot_id), None
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if (isinstance(saved, dict) and self.boot_id and saved.get('boot_id') == self.boot_id
                    and saved.get('source') == 'first_valid_unarmed_gps_per_boot' and self._valid(saved)):
                self.home = saved
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _valid(gps):
        try:
            lat, lon = float(gps['latitude']), float(gps['longitude'])
            return math.isfinite(lat) and math.isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180
        except (KeyError, TypeError, ValueError):
            return False

    def observe(self, *, connected, armed, gps_status, location_source, gps, now):
        if self.home is None and connected and not armed and gps_status >= 3 and location_source in (4, 5):
            # GPS接收年龄只用于诊断；仍须飞控连接、未解锁且GPS状态有效。
            if isinstance(gps, dict) and self._valid(gps):
                self.home = dict(latitude=float(gps['latitude']), longitude=float(gps['longitude']),
                    boot_id=self.boot_id, captured_at=float(now), source='first_valid_unarmed_gps_per_boot')
                # /tmp 文件以 boot_id 校验：断线或重启 ROS 节点不改落点，真正重启飞机才清空。
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps(self.home), encoding='utf-8')
                temporary.replace(self.path)
        return dict(self.home) if self.home else None

    def diagnostic_reason(self, *, connected, armed, gps_status, location_source, gps):
        """逐项说明现有记录条件；只读，不修改或放宽条件。"""
        if self.home is not None:
            return 'recorded'
        reasons = []
        if not connected:
            reasons.append('flight_state_missing_or_disconnected')
        if armed:
            reasons.append('armed_before_home_recorded')
        if gps_status < 3:
            reasons.append('gps_fix_below_3d')
        if location_source not in (4, 5):
            reasons.append('location_source_not_gps_or_rtk')
        if not isinstance(gps, dict) or not self._valid(gps):
            reasons.append('global_position_missing_or_invalid')
        return ','.join(reasons) or 'awaiting_home_record'
