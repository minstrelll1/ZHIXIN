"""双机型机队配置。配置中不保存认证令牌，不根据编号猜测 IP。"""
import copy
import hashlib
import ipaddress
import json
import math
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
PROFILES = {
    "p600": {"label": "P600", "workspace": "~/p600_experiment"},
    "su17": {"label": "SU17", "workspace": "~/su17_experiment"},
}

P600_ONBOARD_HOSTS = {
    1: "192.168.1.202", 2: "192.168.1.207", 3: "192.168.1.212",
    4: "192.168.1.217", 5: "192.168.1.222", 6: "192.168.1.227",
}
# SU17 目前只有一台可用机载电脑；地面端机地网卡使用现场的
# 192.168.1.121～192.168.1.126 之一，因此只自动固定机载地址，地面地址由
# 任务发布端在机队配置页按实际接线选择。
SU17_ONBOARD_HOST = "192.168.1.88"
# 机地直连网卡默认使用 192.168.1.121～192.168.1.126。
GROUND_LINK_HOSTS = {
    1: "192.168.1.121", 2: "192.168.1.122", 3: "192.168.1.123",
    4: "192.168.1.124", 5: "192.168.1.125", 6: "192.168.1.126",
}
SU17_GROUND_HOSTS = set(GROUND_LINK_HOSTS.values())
# 地面端之间同步仍使用 192.168.2.* 网段；它与机地直连网卡分开。
GROUND_PEER_HOSTS = {
    1: "192.168.2.202", 2: "192.168.2.207", 3: "192.168.2.212",
    4: "192.168.2.217", 5: "192.168.2.222", 6: "192.168.2.227",
}


def vehicle_defaults(uav_id):
    return dict(uav_id=uav_id, model="p600", enabled=True, device_id="",
                ground_terminal_id=uav_id, onboard_host="", ground_host="",
                peer_host="", web_port=8000, task_port=56100, image_port=56010,
                rosbridge_port=9090, video_webrtc_port=8891, video_rtsp_source="",
                max_speed_mps=1.0, max_distance_m=2000.0,
                scan_mode="external_ack", scan_timeout_sec=30.0,
                image_stamp_mode="preserve_or_receive", pointcloud_enabled=True,
                pointcloud_source="groundstation_shared")


def default_fleet():
    return {"schema_version": SCHEMA_VERSION,
            "vehicles": [vehicle_defaults(i) for i in range(1, 7)]}


def apply_fixed_binding(config, terminal_id, model):
    """补齐已确认的默认值；保留发布端的覆盖值，换机型时清除旧设备绑定。"""
    terminal_id = _integer(terminal_id, "地面终端编号", 1, 6)
    if model not in PROFILES:
        raise ValueError("机型必须为 p600 或 su17")
    value = validate_fleet(copy.deepcopy(config))
    matches = [v for v in value["vehicles"] if v["ground_terminal_id"] == terminal_id]
    if len(matches) != 1:
        raise ValueError("地面终端编号必须唯一对应一架无人机")
    target = matches[0]
    changed_model = target["model"] != model
    if changed_model:
        # 新机型的物理设备身份不能沿用旧绑定；SU17 网络未确认，不猜测。
        target.update(device_id="", onboard_host="", ground_host="", video_rtsp_source="")
    target["model"] = model
    target["peer_host"] = target["peer_host"] or GROUND_PEER_HOSTS[terminal_id]
    if model == "p600":
        onboard_host = target["onboard_host"] or P600_ONBOARD_HOSTS[target["uav_id"]]
        target["onboard_host"] = onboard_host
        # 六套 P600 的机地直连网卡默认使用 192.168.1.121～.126。
        target["ground_host"] = target["ground_host"] or GROUND_LINK_HOSTS[terminal_id]
        target["video_rtsp_source"] = target["video_rtsp_source"] or "rtsp://%s:8554/live" % onboard_host
    elif model == "su17":
        target["onboard_host"] = target["onboard_host"] or SU17_ONBOARD_HOST
    return validate_fleet(value)


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("%s 必须是 %s～%s 的整数" % (name, low, high))
    return value


def validate_fleet(raw):
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("不支持的机队配置版本")
    entries = raw.get("vehicles")
    if not isinstance(entries, list) or len(entries) != 6:
        raise ValueError("配置必须包含 UAV1～UAV6 六个条目，可将暂不使用的飞机停用")
    result, ids, devices, terminals, onboard_hosts = [], set(), set(), set(), set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("每架飞机的配置必须为对象")
        uid = _integer(item.get("uav_id"), "无人机编号", 1, 6)
        if uid in ids:
            raise ValueError("无人机编号重复")
        ids.add(uid)
        v = vehicle_defaults(uid)
        unknown = set(item) - set(v)
        if unknown:
            raise ValueError("未知配置字段：" + ", ".join(sorted(unknown)))
        v.update(item)
        for key in ("onboard_host", "ground_host", "peer_host", "device_id", "video_rtsp_source"):
            if not isinstance(v[key], str):
                raise ValueError(key + " 必须为字符串，可留空")
        if v["model"] not in PROFILES:
            raise ValueError("机型必须为 p600 或 su17")
        for flag in ("enabled", "pointcloud_enabled"):
            if not isinstance(v[flag], bool):
                raise ValueError(flag + " 必须为布尔值")
        for name in ("onboard_host", "ground_host", "peer_host"):
            value = str(v[name]).strip()
            if value:
                address = ipaddress.ip_address(value)
                if address.version != 4 or address.is_unspecified or address.is_multicast:
                    raise ValueError(name + " 必须为有效单播 IPv4 地址")
            v[name] = value
        if v["model"] == "su17":
            if v["onboard_host"] and v["onboard_host"] != SU17_ONBOARD_HOST:
                raise ValueError("SU17 机载 IP 必须为 192.168.1.88")
            if v["ground_host"] and v["ground_host"] not in SU17_GROUND_HOSTS:
                raise ValueError("SU17 地面机地网卡 IP 必须为 192.168.1.121～192.168.1.126")
        for name in ("web_port", "task_port", "image_port", "rosbridge_port", "video_webrtc_port"):
            v[name] = _integer(v[name], name, 1, 65535)
        if len({v[n] for n in ("web_port", "task_port", "image_port", "video_webrtc_port")}) != 4:
            raise ValueError("同一地面终端的网页、任务、图片和视频端口不能重复")
        v["ground_terminal_id"] = _integer(v["ground_terminal_id"], "地面终端编号", 1, 6)
        for name in ("max_speed_mps", "max_distance_m", "scan_timeout_sec"):
            if isinstance(v[name], bool):
                raise ValueError(name + " 必须为正数")
            v[name] = float(v[name])
            if not math.isfinite(v[name]) or v[name] <= 0:
                raise ValueError(name + " 必须为有限正数")
        if v["scan_mode"] not in ("external_ack", "timed_hover"):
            raise ValueError("扫描方式必须为 external_ack 或 timed_hover")
        if v["image_stamp_mode"] not in ("preserve_or_receive", "require_source"):
            raise ValueError("图像时间戳方式无效")
        if v["pointcloud_source"] not in ("groundstation_shared", "onboard"):
            raise ValueError("点云来源必须为 groundstation_shared 或 onboard")
        v["device_id"] = str(v["device_id"]).strip()
        if len(v["device_id"]) > 128 or any(ord(c) < 32 for c in v["device_id"]):
            raise ValueError("设备标识无效")
        v["video_rtsp_source"] = str(v["video_rtsp_source"]).strip()
        if v["video_rtsp_source"]:
            url = urlsplit(v["video_rtsp_source"])
            if url.scheme not in ("rtsp", "rtsps") or not url.hostname or url.username or url.password or any(c.isspace() for c in v["video_rtsp_source"]):
                raise ValueError("视频源必须为不含密码的 RTSP 地址")
        if v["enabled"]:
            if v["ground_terminal_id"] in terminals:
                raise ValueError("启用的飞机不能共用同一个地面终端编号")
            terminals.add(v["ground_terminal_id"])
            if v["device_id"] and v["device_id"] in devices:
                raise ValueError("设备标识重复")
            if v["device_id"]:
                devices.add(v["device_id"])
            # 独立网段可以重复使用机载地址；同一个地面地址不能重复配对同一地址。
            endpoint = (v["ground_host"], v["onboard_host"])
            if all(endpoint) and endpoint in onboard_hosts:
                raise ValueError("同一网络的机载地址重复")
            onboard_hosts.add(endpoint)
        result.append(v)
    return {"schema_version": SCHEMA_VERSION, "vehicles": sorted(result, key=lambda v: v["uav_id"])}


def revision(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def vehicle(config, uid):
    return next(v for v in config["vehicles"] if v["uav_id"] == int(uid))


def resolved_vehicle(v):
    out = copy.deepcopy(v)
    prefix = "/uav%d" % v["uav_id"]
    out.update(ros_namespace=prefix, model_label=PROFILES[v["model"]]["label"],
               vendor_workspace=PROFILES[v["model"]]["workspace"],
               local_ros_uav_id=(1 if v["model"] == "su17" else v["uav_id"]),
               state_topic=prefix + "/prometheus/state", command_topic=prefix + "/prometheus/command",
               control_state_topic=prefix + "/prometheus/control_state",
               raw_image_topic=prefix + "/gimbal/image_original",
               image_topic=prefix + "/competition/image_stamped",
               scan_request_topic=prefix + "/competition/scan/request",
                scan_status_topic=prefix + "/competition/scan/status",
                pointcloud_topic=prefix + "/octomap_point_cloud_centers/reduce_the_frequency")
    out["pending"] = [label for name, label in (("onboard_host", "机载 IP"), ("ground_host", "机地网卡 IP"), ("device_id", "设备标识")) if not v[name]]
    return out


class FleetStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._value = validate_fleet(json.loads(self.path.read_text(encoding="utf-8-sig"))) if self.path.exists() else default_fleet()

    def read(self):
        with self._lock:
            return copy.deepcopy(self._value)

    def save(self, raw, expected_revision):
        value = validate_fleet(raw)
        with self._lock:
            if expected_revision != revision(self._value):
                raise ValueError("配置已被其他窗口修改，请重新读取后保存")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="fleet-", suffix=".tmp", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, str(self.path))
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            self._value = value
            return self.read()


def validate_identity(hello, config):
    """按固定 UAV 编号和机型接入；设备标识仅作状态信息，不阻塞启动。"""
    uid = _integer(hello.get("uav_id"), "飞控编号", 1, 6)
    v = vehicle(config, uid)
    if not v["enabled"]:
        raise ValueError("该无人机已停用")
    if hello.get("flight_controller_id") != uid or hello.get("ros_namespace") != "/uav%d" % uid:
        raise ValueError("飞控编号、ROS 命名空间与竞赛编号不一致")
    if hello.get("model") != v["model"]:
        raise ValueError("上报机型与机队配置不一致")
    if hello.get("identity_verified") is not True:
        raise ValueError("机载程序尚未核验飞控编号")
    return {key: copy.deepcopy(hello.get(key)) for key in
            ("device_id", "model", "flight_controller_id", "ros_namespace", "identity_verified", "capabilities", "software_version", "fleet_revision")}
