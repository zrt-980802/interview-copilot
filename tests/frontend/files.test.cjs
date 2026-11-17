const {test,before,after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const base = process.env.INTERVIEW_PREVIEW_URL, phone = process.env.INTERVIEW_MOBILE_URL, skip = !base;
let browser;
before(async () => { if (!skip) browser = await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined,
  channel:process.env.PLAYWRIGHT_EXECUTABLE_PATH ? undefined : process.env.PLAYWRIGHT_CHANNEL || undefined}); });
after(async () => { await browser?.close(); });
async function fixture(t,viewport = {width:1365,height:900}) {
  const context = await browser.newContext({viewport}), page = await context.newPage(), errors = [];
  page.on('pageerror',error => errors.push(error.message)); t.after(() => context.close());
  return {page,request:context.request,errors};
}
async function screenshot(page,name) {
  if (!process.env.INTERVIEW_SCREENSHOT_DIR) return;
  fs.mkdirSync(process.env.INTERVIEW_SCREENSHOT_DIR,{recursive:true});
  await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,name + '.png'),fullPage:true});
}
const file = (name,text) => ({name,mimeType:'text/plain',buffer:Buffer.from(text)});
const identity = 'a'.repeat(32);
function record(overrides = {}) {
  return {id:identity,name:'架构设计参考.md',title:'架构设计参考',kind:'reference',extension:'.md',enabled:true,archived:false,
    status:'ready',error:'',bytes:1024,chars:150,chunks:2,created_at:'2026-11-17 12:00:00',updated_at:'2026-11-17 12:00:00',
    overview:'PostgreSQL 查询计划与索引适用范围。',preview:'PostgreSQL 查询计划与索引适用范围。',source_url:'',...overrides};
}

test('files batch upload initializes reference documents, previews, renames, switches scope and archives', {skip}, async t => {
  const {page,request,errors} = await fixture(t);
  await page.goto(base + '/files');
  const name = 'browser-reference-' + Date.now() + '.md';
  await page.locator('#fileInput').setInputFiles([
    file(name,'# PostgreSQL\nEXPLAIN ANALYZE executes queries and returns actual plan statistics.'),
    file('browser-reference-redis.txt','Redis documentation about expiry and cache consistency, used as reference.'),
    file('unsupported.exe','invalid format')]);
  await page.locator('#notice').filter({hasText:'不支持'}).waitFor();
  await page.locator('#fileDetail:not([hidden])').waitFor();
  await page.locator('#initialization').filter({hasText:'可以参考'}).waitFor();
  await page.waitForFunction(() => document.querySelector('#fileMeta').textContent.includes('已就绪'));
  await page.getByRole('button',{name:'正文',exact:true}).click();
  assert.match(await page.locator('#preview').textContent(),/EXPLAIN ANALYZE/);
  const id = new URL(page.url()).searchParams.get('file');
  await page.locator('#fileTitle').fill('查询计划参考');
  await page.getByRole('button',{name:'保存属性'}).click();
  await page.locator(`[data-file-id="${id}"] .file-select`).filter({hasText:'查询计划参考'}).waitFor();
  const checkbox = page.locator(`[data-file-id="${id}"] input[type=checkbox]`);
  await checkbox.uncheck();
  await page.waitForFunction(async id => !(await (await fetch('/api/files/' + id)).json()).file.enabled,id);
  await page.getByRole('button',{name:'归档文件'}).click();
  await page.getByRole('button',{name:'已归档',exact:true}).click();
  await page.locator(`[data-file-id="${id}"]`).waitFor();
  assert.match((await request.get(`${base}/api/files/${id}/download`)).headers()['content-disposition'],/attachment/);
  await page.getByRole('button',{name:'恢复文件'}).click();
  await page.getByRole('button',{name:'全部',exact:true}).click();
  await page.locator(`[data-file-id="${id}"]`).waitFor();
  assert.equal(await checkbox.isChecked(),false);
  await page.locator('#fileInput').setInputFiles(file(name,'# PostgreSQL\nEXPLAIN ANALYZE executes queries and returns actual plan statistics.'));
  await page.locator('#notice').filter({hasText:'1 份已存在'}).waitFor();
  await page.reload();
  await page.locator('#detailName').filter({hasText:name}).waitFor();
  assert.match(await page.locator('#initialization').textContent(),/可以参考/);
  await screenshot(page,'files-reference-desktop');
  if (phone) assert.equal((await request.get(phone + '/api/files')).status(),404);
  assert.deepEqual(errors,[]);
});

test('files parse failure stays visible and can be retried', {skip}, async t => {
  const {page,errors} = await fixture(t);
  await page.goto(base + '/files');
  await page.locator('#fileInput').setInputFiles(file('empty-reference.txt',' '));
  await page.locator('#retry:not([hidden])').waitFor();
  assert.match(await page.locator('#fileError').textContent(),/OCR/);
  const pending = page.waitForResponse(response => response.url().endsWith('/retry') && response.request().method() === 'POST');
  await page.getByRole('button',{name:'重新初始化'}).click();
  assert.equal((await pending).status(),200);
  await page.locator('#retry:not([disabled])').waitFor();
  const id = new URL(page.url()).searchParams.get('file');
  await page.locator(`[data-file-id="${id}"] .status-error`).waitFor();
  await screenshot(page,'files-parse-error');
  assert.deepEqual(errors,[]);
});

test('files GitHub import keeps the URL as reference and reports invalid links', {skip}, async t => {
  const {page,errors} = await fixture(t);
  let imported = false;
  const value = record({source_url:'https://raw.githubusercontent.com/example/repo/main/guide.md'});
  await page.route('**/api/files',route => route.fulfill({json:{files:imported ? [value] : [],legacy:[],resume_mode:'legacy'}}));
  await page.route('**/api/files/' + identity,route => route.fulfill({json:{file:value}}));
  await page.route('**/api/files/import-github',route => {
    const url = route.request().postDataJSON().url;
    if (url.includes('invalid')) return route.fulfill({status:400,json:{error:'仅支持具体公开 Markdown 文件链接'}});
    imported = true; return route.fulfill({status:202,json:{ok:true,file:value,duplicate:false}});
  });
  await page.goto(base + '/files');
  await page.getByRole('button',{name:'导入 GitHub',exact:true}).click();
  await page.locator('#githubUrl').fill('https://invalid.example/guide.md');
  await page.getByRole('button',{name:'导入',exact:true}).click();
  await page.locator('#githubError').filter({hasText:'仅支持'}).waitFor();
  await page.locator('#githubUrl').fill('https://github.com/example/repo/blob/main/guide.md');
  await page.getByRole('button',{name:'导入',exact:true}).click();
  await page.locator('#sourceLink:not([hidden])').waitFor();
  assert.equal(await page.locator('#sourceLink').getAttribute('href'),value.source_url);
  assert.equal(await page.locator('#fileKind').inputValue(),'reference');
  assert.deepEqual(errors,[]);
});

test('files desktop and narrow layouts preserve long names, initialization state and safe raw text', {skip}, async t => {
  const {page,errors} = await fixture(t);
  const value = record({name:'架构设计与实现约束'.repeat(10) + '.md',title:'超长文件名'.repeat(15),
    source_url:'https://raw.githubusercontent.com/example/repo/main/' + 'long-path-'.repeat(20) + '.md',
    preview:'<script>window.injected=true</script>\n' + 'long_identifier_'.repeat(100),overview:'原始文件内容，作为面试参考。'.repeat(40)});
  await page.route('**/api/files',route => route.fulfill({json:{files:[value],legacy:[],resume_mode:'legacy'}}));
  await page.route('**/api/files/' + identity,route => route.fulfill({json:{file:value}}));
  await page.goto(base + '/files?file=' + identity);
  await page.locator('#fileDetail:not([hidden])').waitFor();
  await page.getByRole('button',{name:'正文',exact:true}).click();
  for (const [name,width,height] of [['desktop',1365,900],['390',390,844],['320',320,740],['zoom-200-equivalent',683,450]]) {
    await page.setViewportSize({width,height});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    assert.equal(await page.evaluate(() => {const panel=document.querySelector('#inspector');return panel.scrollWidth<=panel.clientWidth+1;}),true);
    assert.equal(await page.evaluate(() => !!window.injected),false);
    await screenshot(page,'files-long-' + name);
  }
  await page.getByRole('button',{name:'返回文件列表'}).click();
  assert.equal(new URL(page.url()).searchParams.has('file'),false);
  assert.equal(await page.locator('#filesPanel').isVisible(),true);
  await screenshot(page,'files-list-683');
  assert.deepEqual(errors,[]);
});
