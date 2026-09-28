const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.resolve(
  __dirname, '../../competition_backend/competition_backend/web/index.html'), 'utf8');
const selectStart = html.indexOf('function 读取机载GPS参考组(');
const selectEnd = html.indexOf('function 读取机载GPS参考()', selectStart);
const mapStart = html.indexOf('function 规划图遥测(');
const mapEnd = html.indexOf('function 更新实际轨迹(', mapStart);
assert.ok(selectStart >= 0 && selectEnd > selectStart);
assert.ok(mapStart >= 0 && mapEnd > mapStart);

const context = {};
vm.createContext(context);
vm.runInContext(html.slice(selectStart, selectEnd), context);
vm.runInContext(html.slice(mapStart, mapEnd), context);

const coarse = {latitude: 38.88025665283203, longitude: 121.52688598632812};
const precise = {latitude: 38.880256421, longitude: 121.526884317,
  age_seconds: 0.2, source: 'mavros_global'};
const sample = {connected: true, received_at: 999.7,
  gps_telemetry_age_seconds: 0.1, gps_status: 3, location_source: 4,
  ...coarse, gps_position: precise, rel_alt: 1.5,
  position: [0, 0, 1.5], velocity: [0, 0, 0]};
const select = item => {
  context.当前状态 = {active_uav_ids: [1], telemetry: {'1': item}, server_time: 1000};
  return context.读取机载GPS参考组()['1'] || null;
};
const map = (item, reference) => context.规划图遥测({search_area: {
  coordinate_mode: 'gps', gps_origin: reference,
  coverage: {projection: {north_m_per_degree: 110926, west_m_per_degree: 92535}}
}}, item, 1000);
const nearZero = position => {
  assert.ok(Math.abs(position[0]) < 1e-6, `X ${position[0]} 应接近 0`);
  assert.ok(Math.abs(position[1]) < 1e-6, `Y ${position[1]} 应接近 0`);
};

let reference = select(sample);
assert.equal(reference.latitude, precise.latitude);
assert.equal(reference.longitude, precise.longitude);
assert.equal(reference.source, 'mavros_global');
let mapped = map(sample, reference);
assert.equal(mapped.source, reference.source);
nearZero(mapped.position);

const staleFix = {...sample, gps_position: {...precise, age_seconds: 1.8}};
reference = select(staleFix);
assert.equal(reference.latitude, coarse.latitude);
assert.equal(reference.longitude, coarse.longitude);
assert.equal(reference.source, 'prometheus_gps_low_precision');
mapped = map(staleFix, reference);
assert.equal(mapped.source, 'prometheus_gps');
nearZero(mapped.position);

for (const invalid of [
  {...sample, connected: false},
  {...sample, received_at: 997.9},
  {...sample, gps_telemetry_age_seconds: 2.0},
  {...sample, gps_status: 2},
  {...sample, location_source: 2},
  {...sample, gps_position: null, latitude: null},
  {...sample, gps_position: null, longitude: 181},
]) {
  assert.equal(select(invalid), null);
  assert.equal(map(invalid, coarse), null);
}

process.stdout.write('GPS 规划参考与地图定位同源、精度回退和无效数据检查通过。\n');
