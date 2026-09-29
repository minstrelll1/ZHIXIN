# 目标提交模板字段说明文档

**版本：** 1.0  
**更新时间：** 2026-09-10  
**文件格式：** GeoJSON 扩展格式  

---

## 一、文件结构概览

```
target-submission-template.json
├── type              # 固定为 "FeatureCollection"
├── name              # 参赛队名
├── description       # 集合描述
├── crs               # 坐标系定义，固定为EPSG:4326
├── features          # 目标结果列表
│   └── [Feature]     # 单个目标信息
└── metadata          # 元数据信息
```

---

## 二、根字段说明

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `type` | String | 是 | 固定值 `"FeatureCollection"`，标识为 GeoJSON 要素集合 |
| `name` | String | 事 | 参赛队名称，用于标识该文件所属参赛队，必填项 |
| `description` | String | 否 | 集合描述信息 |
| `crs` | Object | 否 | 坐标系参考系统定义，默认 EPSG:4326 |
| `features` | Array | 是 | 目标结果列表，包含一个或多个侦察目标结果 |
| `metadata` | Object | 否 | 文件元数据，包含版本、创建时间等信息 |

---

## 三、坐标系定义 (crs)

```json
"crs": {
  "type": "lonlat",
  "properties": {
    "lonlat": "EPSG:4326"
  }
}
```

| 字段 | 说明 |
|------|------|
| `type` | 固定为 `"lonlat"` |
| `properties.lonlat` | 坐标系标识，`EPSG:4326` |

---

## 四、目标 (Feature) 结构

每个目标对象包含以下字段：

```json
{
  "type": "Feature",
  "id": "target-001",
  "geometry": { ... },
  "properties": { ... }
}
```

### 4.1 Feature 基础字段

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `type` | String | 是 | 固定值 `"Feature"` |
| `id` | String | 是 | 参赛队为目标设定的唯一标识符，建议格式：`target-XXX`|
| `geometry` | Object | 是 | 几何信息，定义目标位置/轨迹|
| `properties` | Object | 是 | 属性信息，包含目标详细信息 |

---

## 五、几何字段 (geometry)

### 5.1 固定目标 - Point 类型

```json
"geometry": {
  "type": "Point",
  "coordinates": [经度，纬度]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | String | 固定值 `"Point"` |
| `coordinates` | Array[3] | 坐标数组 `[longitude, latitude]`，`[经度，纬度]`, 顺序不可颠倒 |

**坐标说明：**
- **经度 (longitude)：** -180 ~ 180，单位：度 (°)
- **纬度 (latitude)：** -90 ~ 90，单位：度 (°)

### 5.2 移动目标 - LineString 类型

```json
"geometry": {
  "type": "LineString",
  "coordinates": [
    [经度 1, 纬度 1],
    [经度 2, 纬度 2],
    ...
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | String | 固定值 `"LineString"` |
| `coordinates` | Array[3][] | 坐标点序列数组，至少包含 2 个点 |

---

## 六、属性字段 (properties)

### 6.1 通用字段（静态/动态目标共有）

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `targetCategory` | String | 是 | 目标类别：`"固定"` 或 `"移动"`  |
| `targetType` | String | 是 | 目标类型：`"车辆"`、`"人员"`、`"工事"`、`"其他"`等 |
| `targetModel` | String | 是 | 目标型号，例如车辆种类、人员阵营、工事种类 |
| `imagePath` | String | 否 | 目标图片相对路径，如 `"./images/target-001.jpg"` |
| `confidence` | Number | 否 | 识别置信度，范围 0.0 ~ 1.0 |

### 6.2 静态目标专用字段

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `timestamp` | String | 是 | 目标发现时间，ISO 8601 格式 |

**时间格式示例：**
```
2026-10-23T10:00:00+08:00
```

### 6.3 动态目标专用字段

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `trackStartTime` | String | 是 | 轨迹起始时间，ISO 8601 格式 |
| `trackEndTime` | String | 是 | 轨迹结束时间，ISO 8601 格式 |
| `trackPoints` | Array | 是 | 轨迹点详细信息数组 |

#### trackPoints 数组元素结构

```json
{
  "coordinates": [经度，纬度],
  "timestamp": "2026-07-23T10:05:00+08:00"
}
```

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `coordinates` | Array[3] | 是 | 坐标点 `[longitude, latitude]` |
| `timestamp` | String | 是 | 该轨迹点的时间戳，ISO 8601 格式 |

---

## 七、元数据 (metadata)

对本JSON文件的基本说明，均为非必填字段。

```json
"metadata": {
  "version": "1.0",
  "createdAt": "2026-10-23T10:25:00+08:00",
  "coordinateSystem": "WGS84",
  "coordinateOrder": "[经度 longitude, 纬度 latitude]",
  "targetCategories": ["static", "dynamic"],
  "targetTypes": ["车辆", "人员", "工事"]
}
```

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `version` | String | 文件版本号 |
| `createdAt` | String | 文件创建时间，ISO 8601 格式 |
| `coordinateSystem` | String | 坐标系名称 |
| `coordinateOrder` | String | 坐标数组字段顺序说明，（经度，纬度） |
| `targetCategories` | Array | 支持的目标类别列表，静态、动态 |
| `targetTypes` | Array | 支持的目标类型列表，车辆、人员、工事 |

---

## 八、示例

### 8.1 静态目标示例

```json
{
  "type": "Feature",
  "id": "target-001",
  "geometry": {
    "type": "Point",
    "coordinates": [116.4074, 39.9042]
  },
  "properties": {
    "targetCategory": "固定",
    "targetType": "车辆",
    "targetModel": "车辆类2",
    "imagePath": "./images/target-001.jpg",
    "confidence": 0.95,
    "timestamp": "2026-07-23T10:00:00+08:00"
  }
}
```

### 8.2 移动目标示例

```json
{
  "type": "Feature",
  "id": "target-002",
  "geometry": {
    "type": "LineString",
    "coordinates": [
      [116.4100, 39.9100],
      [116.4120, 39.9120],
      [116.4150, 39.9150],
      [116.4180, 39.9180]
    ]
  },
  "properties": {
    "targetCategory": "移动",
    "targetType": "车辆",
    "vehicleModel": "车辆类5",
    "imagePath": "./images/target-002.jpg",
    "confidence": 0.92,
    "trackStartTime": "2026-07-23T10:15:00+08:00",
    "trackEndTime": "2026-07-23T10:15:03+08:00",
    "trackPoints": [
      {
        "coordinates": [116.4100, 39.9100],
        "timestamp": "2026-07-23T10:15:00+08:00"
      },
      {
        "coordinates": [116.4120, 39.9120],
        "timestamp": "2026-07-23T10:15:03+08:00"
      }
    ]
  }
}
```

---

## 九、数据校验规则

| 规则 | 说明 |
|------|------|
| 坐标顺序 | 必须为 `[经度，纬度]`，不可颠倒 |
| 经度范围 | -180 ~ 180 |
| 纬度范围 | -90 ~ 90 |
| 时间格式 | ISO 8601 |
| 置信度范围 | 0.0 ~ 1.0 |
| 动态目标轨迹点 | 至少包含 2 个坐标点 |

---

## 十、文件命名建议

- **模板文件：** `target-submission-template.json`
- **图片目录：** `./images/`

---

## 十一、常见问题

**Q1: 坐标顺序能否改为 [纬度，经度]？**  
A: 不可以。GeoJSON 标准规定坐标顺序为 `[经度，纬度]`，本模板遵循该标准。

**Q2: 移动目标是否必须提供 trackPoints？**  
A: 必须提供。`geometry.coordinates` 仅存储位置序列，`trackPoints` 包含时间戳等详细信息。

**Q3: 图片路径是否必须为相对路径？**  
A: 否。可使用相对路径或绝对路径。

**Q4: 如何表示目标其他属性？**  
A: 可在 `properties` 中添加获取到的其他目标属性字段，例如： `"status": "lost"` 或 `"status": "destroyed"` 。

