import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from competition_backend.api import create_app


class TerrainAssetsTest(unittest.TestCase):
    def test_offline_assets_and_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(environment={"COMPETITION_ADAPTER": "sim", "COMPETITION_DATA_DIR": directory})
            # 不启动生命周期、通信线程或真实设备；只检查同源静态资源。
            client = TestClient(app)
            for name, content_type in [("terrain_basemap.js", "application/javascript"),
                                       ("competition_esri_20170724.jpg", "image/jpeg")]:
                response = client.get("/map-assets/" + name)
                self.assertEqual(response.status_code, 200)
                self.assertIn(content_type, response.headers['content-type'])
                self.assertIn('max-age=', response.headers['cache-control'])
                self.assertGreater(len(response.content), 1000)
            self.assertEqual(client.get('/map-assets/fleet.json').status_code, 404)
            self.assertEqual(client.get('/map-assets/index.html').status_code, 404)
            self.assertIn('/map-assets/terrain_basemap.js', client.get('/').text)
            client.close()


if __name__ == '__main__':
    unittest.main()
