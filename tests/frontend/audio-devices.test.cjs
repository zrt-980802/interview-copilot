const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const base=process.env.INTERVIEW_PREVIEW_URL,skip=!base;
let browser;
before(async()=>{if(!skip)browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH||undefined});});
after(async()=>{await browser?.close();});
async function fixture(t){
  const context=await browser.newContext({viewport:{width:1365,height:900}}),page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));t.after(()=>context.close());
  return {page,request:context.request,errors};
}

test('headphones and microphone select independently and restore the running devices after reload',{skip},async t=>{
  const {page,request,errors}=await fixture(t),starts=[];
  assert.equal((await(await request.get(base+'/api/health')).json()).preview,true);
  for(const source of ['loopback','mic'])await request.post(base+'/api/stop',{data:{source}});
  t.after(async()=>{for(const source of ['loopback','mic'])await fetch(base+'/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source})});});
  await page.route('**/api/start',route=>{starts.push(route.request().postDataJSON());return route.continue();});
  await page.goto(base);
  await page.locator('#deviceSel option[value="22"]').waitFor({state:'attached'});
  assert.match(await page.locator('#deviceSel option[value=""]').textContent(),/系统默认输出.*耳机/);
  assert.equal(starts.length,0);
  await page.locator('#deviceSel').selectOption('22');
  await page.locator('#micSel').selectOption('24');
  await page.locator('#btnStart').click();
  await page.waitForFunction(()=>document.querySelector('#captureStatus').textContent==='双通道采集中');
  assert.deepEqual(starts,[{source:'loopback',device_index:22},{source:'mic',device_index:24}]);
  assert.equal(await page.locator('#btnRefreshDevices').isDisabled(),true);
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#deviceSel').value==='22'&&document.querySelector('#micSel').value==='24');
  assert.match(await page.locator('#outputDeviceState').textContent(),/采集中/);
  await page.locator('#deviceSel').selectOption('23');
  await page.waitForFunction(async()=>(await(await fetch('/api/state')).json()).engine.selection===23);
  const state=await(await request.get(base+'/api/state')).json();
  assert.equal(state.self_engine.selection,24);
  assert.deepEqual(starts.at(-1),{source:'loopback',device_index:23});
  assert.equal(starts.filter(item=>item.source==='mic').length,1);
  await page.locator('#btnStop').click();
  await page.locator('#btnRefreshDevices:not([disabled])').waitFor();
  assert.deepEqual(errors,[]);
});

test('refresh rescans devices and retains the same named headset when indices change',{skip},async t=>{
  const {page,errors}=await fixture(t);let generation=0,starts=0;
  const devices=()=>generation===2?[{index:90,name:'USB speakers',hostapi:'Synthetic',is_default:true}]:
    [{index:generation?72:22,name:'USB headset',hostapi:'Synthetic',is_default:true}];
  await page.route('**/api/state',route=>route.fulfill({json:{listening:false,self_listening:false,devices:devices(),input_devices:[],resume:{loaded:false}}}));
  await page.route('**/api/start',route=>{starts++;return route.fulfill({json:{ok:true}});});
  await page.route('**/api/audio/devices/refresh',route=>{generation++;return route.fulfill({json:{ok:true,devices:devices(),input_devices:[]}});});
  await page.goto(base);await page.locator('#deviceSel option[value="22"]').waitFor({state:'attached'});
  await page.locator('#deviceSel').selectOption('22');
  await page.locator('#btnRefreshDevices').click();
  await page.waitForFunction(()=>document.querySelector('#deviceSel').value==='72');
  assert.equal(starts,0);
  await page.locator('#btnRefreshDevices:not([disabled])').click();
  await page.waitForFunction(()=>document.querySelector('#deviceSel').value==='');
  assert.match(await page.locator('#deviceSel option[value=""]').textContent(),/USB speakers/);
  assert.equal(starts,0);
  assert.deepEqual(errors,[]);
});

test('device refresh errors appear next to capture controls and permit retry',{skip},async t=>{
  const {page,errors}=await fixture(t);let fail=true;
  await page.route('**/api/state',route=>route.fulfill({json:{listening:false,self_listening:false,devices:[],input_devices:[],resume:{loaded:false}}}));
  await page.route('**/api/audio/devices/refresh',route=>fail?route.fulfill({status:503,json:{ok:false,error:'模拟设备刷新失败'}}):route.fulfill({json:{ok:true,devices:[],input_devices:[]}}));
  await page.goto(base);await page.locator('#btnRefreshDevices').click();
  await page.locator('#captureNotice.error').filter({hasText:'模拟设备刷新失败'}).waitFor();
  assert.doesNotMatch(await page.locator('#feed').innerText(),/模拟设备刷新失败/);
  fail=false;await page.locator('#btnRefreshDevices:not([disabled])').click();
  await page.locator('#captureNotice:not(.error)').filter({hasText:'设备列表已刷新'}).waitFor();
  assert.deepEqual(errors,[]);
});
