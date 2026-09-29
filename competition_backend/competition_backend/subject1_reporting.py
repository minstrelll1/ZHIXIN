"""科目一：校验、冻结 UTF-8 JSON 文件，并手动提交给赛事接口。"""
from datetime import datetime
import hashlib
import http.client
import importlib.util
import json
import math
from pathlib import Path
import re
import threading
import urllib.error
import urllib.request
import uuid

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

ENDPOINT = 'http://192.168.1.199:8001/api/v1/public/recognition-results'
TEAM_NAME = '北方自控智群队'
MAX_BYTES = 16 * 1024 * 1024


def validate_document(document):
    def require(condition, message):
        if not condition:
            raise ValueError(message)
    def text(value):
        return isinstance(value, str) and bool(value.strip())
    def number(value):
        try:
            return type(value) in (float, int) and math.isfinite(value)
        except OverflowError:
            return False
    def coordinate(value):
        return (isinstance(value, list) and len(value) == 2 and all(number(v) for v in value)
                and -180 <= value[0] <= 180 and -90 <= value[1] <= 90)
    def instant(value):
        try:
            require(isinstance(value, str) and 'T' in value, '时间须使用 ISO 8601 格式')
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
            require(result.tzinfo is not None, '时间戳须包含时区，例如 +08:00 或 Z')
            return result
        except (ValueError, TypeError) as error:
            raise ValueError('时间戳须为带时区的 ISO 8601 时间：%s' % value) from error
    require(isinstance(document, dict) and document.get('type') == 'FeatureCollection', '根 type 须为 FeatureCollection')
    require(text(document.get('name')) and document['name'].strip() != 'XXX(参赛队名)', '请填写正式参赛队名')
    if 'crs' in document:
        require(document['crs'] == {'type': 'lonlat', 'properties': {'lonlat': 'EPSG:4326'}}, '坐标系须为 EPSG:4326')
    features = document.get('features')
    require(isinstance(features, list) and len(features) > 0, '没有有效目标结果，不能上报空文件')
    seen = set()
    counts = {'fixed': 0, 'moving': 0}
    for index, feature in enumerate(features, 1):
        label = '第 %d 个目标：' % index
        try:
            require(isinstance(feature, dict) and feature.get('type') == 'Feature', 'type 须为 Feature')
            identifier = feature.get('id')
            require(text(identifier) and identifier not in seen, 'id 须是非空且唯一的字符串')
            seen.add(identifier)
            geometry, props = feature.get('geometry'), feature.get('properties')
            require(isinstance(geometry, dict) and isinstance(props, dict), '缺少 geometry 或 properties')
            require(props.get('targetCategory') in ('固定', '移动'), 'targetCategory 须为固定或移动')
            for key in ('targetType', 'targetModel'):
                require(text(props.get(key)), '缺少 ' + key)
            if 'confidence' in props:
                value = props['confidence']
                require(number(value) and 0 <= value <= 1, 'confidence 须为 0～1 的数值')
            if 'imagePath' in props:
                require(text(props['imagePath']), 'imagePath 如填写须为非空字符串')
            coords = geometry.get('coordinates')
            if props['targetCategory'] == '固定':
                require(geometry.get('type') == 'Point' and coordinate(coords), '固定目标须为 Point，坐标为 [经度, 纬度]')
                instant(props.get('timestamp'))
                counts['fixed'] += 1
            else:
                require(geometry.get('type') == 'LineString' and isinstance(coords, list)
                        and len(coords) >= 2 and all(coordinate(p) for p in coords), '移动目标须有至少两个 [经度, 纬度] 轨迹点')
                points = props.get('trackPoints')
                require(isinstance(points, list) and len(points) == len(coords), 'trackPoints 须与轨迹坐标逐点对应')
                times = []
                for point, pos in zip(points, coords):
                    require(isinstance(point, dict) and point.get('coordinates') == pos, 'trackPoints 坐标与 geometry 不一致')
                    times.append(instant(point.get('timestamp')))
                require(times == sorted(times), '轨迹时间须按先后顺序排列')
                require(instant(props.get('trackStartTime')) == times[0]
                        and instant(props.get('trackEndTime')) == times[-1], '轨迹起止时间须与首尾轨迹点一致')
                counts['moving'] += 1
        except ValueError as error:
            raise ValueError(label + str(error)) from error
    return dict(target_count=len(features), **counts)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Subject1Reporter:
    def __init__(self, image_root):
        self.image_root = Path(image_root).resolve()
        self.root = self.image_root / 'subject1_reports'
        self.lock = threading.Lock()

    def missions(self):
        # 图片合并完成后，即使发布端本机尚无识别结果，也能找到其他终端的任务。
        names = set()
        for path in self.image_root.glob('UAV*/*/*.json'):
            try:
                data = json.loads(path.read_text(encoding='utf-8-sig'))
                name = data.get('mission_id', '')
                if isinstance(name, str) and name.startswith('subject1') and re.fullmatch(r'[\w.-]+', name):
                    names.add(name)
            except (OSError, ValueError, AttributeError):
                continue
        return sorted(names, reverse=True)

    def build(self, mission_id, team_name):
        if mission_id not in self.missions():
            raise ValueError('未找到此科目一任务的已回传结果；请检查图片同步是否完成')
        source = Path(__file__).resolve().parents[2] / 'src/su17_image_transfer/src/su17_image_transfer/submission.py'
        spec = importlib.util.spec_from_file_location('subject1_export', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        path = module.update_subject1_submission(self.image_root, mission_id, team_name)
        return json.loads(path.read_text(encoding='utf-8'))

    def prepare(self, document):
        summary = validate_document(document)
        try:
            raw = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
        except (TypeError, ValueError) as error:
            raise ValueError('JSON 含不支持的值') from error
        if len(raw) > MAX_BYTES:
            raise ValueError('JSON 文件超过 16 MB，请分批整理结果')
        digest = hashlib.sha256(raw).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (digest + '.json')
        temporary = self.root / (uuid.uuid4().hex + '.part')
        temporary.write_bytes(raw)
        temporary.replace(path)
        metadata = document.get('metadata')
        skipped = metadata.get('indoorTargets', []) if isinstance(metadata, dict) else []
        return dict(draft_id=digest, team_name=document['name'], filename='target-submission.json',
                    download_url='/api/v1/subject1/report/files/' + digest, endpoint=ENDPOINT,
                    skipped_count=len(skipped) if isinstance(skipped, list) else 0,
                    image_count=sum(bool(f['properties'].get('imagePath')) for f in document['features']), **summary)

    def draft(self, digest):
        if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('结果文件标识无效，请重新生成并核对')
        path = self.root / (digest + '.json')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('结果文件不存在或已变化，请重新生成并核对')
        return path

    def submit(self, digest):
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('已有结果文件正在上报，请等待回执，勿重复点击')
        try:
            raw = self.draft(digest).read_bytes()
            validate_document(json.loads(raw.decode('utf-8')))
            boundary = '----ZhiXin' + uuid.uuid4().hex
            body = ('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="target-submission.json"\r\n'
                    'Content-Type: application/json; charset=utf-8\r\n\r\n').encode('ascii') + raw + ('\r\n--' + boundary + '--\r\n').encode('ascii')
            request = urllib.request.Request(ENDPOINT, data=body, method='POST', headers={
                'Content-Type': 'multipart/form-data; boundary=' + boundary, 'Accept': 'application/json'})
            # 局域网赛事接口不使用电脑上的 HTTP 代理，也不携带机地通信令牌。
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            receipt = dict(draft_id=digest, endpoint=ENDPOINT, sent_at=datetime.now().astimezone().isoformat(),
                           http_status=None, state='unknown', detail='尚未获得赛事回执')
            try:
                try:
                    response = opener.open(request, timeout=20)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    code = int(response.code)
                    reply = response.read(65537)
                receipt.update(http_status=code, response=reply[:65536].decode('utf-8', errors='replace'), response_truncated=len(reply)>65536)
                receipt.update(state='http_received' if 200 <= code < 300 else 'rejected',
                               detail='接口已响应 HTTP %s，请核对下方赛事回执；是否受理以赛事回执为准' % code)
            except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
                receipt.update(detail='未取得明确回执：%s。未自动重试；请先向赛事方核实是否收到，再决定是否重报。' % error)
            finally:
                target = self.root / ('receipt-' + uuid.uuid4().hex + '.json')
                target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
            return receipt
        finally:
            self.lock.release()


def reporting_router(image_root, require_publisher):
    reporter = Subject1Reporter(image_root)
    router = APIRouter(prefix='/api/v1/subject1/report', tags=['科目一结果上报'])
    def permitted(request):
        from urllib.parse import urlparse
        if request.client and request.client.host not in ('127.0.0.1', '::1', 'testclient'):
            raise HTTPException(status_code=403, detail='请在任务发布端本机操作赛事上报')
        origin = request.headers.get('origin')
        if origin and urlparse(origin).hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise HTTPException(status_code=403, detail='不允许其他网站调用赛事上报')
        require_publisher()
    @router.get('')
    def status(request: Request):
        permitted(request)
        return dict(team_name=TEAM_NAME, endpoint=ENDPOINT, missions=reporter.missions())
    @router.post('/prepare')
    def prepare(request: Request, payload: dict = Body(...)):
        permitted(request)
        try:
            team_name = payload.get('team_name', TEAM_NAME)
            if not isinstance(team_name, str) or not team_name.strip():
                raise ValueError('参赛队名不能为空')
            document = payload.get('document')
            if document is None:
                document = reporter.build(payload.get('mission_id'), team_name.strip())
            if not isinstance(document, dict):
                raise ValueError('JSON 顶层须是对象')
            document['name'] = team_name.strip()
            return reporter.prepare(document)
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    @router.get('/files/{digest}')
    def download(digest: str, request: Request):
        permitted(request)
        try:
            return FileResponse(reporter.draft(digest), filename='target-submission.json', media_type='application/json')
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
    @router.post('/submit')
    def submit(request: Request, payload: dict = Body(...)):
        permitted(request)
        if payload.get('confirmed') is not True:
            raise HTTPException(status_code=422, detail='请先核对结果文件并确认上报')
        try:
            return reporter.submit(payload.get('draft_id'))
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    return router
