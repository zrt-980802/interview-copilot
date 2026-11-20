const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

const base = process.env.INTERVIEW_PREVIEW_URL;
const mobile = process.env.INTERVIEW_MOBILE_URL;
const skip = !base || !mobile;
let browser;

before(async () => {
  if (!skip) browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH});
});
after(async () => { await browser?.close(); });

async function screenshot(page, name) {
  const dir = process.env.INTERVIEW_SCREENSHOT_DIR;
  if (!dir) return;
  fs.mkdirSync(dir, {recursive:true});
  await page.screenshot({path:path.join(dir, name + '.png')});
}

for (const viewport of [{width:1365,height:768},{width:390,height:844}]) {
  test(`capture, reading position and phone pairing survive view switches at ${viewport.width}px`, {skip}, async t => {
    const page = await browser.newPage({viewport});
    const errors = [], writes = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.method() === 'POST') writes.push(new URL(request.url()).pathname);
    });
    t.after(() => page.close());
    const request = page.request;
    const state = () => request.get(base+'/api/state').then(response => response.json());
    const snapshot = () => request.get(base+'/api/session/snapshot').then(response => response.json());
    assert.equal((await request.get(base+'/api/health').then(response => response.json())).preview,true);
    for (const source of ['loopback','mic']) await request.post(base+'/api/stop',{data:{source}});
    t.after(async () => {
      for (const source of ['loopback','mic']) await fetch(base+'/api/stop',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({source})});
    });
    await request.post(base+'/api/clear');
    const code = await request.post(base+'/api/mobile/pairing').then(response => response.json());
    const paired = await request.post(mobile+'/api/pair',{data:{code:code.code}});
    assert.equal(paired.ok(),true);
    await page.goto(base);
    await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
    await page.locator('#prepOutput').filter({hasText:'尚未检测声音'}).waitFor();
    await page.locator('#prepModel').filter({hasText:/连接未检查|未配置/}).waitFor();
    await page.locator('#prepRecording').filter({hasText:/本地归档|已关闭/}).waitFor();
    assert.equal(await page.locator('#feed').evaluate(feed=>feed.scrollTop),0);
    assert.ok(await page.locator('#prepOutput').evaluate(element=>{
      const box=element.getBoundingClientRect();
      return box.top>=element.closest('#feed').getBoundingClientRect().top&&box.bottom<=innerHeight;
    }));
    assert.deepEqual(writes,[]);
    await screenshot(page,`preparation-${viewport.width}`);
    await page.locator('#btnStart').click();
    await page.locator('#captureStatus').filter({hasText:'双通道采集中'}).waitFor();
    await page.locator('#recordingState').filter({hasText:'正在归档'}).waitFor();
    await request.post(base+'/api/ask',{data:{text:'请说明消息队列重复消费、请求幂等和异常恢复的边界。'.repeat(3)}});
    await page.locator('.quickcard .opener').waitFor();
    await page.waitForFunction(async () => {
      const response=await fetch('/api/session/snapshot');
      return (await response.json()).turns[0]?.answers[0]?.status === 'complete';
    });
    await request.post(base+'/api/ask',{data:{text:'[regression:block] 如果库存与订单分别使用独立数据库，如何设计失败恢复？'}});
    await page.locator('.qa').nth(1).locator('.cancel-generation').waitFor();
    if (!(await page.locator('#questionSidebar').isVisible())) await page.locator('#toggleQuestions').click();
    await page.locator('#questionList button').first().click();
    const before = await snapshot(), modeBefore = await state(), writesBefore = writes.length;
    const readerBefore = await page.locator('.reader').boundingBox();
    await page.locator('#viewFocus').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#viewFocus').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('.app-nav').isVisible(),false);
    assert.equal(await page.locator('#captureSettings').isVisible(),false);
    assert.equal(await page.locator('#questionList button').first().getAttribute('aria-current'),'true');
    const readerAfter = await page.locator('.reader').boundingBox();
    assert.ok(readerAfter.y < readerBefore.y,'Focus view should move the reader up');
    await page.locator('#toggleQuestions').click();
    assert.equal(await page.locator('#questionSidebar').isVisible(),false);
    await screenshot(page,`focus-${viewport.width}`);
    await page.locator('#viewWorkbench').click();
    assert.equal(await page.locator('#questionSidebar').isVisible(),false);
    await page.locator('#toggleQuestions').click();
    assert.equal(await page.locator('#questionList button').first().getAttribute('aria-current'),'true');
    const after = await snapshot(), modeAfter = await state();
    assert.equal(after.session_id,before.session_id);
    assert.deepEqual(after.turns.map(turn => turn.turn_id), before.turns.map(turn => turn.turn_id));
    assert.equal(after.turns[1].answers[0].task_id,before.turns[1].answers[0].task_id);
    assert.deepEqual([modeAfter.listening,modeAfter.self_listening,modeAfter.speech.mode],
                     [modeBefore.listening,modeBefore.self_listening,modeBefore.speech.mode]);
    assert.equal(writes.length,writesBefore);
    assert.equal((await request.get(mobile+'/api/snapshot')).status(),200);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    await screenshot(page,`workbench-${viewport.width}`);
    await page.locator('#viewFocus').click();
    await page.reload();
    await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
    assert.equal(await page.locator('#viewFocus').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('#questionSidebar').isVisible(),true);
    await request.post(base+'/__regression__/disconnect');
    await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
    assert.equal(await page.locator('.qa').count(),2);
    assert.deepEqual(errors,[]);
  });
}

test('quiet capture, unavailable input and distinct draft reasons use separate UI states', {skip}, async t => {
  const page = await browser.newPage({viewport:{width:390,height:844}});
  t.after(() => page.close());
  await page.request.post(base+'/api/clear');
  let failed = [];
  await page.route('**/api/state', async route => {
    const response = await route.fetch();
    const state = await response.json();
    await route.fulfill({response,json:{...state, listening:true, self_listening:false,
      capture_failed:failed, engine:{...state.engine,speaking:false}}});
  });
  await page.goto(base);
  await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
  await page.locator('#captureSignal').waitFor();
  assert.equal(await page.locator('#captureIssue').isVisible(),false);
  await page.request.post(base+'/api/ask',{data:{text:'如何把断点续传和重复提交分开处理？'}});
  await page.locator('.quickcard .opener').waitFor();
  await page.locator('#viewFocus').click();
  await page.request.post(base+'/__regression__/speech-draft',{data:{state:'collecting',text:'补充条件'}});
  await page.locator('#speechDrafts').filter({hasText:'正在听'}).waitFor();
  assert.equal(await page.locator('#speechDrafts textarea').isVisible(),false);
  const cases = [
    ['low_audio_level', '音量过低'],
    ['endpoint_low', '尚未检测到完整结束'],
    ['recognition_failed', '转写失败'],
    ['audio_gap', '音频存在缺口'],
  ];
  for (const [reason, label] of cases) {
    await page.request.post(base+'/__regression__/speech-draft',{data:{state:'needs_review',text:'补充：不使用共享数据库。',reason}});
    await page.locator('.draft-heading [role="status"]').filter({hasText:label}).waitFor();
    await page.locator('#speechDrafts textarea').waitFor();
  }
  await screenshot(page,'review-390');
  failed = ['mic'];
  await page.locator('#captureIssue').filter({hasText:'麦克风采集失败'}).waitFor();
  assert.match(await page.locator('.qbox').textContent(),/断点续传/);
  await screenshot(page,'capture-error-390');
  await page.getByRole('button',{name:'查看声音设置',exact:true}).click();
  assert.equal(await page.locator('#captureSettings').isVisible(),true);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
});
