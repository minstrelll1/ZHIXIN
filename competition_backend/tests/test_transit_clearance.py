import math
import unittest

from shapely.geometry import LineString, Point, Polygon

from competition_backend.transit_routes import clearance_region, clearance_routes


class ClearanceGeometryTest(unittest.TestCase):
    def test_concave_boundary_uses_interior_corners_with_five_metre_clearance(self):
        boundary = [[0,0],[100,0],[100,100],[60,100],[60,40],[40,40],[40,100],[0,100]]
        route = clearance_routes(boundary,[20,80],[[80,80]],5)[0]
        outer = Polygon(boundary)
        self.assertGreater(len(route),2)
        self.assertTrue(outer.covers(LineString(route)))
        self.assertGreaterEqual(LineString(route).distance(outer.boundary),5)

    def test_required_takeoff_connector_enters_once_then_stays_in_safe_region(self):
        boundary = [[0,0],[100,0],[100,100],[0,100]]
        outer, safe = Polygon(boundary), clearance_region(boundary,5)
        for home in ([-20,50],[0,0],[2,10],[50,50]):
            route = clearance_routes(boundary,home,[[80,80]],5)[0]
            self.assertEqual(route[0],home)
            self.assertEqual(route[-1],[80,80])
            reached = safe.covers(Point(home))
            for a,b in zip(route,route[1:]):
                line=LineString([a,b])
                if reached:
                    self.assertGreaterEqual(line.distance(outer.boundary),5)
                else:
                    self.assertTrue(safe.buffer(2e-5).covers(Point(b)))
                    intersection=line.intersection(outer)
                    self.assertEqual(intersection.geom_type,'LineString')
                    self.assertLess(intersection.distance(Point(b)),1e-6)
                    reached=True

    def test_near_boundary_target_requires_explicit_connector_policy(self):
        boundary = [[0,0],[100,0],[100,100],[0,100]]
        with self.assertRaisesRegex(ValueError,'侦察航点距离'):
            clearance_routes(boundary,[50,50],[[0,20]],5)
        route=clearance_routes(boundary,[50,50],[[0,20]],5,allow_target_connectors=True)[0]
        self.assertEqual(route[-1],[0,20])
        self.assertGreaterEqual(LineString(route[:-1]).distance(Polygon(boundary).boundary),5)
        self.assertTrue(Polygon(boundary).covers(LineString(route)))
        self.assertAlmostEqual(math.dist(route[-1],route[-2]),5.02)

    def test_clearance_does_not_silently_cross_a_narrow_disconnected_neck(self):
        boundary = [[0,0],[30,0],[30,14],[60,14],[60,0],[90,0],[90,30],
                    [60,30],[60,16],[30,16],[30,30],[0,30]]
        with self.assertRaisesRegex(ValueError,'没有连通'):
            clearance_routes(boundary,[15,15],[[75,15]],5)

    def test_clearance_independent_of_local_translation(self):
        boundary = [[0,0],[100,0],[100,100],[60,100],[60,40],[40,40],[40,100],[0,100]]
        original=clearance_routes(boundary,[20,80],[[80,80]],5)[0]
        for dx,dy in ((1000,2000),(-250,-750)):
            shift=lambda p:[p[0]+dx,p[1]+dy]
            route=clearance_routes([shift(p) for p in boundary],shift([20,80]),[shift([80,80])],5)[0]
            self.assertEqual(len(route),len(original))
            for p,q in zip(route,original):
                self.assertLess(math.dist(p,shift(q)),1e-9)
