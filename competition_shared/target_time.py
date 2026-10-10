"""把定位事件映射到任务发布端时间；不改原始文件，也不设置任何系统时钟。"""
import json
import math
from pathlib import Path

NS = 1_000_000_000
MIN_EPOCH = 1_577_836_800
MAX_EPOCH = 4_102_444_800
MAX_REFERENCE_DISTANCE_SEC = 24 * 3600


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _positive_ns(value):
    return type(value) is int and value > 0


def load_time_references(root):
    try:
        document = json.loads((Path(root) / 'time_references.json').read_text(encoding='utf-8'))
        if not isinstance(document, dict) or document.get('schema_version') != 1:
            return {}, 'invalid_reference_file'
        references = document.get('references')
        if not isinstance(references, dict):
            return {}, 'invalid_reference_file'
        return references, ''
    except FileNotFoundError:
        return {}, 'missing_reference_file'
    except (OSError, ValueError):
        return {}, 'unreadable_reference_file'


def map_localization_time(metadata, uav_id, references, reference_error=''):
    """返回内存整理副本和审计；新协议无法校验时等待，旧记录明确标记未校验。"""
    result = dict(metadata)
    raw = metadata.get('localization_time')
    context = metadata.get('localization_clock')
    audit = dict(status='pending', reason='', original_localization_time=raw,
                 uav_id=uav_id, clock_source='publisher_windows')

    def fail(reason):
        audit['reason'] = reason
        # 对新回传不把错误的 RTC 日期伪装成准确北京时间；原始文件不变。
        result['localization_time'] = None
        return result, audit

    if context is None:
        audit.update(status='unverified_legacy', reason='no_source_clock_evidence')
        return result, audit
    if not isinstance(context, dict) or context.get('schema_version') != 1:
        return fail('invalid_source_clock_evidence')
    audit.update(boot_id=context.get('boot_id'), segment_id=context.get('segment_id'))
    boot_id = context.get('boot_id')
    if not isinstance(boot_id, str) or not boot_id:
        return fail('unknown_boot')
    reference = references.get(boot_id)
    if not isinstance(reference, dict):
        return fail(reference_error or 'missing_boot_reference')
    if (type(uav_id) is not int or not 1 <= uav_id <= 6
            or type(reference.get('uav_id')) is not int or reference['uav_id'] != uav_id
            or reference.get('boot_id') != boot_id
            or reference.get('source') != 'publisher_windows'):
        return fail('reference_identity_mismatch')
    if 'uav_id' in metadata and (type(metadata['uav_id']) is not int or metadata['uav_id'] != uav_id):
        return fail('source_uav_mismatch')
    ground = reference.get('ground_epoch')
    remote = reference.get('remote_monotonic')
    uncertainty = reference.get('uncertainty_sec')
    if (not _finite(ground) or not MIN_EPOCH <= ground < MAX_EPOCH
            or not _finite(remote) or remote <= 0
            or not _finite(uncertainty) or not 0 <= uncertainty <= 1):
        return fail('invalid_reference_sample')
    sample = context.get('sample_monotonic_ns')
    origin_ns = round(ground * NS) - round(remote * NS)
    # 接收顺序独立于定位时间是否有效。否则最新无效结果会以错误RTC排在
    # 已校正旧结果之前，静目标误留旧坐标。只在内存中归一化排序字段。
    if _positive_ns(sample) and abs(sample / NS - remote) <= MAX_REFERENCE_DISTANCE_SEC:
        result['detection_received_at_unix_ns'] = origin_ns + sample
        audit['receipt_order_corrected'] = True
    if context.get('mapping_valid') is not True or context.get('use_sim_time'):
        audit['source_reason'] = context.get('reason')
        return fail('source_clock_mapping_invalid')
    if (not isinstance(raw, dict) or type(raw.get('secs')) is not int
            or type(raw.get('nsecs')) is not int or raw['secs'] < 0
            or not 0 <= raw['nsecs'] < NS or raw['secs'] == raw['nsecs'] == 0):
        return fail('invalid_localization_time')
    event = context.get('event_monotonic_ns')
    ros = context.get('source_ros_ns')
    source_uncertainty = context.get('sampling_uncertainty_ns')
    segment_start = context.get('segment_start_monotonic_ns')
    if (not all(_positive_ns(value) for value in (event, sample, ros, segment_start))
            or type(source_uncertainty) is not int or not 0 <= source_uncertainty <= 50_000_000
            or event < segment_start - 50_000_000 or event > sample + 50_000_000
            or sample + raw['secs'] * NS + raw['nsecs'] - ros != event):
        return fail('inconsistent_source_clock_evidence')
    age = event / NS - remote
    if abs(age) > MAX_REFERENCE_DISTANCE_SEC:
        return fail('reference_too_distant')
    # 单调时间差仍使用整数，避免先把原始纳秒转成 epoch 浮点数丢精度。
    corrected_ns = origin_ns + event
    sec, nsec = divmod(corrected_ns, NS)
    if not MIN_EPOCH <= sec < MAX_EPOCH:
        return fail('converted_time_out_of_range')
    result['localization_time'] = dict(secs=sec, nsecs=nsec)
    audit.update(status='corrected', reason='ok', corrected_localization_time=result['localization_time'],
                 event_monotonic_ns=event, reference=dict(reference),
                 reference_distance_sec=age,
                 sampling_uncertainty_sec=uncertainty + source_uncertainty / NS)
    return result, audit
