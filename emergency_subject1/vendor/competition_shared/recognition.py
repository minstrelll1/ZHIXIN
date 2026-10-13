"""科目一任务卡类别协议；业务编号独立于检测模型 class_id。"""
CATEGORY_CATALOG = (
    [{'id': i, 'name': '车辆%d' % (i + 1)} for i in range(0, 7)]
    + [{'id': i, 'name': '工事%d' % (i - 6)} for i in range(7, 11)]
    + [{'id': i, 'name': '人员%d' % (i - 10)} for i in range(11, 15)]
    + [{'id': i, 'name': '运动的人员%d' % (i - 14)} for i in range(15, 19)]
)


def validate_recognition_selection(value):
    if not isinstance(value, dict):
        raise ValueError('识别类别配置必须包含类别个数和类别编号')
    count, ids = value.get('category_count'), value.get('category_ids')
    if type(count) is not int or not 0 <= count <= 19:
        raise ValueError('识别类别个数必须为 0～19')
    if not isinstance(ids, list) or len(ids) != count:
        raise ValueError('已选类别数量必须与要求识别的类别个数一致')
    if any(type(item) is not int or not 0 <= item <= 18 for item in ids):
        raise ValueError('识别类别编号必须为 0～18 的整数')
    if len(set(ids)) != count:
        raise ValueError('识别类别不能重复')
    ids = sorted(ids)
    names = {item['id']: item['name'] for item in CATEGORY_CATALOG}
    return dict(category_count=count, category_ids=ids, category_names=[names[item] for item in ids])


def optional_recognition_selection(value):
    """类别是可选侦察提示；缺失或旧格式均退为 0 类，不影响飞行任务。"""
    raw = value.get('category_ids') if isinstance(value, dict) else None
    ids = sorted(set(item for item in raw if type(item) is int and 0 <= item <= 18)) if isinstance(raw, list) else []
    return validate_recognition_selection({'category_count': len(ids), 'category_ids': ids})
