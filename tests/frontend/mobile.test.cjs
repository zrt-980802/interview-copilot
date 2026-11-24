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
  await request.post(desktopUrl + '/api/mobile/revoke');
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

test('phone shows pending speech state across reload without exposing draft text',{skip},async t=>{
  const {phone,request,pair,ask,complete,errors}=await fixture(t);
  await pair();await ask('如何避免重复消费？');await complete(1);
  await request.post(desktopUrl+'/__regression__/speech-draft',{data:{state:'needs_review',reason:'low_audio_level',text:'草稿保密内容918'}});
  await phone.locator('#speechStatus').filter({hasText:'待确认'}).waitFor();
  assert.match(await phone.locator('#speechStatus').innerText(),/音量过低/);
  assert.doesNotMatch(await phone.locator('body').innerText(),/草稿保密内容918/);
  const snapshot=await(await request.get(phoneUrl+'/api/snapshot')).json();
  assert.doesNotMatch(JSON.stringify(snapshot),/草稿保密内容918/);
  await phone.reload();await phone.locator('#connection').filter({hasText:'已连接'}).waitFor();
  await phone.locator('#speechStatus').filter({hasText:'待确认'}).waitFor();
  await screenshot(phone,'phone-speech-pending');
  await request.post(desktopUrl+'/__regression__/speech-draft',{data:{state:'empty',text:''}});
  await phone.locator('#speechStatus').waitFor({state:'hidden'});
  assert.deepEqual(errors,[]);
});

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
  if (process.env.INTERVIEW_SCREENSHOT_DIR) {
    await phone.waitForFunction(() => window.ViewerPerformance.renderTimings.length > 0);
    const desktop = await (await request.get(desktopUrl + '/api/session/snapshot')).json();
    const answer = desktop.turns[0].answers[0].document.timings_ms;
    assert.ok(answer.retrieval_ms >= 0 && answer.task_to_complete_ms >= answer.task_to_first_content_ms);
    const browser = await phone.evaluate(() => window.ViewerPerformance.renderTimings);
    fs.writeFileSync(path.join(process.env.INTERVIEW_SCREENSHOT_DIR, 'paired-stage-timings.json'),
      JSON.stringify({answer_ms: answer, browser_ms: browser}, null, 2));
  }
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

test('revocation clears the visible answer and rejects the old cookie', {skip}, async t => {
  const {phone, request, errors, pair, ask, complete} = await fixture(t);
  await pair(); await ask('撤销前的问题'); await complete(1);
  await request.post(desktopUrl + '/api/mobile/revoke');
  await phone.locator('#pairing:not([hidden])').waitFor();
  assert.equal(await phone.locator('#question').textContent(), '');
  assert.equal(await phone.locator('#answers').textContent(), '');
  assert.equal((await request.get(phoneUrl + '/api/snapshot')).status(), 401);
  assert.deepEqual(errors, []);
});

test('new sessions keep the same paired phone and follow new questions across reconnects', {skip}, async t => {
  const {context, phone, computer, request, errors, pair, ask, complete} = await fixture(t, {desktop:true});
  await pair(); await ask('旧会话的问题'); await complete(1);
  await phone.locator('#turns').selectOption(await phone.locator('#turns option').first().getAttribute('value'));
  const cookie = (await context.cookies(phoneUrl)).find(item => item.name === 'copilot_viewer').value;
  await computer.reload();
  await request.post(desktopUrl + '/api/mobile/pairing');
  assert.equal((await request.get(phoneUrl + '/api/snapshot')).status(),200);
  for (let index = 1; index <= 3; index++) {
    await request.post(desktopUrl + '/api/clear');
    await phone.waitForFunction(() => document.querySelector('#turns').options.length === 0);
    assert.equal(await phone.locator('#viewer').isVisible(),true);
    assert.equal(await phone.locator('#question').textContent(),'');
    assert.equal(await phone.locator('#answers').textContent(),'');
    assert.equal(await phone.locator('#latest').getAttribute('aria-pressed'),'true');
    assert.equal((await request.get(desktopUrl + '/api/mobile')).status(),200);
    const snapshot = await (await request.get(phoneUrl + '/api/snapshot')).json();
    assert.deepEqual(snapshot.turns,[]);
    if (index === 2) {
      await request.post(desktopUrl + '/__regression__/disconnect');
      await phone.reload();
      await phone.locator('#connection').filter({hasText:'已连接'}).waitFor();
    }
    await ask('新会话的问题 ' + index); await complete(1);
    assert.equal(await phone.locator('#question').textContent(),'新会话的问题 ' + index);
    assert.equal((await context.cookies(phoneUrl)).find(item => item.name === 'copilot_viewer').value,cookie);
  }
  assert.equal((await (await request.get(desktopUrl + '/api/mobile')).json()).devices,1);
  await screenshot(phone,'phone-pairing-after-new-session');
  assert.deepEqual(errors,[]);
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
  const original=(await(await request.get(desktopUrl+'/api/session/snapshot')).json()).turns[0];
  await request.post(desktopUrl+'/__regression__/voice',{data:{text:'结果以什么身份回传？'}});
  await phone.locator('#question').filter({hasText:'结果以什么身份回传'}).waitFor(); await complete(1);
  assert.equal(await computer.locator('.qa').count(),1);
  assert.match(await computer.locator('.qbox').textContent(),/模型返回什么结构.*结果以什么身份回传/);
  await computer.getByText('补充后的完整问题',{exact:true}).waitFor();
  await phone.locator('#number').filter({hasText:'已补充'}).waitFor();
  const state=await (await request.get(desktopUrl+'/api/session/snapshot')).json();
  assert.notEqual(state.turns[0].answers[0].task_id,original.answers[0].task_id);
  const history=await (await request.get(desktopUrl+'/api/history/session/'+state.session_id)).json();
  assert.equal(history.session.turns.length,1);
  assert.equal(history.session.turns[0].question,first+' 结果以什么身份回传？');
  assert.deepEqual(history.session.turns[0].revisions,[{
    question:first,answer:original.answers[0].content,answer_status:'complete'
  }]);
  await computer.reload();
  const previous=computer.locator('.revision-list');
  await previous.waitFor();
  assert.equal(await previous.getAttribute('open'),null);
  await previous.locator('summary').click();
  assert.match(await previous.locator('.revision-question').innerText(),/模型返回什么结构/);
  await computer.goto(desktopUrl+'/history?session='+state.session_id);
  await computer.getByText('补充后的完整问题',{exact:true}).waitFor();
  await computer.locator('.revision-list summary').click();
  assert.match(await computer.locator('.revision-answer').innerText(),/先明确目标/);
  await phone.reload(); await phone.locator('#connection').filter({hasText:'已连接'}).waitFor();
  assert.equal(await phone.locator('#turns option').count(),1);
  assert.match(await phone.locator('#question').textContent(),/结果以什么身份回传/);
  await phone.locator('#number').filter({hasText:'已补充'}).waitFor();
  assert.deepEqual(errors,[]);
});

test('background correction preserves the first answer and projects the revised question', {skip}, async t => {
  const {phone,computer,request,errors,pair,complete}=await fixture(t,{desktop:true});
  await pair();
  const first='如果可以使用缓存，你会如何防止重复处理？';
  const corrected='如果不可以使用缓存，你会如何防止重复处理？';
  const endpoint=desktopUrl+'/__regression__/speech-review';
  assert.equal((await request.post(endpoint,{data:{first,corrected}})).status(),200);
  await computer.getByText('后台复听中',{exact:true}).waitFor();
  await phone.locator('#number').filter({hasText:'后台复听中'}).waitFor();
  await complete(1);
  const original=(await (await request.get(desktopUrl+'/api/session/snapshot')).json()).turns[0];
  await request.post(endpoint,{data:{release:true}});
  await computer.getByText('复听修正后的问题',{exact:true}).waitFor();
  await phone.locator('#question').filter({hasText:corrected}).waitFor();
  await phone.locator('#number').filter({hasText:'已按复听修正'}).waitFor();
  await complete(1);
  const state=await (await request.get(desktopUrl+'/api/session/snapshot')).json();
  assert.equal(state.turns[0].turn_id,original.turn_id);
  assert.equal(state.turns[0].revisions[0].answer,original.answers[0].content);
  await computer.goto(desktopUrl+'/history?session='+state.session_id);
  await computer.getByText('复听依据',{exact:true}).click();
  await computer.locator('pre').filter({hasText:corrected}).waitFor();
  assert.deepEqual(errors,[]);
});

test('a new question answers while its review waits and then gets reviewed', {skip}, async t => {
  const {phone,computer,request,errors,pair,complete}=await fixture(t,{desktop:true});
  await pair();
  const endpoint=desktopUrl+'/__regression__/speech-review';
  const first='如何分析缓存命中率？';
  const next='下一个问题，如何检查数据库事务是否丢失？';
  const corrected='下一个问题，如何检查数据库事务是否丢失或重复？';
  await request.post(endpoint,{data:{draft:'old',first,corrected:'不应显示的旧复听文字'}});
  await computer.getByText('后台复听中',{exact:true}).waitFor();
  await request.post(endpoint,{data:{draft:'next',first:next,corrected}});
  await computer.getByText('等待后台复听',{exact:true}).waitFor();
  await phone.locator('#number').filter({hasText:'等待后台复听'}).waitFor();
  await complete(2);
  await request.post(endpoint,{data:{release:true}});
  await phone.locator('#question').filter({hasText:corrected}).waitFor();
  const state=await (await request.get(desktopUrl+'/api/session/snapshot')).json();
  assert.deepEqual(state.turns.map(turn => [turn.question,turn.review_status]),[[first,'late'],[corrected,'corrected']]);
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

test('background is silent and a corrected RAG scenario remains one turn on both displays', {skip}, async t => {
  const {phone,computer,request,errors,pair,complete} = await fixture(t,{desktop:true});
  await pair();
  const voice = text => request.post(desktopUrl + '/__regression__/voice',{data:{text}});
  await voice('我们后续有一次终面，然后两周线下培训。');
  const initial = await (await request.get(desktopUrl + '/api/session/snapshot')).json();
  assert.equal(initial.turns.length,0);
  const first = '假设财报切片都已入库，不知道你对这个IG了不了解';
  await voice(first); await complete(1);
  await voice('RAG');
  await voice('比如用户问三年利润率为什么下降，怎么找数据和计算？');
  await phone.locator('#question').filter({hasText:'怎么找数据和计算'}).waitFor();
  await complete(1);
  assert.equal(await computer.locator('.qa').count(),1);
  assert.match(await computer.locator('.qbox').textContent(),/IG.*RAG.*三年利润率/);
  const state = await (await request.get(desktopUrl + '/api/session/snapshot')).json();
  const history = await (await request.get(desktopUrl + '/api/history/session/' + state.session_id)).json();
  assert.equal(history.session.turns.length,1);
  assert.match(history.session.turns[0].question,/IG.*RAG.*怎么找数据和计算/);
  const projection = await (await request.get(phoneUrl + '/api/snapshot')).json();
  assert.equal(JSON.stringify(projection).includes('interviewer_background'),false);
  assert.deepEqual(errors,[]);
});
