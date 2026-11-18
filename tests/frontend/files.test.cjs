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

for (const viewport of [{width:1365,height:900},{width:390,height:844}]) {
  test(`facts preserve sources and allow reversible review at ${viewport.width}px`,{skip},async t => {
    const {page,errors} = await fixture(t,viewport);
    const fact = {id:'fact-one',subject:'图谱项目',field:'action',value:'完成多源实体链接。',quote:'完成多源实体链接。',
      lines:[4,4],state:'needs_confirmation',review:'auto'};
    const value = () => record({knowledge:{status:'ready',summary:'整理图谱项目。',facts:[fact],projects:[{name:'图谱项目',fact_ids:[fact.id]}],semantic:{status:'ready'}}});
    await page.route('**/api/files',route => route.fulfill({json:{files:[value()],legacy:[],resume_mode:'managed'}}));
    await page.route('**/api/files/' + identity,route => route.fulfill({json:{file:value()}}));
    await page.route(`**/api/files/${identity}/source?*`,route => route.fulfill({json:{text:'项目说明\n完成多源实体链接。\n结果待测',lines:[3,5],total_lines:5}}));
    await page.route(`**/api/files/${identity}/facts/${fact.id}`,route => {
      fact.review = route.request().postDataJSON().decision;
      fact.state = {confirm:'confirmed',reject:'excluded',auto:'needs_confirmation'}[fact.review];
      return route.fulfill({json:{ok:true}});
    });
    await page.goto(base + '/files?file=' + identity);
    await page.getByRole('button',{name:'事实与项目',exact:true}).click();
    await page.locator('.fact-status').filter({hasText:'待确认'}).waitFor();
    await page.getByRole('button',{name:'原文',exact:true}).click();
    await page.locator('#sourceText').filter({hasText:'L4  完成多源实体链接。'}).waitFor();
    await page.getByRole('button',{name:'关闭原文',exact:true}).click();
    await page.getByRole('button',{name:'确认本人事实',exact:true}).click();
    await page.locator('.fact-status').filter({hasText:'已确认'}).waitFor();
    await page.getByRole('button',{name:'排除',exact:true}).click();
    await page.locator('.fact-status').filter({hasText:'已排除'}).waitFor();
    await page.getByRole('button',{name:'撤销核对',exact:true}).click();
    await page.locator('.fact-status').filter({hasText:'待确认'}).waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    assert.equal(await page.locator('.fact-record').evaluate(element => element.scrollWidth <= element.clientWidth),true);
    await screenshot(page,`knowledge-review-${viewport.width}`);
    assert.deepEqual(errors,[]);
  });
}

test('files batch upload initializes reference documents, previews, renames, switches scope and archives', {skip}, async t => {
  const {page,request,errors} = await fixture(t);
  await page.goto(base + '/files');
  const name = 'browser-reference-' + Date.now() + '.md';
  const text = '# PostgreSQL\nEXPLAIN ANALYZE executes queries and returns actual plan statistics.\n' + name;
  await page.locator('#fileInput').setInputFiles([
    file(name,text),
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
  await page.locator('#fileInput').setInputFiles(file(name,text));
  await page.locator('#notice').filter({hasText:'1 份已存在'}).waitFor();
  await page.reload();
  await page.locator('#detailName').filter({hasText:name}).waitFor();
  assert.match(await page.locator('#initialization').textContent(),/可以参考/);
  await screenshot(page,'files-reference-desktop');
  if (phone) assert.equal((await request.get(phone + '/api/files')).status(),404);
  assert.deepEqual(errors,[]);
});

test('files wait for a pending save before archiving and recover controls after an error', {skip}, async t => {
  const {page,errors} = await fixture(t);
  let value = record(), started, release, patches = 0;
  const pending = new Promise(resolve => { started = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  t.after(() => release());
  await page.route('**/api/files',route => route.fulfill({json:{files:[value],legacy:[],resume_mode:'legacy'}}));
  await page.route('**/api/files/' + identity,async route => {
    if (route.request().method() === 'PATCH') {
      const changes = route.request().postDataJSON(); patches++;
      if (patches === 1) { started(); await held; }
      if (patches === 3) return route.fulfill({status:503,json:{error:'合成保存失败，请重试'}});
      value = {...value,...changes};
    }
    return route.fulfill({json:{file:value}});
  });
  await page.goto(base + '/files?file=' + identity);
  await page.locator('#fileDetail:not([hidden])').waitFor();
  await page.locator(`[data-file-id="${identity}"] input`).uncheck();
  await pending;
  assert.equal(await page.locator('#fileDetail').getAttribute('aria-busy'),'true');
  assert.equal(await page.locator(`[data-file-id="${identity}"] input`).isChecked(),false);
  for (const id of ['archive','retry','save','fileTitle','fileKind']) assert.equal(await page.locator('#' + id).isDisabled(),true,id);
  const archive = page.getByRole('button',{name:'归档文件'}).click();
  release(); await archive;
  await page.locator('#notice').filter({hasText:'文件已归档'}).waitFor();
  await page.getByRole('button',{name:'已归档',exact:true}).click();
  await page.locator(`[data-file-id="${identity}"]`).waitFor();
  await page.getByRole('button',{name:'恢复文件'}).click();
  await page.locator('#notice').filter({hasText:'合成保存失败'}).waitFor();
  await page.locator('#archive:not([disabled])').waitFor();
  assert.equal(await page.locator('#fileDetail').getAttribute('aria-busy'),'false');
  assert.equal(await page.locator('#save').isDisabled(),true);
  assert.equal(patches,3);
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
