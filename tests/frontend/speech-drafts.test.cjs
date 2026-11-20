const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const base=process.env.INTERVIEW_PREVIEW_URL,skip=!base;
let browser;
before(async()=>{if(!skip)browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH||undefined});});
after(async()=>{await browser?.close();});

test('draft snapshot survives reload; editing holds automatic submit and submits corrected text once',{skip},async t=>{
  const page=await browser.newPage({viewport:{width:1365,height:900}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));t.after(()=>page.close());
  await page.request.post(base+'/api/clear');
  await page.goto(base);await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
  await page.request.post(base+'/__regression__/speech-draft',{data:{text:'错误术语和不完整问题',reason:'uncertain_recognition'}});
  await page.getByRole('textbox',{name:'面试官问题草稿'}).waitFor();
  assert.equal(await page.locator('#questionCount').textContent(),'0 题');
  await page.reload();await page.getByRole('textbox',{name:'面试官问题草稿'}).waitFor();
  assert.equal(await page.getByRole('textbox',{name:'面试官问题草稿'}).inputValue(),'错误术语和不完整问题');
  for(const viewport of [{width:1365,height:900},{width:390,height:844},{width:320,height:740}]){
    await page.setViewportSize(viewport);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    for(const action of ['edit','submit','discard']){
      const button=page.locator(`[data-action="${action}"]`);await button.scrollIntoViewIfNeeded();
      assert.equal(await button.evaluate(el=>{const r=el.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return hit===el||el.contains(hit);}),true);
    }
    if(process.env.INTERVIEW_SCREENSHOT_DIR){
      fs.mkdirSync(process.env.INTERVIEW_SCREENSHOT_DIR,{recursive:true});
      await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,`speech-draft-${viewport.width}.png`),fullPage:true});
    }
  }
  await page.getByRole('button',{name:'编辑草稿',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('#speechDrafts textarea').readOnly);
  await page.getByRole('textbox',{name:'面试官问题草稿'}).fill('如何保证消息消费幂等？');
  await page.getByRole('button',{name:'确认提问',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#questionCount').textContent==='1 题');
  await page.locator('#speechDrafts').waitFor({state:'hidden'});
  assert.match(await page.locator('.qbox').innerText(),/消息消费幂等/);
  assert.deepEqual(errors,[]);
});

test('discard keeps questions unchanged and mode switch failures stay visible',{skip},async t=>{
  const page=await browser.newPage();t.after(()=>page.close());
  await page.request.post(base+'/api/clear');await page.goto(base);
  await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
  await page.request.post(base+'/__regression__/speech-draft',{data:{text:'无关语句'}});
  await page.getByRole('button',{name:'放弃草稿',exact:true}).click();
  await page.locator('#speechDrafts').waitFor({state:'hidden'});
  assert.equal(await page.locator('#questionCount').textContent(),'0 题');
  await page.route('**/api/speech/mode',route=>route.fulfill({status:503,json:{ok:false,error:'模型校验失败'}}));
  await page.getByRole('combobox',{name:'语音流程',exact:true}).selectOption('active');
  await page.locator('#captureNotice.error').filter({hasText:'模型校验失败'}).waitFor();
  assert.equal(await page.getByRole('combobox',{name:'语音流程',exact:true}).inputValue(),'legacy');
});
