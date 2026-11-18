const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const base=process.env.INTERVIEW_PREVIEW_URL,skip=!base;
let browser;
before(async()=>{if(!skip)browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH||undefined});});
after(async()=>{await browser?.close();});
async function fixture(t,viewport={width:1365,height:900}){
  const page=await browser.newPage({viewport}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));t.after(()=>page.close());
  return {page,errors};
}
async function screenshot(page,name){
  if(!process.env.INTERVIEW_SCREENSHOT_DIR)return;
  fs.mkdirSync(process.env.INTERVIEW_SCREENSHOT_DIR,{recursive:true});
  await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,name+'.png'),fullPage:true});
}
async function reachable(locator){
  await locator.waitFor({state:'visible'});
  assert.equal(await locator.evaluate(element=>{
    const r=element.getBoundingClientRect(),hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
    return r.width>0&&r.height>0&&r.left>=0&&r.right<=innerWidth+1&&r.top>=0&&r.bottom<=innerHeight+1
      &&(hit===element||element.contains(hit));
  }),true,await locator.innerText());
}

test('named primary navigation connects every desktop page without returning to the workspace',{skip},async t=>{
  const {page,errors}=await fixture(t);
  const links=[['面试工作区','/'],['资料库','/files'],['历史复盘','/history'],['模型设置','/settings']];
  for(const viewport of [{width:1365,height:900},{width:390,height:844},{width:320,height:740}]){
    await page.setViewportSize(viewport);await page.goto(base);
    for(const [label,route] of [...links.slice(1),links[0]]){
      const navigation=page.getByRole('navigation',{name:'主导航'});
      for(const [name] of links)await reachable(navigation.getByRole('link',{name,exact:true}));
      await navigation.getByRole('link',{name:label,exact:true}).click();
      assert.equal(new URL(page.url()).pathname,route);
      assert.equal(await navigation.locator('[aria-current=page]').textContent(),label);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    }
  }
  assert.deepEqual(errors,[]);
});

test('workspace main commands are named and reachable on desktop and narrow screens',{skip},async t=>{
  const {page,errors}=await fixture(t);
  assert.equal((await(await page.request.get(base+'/api/health')).json()).preview,true);
  await page.request.post(base+'/api/clear');
  for(const [name,width,height] of [['desktop',1365,900],['narrow',390,844],['small',320,740]]){
    await page.setViewportSize({width,height});await page.goto(base);
    await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
    await page.locator('#readerEmpty').filter({hasText:'等待新问题'}).waitFor();
    for(const id of ['btnStart','btnStop','btnResume','btnPhone','btnClear','btnRecal'])await reachable(page.locator('#'+id));
    await reachable(page.locator('#manualInput'));await reachable(page.locator('#btnAsk'));
    assert.match(await page.locator('#btnStart').innerText(),/开始监听/);
    assert.match(await page.locator('#btnPhone').innerText(),/手机显示/);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await screenshot(page,'workspace-controls-'+name);
  }
  await page.setViewportSize({width:740,height:390});
  await page.locator('#manualInput').scrollIntoViewIfNeeded();await reachable(page.locator('#manualInput'));
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(errors,[]);
});

test('provider save action stays on screen while scrolling primary and fallback settings',{skip},async t=>{
  const {page,errors}=await fixture(t,{width:390,height:844});
  await page.goto(base+'/settings');await page.locator('#fields:not([disabled])').waitFor();
  await page.locator('#fallback_enabled').check();
  await page.locator('#base_url').scrollIntoViewIfNeeded();await reachable(page.locator('#save'));
  await page.locator('#fallback_model').scrollIntoViewIfNeeded();await reachable(page.locator('#save'));
  await page.locator('#fields .settings-scroll').evaluate(element=>element.scrollTop=0);
  await reachable(page.locator('#save'));
  assert.equal(await page.locator('#quick_model').isVisible(),true);
  assert.deepEqual(errors,[]);
});
