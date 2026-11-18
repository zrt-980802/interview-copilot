const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const desktopUrl = process.env.INTERVIEW_PREVIEW_URL, phoneUrl = process.env.INTERVIEW_MOBILE_URL;
const skip = !desktopUrl || !phoneUrl;
let browser;
before(async () => {
  if (!skip) browser = await chromium.launch({headless:true,
    executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined,
    channel:process.env.PLAYWRIGHT_EXECUTABLE_PATH ? undefined : process.env.PLAYWRIGHT_CHANNEL || undefined});
});
after(async () => { await browser?.close(); });

async function fixture(t, {desktop = false} = {}) {
  const context = await browser.newContext({viewport:{width:390,height:844}});
  t.after(() => context.close());
  const phone = await context.newPage(), errors = [];
  phone.on('pageerror', error => errors.push(error.message));
  await phone.addInitScript(() => {
    window.mediaCalls = 0;
    if (navigator.mediaDevices) {
      navigator.mediaDevices.getUserMedia = () => { window.mediaCalls++; throw new Error('Unexpected phone media request'); };
      navigator.mediaDevices.getDisplayMedia = () => { window.mediaCalls++; throw new Error('Unexpected phone screen request'); };
    }
  });
  const request = context.request;
  await request.post(desktopUrl + '/api/clear');
  await request.post(desktopUrl + '/__regression__/next-pairing-window');
  let computer;
  if (desktop) {
    computer = await context.newPage(); await computer.setViewportSize({width:1365,height:900});
    computer.on('pageerror', error => errors.push(error.message)); await computer.goto(desktopUrl);
    await computer.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
  }
  await phone.goto(phoneUrl);
  await phone.locator('#pairing:not([hidden])').waitFor();
  const issue = async () => (await (await request.post(desktopUrl + '/api/mobile/pairing')).json()).code;
  const pair = async code => {
    await phone.locator('#code').fill(code || await issue());
    await phone.getByRole('button',{name:'配对',exact:true}).click();
    await phone.locator('#connection').filter({hasText:'已连接'}).waitFor();
  };
  const ask = async text => {
    const response = await request.post(desktopUrl + '/api/ask', {data:{text}});
    assert.equal(response.status(), 200); return response.json();
  };
  const complete = async count => {
    await phone.waitForFunction(count => document.querySelector('#turns').options.length === count, count);
    await phone.locator('.answer-status').filter({hasText:'已完成'}).waitFor();
  };
  return {context, phone, computer, request, errors, pair, ask, complete};
}

async function screenshot(page, name) {
  if (!process.env.INTERVIEW_SCREENSHOT_DIR) return;
  fs.mkdirSync(process.env.INTERVIEW_SCREENSHOT_DIR, {recursive:true});
  await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR, name + '.png'), fullPage:true});
}

test('desktop pairing QR and phone code grant share one answer without auto recording', {skip}, async t => {
  const {context, phone, computer, request, errors, pair, ask, complete} = await fixture(t, {desktop:true});
  const started = [];
  computer.on('request', req => { if (req.url().endsWith('/api/start')) started.push(req.url()); });
  await computer.reload();
  await computer.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
  assert.deepEqual(started, []);
  await computer.getByRole('button',{name:'手机显示',exact:true}).click();
  await computer.locator('#phoneState').filter({hasText:'仅本机预览'}).waitFor();
  await computer.getByRole('button',{name:'生成配对码'}).click();
  await computer.waitForFunction(() => document.querySelector('#phoneQr').naturalWidth > 0);
  await screenshot(computer, 'pairing-desktop');
  await pair(await computer.locator('#phoneCode').textContent());
  const cookie = (await context.cookies(phoneUrl)).find(item => item.name === 'copilot_viewer');
  assert.equal(cookie.httpOnly, true); assert.equal(cookie.sameSite, 'Strict');
  await computer.locator('#phoneClose').click();
  await ask('[regression:sources] 介绍你做过的优化'); await complete(1);
  assert.match(await computer.locator('.qbox').textContent(), /介绍你做过的优化/);
  assert.match(await phone.locator('#question').textContent(), /介绍你做过的优化/);
  assert.equal(await phone.locator('.answer-sources').count(), 0);
  assert.equal(await phone.locator('.citation-note').textContent(), '[材料]');
  const projection = await (await request.get(phoneUrl + '/api/snapshot')).json();
  assert.deepEqual(projection.turns[0].answers[0].document.sources, []);
  assert.equal(await phone.evaluate(() => window.mediaCalls), 0);
  await screenshot(computer, 'workspace-desktop');
  await screenshot(phone, 'phone-answer');
  assert.deepEqual(errors, []);
});

test('phone keeps old selection across new questions, reconnect, reload and desktop closure', {skip}, async t => {
  const {phone, computer, request, errors, pair, ask, complete} = await fixture(t, {desktop:true});
  await pair(); await ask('第一个问题'); await complete(1); await ask('第二个问题'); await complete(2);
  const first = await phone.locator('#turns option').first().getAttribute('value');
  await phone.locator('#turns').selectOption(first);
  await ask('第三个问题');
  await phone.waitForFunction(() => document.querySelector('#turns').options.length === 3);
  assert.equal(await phone.locator('#question').textContent(), '第一个问题');
  await request.post(desktopUrl + '/__regression__/disconnect');
  await phone.locator('#connection').filter({hasText:'已连接'}).waitFor();
  assert.equal(await phone.locator('#question').textContent(), '第一个问题');
  await phone.getByRole('button',{name:'跟随最新题'}).click();
  await phone.locator('#question').filter({hasText:'第三个问题'}).waitFor();
  assert.equal(await phone.locator('#question').textContent(), '第三个问题');
  await computer.close(); await phone.reload();
  await phone.locator('#connection').filter({hasText:'已连接'}).waitFor();
  assert.equal(await phone.locator('#turns option').count(), 3);
  await ask('桌面关闭后的问题'); await complete(4);
  assert.equal(await phone.locator('#question').textContent(), '桌面关闭后的问题');
  assert.equal(await phone.locator('#answers .answer-entry').count(), 1);
  assert.deepEqual(errors, []);
});

test('revocation and session reset clear the visible answer and reject the old cookie', {skip}, async t => {
  const {phone, request, errors, pair, ask, complete} = await fixture(t);
  for (const route of ['/api/mobile/revoke', '/api/clear']) {
    await pair(); await ask('撤销前的问题'); await complete(1);
    await request.post(desktopUrl + route);
    await phone.locator('#pairing:not([hidden])').waitFor();
    assert.equal(await phone.locator('#question').textContent(), '');
    assert.equal(await phone.locator('#answers').textContent(), '');
    assert.equal((await request.get(phoneUrl + '/api/snapshot')).status(), 401);
    await request.post(desktopUrl + '/api/clear');
  }
  assert.deepEqual(errors, []);
});

test('QR fragment is removed immediately and exchanged without leaking a token in the URL', {skip}, async t => {
  const {phone, request, errors} = await fixture(t);
  const code = (await (await request.post(desktopUrl + '/api/mobile/pairing')).json()).code;
  const seen = []; phone.on('request', req => seen.push(req.url()));
  await phone.goto(phoneUrl + '/#pair=' + code);
  await phone.locator('#connection').filter({hasText:'已连接'}).waitFor();
  assert.equal(new URL(phone.url()).hash, '');
  assert.ok(seen.every(url => !new URL(url).search.includes(code) && !url.includes('token=')));
  assert.equal(await phone.evaluate(() => document.cookie.includes('copilot_viewer')), false);
  await phone.getByRole('button',{name:'断开配对',exact:true}).click();
  await phone.locator('#pairing:not([hidden])').waitFor();
  assert.equal((await request.get(phoneUrl + '/api/snapshot')).status(), 401);
  assert.deepEqual(errors, []);
});

test('phone attempts and cancellation recover with the same task identity', {skip}, async t => {
  const {phone, request, errors, pair, ask, complete} = await fixture(t);
  await pair(); await ask('[regression:failover] 模型切换'); await complete(1);
  assert.doesNotMatch(await phone.locator('#answers').textContent(), /Discarded primary fragment/);
  assert.match(await phone.locator('.answer-status').textContent(), /备用模型/);
  await ask('[regression:block] 保留已生成内容');
  await phone.locator('#question').filter({hasText:'保留已生成内容'}).waitFor();
  await phone.locator('#answers').filter({hasText:'先明确目标'}).waitFor();
  const snapshot = await (await request.get(desktopUrl + '/api/session/snapshot')).json();
  const turn = snapshot.turns.at(-1), task = turn.answers[0];
  await request.post(desktopUrl + '/api/cancel', {data:{session_id:snapshot.session_id,task_id:task.task_id}});
  await phone.locator('.answer-status').filter({hasText:'已取消'}).waitFor();
  await phone.reload(); await phone.locator('.answer-status').filter({hasText:'已取消'}).waitFor();
  assert.match(await phone.locator('#answers').textContent(), /先明确目标/);
  assert.deepEqual(errors, []);
});

test('speech supplement revises one shared turn on desktop phone and history', {skip},async t => {
  const {phone,computer,request,errors,pair,complete}=await fixture(t,{desktop:true});
  await pair();
  const first='说明 Function Call 消息协议流，模型返回什么结构？';
  await request.post(desktopUrl+'/__regression__/voice',{data:{text:first}}); await complete(1);
  await request.post(desktopUrl+'/__regression__/voice',{data:{text:'结果以什么身份回传？'}});
  await phone.locator('#question').filter({hasText:'结果以什么身份回传'}).waitFor(); await complete(1);
  assert.equal(await computer.locator('.qa').count(),1);
  assert.match(await computer.locator('.qbox').textContent(),/模型返回什么结构.*结果以什么身份回传/);
  const state=await (await request.get(desktopUrl+'/api/session/snapshot')).json();
  const history=await (await request.get(desktopUrl+'/api/history/session/'+state.session_id)).json();
  assert.equal(history.session.turns.length,1);
  assert.equal(history.session.turns[0].question,first+' 结果以什么身份回传？');
  await phone.reload(); await phone.locator('#connection').filter({hasText:'已连接'}).waitFor();
  assert.equal(await phone.locator('#turns option').count(),1);
  assert.match(await phone.locator('#question').textContent(),/结果以什么身份回传/);
  assert.deepEqual(errors,[]);
});

test('phone narrow screens, large type, theme and focus stay within the viewport', {skip}, async t => {
  const {phone, errors, pair, ask, complete} = await fixture(t);
  await pair(); await ask('手机阅读与布局'); await complete(1);
  await phone.locator('#fontSize').fill('26');
  assert.equal(await phone.locator('#reader').evaluate(element => getComputedStyle(element).fontSize), '26px');
  for (const [width,height] of [[390,844],[320,740],[740,390]]) {
    await phone.setViewportSize({width,height});
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await phone.locator('#reader').evaluate(element => element.scrollWidth > element.clientWidth), false);
    await screenshot(phone, `phone-${width}x${height}-large`);
  }
  await phone.getByRole('button',{name:'切换明暗'}).click();
  assert.equal(await phone.locator('html').getAttribute('data-theme'), 'light');
  await phone.getByRole('button',{name:'专注显示'}).click();
  assert.equal(await phone.locator('body').evaluate(element => element.classList.contains('focused')), true);
  await phone.setViewportSize({width:390,height:844});
  await screenshot(phone, 'phone-light-focus');
  await phone.reload();
  await phone.locator('#connection').filter({hasText:'已连接'}).waitFor();
  assert.equal(await phone.locator('html').getAttribute('data-theme'), 'light');
  assert.equal(await phone.locator('#fontSize').inputValue(), '26');
  assert.deepEqual(errors, []);
});
