import os
import unittest
from unittest.mock import patch

from competition_backend.api import create_app


class RestartEndpointTest(unittest.TestCase):
    def test_local_restart_routes_only_requested_id(self):
        with patch.dict(os.environ, {"COMPETITION_ADAPTER": "tcp", "COMPETITION_ACTIVE_UAV_IDS": "1,3"}):
            app = create_app()
        endpoint = next(r.endpoint for r in app.routes if r.path == "/api/v1/uavs/{uav_id}/restart-executor")
        with patch.object(app.state.adapter, "forward_command") as forward:
            self.assertTrue(endpoint(3)["sent"])
            forward.assert_called_once_with(3, "restart_executor", {})

    def test_remote_restart_uses_peer_route(self):
        with patch.dict(os.environ, {"COMPETITION_ADAPTER": "distributed", "COMPETITION_LOCAL_UAV_ID": "1", "COMPETITION_GROUND_PEERS": "3=http://192.168.2.123:8000", "COMPETITION_PEER_TOKEN": "test"}):
            app = create_app()
        endpoint = next(r.endpoint for r in app.routes if r.path == "/api/v1/uavs/{uav_id}/restart-executor")
        with patch.object(app.state.adapter, "_request_json") as request:
            endpoint(3)
            self.assertEqual(request.call_args.args[0], "http://192.168.2.123:8000/api/v1/peer/command")
            self.assertEqual(request.call_args.kwargs["payload"]["command_type"], "restart_executor")


if __name__ == "__main__":
    unittest.main()
