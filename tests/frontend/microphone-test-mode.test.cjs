const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const base=process.env.INTERVIEW_PREVIEW_URL,skip=!base;
let browser;
before(async()=>{if(!skip)browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH||undefined});});
after(async()=>{await browser?.close();});

async function reset(request){
  for(const source of ['loopback','mic'])await request.post(base+'/api/stop',{data:{source}});
  await request.post(base+'/api/audio/test-mode',{data:{enabled:false}});
  await request.post(base+'/api/clear');
}

test('microphone test starts only mic, asks a question, survives reload and restores normal context',{skip},async t=>{
  const page=await browser.newPage({viewport:{width:1365,height:900}}),errors=[],starts=[];
  page.on('pageerror',e=>errors.push(e.message));
  t.after(async()=>{await reset(page.request);await page.close();});
  assert.equal((await(await page.request.get(base+'/api/health')).json()).preview,true);
  await reset(page.request);
  await page.route('**/api/start',route=>{starts.push(route.request().postDataJSON());return route.continue();});
  await page.goto(base);await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
  await page.locator('#micSel').selectOption('');
  await page.getByRole('checkbox',{name:'麦克风提问测试'}).check();
  await page.waitForFunction(()=>document.querySelector('#captureStatus').textContent==='麦克风提问测试 · 未开启');
  assert.equal(starts.length,0);
  assert.equal(await page.locator('#micSel').inputValue(),'default');
  assert.equal(await page.locator('#deviceSel').isDisabled(),true);
  await page.locator('#micSel').selectOption('24');
  await page.getByRole('button',{name:'开始监听',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#captureStatus').textContent==='麦克风提问测试中');
  assert.deepEqual(starts,[{source:'mic',device_index:24}]);
  assert.equal(await page.locator('#btnStart').isDisabled(),true);
  assert.equal(await page.getByRole('checkbox',{name:'麦克风提问测试'}).isDisabled(),true);
  await page.request.post(base+'/__regression__/voice',{data:{source:'mic',text:'如何保证消息消费幂等？'}});
  await page.waitForFunction(()=>document.querySelector('#questionCount').textContent==='1 题');
  assert.match(await page.locator('.qbox').innerText(),/消息消费幂等/);
  assert.equal(await page.locator('#selfBar').isVisible(),false);
  await page.reload();await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
  await page.waitForFunction(()=>document.querySelector('#audioTestMode').checked);
  assert.equal(await page.locator('#deviceSel').isDisabled(),true);
  assert.equal(await page.locator('#micSel').inputValue(),'24');
  await page.getByRole('button',{name:'停止采集'}).click();
  await page.locator('#audioTestMode:not([disabled])').waitFor();
  await page.getByRole('checkbox',{name:'麦克风提问测试'}).uncheck();
  await page.waitForFunction(()=>!document.querySelector('#deviceSel').disabled);
  await page.getByRole('button',{name:'开始监听',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#captureStatus').textContent==='双通道采集中');
  await page.request.post(base+'/__regression__/voice',{data:{source:'mic',text:'我使用唯一业务键处理重复消费'}});
  await page.locator('.bub.me').filter({hasText:'唯一业务键'}).waitFor();
  assert.equal(await page.locator('#questionCount').textContent(),'1 题');
  assert.deepEqual(errors,[]);
});

test('test switch fits narrow viewports and a rejected change restores the checkbox',{skip},async t=>{
  const page=await browser.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  t.after(async()=>{await reset(page.request);await page.close();});
  await reset(page.request);await page.goto(base);
  await page.locator('#audioTestMode:not([disabled])').waitFor();
  for(const viewport of [{width:1365,height:900},{width:390,height:844},{width:320,height:740}]){
    await page.setViewportSize(viewport);
    const checkbox=page.getByRole('checkbox',{name:'麦克风提问测试'});
    await checkbox.scrollIntoViewIfNeeded();
    assert.equal(await checkbox.evaluate(el=>{const r=el.getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===el;}),true);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    if(process.env.INTERVIEW_SCREENSHOT_DIR){
      fs.mkdirSync(process.env.INTERVIEW_SCREENSHOT_DIR,{recursive:true});
      await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,`mic-test-${viewport.width}.png`),fullPage:true});
    }
  }
  await page.route('**/api/audio/test-mode',route=>route.fulfill({status:409,json:{ok:false,error:'请先暂停采集'}}));
  await page.getByRole('checkbox',{name:'麦克风提问测试'}).check();
  await page.locator('#captureNotice.error').filter({hasText:'请先暂停采集'}).waitFor();
  await page.waitForFunction(()=>!document.querySelector('#audioTestMode').checked&&!document.querySelector('#audioTestMode').disabled);
  assert.equal(await page.getByRole('checkbox',{name:'麦克风提问测试'}).isChecked(),false);
  assert.equal(await page.getByRole('checkbox',{name:'麦克风提问测试'}).isEnabled(),true);
  assert.deepEqual(errors,[]);
});
