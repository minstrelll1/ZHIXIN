import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from su17_competition_executor.boot_gps_home import BootGpsHome
from su17_competition_executor.flight_diagnostics import FlightDiagnostics


class LocalizationEvidenceTest(unittest.TestCase):
    def test_home_reasons_are_readonly_and_real_observation_still_records_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'home.json'
            home=BootGpsHome(path,'A')
            invalid=dict(connected=False,armed=False,gps_status=1,location_source=4,gps=None)
            reason=home.diagnostic_reason(**invalid)
            self.assertIn('flight_state_missing',reason)
            self.assertIn('gps_fix_below_3d',reason)
            self.assertIn('global_position_missing',reason)
            self.assertFalse(path.exists())
            good=dict(connected=True,armed=False,gps_status=3,location_source=4,gps=dict(latitude=39.,longitude=121.))
            self.assertEqual(home.diagnostic_reason(**good),'awaiting_home_record')
            home.observe(**good,now=100)
            original=path.read_bytes()
            self.assertEqual(home.diagnostic_reason(**invalid),'recorded')
            self.assertEqual(path.read_bytes(),original)

    def test_optional_sensor_callbacks_use_correct_topics_and_do_not_create_publishers(self):
        callbacks={}
        ros=Mock()
        ros.Subscriber.side_effect=lambda topic,kind,callback,**kwargs: callbacks.setdefault(topic,callback) or Mock()
        packages={'mavros_msgs.msg': SimpleNamespace(StatusText=object,GPSRAW=object,EstimatorStatus=object),
                  'prometheus_msgs.msg': SimpleNamespace(TextInfo=object),
                  'sensor_msgs.msg': SimpleNamespace(NavSatFix=object),
                  'geometry_msgs.msg': SimpleNamespace(PoseStamped=object)}
        diagnostic=FlightDiagnostics()
        with patch.dict('sys.modules',packages): diagnostic.subscribe(ros,'/uav3')
        self.assertEqual(len(callbacks),6)
        callbacks['/uav3/mavros/gpsstatus/gps1/raw'](SimpleNamespace(fix_type=1,satellites_visible=6,eph=500,epv=700))
        data=diagnostic.localization_snapshot(None,None,None,None,None,'missing')
        self.assertEqual(data['samples']['gps_raw']['fix_type'],1)
        self.assertEqual(data['samples']['gps_raw']['satellites_visible'],6)
        ros.Publisher.assert_not_called()
        ros.ServiceProxy.assert_not_called()
        json.dumps(data,allow_nan=False)


if __name__=='__main__': unittest.main()
