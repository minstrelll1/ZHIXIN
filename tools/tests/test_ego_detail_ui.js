// 已按操作员要求移除 EGO 界面显示；机载采集不受影响。
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const html=fs.readFileSync(path.resolve(__dirname,'../../competition_backend/competition_backend/web/index.html'),'utf8');
assert.ok(!html.includes('EGO 状态'));
assert.ok(!html.includes('function EGO状态显示('));
assert.ok(html.includes('心跳包异常'));
console.log('EGO 已从实时详情移除。');
