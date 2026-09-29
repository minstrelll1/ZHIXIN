"""科目一任务卡类别协议；业务编号独立于检测模型 class_id。"""
CATEGORY_CATALOG = (
    [{'id': i, 'name': '车辆%d' % i} for i in range(1, 8)]
    + [{'id': i + 7, 'name': '工事%d' % i} for i in range(1, 5)]
    + [{'id': i + 11, 'name': '人员%d' % i} for i in range(1, 5)]
)


def validate_recognition_selection(value):
    if not isinstance(value, dict):
        raise ValueError('识别类别配置必须包含类别个数和类别编号')
    count, ids = value.get('category_count'), value.get('category_ids')
    if type(count) is not int or not 1 <= count <= 15:
        raise ValueError('识别类别个数必须为 1～15')
    if not isinstance(ids, list) or len(ids) != count:
        raise ValueError('已选类别数量必须与要求识别的类别个数一致')
    if any(type(item) is not int or not 1 <= item <= 15 for item in ids):
        raise ValueError('识别类别编号必须为 1～15 的整数')
    if len(set(ids)) != count:
        raise ValueError('识别类别不能重复')
    ids = sorted(ids)
    names = {item['id']: item['name'] for item in CATEGORY_CATALOG}
    return dict(category_count=count, category_ids=ids, category_names=[names[item] for item in ids])
