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
            try:
                fresh = isinstance(gps, dict) and 0 <= float(gps['age_seconds']) <= 2 and self._valid(gps)
            except (KeyError, TypeError, ValueError):
                fresh = False
            if fresh:
                self.home = dict(latitude=float(gps['latitude']), longitude=float(gps['longitude']),
                    boot_id=self.boot_id, captured_at=float(now), source='first_valid_unarmed_gps_per_boot')
                # /tmp 文件以 boot_id 校验：断线或重启 ROS 节点不改落点，真正重启飞机才清空。
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps(self.home), encoding='utf-8')
                temporary.replace(self.path)
        return dict(self.home) if self.home else None
