"""正式赛事副本的编号和类别格式化；原始结果和审计数据独立保留。"""
from copy import deepcopy
import hashlib
import json


_MOVING_PERSON_MODELS = {
    "运动的人员%d" % number: "人员%d" % number for number in range(1, 5)
}


def submission_content_hash(document):
    """成果正文摘要；忽略重新生成时间，保留目标和轨迹的顺序。"""
    if not isinstance(document, dict):
        raise ValueError("赛事结果必须为 JSON 对象")
    content = deepcopy(document)
    metadata = content.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("createdAt", None)
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def format_submission(document):
    """返回正式 JSON 深拷贝及编号/未入选目标审计，不更改原始数据。

    仅修改 Feature.id、精确匹配的运动人员型号，并将 indoorTargets 从
    正式 metadata 移到本地审计。保留输入排序、坐标、时间及图片引用。
    对已格式化结果重复调用不会再次改变正式 JSON。
    """
    if not isinstance(document, dict):
        raise ValueError("赛事结果必须为 JSON 对象")
    result = deepcopy(document)
    features = result.get("features", [])
    if not isinstance(features, list):
        raise ValueError("赛事结果 features 必须为列表")
    audit = {"id_mapping": [], "excluded_targets": []}
    for index, feature in enumerate(features, 1):
        if not isinstance(feature, dict):
            raise ValueError("赛事结果的每个目标必须为 JSON 对象")
        submission_id = "target-%03d" % index
        audit["id_mapping"].append({
            "source_id": feature.get("id"),
            "submission_id": submission_id,
        })
        feature["id"] = submission_id
        properties = feature.get("properties")
        if isinstance(properties, dict):
            model = properties.get("targetModel")
            if isinstance(model, str) and model in _MOVING_PERSON_MODELS:
                properties["targetModel"] = _MOVING_PERSON_MODELS[model]
    metadata = result.get("metadata")
    if isinstance(metadata, dict) and "indoorTargets" in metadata:
        excluded = metadata.pop("indoorTargets")
        if isinstance(excluded, list):
            audit["excluded_targets"] = excluded
        elif excluded is not None:
            # 保留旧版非列表记录，避免格式规范化时丢失排除原因。
            audit["excluded_targets"] = [excluded]
    audit["document_sha256"] = submission_content_hash(result)
    return result, audit
