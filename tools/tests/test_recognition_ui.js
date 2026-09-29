const {chromium}=require('playwright'),fs=require('fs'),assert=require('node:assert/strict'),path=require('path');
const root=process.env.COMPETITION_TEST_ROOT||path.resolve(__dirname,'../..');
const web=path.join(root,'competition_backend/competition_backend/web');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
 const page=await browser.newPage({viewport:{width:1400,height:1000}}),errors=[],plans=[];
 let saved=null,saves=0,failSave=false;
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(url.pathname==='/')return route.fulfill({body:fs.readFileSync(web+'/index.html','utf8').split('\n').filter(l=>!l.startsWith('选择地面终端().then')).join('\n'),contentType:'text/html; charset=utf-8'});
   if(url.pathname.includes('terrain_basemap.js'))return route.fulfill({body:fs.readFileSync(web+'/terrain_basemap.js'),contentType:'application/javascript'});
   if(url.pathname==='/api/v1/recognition-categories'){
     if(route.request().method()==='PUT'){
       saves++;
       if(failSave)return route.fulfill({status:422,json:{detail:'模拟保存失败'}});
       saved=route.request().postDataJSON();saved.category_names=saved.category_ids.map(i=>i<=7?'车辆'+i:i<=11?'工事'+(i-7):'人员'+(i-11));
     }
     return route.fulfill({json:{selection:saved}});
   }
   if(url.pathname==='/api/v1/plan'){
     plans.push(route.request().postDataJSON());return route.fulfill({json:{mission:{mission_id:'preview',phase:'planned',uavs:{}},dispatch_status:{assigned_uav_ids:[]}}});
   }
   return route.fulfill({json:{}});
 });
 await page.goto('http://recognition.test/');
 assert.equal(await page.locator('#recognitionSettings').evaluate(e=>e.open),false);
 assert.equal(await page.locator('#recognitionSettings').evaluate(e=>e.previousElementSibling.id),'coverageSettings');
 await page.locator('#recognitionSettings summary').click();
 await page.waitForFunction(()=>识别类别已加载);
 assert.equal(await page.locator('#recognitionGroups input').count(),15);
 await page.locator('#recognitionCount').selectOption('3');
 for(const id of [1,8])await page.locator(`#recognitionGroups input[value="${id}"]`).check();
 assert.equal(await page.locator('#saveRecognition').isEnabled(),false);
 await page.locator('#recognitionGroups input[value="12"]').check();
 assert.match(await page.locator('#recognitionCounter').textContent(),/3 \/ 3/);
 await page.evaluate(()=>生成比赛覆盖());assert.equal(plans.length,0,'未保存不得分派');
 await page.locator('#saveRecognition').click();await page.waitForFunction(()=>!识别类别保存中&&已保存识别类别);
 assert.deepEqual(saved.category_ids,[1,8,12]);assert.equal(saves,1);assert.equal(plans.length,0,'保存不分派');
 // Test a complete plan payload without touching a real ground service.
 await page.evaluate(()=>{当前角色={task_publisher:true};渲染=()=>{};});
 await page.evaluate(()=>生成比赛覆盖());assert.equal(plans.length,1);assert.deepEqual(plans[0].recognition_selection.category_ids,[1,8,12]);
 await page.reload();assert.equal(await page.locator('#recognitionSettings').evaluate(e=>e.open),false);
 await page.locator('#recognitionSettings summary').click();await page.waitForFunction(()=>已保存识别类别?.category_count===3);
 assert.equal(await page.locator('#recognitionGroups input:checked').count(),3);
 if(process.env.RECOGNITION_SCREENSHOT)await page.locator('#recognitionSettings').screenshot({path:process.env.RECOGNITION_SCREENSHOT});
 await page.locator('#recognitionCount').selectOption('2');assert.equal(await page.locator('#saveRecognition').isEnabled(),false);
 await page.locator('#recognitionGroups input[value="12"]').uncheck();
 failSave=true;await page.locator('#saveRecognition').click();await page.waitForFunction(()=>document.getElementById('recognitionFeedback').textContent.includes('模拟保存失败'));
 await page.evaluate(()=>生成比赛覆盖());assert.equal(plans.length,1,'保存失败不得用旧配置分派');
 failSave=false;await page.locator('#recognitionCount').selectOption('15');
 for(let id=1;id<=15;id++)await page.locator(`#recognitionGroups input[value="${id}"]`).check();
 await page.locator('#saveRecognition').click();await page.waitForFunction(()=>已保存识别类别?.category_count===15&&!识别类别保存中);
 assert.deepEqual(saved.category_ids,Array.from({length:15},(_,i)=>i+1));
 await page.locator('#subject').selectOption('subject2');assert.equal(await page.locator('#recognitionSettings').isVisible(),false);
 await page.evaluate(()=>{渲染=()=>{};});await page.evaluate(()=>生成比赛覆盖());assert.equal(plans.at(-1).recognition_selection,undefined);
 assert.deepEqual(errors,[]);
 console.log('类别窗口默认折叠、15类映射、计数、保存与恢复、未保存拦截、分派字段及其他科目隔离通过。');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
