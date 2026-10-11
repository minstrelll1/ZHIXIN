"""Batch geometry must preserve the existing scalar visibility/shortest-path result."""
import math
import unittest
from unittest.mock import patch

import numpy as np
from shapely import prepare
from shapely.geometry import LineString, Point, box

from competition_backend.landing_avoidance import _visible_pairs, routes_from_home


def scalar_visibility(region, starts, ends):
    return np.array([math.dist(a,b)<1e-9 or region.covers(LineString([a,b]))
                     for a,b in zip(starts,ends)],dtype=bool)


class FastLandingAvoidanceTest(unittest.TestCase):
    def test_chunked_visibility_preserves_scalar_results_and_coincident_points(self):
        region=box(5,5,95,95).difference(Point(50,50).buffer(10,quad_segs=4)).buffer(1e-8)
        prepare(region)
        random=np.random.default_rng(124)
        starts=random.uniform(0,100,size=(5001,2))
        ends=random.uniform(0,100,size=(5001,2))
        ends[::17]=starts[::17]
        np.testing.assert_array_equal(_visible_pairs(region,starts,ends),scalar_visibility(region,starts,ends))

    def test_optimized_shortest_paths_match_scalar_paths_exactly(self):
        boundary=[[0,0],[100,0],[100,100],[60,100],[60,60],[40,60],[40,100],[0,100]]
        home=[20,20];targets=[[80,80],[20,80],[80,20]];others=[[45,20],[65,20],[30,40]]
        optimized=routes_from_home(boundary,home,targets,others)
        with patch('competition_backend.landing_avoidance._visible_pairs',side_effect=scalar_visibility):
            reference=routes_from_home(boundary,home,targets,others)
        self.assertEqual(optimized,reference)
        for path in optimized:
            line=LineString(path)
            for other in others:self.assertGreaterEqual(line.distance(Point(other)),2.5)


if __name__=='__main__':unittest.main()
