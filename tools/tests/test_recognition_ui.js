const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const html=fs.readFileSync(path.resolve(__dirname,'../../competition_backend/competition_backend/web/index.html'),'utf8');
const start=html.indexOf('const 识别类别目录=');
const end=html.indexOf('let 比赛覆盖方案=',start);
assert.ok(start>0&&end>start);
assert.doesNotMatch(html, /id="saveRecognition"|require_recognition_selection:true/);
assert.doesNotMatch(html, /id="recognitionCount"/);
assert.match(html, /id="recognitionSelectAll"[^>]*>全选<\/button>/);

function openPage(){
 const nodes=new Map();
 const inputs=[];
 const makeNode=()=>({append(){},textContent:'',disabled:false,style:{}});
 const get=id=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id)};
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
assert.equal(first.get('recognitionCounter').textContent,'已选择 0 类');
assert.deepEqual(JSON.parse(JSON.stringify(first.run('获取规划识别类别()'))),{category_count:0,category_ids:[]});

assert.ok(first.inputs.every(item=>!item.disabled),'默认 0 类时所有类别均可直接勾选');
for(const id of [18,0,7]){
 const input=first.inputs.find(item=>item.value===id);
 input.checked=true;input.onchange();
}
assert.equal(first.get('recognitionCounter').textContent,'已选择 3 类');
assert.deepEqual(JSON.parse(JSON.stringify(first.run('获取规划识别类别()'))),{category_count:3,category_ids:[0,7,18]});
assert.ok(first.inputs.every(item=>!item.disabled),'选择若干类别后不能限制继续勾选');
first.get('recognitionSelectAll').onclick();
assert.equal(first.get('recognitionCounter').textContent,'已选择 19 类');
assert.equal(first.get('recognitionSummary').textContent,'· 已选 19 类');
assert.equal(first.get('recognitionSelectAll').disabled,true);
assert.deepEqual(JSON.parse(JSON.stringify(first.run('获取规划识别类别()'))),{category_count:19,category_ids:Array.from({length:19},(_,i)=>i)});
first.inputs[7].checked=false;first.inputs[7].onchange();
assert.equal(first.get('recognitionCounter').textContent,'已选择 18 类');
assert.equal(first.get('recognitionSelectAll').disabled,false);
assert.ok(first.inputs.every(item=>!item.disabled),'全选后仍可逐项取消');
assert.deepEqual(JSON.parse(JSON.stringify(first.run('获取规划识别类别()'))).category_ids,Array.from({length:19},(_,i)=>i).filter(i=>i!==7));
for(const input of first.inputs){input.checked=false;input.onchange()}
assert.equal(first.get('recognitionCounter').textContent,'已选择 0 类');
assert.deepEqual(JSON.parse(JSON.stringify(first.run('获取规划识别类别()'))),{category_count:0,category_ids:[]});
const reopened=openPage();
assert.equal(reopened.inputs.filter(item=>item.checked).length,0);
assert.deepEqual(JSON.parse(JSON.stringify(reopened.run('获取规划识别类别()'))),{category_count:0,category_ids:[]});
// 执行页面实际的科目切换与类别取值逻辑，确认科目二不会被隐藏或漏带类别。
assert.match(html, /<summary>科目一／二识别类别/);
const modeStart=html.indexOf('function 更新规划模式()');
const modeEnd=html.indexOf('function 更新任务按钮',modeStart);
reopened.context.当前状态=null;
reopened.context.更新任务按钮=()=>{};
reopened.run(html.slice(modeStart,modeEnd));
const selectionExpr=html.match(/const recognitionSelection=(.+);/)[1];
for(const subject of ['subject1','subject2','subject3']){
 reopened.get('subject').value=subject;
 reopened.run('更新规划模式()');
 assert.equal(reopened.get('recognitionSettings').style.display,subject==='subject3'?'none':'block');
 for(const input of reopened.inputs)input.checked=false;
 assert.deepEqual(JSON.parse(JSON.stringify(reopened.run(selectionExpr))),
   subject==='subject3'?null:{category_count:0,category_ids:[]});
 reopened.get('recognitionSelectAll').onclick();
 assert.deepEqual(JSON.parse(JSON.stringify(reopened.run(selectionExpr))),
   subject==='subject3'?null:{category_count:19,category_ids:Array.from({length:19},(_,i)=>i)});
}
console.log('19 类勾选、全选、清空、重新打开重置，科目一／二显示与规划类别取值通过。');
