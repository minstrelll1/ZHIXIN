const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const html=fs.readFileSync(path.resolve(__dirname,'../../competition_backend/competition_backend/web/index.html'),'utf8');
const start=html.indexOf('const 识别类别目录=');
const end=html.indexOf('let 比赛覆盖方案=',start);
assert.ok(start>0&&end>start);
assert.doesNotMatch(html, /id="saveRecognition"|require_recognition_selection:true/);
assert.match(html, /<option value="0" selected>0 类<\/option>/);

function openPage(){
 const nodes=new Map();
 const inputs=[];
 const makeNode=()=>({append(){},textContent:'',disabled:false});
 const get=id=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id)};
 get('recognitionCount').value='0';
 const context={
  $:get,
  document:{
   querySelectorAll:selector=>selector==='#recognitionGroups input:checked'?inputs.filter(item=>item.checked):inputs,
   createElement:tag=>{
    const item=makeNode();
    if(tag==='input'){item.checked=false;inputs.push(item)}
    return item;
   },
   createTextNode:text=>({textContent:text}),
  },
 };
 vm.createContext(context);
 vm.runInContext(html.slice(start,end),context);
 return {context,inputs,get,run:code=>vm.runInContext(code,context)};
}

const first=openPage();
const catalog=JSON.parse(JSON.stringify(first.run('识别类别目录')));
assert.equal(catalog.length,19);
assert.deepEqual(catalog.map(item=>item.id),Array.from({length:19},(_,index)=>index));
assert.deepEqual(catalog.map(item=>item.name),
 ['车辆1','车辆2','车辆3','车辆4','车辆5','车辆6','车辆7',
  '工事1','工事2','工事3','工事4','人员1','人员2','人员3','人员4',
  '运动的人员1','运动的人员2','运动的人员3','运动的人员4']);
assert.equal(first.inputs.length,19);
assert.equal(first.get('recognitionCounter').textContent,'已选择 0 / 0 类');
assert.deepEqual(JSON.parse(JSON.stringify(first.run('获取规划识别类别()'))),{category_count:0,category_ids:[]});

first.get('recognitionCount').value='3';first.get('recognitionCount').onchange();
for(const id of [18,0,7]){
 const input=first.inputs.find(item=>item.value===id);
 input.checked=true;input.onchange();
}
assert.equal(first.get('recognitionCounter').textContent,'已选择 3 / 3 类');
assert.deepEqual(JSON.parse(JSON.stringify(first.run('获取规划识别类别()'))),{category_count:3,category_ids:[0,7,18]});
first.get('recognitionCount').value='2';first.get('recognitionCount').onchange();
assert.deepEqual(JSON.parse(JSON.stringify(first.run('获取规划识别类别()'))),{category_count:3,category_ids:[0,7,18]},'数量不一致也不能阻断任务');
const reopened=openPage();
assert.equal(reopened.inputs.filter(item=>item.checked).length,0);
assert.deepEqual(JSON.parse(JSON.stringify(reopened.run('获取规划识别类别()'))),{category_count:0,category_ids:[]});
console.log('19 类编号、默认 0 类、无需保存、数量不匹配不阻断及重新打开重置通过。');
