from __future__ import annotations

import asyncio
import hmac
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

from .adapter import RecordingAdapter
from .config import load_config
from .distributed_adapter import DistributedFleetAdapter, parse_ground_peers
from .image_aggregation import (
    PeerImageCollector,
    local_image_manifest,
    resolve_image_file,
)
from .journal import EventJournal
from .models import ReturnReason, Telemetry
from .orchestrator import CompetitionOrchestrator, MissionError
from .pointcloud import (
    DEFAULT_GROUNDSTATION_RELAY_TOPIC_TEMPLATE,
    DEFAULT_ONBOARD_TOPIC_TEMPLATE,
    POINTCLOUD_SOURCE_GROUNDSTATION_RELAY,
    POINTCLOUD_SOURCE_GROUNDSTATION_SHARED,
    POINTCLOUD_SOURCE_ONBOARD,
    RosbridgePointCloudCollector,
    parse_pointcloud_hosts,
    parse_pointcloud_topics,
    parse_pointcloud_uav_ids,
)
from .recording_manager import FlightRecordingManager, parse_ssh_hosts
from .ros_adapter import RosFleetAdapter
from .tcp_adapter import TcpFleetAdapter
from .traffic_monitor import TrafficMonitor, parse_uav_traffic_hosts


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_PATH = Path(__file__).resolve().parent / "web" / "index.html"


def _parse_video_sources(raw: str) -> Dict[str, str]:
    """Parse ``UAV_ID=http(s)://...`` entries separated by semicolons."""
    sources: Dict[str, str] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        uav_id_text, separator, url = entry.partition("=")
        if not separator:
            raise ValueError(
                "COMPETITION_VIDEO_SOURCES entries must be UAV_ID=http(s)://..."
            )
        uav_id = int(uav_id_text.strip())
        url = url.strip()
        if uav_id not in range(1, 7):
            raise ValueError("video source UAV ID must be between 1 and 6")
        if not url.startswith(("http://", "https://")):
            raise ValueError("video source URL must use http:// or https://")
        sources[str(uav_id)] = url
    return sources


def _mission_call(function: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except MissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def create_app() -> FastAPI:
    config_path = os.environ.get(
        "COMPETITION_CONFIG",
        str(PACKAGE_ROOT / "config" / "competition.example.json"),
    )
    adapter_name = os.environ.get("COMPETITION_ADAPTER", "sim").strip().lower()
    config = load_config(config_path)
    video_sources = _parse_video_sources(
        os.environ.get("COMPETITION_VIDEO_SOURCES", "")
    )
    active_uav_ids = [
        int(value.strip())
        for value in os.environ.get(
            "COMPETITION_ACTIVE_UAV_IDS",
            ",".join(str(value) for value in config.uav_ids),
        ).split(",")
        if value.strip()
    ]
    local_uav_id = int(os.environ.get("COMPETITION_LOCAL_UAV_ID", "1"))
    ground_node_id = os.environ.get(
        "COMPETITION_GROUND_NODE_ID", "ground-uav{}".format(local_uav_id)
    )
    ground_peers = parse_ground_peers(os.environ.get("COMPETITION_GROUND_PEERS", ""))
    pointcloud_source_mode = os.environ.get(
        "COMPETITION_POINTCLOUD_SOURCE", POINTCLOUD_SOURCE_GROUNDSTATION_SHARED
    ).strip().lower()
    pointcloud_topics = parse_pointcloud_topics(
        os.environ.get("COMPETITION_POINTCLOUD_TOPICS", "")
    )
    if pointcloud_source_mode in (
        POINTCLOUD_SOURCE_GROUNDSTATION_RELAY,
        POINTCLOUD_SOURCE_GROUNDSTATION_SHARED,
    ):
        relay_uav_ids = parse_pointcloud_uav_ids(
            os.environ.get("COMPETITION_POINTCLOUD_RELAY_UAV_IDS", ""),
            config.uav_ids,
        )
        if pointcloud_source_mode == POINTCLOUD_SOURCE_GROUNDSTATION_SHARED:
            # IDs are retained for status and validation; the collector opens
            # no rosbridge socket in this mode.
            pointcloud_hosts = {uav_id: "groundstation-shared-ingress" for uav_id in relay_uav_ids}
            pointcloud_port = 0
        else:
            relay_host = os.environ.get("COMPETITION_POINTCLOUD_RELAY_HOST", "127.0.0.1").strip()
            if not relay_host:
                raise ValueError("COMPETITION_POINTCLOUD_RELAY_HOST cannot be empty")
            pointcloud_hosts = {uav_id: relay_host for uav_id in relay_uav_ids}
            pointcloud_port = int(os.environ.get("COMPETITION_POINTCLOUD_RELAY_PORT", "9090"))
        pointcloud_default_topic_template = os.environ.get(
            "COMPETITION_POINTCLOUD_RELAY_TOPIC_TEMPLATE",
            DEFAULT_GROUNDSTATION_RELAY_TOPIC_TEMPLATE,
        )
    elif pointcloud_source_mode == POINTCLOUD_SOURCE_ONBOARD:
        pointcloud_hosts = parse_pointcloud_hosts(os.environ.get("COMPETITION_POINTCLOUD_ROSBRIDGE_HOSTS", ""))
        pointcloud_port = int(os.environ.get("COMPETITION_POINTCLOUD_ROSBRIDGE_PORT", "9090"))
        pointcloud_default_topic_template = DEFAULT_ONBOARD_TOPIC_TEMPLATE
    else:
        raise ValueError(
            "COMPETITION_POINTCLOUD_SOURCE must be 'onboard', 'groundstation_relay', or 'groundstation_shared'"
        )
    peer_token = os.environ.get("COMPETITION_PEER_TOKEN", "")
    if os.environ.get("COMPETITION_CONFIRM_LIVE_CONFIG", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        config.safety.production_config_confirmed = True
    if adapter_name == "ros":
        adapter = RosFleetAdapter(active_uav_ids)
        live_mode = True
    elif adapter_name == "tcp":
        adapter = TcpFleetAdapter(
            active_uav_ids,
            bind_host=os.environ.get("COMPETITION_TCP_BIND", "0.0.0.0"),
            port=int(os.environ.get("COMPETITION_TCP_PORT", "56100")),
            auth_token=os.environ.get("COMPETITION_TCP_TOKEN", ""),
        )
        live_mode = True
    elif adapter_name == "distributed":
        # Planning/execution always covers all six UAVs.  Only the paired UAV is
        # allowed to connect directly to this computer's TCP port.
        active_uav_ids = list(config.uav_ids)
        adapter = DistributedFleetAdapter(
            active_uav_ids,
            local_uav_id=local_uav_id,
            node_id=ground_node_id,
            peers=ground_peers,
            bind_host=os.environ.get("COMPETITION_TCP_BIND", "0.0.0.0"),
            port=int(os.environ.get("COMPETITION_TCP_PORT", "56100")),
            uav_auth_token=os.environ.get("COMPETITION_TCP_TOKEN", ""),
            peer_token=peer_token,
        )
        video_sources = {
            key: value
            for key, value in video_sources.items()
            if int(key) == local_uav_id
        }
        live_mode = True
    elif adapter_name == "sim":
        adapter = RecordingAdapter()
        live_mode = False
    else:
        raise ValueError(
            "COMPETITION_ADAPTER must be 'sim', 'tcp', 'distributed', or 'ros'"
        )

    data_directory = os.environ.get(
        "COMPETITION_DATA_DIR", str(PACKAGE_ROOT / "data")
    )
    orchestrator = CompetitionOrchestrator(
        config=config,
        adapter=adapter,
        journal=EventJournal(data_directory),
        live_mode=live_mode,
        active_uav_ids=active_uav_ids,
    )
    if isinstance(adapter, DistributedFleetAdapter):
        adapter.set_snapshot_provider(orchestrator.snapshot)
    recording_manager = FlightRecordingManager(
        project_root=PACKAGE_ROOT.parent,
        ssh_hosts=parse_ssh_hosts(
            os.environ.get("COMPETITION_UAV_SSH_HOSTS", "1=192.168.1.88")
        ),
        ssh_user=os.environ.get("COMPETITION_UAV_SSH_USER", "amov"),
    )
    image_root = Path(
        os.environ.get("COMPETITION_IMAGE_ROOT", str(PACKAGE_ROOT.parent / "received_images"))
    )
    image_collector = (
        PeerImageCollector(
            image_root,
            local_uav_id=local_uav_id,
            peers=ground_peers,
            peer_token=peer_token,
            interval_sec=float(os.environ.get("COMPETITION_IMAGE_SYNC_INTERVAL", "5")),
            should_collect=lambda: adapter.is_coordinator,
        )
        if isinstance(adapter, DistributedFleetAdapter)
        else None
    )
    pointcloud_collector = RosbridgePointCloudCollector(
        Path(
            os.environ.get(
                "COMPETITION_POINTCLOUD_ROOT",
                str(PACKAGE_ROOT.parent / "pointcloud_records"),
            )
        ),
        hosts=pointcloud_hosts,
        topics=pointcloud_topics,
        port=pointcloud_port,
        source_mode=pointcloud_source_mode,
        default_topic_template=pointcloud_default_topic_template,
        max_points=int(os.environ.get("COMPETITION_POINTCLOUD_MAX_POINTS", "20000")),
        save_interval_sec=float(
            os.environ.get("COMPETITION_POINTCLOUD_SAVE_INTERVAL", "1.0")
        ),
        max_frames_per_uav=int(
            os.environ.get("COMPETITION_POINTCLOUD_MAX_FRAMES", "300")
        ),
    )
    traffic_hosts = parse_uav_traffic_hosts(
        os.environ.get("COMPETITION_UAV_TRAFFIC_HOSTS", "")
    )
    traffic_monitor = TrafficMonitor(
        traffic_hosts,
        interval_sec=float(os.environ.get("COMPETITION_TRAFFIC_INTERVAL", "1.0"))
    )
    # MediaMTX pulls the gimbal RTSP stream from the camera computer. Include
    # that endpoint in the same UAV accounting even though it is not the
    # onboard computer IP used by the task/telemetry link.
    video_rtsp_source = os.environ.get("COMPETITION_VIDEO_RTSP_SOURCE", "")
    video_host = ""
    if video_rtsp_source:
        try:
            video_host = urlsplit(video_rtsp_source).hostname or ""
        except ValueError:
            video_host = ""
    if video_host and local_uav_id in traffic_hosts:
        traffic_monitor.video_hosts[local_uav_id] = video_host
        traffic_monitor._host_to_uav[video_host] = local_uav_id
    pointcloud_collector.traffic_recorder = traffic_monitor.record_application_bytes

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        orchestrator.start()
        if image_collector is not None:
            image_collector.start()
        pointcloud_collector.start()
        traffic_monitor.start()
        yield
        traffic_monitor.stop()
        pointcloud_collector.stop()
        if image_collector is not None:
            image_collector.stop()
        recording_manager.close()
        orchestrator.stop()

    app = FastAPI(
        title="SU17 六机竞赛任务后端",
        description="六架无人机的任务规划、起飞预检、任务执行和自动返航接口。",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.orchestrator = orchestrator
    app.state.adapter = adapter
    app.state.recording_manager = recording_manager
    app.state.image_collector = image_collector
    app.state.pointcloud_collector = pointcloud_collector
    app.state.traffic_monitor = traffic_monitor

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def frontend() -> HTMLResponse:
        return HTMLResponse(FRONTEND_PATH.read_text(encoding="utf-8"))

    @app.get("/health", summary="检查后端健康状态", tags=["系统"])
    def health() -> Dict[str, Any]:
        result = {
            "ok": True,
            "adapter": adapter_name,
            "live_mode": live_mode,
            "active_uav_ids": active_uav_ids,
            "pointcloud_source_mode": pointcloud_source_mode,
            "traffic_monitor": traffic_monitor.status(),
            "production_config_confirmed": config.safety.production_config_confirmed,
        }
        if isinstance(adapter, TcpFleetAdapter):
            result["tcp_bind"] = adapter.bind_host
            result["tcp_port"] = adapter.bound_port
            result["connected_uav_ids"] = adapter.connected_uav_ids
        if isinstance(adapter, DistributedFleetAdapter):
            result.update(
                {
                    "ground_node_id": ground_node_id,
                    "local_uav_id": local_uav_id,
                    "tcp_bind": adapter.bind_host,
                    "tcp_port": adapter.bound_port,
                    "directly_connected_uav_ids": adapter.connected_uav_ids,
                    "configured_peer_uav_ids": sorted(ground_peers),
                    "peer_sync": adapter.peer_sync_status(),
                    "image_aggregation": image_collector.status(),
                }
            )
        return result

    @app.get("/api/v1/status", summary="获取六机任务状态", tags=["任务状态"])
    def status() -> Dict[str, Any]:
        result = orchestrator.snapshot()
        result["pointcloud"] = pointcloud_collector.status()
        result["traffic"] = traffic_monitor.status()
        if isinstance(adapter, DistributedFleetAdapter):
            result["traffic"]["by_uav"].update(adapter.peer_traffic_status())
            mirrored = adapter.mirrored_snapshot()
            if mirrored is not None and not adapter.is_coordinator:
                result["mission"] = mirrored.get("mission")
            result["ground_node_id"] = ground_node_id
            result["local_uav_id"] = local_uav_id
            result["image_aggregation"] = image_collector.status()
            result["recordable_uav_ids"] = [local_uav_id]
        return result

    @app.get("/api/v1/video-sources", summary="获取六机视频源配置", tags=["任务状态"])
    def get_video_sources() -> Dict[str, Any]:
        result: Dict[str, Any] = {"sources": video_sources}
        if isinstance(adapter, DistributedFleetAdapter):
            result["local_uav_id"] = local_uav_id
        return result

    @app.get("/api/v1/pointcloud/status", summary="获取科目三点云接收状态", tags=["科目三点云"])
    def pointcloud_status() -> Dict[str, Any]:
        return pointcloud_collector.status()

    @app.get("/api/v1/traffic/status", summary="获取各机地分项通信统计", tags=["通信流量"])
    def traffic_status() -> Dict[str, Any]:
        return traffic_monitor.status()

    @app.post("/api/v1/traffic/report", summary="上报本地中继或 UDP 字节数", tags=["通信流量"])
    def traffic_report(
        payload: Dict[str, Any] = Body(...),
        traffic_token: str = Header(default="", alias="X-Traffic-Token"),
    ) -> Dict[str, Any]:
        expected = os.environ.get("COMPETITION_TRAFFIC_REPORT_TOKEN", "")
        if expected and not hmac.compare_digest(traffic_token, expected):
            raise HTTPException(status_code=401, detail="invalid traffic report token")
        try:
            uav_id = int(payload["uav_id"])
            category = str(payload["category"])
            sent_bytes = int(payload.get("sent_bytes", 0))
            received_bytes = int(payload.get("received_bytes", 0))
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="invalid traffic report") from error
        if uav_id not in range(1, 7) or sent_bytes < 0 or received_bytes < 0:
            raise HTTPException(status_code=422, detail="invalid traffic report")
        traffic_monitor.record_application_bytes(
            uav_id,
            category,
            sent_bytes=sent_bytes,
            received_bytes=received_bytes,
        )
        return traffic_monitor.status_for_uav(uav_id)

    @app.post("/api/v1/pointcloud/{uav_id}/ingest", summary="接收 GroundStation 已获取的点云", tags=["科目三点云"])
    def ingest_pointcloud(
        uav_id: int,
        payload: Dict[str, Any] = Body(...),
        pointcloud_token: str = Header(default="", alias="X-Pointcloud-Token"),
    ) -> Dict[str, Any]:
        if uav_id not in range(1, 7):
            raise HTTPException(status_code=404, detail="unknown UAV ID")
        if pointcloud_source_mode != POINTCLOUD_SOURCE_GROUNDSTATION_SHARED:
            raise HTTPException(
                status_code=409,
                detail="pointcloud ingress requires COMPETITION_POINTCLOUD_SOURCE=groundstation_shared",
            )
        expected = os.environ.get("COMPETITION_POINTCLOUD_INGEST_TOKEN", "")
        if expected and not hmac.compare_digest(pointcloud_token, expected):
            raise HTTPException(status_code=401, detail="invalid pointcloud ingest token")
        try:
            return pointcloud_collector.ingest_rosbridge_payload(uav_id, payload)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/pointcloud/{uav_id}/latest", summary="获取最近一帧点云", tags=["科目三点云"])
    def pointcloud_latest(uav_id: int) -> Dict[str, Any]:
        if uav_id not in range(1, 7):
            raise HTTPException(status_code=404, detail="unknown UAV ID")
        frame = pointcloud_collector.latest(uav_id)
        if frame is None:
            raise HTTPException(status_code=404, detail="no point cloud has been received")
        return frame

    @app.get("/api/v1/pointcloud/{uav_id}/pcd", summary="下载最近一帧 PCD 点云", tags=["科目三点云"])
    def pointcloud_pcd(uav_id: int) -> FileResponse:
        if uav_id not in range(1, 7):
            raise HTTPException(status_code=404, detail="unknown UAV ID")
        path = pointcloud_collector.latest_path(uav_id)
        if path is None:
            raise HTTPException(status_code=404, detail="no stored point cloud is available")
        return FileResponse(path, media_type="application/pcd", filename=path.name)

    @app.post("/api/v1/plan", summary="规划并分配六机任务", tags=["任务控制"])
    def plan(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        tasks = payload.get("tasks_by_uav")
        normalized_tasks = None
        if tasks is not None:
            normalized_tasks = {int(key): dict(value) for key, value in tasks.items()}
        if isinstance(adapter, DistributedFleetAdapter):
            try:
                connected_uav_ids = adapter.connected_uav_ids_snapshot()
                # Keep planning independent from physical availability.  With
                # no fresh UAV telemetry this is a planning-only mission: the
                # solver still returns all six routes, but no assignment is
                # sent and no ground-computer lease is acquired.
                orchestrator.set_active_uav_ids(connected_uav_ids)
                if connected_uav_ids:
                    adapter.acquire_coordination(connected_uav_ids)
            except Exception as error:
                raise HTTPException(
                    status_code=409,
                    detail="unable to reserve all six ground computers: {}".format(error),
                ) from error
        try:
            result = _mission_call(
                orchestrator.plan,
                str(payload["subject"]),
                normalized_tasks,
                payload.get("duration_seconds"),
                payload.get("search_area"),
                str(payload.get("flight_profile", "lab")),
                str(payload.get("controller_mode", "internal")),
                payload.get("gps_origin"),
                payload.get("landing_area"),
            )
        except Exception:
            if isinstance(adapter, DistributedFleetAdapter):
                adapter.release_coordination()
            raise
        if image_collector is not None:
            image_collector.activate()
        if isinstance(adapter, DistributedFleetAdapter):
            result["dispatch_status"] = {
                "mode": "assigned" if connected_uav_ids else "planning_only",
                "connected_uav_ids": list(connected_uav_ids),
                "assigned_uav_ids": list(connected_uav_ids),
                "ack_required_before_takeoff": True,
            }
        return result

    @app.post("/api/v1/peer/lease", include_in_schema=False)
    def peer_lease(
        payload: Dict[str, Any] = Body(...),
        x_competition_peer_token: str = Header(default=""),
    ) -> Dict[str, Any]:
        _require_peer_token(x_competition_peer_token)
        if not isinstance(adapter, DistributedFleetAdapter):
            raise HTTPException(status_code=404, detail="distributed adapter is disabled")
        try:
            coordinator_id = str(payload["coordinator_id"])
            action = str(payload.get("action", "claim"))
            if action == "claim":
                adapter.claim_peer_coordinator(coordinator_id)
            elif action == "release":
                adapter.release_peer_coordinator(coordinator_id)
            else:
                raise ValueError("lease action must be claim or release")
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"accepted": True, "local_uav_id": local_uav_id}

    @app.post("/api/v1/peer/mission", include_in_schema=False)
    def peer_mission(
        payload: Dict[str, Any] = Body(...),
        x_competition_peer_token: str = Header(default=""),
    ) -> Dict[str, Any]:
        _require_peer_token(x_competition_peer_token)
        if not isinstance(adapter, DistributedFleetAdapter):
            raise HTTPException(status_code=404, detail="distributed adapter is disabled")
        try:
            adapter.accept_peer_snapshot(
                str(payload["coordinator_id"]), dict(payload["snapshot"])
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"accepted": True, "local_uav_id": local_uav_id}

    def _require_peer_token(supplied: str) -> None:
        if peer_token and not hmac.compare_digest(supplied, peer_token):
            raise HTTPException(status_code=403, detail="ground peer authentication failed")

    @app.get("/api/v1/peer/telemetry/{uav_id}", include_in_schema=False)
    def peer_telemetry(
        uav_id: int, x_competition_peer_token: str = Header(default="")
    ) -> Dict[str, Any]:
        _require_peer_token(x_competition_peer_token)
        if not isinstance(adapter, DistributedFleetAdapter) or uav_id != local_uav_id:
            raise HTTPException(status_code=404, detail="UAV is not local to this computer")
        return {
            "uav_id": uav_id,
            "telemetry": adapter.local_telemetry_dict(),
            "traffic": traffic_monitor.status_for_uav(uav_id),
        }

    @app.post("/api/v1/peer/command", include_in_schema=False)
    def peer_command(
        payload: Dict[str, Any] = Body(...),
        x_competition_peer_token: str = Header(default=""),
    ) -> Dict[str, Any]:
        _require_peer_token(x_competition_peer_token)
        if not isinstance(adapter, DistributedFleetAdapter):
            raise HTTPException(status_code=404, detail="distributed adapter is disabled")
        try:
            adapter.accept_peer_command(
                str(payload["coordinator_id"]),
                int(payload["uav_id"]),
                str(payload["command_type"]),
                dict(payload.get("payload", {})),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"accepted": True, "local_uav_id": local_uav_id}

    @app.get("/api/v1/peer/images/manifest", include_in_schema=False)
    def peer_image_manifest(
        uav_id: int, x_competition_peer_token: str = Header(default="")
    ) -> Dict[str, Any]:
        _require_peer_token(x_competition_peer_token)
        if not isinstance(adapter, DistributedFleetAdapter) or uav_id != local_uav_id:
            raise HTTPException(status_code=404, detail="UAV is not local to this computer")
        return {"uav_id": uav_id, "files": local_image_manifest(image_root, uav_id)}

    @app.get("/api/v1/peer/images/file", include_in_schema=False)
    def peer_image_file(
        uav_id: int,
        relative_path: str,
        x_competition_peer_token: str = Header(default=""),
    ) -> FileResponse:
        _require_peer_token(x_competition_peer_token)
        if not isinstance(adapter, DistributedFleetAdapter) or uav_id != local_uav_id:
            raise HTTPException(status_code=404, detail="UAV is not local to this computer")
        try:
            path = resolve_image_file(image_root, uav_id, relative_path)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(path)

    @app.post("/api/v1/takeoff/prepare", summary="执行六机起飞预检", tags=["起飞控制"])
    def prepare_takeoff() -> Dict[str, Any]:
        return _mission_call(orchestrator.prepare_takeoff)

    @app.post("/api/v1/takeoff/confirm", summary="确认六机一键起飞", tags=["起飞控制"])
    def confirm_takeoff(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return _mission_call(orchestrator.confirm_takeoff, str(payload["token"]))

    @app.post("/api/v1/return", summary="命令六机全部返航", tags=["返航控制"])
    def return_all(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        raw_reason = str(payload.get("reason", ReturnReason.MANUAL.value))
        try:
            reason = ReturnReason(raw_reason)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid return reason") from error
        return _mission_call(orchestrator.request_return_all, reason)

    @app.post("/api/v1/uavs/{uav_id}/restart-executor", tags=["机载维护"])
    def restart_executor(uav_id: int) -> Dict[str, Any]:
        if uav_id not in config.uav_ids:
            raise HTTPException(status_code=422, detail="invalid UAV ID")
        try:
            if isinstance(adapter, DistributedFleetAdapter):
                adapter._command(uav_id, "restart_executor", {})
            elif isinstance(adapter, TcpFleetAdapter):
                adapter.forward_command(uav_id, "restart_executor", {})
            else:
                raise ValueError("restart requires TCP mode")
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"sent": True, "uav_id": uav_id}

    @app.post("/api/v1/uavs/{uav_id}/restart-all", tags=["机载维护"])
    def restart_all(uav_id: int) -> Dict[str, Any]:
        if uav_id not in config.uav_ids:
            raise HTTPException(status_code=422, detail="invalid UAV ID")
        try:
            if isinstance(adapter, DistributedFleetAdapter):
                adapter._command(uav_id, "restart_all_programs", {})
            elif isinstance(adapter, TcpFleetAdapter):
                adapter.forward_command(uav_id, "restart_all_programs", {})
            else:
                raise ValueError("restart all requires TCP mode")
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"sent": True, "uav_id": uav_id}

    @app.post("/api/v1/uavs/{uav_id}/restart-takeoff", tags=["机载维护"])
    def restart_takeoff(uav_id: int) -> Dict[str, Any]:
        """Reset the saved task to waypoint zero and request a fresh takeoff."""
        if uav_id not in config.uav_ids:
            raise HTTPException(status_code=422, detail="invalid UAV ID")
        mission = orchestrator.snapshot().get("mission") or {}
        runtime = (mission.get("uavs") or {}).get(str(uav_id))
        if not runtime:
            raise HTTPException(status_code=409, detail="UAV has no assigned mission")
        payload = {
            "mission_id": mission.get("mission_id"),
            "target_altitude_m": runtime.get("target_altitude_m"),
            "recon_start_mode": 1,
        }
        try:
            if isinstance(adapter, DistributedFleetAdapter):
                adapter._command(uav_id, "restart_takeoff", payload)
            elif isinstance(adapter, TcpFleetAdapter):
                adapter.forward_command(uav_id, "restart_takeoff", payload)
            else:
                raise ValueError("restart takeoff requires TCP mode")
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"sent": True, "uav_id": uav_id, "recon_start_mode": 1}

    @app.get("/api/v1/recording/status", summary="获取飞行数据采集状态", tags=["数据采集"])
    def recording_status() -> Dict[str, Any]:
        return recording_manager.status()

    @app.post("/api/v1/recording/start", summary="开始机载飞行数据采集", tags=["数据采集"])
    def recording_start(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        uav_id = int(payload.get("uav_id", 1))
        if isinstance(adapter, DistributedFleetAdapter) and uav_id != local_uav_id:
            raise HTTPException(
                status_code=422,
                detail="this computer may only record its paired UAV",
            )
        if uav_id not in active_uav_ids:
            raise HTTPException(status_code=422, detail="UAV is not active in this run")
        try:
            return recording_manager.start(uav_id)
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/recording/stop", summary="停止采集并生成报告", tags=["数据采集"])
    def recording_stop() -> Dict[str, Any]:
        try:
            return recording_manager.stop()
        except (OSError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/v1/recording/report", summary="打开最近一次定位报告", tags=["数据采集"])
    def recording_report() -> FileResponse:
        try:
            path = recording_manager.report_path()
        except RuntimeError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(path, media_type="text/html")

    @app.delete("/api/v1/recording/onboard-backup", summary="删除已校验的机载备份", tags=["数据采集"])
    def recording_delete_onboard_backup() -> Dict[str, Any]:
        try:
            return recording_manager.delete_onboard_backup()
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v1/sim/uavs/{uav_id}/telemetry", summary="注入单机模拟遥测", tags=["模拟测试"])
    def inject_telemetry(
        uav_id: int, payload: Dict[str, Any] = Body(...)
    ) -> Dict[str, Any]:
        if not isinstance(adapter, RecordingAdapter):
            raise HTTPException(status_code=404, detail="simulation endpoint disabled")
        now = orchestrator.clock()
        mission = orchestrator.snapshot().get("mission")
        telemetry = Telemetry(
            uav_id=uav_id,
            received_at=float(payload.get("received_at", now)),
            connected=bool(payload.get("connected", True)),
            armed=bool(payload.get("armed", True)),
            odom_valid=bool(payload.get("odom_valid", True)),
            failsafe=bool(payload.get("failsafe", False)),
            control_state=int(payload.get("control_state", 2)),
            battery_percentage=float(payload.get("battery_percentage", 0.9)),
            position=[float(value) for value in payload.get("position", [0, 0, 0])],
            velocity=[float(value) for value in payload.get("velocity", [0, 0, 0])],
            task_complete=bool(payload.get("task_complete", False)),
            task_assignment_acked=bool(payload.get("task_assignment_acked", True)),
            task_assignment_mission_id=str(
                payload.get(
                    "task_assignment_mission_id",
                    mission["mission_id"] if mission else "",
                )
            ),
            task_assignment_checksum=str(
                payload.get(
                    "task_assignment_checksum",
                    mission["uavs"][str(uav_id)]["assignment_checksum"]
                    if mission and str(uav_id) in mission["uavs"]
                    else "",
                )
            ),
        )
        adapter.inject(telemetry)
        orchestrator.tick()
        return orchestrator.snapshot()

    @app.get("/api/v1/sim/commands", summary="查看模拟指令记录", tags=["模拟测试"])
    def simulation_commands() -> Dict[str, Any]:
        if not isinstance(adapter, RecordingAdapter):
            raise HTTPException(status_code=404, detail="simulation endpoint disabled")
        return {"commands": adapter.snapshot()}

    @app.websocket("/ws/status")
    async def websocket_status(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(status())
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            return

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run(
        "competition_backend.api:app",
        host=os.environ.get("COMPETITION_HOST", "127.0.0.1"),
        port=int(os.environ.get("COMPETITION_PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    run()
