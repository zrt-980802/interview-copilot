const {test,before,after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const base=process.env.INTERVIEW_PREVIEW_URL,phone=process.env.INTERVIEW_MOBILE_URL,skip=!base;
let browser;
before(async()=>{if(!skip)browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH||undefined});});
after(async()=>{await browser?.close();});
async function fixture(t,viewport={width:1365,height:900}) {
  const context=await browser.newContext({viewport}),page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));t.after(()=>context.close());
  return {page,request:context.request,errors};
}
async function screenshot(page,name) {
  if(!process.env.INTERVIEW_SCREENSHOT_DIR)return;
  fs.mkdirSync(process.env.INTERVIEW_SCREENSHOT_DIR,{recursive:true});
  await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,name+'.png'),fullPage:true});
}
async function ready(page) {
  await page.goto(base+'/settings');await page.locator('#fields:not([disabled])').waitFor();
}

test('provider inventory counts saved complete services, distinguishes disabled backup and offers GLM preset',{skip},async t=>{
  const {page,request,errors}=await fixture(t);
  async function save(){
    const saved=page.waitForResponse(response=>response.url()===base+'/api/providers'&&response.request().method()==='POST');
    await page.locator('#save').click();assert.equal((await saved).status(),200);
    await page.locator('#fields:not([disabled])').waitFor();
  }
  const reset=await request.post(base+'/api/providers',{data:{base_url:'https://synthetic-primary.invalid/v1',
    quick_model:'offline-primary',detail_model:'offline-primary',allow_anonymous:true,clear_api_key:true,
    fallback_enabled:false,fallback_base_url:'',fallback_model:'',clear_fallback_api_key:true}});
  assert.equal(reset.status(),200);
  await ready(page);
  assert.match(await page.locator('#providerCount').textContent(),/已配置 1 \/ 2.*已启用 1/);
  await page.locator('#fallback_enabled').check();
  await page.locator('#fallbackPreset').selectOption('zhipu');
  assert.equal(await page.locator('#fallback_model').inputValue(),'glm-5.3');
  assert.equal(await page.locator('#fallback_base_url').inputValue(),'https://open.bigmodel.cn/api/paas/v4');
  await page.locator('#fallback_api_key').fill('synthetic-inventory-key');
  await page.locator('#remember').uncheck();
  assert.match(await page.locator('#providerCount').textContent(),/已配置 1 \/ 2/);
  await save();
  await page.reload();await page.locator('#fields:not([disabled])').waitFor();
  assert.match(await page.locator('#providerCount').textContent(),/已配置 2 \/ 2.*已启用 2/);
  assert.match(await page.locator('#providerInventory').textContent(),/备用智谱 GLM已启用/);
  assert.equal(await page.locator('#fallback_api_key').inputValue(),'');
  for(const [name,width,height] of [['desktop',1365,900],['390',390,844],['320',320,740]]){
    await page.setViewportSize({width,height});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    const count=await page.locator('#providerCount').boundingBox();
    assert.ok(count.x>=0&&count.x+count.width<=width);
    await screenshot(page,'provider-inventory-'+name);
  }
  await page.locator('#fallback_enabled').uncheck();
  assert.match(await page.locator('#providerCount').textContent(),/已启用 2/);
  await save();
  assert.match(await page.locator('#providerCount').textContent(),/已配置 2 \/ 2.*已启用 1/);
  await page.locator('#fallback_enabled').check();
  await page.getByRole('button',{name:'清除备用密钥',exact:true}).click();
  await save();
  assert.match(await page.locator('#providerCount').textContent(),/已配置 1 \/ 2.*已启用 1/);
  assert.match(await page.locator('#providerInventory').textContent(),/备用智谱 GLM配置未完成/);
  assert.deepEqual(errors,[]);
});

test('provider configuration tests, lists models, saves and clears keys without echoing them',{skip},async t=>{
  const {page,request,errors}=await fixture(t);
  await ready(page);
  await page.locator('#preset').selectOption('custom');
  await page.locator('#base_url').fill('https://synthetic-browser.invalid/v1');
  await page.locator('#quick_model').fill('offline-primary');
  await page.locator('#detail_model').fill('offline-primary');
  await page.locator('#allow_anonymous').uncheck();
  await page.locator('#api_key').fill('synthetic-browser-only-key');
  await page.locator('#remember').uncheck();
  const revision=(await (await request.get(base+'/api/providers')).json()).revision;
  await page.locator('[data-action=models][data-channel=primary]').click();
  await page.locator('#primaryResult').filter({hasText:'已获取 2 个模型'}).waitFor();
  assert.equal(await page.locator('#primaryModels option').count(),2);
  await page.locator('[data-action=test][data-channel=primary]').click();
  await page.locator('#primaryResult').filter({hasText:'模拟连接通过'}).waitFor();
  assert.equal((await (await request.get(base+'/api/providers')).json()).revision,revision);
  await page.locator('#save').click();
  await page.locator('#notice').filter({hasText:'已保存'}).waitFor();
  assert.equal(await page.locator('#api_key').inputValue(),'');
  assert.match(await page.locator('#primaryStatus').textContent(),/仅本次进程/);
  assert.doesNotMatch(await page.locator('body').innerText(),/synthetic-browser-only-key/);
  assert.doesNotMatch(await (await request.get(base+'/api/providers')).text(),/synthetic-browser-only-key/);
  await page.reload();await page.locator('#fields:not([disabled])').waitFor();
  assert.equal(await page.locator('#api_key').inputValue(),'');
  assert.match(await page.locator('#api_key').getAttribute('placeholder'),/留空保留/);
  await page.locator('#save').click();
  await page.locator('#notice').filter({hasText:'已保存'}).waitFor();
  assert.equal((await (await request.get(base+'/api/providers')).json()).key_set,true);
  await screenshot(page,'provider-settings-desktop');
  await page.getByRole('button',{name:'清除主服务密钥',exact:true}).click();
  await page.locator('#save').click();await page.locator('#notice').filter({hasText:'已保存'}).waitFor();
  assert.equal((await (await request.get(base+'/api/providers')).json()).key_set,false);
  if(phone)assert.equal((await request.get(phone+'/api/providers')).status(),404);
  assert.deepEqual(errors,[]);
});

test('fallback service and narrow settings layout remain usable',{skip},async t=>{
  const {page,errors}=await fixture(t,{width:390,height:844});
  await ready(page);await page.locator('#fallback_enabled').check();
  await page.locator('#fallback_base_url').fill('https://synthetic-backup.invalid/v1');
  await page.locator('#fallback_model').fill('offline-fallback');
  await page.locator('#fallback_api_key').fill('synthetic-backup-key');
  await page.locator('[data-action=test][data-channel=fallback]').click();
  await page.locator('#fallbackResult').filter({hasText:'模拟连接通过'}).waitFor();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  for(const id of ['base_url','quick_model','detail_model','fallback_base_url','fallback_model','save']) {
    const box=await page.locator('#'+id).boundingBox();assert.ok(box.x>=0&&box.x+box.width<=390);
  }
  await screenshot(page,'provider-settings-narrow');
  await page.locator('#fallback_enabled').uncheck();assert.equal(await page.locator('#fallbackFields').isVisible(),false);
  assert.deepEqual(errors,[]);
});

test('provider save conflict keeps edits visible and allows explicit reload',{skip},async t=>{
  const {page,request,errors}=await fixture(t);
  await ready(page);
  await page.locator('#quick_model').fill('pending-edit');
  const value=await (await request.get(base+'/api/providers')).json();
  assert.equal((await request.post(base+'/api/providers',{data:{revision:value.revision,quick_model:'other-window'}})).status(),200);
  await page.locator('#save').click();await page.locator('#notice.error').filter({hasText:'更新'}).waitFor();
  assert.equal(await page.locator('#quick_model').inputValue(),'pending-edit');
  await page.locator('#reload').click();await page.locator('#notice').filter({hasText:'已加载'}).waitFor();
  assert.equal(await page.locator('#quick_model').inputValue(),'other-window');
  assert.deepEqual(errors,[]);
});

test('provider connection errors are visible and leave configuration unchanged',{skip},async t=>{
  const {page,request,errors}=await fixture(t);
  const original=await (await request.get(base+'/api/providers')).json();
  await page.route('**/api/providers/test',route=>route.fulfill({status:400,json:{ok:false,error:'服务返回 HTTP 401，请检查密钥'}}));
  await ready(page);await page.locator('[data-action=test][data-channel=primary]').click();
  await page.locator('#primaryResult.error').filter({hasText:'HTTP 401'}).waitFor();
  assert.equal(await page.locator('#save').isEnabled(),true);
  assert.equal((await (await request.get(base+'/api/providers')).json()).revision,original.revision);
  assert.deepEqual(errors,[]);
});

test('desktop defaults to own microphone without starting capture on page open; live switch and stop work',{skip},async t=>{
  const {page,request,errors}=await fixture(t);
  await request.post(base+'/api/stop',{data:{source:'loopback'}});
  await request.post(base+'/api/stop',{data:{source:'mic'}});
  await page.goto(base+'/');
  await page.waitForFunction(()=>document.querySelector('#micSel').options.length>=3);
  assert.equal(await page.locator('#micSel').inputValue(),'default');
  assert.equal((await (await request.get(base+'/api/state')).json()).self_listening,false);
  assert.equal(await page.locator('#previewBadge').isVisible(),true);
  await page.locator('#btnStart').click();
  await page.waitForFunction(async()=>{const s=await(await fetch('/api/state')).json();return s.listening&&s.self_listening;});
  await page.locator('#micSel').selectOption('24');
  await page.waitForFunction(async()=>{const s=await(await fetch('/api/state')).json();return s.self_engine.selection===24;});
  await page.locator('#micSel').selectOption('');
  await page.waitForFunction(async()=>{const s=await(await fetch('/api/state')).json();return s.listening&&!s.self_listening;});
  await page.locator('#micSel').selectOption('default');
  await page.waitForFunction(async()=>(await(await fetch('/api/state')).json()).self_listening);
  await page.locator('#btnStop').click();
  await page.locator('#btnStart:not([disabled])').waitFor();
  assert.equal((await (await request.get(base+'/api/state')).json()).self_listening,false);
  await page.getByRole('link',{name:'模型设置'}).click();
  await page.locator('#fields:not([disabled])').waitFor();
  assert.deepEqual(errors,[]);
});

test('desktop device list refreshes after a microphone is connected',{skip},async t=>{
  const {page,errors}=await fixture(t);
  let connected=false;
  await page.route('**/api/state',async route=>{
    const response=await route.fetch(),value=await response.json();
    value.input_devices=connected?[{index:99,name:'New synthetic microphone',hostapi:'Synthetic'}]:[];
    await route.fulfill({json:value});
  });
  await page.goto(base+'/');await page.waitForFunction(()=>document.querySelector('#micSel').dataset.devices==='[]');
  connected=true;await page.evaluate(()=>refreshState());
  await page.locator('#micSel option[value="99"]').waitFor({state:'attached'});
  assert.deepEqual(errors,[]);
});
