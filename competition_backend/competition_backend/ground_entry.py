"""先显示网页选终端，再启动该终端的机地服务；不预先绑定 UAV1。"""
import asyncio
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Body, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, FileResponse
from .program_manager import ProgramManager, ConsoleTee
from competition_shared.fleet import FleetStore, PROFILES
from competition_shared.runtime import ground_environment

ROOT = Path(__file__).resolve().parents[2]
WEB = Path(__file__).parent / "web"


class GroundServices:
    """只管理本次启动的图片接收器和 MediaMTX。"""
    def __init__(self, local, environment):
        self.local, self.env = local, environment
        self.receiver = None
        self.thread = None
        self.process = None
        self.log = None

    @staticmethod
    def check_port(port):
        with socket.socket() as probe:
            probe.bind(("0.0.0.0", port))

    @staticmethod
    def _existing_media_pid(rtsp_port, webrtc_port):
        """在 Windows 上识别同时监听两个视频端口的同一进程。"""
        if os.name != "nt":
            return None
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"], capture_output=True,
                text=False, timeout=3, check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        owners = {}
        raw_output = result.stdout or b""
        if isinstance(raw_output, bytes):
            # Windows netstat may use the system code page (GBK/CP936),
            # while newer terminals may emit UTF-8. Decode defensively so a
            # console encoding mismatch cannot abort video startup.
            output = None
            for encoding in ("utf-8-sig", "gbk", "mbcs"):
                try:
                    output = raw_output.decode(encoding)
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            output = output if output is not None else raw_output.decode("utf-8", errors="replace")
        else:
            output = str(raw_output)
        for line in output.splitlines():
            fields = line.split()
            if len(fields) < 5 or fields[0].upper() != "TCP" or fields[3].upper() != "LISTENING":
                continue
            try:
                port = int(fields[1].rsplit(":", 1)[-1])
                pid = int(fields[4])
            except ValueError:
                continue
            if port in (rtsp_port, webrtc_port):
                owners.setdefault(port, set()).add(pid)
        shared = owners.get(rtsp_port, set()) & owners.get(webrtc_port, set())
        return next(iter(shared)) if len(shared) == 1 else None

    def start(self):
        self.check_port(self.local["image_port"])
        spec = importlib.util.spec_from_file_location("competition_ground_receiver", ROOT / "src/su17_image_transfer/ground/ground_image_receiver.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.receiver = module.GroundImageReceiver("0.0.0.0", self.local["image_port"],
                                                  Path(self.env["COMPETITION_IMAGE_ROOT"]), self.env["COMPETITION_TCP_TOKEN"],
                                                  allowed_uav_ids=[self.local["uav_id"]])
        errors = []
        def receive():
            try:
                self.receiver.serve_forever()
            except Exception as error:
                errors.append(error)
        self.thread = threading.Thread(target=receive, name="ground-image-receiver", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 3
        while self.receiver.server is None and not errors and time.monotonic() < deadline:
            time.sleep(.02)
        if errors or self.receiver.server is None:
            raise RuntimeError("图片接收器启动失败：" + str(errors[0] if errors else "监听超时"))
        if self.local["video_rtsp_source"]:
            try:
                self.start_media()
            except Exception as error:
                # 视频转发异常不应阻止任务、遥测和图片回传服务上线。
                print("视频转发未启动：%s；其他地面服务继续运行。" % error, flush=True)

    def start_media(self):
        media = Path(self.env.get("COMPETITION_MEDIAMTX_DIRECTORY", str(ROOT / "third_party/mediamtx")))
        executable = media / ("mediamtx.exe" if os.name == "nt" else "mediamtx")
        if not executable.exists():
            raise RuntimeError("缺少 MediaMTX，请先运行地面安装脚本")
        runtime = ROOT / "ground_runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        target = runtime / ("mediamtx_uav%d.yml" % self.local["uav_id"])
        template = (media / "mediamtx.template.yml").read_text(encoding="utf-8-sig")
        desired = (template.replace("__UAV_PATH__", "uav%d" % self.local["uav_id"])
                   .replace("__RTSP_SOURCE__", self.local["video_rtsp_source"])
                   .replace("__WEBRTC_PORT__", str(self.local["video_webrtc_port"])))
        existing_matches = target.exists() and target.read_text(encoding="utf-8") == desired
        existing_pid = self._existing_media_pid(8554, self.local["video_webrtc_port"])
        if existing_pid is not None:
            if not existing_matches:
                raise RuntimeError("视频端口已被进程 %s 占用，且当前配置与本终端不一致；请先关闭旧视频服务" % existing_pid)
            print("复用已运行的 MediaMTX（PID %s），不重复启动视频服务。" % existing_pid, flush=True)
            return
        self.check_port(8554)
        self.check_port(self.local["video_webrtc_port"])
        target.write_text(desired, encoding="utf-8")
        logs = ROOT / "ground_logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.log = (logs / "mediamtx.log").open("ab")
        self.process = subprocess.Popen([str(executable), str(target)], cwd=str(media),
                                        stdout=self.log, stderr=self.log,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        time.sleep(.2)
        if self.process.poll() is not None:
            raise RuntimeError("MediaMTX 启动失败，请查看 ground_logs/mediamtx.log")

    def stop(self):
        if self.receiver:
            self.receiver.stop()
        if self.thread:
            self.thread.join(timeout=2)
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.log:
            self.log.close()


class GroundEntry:
    def __init__(self, environment=None, app_factory=None, services_factory=GroundServices, programs_factory=ProgramManager):
        self.env = dict(os.environ if environment is None else environment)
        self.store = FleetStore(self.env.get("COMPETITION_FLEET_CONFIG", str(ROOT / "config/fleet.json")))
        self.app_factory = app_factory
        self.services_factory = services_factory
        self.active = None
        self.programs = programs_factory(ROOT, self.env)
        self.worker = None
        self.stop_event = asyncio.Event()
        self.selection = None
        self.error = ""
        self.shutdown_callback = None
        self.shutting_down = False
        self.shutdown_lock = asyncio.Lock()

        @asynccontextmanager
        async def lifespan(app):
            yield
            self.stop_event.set()
            self.programs.stop()
            if self.worker:
                await self.worker

        app = FastAPI(lifespan=lifespan)
        self.entry = app

        @app.get("/", response_class=HTMLResponse)
        def index():
            return HTMLResponse((WEB / "index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

        @app.get("/map-assets/{name}")
        def asset(name: str):
            media = {"terrain_basemap.js": "application/javascript", "competition_esri_20170724.jpg": "image/jpeg"}
            if name not in media or not (WEB / name).is_file():
                raise HTTPException(status_code=404, detail="地图资源尚未部署，请更新地面端代码")
            return FileResponse(WEB / name, media_type=media[name], headers={"Cache-Control": "no-cache"})

        @app.get("/api/v1/programs")
        def programs():
            result = self.programs.snapshot()
            if self.error:
                result['programs']['ground'].update(state='error', detail=self.error)
            return result

        @app.post("/api/v1/programs/reconnect")
        def reconnect_programs(request: Request):
            # 此操作会打开本机密码输入窗口，只允许本机浏览器触发。
            from urllib.parse import urlparse
            if request.client and request.client.host not in ('127.0.0.1', '::1', 'testclient'):
                raise HTTPException(status_code=403, detail='请在本机网页完成 SSH 授权')
            origin = request.headers.get('origin', '')
            if origin and urlparse(origin).hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise HTTPException(status_code=403, detail='只允许本机网页操作')
            try:
                return self.programs.reconnect()
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

        @app.post("/api/v1/programs/{key}/stop")
        async def stop_program(key: str, request: Request):
            from urllib.parse import urlparse
            if request.client and request.client.host not in ('127.0.0.1', '::1', 'testclient'):
                raise HTTPException(status_code=403, detail='只能在本机网页停止程序')
            origin = request.headers.get('origin', '')
            if origin and urlparse(origin).hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise HTTPException(status_code=403, detail='只允许本机网页操作')
            if key == 'ground':
                if not self.shutdown_callback:
                    raise HTTPException(status_code=409, detail='当前启动方式不支持网页退出，请运行 tools/stop_ground.ps1')
                async with self.shutdown_lock:
                    if not self.shutting_down:
                        if os.name == 'nt':
                            # 独立检查进程退出，即使后端清理卡住也能处理已核验的残留。
                            cleanup = subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                              str(ROOT / 'tools/stop_ground.ps1'), '-BackendPid', str(os.getpid()),
                                              '-WebPort', self.env.get('COMPETITION_PORT', '8000'), '-SkipRequest', '-Quiet'],
                                             cwd=str(ROOT), creationflags=subprocess.CREATE_NO_WINDOW,
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                            # 等独立助手记录进程树后才退出，避免助手启动较慢时漏掉孤儿子进程。
                            try:
                                ready = await asyncio.wait_for(asyncio.to_thread(cleanup.stdout.readline), timeout=10)
                            except asyncio.TimeoutError as error:
                                raise HTTPException(status_code=503, detail='退出核验助手尚未就绪，请运行 stop_ground.cmd 查看结果') from error
                            finally:
                                if cleanup.poll() is not None:
                                    cleanup.stdout.close()
                            if ready.strip() != b'GROUND_STOP_READY':
                                raise HTTPException(status_code=503, detail='退出核验助手启动失败，请运行 stop_ground.cmd 查看结果')
                            cleanup.stdout.close()
                        self.shutting_down = True
                        asyncio.get_running_loop().call_later(.5, self.shutdown_callback)
                        print('正在关闭地面竞赛服务；独立清理程序将核验进程退出。', flush=True)
                return dict(state='stopping', detail='正在停止地面服务并核验进程；结果写入 ground_logs/ground_stop.log')
            try:
                return await asyncio.to_thread(self.programs.stop_program, key)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            except RuntimeError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error

        @app.get("/api/v1/programs/{key}/logs")
        def program_logs(key: str, offset: int = 0):
            try:
                return self.programs.read_log(key, offset)
            except ValueError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error

        @app.get("/api/v1/operator")
        def operator():
            return self.operator_status()

        @app.post("/api/v1/operator")
        async def choose(payload: dict = Body(...)):
            uid = payload.get("ground_terminal_id")
            if type(uid) is not int or uid not in range(1, 7) or type(payload.get("task_publisher")) is not bool:
                raise HTTPException(status_code=422, detail="请选择 1～6 号地面终端并填写发布角色")
            try:
                local, _ = ground_environment(self.store.read(), uid)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            # 机型来自固定机队配置，网页不再允许临时改机型。
            selection = {"ground_terminal_id": uid, "model": local["model"],
                         "task_publisher": bool(payload["task_publisher"])}
            if local["web_port"] != int(self.env.get("COMPETITION_PORT", "8000")):
                raise HTTPException(status_code=409, detail="该终端配置了不同的网页端口，请使用 -WebPort %s 重启入口" % local["web_port"])
            if self.worker and not self.worker.done():
                if selection != self.selection:
                    raise HTTPException(status_code=409, detail="终端正在启动，请等待完成后再操作")
                return self.operator_status()
            self.selection, self.error = selection, ""
            self.programs.select(local)
            self.worker = asyncio.create_task(self.activate(selection))
            return self.operator_status()

        @app.get("/health")
        @app.get("/api/v1/status")
        def status():
            return self.startup_snapshot()

        @app.get("/api/v1/recording/status")
        def recording():
            return {"state": "idle"}

        @app.get("/api/v1/video-sources")
        def video():
            return {"sources": {}}

        @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
        def not_ready(path: str):
            raise HTTPException(status_code=409, detail=self.error or "请先选择终端并等待本机服务启动")

        @app.websocket("/ws/status")
        async def websocket(ws: WebSocket):
            await ws.accept()
            try:
                while True:
                    if self.active is None:
                        snapshot = self.startup_snapshot()
                    else:
                        endpoint = next(r.endpoint for r in self.active.routes if r.path == "/api/v1/status")
                        snapshot = await asyncio.to_thread(endpoint)
                    await ws.send_json(snapshot)
                    await asyncio.sleep(.5)
            except WebSocketDisconnect:
                pass

    def operator_status(self):
        selection = self.selection or {"ground_terminal_id": None, "model": "p600", "task_publisher": False}
        return {**selection, "configured": False, "selection_required": True, "profile_matches": True,
                "selection_pending": bool(self.worker and not self.worker.done()), "selection_error": self.error,
                "can_edit_fleet": False, "bootstrap": True, "profiles": PROFILES}

    def startup_snapshot(self):
        return {"ok": True, "adapter": "awaiting_selection", "live_mode": True,
                "server_time": time.time(), "mission": None, "telemetry": {}, "active_uav_ids": [],
                "recordable_uav_ids": [], "operator": self.operator_status(),
                "fleet": {"config": self.store.read()}, "traffic": {"by_uav": {}}, "pointcloud": {}}

    async def activate(self, selection):
        services = None
        try:
            if not self.env.get("COMPETITION_TCP_TOKEN") or not self.env.get("COMPETITION_PEER_TOKEN"):
                raise RuntimeError("缺少机地认证配置，请使用 tools/start_ground.ps1 启动")
            local, overrides = ground_environment(self.store.read(), selection["ground_terminal_id"])
            env = {**self.env, **overrides, "COMPETITION_GROUND_TERMINAL_ID": str(selection["ground_terminal_id"]),
                   "COMPETITION_FLEET_CONFIG": str(self.store.path),
                   "COMPETITION_ADAPTER": "distributed", "COMPETITION_ASYNC_OPERATOR_SELECTION": "1",
                   "COMPETITION_TASK_PUBLISHER": "", "COMPETITION_IMAGE_ROOT": str(ROOT / "received_images")}
            if self.app_factory is None:
                from .api import create_app
                factory = create_app
            else:
                factory = self.app_factory
            runtime = await asyncio.to_thread(factory, env)
            services = self.services_factory(local, env)
            try:
                await asyncio.to_thread(services.start)
            except Exception as error:
                self.error = "图片或视频服务启动失败：" + str(error)
                print(self.error + "；网页与任务服务继续启动。", flush=True)
            async with runtime.router.lifespan_context(runtime):
                self.active = runtime
                endpoint = next(r.endpoint for r in runtime.routes if r.path == "/api/v1/operator" and "POST" in r.methods)
                await asyncio.to_thread(endpoint, selection)
                print("本机服务已启动：地面终端 %s / UAV%s；机型和机地配对来自固定配置。" % (selection["ground_terminal_id"], local["uav_id"]), flush=True)
                await self.stop_event.wait()
        except Exception as error:
            self.error = "本机服务启动失败：" + str(error)
            print(self.error, flush=True)
        finally:
            self.active = None
            if services:
                await asyncio.to_thread(services.stop)

    async def __call__(self, scope, receive, send):
        # 状态流可跨越本机服务启动过程，避免关闭/重开网页。
        entry_path = scope.get("path", "").startswith(("/map-assets/", "/api/v1/programs"))
        target = self.entry if scope["type"] in ("lifespan", "websocket") or self.active is None or entry_path else self.active
        if target is self.active and scope.get('path') == '/api/v1/operator' and scope.get('method') == 'POST':
            # 只有成功确认本地终端才请求启动，不响应其他终端的配置同步报文。
            async def after_selection(message):
                if message['type'] == 'http.response.start' and message['status'] == 200 and self.selection:
                    local, _ = ground_environment(self.store.read(), self.selection['ground_terminal_id'])
                    self.programs.select(local)
                await send(message)
            await target(scope, receive, after_selection)
        else:
            await target(scope, receive, send)


def run():
    import uvicorn
    logs = ROOT / "ground_logs" / "programs"
    logs.mkdir(parents=True, exist_ok=True)
    secrets = [v for k, v in os.environ.items() if 'TOKEN' in k and v]
    sys.stdout = ConsoleTee(sys.stdout, logs / 'ground.log', secrets)
    sys.stderr = ConsoleTee(sys.stderr, logs / 'ground.log', secrets)
    print("地面竞赛程序正在启动；请选择本地地面终端。", flush=True)
    for filename in ('terrain_basemap.js', 'competition_esri_20170724.jpg'):
        if not (WEB / filename).is_file():
            print("底图文件缺失，请更新地面端：" + filename, flush=True)
    entry = GroundEntry()
    server = uvicorn.Server(uvicorn.Config(entry, host=os.environ.get("COMPETITION_HOST", "0.0.0.0"),
                                         port=int(os.environ.get("COMPETITION_PORT", "8000")),
                                         log_level="warning", access_log=False, timeout_graceful_shutdown=8))
    def shutdown():
        entry.programs.stop()
        entry.stop_event.set()
        server.should_exit = True
    entry.shutdown_callback = shutdown
    server.run()


if __name__ == "__main__":
    run()
