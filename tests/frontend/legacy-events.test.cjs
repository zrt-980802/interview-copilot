const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8')
  .replace('<link rel="stylesheet" href="/static/app-shell.css">', '<style>' + fs.readFileSync(path.join(__dirname, '../../static/app-shell.css'), 'utf8') + '</style>')
  .replace('<link rel="stylesheet" href="/static/workspace.css">', '<style>' + fs.readFileSync(path.join(__dirname, '../../static/workspace.css'), 'utf8') + '</style>');
let browser;
before(async () => { browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined,
  channel: process.env.PLAYWRIGHT_EXECUTABLE_PATH ? undefined : process.env.PLAYWRIGHT_CHANNEL || undefined}); });
after(async () => { await browser?.close(); });

async function fixture(t) {
  const page = await browser.newPage({viewport: {width: 1150, height: 920}});
  t.after(() => page.close());
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.evaluate(() => {
    window.requests = [];
    window.confirm = () => true;
    window.setInterval = () => 0;
    window.fetch = async (url, options = {}) => {
      window.requests.push({url, body: options.body ? JSON.parse(options.body) : null});
      const state = {listening: true, self_listening: false, resume: {loaded: false}, devices: [], input_devices: []};
      return {ok: true, json: async () => url === '/api/state' ? state : {ok: true, task_id: 'detail-1', session_id: '2'}};
    };
    window.EventSource = class {
      constructor() { window.stream = this; }
      close() {}
    };
  });
  const renderer = fs.readFileSync(path.join(__dirname, '../../static/js/answer-content.js'), 'utf8');
  await page.evaluate(async renderer => {
    window.AnswerContent = await import('data:text/javascript;base64,' + btoa(unescape(encodeURIComponent(renderer))));
  }, renderer);
  await page.setContent(html);
  await page.waitForFunction(() => typeof window.stream?.onmessage === 'function');
  const emit = event => page.evaluate(event => window.stream.onmessage({data: JSON.stringify(event)}), event);
  const quick = async (turn, task, question) => {
    const base = {session_id: '1', turn_id: turn, generation: Number(turn), task_id: task, level: 'quick'};
    await emit({...base, type: 'question', text: question, manual: true});
    await emit({...base, type: 'answer_start', question});
    return base;
  };
  return {page, errors, emit, quick};
}

test('detail renders on its original card while another question streams', async t => {
  const {page, errors, emit, quick} = await fixture(t);
  const a = await quick('101', 'quick-1', 'First question');
  await emit({...a, type: 'answer_done', full: '【开口】First answer\n【方向】Mechanism ::: explain'});
  const b = await quick('202', 'quick-2', 'Second question');
  await page.getByRole('button', {name: 'Mechanism'}).click();
  const sent = await page.evaluate(() => window.requests.find(request => request.url === '/api/detail'));
  assert.equal(sent.body.turn_id, '101');
  const detail = {...a, task_id: 'detail-1', level: 'detail', direction: 'Mechanism'};
  await emit({...detail, type: 'detail_start'});
  await emit({...b, type: 'answer_delta', delta: '【开口】Second answer'});
  await emit({...detail, type: 'answer_delta', delta: 'First details'});
  await emit({...detail, type: 'answer_done', full: 'First details'});
  assert.match(await page.locator('.qa').nth(0).textContent(), /First details/);
  assert.doesNotMatch(await page.locator('.qa').nth(1).textContent(), /First details/);
  assert.match(await page.locator('.qa').nth(1).locator('.quickcard').textContent(), /Second answer/);
  assert.deepEqual(errors, []);
  if(process.env.INTERVIEW_SCREENSHOT_DIR){
    fs.mkdirSync(process.env.INTERVIEW_SCREENSHOT_DIR,{recursive:true});
    await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,'detail-isolation-desktop.png'),fullPage:true});
  }
});

test('reset rejects old answer events and clears question retry state', async t => {
  const {page, errors, emit, quick} = await fixture(t);
  const old = await quick('101', 'quick-1', 'Old question');
  await emit({type: 'session_reset', session_id: '2'});
  await emit({...old, type: 'answer_start', question: 'Old question'});
  await emit({...old, type: 'answer_delta', delta: 'late content'});
  await emit({...old, type: 'answer_done', full: 'late content'});
  assert.equal(await page.locator('.qa').count(), 0);
  await page.keyboard.press('F8');
  assert.equal(await page.evaluate(() => window.requests.filter(request => request.url === '/api/ask').length), 0);
  assert.deepEqual(errors, []);
});

test('supplement updates the same question card and rejects old generation events',async t => {
  const {page,errors,emit,quick}=await fixture(t);
  const initial=await quick('101','quick-old','Partial protocol question');
  await emit({...initial,type:'answer_done',full:'【开口】Obsolete answer\n【方向】Old direction ::: old'});
  const revised={...initial,generation:102,task_id:'quick-revised'};
  await emit({...revised,type:'answer_start',question:'Complete protocol question with tool result',revised:true});
  await emit({...initial,type:'answer_done',full:'late obsolete answer'});
  await emit({...revised,type:'answer_done',full:'【开口】Corrected complete answer'});
  assert.equal(await page.locator('.qa').count(),1);
  assert.equal(await page.locator('.question-link').count(),1);
  assert.match(await page.locator('.qbox').textContent(),/Complete protocol/);
  assert.match(await page.locator('.nav-label').textContent(),/Complete protocol/);
  assert.match(await page.locator('.quickcard').textContent(),/Corrected complete answer/);
  assert.doesNotMatch(await page.locator('.qa').textContent(),/Obsolete|Old direction|late obsolete/);
  assert.deepEqual(errors,[]);
});

test('detail failures render locally and allow retry', async t => {
  const {page, errors, emit, quick} = await fixture(t);
  const a = await quick('101', 'quick-1', 'Question');
  await emit({...a, type: 'answer_done', full: '【开口】Answer\n【方向】Mechanism ::: explain'});
  await page.getByRole('button', {name: 'Mechanism'}).click();
  const detail = {...a, task_id: 'detail-1', level: 'detail', direction: 'Mechanism'};
  await emit({...detail, type: 'detail_start'});
  await emit({...detail, type: 'answer_error', message: 'Synthetic timeout'});
  assert.match(await page.locator('.subcard').textContent(), /Synthetic timeout/);
  assert.equal(await page.getByRole('button', {name: 'Mechanism'}).isEnabled(), true);
  assert.deepEqual(errors, []);
});

const epoch='a'.repeat(32);
const cursor=sequence=>`${epoch}:${sequence}`;

test('snapshot restores streaming answers and ignores duplicate events and stale resets', async t => {
  const {page, errors, emit} = await fixture(t);
  const base={session_id:'1',turn_id:'101',generation:1,task_id:'quick-1',level:'quick'};
  await emit({type:'session_snapshot',session_id:'1',event_id:cursor(10),turns:[
    {turn_id:'101',generation:1,question:'Restored question',at:'10:00:00',status:'pending',answers:[
      {task_id:'quick-1',level:'quick',direction:'',content:'【开口】Partial',status:'streaming',error:''}
    ]}
  ]});
  assert.match(await page.locator('.quickcard').textContent(),/Partial/);
  const delta={...base,type:'answer_delta',delta:' continued',event_id:cursor(11)};
  await emit(delta);
  await emit(delta);
  await emit({type:'session_reset',session_id:'old-session',event_id:cursor(9)});
  await emit({type:'session_snapshot',session_id:'old-session',event_id:cursor(8),turns:[]});
  assert.equal(await page.locator('.qa').count(),1);
  assert.match(await page.locator('.quickcard').textContent(),/Partial continued/);
  assert.doesNotMatch(await page.locator('.quickcard').textContent(),/continued continued/);
  await emit({...base,type:'answer_done',full:'【开口】Complete\n【方向】Mechanism ::: explain',event_id:cursor(12)});
  await page.getByRole('button',{name:'Mechanism'}).click();
  const requests=await page.evaluate(()=>window.requests);
  assert.equal(requests.filter(request=>request.url==='/api/ask').length,0);
  assert.equal(requests.find(request=>request.url==='/api/detail').body.turn_id,'101');
  assert.deepEqual(errors,[]);
});

test('snapshot restores failed details for retry and cancelled quick answers',async t=>{
  const {page, errors, emit} = await fixture(t);
  await emit({type:'session_snapshot',session_id:'1',event_id:cursor(20),turns:[
    {turn_id:'101',generation:1,question:'Cancelled question',status:'cancelled',answers:[
      {task_id:'quick-1',level:'quick',direction:'',content:'【开口】Incomplete',status:'cancelled',error:''}
    ]},
    {turn_id:'102',generation:2,question:'Completed question',status:'complete',answers:[
      {task_id:'quick-2',level:'quick',direction:'',content:'【开口】Answer\n【方向】Mechanism ::: explain',status:'complete',error:''},
      {task_id:'detail-1',level:'detail',direction:'Mechanism',content:'Partial details',status:'error',error:'Synthetic timeout'}
    ]}
  ]});
  assert.equal(await page.locator('.qa').count(),2);
  assert.equal(await page.locator('.quickcard.pending').count(),0);
  assert.match(await page.locator('.subcard').textContent(),/Synthetic timeout/);
  assert.equal(await page.getByRole('button',{name:'Mechanism'}).isEnabled(),true);
  await page.getByRole('button',{name:'Mechanism'}).click();
  assert.deepEqual(errors,[]);
});

test('event gaps reopen the stream and old connection callbacks cannot overwrite the snapshot',async t=>{
  const {page, errors, emit} = await fixture(t);
  await emit({type:'session_snapshot',session_id:'1',event_id:cursor(0),turns:[]});
  await page.evaluate(()=>window.oldStream=window.stream);
  await emit({type:'status',message:'missed previous event',event_id:cursor(2)});
  assert.equal(await page.evaluate(()=>window.oldStream!==window.stream),true);
  await emit({type:'session_snapshot',session_id:'2',event_id:cursor(3),turns:[]});
  await page.evaluate(()=>window.oldStream.onmessage({data:JSON.stringify({type:'session_reset',session_id:'old'})}));
  const session=await page.evaluate(()=>currentSession);
  assert.equal(session,'2');
  assert.deepEqual(errors,[]);
});

test('snapshot keeps old-turn detail busy while another quick answer streams',async t=>{
  const {page, errors, emit} = await fixture(t);
  await emit({type:'session_snapshot',session_id:'1',event_id:cursor(30),turns:[
    {turn_id:'101',generation:1,question:'First',status:'complete',answers:[
      {task_id:'quick-1',level:'quick',direction:'',content:'【开口】Answer\n【方向】Mechanism ::: explain',status:'complete',error:''},
      {task_id:'detail-1',level:'detail',direction:'Mechanism',content:'Partial',status:'streaming',error:''}
    ]},
    {turn_id:'102',generation:2,question:'Second',status:'pending',answers:[
      {task_id:'quick-2',level:'quick',direction:'',content:'',status:'pending',error:''}
    ]}
  ]});
  assert.equal(await page.getByRole('button',{name:'完整版'}).isDisabled(),true);
  await emit({type:'answer_delta',event_id:cursor(31),session_id:'1',turn_id:'101',generation:1,task_id:'detail-1',level:'detail',delta:' continuation'});
  await emit({type:'answer_delta',event_id:cursor(32),session_id:'1',turn_id:'102',generation:2,task_id:'quick-2',level:'quick',delta:'【开口】Second answer'});
  assert.match(await page.locator('.qa').nth(0).textContent(),/Partial continuation/);
  assert.doesNotMatch(await page.locator('.qa').nth(1).textContent(),/Partial/);
  await emit({type:'answer_done',event_id:cursor(33),session_id:'1',turn_id:'101',generation:1,task_id:'detail-1',level:'detail',full:'Finished details'});
  assert.equal(await page.getByRole('button',{name:'完整版'}).isEnabled(),true);
  assert.deepEqual(errors,[]);
});

test('snapshot after server restart accepts a new stream with a lower sequence',async t=>{
  const {page, errors, emit} = await fixture(t);
  await emit({type:'session_snapshot',session_id:'1',event_id:cursor(100),turns:[]});
  await emit({type:'session_snapshot',session_id:'1',event_id:`${'b'.repeat(32)}:0`,turns:[]});
  await emit({type:'stream_ready',event_id:`${'b'.repeat(32)}:0`});
  await emit({type:'status',event_id:`${'b'.repeat(32)}:1`,message:'New process connected'});
  assert.match(await page.locator('#feed').textContent(),/New process connected/);
  assert.equal(await page.locator('#connectionState').textContent(),'已连接');
  assert.deepEqual(errors,[]);
});

test('late clear HTTP response does not erase a newer session',async t=>{
  const {page, errors, emit, quick} = await fixture(t);
  await page.evaluate(()=>{
    const originalFetch=window.fetch;
    window.fetch=(url,options)=>url==='/api/clear'
      ?new Promise(resolve=>{window.completeClear=()=>resolve({json:async()=>({ok:true,session_id:'old-session'})});})
      :originalFetch(url,options);
  });
  await page.locator('#btnClear').click();
  await emit({type:'session_reset',session_id:'1'});
  const current=await quick('101','quick-1','Newer question');
  await emit({...current,type:'answer_done',full:'【开口】Current answer'});
  await page.evaluate(()=>window.completeClear());
  assert.equal(await page.locator('.qa').count(),1);
  assert.match(await page.locator('.qa').textContent(),/Newer question/);
  assert.equal(await page.evaluate(()=>currentSession),'1');
  assert.deepEqual(errors,[]);
});

test('HTTP and SSE connect the real entry point to both browser windows',
  {skip: !process.env.INTERVIEW_PREVIEW_URL}, async t => {
  const page = await browser.newPage({viewport:{width:1150,height:920}});
  const other = await browser.newPage({viewport:{width:1150,height:920}});
  t.after(async()=>{await page.close();await other.close();});
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.request.post(process.env.INTERVIEW_PREVIEW_URL + '/api/clear');
  await Promise.all([page.goto(process.env.INTERVIEW_PREVIEW_URL),other.goto(process.env.INTERVIEW_PREVIEW_URL)]);
  await Promise.all([page.locator('#connectionState').filter({hasText:'已连接'}).waitFor(),
                     other.locator('#connectionState').filter({hasText:'已连接'}).waitFor()]);
  await page.locator('#manualInput').fill('First HTTP question');
  await page.locator('#btnAsk').click();
  await page.locator('.qa').nth(0).locator('.dirrow').waitFor();
  await page.locator('#manualInput').fill('Second HTTP question');
  await page.locator('#btnAsk').click();
  await page.locator('.qa').nth(1).waitFor();
  await page.locator('.qa').nth(0).locator('.chip').first().click();
  await page.locator('.qa').nth(0).locator('.subcard:not(.pending)').waitFor();
  await page.waitForFunction(()=>document.querySelector('.subcard')?.textContent.includes('First HTTP question'));
  await page.waitForFunction(()=>document.querySelectorAll('.qa .dirrow').length===2);
  assert.equal(await other.locator('.qa').count(),2);
  assert.doesNotMatch(await page.locator('.qa').nth(1).textContent(),/First HTTP question/);
  const tooTall=await page.locator('header button').evaluateAll(buttons=>buttons.some(button=>button.getBoundingClientRect().height>48));
  assert.equal(tooTall,false);
  if(process.env.INTERVIEW_SCREENSHOT_DIR){
    await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,'live-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,'live-narrow.png'),fullPage:true});
  }
  page.once('dialog',dialog=>dialog.accept());
  await page.locator('#btnClear').click();
  await page.waitForFunction(()=>document.querySelectorAll('.qa').length===0);
  await other.waitForFunction(()=>document.querySelectorAll('.qa').length===0);
  assert.deepEqual(errors,[]);
});

test('refresh during quick and detail streams restores the same task without new model requests',
  {skip:!process.env.INTERVIEW_PREVIEW_URL},async t=>{
  const page=await browser.newPage({viewport:{width:1150,height:920}});
  t.after(()=>page.close());
  const errors=[],commands=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{if(['/api/ask','/api/detail'].some(path=>request.url().endsWith(path)))commands.push(request.url());});
  await page.goto(process.env.INTERVIEW_PREVIEW_URL);
  await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/api/clear`);
  await page.waitForFunction(()=>qaByTurn.size===0);
  await page.locator('#manualInput').fill('Refresh recovery question');
  await page.locator('#btnAsk').click();
  await page.waitForFunction(()=>[...taskTargets.values()].some(target=>target.raw.length>0));
  const turnId=await page.locator('.qa').getAttribute('data-turn-id');
  await page.reload();
  await page.locator('.dirrow').waitFor();
  assert.equal(await page.locator('.qa').count(),1);
  assert.equal(await page.locator('.qa').getAttribute('data-turn-id'),turnId);
  await page.locator('.chip').first().click();
  await page.waitForFunction(()=>[...taskTargets.values()].some(target=>target.level==='detail'&&target.raw.length>0));
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('.subcard')?.textContent.includes('替代方案'));
  assert.equal(await page.locator('.subcard').count(),1);
  assert.match(await page.locator('.subcard').textContent(),/Refresh recovery question/);
  assert.equal(commands.filter(url=>url.endsWith('/api/ask')).length,1);
  assert.equal(commands.filter(url=>url.endsWith('/api/detail')).length,1);
  assert.equal(await page.locator('.chip').first().isDisabled(),true);
  assert.deepEqual(errors,[]);
  if(process.env.INTERVIEW_SCREENSHOT_DIR){
    await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,'restored-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,'restored-narrow.png'),fullPage:true});
  }
});

test('native EventSource reconnect replays offline answers and a reset with Last-Event-ID',
  {skip:!process.env.INTERVIEW_PREVIEW_URL},async t=>{
  const page=await browser.newPage();
  t.after(()=>page.close());
  const errors=[],streams=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{if(request.url().endsWith('/api/events'))streams.push(request);});
  await page.goto(process.env.INTERVIEW_PREVIEW_URL);
  await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/api/clear`);
  await page.waitForFunction(()=>qaByTurn.size===0);
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/__regression__/disconnect`);
  await page.locator('#connectionState').filter({hasText:'连接中断'}).waitFor();
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/api/ask`,{data:{text:'Question while disconnected'}});
  await page.locator('.dirrow').waitFor();
  assert.match(await page.locator('.qa').textContent(),/Question while disconnected/);
  assert.equal(await page.locator('.qa').count(),1);
  assert.equal(streams.length,2);
  assert.ok((await streams[1].allHeaders())['last-event-id']);
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/__regression__/disconnect`);
  await page.locator('#connectionState').filter({hasText:'连接中断'}).waitFor();
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/api/clear`);
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/api/ask`,{data:{text:'New session while disconnected'}});
  await page.waitForFunction(()=>document.querySelector('.qa')?.textContent.includes('New session while disconnected'));
  await page.locator('.dirrow').waitFor();
  assert.equal(await page.locator('.qa').count(),1);
  assert.doesNotMatch(await page.locator('.qa').textContent(),/Question while disconnected/);
  assert.equal(streams.length,3);
  assert.ok((await streams[2].allHeaders())['last-event-id']);
  assert.deepEqual(errors,[]);
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/api/clear`);
});

test('attempt boundaries replace partial answers and reject late provider events',async t=>{
  const {page,errors,emit,quick}=await fixture(t);
  const answer=await quick('101','quick-1','Attempt boundary question');
  await emit({...answer,type:'answer_attempt',attempt:1,model:'primary'});
  await emit({...answer,type:'answer_delta',attempt:1,delta:'Discarded primary'});
  await emit({...answer,type:'answer_attempt',attempt:2,model:'fallback'});
  assert.doesNotMatch(await page.locator('.qa').textContent(),/Discarded/);
  await emit({...answer,type:'answer_delta',attempt:1,delta:'Late primary'});
  await emit({...answer,type:'answer_done',attempt:1,full:'Wrong completion'});
  await emit({...answer,type:'answer_delta',attempt:2,delta:'Fallback partial'});
  await emit({...answer,type:'answer_error',attempt:2,message:'connection interrupted'});
  const text=await page.locator('.qa').textContent();
  assert.match(text,/Fallback partial/);
  assert.match(text,/connection interrupted/);
  assert.doesNotMatch(text,/Discarded|Late primary|Wrong completion/);
  assert.equal(await page.getByRole('button',{name:'取消生成'}).count(),0);
  assert.deepEqual(errors,[]);
});

test('cancel controls target the selected task and cancelled details can retry',async t=>{
  const {page,errors,emit,quick}=await fixture(t);
  const first=await quick('101','quick-1','Cancelled question');
  await emit({...first,type:'answer_delta',delta:'Partial answer'});
  await page.getByRole('button',{name:'取消生成'}).click();
  const request=await page.evaluate(()=>window.requests.find(request=>request.url==='/api/cancel'));
  assert.deepEqual(request.body,{session_id:'1',task_id:'quick-1'});
  await emit({...first,type:'answer_cancelled'});
  await emit({...first,type:'answer_done',full:'Late completion'});
  assert.match(await page.locator('.qa').first().textContent(),/Partial answer.*已取消/s);
  assert.doesNotMatch(await page.locator('.qa').first().textContent(),/Late completion/);
  const second=await quick('202','quick-2','Detail question');
  await emit({...second,type:'answer_done',full:'【开口】Answer\n【方向】Mechanism ::: explain'});
  const detail={...second,task_id:'detail-1',level:'detail',direction:'Mechanism'};
  await emit({...detail,type:'detail_start'});
  await emit({...detail,type:'answer_delta',delta:'Partial detail'});
  await page.getByRole('button',{name:'取消生成'}).click();
  await emit({...detail,type:'answer_cancelled'});
  assert.equal(await page.getByRole('button',{name:'Mechanism'}).isEnabled(),true);
  await page.getByRole('button',{name:'Mechanism'}).click();
  assert.equal(await page.evaluate(()=>window.requests.filter(request=>request.url==='/api/detail').length),1);
  assert.deepEqual(errors,[]);
});

test('snapshot restores active attempt metadata and its cancellation control',async t=>{
  const {page,errors,emit}=await fixture(t);
  await emit({type:'session_snapshot',event_id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:8',session_id:'s',turns:[{
    turn_id:'101',generation:1,question:'Restored fallback',status:'pending',at:'12:00',answers:[{
      task_id:'active',level:'quick',direction:'',content:'Fallback in progress',status:'streaming',error:'',attempt:2,model:'fallback-model'
    }]
  }]});
  assert.match(await page.locator('.answer-state').textContent(),/备用模型.*fallback-model/);
  await page.getByRole('button',{name:'取消生成'}).click();
  assert.deepEqual(await page.evaluate(()=>window.requests.find(request=>request.url==='/api/cancel').body),{session_id:'s',task_id:'active'});
  assert.deepEqual(errors,[]);
});

test('real model transport fallback and cancellation restore consistently after refresh',
  {skip:!process.env.INTERVIEW_PREVIEW_URL},async t=>{
  const page=await browser.newPage({viewport:{width:1150,height:920}});
  t.after(()=>page.close());
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(process.env.INTERVIEW_PREVIEW_URL);
  await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/api/clear`);
  await page.waitForFunction(()=>qaByTurn.size===0);
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/api/ask`,{data:{text:'[regression:failover] question'}});
  await page.locator('.dirrow').waitFor();
  assert.doesNotMatch(await page.locator('.qa').textContent(),/Discarded primary/);
  assert.match(await page.locator('.answer-state').textContent(),/备用模型.*offline-fallback/);
  const completed=await (await page.request.get(`${process.env.INTERVIEW_PREVIEW_URL}/api/session/snapshot`)).json();
  assert.equal(completed.turns[0].answers[0].attempt,2);
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/api/ask`,{data:{text:'[regression:block] question'}});
  await page.waitForFunction(()=>document.querySelectorAll('.qa')[1]?.querySelector('.answer-body')?.textContent.includes('先明确目标'));
  await page.locator('.qa').nth(1).scrollIntoViewIfNeeded();
  const control=page.locator('.qa').nth(1).getByRole('button',{name:'取消生成'});
  const dimensions=await control.boundingBox();
  assert.ok(dimensions.width>=88&&dimensions.width<=120);assert.ok(dimensions.height>=32);
  assert.match(await control.innerText(),/取消生成/);
  if(process.env.INTERVIEW_SCREENSHOT_DIR){
    await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,'active-cancel-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await control.scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,'active-cancel-narrow.png'),fullPage:true});
    await page.setViewportSize({width:1150,height:920});
  }
  await page.locator('.qa').nth(1).getByRole('button',{name:'取消生成'}).click();
  await page.locator('.qa').nth(1).locator('.answer-state').filter({hasText:'已取消'}).waitFor();
  const cancelled=await (await page.request.get(`${process.env.INTERVIEW_PREVIEW_URL}/api/session/snapshot`)).json();
  assert.equal(cancelled.turns[1].answers[0].status,'cancelled');
  assert.ok(cancelled.turns[1].answers[0].content.length>0);
  await page.reload();
  await page.locator('.qa').nth(1).locator('.answer-state').filter({hasText:'已取消'}).waitFor();
  assert.equal(await page.getByRole('button',{name:'取消生成'}).count(),0);
  assert.equal(await page.locator('.qa').count(),2);
  if(process.env.INTERVIEW_SCREENSHOT_DIR){
    await page.locator('.qa').nth(1).scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,'attempt-cancel-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.locator('.qa').nth(1).scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,'attempt-cancel-narrow.png'),fullPage:true});
  }
  assert.deepEqual(errors,[]);
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/api/clear`);
});

test('structured sources open the referenced excerpt and unmatched quotes stay unverified',async t=>{
  const {page,errors,emit,quick}=await fixture(t);
  const answer=await quick('101','structured','Source question');
  const document={version:1,level:'quick',complete:true,directions:[],
    sources:[{id:'resume',source:'context:resume',kind:'candidate_material',excerpt:'接口开发。<script>literal</script>'}],
    blocks:[
      {kind:'conclusion',title:'',text:'范围〔context:resume〕',html:'<p>范围〔context:resume〕</p>',
        citations:[{raw:'〔context:resume〕',source:'context:resume',source_indices:[0],valid:true}]},
      {kind:'material',title:'context:resume',text:'接口开发。',support:{status:'quote_matched',source_indices:[0]}},
      {kind:'material',title:'context:resume',text:'我完成了压测。',support:{status:'unverified',source_indices:[0]}},
      {kind:'unknown',title:'上线',text:'当前资料未说明'}
    ],findings:[{code:'material_quote_not_found',block:2}]};
  await emit({...answer,type:'answer_done',full:'Legacy text remains available',document});
  assert.equal(await page.locator('[data-support="quote_matched"]').count(),1);
  assert.equal(await page.locator('[data-support="unverified"]').count(),1);
  assert.match(await page.locator('.answer-review').textContent(),/1 处来源待核实/);
  await page.getByRole('button',{name:'查看来源 〔context:resume〕'}).click();
  assert.equal(await page.locator('.answer-sources[open] .source-item[open]').count(),1);
  assert.match(await page.locator('.source-excerpt').textContent(),/<script>literal<\/script>/);
  assert.equal(await page.locator('.source-excerpt script').count(),0);
  assert.equal(await page.getByRole('button',{name:'完整版',exact:true}).isEnabled(),true);
  assert.deepEqual(errors,[]);
});

test('unstructured lines and code-shaped protocol labels remain visible',async t=>{
  const {page,errors,emit,quick}=await fixture(t);
  const answer=await quick('101','fallback','Fallback question');
  const raw='Unexpected preface\n【开口】Main answer\nUnmarked explanation\n【要点】Code :::\n```text\n【材料】literal code\n```\n【方案】Hypothesis ::: If used';
  await emit({...answer,type:'answer_done',full:raw});
  const content=await page.locator('.answer-body').textContent();
  assert.match(content,/Unexpected preface/);
  assert.match(content,/Unmarked explanation/);
  assert.match(content,/【材料】literal code/);
  assert.equal(await page.locator('[data-kind="material"]').count(),0);
  assert.equal(await page.locator('[data-kind="hypothesis"]').count(),1);
  assert.deepEqual(errors,[]);
});

test('real answer documents and source excerpts survive refresh across desktop and narrow layouts',
  {skip:!process.env.INTERVIEW_PREVIEW_URL},async t=>{
  const page=await browser.newPage({viewport:{width:1365,height:900}});
  t.after(()=>page.close());
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(process.env.INTERVIEW_PREVIEW_URL);
  await page.locator('#connectionState').filter({hasText:'已连接'}).waitFor();
  await page.request.post(`${process.env.INTERVIEW_PREVIEW_URL}/api/clear`);
  await page.waitForFunction(()=>qaByTurn.size===0);
  await page.locator('#manualInput').fill('[regression:sources] 介绍你做过的优化');
  await page.locator('#btnAsk').click();
  await page.locator('[data-support="quote_matched"]').waitFor();
  await page.locator('.dirrow').waitFor();
  const snapshot=await (await page.request.get(`${process.env.INTERVIEW_PREVIEW_URL}/api/session/snapshot`)).json();
  const expected=snapshot.turns[0].answers[0].document;
  assert.equal(expected.blocks[1].support.status,'quote_matched');
  await page.getByRole('button',{name:'查看来源 〔context:resume〕'}).click();
  assert.match(await page.locator('.source-excerpt').textContent(),/角色为接口开发/);
  for(const [name,width,height] of [['desktop',1365,900],['narrow',390,844],['small',320,740]]){
    await page.setViewportSize({width,height});
    await page.locator('.source-item[open]').scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    const outside=await page.locator('.answer-block,.source-item').evaluateAll(items=>items.some(item=>{
      const rect=item.getBoundingClientRect();return rect.left<0||rect.right>innerWidth+1;
    }));
    assert.equal(outside,false);
    if(process.env.INTERVIEW_SCREENSHOT_DIR){
      fs.mkdirSync(process.env.INTERVIEW_SCREENSHOT_DIR,{recursive:true});
      await page.screenshot({path:path.join(process.env.INTERVIEW_SCREENSHOT_DIR,`answer-sources-${name}.png`),fullPage:true});
    }
  }
  await page.setViewportSize({width:1280,height:900});
  await page.evaluate(()=>document.documentElement.style.zoom='2');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.evaluate(()=>document.documentElement.style.zoom='');
  await page.reload();
  await page.locator('[data-support="quote_matched"]').waitFor();
  await page.getByRole('button',{name:'查看原文',exact:true}).click();
  assert.match(await page.locator('.source-excerpt').textContent(),/角色为接口开发/);
  const restored=await (await page.request.get(`${process.env.INTERVIEW_PREVIEW_URL}/api/session/snapshot`)).json();
  assert.deepEqual(restored.turns[0].answers[0].document,expected);
  assert.deepEqual(errors,[]);
});

test('long source names wrap while long code scrolls inside its own block',async t=>{
  const {page,errors,emit,quick}=await fixture(t);
  await page.setViewportSize({width:320,height:740});
  const answer=await quick('101','long-content','Long content question');
  const code='long_identifier_'.repeat(100), source='notes:'+'很长的文件名'.repeat(50)+'.md';
  const document={version:1,level:'quick',complete:true,directions:[],findings:[],
    sources:[{id:'long',source,kind:'reference',lines:[1,2],excerpt:code}],
    blocks:[{kind:'conclusion',title:'',text:'代码边界'},
      {kind:'point',title:'示例',text:code,html:`<pre><code>${code}</code></pre>`}]};
  await emit({...answer,type:'answer_done',full:'代码边界',document});
  await page.locator('.answer-sources > summary').click();
  await page.locator('.source-item > summary').click();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.equal(await page.locator('.block-content pre').evaluate(element=>element.scrollWidth>element.clientWidth),true);
  const bounds=await page.locator('.source-item').boundingBox();
  assert.ok(bounds.x>=0&&bounds.x+bounds.width<=320);
  assert.deepEqual(errors,[]);
});

test('question navigation preserves the selected old question when another answer arrives',async t=>{
  const {page,errors,emit,quick}=await fixture(t);
  const first=await quick('101','nav-1','First navigable question');
  await emit({...first,type:'answer_done',full:'【开口】'+('Long content. '.repeat(140))});
  const second=await quick('202','nav-2','Second navigable question');
  await emit({...second,type:'answer_done',full:'【开口】Second answer'});
  await page.locator('.question-link').first().click();
  const before=await page.locator('#feed').evaluate(element=>element.scrollTop);
  const third=await quick('303','nav-3','Third navigable question');
  await emit({...third,type:'answer_delta',delta:'【开口】New content'});
  assert.equal(await page.locator('.question-link[aria-current="true"]').getAttribute('data-turn-id'),'101');
  assert.equal(await page.locator('#feed').evaluate(element=>element.scrollTop),before);
  assert.equal(await page.locator('#questionCount').textContent(),'3 题');
  await page.locator('#followLatest').click();
  assert.equal(await page.locator('#followLatest').isHidden(),true);
  assert.deepEqual(errors,[]);
});
