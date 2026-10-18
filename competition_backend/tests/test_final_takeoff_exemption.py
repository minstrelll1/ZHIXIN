"""起降区免扫描固定数据回归；所有飞机通信、分派和起飞均模拟。"""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from shapely.geometry import Point, box, shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tools'))
import import_final_scenes
from build_planning_preview import build_data
from competition_backend.final_scenes import PATH
import test_clearance_fallback_dispatch as dispatch
from su17_competition_executor.task_protocol import validate_assignment
from su17_competition_executor.transit_protocol import route_messages


LAYOUTS = [
    [(33.864306940008184,113.70553180567248),(33.864314865041855,113.70561865697313),
     (33.864320412565,113.70567973920657),(33.864250672247806,113.70553276008236),
     (33.86426572982112,113.70563201871171),(33.864276824873436,113.70572841411138)],
    [(33.86468246271279,113.7054870520457),(33.86468858747987,113.70556617720946),
     (33.86470195060649,113.70565871341792),(33.864612306258635,113.70549040480688),
     (33.8646273397894,113.70559434040334),(33.86463235096571,113.70568151219392)],
    [(33.86433432,113.70562057),(33.86433432,113.70552330),(33.86433432,113.70571783),
     (33.86424416,113.70552330),(33.86424416,113.70571783),(33.86424416,113.70562057)],
    [(33.86470258,113.70564454),(33.86470258,113.70545001),(33.86461242,113.70570939),
     (33.86461242,113.70551486),(33.86461242,113.70561212),(33.86470258,113.70554728)],
]


class FinalTakeoffExemptionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixed = json.loads(PATH.read_text(encoding='utf8'))
        with patch.object(import_final_scenes, 'exclude_takeoff_area'):
            cls.before = import_final_scenes.compile_bundle(ROOT/'docs/planning_results_v2.zip')

    def test_only_two_finals_and_only_inside_scan_centers_change(self):
        self.assertEqual(self.before['scenes']['xuchang_small_2'], self.fixed['scenes']['xuchang_small_2'])
        for sid, changed in [('final_scene_1', {'1','4'}), ('final_scene_2', {'1','2'})]:
            before, after = [d['scenes'][sid]['plan'] for d in (self.before,self.fixed)]
            area = after['search_area']; exemption = area['coverage_exempt_areas'][0]
            vertices = exemption['geometry_wgs84']['coordinates'][0]
            for actual, expected in zip(vertices, [(113.70589722222222,33.864219444444444),
                    (113.70581944444444,33.864805555555556),(113.70532222222222,33.86476944444445),
                    (113.7054,33.86416666666667)]):
                self.assertAlmostEqual(actual[0],expected[0],places=12)
                self.assertAlmostEqual(actual[1],expected[1],places=12)
            self.assertFalse(exemption['flight_prohibited'])
            self.assertFalse(exemption['scan_disk_intersection_forbidden'])
            polygon = shape(exemption['geometry_wgs84'])
            for key in ('points_m','points','excluded_polygons_m','scan_exclusion_polygons_m',
                        'departure_point_m','departure_point_wgs84','home_reference_policy'):
                self.assertEqual(before['search_area'][key],area[key])
            for uid, uav in after['planned_uavs'].items():
                old = before['planned_uavs'][uid]; task=uav['task']; previous=old['task']
                self.assertEqual(uav['target_altitude_m'],old['target_altitude_m'])
                for key in ('polygon_m','polygon_holes_m','speed_mps','hover_scan_seconds','scan_passes',
                            'reconnaissance_radius_m','execution_mode','mission_time_limit_s'):
                    self.assertEqual(task[key],previous[key])
                old_layers = previous.get('layers') or [previous]
                new_layers = task.get('layers') or [task]
                for a,b in zip(old_layers,new_layers):
                    expected = [p for p in a['waypoints_wgs84'] if not polygon.covers(Point(p[1],p[0]))]
                    self.assertCountEqual(b['waypoints_wgs84'],expected)
                    self.assertEqual(len(a['waypoints_m'])-len(b['waypoints_m']),1 if uid in changed else 0)
                    self.assertEqual(b['reconnaissance_radius_m'],a['reconnaissance_radius_m'])
                    if task.get('layers'):self.assertEqual(b['altitude_m'],a['altitude_m'])
                if uid not in changed:
                    a,b=copy.deepcopy(previous),copy.deepcopy(task)
                    a.pop('transit_routes');b.pop('transit_routes')
                    self.assertEqual(a,b)
                if task.get('layers'):
                    self.assertEqual(new_layers[0]['waypoints_m'][0],new_layers[1]['waypoints_m'][0])
                    self.assertEqual(task['mission_budget_seconds'],960.)

    def test_all_recipients_have_current_catalog_and_first_point(self):
        for sid, count in [('final_scene_1',59),('final_scene_2',21)]:
            scene=self.fixed['scenes'][sid];plan=scene['plan']
            for uid,uav in plan['planned_uavs'].items():
                task=uav['task'];routes=task['transit_routes'];vehicle=scene['transit_geometry']['vehicles'][uid]
                self.assertEqual(len(routes['return_paths']),count)
                self.assertEqual(len(vehicle['fleet_return_paths_m']),count)
                self.assertEqual(routes['entry_path'][-1],list(reversed(task['waypoints_wgs84'][0])))
                self.assertEqual(vehicle['entry_path_m'][-1],task['waypoints_m'][0])
                for r,local in zip(routes['return_paths'],vehicle['fleet_return_paths_m']):
                    source=plan['planned_uavs'][str(r['source_uav_id'])]['task']
                    self.assertEqual(r['path'][0],list(reversed(source['waypoints_wgs84'][r['waypoint_index']-1])))
                    self.assertEqual(local['path'][0],source['waypoints_m'][r['waypoint_index']-1])
                    self.assertEqual(r['path'][-1],routes['departure'])
                    self.assertEqual(routes['waypoints_by_uav'][str(r['source_uav_id'])][r['waypoint_index']-1],r['path'][0])

    def test_pond_north_improves_without_extra_points_or_parameter_changes(self):
        with patch.object(import_final_scenes, 'refine_pond_north'):
            before=json.loads(json.dumps(import_final_scenes.compile_bundle(ROOT/'docs/planning_results_v2.zip')))
        for sid in ('final_scene_1','xuchang_small_2'):
            self.assertEqual(before['scenes'][sid],self.fixed['scenes'][sid])
        old=before['scenes']['final_scene_2']['plan'];new=self.fixed['scenes']['final_scene_2']['plan']
        for uid,uav in new['planned_uavs'].items():
            previous=old['planned_uavs'][uid];task=uav['task'];original=previous['task']
            self.assertEqual(uav['target_altitude_m'],previous['target_altitude_m'])
            for key in ('polygon_m','polygon_holes_m','speed_mps','hover_scan_seconds','scan_passes',
                        'execution_mode','mission_budget_seconds','layer_transition','entry_path_m'):
                self.assertEqual(task[key],original[key])
            for a,b in zip(original['layers'],task['layers']):
                self.assertEqual(len(a['waypoints_m']),len(b['waypoints_m']))
                self.assertEqual(a['waypoints_m'][0],b['waypoints_m'][0])
                for key in ('altitude_m','reconnaissance_radius_m','scan_execution_order'):
                    self.assertEqual(a[key],b[key])
                removed=set(map(tuple,a['waypoints_m']))-set(map(tuple,b['waypoints_m']))
                added=set(map(tuple,b['waypoints_m']))-set(map(tuple,a['waypoints_m']))
                self.assertEqual(len(removed),1 if uid=='4' else 0)
                self.assertEqual(len(added),1 if uid=='4' else 0)
            if uid!='4':
                a,b=copy.deepcopy(original),copy.deepcopy(task)
                a.pop('transit_routes');b.pop('transit_routes')
                self.assertEqual(a,b)
        self.assertLessEqual(new['planned_uavs']['4']['task']['mission_time_s'],960.)
        preview,_=build_data()
        scene=next(s for s in preview['scenes'] if s['scene_id']=='final_scene_2')
        p=new['search_area']['coverage']['projection']
        def local(lon,lat,z=None):
            return ((lat-p['latitude'])*p['north_m_per_degree'],-(lon-p['longitude'])*p['west_m_per_degree'])
        pond=transform(local,shape(next(e['geometry_wgs84'] for e in scene['exclusions'] if e['id']=='pond_exclusion_anchor')))
        required=transform(local,shape(scene['effective_area_geometry_wgs84']))
        _,west_min,north_max,west_max=pond.bounds
        focus=box(north_max,west_min,north_max+30.,west_max).intersection(required)
        for layer_index,minimum in ((0,.9),(1,.74)):
            ratios=[]
            for plan in (old,new):
                disks=[]
                for uav in plan['planned_uavs'].values():
                    layer=uav['task']['layers'][layer_index]
                    disks.extend(Point(point).buffer(layer['reconnaissance_radius_m']) for point in layer['waypoints_m'])
                ratios.append(unary_union(disks).intersection(focus).area/focus.area)
            self.assertGreater(ratios[1],minimum)
            self.assertGreater(ratios[1]-ratios[0],.5)

    def test_reimport_and_viewer_effective_area_match_runtime(self):
        compiled=import_final_scenes.compile_bundle(ROOT/'docs/planning_results_v2.zip')
        self.assertEqual(json.loads(json.dumps(compiled)),self.fixed)
        preview,_=build_data()
        for scene in preview['scenes']:
            sid=scene['scene_id'];ex=[e for e in scene['exclusions'] if e['id']=='competition_takeoff_area']
            self.assertEqual(len(ex),0 if sid=='xuchang_small_2' else 1)
            if not ex:continue
            self.assertLess(shape(scene['effective_area_geometry_wgs84']).intersection(shape(ex[0]['geometry_wgs84'])).area,1e-15)
            area=self.fixed['scenes'][sid]['plan']['search_area']
            for layer in ('upper','lower'):
                self.assertAlmostEqual(scene['scene_metrics'][layer]['coverage_ratio'],area['coverage']['layer_metrics'][layer]['coverage_ratio'],places=9)

    def test_four_home_layouts_dispatch_protocol_and_takeoff(self):
        for sid in ('final_scene_1','final_scene_2'):
            for layout in LAYOUTS:
                with self.subTest(scene=sid,first_home=layout[0]):
                    fixture=dispatch.ClearanceFallbackDispatchTest()
                    try:
                        fixture.setup_scene(sid)
                        for uid,(lat,lon) in enumerate(layout,1):
                            fixture.home=dict(latitude=lat,longitude=lon);fixture.update(uid)
                        result=fixture.plan();mission=result['mission']
                        self.assertEqual(result['dispatch_status']['assigned_uav_ids'],list(range(1,7)))
                        self.assertEqual(result['planning_warnings'],[])
                        for call in fixture.adapter.command_assign_task.call_args_list:
                            uid,payload=call.args
                            checked=validate_assignment(json.loads(json.dumps(dict(payload,type='assign_task',uav_id=uid),allow_nan=False)),uid)
                            entry,returning=route_messages(checked)
                            self.assertEqual(entry['boundary_clearance_m'],5.)
                            self.assertEqual(entry['path'][0][:2],list(reversed(layout[uid-1])))
                            self.assertEqual(entry['path'][-1][:2],list(reversed(checked['task']['waypoints_wgs84'][0])))
                            self.assertEqual(returning['landing_point'][:2],list(reversed(layout[uid-1])))
                        fixture.acknowledge(mission)
                        ready=fixture.client.post('/api/v1/takeoff/prepare').json()
                        self.assertTrue(ready['ready'],ready)
                        response=fixture.client.post('/api/v1/takeoff/confirm',json={'token':ready['confirmation_token']})
                        self.assertEqual(response.status_code,200,response.text[:1000])
                        self.assertEqual([c.args[0] for c in fixture.adapter.command_takeoff.call_args_list],list(range(1,7)))
                    finally:fixture.doCleanups()


if __name__=='__main__':unittest.main()
