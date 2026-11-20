const {test,before,after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const base = process.env.INTERVIEW_PREVIEW_URL, skip = !base;
let browser;
before(async()=>{if(!skip)browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH||undefined});});
after(async()=>{await browser?.close();});

for (const width of [1365,390]) test(`local recording review, playback and corrections at ${width}px`,{skip},async t=>{
  const context = await browser.newContext({viewport:{width,height:900}});
  t.after(()=>context.close());
  const page = await context.newPage(), errors = [];
  page.on('pageerror',error=>errors.push(error.message));
  await context.request.post(base+'/api/clear');
  const settings = await (await context.request.get(base+'/api/recordings/settings')).json();
  await context.request.post(base+'/api/recordings/settings',{data:{revision:settings.revision,enabled:true,auto_transcribe:true}});
  await context.request.post(base+'/__regression__/recording');
  await page.goto(base+'/');
  await page.locator('#btnEnd').click();
  await page.waitForURL(/\/history\?session=/);
  await page.locator('.recording-segment').first().waitFor();
  const section = page.locator('.recording-review');
  await page.waitForFunction(()=>document.querySelector('.recording-review')?.textContent.includes('已完成'));
  await section.locator('.recording-segment summary').first().click();
  const audio = section.locator('audio').first();
  assert.equal((await context.request.get(base + await audio.getAttribute('src'))).status(),200);
  const first = section.locator('.recording-segment').first();
  await first.getByRole('button',{name:'核对文本',exact:true}).click();
  await first.locator('textarea').fill('人工核对：库存不能超卖。');
  await first.getByRole('button',{name:'保存核对',exact:true}).click();
  await first.locator('.recording-review-state').filter({hasText:'已人工核对'}).waitFor();
  assert.match(await first.locator('.recording-review-state').textContent(),/已人工核对/);
  assert.match(await first.locator('.recording-text').textContent(),/人工核对/);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  if(process.env.INTERVIEW_SCREENSHOT_DIR){
    fs.mkdirSync(process.env.INTERVIEW_SCREENSHOT_DIR,{recursive:true});
    await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,`recording-review-${width}.png`),fullPage:true});
  }
  const exported = await (await context.request.get(base + await section.getByRole('link',{name:'导出转写与对照'}).getAttribute('href'))).json();
  assert.ok(exported.transcript.segments.some(s=>s.reviewed_text==='人工核对：库存不能超卖。'));
  if(process.env.INTERVIEW_SCREENSHOT_DIR && width === 1365){
    fs.writeFileSync(path.join(process.env.INTERVIEW_SCREENSHOT_DIR,'synthetic-recording-export.json'),
      JSON.stringify(exported,null,2));
  }
  assert.deepEqual(errors,[]);
});

test('recording settings expose only local models and persist independently',{skip},async t=>{
  const context = await browser.newContext({viewport:{width:390,height:844}});
  t.after(()=>context.close()); const page = await context.newPage();
  await page.goto(base+'/settings');
  await page.locator('#fields:not([disabled])').waitFor();
  await page.locator('#recordingSettings').scrollIntoViewIfNeeded();
  await page.locator('#recordingModel').selectOption('large-v3');
  await page.locator('#recordingDevice').selectOption('cpu');
  await page.locator('#recordingSave').click();
  await page.waitForFunction(()=>document.getElementById('recordingConfigState').textContent.includes('已保存'));
  const value = await (await context.request.get(base+'/api/recordings/settings')).json();
  assert.equal(value.model,'large-v3'); assert.equal(value.device,'cpu'); assert.equal(value.local_only,true);
  assert.equal(await page.locator('#recordingSettings input[type=url]').count(),0);
  await context.request.post(base+'/api/recordings/settings',{data:{revision:value.revision,device:'auto'}});
});
