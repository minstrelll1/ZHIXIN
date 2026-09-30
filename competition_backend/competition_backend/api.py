from __future__ import annotations

import asyncio
import copy
import hmac
import math
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

from competition_shared.fleet import (
    FleetStore,
    PROFILES,
    apply_fixed_binding,
    revision,
    resolved_vehicle,
    validate_fleet,
    validate_identity,
)
from competition_shared.runtime import ground_environment
from .adapter import RecordingAdapter
from .config import load_config
from .distributed_adapter import DistributedFleetAdapter, parse_ground_peers
from .image_aggregation import (
    PeerImageCollector,
    local_image_manifest,
    resolve_image_file,
)
from .subject1_reporting import reporting_router
from .recognition_settings import RecognitionSettings
from competition_shared.recognition import validate_recognition_selection
from .journal import EventJournal
from .groundstation_capture import PassivePointCloudCapture
from .models import ReturnReason, Telemetry
from .pengfei_telemetry import sanitize_pengfei
from .orchestrator import CompetitionOrchestrator, MissionError
from .plan_jobs import PlanJobRegistry
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


def _fresh_onboard_gps_reference(item, now, max_age_seconds, uav_id, require_validity=True):
    """Choose the same fresh WGS84 fix that the live map uses."""
    if not item.get("connected"):
        return None
    try:
        delay = now - float(item["received_at"])
        position_age = float(item.get("gps_telemetry_age_seconds") or 0)
        gps_status = int(item.get("gps_status")) if require_validity else 3
        location_source = int(item.get("location_source")) if require_validity else 5
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(delay) or not math.isfinite(position_age)
            or delay < 0 or position_age < 0 or position_age + delay > max_age_seconds
            or gps_status < 3 or location_source not in (4, 5)):
        return None

    def coordinates(value):
        if not isinstance(value, dict):
            return None
        try:
            latitude = float(value["latitude"])
            longitude = float(value["longitude"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if (not all(math.isfinite(v) for v in (latitude, longitude))
                or abs(latitude) > 90 or abs(longitude) > 180):
            return None
        return latitude, longitude

    precise = item.get("gps_position")
    if isinstance(precise, dict):
        try:
            fix_age = float(precise["age_seconds"])
        except (KeyError, TypeError, ValueError, OverflowError):
            fix_age = math.inf
        fix = coordinates(precise)
        if fix and math.isfinite(fix_age) and 0 <= fix_age and fix_age + delay <= max_age_seconds:
            return {"latitude": fix[0], "longitude": fix[1],
                    "source": "mavros_global"}
    coarse = coordinates(item)
    if coarse:
        return {"latitude": coarse[0], "longitude": coarse[1],
                "source": "prometheus_gps_low_precision"}
    return None


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
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def create_app(environment=None) -> FastAPI:
    env = dict(os.environ if environment is None else environment)
    plan_jobs = PlanJobRegistry()
    fleet_path = env.get("COMPETITION_FLEET_CONFIG") or str(PACKAGE_ROOT.parent / "config" / "fleet.json")
    fleet_store = FleetStore(fleet_path)
    fleet_config = fleet_store.read()
    fleet_enabled = bool(env.get("COMPETITION_FLEET_CONFIG"))
    terminal_id = int(env.get("COMPETITION_GROUND_TERMINAL_ID", "1"))
    if fleet_enabled:
        local_vehicle, overrides = ground_environment(fleet_config, terminal_id)
        env.update(overrides)
    identity_validator = (lambda hello: validate_identity(hello, fleet_config)) if fleet_enabled else None
    config_path = env.get(
        "COMPETITION_CONFIG",
        str(PACKAGE_ROOT / "config" / "competition.example.json"),
    )
    adapter_name = env.get("COMPETITION_ADAPTER", "sim").strip().lower()
    config = load_config(config_path)
    video_sources = _parse_video_sources(
        env.get("COMPETITION_VIDEO_SOURCES", "")
    )
    active_uav_ids = [
        int(value.strip())
        for value in env.get(
            "COMPETITION_ACTIVE_UAV_IDS",
            ",".join(str(value) for value in config.uav_ids),
        ).split(",")
        if value.strip()
    ]
    local_uav_id = int(env.get("COMPETITION_LOCAL_UAV_ID", "1"))
    ground_node_id = env.get(
        "COMPETITION_GROUND_NODE_ID", "ground-uav{}".format(local_uav_id)
    )
    ground_peers = parse_ground_peers(env.get("COMPETITION_GROUND_PEERS", ""))
    pointcloud_source_mode = env.get(
        "COMPETITION_POINTCLOUD_SOURCE", POINTCLOUD_SOURCE_GROUNDSTATION_SHARED
    ).strip().lower()
    pointcloud_topics = parse_pointcloud_topics(
        env.get("COMPETITION_POINTCLOUD_TOPICS", "")
    )
    if pointcloud_source_mode in (
        POINTCLOUD_SOURCE_GROUNDSTATION_RELAY,
        POINTCLOUD_SOURCE_GROUNDSTATION_SHARED,
    ):
        relay_uav_ids = parse_pointcloud_uav_ids(
            env.get("COMPETITION_POINTCLOUD_RELAY_UAV_IDS", ""),
            config.uav_ids,
        )
        if pointcloud_source_mode == POINTCLOUD_SOURCE_GROUNDSTATION_SHARED:
            # IDs are retained for status and validation; the collector opens
            # no rosbridge socket in this mode.
            pointcloud_hosts = {uav_id: "groundstation-shared-ingress" for uav_id in relay_uav_ids}
            pointcloud_port = 0
        else:
            relay_host = env.get("COMPETITION_POINTCLOUD_RELAY_HOST", "127.0.0.1").strip()
            if not relay_host:
                raise ValueError("COMPETITION_POINTCLOUD_RELAY_HOST cannot be empty")
            pointcloud_hosts = {uav_id: relay_host for uav_id in relay_uav_ids}
            pointcloud_port = int(env.get("COMPETITION_POINTCLOUD_RELAY_PORT", "9090"))
        pointcloud_default_topic_template = env.get(
            "COMPETITION_POINTCLOUD_RELAY_TOPIC_TEMPLATE",
            DEFAULT_GROUNDSTATION_RELAY_TOPIC_TEMPLATE,
        )
    elif pointcloud_source_mode == POINTCLOUD_SOURCE_ONBOARD:
        pointcloud_hosts = parse_pointcloud_hosts(env.get("COMPETITION_POINTCLOUD_ROSBRIDGE_HOSTS", ""))
        pointcloud_port = int(env.get("COMPETITION_POINTCLOUD_ROSBRIDGE_PORT", "9090"))
        pointcloud_default_topic_template = DEFAULT_ONBOARD_TOPIC_TEMPLATE
    else:
        raise ValueError(
            "COMPETITION_POINTCLOUD_SOURCE must be 'onboard', 'groundstation_relay', or 'groundstation_shared'"
        )
    peer_token = env.get("COMPETITION_PEER_TOKEN", "")
    if fleet_enabled and not local_vehicle["pointcloud_enabled"]:
        pointcloud_hosts = {}
        pointcloud_topics = {}
    if env.get("COMPETITION_CONFIRM_LIVE_CONFIG", "").strip().lower() in (
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
            bind_host=env.get("COMPETITION_TCP_BIND", "0.0.0.0"),
            port=int(env.get("COMPETITION_TCP_PORT", "56100")),
            auth_token=env.get("COMPETITION_TCP_TOKEN", ""),
            identity_validator=identity_validator,
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
            bind_host=env.get("COMPETITION_TCP_BIND", "0.0.0.0"),
            port=int(env.get("COMPETITION_TCP_PORT", "56100")),
            uav_auth_token=env.get("COMPETITION_TCP_TOKEN", ""),
            identity_validator=identity_validator,
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

    operator_lock = threading.RLock()
    operator_selection_required = isinstance(adapter, DistributedFleetAdapter) and fleet_enabled
    default_task_publisher = (
        not isinstance(adapter, DistributedFleetAdapter)
        or not fleet_enabled
        or env.get("COMPETITION_TASK_PUBLISHER", "").strip().lower() in ("1", "true", "yes")
    )
    selected_model = local_vehicle["model"] if fleet_enabled else "p600"
    operator_state: Dict[str, Any] = {
        "configured": not operator_selection_required,
        "ground_terminal_id": terminal_id,
        "model": selected_model,
        "task_publisher": default_task_publisher,
        "selection_pending": False,
        "selection_error": "",
    }
    if isinstance(adapter, DistributedFleetAdapter):
        adapter.set_task_publisher(default_task_publisher)

    data_directory = env.get(
        "COMPETITION_DATA_DIR", str(PACKAGE_ROOT / "data")
    )
    recognition_settings = RecognitionSettings(data_directory)
    orchestrator = CompetitionOrchestrator(
        config=config,
        adapter=adapter,
        journal=EventJournal(data_directory),
        live_mode=live_mode,
        active_uav_ids=active_uav_ids,
    )

    def onboard_gps_references(required_ids=None) -> Dict[str, Dict[str, Any]]:
        """读取规划前各架已连接无人机的有效 WGS84 经纬度。"""
        snapshot = orchestrator.snapshot()
        telemetry = snapshot.get("telemetry", {})
        candidate_ids = list(required_ids or active_uav_ids)
        now = time.time()
        references: Dict[str, Dict[str, Any]] = {}
        for uav_id in candidate_ids:
            item = telemetry.get(str(uav_id)) or {}
            reference = _fresh_onboard_gps_reference(
                item, now, config.safety.telemetry_max_age_seconds, uav_id,
                require_validity=live_mode,
            )
            if reference:
                references[str(uav_id)] = reference
        return references

    def onboard_gps_reference() -> Optional[Dict[str, Any]]:
        """兼容旧接口：返回第一架有效无人机的经纬度参考。"""
        references = onboard_gps_references()
        return next(iter(references.values()), None)

    if isinstance(adapter, DistributedFleetAdapter):
        adapter.set_snapshot_provider(orchestrator.snapshot)
    recording_manager = FlightRecordingManager(
        project_root=PACKAGE_ROOT.parent,
        ssh_hosts=parse_ssh_hosts(
            env.get("COMPETITION_UAV_SSH_HOSTS", "")
        ),
        ssh_user=env.get("COMPETITION_UAV_SSH_USER", "amov"),
        vehicle_profiles=(
            {
                int(vehicle["uav_id"]): resolved_vehicle(vehicle)
                for vehicle in fleet_config["vehicles"]
            }
            if fleet_enabled
            else {}
        ),
    )
    image_root = Path(
        env.get("COMPETITION_IMAGE_ROOT", str(PACKAGE_ROOT.parent / "received_images"))
    )
    image_collector = (
        PeerImageCollector(
            image_root,
            local_uav_id=local_uav_id,
            peers=ground_peers,
            peer_token=peer_token,
            interval_sec=float(env.get("COMPETITION_IMAGE_SYNC_INTERVAL", "5")),
            should_collect=lambda: adapter.is_coordinator,
        )
        if isinstance(adapter, DistributedFleetAdapter)
        else None
    )
    pointcloud_collector = RosbridgePointCloudCollector(
        Path(
            env.get(
                "COMPETITION_POINTCLOUD_ROOT",
                str(PACKAGE_ROOT.parent / "pointcloud_records"),
            )
        ),
        hosts=pointcloud_hosts,
        topics=pointcloud_topics,
        port=pointcloud_port,
        source_mode=pointcloud_source_mode,
        default_topic_template=pointcloud_default_topic_template,
        max_points=int(env.get("COMPETITION_POINTCLOUD_MAX_POINTS", "20000")),
        save_interval_sec=float(
            env.get("COMPETITION_POINTCLOUD_SAVE_INTERVAL", "1.0")
        ),
        max_frames_per_uav=int(
            env.get("COMPETITION_POINTCLOUD_MAX_FRAMES", "300")
        ),
    )
    capture_local_ip = env.get("COMPETITION_POINTCLOUD_CAPTURE_LOCAL_IP", "").strip()
    capture_remote_ip = env.get("COMPETITION_POINTCLOUD_CAPTURE_REMOTE_IP", "192.168.1.88")
    capture_remote_port = int(env.get("COMPETITION_POINTCLOUD_CAPTURE_REMOTE_PORT", "9090"))
    capture_uav_id = int(env.get("COMPETITION_POINTCLOUD_CAPTURE_UAV_ID", str(local_uav_id)))
    if capture_local_ip:
        if pointcloud_source_mode != POINTCLOUD_SOURCE_GROUNDSTATION_SHARED:
            raise ValueError("地面抓取需要 groundstation_shared 点云模式")
        if capture_uav_id not in pointcloud_hosts:
            raise ValueError("地面抓取的网页 UAV 编号未配置在点云接收列表中")
        pointcloud_collector.capture = PassivePointCloudCapture(
            capture_local_ip,
            capture_remote_ip,
            capture_remote_port,
            capture_uav_id, pointcloud_collector.topic_for(capture_uav_id),
            pointcloud_collector.ingest_rosbridge_payload,
        )
    traffic_hosts = parse_uav_traffic_hosts(
        env.get("COMPETITION_UAV_TRAFFIC_HOSTS", "")
    )
    traffic_monitor = TrafficMonitor(
        traffic_hosts,
        interval_sec=float(env.get("COMPETITION_TRAFFIC_INTERVAL", "1.0"))
    )
    # MediaMTX pulls the gimbal RTSP stream from the camera computer. Include
    # that endpoint in the same UAV accounting even though it is not the
    # onboard computer IP used by the task/telemetry link.
    video_rtsp_source = env.get("COMPETITION_VIDEO_RTSP_SOURCE", "")
    video_host = ""
    if video_rtsp_source:
        try:
            video_host = urlsplit(video_rtsp_source).hostname or ""
        except ValueError:
            video_host = ""
    if video_host and local_uav_id in traffic_hosts:
        traffic_monitor.video_hosts[local_uav_id] = video_host
        traffic_monitor._host_to_uav[video_host] = local_uav_id

    # Keep an explicit end-to-end inventory next to byte counters.  This is
    # the contract shown to operators: every aircraft/ground data path is
    # listed, while the live TCP table below proves which paths are active.
    link_plan: Dict[int, list] = {}
    for uav_id, host in traffic_hosts.items():
        entries = [
            {
                "id": "task_telemetry",
                "category": "task_telemetry",
                "label": "任务/遥测",
                "transport": "TCP",
                "direction": "双向",
                "endpoint": "{}:56100".format(host),
            },
            {
                "id": "image_return",
                "category": "image_return",
                "label": "图片回传",
                "transport": "TCP",
                "direction": "机载 → 地面",
                "endpoint": "{}:{} → ground:56010".format(host, 56010),
            },
            {
                "id": "prometheus",
                "category": "prometheus",
                "label": "Prometheus 飞行状态",
                "transport": "TCP/UDP",
                "direction": "双向",
                "endpoint": "{}:55555/55556/8889".format(host),
            },
        ]
        if pointcloud_source_mode == POINTCLOUD_SOURCE_GROUNDSTATION_SHARED and uav_id == capture_uav_id:
            entries.append(
                {
                    "id": "pointcloud_groundstation_shared",
                    "category": "rviz_pointcloud",
                    "label": "GroundStation 点云共享抓取",
                    "transport": "TCP 被动复制",
                    "direction": "机载 → GroundStation → 地面网页",
                    "endpoint": "{}:{} → 网卡 {}".format(
                        capture_remote_ip, capture_remote_port, capture_local_ip or "GroundStation"
                    ),
                    "source_mode": pointcloud_source_mode,
                }
            )
        elif pointcloud_source_mode == POINTCLOUD_SOURCE_ONBOARD:
            entries.append(
                {
                    "id": "pointcloud_rosbridge",
                    "category": "rviz_pointcloud",
                    "label": "机载 ROSBridge 点云",
                    "transport": "WebSocket",
                    "direction": "机载 → 地面网页",
                    "endpoint": "{}:{}".format(host, pointcloud_port),
                    "source_mode": pointcloud_source_mode,
                }
            )
        elif pointcloud_source_mode == POINTCLOUD_SOURCE_GROUNDSTATION_RELAY:
            entries.append(
                {
                    "id": "pointcloud_groundstation_relay",
                    "category": "rviz_pointcloud",
                    "label": "GroundStation ROSBridge 中继点云",
                    "transport": "WebSocket",
                    "direction": "GroundStation → 地面网页",
                    "endpoint": "{}:{}".format(
                        env.get("COMPETITION_POINTCLOUD_RELAY_HOST", "127.0.0.1"),
                        int(env.get("COMPETITION_POINTCLOUD_RELAY_PORT", "9090")),
                    ),
                    "source_mode": pointcloud_source_mode,
                }
            )
        if uav_id in traffic_monitor.video_hosts:
            entries.append(
                {
                    "id": "gimbal_video_rtsp",
                    "category": "gimbal_video",
                    "label": "云台主摄 RTSP → MediaMTX",
                    "transport": "RTSP",
                    "direction": "相机 → 地面 MediaMTX",
                    "endpoint": video_rtsp_source,
                }
            )
            entries.append(
                {
                    "id": "gimbal_video_webrtc",
                    "category": "gimbal_video",
                    "label": "MediaMTX 浏览器 WebRTC",
                    "transport": "WebRTC（地面本机）",
                    "direction": "MediaMTX → 浏览器",
                    "endpoint": video_sources.get(str(uav_id), ""),
                    "ground_only": True,
                }
            )
        link_plan[uav_id] = entries
    traffic_monitor.set_link_plan(link_plan)

    video_url_owners: Dict[str, list] = {}
    for uav_id, source in video_sources.items():
        video_url_owners.setdefault(source.rstrip("/"), []).append(int(uav_id))

    def communication_status() -> Dict[str, Any]:
        traffic = traffic_monitor.status()
        runtime_duplicates = []
        for uav_id, item in traffic.get("by_uav", {}).items():
            for duplicate in item.get("duplicate_links", []):
                runtime_duplicates.append({"uav_id": int(uav_id), **duplicate})
        configured_duplicates = [
            {
                "kind": "video_source",
                "uav_ids": sorted(uav_ids),
                "endpoint": source,
                "reason": "多个 UAV 使用同一个浏览器视频源",
            }
            for source, uav_ids in video_url_owners.items()
            if len(uav_ids) > 1
        ]
        direct_pointcloud_hosts = env.get(
            "COMPETITION_POINTCLOUD_ROSBRIDGE_HOSTS", ""
        ).strip()
        if (
            pointcloud_source_mode == POINTCLOUD_SOURCE_GROUNDSTATION_SHARED
            and capture_local_ip
            and direct_pointcloud_hosts
        ):
            configured_duplicates.append(
                {
                    "kind": "pointcloud_source",
                    "endpoint": direct_pointcloud_hosts,
                    "reason": "已启用 GroundStation 共享抓取，同时配置了机载 ROSBridge 直连",
                }
            )
        pointcloud_runtime = pointcloud_collector.status()
        peer_sync = adapter.peer_sync_status() if isinstance(adapter, DistributedFleetAdapter) else {}
        peer_traffic = adapter.peer_traffic_status() if isinstance(adapter, DistributedFleetAdapter) else {}
        return {
            "generated_at": time.time(),
            "accounting": {
                "tcp": "Windows TCP extended statistics",
                "application_relays": "点云/本地中继通过应用字节计数补充",
                "udp": "Prometheus UDP 需要通过 /api/v1/traffic/report 上报",
            },
            "links_by_uav": traffic.get("by_uav", {}),
            "configured_links": link_plan,
            "pointcloud": {
                "source_mode": pointcloud_source_mode,
                "capture": pointcloud_runtime.get("capture", {}),
                "topics": pointcloud_runtime.get("topics", {}),
            },
            "video": {
                "rtsp_source": video_rtsp_source,
                "browser_sources": video_sources,
                "webrtc_is_ground_local": True,
            },
            "ground_peers": {
                str(uav_id): url for uav_id, url in sorted(ground_peers.items())
            },
            "ground_peer_sync": peer_sync,
            "ground_peer_traffic": peer_traffic,
            "image_aggregation": image_collector.status()
            if image_collector is not None
            else {},
            "duplicates": configured_duplicates + runtime_duplicates,
            "duplicate_check": {
                "status": "warning" if configured_duplicates or runtime_duplicates else "ok",
                "checked": ["rviz_pointcloud", "gimbal_video"],
                "message": "发现重复链路，请关闭备用点云转发器或重复 MediaMTX。"
                if configured_duplicates or runtime_duplicates
                else "未发现点云或视频重复链路。",
            },
        }
    pointcloud_collector.traffic_recorder = traffic_monitor.record_application_bytes

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            orchestrator.start()
            if image_collector is not None:
                image_collector.start()
            pointcloud_collector.start()
            traffic_monitor.start()
            yield
        finally:
            traffic_monitor.stop()
            pointcloud_collector.stop()
            if image_collector is not None:
                image_collector.stop()
            recording_manager.close()
            orchestrator.stop()

    app = FastAPI(
        title="智信 P600 / SU17 六机竞赛任务后端",
        description="六架无人机的任务规划、起飞预检、任务执行和自动返航接口。",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.orchestrator = orchestrator
    app.state.adapter = adapter
    app.state.fleet_store = fleet_store
    app.state.recording_manager = recording_manager
    app.state.image_collector = image_collector
    app.state.pointcloud_collector = pointcloud_collector
    app.state.traffic_monitor = traffic_monitor

    def operator_status() -> Dict[str, Any]:
        with operator_lock:
            result = dict(operator_state)
        current = fleet_store.read()
        matches = [
            item for item in current["vehicles"]
            if item["ground_terminal_id"] == result["ground_terminal_id"] and item["enabled"]
        ]
        result["local_uav_id"] = local_uav_id
        result["ground_node_id"] = ground_node_id
        result["fleet_revision"] = revision(current)
        result["restart_required"] = fleet_enabled and revision(current) != revision(fleet_config)
        result["profile_matches"] = bool(
            len(matches) == 1 and matches[0]["model"] == result["model"]
        )
        result["task_scope"] = "fleet" if result["task_publisher"] else "local"
        result["can_edit_fleet"] = bool(result["configured"] and result["task_publisher"])
        result["selection_required"] = operator_selection_required
        if matches:
            preview = apply_fixed_binding(current, result["ground_terminal_id"], result["model"])
            result["binding_preview"] = next(v for v in preview["vehicles"] if v["ground_terminal_id"] == result["ground_terminal_id"])
        return result

    def _require_idle_for_configuration() -> None:
        snapshot = orchestrator.snapshot()
        mission = snapshot.get("mission") or {}
        mirrored = adapter.mirrored_snapshot() if isinstance(adapter, DistributedFleetAdapter) else None
        remote_mission = (mirrored or {}).get("mission") or {}
        if any(m.get("phase") in ("taking_off", "running", "returning") for m in (mission, remote_mission)) or any(
            telemetry.get("armed") for telemetry in snapshot.get("telemetry", {}).values()
        ):
            raise HTTPException(status_code=409, detail="任务执行中不能修改机队配置或操作角色")

    def _require_operator_ready() -> Dict[str, Any]:
        current = operator_status()
        if current.get("selection_pending"):
            raise HTTPException(status_code=409, detail="正在后台核验地面角色和同步配置，请稍候")
        if current.get("selection_error"):
            raise HTTPException(status_code=409, detail=current["selection_error"])
        if current["selection_required"] and not current["configured"]:
            raise HTTPException(status_code=409, detail="请先选择地面终端编号并完成发布角色选择")
        if not current["profile_matches"]:
            raise HTTPException(status_code=409, detail="所选机型与发布端同步的机队配置不一致")
        return current

    def _require_task_publisher() -> Dict[str, Any]:
        current = operator_status()
        if current.get("selection_pending") or not current["configured"] or not current["task_publisher"]:
            raise HTTPException(status_code=403, detail="只有任务发布端可以修改并同步机队配置")
        return current

    def _require_results_publisher():
        role = _require_operator_ready()
        if not role["task_publisher"]:
            raise HTTPException(status_code=403, detail="请由任务发布端统一上报科目一结果")
        return role

    app.include_router(reporting_router(image_root, _require_results_publisher))

    def _verify_peer_publisher(node_id: str) -> Dict[str, Any]:
        if not operator_selection_required:
            return {}
        candidates = [v for v in fleet_config["vehicles"] if v["enabled"] and
                      "ground-{}".format(v["ground_terminal_id"]) == node_id and v["uav_id"] != local_uav_id]
        if len(candidates) != 1 or operator_status()["task_publisher"]:
            raise HTTPException(status_code=403, detail="发送端不是本机认可的任务发布端")
        uid = candidates[0]["uav_id"]
        try:
            remote = adapter._request_json(adapter.peers[uid] + "/api/v1/peer/operator")
        except Exception as error:
            raise HTTPException(status_code=409, detail="无法核验任务发布端身份，请检查地面互联") from error
        if (remote.get("ground_node_id") != node_id or remote.get("local_uav_id") != uid
                or remote.get("configured") is not True or remote.get("task_publisher") is not True):
            raise HTTPException(status_code=403, detail="只有已选择发布角色的地面终端可以发送跨机任务或配置")
        return remote

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def frontend() -> HTMLResponse:
        return HTMLResponse(FRONTEND_PATH.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @app.get("/map-assets/{asset_name}", include_in_schema=False)
    def map_asset(asset_name: str):
        # 仅提供随代码部署的底图和渲染脚本，不开放任意本机文件路径。
        media = {"terrain_basemap.js": "application/javascript",
                 "competition_esri_20170724.jpg": "image/jpeg"}
        if asset_name not in media:
            raise HTTPException(status_code=404, detail="地图资源不存在")
        path = FRONTEND_PATH.parent / asset_name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="地图资源尚未部署，请更新地面端代码")
        return FileResponse(path, media_type=media[asset_name],
                            headers={"Cache-Control": "no-cache" if asset_name.endswith(".js") else "public, max-age=3600"})

    @app.get("/fleet", response_class=HTMLResponse, include_in_schema=False)
    def fleet_page():
        return HTMLResponse((FRONTEND_PATH.parent / "fleet.html").read_text(encoding="utf-8"))

    @app.get("/api/v1/operator", tags=["地面终端"])
    def get_operator():
        result = operator_status()
        result["profiles"] = PROFILES
        return result

    def configure_operator(payload: Dict[str, Any]):
        try:
            selected_terminal = payload["ground_terminal_id"]
            selected_publisher = payload["task_publisher"]
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="地面终端选择参数不完整") from error
        if type(selected_terminal) is not int or selected_terminal != terminal_id:
            raise HTTPException(
                status_code=409,
                detail="当前已绑定地面终端 {}，更换编号请重启地面程序后重新选择".format(terminal_id),
            )
        selected_type = str(payload.get("model") or selected_model).strip().lower()
        if selected_type != selected_model:
            raise HTTPException(status_code=409, detail="本终端机型已由机队配置固定为 %s，请在任务发布端的机队配置页修改" % selected_model.upper())
        if not isinstance(selected_publisher, bool):
            raise HTTPException(status_code=422, detail="任务发布角色无效")
        before = operator_status()
        if before["configured"] and (before["ground_terminal_id"], before["model"], before["task_publisher"]) == (selected_terminal, selected_type, selected_publisher):
            # 飞行期间刷新页面只确认原角色，不切换身份、不重写配置。
            return before
        _require_idle_for_configuration()
        mission = orchestrator.snapshot().get("mission") or {}
        if mission.get("uavs") and mission.get("phase") in ("planned", "preflight_ready"):
            raise HTTPException(status_code=409, detail="已有分派任务，不能切换操作角色；请在任务结束后重新启动配置")
        peer_roles: Dict[int, Dict[str, Any]] = {}
        if isinstance(adapter, DistributedFleetAdapter) and selected_publisher:
            peer_roles = adapter.peer_operator_status()
            conflicts = [
                uav_id for uav_id, item in peer_roles.items()
                if item.get("task_publisher") and item.get("configured")
            ]
            if conflicts:
                raise HTTPException(
                    status_code=409,
                    detail="地面终端 {} 已被选为任务发布端".format(conflicts[0]),
                )
        with operator_lock:
            operator_state.update(
                configured=True,
                ground_terminal_id=selected_terminal,
                model=selected_type,
                task_publisher=selected_publisher,
            )
        if isinstance(adapter, DistributedFleetAdapter):
            adapter.set_task_publisher(selected_publisher)
        synchronization = None
        if selected_publisher:
            current = fleet_store.read()
            candidate = apply_fixed_binding(current, selected_terminal, selected_type)
            for uid, choice in peer_roles.items():
                entry = next((v for v in current["vehicles"] if v["uav_id"] == uid), None)
                if entry and choice.get("configured") and choice.get("ground_terminal_id") == entry["ground_terminal_id"] and choice.get("model") in PROFILES:
                    candidate = apply_fixed_binding(candidate, entry["ground_terminal_id"], choice["model"])
            if revision(candidate) != revision(current):
                try:
                    fleet_store.save(candidate, revision(current))
                except ValueError as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
            if isinstance(adapter, DistributedFleetAdapter):
                synchronization = adapter.synchronize_fleet_config(fleet_store.read())
        result = operator_status()
        result["profiles"] = PROFILES
        result["peer_roles"] = {str(key): value for key, value in peer_roles.items()}
        result["synchronization"] = synchronization
        result["restart_required"] = revision(fleet_store.read()) != revision(fleet_config)
        return result

    @app.post("/api/v1/operator", tags=["地面终端"])
    def select_operator(payload: Dict[str, Any] = Body(...)):
        if env.get("COMPETITION_ASYNC_OPERATOR_SELECTION") != "1":
            return configure_operator(payload)
        if (type(payload.get("ground_terminal_id")) is not int or payload["ground_terminal_id"] != terminal_id
                or type(payload.get("task_publisher")) is not bool):
            raise HTTPException(status_code=422, detail="终端编号或发布角色无效")
        payload = dict(payload)
        payload["model"] = selected_model
        with operator_lock:
            if operator_state["selection_pending"]:
                return operator_status()
            if operator_state["configured"] and all(operator_state[k] == payload[k] for k in ("ground_terminal_id", "model", "task_publisher")):
                return operator_status()
        _require_idle_for_configuration()
        with operator_lock:
            if operator_state["selection_pending"]:
                return operator_status()
            operator_state.update(selection_pending=True, selection_error="")

        def finish_selection():
            try:
                configure_operator(payload)
            except Exception as error:
                with operator_lock:
                    operator_state.update(configured=False, task_publisher=False,
                                          selection_error=str(getattr(error, "detail", error)))
                if isinstance(adapter, DistributedFleetAdapter):
                    adapter.set_task_publisher(False)
            finally:
                with operator_lock:
                    operator_state["selection_pending"] = False
        threading.Thread(target=finish_selection, name="ground-operator-selection", daemon=True).start()
        return operator_status()

    def fleet_status():
        current = fleet_store.read()
        tcp = adapter.local_adapter if isinstance(adapter, DistributedFleetAdapter) else adapter
        return {"config": current, "revision": revision(current), "profiles": PROFILES,
                "resolved": [resolved_vehicle(v) for v in current["vehicles"]],
                "active_revision": revision(fleet_config), "strict_identity": fleet_enabled,
                "restart_required": revision(current) != revision(fleet_config),
                "ground_terminal_id": terminal_id,
                "operator": operator_status(),
                "synchronization": adapter.fleet_sync_status() if isinstance(adapter, DistributedFleetAdapter) else None,
                "identity_errors": dict(getattr(tcp, "identity_errors", {}))}

    @app.get("/api/v1/fleet/config", tags=["机队配置"])
    def get_fleet_config():
        return fleet_status()

    @app.get("/api/v1/fleet/selection-preview", tags=["机队配置"])
    def fleet_selection_preview():
        role = _require_task_publisher()
        current = fleet_store.read()
        candidate = apply_fixed_binding(current, terminal_id, role["model"])
        peers = adapter.peer_operator_status() if isinstance(adapter, DistributedFleetAdapter) else {}
        for uid, choice in peers.items():
            entry = next((v for v in current["vehicles"] if v["uav_id"] == uid), None)
            if entry and choice.get("configured") and choice.get("ground_terminal_id") == entry["ground_terminal_id"] and choice.get("model") in PROFILES:
                candidate = apply_fixed_binding(candidate, entry["ground_terminal_id"], choice["model"])
        return {"config": candidate, "revision": revision(current), "selections": peers}

    @app.put("/api/v1/fleet/config", tags=["机队配置"])
    def save_fleet_config(payload: Dict[str, Any] = Body(...), x_competition_peer_token: str = Header(default="")):
        _require_task_publisher()
        _require_peer_token(x_competition_peer_token)
        _require_idle_for_configuration()
        try:
            current = fleet_store.read()
            previous_by_terminal = {
                int(item["ground_terminal_id"]): item for item in current["vehicles"]
            }
            raw_candidate = copy.deepcopy(payload.get("config"))
            if not isinstance(raw_candidate, dict) or not isinstance(raw_candidate.get("vehicles"), list):
                raise ValueError("配置必须包含 vehicles 列表")
            # 先清除切换机型时遗留的旧物理地址，再执行完整配置校验。
            for item in raw_candidate["vehicles"]:
                previous = previous_by_terminal.get(int(item.get("ground_terminal_id", 0)))
                if previous and previous.get("model") != item.get("model"):
                    item.update(device_id="", onboard_host="", ground_host="", video_rtsp_source="")
            candidate = validate_fleet(raw_candidate)
            # 切换机型时自动清理旧地址：SU17 机载地址固定为 192.168.1.88，
            # P600 恢复到其固定六机地址；两种机型的图传网卡均默认为 192.168.1.230。
            for item in list(candidate["vehicles"]):
                previous = previous_by_terminal.get(int(item["ground_terminal_id"]))
                if previous and previous.get("model") != item.get("model"):
                    candidate = apply_fixed_binding(
                        candidate, int(item["ground_terminal_id"]), str(item["model"])
                    )
            saved = fleet_store.save(candidate, payload.get("revision"))
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        result = fleet_status()
        if isinstance(adapter, DistributedFleetAdapter):
            result["synchronization"] = adapter.synchronize_fleet_config(saved)
        return result

    def _fleet_motion_guard():
        if fleet_enabled and revision(fleet_store.read()) != revision(fleet_config):
            raise HTTPException(status_code=409, detail="机队配置已更新，请先重启程序再分派任务或起飞")

    @app.get("/health", summary="检查后端健康状态", tags=["系统"])
    def health() -> Dict[str, Any]:
        result = {
            "ok": True,
            "fleet": fleet_status(),
            "adapter": adapter_name,
            "live_mode": live_mode,
            "active_uav_ids": active_uav_ids,
            "pointcloud_source_mode": pointcloud_source_mode,
            "traffic_monitor": traffic_monitor.status(),
            "communication": communication_status(),
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
        result["fleet"] = fleet_status()
        result["operator"] = operator_status()
        result["pointcloud"] = pointcloud_collector.status()
        result["traffic"] = traffic_monitor.status()
        result["communication"] = communication_status()
        if isinstance(adapter, DistributedFleetAdapter):
            result["traffic"]["by_uav"].update(adapter.peer_traffic_status())
            mirrored = adapter.mirrored_snapshot()
            local_mission = result.get("mission") or {}
            if mirrored is not None and not adapter.is_coordinator and (not local_mission or local_mission.get("phase") in ("completed", "failed", "idle")):
                result["mission"] = mirrored.get("mission")
                result["active_uav_ids"] = mirrored.get("active_uav_ids", result["active_uav_ids"])
                result["mission_read_only"] = True
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

    @app.get("/api/v1/communication/status", summary="获取机地全链路实时通信和重复链路检查", tags=["通信流量"])
    def communication_status_route() -> Dict[str, Any]:
        return communication_status()

    @app.post("/api/v1/traffic/report", summary="上报本地中继或 UDP 字节数", tags=["通信流量"])
    def traffic_report(
        payload: Dict[str, Any] = Body(...),
        traffic_token: str = Header(default="", alias="X-Traffic-Token"),
    ) -> Dict[str, Any]:
        expected = env.get("COMPETITION_TRAFFIC_REPORT_TOKEN", "")
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
            raise HTTPException(status_code=404, detail="未知的无人机编号")
        if pointcloud_source_mode != POINTCLOUD_SOURCE_GROUNDSTATION_SHARED:
            raise HTTPException(
                status_code=409,
                detail="点云转入需要启用 groundstation_shared 模式",
            )
        expected = env.get("COMPETITION_POINTCLOUD_INGEST_TOKEN", "")
        if expected and not hmac.compare_digest(pointcloud_token, expected):
            raise HTTPException(status_code=401, detail="点云接收令牌不正确")
        try:
            return pointcloud_collector.ingest_rosbridge_payload(uav_id, payload)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/v1/pointcloud/{uav_id}/latest", summary="获取最近一帧点云", tags=["科目三点云"])
    def pointcloud_latest(uav_id: int) -> Dict[str, Any]:
        if uav_id not in range(1, 7):
            raise HTTPException(status_code=404, detail="未知的无人机编号")
        frame = pointcloud_collector.latest(uav_id)
        if frame is None:
            raise HTTPException(status_code=404, detail="尚未收到有效点云")
        return frame

    @app.get("/api/v1/pointcloud/{uav_id}/pcd", summary="下载最近一帧 PCD 点云", tags=["科目三点云"])
    def pointcloud_pcd(uav_id: int) -> FileResponse:
        if uav_id not in range(1, 7):
            raise HTTPException(status_code=404, detail="未知的无人机编号")
        path = pointcloud_collector.latest_path(uav_id)
        if path is None:
            raise HTTPException(status_code=404, detail="尚未保存有效点云")
        return FileResponse(path, media_type="application/pcd", filename=path.name)

    @app.post("/api/v1/planning/competition-coverage", summary="读取赛前固定比赛覆盖方案", tags=["任务规划"])
    def competition_coverage(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        # 独立预览不会改写当前任务，也不获取主控租约或下发指令。
        subject = str(payload.get("subject", "subject1"))
        if subject not in ("subject1", "subject2"):
            raise HTTPException(status_code=422, detail="此区域仅用于科目一和科目二")
        try:
            from .polygon_coverage import (
                adapt_competition_plan,
                plan_competition_coverage,
                PlanNotPreparedError,
            )
            from .stadium_departure import (
                anchor_stadium_plan,
                load_prepared_stadium_plan,
            )
            departure_point = str(payload.get("departure_point", "southeast")).strip().lower()
            if departure_point not in ("southeast", "stadium_center"):
                raise HTTPException(status_code=422, detail="未知的出发点")
            coordinate_mode = str(payload.get("coordinate_mode", "gps")).strip().lower()
            flight_profile = str(payload.get("flight_profile", "competition")).strip().lower()
            flight_altitude_plan = str(payload.get("flight_altitude_plan", "default")).strip().lower()
            if flight_altitude_plan not in ("default", "around1m", "around2m", "around5m", "around45m"):
                raise HTTPException(status_code=422, detail="未知的飞行高度方案")
            if flight_profile not in ("lab", "lab10", "outdoor5", "competition"):
                raise HTTPException(status_code=422, detail="未知的飞行场景")
            gps_origin = payload.get("gps_origin")
            gps_origins_by_uav = payload.get("gps_origins_by_uav")
            if coordinate_mode == "gps":
                if live_mode:
                    # GPS 任务必须使用任务发布端所控无人机作为唯一原点，
                    # 这样发布端和普通终端在同一任务中得到完全一致的坐标。
                    publisher_uav_id = (local_uav_id if not isinstance(adapter, DistributedFleetAdapter)
                                        else (adapter.publisher_uav_id or local_uav_id))
                    if isinstance(adapter, DistributedFleetAdapter) and not adapter.task_publisher:
                        roles = adapter.peer_operator_status()
                        publisher_ids = [uid for uid, item in roles.items()
                                         if item.get("configured") and item.get("task_publisher")]
                        if publisher_ids:
                            publisher_uav_id = publisher_ids[0]
                            adapter.remember_publisher(publisher_uav_id)
                    onboard_references = onboard_gps_references([publisher_uav_id])
                    if str(publisher_uav_id) not in onboard_references:
                        connected_ids = (
                            adapter.connected_uav_ids_snapshot()
                            if isinstance(adapter, DistributedFleetAdapter)
                            else list(getattr(adapter, "connected_uav_ids", []))
                        )
                        # 没有任何无人机连接时允许使用页面填写的基准点进行预览/规划；
                        # 一旦存在在线无人机，真机 GPS 任务仍必须使用发布端实测位置。
                        if not connected_ids and isinstance(gps_origin, dict):
                            try:
                                latitude = float(gps_origin["latitude"])
                                longitude = float(gps_origin["longitude"])
                                if (not math.isfinite(latitude) or not math.isfinite(longitude)
                                        or abs(latitude) > 90.0 or abs(longitude) > 180.0):
                                    raise ValueError
                                gps_origin = {"latitude": latitude, "longitude": longitude, "source": "页面预览基准点"}
                                gps_origins_by_uav = {str(uav_id): dict(gps_origin) for uav_id in config.uav_ids}
                            except (KeyError, TypeError, ValueError):
                                raise HTTPException(status_code=422, detail="页面 GPS 基准点的纬度或经度无效")
                        else:
                            raise HTTPException(
                                status_code=409,
                                detail="GPS 规划前尚未收到任务发布端 UAV{} 的有效纬度和经度遥测".format(publisher_uav_id),
                            )
                    else:
                        gps_origin = onboard_references[str(publisher_uav_id)]
                        gps_origins_by_uav = {str(uav_id): dict(gps_origin) for uav_id in config.uav_ids}
                elif flight_profile in ("lab", "lab10", "outdoor5"):
                    gps_origin = onboard_gps_reference() or gps_origin
            scaled_lab = flight_profile in ("lab", "lab10", "outdoor5")
            default_radius = 1.0 if scaled_lab else 75.0
            default_speed = 0.2 if flight_profile in ("lab", "outdoor5") else (0.5 if flight_profile == "lab10" else 5.0)
            # 实验室方案复用赛前固定的真实区域航线，再做等比例缩放；
            # 不以实验室参数触发新的比赛区域求解。
            source_radius = 75.0 if scaled_lab else payload.get("reconnaissance_radius_m", default_radius)
            source_speed = 5.0 if scaled_lab else payload.get("speed_mps", default_speed)
            max_extent_m = 3.0 if flight_profile == "lab" else (10.0 if flight_profile == "lab10" else (5.0 if flight_profile == "outdoor5" else 3.0))
            base_plan = plan_competition_coverage(
                source_radius,
                source_speed,
                payload.get("hover_scan_seconds", 10.0),
                max_region_aspect_ratio=payload.get("max_region_aspect_ratio", 2.0),
                forest_edge_m=payload.get("forest_edge_m", 5.0),
                terrain_exclusions_enabled=payload.get("terrain_exclusions_enabled", True),
                uav_count=payload.get("uav_count", 6),
            )
            source_plan = (
                load_prepared_stadium_plan(
                    base_plan,
                    flight_profile=flight_profile,
                    reconnaissance_radius_m=payload.get("reconnaissance_radius_m", default_radius),
                    speed_mps=payload.get("speed_mps", default_speed),
                )
                if departure_point == "stadium_center" else base_plan
            )
            result = adapt_competition_plan(
                source_plan,
                coordinate_mode=coordinate_mode,
                flight_profile=flight_profile,
                max_extent_m=max_extent_m,
                lab_radius_m=payload.get("reconnaissance_radius_m", default_radius),
                lab_speed_mps=payload.get("speed_mps", default_speed),
                lab_hover_seconds=payload.get("hover_scan_seconds", 10.0),
                gps_origin=gps_origin,
                gps_origins_by_uav=gps_origins_by_uav,
            )
            if departure_point == "stadium_center":
                result = anchor_stadium_plan(result, source_plan)
        except PlanNotPreparedError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except ImportError as error:
            raise HTTPException(status_code=503, detail="缺少覆盖规划依赖，请在后端虚拟环境运行 pip install -r requirements.txt") from error
        result["subject"] = subject
        result["flight_altitude_plan"] = flight_altitude_plan
        return result

    @app.get("/api/v1/recognition-categories", tags=["科目一识别类别"])
    def get_recognition_categories():
        try:
            return recognition_settings.snapshot()
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.put("/api/v1/recognition-categories", tags=["科目一识别类别"])
    def save_recognition_categories(payload: Dict[str, Any] = Body(...)):
        _require_operator_ready()
        try:
            return {"selection": recognition_settings.save(payload)}
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/plan", summary="规划并分配六机任务", tags=["任务控制"])
    def plan(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        role = _require_operator_ready()
        _fleet_motion_guard()
        recognition_selection = None
        if payload.get("subject") == "subject1":
            try:
                recognition_selection = payload.get("recognition_selection")
                if recognition_selection is None:
                    recognition_selection = recognition_settings.read()
                if recognition_selection is not None:
                    recognition_selection = validate_recognition_selection(recognition_selection)
                elif payload.get("require_recognition_selection"):
                    raise ValueError("请先选择并保存科目一识别类别")
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        prepared = None
        requested_profile = str(
            payload.get("flight_profile", "competition" if payload.get("planning_mode") == "competition" else "lab")
        ).strip().lower()
        requested_altitude_plan = str(payload.get("flight_altitude_plan", "default")).strip().lower()
        if payload.get("planning_mode") == "competition":
            if payload.get("tasks_by_uav") is not None:
                raise HTTPException(status_code=422, detail="比赛区域任务由已保存方案生成，不能混用手工任务")
            prepared = competition_coverage({
                "subject": payload.get("subject", "subject1"),
                "coordinate_mode": payload.get("coordinate_mode", "gps"),
                "flight_profile": requested_profile,
                "flight_altitude_plan": requested_altitude_plan,
                "reconnaissance_radius_m": payload.get("reconnaissance_radius_m", 1.0 if requested_profile in ("lab", "lab10", "outdoor5") else 75.0),
                "speed_mps": payload.get("speed_mps", 0.2 if requested_profile in ("lab", "outdoor5") else (0.5 if requested_profile == "lab10" else 5.0)),
                "hover_scan_seconds": payload.get("hover_scan_seconds", 10.0),
                "max_region_aspect_ratio": payload.get("max_region_aspect_ratio", 2.0),
                "forest_edge_m": payload.get("forest_edge_m", 5.0),
                "terrain_exclusions_enabled": payload.get("terrain_exclusions_enabled", True),
                "uav_count": payload.get("uav_count", 6),
                "departure_point": payload.get("departure_point", "southeast"),
                "gps_origin": payload.get("gps_origin"),
                "gps_origins_by_uav": payload.get("gps_origins_by_uav"),
            })
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
                selected_uav_ids = (
                    connected_uav_ids
                    if role["task_publisher"]
                    else ([local_uav_id] if local_uav_id in connected_uav_ids else [])
                )
                if not role["task_publisher"]:
                    adapter.assert_local_control_available()
                    mirrored_mission = (adapter.mirrored_snapshot() or {}).get("mission") or {}
                    if mirrored_mission.get("phase") in ("taking_off", "running", "returning"):
                        raise RuntimeError("发布端任务仍在执行，不能用本地规划覆盖")
                orchestrator.set_active_uav_ids(selected_uav_ids)
                if role["task_publisher"] and selected_uav_ids:
                    adapter.acquire_coordination(selected_uav_ids)
            except Exception as error:
                raise HTTPException(
                    status_code=409,
                    detail="无法取得任务控制权限：{}".format(error),
                ) from error
        try:
            result = _mission_call(
                orchestrator.plan,
                str(payload["subject"]),
                normalized_tasks,
                payload.get("duration_seconds"),
                payload.get("search_area"),
                requested_profile,
                requested_altitude_plan,
                str(payload.get("controller_mode", "internal")),
                payload.get("gps_origin"),
                payload.get("landing_area"),
                prepared,
                recognition_selection,
            )
        except Exception:
            if isinstance(adapter, DistributedFleetAdapter):
                adapter.release_coordination()
            raise
        if image_collector is not None:
            image_collector.activate()
        if isinstance(adapter, DistributedFleetAdapter):
            assigned_uav_ids = list(orchestrator.active_uav_ids)
            result["dispatch_status"] = {
                "mode": (
                    "planning_only" if not assigned_uav_ids
                    else "assigned" if role["task_publisher"]
                    else "local_only"
                ),
                "connected_uav_ids": list(connected_uav_ids),
                "assigned_uav_ids": assigned_uav_ids,
                "ack_required_before_takeoff": True,
            }
            result["operator"] = operator_status()
        return result

    @app.post("/api/v1/plan/jobs", summary="启动规划分派作业", tags=["任务控制"])
    def submit_plan_job(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        # 先返回作业编号；断开的浏览器请求不会中止或重复启动后端分派。
        # 有作业正在执行时，重复点击只返回同一个编号，不再次下发任务。
        try:
            return plan_jobs.submit(payload, plan)
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/v1/plan/jobs/current", summary="查看最近规划作业", tags=["任务状态"])
    def current_plan_job() -> Dict[str, Any]:
        job = plan_jobs.get()
        if job is None:
            raise HTTPException(status_code=404, detail="当前没有规划分派作业")
        return job

    @app.get("/api/v1/plan/jobs/{job_id}", summary="查看规划分派作业", tags=["任务状态"])
    def get_plan_job(job_id: str) -> Dict[str, Any]:
        job = plan_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="规划作业不存在或地面后端已重启，请核对任务状态")
        return job

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
                _verify_peer_publisher(coordinator_id)
                _require_operator_ready()
                _fleet_motion_guard()
                local_mission = orchestrator.snapshot().get("mission") or {}
                if local_mission.get("uavs") and local_mission.get("phase") not in (None, "completed", "failed", "idle"):
                    raise RuntimeError("本机已有独立分派任务，无法接受跨终端控制")
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
            if operator_selection_required:
                adapter.require_peer_coordinator(str(payload["coordinator_id"]))
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

    @app.get("/api/v1/peer/operator", include_in_schema=False)
    def peer_operator(x_competition_peer_token: str = Header(default="")):
        _require_peer_token(x_competition_peer_token)
        return operator_status()

    @app.post("/api/v1/peer/fleet-config", include_in_schema=False)
    def peer_fleet_config(
        payload: Dict[str, Any] = Body(...),
        x_competition_peer_token: str = Header(default=""),
    ):
        _require_peer_token(x_competition_peer_token)
        if not isinstance(adapter, DistributedFleetAdapter):
            raise HTTPException(status_code=404, detail="distributed adapter is disabled")
        _require_idle_for_configuration()
        try:
            publisher_id = str(payload["publisher_id"]).strip()
            incoming = validate_fleet(payload["config"])
            if not publisher_id:
                raise ValueError("缺少发布端标识")
            local_role = operator_status()
            if local_role["task_publisher"] and publisher_id != ground_node_id:
                raise RuntimeError("本机已被选为任务发布端，拒绝其他发布端覆盖")
            source = _verify_peer_publisher(publisher_id)
            if operator_selection_required and source.get("fleet_revision") != revision(incoming):
                raise RuntimeError("收到的配置不是发布端当前版本，请重新同步")
            current = fleet_store.read()
            fleet_store.save(incoming, revision(current))
            try:
                adapter.accept_fleet_config(int(source.get("local_uav_id")))
            except (TypeError, ValueError):
                pass
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        result = fleet_status()
        return {
            "accepted": True,
            "revision": result["revision"],
            "restart_required": result["restart_required"],
            "ground_terminal_id": terminal_id,
        }

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
            if operator_selection_required:
                _require_operator_ready()
                _verify_peer_publisher(str(payload["coordinator_id"]))
                adapter.require_peer_coordinator(str(payload["coordinator_id"]))
                if payload.get("command_type") != "return_home":
                    _fleet_motion_guard()
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
        _require_operator_ready()
        _fleet_motion_guard()
        return _mission_call(orchestrator.prepare_takeoff)

    @app.post("/api/v1/takeoff/confirm", summary="确认六机一键起飞", tags=["起飞控制"])
    def confirm_takeoff(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        _require_operator_ready()
        _fleet_motion_guard()
        return _mission_call(orchestrator.confirm_takeoff, str(payload["token"]))

    @app.post("/api/v1/return", summary="命令六机全部返航", tags=["返航控制"])
    def return_all(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        role = _require_operator_ready()
        raw_reason = str(payload.get("reason", ReturnReason.MANUAL.value))
        try:
            reason = ReturnReason(raw_reason)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid return reason") from error
        if isinstance(adapter, DistributedFleetAdapter) and not role["task_publisher"]:
            # 从发布端镜像看到的任务不属于本地编排器；本地操作只对配对机发返航。
            mission = orchestrator.snapshot().get("mission") or {}
            if not mission or mission.get("phase") in ("completed", "failed", "idle"):
                mission = (adapter.mirrored_snapshot() or {}).get("mission") or {}
            if str(local_uav_id) not in mission.get("uavs", {}):
                raise HTTPException(status_code=409, detail="本机配对无人机没有已分派任务")
            _mission_call(adapter.command_return, local_uav_id, {
                "mission_id": mission["mission_id"], "reason": reason.value, "land_after_return": True,
            })
            return status()
        return _mission_call(orchestrator.request_return_all, reason)

    @app.post("/api/v1/uavs/{uav_id}/restart-executor", tags=["机载维护"])
    def restart_executor(uav_id: int) -> Dict[str, Any]:
        role = _require_operator_ready()
        if uav_id not in config.uav_ids:
            raise HTTPException(status_code=422, detail="invalid UAV ID")
        if isinstance(adapter, DistributedFleetAdapter) and not role["task_publisher"] and uav_id != local_uav_id:
            raise HTTPException(status_code=403, detail="非任务发布端只能维护本机配对无人机")
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
        role = _require_operator_ready()
        if uav_id not in config.uav_ids:
            raise HTTPException(status_code=422, detail="invalid UAV ID")
        if isinstance(adapter, DistributedFleetAdapter) and not role["task_publisher"] and uav_id != local_uav_id:
            raise HTTPException(status_code=403, detail="非任务发布端只能维护本机配对无人机")
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
        role = _require_operator_ready()
        _fleet_motion_guard()
        """Reset the saved task to waypoint zero and request a fresh takeoff."""
        if uav_id not in config.uav_ids:
            raise HTTPException(status_code=422, detail="invalid UAV ID")
        if isinstance(adapter, DistributedFleetAdapter) and not role["task_publisher"] and uav_id != local_uav_id:
            raise HTTPException(status_code=403, detail="非任务发布端只能维护本机配对无人机")
        mission = orchestrator.snapshot().get("mission") or {}
        runtime = (mission.get("uavs") or {}).get(str(uav_id))
        if not runtime:
            raise HTTPException(status_code=409, detail="UAV has no assigned mission")
        payload = {
            "mission_id": mission.get("mission_id"),
            "target_altitude_m": runtime.get("target_altitude_m"),
            "recon_start_mode": 0,
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
        return {"sent": True, "uav_id": uav_id, "recon_start_mode": 0}

    @app.get("/api/v1/recording/status", summary="获取飞行数据采集状态", tags=["数据采集"])
    def recording_status() -> Dict[str, Any]:
        return recording_manager.status()

    @app.post("/api/v1/recording/start", summary="开始机载飞行数据采集", tags=["数据采集"])
    def recording_start(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        uav_id = int(payload.get("uav_id", 1))
        recording_mode = str(
            payload.get("recording_mode", payload.get("mode", "core"))
        ).strip().lower()
        if recording_mode == "complex":
            recording_mode = "full"
        if recording_mode not in ("core", "full"):
            raise HTTPException(status_code=422, detail="采集模式必须是核心或完整")
        if isinstance(adapter, DistributedFleetAdapter) and uav_id != local_uav_id:
            raise HTTPException(
                status_code=422,
                detail="this computer may only record its paired UAV",
            )
        if uav_id not in active_uav_ids:
            raise HTTPException(status_code=422, detail="UAV is not active in this run")
        try:
            return recording_manager.start(uav_id, recording_mode)
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
        def optional_float(value):
            return None if value is None else float(value)
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
            gps_status=int(payload.get("gps_status", 0)),
            location_source=int(payload.get("location_source", -1)),
            gps_num=int(payload.get("gps_num", 0)),
            latitude=optional_float(payload.get("latitude")),
            longitude=optional_float(payload.get("longitude")),
            altitude=optional_float(payload.get("altitude")),
            rel_alt=optional_float(payload.get("rel_alt")),
            pengfei=sanitize_pengfei(payload.get("pengfei")),
            pengfei_age_seconds=optional_float(payload.get("pengfei_age_seconds")),
            ego_exec_state=(int(payload["ego_exec_state"])
                            if payload.get("ego_exec_state") is not None else None),
            ego_exec_state_age_seconds=optional_float(payload.get("ego_exec_state_age_seconds")),
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
