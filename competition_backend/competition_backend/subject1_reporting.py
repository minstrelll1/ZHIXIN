"""科目一：校验、冻结 UTF-8 JSON 文件，支持人工与赛时定时上报。"""
from datetime import datetime
import copy
import hashlib
import http.client
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse
from competition_shared.subject1_dedup import consolidate
from competition_shared.target_quality import category_fields
from competition_shared.submission_format import format_submission, submission_content_hash

ENDPOINT = 'http://192.168.1.199:8001/api/v1/public/recognition-results'
TEAM_NAME = '北方自控智群队'
MAX_BYTES = 16 * 1024 * 1024


def validate_document(document, *, allow_excess=False):
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
    require(allow_excess or len(features) <= 16, '赛事结果最多包含 16 个目标，请先核对并整理')
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


def _write_intermediate(path, document):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Subject1Reporter:
    _draft_file_lock = threading.Lock()

    def __init__(self, image_root, audit=None, publisher_dedup=False):
        self.audit = audit
        self.image_root = Path(image_root).resolve()
        self.publisher_dedup = bool(publisher_dedup)
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
        path = module.update_subject1_submission(self.image_root, mission_id, team_name,
                                                 publisher_dedup=self.publisher_dedup)
        document = json.loads(path.read_text(encoding='utf-8'))
        if self.audit:
            self.audit.record('科目一赛事结果已生成', mission_id=mission_id,
                              package_file=str(path), target_count=len(document.get('features', [])),
                              image_count=sum(bool(f.get('properties', {}).get('imagePath'))
                                              for f in document.get('features', [])))
        return document

    def _local_result_path(self, document):
        # 操作员可能把本机生成的 target-submission.json 再从文件导入。
        # 它已按 UAV 来源去重，不能在缺少来源字段的副本上再次合并。
        def content(value):
            value = copy.deepcopy(value)
            if isinstance(value.get('metadata'), dict):
                value['metadata'].pop('createdAt', None)
            return value
        paths = list(self.image_root.glob('subject1_submissions/*/target-submission.json'))
        paths.extend(path for path in self.root.glob('*.json')
                     if re.fullmatch('[a-f0-9]{64}\.json', path.name))
        for path in paths:
            try:
                if content(json.loads(path.read_text(encoding='utf-8'))) == content(document):
                    return path
            except (OSError, ValueError):
                continue
        return None

    def prepare(self, document, *, already_deduplicated=False):
        # 本机生成的结果已经按组内两两距离去重；重复整理会丢失被合并成员的约束。
        # 人工导入的原始赛事 JSON 则在此完成去重和置信度排序。
        local_path = self._local_result_path(document)
        already_deduplicated = already_deduplicated or (local_path is not None and
                               (self.publisher_dedup or local_path.parent == self.root))
        validate_document(document, allow_excess=not already_deduplicated)
        original = copy.deepcopy(document)
        document = copy.deepcopy(original)
        for feature in document['features']:
            props = feature['properties']
            kind, model = category_fields(dict(target_type=props['targetType'], target_model=props['targetModel']))
            props['targetType'], props['targetModel'] = kind, model
        if already_deduplicated:
            decisions = {'raw_count': len(document['features']),
                         'result_count': len(document['features']), 'merged': [], 'omitted': []}
        else:
            document, decisions = consolidate(document)
        document, format_audit = format_submission(document)
        if not already_deduplicated:
            format_audit['deduplication'] = {key: decisions.get(key) for key in
                ('raw_count', 'deduplicated_count', 'result_count', 'backfilled_count', 'backfilled',
                 'selection', 'static_quality_order', 'moving_quality_order')}
        if local_path is not None:
            try:
                audit_path = (local_path.with_suffix('.format.json') if local_path.parent == self.root
                              else local_path.with_name('submission-format.json'))
                local_audit = json.loads(audit_path.read_text(encoding='utf-8'))
                if local_audit.get('document_sha256') != submission_content_hash(original):
                    raise ValueError('审计文件与本次成果版本不一致')
                # 本机成果再次冻结时，保留原始目标 ID 的映射及未纳入原因。
                if (local_audit.get('id_mapping') and
                        [entry['submission_id'] for entry in local_audit['id_mapping']] ==
                        [entry['source_id'] for entry in format_audit['id_mapping']]):
                    format_audit['id_mapping'] = local_audit['id_mapping']
                format_audit['excluded_targets'] = local_audit.get('excluded_targets', format_audit['excluded_targets'])
                for key in ('target_time', 'category_records', 'motion_classification', 'deduplication'):
                    if isinstance(local_audit.get(key), (dict, list)):
                        format_audit[key] = local_audit[key]
            except (OSError, ValueError, TypeError, KeyError):
                pass
        summary = validate_document(document)
        try:
            raw = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
        except (TypeError, ValueError) as error:
            raise ValueError('JSON 含不支持的值') from error
        if len(raw) > MAX_BYTES:
            raise ValueError('JSON 文件超过 16 MB，请分批整理结果')
        digest = hashlib.sha256(raw).hexdigest()
        with self._draft_file_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / (digest + '.json')
            if path.is_file():
                if path.read_bytes() != raw:
                    raise ValueError('已有同名结果文件内容不一致，请检查文件')
            else:
                temporary = self.root / (uuid.uuid4().hex + '.part')
                try:
                    temporary.write_bytes(raw)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            _write_intermediate(self.root / (digest + '.format.json'), format_audit)
            if decisions['merged'] or decisions['omitted'] or original != document:
                _write_intermediate(self.root / (digest + '.dedup.json'), decisions)
                _write_intermediate(self.root / (digest + '.raw.json'), original)
        if self.audit:
            self.audit.record('赛事结果文件已整理', draft_id=digest, file=str(path),
                              merged_count=len(decisions['merged']), omitted_count=len(decisions['omitted']),
                              backfilled_count=format_audit.get('deduplication', {}).get('backfilled_count', 0),
                              selection=format_audit.get('deduplication', {}).get('selection', {}),
                              skipped_count=len(format_audit['excluded_targets']),
                              reclassified_static_count=sum(bool(row.get('converted_to_static')) for row in format_audit.get('motion_classification', [])),
                              format_audit_file=str(self.root / (digest + '.format.json')),
                              time_corrected_count=format_audit.get('target_time', {}).get('corrected_count', 0),
                              time_pending_count=format_audit.get('target_time', {}).get('pending_count', 0),
                              time_unverified_legacy_count=format_audit.get('target_time', {}).get('unverified_legacy_count', 0),
                              **summary)
        skipped = format_audit['excluded_targets']
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

    def submit(self, digest, *, scheduled=False):
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
            if self.audit:
                self.audit.record('赛事上报开始', draft_id=digest, endpoint=ENDPOINT, bytes=len(raw))
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
                suffix = ('其余定时上报仍将按计划发送，请核对最终赛事回执。' if scheduled
                          else '未自动重试；请先向赛事方核实是否收到，再决定是否重报。')
                receipt.update(detail='未取得明确回执：%s。%s' % (error, suffix))
            finally:
                target = self.root / ('receipt-' + uuid.uuid4().hex + '.json')
                target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
                if self.audit:
                    self.audit.record('赛事上报回执', receipt_file=str(target), receipt=receipt)
            return receipt
        finally:
            self.lock.release()


class AutoSubject1Reporter:
    """只在任务发布端按当前比赛计时上报本次科目一任务。"""

    TRIGGER_SECONDS = 24 * 60
    INTERVAL_SECONDS = 5

    def __init__(self, image_root, competition_clock, is_publisher, audit=None,
                 publisher_dedup=False, telemetry_provider=None, takeoff_provider=None):
        self.telemetry_provider = telemetry_provider or (lambda: {})
        self.takeoff_provider = takeoff_provider or (lambda: {})
        self.image_root = image_root
        self.publisher_dedup = bool(publisher_dedup)
        self.clock = competition_clock
        self.is_publisher = is_publisher
        self.audit = audit
        self._lock = threading.RLock()
        self._prepare_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._session_id = None
        self._mission_id = None
        self._frozen_mission_id = None
        self._started = 0
        self._finished = 0
        self._last_result = None
        from .return_report_schedule import ReturnReportSchedule
        self._schedule = ReturnReportSchedule()
        self._checksums = {}

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name='subject1-auto-report', daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _run(self):
        while not self._stop.wait(0.2):
            try:
                self.tick()
            except Exception as error:
                if self.audit:
                    self.audit.record('科目一自动上报计时异常', error=str(error))

    def _sync_session(self, state):
        session = state.get('session_id') if state.get('running') and state.get('is_authority') else None
        if session != self._session_id:
            self._session_id = session
            self._mission_id = None
            self._frozen_mission_id = None
            self._started = self._finished = 0
            self._last_result = None
            from .return_report_schedule import ReturnReportSchedule
            self._schedule = ReturnReportSchedule()
            self._checksums = {}

    def note_mission(self, mission_id, uavs=None):
        state = self.clock.snapshot()
        if not (state.get('running') and state.get('is_authority') and self.is_publisher()):
            return
        if not isinstance(mission_id, str) or not mission_id.startswith('subject1-'):
            return
        with self._lock:
            self._sync_session(state)
            if self._started == 0:
                if self._mission_id != mission_id:
                    from .return_report_schedule import ReturnReportSchedule
                    self._schedule = ReturnReportSchedule()
                self._mission_id = mission_id
                self._checksums = {str(uid): item.get('assignment_checksum')
                                   for uid, item in (uavs or {}).items()}
        if self.audit:
            self.audit.record('科目一自动上报已关联本次任务', mission_id=mission_id,
                              competition_session=state['session_id'])

    def tick(self, now=None):
        state = self.clock.snapshot()
        telemetry = self.telemetry_provider()
        takeoff = self.takeoff_provider()
        with self._lock:
            self._sync_session(state)
            if (not state.get('is_authority') or not self.is_publisher()
                    or not self._mission_id or self._stop.is_set()):
                return False
            now = time.monotonic() if now is None else now
            mission_id = self._frozen_mission_id or self._mission_id
            if takeoff.get('mission_id') == mission_id and takeoff.get('uav_ids'):
                if self._schedule.set_participants(takeoff['uav_ids']) and self.audit:
                    self.audit.record('科目一自动上报已固定起飞参与名单', mission_id=mission_id,
                                      uav_ids=sorted(self._schedule.participants),
                                      competition_session=self._session_id)
            added = self._schedule.observe(telemetry, mission_id, self._session_id,
                                           self._checksums, now)
            if added and self.audit:
                self.audit.record('成功返航标记已汇集', mission_id=mission_id,
                                  uav_ids=added, returned_uav_ids=sorted(self._schedule.returned),
                                  participant_uav_ids=sorted(self._schedule.participants),
                                  pending_return_uav_ids=sorted(self._schedule.participants.difference(self._schedule.returned)))
            previous_strategy = self._schedule.selected_strategy
            reasons = self._schedule.due(now, float(state.get('elapsed_seconds', 0)))
            if self._schedule.selected_strategy != previous_strategy and self.audit:
                self.audit.record('科目一自动上报策略已选定', mission_id=mission_id,
                                  competition_session=self._session_id,
                                  strategy=self._schedule.selected_strategy,
                                  competition_elapsed_seconds=state.get('elapsed_seconds'))
            if not reasons:
                return False
            self._frozen_mission_id = mission_id
            self._started += 1
            number = self._started
            session_id = self._session_id
        if self.audit:
            self.audit.record('科目一自动上报尝试开始', mission_id=mission_id,
                              competition_session=session_id, attempt=number, triggers=reasons,
                              competition_elapsed_seconds=state.get('elapsed_seconds'))
        threading.Thread(target=self._submit_once, args=(session_id, mission_id, number, reasons),
                         name='subject1-auto-report-%d' % number, daemon=True).start()
        return True

    def _submit_once(self, session_id, mission_id, number, reasons=None):
        # 每轮重新整理最新已回传目标；请求超时不会阻塞后续节拍。
        try:
            reporter = Subject1Reporter(self.image_root, audit=self.audit,
                                        publisher_dedup=self.publisher_dedup)
            with self._prepare_lock:
                document = reporter.build(mission_id, TEAM_NAME)
                prepared = reporter.prepare(document, already_deduplicated=self.publisher_dedup)
            current = self.clock.snapshot()
            if (self._stop.is_set() or current.get('session_id') != session_id
                    or not current.get('is_authority') or not self.is_publisher()):
                raise RuntimeError('比赛会话或发布端身份已变化，本次不再发送旧任务结果')
            if (reasons == ['competition_time']
                    and not 1440 <= float(current.get('elapsed_seconds', 0)) < 1530):
                raise RuntimeError('比赛时间已离开24分至25分30秒窗口，取消尚未发送的计时上报')
            receipt = reporter.submit(prepared['draft_id'], scheduled=True)
            outcome = dict(attempt=number, mission_id=mission_id, state=receipt['state'],
                           http_status=receipt['http_status'], detail=receipt['detail'],
                           draft_id=prepared['draft_id'])
        except Exception as error:
            outcome = dict(attempt=number, mission_id=mission_id, state='failed',
                           detail='结果整理或上报失败：%s' % error)
        with self._lock:
            if session_id == self._session_id:
                self._finished += 1
                if self._last_result is None or number >= self._last_result['attempt']:
                    self._last_result = outcome
        if self.audit:
            self.audit.record('科目一自动上报尝试结束', competition_session=session_id, **outcome)
        print('科目一自动上报第 %d 次：%s' % (number, outcome['detail']), flush=True)

    def snapshot(self):
        with self._lock:
            state = self.clock.snapshot()
            self._sync_session(state)
            return dict(enabled=bool(state.get('is_authority') and self.is_publisher()),
                        trigger_elapsed_seconds=self.TRIGGER_SECONDS,
                        interval_seconds=self.INTERVAL_SECONDS, **self._schedule.snapshot(),
                        mission_id=self._frozen_mission_id or self._mission_id,
                        attempts_started=self._started, attempts_finished=self._finished,
                        last_result=dict(self._last_result) if self._last_result else None)


def reporting_router(image_root, require_publisher, audit=None, auto_reporter=None,
                     publisher_dedup=False):
    reporter = Subject1Reporter(image_root, audit=audit, publisher_dedup=publisher_dedup)
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
        return dict(team_name=TEAM_NAME, endpoint=ENDPOINT, missions=reporter.missions(),
                    results_directory=str(reporter.image_root),
                    automatic=auto_reporter.snapshot() if auto_reporter is not None else None)
    @router.post('/prepare')
    def prepare(request: Request, payload: dict = Body(...)):
        permitted(request)
        try:
            team_name = payload.get('team_name', TEAM_NAME)
            if not isinstance(team_name, str) or not team_name.strip():
                raise ValueError('参赛队名不能为空')
            document = payload.get('document')
            built_here = document is None
            if built_here:
                document = reporter.build(payload.get('mission_id'), team_name.strip())
            if not isinstance(document, dict):
                raise ValueError('JSON 顶层须是对象')
            document['name'] = team_name.strip()
            return reporter.prepare(document, already_deduplicated=built_here and publisher_dedup)
        except (ValueError, OSError) as error:
            if audit:
                audit.record('科目一赛事结果整理失败', mission_id=payload.get('mission_id'), error=str(error))
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
