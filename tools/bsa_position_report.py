#!/usr/bin/env python3
"""Normalize ROS odometry CSV and build a standalone position report."""

from __future__ import annotations

import argparse
import bisect
import csv
import html
import json
import math
import statistics
from datetime import datetime
from pathlib import Path


TIME_COLUMN = "%time"
X_COLUMN = "field.pose.pose.position.x"
Y_COLUMN = "field.pose.pose.position.y"
Z_COLUMN = "field.pose.pose.position.z"
VX_COLUMN = "field.twist.twist.linear.x"
VY_COLUMN = "field.twist.twist.linear.y"
VZ_COLUMN = "field.twist.twist.linear.z"

COMMAND_COLUMNS = {
    "agent": "field.Agent_CMD",
    "mode": "field.Move_mode",
    "px": "field.position_ref0",
    "py": "field.position_ref1",
    "pz": "field.position_ref2",
    "vx": "field.velocity_ref0",
    "vy": "field.velocity_ref1",
    "vz": "field.velocity_ref2",
}

STATE_COLUMNS = {
    "x": "field.position0",
    "y": "field.position1",
    "z": "field.position2",
    "vx": "field.velocity0",
    "vy": "field.velocity1",
    "vz": "field.velocity2",
}


def detect_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    return "utf-8"


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.dist(a, b)


def sample_by_interval(points: list[dict], interval_seconds: float) -> list[dict]:
    if len(points) <= 2:
        return points[:]
    sampled = [points[0]]
    for point in points[1:-1]:
        if point["t"] - sampled[-1]["t"] >= interval_seconds:
            sampled.append(point)
    sampled.append(points[-1])
    return sampled


def sample_for_html(points: list[dict], maximum: int = 2500) -> list[dict]:
    if len(points) <= maximum:
        selected = points
    else:
        stride = math.ceil(len(points) / maximum)
        selected = points[::stride]
        if selected[-1] is not points[-1]:
            selected.append(points[-1])
    numeric_keys = (
        "t", "x", "y", "z", "vx", "vy", "vz",
        "actual_x", "actual_y", "actual_z", "actual_vx", "actual_vy", "actual_vz",
        "cmd_x", "cmd_y", "cmd_z", "cmd_vx", "cmd_vy", "cmd_vz",
    )
    return [
        {key: round(float(point.get(key, 0.0)), 6) for key in numeric_keys}
        for point in selected
    ]


def load_command_samples(path) -> list[dict]:
    if path is None or not path.exists():
        return []
    raw = path.read_bytes()
    encoding = detect_encoding(raw)
    with path.open("r", encoding=encoding, newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            return []
        missing = [column for column in COMMAND_COLUMNS.values() if column not in reader.fieldnames]
        if missing:
            raise ValueError(f"Command CSV columns are missing: {missing}")
        samples = []
        for row in reader:
            try:
                timestamp_ns = int(row[TIME_COLUMN])
                agent = int(row[COMMAND_COLUMNS["agent"]])
                mode = int(row[COMMAND_COLUMNS["mode"]])
                raw_values = {
                    key: float(row[column])
                    for key, column in COMMAND_COLUMNS.items()
                    if key not in ("agent", "mode")
                }
            except (TypeError, ValueError, KeyError):
                continue

            effective = {key: 0.0 for key in ("px", "py", "pz", "vx", "vy", "vz")}
            if agent == 4:  # UAVCommand.Move
                if mode in (0, 3):  # XYZ_POS / XYZ_POS_BODY
                    effective.update({key: raw_values[key] for key in ("px", "py", "pz")})
                elif mode in (1, 5):  # XY_VEL_Z_POS / XY_VEL_Z_POS_BODY
                    effective.update({key: raw_values[key] for key in ("pz", "vx", "vy")})
                elif mode in (2, 4):  # XYZ_VEL / XYZ_VEL_BODY
                    effective.update({key: raw_values[key] for key in ("vx", "vy", "vz")})
                elif mode == 6:  # TRAJECTORY
                    effective.update(raw_values)
            samples.append({"timestamp_ns": timestamp_ns, **effective})
    samples.sort(key=lambda item: item["timestamp_ns"])
    return samples


def attach_commands(points: list[dict], commands: list[dict]) -> None:
    timestamps = [sample["timestamp_ns"] for sample in commands]
    for point in points:
        command_index = bisect.bisect_right(timestamps, point["timestamp_ns"]) - 1
        command = commands[command_index] if command_index >= 0 else {}
        point["cmd_x"] = command.get("px", 0.0)
        point["cmd_y"] = command.get("py", 0.0)
        point["cmd_z"] = command.get("pz", 0.0)
        point["cmd_vx"] = command.get("vx", 0.0)
        point["cmd_vy"] = command.get("vy", 0.0)
        point["cmd_vz"] = command.get("vz", 0.0)


def load_state_samples(path) -> list[dict]:
    if path is None or not path.exists():
        return []
    raw = path.read_bytes()
    encoding = detect_encoding(raw)
    with path.open("r", encoding=encoding, newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            return []
        required = [TIME_COLUMN, *STATE_COLUMNS.values()]
        missing = [column for column in required if column not in reader.fieldnames]
        if missing:
            raise ValueError(f"State CSV columns are missing: {missing}")
        samples = []
        for row in reader:
            try:
                sample = {
                    "timestamp_ns": int(row[TIME_COLUMN]),
                    **{key: float(row[column]) for key, column in STATE_COLUMNS.items()},
                }
            except (TypeError, ValueError, KeyError):
                continue
            samples.append(sample)
    samples.sort(key=lambda item: item["timestamp_ns"])
    return samples


def attach_actual_state(points: list[dict], states: list[dict]) -> None:
    timestamps = [sample["timestamp_ns"] for sample in states]
    for point in points:
        state_index = bisect.bisect_right(timestamps, point["timestamp_ns"]) - 1
        state = states[state_index] if state_index >= 0 else None
        point["actual_x"] = state["x"] if state else point["x"]
        point["actual_y"] = state["y"] if state else point["y"]
        point["actual_z"] = state["z"] if state else point["z"]
        point["actual_vx"] = state["vx"] if state else point["vx"]
        point["actual_vy"] = state["vy"] if state else point["vy"]
        point["actual_vz"] = state["vz"] if state else point["vz"]


def build_findings(metrics: dict) -> list[str]:
    findings = [
        (
            f"有效记录 {metrics['samples']} 条，持续 {metrics['duration']:.2f} 秒，"
            f"平均频率 {metrics['sample_rate']:.2f} Hz；最大采样间隔 {metrics['max_gap'] * 1000:.1f} ms。"
        ),
        (
            f"坐标覆盖范围：X {metrics['x_range']:.3f} m、Y {metrics['y_range']:.3f} m、"
            f"Z {metrics['z_range']:.3f} m。"
        ),
        (
            f"起点到终点的直线位移为 {metrics['net_displacement']:.3f} m；"
            f"按 5 Hz 采样估算的轨迹长度为 {metrics['path_5hz']:.3f} m。"
        ),
    ]
    if metrics["malformed_rows"] == 0:
        findings.append("所有数据行都与表头列数一致，没有发现字段错位。")
    else:
        findings.append(f"有 {metrics['malformed_rows']} 行字段数量异常，已从坐标分析中排除。")
    if metrics["max_gap"] > max(0.2, metrics["median_dt"] * 4):
        findings.append("采样过程中存在明显时间间断，分析轨迹时应检查网络或定位节点是否短暂停顿。")
    else:
        findings.append("采样间隔整体连续，没有发现超过 200 ms 的定位数据中断。")
    if max(metrics["x_range"], metrics["y_range"], metrics["z_range"]) < 0.1:
        findings.append("本次记录的平移量小于 0.1 m；若实际移动距离明显更大，应检查定位话题或坐标系是否正确。")
    return findings


def make_report(source_name: str, source_display: str, encoding: str, metrics: dict, points: list[dict]) -> str:
    data_json = json.dumps(sample_for_html(points), ensure_ascii=False, separators=(",", ":"))
    findings_html = "".join(f"<li>{html.escape(item)}</li>" for item in build_findings(metrics))
    summary_rows = [
        ("有效记录", f"{metrics['samples']:,}", "条"),
        ("记录时长", f"{metrics['duration']:.3f}", "s"),
        ("平均采样频率", f"{metrics['sample_rate']:.3f}", "Hz"),
        ("控制指令记录", f"{metrics['command_samples']:,}", "条"),
        ("飞行状态记录", f"{metrics['state_samples']:,}", "条"),
        ("采样间隔中位数", f"{metrics['median_dt'] * 1000:.3f}", "ms"),
        ("最大采样间隔", f"{metrics['max_gap'] * 1000:.3f}", "ms"),
        ("X 范围", f"{metrics['x_range']:.6f}", "m"),
        ("Y 范围", f"{metrics['y_range']:.6f}", "m"),
        ("Z 范围", f"{metrics['z_range']:.6f}", "m"),
        ("起终点直线位移", f"{metrics['net_displacement']:.6f}", "m"),
        ("轨迹长度（原始 20 Hz）", f"{metrics['path_raw']:.6f}", "m"),
        ("轨迹长度（降至约 5 Hz）", f"{metrics['path_5hz']:.6f}", "m"),
        ("三维包围盒对角线", f"{metrics['bbox_diagonal']:.6f}", "m"),
    ]
    summary_html = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{value}</td><td>{unit}</td></tr>"
        for label, value, unit in summary_rows
    )
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    metadata = html.escape(f"定位源：{source_display} · 数据文件：{source_name} · 原始编码：{encoding} · 生成时间：{generated}")
    report_title = html.escape(f"{source_display} XYZ 三维轨迹分析")

    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{report_title}</title>
<style>
:root {{ color-scheme: light dark; --bg:#f7f9fc; --surface:#ffffff; --text:#172033; --muted:#64748b; --border:#d8e0ea; --grid:#dbe3ec; --primary:#2563eb; --x:#2563eb; --y:#16a34a; --z:#dc2626; --start:#0891b2; --end:#ea580c; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f172a; --surface:#172033; --text:#e5edf7; --muted:#9badc2; --border:#34445a; --grid:#2b3a4e; --primary:#60a5fa; --x:#60a5fa; --y:#4ade80; --z:#f87171; --start:#22d3ee; --end:#fb923c; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:"Microsoft YaHei UI","Noto Sans SC",system-ui,sans-serif; }}
main {{ max-width:1280px; margin:0 auto; padding:24px; }}
h1,h2 {{ font-weight:600; margin:0; }}
h1 {{ font-size:26px; }}
h2 {{ font-size:18px; margin-bottom:14px; }}
.meta {{ color:var(--muted); margin:8px 0 20px; font-size:13px; }}
.layout {{ display:grid; grid-template-columns:minmax(0,2fr) minmax(280px,1fr); gap:18px; align-items:start; }}
.panel {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:18px; }}
.toolbar {{ display:flex; gap:14px; flex-wrap:wrap; align-items:center; margin-bottom:10px; color:var(--muted); font-size:13px; }}
button {{ appearance:none; border:1px solid var(--border); background:var(--surface); color:var(--text); border-radius:7px; padding:7px 12px; cursor:pointer; }}
button:hover {{ border-color:var(--primary); }}
canvas {{ display:block; width:100%; border:1px solid var(--border); background:var(--surface); }}
.canvas-wrap {{ position:relative; }}
.point-tooltip {{ display:none; position:absolute; z-index:3; pointer-events:none; white-space:pre; padding:8px 10px; border:1px solid var(--border); border-radius:7px; background:var(--surface); color:var(--text); box-shadow:0 5px 18px rgba(0,0,0,.18); font:13px/1.5 ui-monospace,"Cascadia Mono",monospace; font-variant-numeric:tabular-nums; }}
#trajectory3d {{ height:560px; cursor:grab; touch-action:none; }}
#trajectory3d.dragging {{ cursor:grabbing; }}
#timeseries {{ height:330px; }}
#positionCompare,#velocityCompare {{ height:clamp(320px,42vh,480px); }}
.legend {{ display:flex; flex-wrap:wrap; gap:16px; color:var(--muted); font-size:13px; }}
.key {{ display:inline-flex; align-items:center; gap:6px; }}
.swatch {{ width:18px; height:3px; display:inline-block; }}
.swatch.x {{ background:var(--x); }} .swatch.y {{ background:var(--y); }} .swatch.z {{ background:var(--z); }}
.line-style {{ width:22px; height:0; display:inline-block; border-top:3px solid var(--text); }}
.line-style.command {{ border-top-style:dashed; opacity:.82; }}
.comparison-toolbar {{ align-items:center; gap:10px 14px; margin-bottom:8px; }}
.series-toggle-list {{ display:flex; flex-wrap:wrap; gap:7px; }}
.series-toggle {{ display:inline-flex; align-items:center; gap:7px; padding:5px 9px; font-size:12px; border-radius:999px; }}
.series-toggle .series-line {{ width:20px; height:0; border-top:3px solid var(--series-color); }}
.series-toggle.command .series-line {{ border-top-style:dashed; }}
.series-toggle.disabled {{ opacity:.38; text-decoration:line-through; }}
.comparison-value {{ flex-basis:100%; min-height:20px; line-height:1.55; font-variant-numeric:tabular-nums; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th,td {{ padding:9px 8px; border-bottom:1px solid var(--border); text-align:right; font-variant-numeric:tabular-nums; }}
th {{ text-align:left; font-weight:500; }} td:last-child {{ color:var(--muted); width:46px; }}
.findings {{ margin-top:18px; }}
.findings ul {{ margin:0; padding-left:22px; }}
.findings li {{ margin:8px 0; line-height:1.55; }}
.full {{ margin-top:18px; }}
.coordinates {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:16px; }}
.coordinates div {{ border-left:3px solid var(--border); padding-left:10px; }}
.coordinates span {{ display:block; color:var(--muted); font-size:12px; }}
.coordinates code {{ display:block; margin-top:4px; color:var(--text); white-space:nowrap; }}
@media (max-width:820px) {{ main {{ padding:14px; }} .layout {{ grid-template-columns:1fr; }} #trajectory3d {{ height:440px; }} .coordinates {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<main>
  <h1>{report_title}</h1>
  <div class="meta">{metadata}</div>
  <section class="layout">
    <div class="panel">
      <h2>三维轨迹</h2>
      <div class="toolbar"><button id="resetView" type="button">恢复视角</button><span>悬停轨迹点查看坐标 · 鼠标拖动旋转 · 滚轮缩放 · 青色为起点 · 橙色为终点</span></div>
      <div class="canvas-wrap"><canvas id="trajectory3d" aria-label="XYZ 三维轨迹交互图"></canvas><div id="pointTooltip" class="point-tooltip" role="tooltip"></div></div>
      <div class="coordinates"><div><span>起点 X / Y / Z（m）</span><code>{metrics['start'][0]:.6f} / {metrics['start'][1]:.6f} / {metrics['start'][2]:.6f}</code></div><div><span>终点 X / Y / Z（m）</span><code>{metrics['end'][0]:.6f} / {metrics['end'][1]:.6f} / {metrics['end'][2]:.6f}</code></div></div>
    </div>
    <aside>
      <div class="panel">
        <h2>关键数据</h2>
        <table><tbody>{summary_html}</tbody></table>
      </div>
      <div class="panel findings">
        <h2>分析结论</h2>
        <ul>{findings_html}</ul>
      </div>
    </aside>
  </section>
  <section class="panel full">
    <h2>位置坐标随时间变化</h2>
    <div class="legend"><span class="key"><i class="swatch x"></i>X</span><span class="key"><i class="swatch y"></i>Y</span><span class="key"><i class="swatch z"></i>Z</span><span id="hoverValue">移动鼠标查看对应时刻坐标</span></div>
    <canvas id="timeseries" aria-label="X Y Z 坐标随时间变化曲线"></canvas>
  </section>
  <section class="panel full">
    <h2>指令位置与实际位置对比</h2>
    <div class="legend comparison-toolbar"><div id="positionLegend" class="series-toggle-list" aria-label="位置曲线显示开关"></div><span id="positionCompareValue" class="comparison-value">移动鼠标查看对应时刻</span></div>
    <canvas id="positionCompare" aria-label="XYZ 指令位置与实际位置对比"></canvas>
  </section>
  <section class="panel full">
    <h2>指令速度与实际速度对比</h2>
    <div class="legend comparison-toolbar"><div id="velocityLegend" class="series-toggle-list" aria-label="速度曲线显示开关"></div><span id="velocityCompareValue" class="comparison-value">移动鼠标查看对应时刻</span></div>
    <canvas id="velocityCompare" aria-label="XYZ 指令速度与实际速度对比"></canvas>
  </section>
</main>
<script>
const points = {data_json};
const rootStyle = getComputedStyle(document.documentElement);
const color = name => rootStyle.getPropertyValue(name).trim();

function setupCanvas(canvas) {{
  const rect = canvas.getBoundingClientRect();
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  canvas.width = Math.round(rect.width * ratio);
  canvas.height = Math.round(rect.height * ratio);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return {{ctx, width:rect.width, height:rect.height}};
}}

const view = {{yaw:-0.72, pitch:0.52, zoom:1}};
const canvas3d = document.getElementById('trajectory3d');
const pointTooltip = document.getElementById('pointTooltip');
let projected3d=[];
let hover3dIndex=null;
function draw3d() {{
  const {{ctx,width,height}} = setupCanvas(canvas3d);
  ctx.clearRect(0,0,width,height);
  const xs=points.map(p=>p.x), ys=points.map(p=>p.y), zs=points.map(p=>p.z);
  const min={{x:Math.min(...xs),y:Math.min(...ys),z:Math.min(...zs)}};
  const max={{x:Math.max(...xs),y:Math.max(...ys),z:Math.max(...zs)}};
  const center={{x:(min.x+max.x)/2,y:(min.y+max.y)/2,z:(min.z+max.z)/2}};
  const span=Math.max(max.x-min.x,max.y-min.y,max.z-min.z,1e-6);
  const scale=Math.min(width,height)*0.68/span*view.zoom;
  const cy=Math.cos(view.yaw), sy=Math.sin(view.yaw), cp=Math.cos(view.pitch), sp=Math.sin(view.pitch);
  const project=(p)=>{{
    const dx=p.x-center.x, dy=p.y-center.y, dz=p.z-center.z;
    const rx=dx*cy-dy*sy;
    const ry=dx*sy+dy*cy;
    const py=ry*cp-dz*sp;
    const depth=ry*sp+dz*cp;
    const perspective=3/(3-depth/span);
    return {{x:width/2+rx*scale*perspective,y:height/2-py*scale*perspective,depth}};
  }};
  projected3d=points.map(project);
  const axisOrigin={{x:min.x,y:min.y,z:min.z}};
  const axisLength=span*0.32;
  const axes=[['X',{{x:min.x+axisLength,y:min.y,z:min.z}},color('--x')],['Y',{{x:min.x,y:min.y+axisLength,z:min.z}},color('--y')],['Z',{{x:min.x,y:min.y,z:min.z+axisLength}},color('--z')]];
  const origin2=project(axisOrigin);
  ctx.lineWidth=1.5; ctx.font='12px system-ui';
  axes.forEach(([label,end,c])=>{{ const e=project(end); ctx.strokeStyle=c; ctx.beginPath(); ctx.moveTo(origin2.x,origin2.y); ctx.lineTo(e.x,e.y); ctx.stroke(); ctx.fillStyle=c; ctx.fillText(label,e.x+5,e.y-5); }});
  ctx.strokeStyle=color('--primary'); ctx.lineWidth=2; ctx.lineJoin='round'; ctx.beginPath();
  projected3d.forEach((q,i)=>{{ if(i===0)ctx.moveTo(q.x,q.y);else ctx.lineTo(q.x,q.y); }}); ctx.stroke();
  const drawMarker=(p,c,label)=>{{ const q=project(p); ctx.fillStyle=c; ctx.beginPath(); ctx.arc(q.x,q.y,5,0,Math.PI*2); ctx.fill(); ctx.font='12px system-ui'; ctx.fillText(label,q.x+8,q.y-8); }};
  drawMarker(points[0],color('--start'),'起点'); drawMarker(points[points.length-1],color('--end'),'终点');
  if(hover3dIndex!==null){{const q=projected3d[hover3dIndex];ctx.strokeStyle=color('--end');ctx.fillStyle=color('--surface');ctx.lineWidth=2.5;ctx.beginPath();ctx.arc(q.x,q.y,7,0,Math.PI*2);ctx.fill();ctx.stroke();}}
}}

let dragging=false,lastX=0,lastY=0;
function clearPointTooltip(){{if(hover3dIndex!==null){{hover3dIndex=null;draw3d();}}pointTooltip.style.display='none';}}
canvas3d.addEventListener('pointerdown',e=>{{clearPointTooltip();dragging=true;lastX=e.clientX;lastY=e.clientY;canvas3d.setPointerCapture(e.pointerId);canvas3d.classList.add('dragging');}});
canvas3d.addEventListener('pointermove',e=>{{
  if(dragging){{view.yaw+=(e.clientX-lastX)*0.008;view.pitch=Math.max(-1.35,Math.min(1.35,view.pitch+(e.clientY-lastY)*0.008));lastX=e.clientX;lastY=e.clientY;draw3d();return;}}
  const rect=canvas3d.getBoundingClientRect(),mx=e.clientX-rect.left,my=e.clientY-rect.top;
  let nearest=null,bestDistance=14*14;
  projected3d.forEach((q,i)=>{{const d=(q.x-mx)*(q.x-mx)+(q.y-my)*(q.y-my);if(d<bestDistance){{bestDistance=d;nearest=i;}}}});
  if(nearest===null){{clearPointTooltip();return;}}
  if(hover3dIndex!==nearest){{hover3dIndex=nearest;draw3d();}}
  const p=points[nearest];pointTooltip.textContent=`t = ${{p.t.toFixed(3)}} s\nX = ${{p.x.toFixed(6)}} m\nY = ${{p.y.toFixed(6)}} m\nZ = ${{p.z.toFixed(6)}} m\n实际 Vx = ${{p.actual_vx.toFixed(6)}} m/s\n实际 Vy = ${{p.actual_vy.toFixed(6)}} m/s\n实际 Vz = ${{p.actual_vz.toFixed(6)}} m/s`;
  pointTooltip.style.display='block';
  const left=Math.max(6,Math.min(rect.width-pointTooltip.offsetWidth-6,mx+14));
  const top=Math.max(6,Math.min(rect.height-pointTooltip.offsetHeight-6,my+14));
  pointTooltip.style.left=`${{left}}px`;pointTooltip.style.top=`${{top}}px`;
}});
canvas3d.addEventListener('pointerup',()=>{{dragging=false;canvas3d.classList.remove('dragging');}});
canvas3d.addEventListener('pointerleave',()=>{{if(!dragging)clearPointTooltip();}});
canvas3d.addEventListener('wheel',e=>{{e.preventDefault();clearPointTooltip();view.zoom=Math.max(0.35,Math.min(4,view.zoom*Math.exp(-e.deltaY*0.001)));draw3d();}},{{passive:false}});
document.getElementById('resetView').addEventListener('click',()=>{{clearPointTooltip();view.yaw=-0.72;view.pitch=0.52;view.zoom=1;draw3d();}});

const timeCanvas=document.getElementById('timeseries');
let seriesGeometry=null;
function drawTimeSeries(hoverIndex=null) {{
  const {{ctx,width,height}}=setupCanvas(timeCanvas); ctx.clearRect(0,0,width,height);
  const margin={{left:66,right:18,top:18,bottom:42}}, plot={{x:margin.left,y:margin.top,w:width-margin.left-margin.right,h:height-margin.top-margin.bottom}};
  const tMax=Math.max(...points.map(p=>p.t)); const vals=points.flatMap(p=>[p.x,p.y,p.z]); let vMin=Math.min(...vals),vMax=Math.max(...vals); const pad=(vMax-vMin||1)*0.06;vMin-=pad;vMax+=pad;
  const sx=t=>plot.x+(t/tMax)*plot.w, sy=v=>plot.y+plot.h-(v-vMin)/(vMax-vMin)*plot.h;
  ctx.strokeStyle=color('--grid');ctx.lineWidth=1;ctx.font='12px system-ui';ctx.fillStyle=color('--muted');
  for(let i=0;i<=5;i++){{const yy=plot.y+plot.h*i/5;ctx.beginPath();ctx.moveTo(plot.x,yy);ctx.lineTo(plot.x+plot.w,yy);ctx.stroke();const value=vMax-(vMax-vMin)*i/5;ctx.textAlign='right';ctx.fillText(value.toFixed(3),plot.x-8,yy+4);}}
  for(let i=0;i<=5;i++){{const xx=plot.x+plot.w*i/5;ctx.beginPath();ctx.moveTo(xx,plot.y);ctx.lineTo(xx,plot.y+plot.h);ctx.stroke();ctx.textAlign=i===0?'left':i===5?'right':'center';ctx.fillText((tMax*i/5).toFixed(1),xx,plot.y+plot.h+22);}}
  ctx.fillStyle=color('--text');ctx.textAlign='center';ctx.fillText('时间（s）',plot.x+plot.w/2,height-8);ctx.save();ctx.translate(16,plot.y+plot.h/2);ctx.rotate(-Math.PI/2);ctx.fillText('位置（m）',0,0);ctx.restore();
  [['x','--x'],['y','--y'],['z','--z']].forEach(([key,c])=>{{ctx.strokeStyle=color(c);ctx.lineWidth=1.7;ctx.beginPath();points.forEach((p,i)=>{{const xx=sx(p.t),yy=sy(p[key]);if(i===0)ctx.moveTo(xx,yy);else ctx.lineTo(xx,yy);}});ctx.stroke();}});
  if(hoverIndex!==null){{const p=points[hoverIndex],xx=sx(p.t);ctx.strokeStyle=color('--muted');ctx.beginPath();ctx.moveTo(xx,plot.y);ctx.lineTo(xx,plot.y+plot.h);ctx.stroke();[['x','--x'],['y','--y'],['z','--z']].forEach(([key,c])=>{{ctx.fillStyle=color(c);ctx.beginPath();ctx.arc(xx,sy(p[key]),4,0,Math.PI*2);ctx.fill();}});}}
  seriesGeometry={{plot,tMax}};
}}
timeCanvas.addEventListener('pointermove',e=>{{if(!seriesGeometry)return;const rect=timeCanvas.getBoundingClientRect();const x=e.clientX-rect.left;const ratio=Math.max(0,Math.min(1,(x-seriesGeometry.plot.x)/seriesGeometry.plot.w));const target=ratio*seriesGeometry.tMax;let lo=0,hi=points.length-1;while(lo<hi){{const mid=(lo+hi)>>1;if(points[mid].t<target)lo=mid+1;else hi=mid;}}const i=Math.max(0,Math.min(points.length-1,lo));const p=points[i];document.getElementById('hoverValue').textContent=`t=${{p.t.toFixed(2)}} s · X=${{p.x.toFixed(4)}} m · Y=${{p.y.toFixed(4)}} m · Z=${{p.z.toFixed(4)}} m`;drawTimeSeries(i);}});
timeCanvas.addEventListener('pointerleave',()=>{{document.getElementById('hoverValue').textContent='移动鼠标查看对应时刻坐标';drawTimeSeries();}});

function makeComparisonChart(canvasId,legendId,valueId,actualKeys,commandKeys,yLabel,valueName) {{
  const canvas=document.getElementById(canvasId),legend=document.getElementById(legendId),valueElement=document.getElementById(valueId);
  let geometry=null;
  const axes=[['X','--x'],['Y','--y'],['Z','--z']];
  const series=[];
  axes.forEach(([axis,colorVariable],axisIndex)=>{{
    series.push({{key:actualKeys[axisIndex],label:`实际 ${{axis}} ${{valueName}}`,colorVariable,isCommand:false,visible:true}});
    series.push({{key:commandKeys[axisIndex],label:`指令 ${{axis}} ${{valueName}}`,colorVariable,isCommand:true,visible:true}});
  }});
  series.forEach((item,index)=>{{
    const button=document.createElement('button');button.type='button';button.className=`series-toggle${{item.isCommand?' command':''}}`;button.style.setProperty('--series-color',color(item.colorVariable));button.setAttribute('aria-pressed','true');button.title=`点击隐藏 ${{item.label}}`;
    const line=document.createElement('i');line.className='series-line';const label=document.createElement('span');label.textContent=item.label;button.append(line,label);legend.appendChild(button);
    button.addEventListener('click',()=>{{item.visible=!item.visible;button.classList.toggle('disabled',!item.visible);button.setAttribute('aria-pressed',String(item.visible));button.title=`点击${{item.visible?'隐藏':'显示'}} ${{item.label}}`;valueElement.textContent='曲线显示已调整，纵轴已自动适配';draw();}});
  }});
  function draw(hoverIndex=null) {{
    const {{ctx,width,height}}=setupCanvas(canvas);ctx.clearRect(0,0,width,height);
    const margin={{left:66,right:18,top:18,bottom:42}},plot={{x:margin.left,y:margin.top,w:width-margin.left-margin.right,h:height-margin.top-margin.bottom}};
    const tMax=Math.max(...points.map(p=>p.t),1e-9);
    const visibleSeries=series.filter(item=>item.visible);
    const values=[];points.forEach(p=>visibleSeries.forEach(item=>values.push(Number.isFinite(p[item.key])?p[item.key]:0)));if(values.length===0)values.push(0);
    let vMin=Math.min(...values),vMax=Math.max(...values);const pad=(vMax-vMin||1)*0.08;vMin-=pad;vMax+=pad;
    const sx=t=>plot.x+(t/tMax)*plot.w,sy=v=>plot.y+plot.h-(v-vMin)/(vMax-vMin)*plot.h;
    ctx.strokeStyle=color('--grid');ctx.lineWidth=1;ctx.font='12px system-ui';ctx.fillStyle=color('--muted');
    for(let i=0;i<=5;i++){{const yy=plot.y+plot.h*i/5;ctx.beginPath();ctx.moveTo(plot.x,yy);ctx.lineTo(plot.x+plot.w,yy);ctx.stroke();ctx.textAlign='right';ctx.fillText((vMax-(vMax-vMin)*i/5).toFixed(3),plot.x-8,yy+4);}}
    for(let i=0;i<=5;i++){{const xx=plot.x+plot.w*i/5;ctx.beginPath();ctx.moveTo(xx,plot.y);ctx.lineTo(xx,plot.y+plot.h);ctx.stroke();ctx.textAlign=i===0?'left':i===5?'right':'center';ctx.fillText((tMax*i/5).toFixed(1),xx,plot.y+plot.h+22);}}
    ctx.fillStyle=color('--text');ctx.textAlign='center';ctx.fillText('时间（s）',plot.x+plot.w/2,height-8);ctx.save();ctx.translate(16,plot.y+plot.h/2);ctx.rotate(-Math.PI/2);ctx.fillText(yLabel,0,0);ctx.restore();
    visibleSeries.forEach(item=>{{ctx.strokeStyle=color(item.colorVariable);ctx.lineWidth=item.isCommand?1.8:2.2;ctx.setLineDash(item.isCommand?[7,5]:[]);ctx.globalAlpha=item.isCommand?.82:1;ctx.beginPath();points.forEach((p,i)=>{{const xx=sx(p.t),yy=sy(Number.isFinite(p[item.key])?p[item.key]:0);if(i===0)ctx.moveTo(xx,yy);else ctx.lineTo(xx,yy);}});ctx.stroke();}});ctx.setLineDash([]);ctx.globalAlpha=1;
    if(hoverIndex!==null){{const p=points[hoverIndex],xx=sx(p.t);ctx.strokeStyle=color('--muted');ctx.beginPath();ctx.moveTo(xx,plot.y);ctx.lineTo(xx,plot.y+plot.h);ctx.stroke();visibleSeries.forEach(item=>{{ctx.fillStyle=color(item.colorVariable);ctx.beginPath();ctx.arc(xx,sy(Number.isFinite(p[item.key])?p[item.key]:0),item.isCommand?3:4,0,Math.PI*2);ctx.fill();}});}}
    geometry={{plot,tMax}};
  }}
  canvas.addEventListener('pointermove',e=>{{
    if(!geometry)return;const rect=canvas.getBoundingClientRect();const ratio=Math.max(0,Math.min(1,(e.clientX-rect.left-geometry.plot.x)/geometry.plot.w));const target=ratio*geometry.tMax;
    let lo=0,hi=points.length-1;while(lo<hi){{const mid=(lo+hi)>>1;if(points[mid].t<target)lo=mid+1;else hi=mid;}}const p=points[lo];
    const visibleValues=series.filter(item=>item.visible).map(item=>`${{item.label}}=${{(Number.isFinite(p[item.key])?p[item.key]:0).toFixed(3)}}`).join(' · ');
    valueElement.textContent=`t=${{p.t.toFixed(2)}} s${{visibleValues?' · '+visibleValues:' · 当前全部曲线已隐藏'}}`;
    draw(lo);
  }});
  canvas.addEventListener('pointerleave',()=>{{valueElement.textContent='移动鼠标查看对应时刻';draw();}});
  return draw;
}}
const drawPositionCompare=makeComparisonChart('positionCompare','positionLegend','positionCompareValue',['actual_x','actual_y','actual_z'],['cmd_x','cmd_y','cmd_z'],'位置（m）','位置');
const drawVelocityCompare=makeComparisonChart('velocityCompare','velocityLegend','velocityCompareValue',['actual_vx','actual_vy','actual_vz'],['cmd_vx','cmd_vy','cmd_vz'],'速度（m/s）','速度');
const redraw=()=>{{draw3d();drawTimeSeries();drawPositionCompare();drawVelocityCompare();}};
new ResizeObserver(redraw).observe(document.querySelector('main'));
if(window.matchMedia)window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>location.reload());
redraw();
</script>
</body>
</html>'''


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize odometry CSV and generate an HTML report.")
    parser.add_argument("--input", required=True, help="Path to a rostopic odometry CSV")
    parser.add_argument("--command-input", help="Optional rostopic UAVCommand CSV")
    parser.add_argument("--state-input", help="Optional rostopic UAVState CSV")
    parser.add_argument("--output-dir", help="Output directory; defaults to the input file directory")
    parser.add_argument("--source", choices=("vision", "mid360"), default="vision")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    command_path = Path(args.command_input).resolve() if args.command_input else None
    state_path = Path(args.state_input).resolve() if args.state_input else None
    output_dir = Path(args.output_dir).resolve() if args.output_dir else input_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    raw = input_path.read_bytes()
    encoding = detect_encoding(raw)
    with input_path.open("r", encoding=encoding, newline="") as stream:
        all_rows = list(csv.reader(stream))
    if len(all_rows) < 2:
        raise ValueError("CSV does not contain any data rows.")

    header = all_rows[0]
    column_count = len(header)
    required = [TIME_COLUMN, X_COLUMN, Y_COLUMN, Z_COLUMN]
    missing = [name for name in required if name not in header]
    if missing:
        raise ValueError(f"Required columns are missing: {missing}")
    indexes = {name: header.index(name) for name in required}
    velocity_indexes = {
        "vx": header.index(VX_COLUMN) if VX_COLUMN in header else None,
        "vy": header.index(VY_COLUMN) if VY_COLUMN in header else None,
        "vz": header.index(VZ_COLUMN) if VZ_COLUMN in header else None,
    }
    good_rows = []
    malformed_rows = 0
    for row in all_rows[1:]:
        if len(row) != column_count:
            malformed_rows += 1
            continue
        good_rows.append(row)
    if not good_rows:
        raise ValueError("No valid data rows remain after column validation.")

    points = []
    first_ns = int(good_rows[0][indexes[TIME_COLUMN]])
    for row in good_rows:
        timestamp_ns = int(row[indexes[TIME_COLUMN]])
        points.append({
            "t": (timestamp_ns - first_ns) / 1e9,
            "timestamp_ns": timestamp_ns,
            "x": float(row[indexes[X_COLUMN]]),
            "y": float(row[indexes[Y_COLUMN]]),
            "z": float(row[indexes[Z_COLUMN]]),
            "vx": float(row[velocity_indexes["vx"]]) if velocity_indexes["vx"] is not None else 0.0,
            "vy": float(row[velocity_indexes["vy"]]) if velocity_indexes["vy"] is not None else 0.0,
            "vz": float(row[velocity_indexes["vz"]]) if velocity_indexes["vz"] is not None else 0.0,
        })

    command_samples = load_command_samples(command_path)
    state_samples = load_state_samples(state_path)
    attach_commands(points, command_samples)
    attach_actual_state(points, state_samples)

    duration = points[-1]["t"] - points[0]["t"]
    intervals = [points[i]["t"] - points[i - 1]["t"] for i in range(1, len(points))]
    xyz = [(p["x"], p["y"], p["z"]) for p in points]
    xs, ys, zs = zip(*xyz)
    ranges = (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    path_raw = sum(distance(xyz[i - 1], xyz[i]) for i in range(1, len(xyz)))
    sampled_5hz = sample_by_interval(points, 0.2)
    xyz_5hz = [(p["x"], p["y"], p["z"]) for p in sampled_5hz]
    path_5hz = sum(distance(xyz_5hz[i - 1], xyz_5hz[i]) for i in range(1, len(xyz_5hz)))
    source_display = "BSA SLAM 视觉定位" if args.source == "vision" else "MID-360 FAST-LIO 激光定位"
    metrics = {
        "location_source": args.source,
        "location_source_display": source_display,
        "samples": len(points),
        "duration": duration,
        "sample_rate": (len(points) - 1) / duration if duration > 0 else 0.0,
        "median_dt": statistics.median(intervals) if intervals else 0.0,
        "p95_dt": percentile(intervals, 0.95),
        "max_gap": max(intervals) if intervals else 0.0,
        "malformed_rows": malformed_rows,
        "x_range": ranges[0], "y_range": ranges[1], "z_range": ranges[2],
        "start": xyz[0], "end": xyz[-1],
        "net_displacement": distance(xyz[0], xyz[-1]),
        "path_raw": path_raw,
        "path_5hz": path_5hz,
        "bbox_diagonal": math.sqrt(sum(value * value for value in ranges)),
        "command_samples": len(command_samples),
        "state_samples": len(state_samples),
    }

    clean_path = output_dir / f"{args.source}_odometry_clean.csv"
    with clean_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(good_rows)

    report_path = output_dir / f"{args.source}_position_report.html"
    report_path.write_text(make_report(input_path.name, source_display, encoding, metrics, points), encoding="utf-8", newline="")
    metrics_path = output_dir / f"{args.source}_position_metrics.json"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Input: {input_path}")
    print(f"Encoding: {encoding}; columns: {column_count}; valid rows: {len(points)}; malformed rows: {malformed_rows}")
    print(f"Duration: {duration:.3f} s; sample rate: {metrics['sample_rate']:.3f} Hz")
    print(f"Command samples: {len(command_samples)}; missing command components are zero")
    print(f"State samples: {len(state_samples)}; fallback to odometry when unavailable")
    print(f"Clean CSV: {clean_path}")
    print(f"HTML report: {report_path}")
    print(f"Metrics JSON: {metrics_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
