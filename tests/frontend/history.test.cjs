const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

const base = process.env.INTERVIEW_PREVIEW_URL, phone = process.env.INTERVIEW_MOBILE_URL;
const skip = !base;
let browser;
before(async () => {
  if (!skip) browser = await chromium.launch({headless:true,
    executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined,
    channel:process.env.PLAYWRIGHT_EXECUTABLE_PATH ? undefined : process.env.PLAYWRIGHT_CHANNEL || undefined});
});
after(async () => { await browser?.close(); });

async function fixture(t, viewport = {width:1365,height:900}) {
  const context = await browser.newContext({viewport, permissions:['clipboard-read','clipboard-write']});
  t.after(() => context.close());
  const page = await context.newPage(), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  return {page, context, request:context.request, errors};
}
async function screenshot(page, name) {
  if (!process.env.INTERVIEW_SCREENSHOT_DIR) return;
  fs.mkdirSync(process.env.INTERVIEW_SCREENSHOT_DIR, {recursive:true});
  await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR, name + '.png'),fullPage:true});
}
async function waitForSaved(request, sid, predicate) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    const value = (await (await request.get(`${base}/api/history/session/${sid}`)).json()).session;
    if (predicate(value)) return value;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error('Synthetic answer was not persisted');
}
function session(id, question = '历史问题 ' + id) {
  return {id, title:'合成会话 ' + id, started_at:'2026-11-17 10:00:00',end_at:'2026-11-17 10:30:00',
    turns:[{id:id * 10,ts:'10:01:00',question,quick_card:'【开口】旧记录原文 <script>window.injected=true</script>',
      quick_document:null,quick_document_status:'legacy',quick_meta:null,details:[]}]};
}
function row(value) { return {...value,turns:value.turns.length,preview:value.turns[0].question}; }
async function mockSession(page, value) {
  await page.route('**/api/history/sessions?*', route => route.fulfill({json:{ok:true,sessions:[row(value)],has_more:false}}));
  await page.route('**/api/history/session/*', route => route.fulfill({json:{ok:true,session:value}}));
}

test('history reads real persisted answers, sources and details, searches and exports the original text', {skip}, async t => {
  const {page,request,errors} = await fixture(t);
  t.after(() => fetch(base + '/api/clear', {method:'POST'}));
  await request.post(base + '/api/clear');
  const question = '[regression:sources] 历史复盘：接口优化的实际依据（我的项目经历）';
  const asked = await (await request.post(base + '/api/ask',{data:{text:question}})).json();
  await waitForSaved(request, asked.session_id, value => value.turns[0]?.quick_document);
  const detail = await request.post(base + '/api/detail',{data:{turn_id:asked.turn_id,direction:'验证方法'}});
  assert.equal(detail.status(), 200);
  const saved = await waitForSaved(request, asked.session_id, value => value.turns[0]?.details.length === 1);
  await page.goto(base + '/history');
  await page.getByRole('searchbox').fill('历史复盘：接口优化');
  await page.locator(`[data-session-id="${asked.session_id}"]`).click();
  await page.getByRole('button',{name:'查看来源 〔context:resume〕'}).click();
  assert.match(await page.locator('.source-excerpt').first().textContent(), /角色为接口开发/);
  assert.match(await page.locator('.answer-toolbar').first().textContent(), /offline-primary/);
  await page.locator('.history-detail>summary').click();
  assert.match(await page.locator('.history-detail').textContent(), /合成内容属于问题/);
  await page.getByRole('button',{name:'复制回答',exact:true}).first().click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  assert.equal(copied.replace(/\r\n/g, '\n'), saved.turns[0].quick_card);
  const downloadReady = page.waitForEvent('download');
  await page.getByRole('link',{name:'导出 Markdown'}).click();
  const download = await downloadReady;
  const exported = fs.readFileSync(await download.path(),'utf8');
  assert.equal(download.suggestedFilename(), `interview-${asked.session_id}.md`);
  assert.ok(exported.includes(saved.turns[0].quick_card));
  assert.ok(exported.includes(saved.turns[0].details[0].content));
  assert.match(exported, /角色为接口开发/);
  await page.reload();
  await page.locator('.history-turn').waitFor();
  assert.equal(new URL(page.url()).searchParams.get('session'), asked.session_id);
  await screenshot(page,'history-persisted-desktop');
  if (phone) assert.equal((await request.get(phone + '/history')).status(), 404);
  assert.deepEqual(errors, []);
});

test('history pagination, literal search and legacy or invalid records keep text visible', {skip}, async t => {
  const {page,errors} = await fixture(t, {width:320,height:740});
  const values = Array.from({length:35}, (_, index) => session(index + 1));
  values[0].turns[0].details = [{id:1,direction:'损坏来源记录',content:'深挖原文依然可读',document:null,document_status:'invalid',meta:null}];
  await page.route('**/api/history/sessions?*', route => {
    const url = new URL(route.request().url()), offset = Number(url.searchParams.get('offset'));
    const filtered = url.searchParams.get('q') ? [values[0]] : values;
    return route.fulfill({json:{ok:true,sessions:filtered.slice(offset, offset + 30).map(row),has_more:filtered.length > offset + 30}});
  });
  await page.route('**/api/history/session/*', route => route.fulfill({json:{ok:true,session:values[0]}}));
  await page.goto(base + '/history');
  await page.getByRole('button',{name:'更多会话'}).click();
  await page.waitForFunction(() => document.querySelectorAll('.session-row').length === 35);
  await page.getByRole('searchbox').fill('50%_');
  await page.waitForFunction(() => document.querySelectorAll('.session-row').length === 1);
  await page.locator('.session-row').click();
  await page.locator('.history-detail>summary').click();
  assert.match(await page.locator('#reviewContent').textContent(), /原始记录/);
  assert.match(await page.locator('#reviewContent').textContent(), /来源记录不可用 · 保留原文/);
  assert.match(await page.locator('.history-answer-body').first().textContent(), /<script>/);
  assert.equal(await page.evaluate(() => !!window.injected), false);
  await screenshot(page,'history-legacy-320');
  await page.getByRole('button',{name:'返回会话列表'}).click();
  assert.equal(new URL(page.url()).searchParams.has('session'), false);
  await page.reload();
  assert.equal(await page.locator('#sessionsPanel').isVisible(), true);
  assert.deepEqual(errors, []);
});

test('history narrow error view can return to list and retry a failed list request', {skip}, async t => {
  const {page,errors} = await fixture(t, {width:390,height:844});
  let fail = true;
  await page.route('**/api/history/sessions?*', route => fail
    ? route.fulfill({status:503,json:{error:'合成存储暂不可用'}})
    : route.fulfill({json:{sessions:[],has_more:false}}));
  await page.route('**/api/history/session/*', route => route.fulfill({status:404,json:{error:'not found'}}));
  await page.goto(base + '/history?session=999999');
  await page.locator('#reviewState').filter({hasText:'会话不存在'}).waitFor();
  await page.getByRole('button',{name:'返回会话列表'}).click();
  assert.match(await page.locator('#listState').textContent(), /合成存储暂不可用/);
  fail = false;
  await page.getByRole('button',{name:'刷新历史'}).click();
  await page.locator('#listState').filter({hasText:'暂无会话'}).waitFor();
  assert.equal(await page.locator('#sessionsPanel').isVisible(), true);
  assert.deepEqual(errors, []);
});

test('history ignores a delayed response for a previously selected session', {skip}, async t => {
  const {page,errors} = await fixture(t);
  const first = session(1), second = session(2);
  let received, release;
  const started = new Promise(resolve => { received = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  await page.route('**/api/history/sessions?*', route => route.fulfill({json:{sessions:[row(first),row(second)],has_more:false}}));
  await page.route('**/api/history/session/*', async route => {
    const value = route.request().url().endsWith('/1') ? first : second;
    if (value === first) { received(); await held; }
    await route.fulfill({json:{session:value}}).catch(() => {});
  });
  await page.goto(base + '/history');
  await page.locator('[data-session-id="1"]').click(); await started;
  await page.locator('[data-session-id="2"]').click();
  await page.locator('#sessionTitle').filter({hasText:'合成会话 2'}).waitFor();
  release();
  await page.getByRole('button',{name:'刷新历史'}).click();
  await page.locator('#sessionTitle').filter({hasText:'合成会话 2'}).waitFor();
  assert.match(await page.locator('.history-turn').textContent(), /历史问题 2/);
  assert.deepEqual(errors, []);
});

test('history long question, source and code fit desktop, narrow and 200-percent equivalent viewports', {skip}, async t => {
  const {page,errors} = await fixture(t);
  const value = session(88, '在约束发生变化时，如何说明索引优化方案的适用范围和验证方法？' + 'LongIdentifier'.repeat(12));
  const turn = value.turns[0], long = 'long_identifier_'.repeat(50);
  turn.quick_card = '【开口】先判断访问模式，再结合执行计划验证收益。';
  turn.quick_document_status = 'stored';
  turn.quick_document = {version:1,level:'quick',directions:[],findings:[],
    blocks:[{kind:'conclusion',text:turn.quick_card,html:'<p>先判断访问模式，再结合执行计划验证收益。</p>'},
      {kind:'point',title:'验证代码',text:long,html:`<pre><code>${long}</code></pre>`}],
    sources:[{source:'notes:' + '很长的资料文件名'.repeat(12) + '.md',kind:'reference',lines:[1,2],excerpt:'用于布局回归的合成摘录。'.repeat(80)}]};
  await mockSession(page,value);
  await page.goto(base + '/history?session=88');
  await page.locator('.history-turn').waitFor();
  await page.locator('.answer-sources>summary').click();
  await page.locator('.source-item>summary').click();
  for (const [name,width,height] of [['desktop',1365,900],['390',390,844],['320',320,740],['zoom-200-equivalent',683,450]]) {
    await page.setViewportSize({width,height});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await page.evaluate(() => {
      const panel = document.querySelector('#reviewContent'); return panel.scrollWidth <= panel.clientWidth + 1;
    }), true);
    const bounds = await page.getByRole('link',{name:'导出 Markdown'}).boundingBox();
    assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= width);
    await screenshot(page,'history-long-' + name);
  }
  assert.deepEqual(errors, []);
});
