import copy
import unittest
from shapely.geometry import Polygon
from competition_backend.task_region import build_task_region
from competition_shared.task_region import task_region_message, validate_task_region
from competition_backend.transit_routes import scene_plan, PROFILES


class TaskRegionTest(unittest.TestCase):
    def test_fixed_scenes_match_plan_and_keep_exclusion(self):
        for profile in PROFILES:
            departures = ['stadium_center'] if profile=='subject1_actual' else ['fixed_dalian'] if profile=='dalian_nanshan' else ['fixed_xuchang'] if profile=='xuchang_small' else ['southeast','stadium_center']
            for departure in departures:
                plan=scene_plan(profile,departure);area=plan['search_area']
                for uid, wrapper in plan['planned_uavs'].items():
                    with self.subTest(profile=profile,departure=departure,uav=uid):
                        task=wrapper['task'];region=build_task_region(task,area,int(uid))
                        validate_task_region(region,int(uid))
                        self.assertGreater(len(region['polygons']),0)
                        self.assertTrue(all(p['outer'][0]==p['outer'][-1] for p in region['polygons']))
                        if region['coordinate_frame']=='ENU':
                            out=Polygon(region['polygons'][0]['outer'],region['polygons'][0]['holes'])
                            self.assertAlmostEqual(out.area,Polygon(task['polygon_m']).area,places=4)

    def test_gps_projection_and_hole_are_not_filled(self):
        task=dict(polygon_m=[[0,0],[20,0],[20,20],[0,20]],waypoints_m=[[0,0],[20,20]],
                  waypoints_wgs84=[[34.,113.],[34.0002,112.9998]],coordinate_frame='LOCAL_NORTH_WEST')
        area=dict(coordinate_mode='gps',excluded_polygons_m=[[[5,5],[15,5],[15,15],[5,15]]])
        d=build_task_region(task,area,4)
        self.assertEqual(d['coordinate_order'],'longitude_latitude')
        self.assertEqual(len(d['polygons'][0]['holes']),1)
        self.assertIn([112.9998,34.0002],d['polygons'][0]['outer'])
        assignment=dict(uav_id=4,mission_id='subject1-test',assignment_checksum='test',
                        target_altitude_m=52.,task=dict(task_region=d))
        message=task_region_message(assignment)
        self.assertEqual(message['relative_altitude_m'],52.)
        self.assertEqual(message['polygons'],d['polygons'])
        d['uav_id']=1
        with self.assertRaises(ValueError):task_region_message(assignment)

    def test_xyz_rectangular_fallback_and_no_altitude_change(self):
        task=dict(bounds_m=dict(x_min=0,x_max=3,y_min=0,y_max=2))
        region=build_task_region(task,{},1)
        self.assertEqual(region['coordinate_frame'],'ENU')
        self.assertEqual(Polygon(region['polygons'][0]['outer']).area,6.)
