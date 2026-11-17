import {draftDocument, isAnswerDocument, renderDocument} from '/assets/answer-content.js';

const $ = id => document.getElementById(id);
const turns = new Map(), tasks = new Map();
let source, session, epoch, sequence = -1, selected, following = true, renderPending = false;
let connectionGeneration = 0, wantedAwake = false, wakeLock;
function takePairingFragment() {
  const code = new URLSearchParams(location.hash.slice(1)).get('pair');
  if (location.hash) history.replaceState(null, '', location.pathname);
  return code;
}
const fragment = takePairingFragment();
const position = id => {
  const match = /^([a-f0-9]{32}):(\d+)$/.exec(id || '');
  return match && Number.isSafeInteger(Number(match[2])) ? {epoch: match[1], sequence: Number(match[2])} : null;
};
const readPreference = key => { try { return localStorage.getItem(key); } catch { return null; } };
const savePreference = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
document.documentElement.dataset.theme = readPreference('viewer-theme') || 'dark';
$('fontSize').value = Math.max(16, Math.min(26, Number(readPreference('viewer-font')) || 19));
$('reader').style.fontSize = $('fontSize').value + 'px';
window.lucide?.createIcons();

function paired(value) {
  $('pairing').hidden = value; $('viewer').hidden = !value;
}
function closeStream() {
  connectionGeneration++; source?.close(); source = null;
}
function revoke(message = '配对已失效，请重新连接') {
  closeStream(); turns.clear(); tasks.clear(); selected = session = epoch = null; sequence = -1;
  $('answers').replaceChildren(); $('question').textContent = ''; $('turns').replaceChildren();
  $('current').hidden = true; $('empty').hidden = false;
  $('connection').textContent = '未配对'; $('pairError').textContent = message; paired(false);
  wantedAwake = false; wakeLock?.release().catch(() => {});
  $('awake').setAttribute('aria-pressed', 'false');
}
function capture(value) {
  if (!value) return;
  const states = [value.listening ? '系统声音 · 采集中' : '系统声音 · 已停止',
    value.self_listening ? '麦克风 · 采集中' : '麦克风 · 已停止'];
  if (value.failed?.length) states.push('设备启动失败');
  $('capture').textContent = states.join('　');
}
function selectLatest() {
  selected = [...turns.keys()].at(-1); following = true;
  $('latest').setAttribute('aria-pressed', 'true'); scheduleRender(true);
}
function scheduleRender(newTurn = false) {
  if (newTurn) $('reader').scrollTop = 0;
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => { renderPending = false; render(); });
}
function render() {
  const reader = $('reader'), top = reader.scrollTop, turn = turns.get(selected);
  $('empty').hidden = !!turn; $('current').hidden = !turn;
  $('turns').replaceChildren(...[...turns.values()].map((item, index) => {
    const option = document.createElement('option'); option.value = item.turn_id;
    option.textContent = `${index + 1}. ${item.question}`; return option;
  }));
  if (!turn) { $('answers').replaceChildren(); return; }
  $('turns').value = selected;
  $('number').textContent = `问题 ${[...turns.keys()].indexOf(selected) + 1} / ${turns.size}`;
  $('question').textContent = turn.question;
  const openDetails = new Set([...$('answers').querySelectorAll('details.detail-answer[open]')].map(item => item.dataset.task));
  const fragment = document.createDocumentFragment();
  for (const answer of turn.answers) {
    const entry = document.createElement(answer.level === 'detail' ? 'details' : 'section');
    entry.className = answer.level === 'detail' ? 'detail-answer' : 'answer-entry';
    entry.dataset.task = answer.task_id;
    if (answer.level === 'detail') {
      entry.open = openDetails.has(answer.task_id);
      const title = document.createElement('summary'); title.textContent = '深入 · ' + answer.direction; entry.append(title);
    }
    const content = document.createElement('div');
    const value = isAnswerDocument(answer.document, answer.level) ? answer.document : draftDocument(answer.content || '', answer.level);
    renderDocument(content, value, {compact: true}); entry.append(content);
    const state = document.createElement('div'); state.className = 'answer-status'; state.setAttribute('role', 'status');
    state.textContent = answer.status === 'error' ? answer.error || '生成失败'
      : answer.status === 'cancelled' ? '已取消' : ['pending', 'streaming'].includes(answer.status) ? '生成中…'
        : answer.attempt > 1 ? '已完成 · 备用模型' : '已完成';
    state.classList.toggle('error', answer.status === 'error'); entry.append(state); fragment.append(entry);
  }
  $('answers').replaceChildren(fragment); reader.scrollTop = top;
}
function restore(event) {
  const pos = position(event.event_id);
  if (!pos || !Array.isArray(event.turns) || (pos.epoch === epoch && pos.sequence < sequence)) return;
  const changedSession = session !== event.session_id;
  session = event.session_id; epoch = pos.epoch; sequence = pos.sequence; turns.clear(); tasks.clear();
  for (const turn of event.turns) {
    turns.set(turn.turn_id, turn);
    for (const answer of turn.answers) tasks.set(answer.task_id, {turn, answer});
  }
  if (changedSession || following || !turns.has(selected)) selected = [...turns.keys()].at(-1);
  capture(event.capture); scheduleRender(changedSession); paired(true);
}
function apply(event) {
  if (event.type === 'access_revoked') { revoke(); return; }
  if (event.type === 'session_snapshot') { restore(event); return; }
  if (event.type === 'capture_state' && !event.event_id) { capture(event.capture); return; }
  const pos = position(event.event_id);
  if (event.type === 'stream_ready') {
    if (!pos || pos.epoch !== epoch || pos.sequence !== sequence) { connect(); return; }
    capture(event.capture); $('connection').textContent = '已连接'; return;
  }
  if (!pos) return;
  if (pos.epoch === epoch && pos.sequence <= sequence) return;
  if (pos.epoch !== epoch || pos.sequence !== sequence + 1) { connect(); return; }
  sequence = pos.sequence;
  if (event.type === 'cursor') return;
  if (event.type === 'capture_state') { capture(event.capture); return; }
  if (event.session_id !== session) return;
  if (event.type === 'answer_start') {
    const previous = turns.get(event.turn_id);
    if (previous && previous.generation >= event.generation) return;
    for (const {answer} of tasks.values()) if (answer.level === 'quick' && ['pending', 'streaming'].includes(answer.status)) answer.status = 'cancelled';
    const turn = {turn_id: event.turn_id, question: event.question, generation: event.generation, answers: []};
    const answer = {task_id: event.task_id, level: 'quick', status: 'pending', content: '', attempt: 0};
    turn.answers.push(answer); turns.set(event.turn_id, turn); tasks.set(event.task_id, {turn, answer});
    if (following) { selected = event.turn_id; scheduleRender(true); } else scheduleRender();
    return;
  }
  const turn = turns.get(event.turn_id);
  if (!turn || turn.generation !== event.generation) return;
  if (event.type === 'detail_start') {
    if (tasks.has(event.task_id)) return;
    const answer = {task_id: event.task_id, level: 'detail', direction: event.direction, status: 'pending', content: '', attempt: 0};
    turn.answers = turn.answers.filter(item => item.level !== 'detail' || item.direction !== event.direction);
    turn.answers.push(answer); tasks.set(event.task_id, {turn, answer}); scheduleRender(); return;
  }
  const target = tasks.get(event.task_id), answer = target?.answer;
  if (!target || target.turn !== turn || answer.level !== event.level || !['pending', 'streaming'].includes(answer.status)) return;
  if (event.type === 'answer_attempt') {
    if (!Number.isInteger(event.attempt) || event.attempt <= answer.attempt) return;
    Object.assign(answer, {content: '', document: null, attempt: event.attempt, model: event.model, status: 'pending'});
  } else {
    if (event.type !== 'answer_cancelled' && (event.attempt || 0) !== answer.attempt) return;
    if (event.type === 'answer_delta') { answer.content += event.delta || ''; answer.status = 'streaming'; }
    else if (event.type === 'answer_done') Object.assign(answer, {content: event.full, document: event.document, status: 'complete'});
    else if (event.type === 'answer_error') Object.assign(answer, {error: event.message, status: 'error'});
    else if (event.type === 'answer_cancelled') answer.status = 'cancelled';
  }
  scheduleRender();
}
function connect() {
  closeStream(); const generation = connectionGeneration;
  const stream = new EventSource('/api/events'); source = stream;
  stream.onmessage = message => {
    if (source !== stream) return;
    try { apply(JSON.parse(message.data)); } catch { $('connection').textContent = '同步失败'; }
  };
  stream.onopen = () => { if (source === stream) $('connection').textContent = '同步中'; };
  stream.onerror = async () => {
    if (source !== stream) return;
    $('connection').textContent = '连接中断 · 正在重试';
    try {
      const response = await fetch('/api/snapshot', {cache: 'no-store'});
      if (generation !== connectionGeneration) return;
      if (response.status === 401) revoke();
    } catch {}
  };
}
async function pair(code) {
  $('pairButton').disabled = true; $('pairError').textContent = '';
  try {
    const response = await fetch('/api/pair', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code})});
    if (!response.ok) throw new Error(response.status === 429 ? '尝试次数过多，请一分钟后重试' : response.status === 503 ? '电脑服务暂未就绪' : '配对码无效或已过期');
    $('code').value = ''; paired(true); connect();
  } catch (error) { $('pairError').textContent = error.message; paired(false); }
  finally { $('pairButton').disabled = false; }
}
$('pairForm').onsubmit = event => { event.preventDefault(); pair($('code').value.trim()); };
$('turns').onchange = () => {
  selected = $('turns').value; following = false; $('latest').setAttribute('aria-pressed', 'false'); scheduleRender(true);
};
$('latest').onclick = selectLatest;
$('focus').onclick = () => {
  const focused = document.body.classList.toggle('focused'); $('focus').setAttribute('aria-pressed', String(focused));
};
$('fontSize').oninput = () => { $('reader').style.fontSize = $('fontSize').value + 'px'; savePreference('viewer-font', $('fontSize').value); };
$('theme').onclick = () => {
  const theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = theme; savePreference('viewer-theme', theme);
};
$('logout').onclick = async () => {
  try {
    const response = await fetch('/api/logout', {method: 'POST'});
    if (!response.ok) throw new Error();
    revoke('已断开配对');
  } catch { $('connection').textContent = '断开失败，请重试'; }
};
async function acquireWakeLock() {
  try {
    if (wantedAwake && document.visibilityState === 'visible') {
      if (wakeLock && !wakeLock.released) return;
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => $('awake').setAttribute('aria-pressed', 'false'));
      $('awake').setAttribute('aria-pressed', 'true');
    }
  } catch { wantedAwake = false; $('awake').setAttribute('aria-pressed', 'false'); $('awake').title = '当前环境无法保持常亮'; }
}
if (!navigator.wakeLock) { $('awake').disabled = true; $('awake').title = '浏览器不支持屏幕常亮'; }
$('awake').onclick = async () => {
  wantedAwake = !wantedAwake;
  if (wantedAwake) await acquireWakeLock(); else await wakeLock?.release();
};
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && !$('viewer').hidden) { connect(); acquireWakeLock(); }
});
window.addEventListener('online', () => { if (!$('viewer').hidden) connect(); });
window.addEventListener('pagehide', closeStream);
window.addEventListener('pageshow', event => { if (event.persisted && !$('viewer').hidden) connect(); });
window.addEventListener('hashchange', () => {
  const code = takePairingFragment();
  if (/^[0-9]{8}$/.test(code || '')) { revoke(''); pair(code); }
});
try {
  const response = await fetch('/api/snapshot', {cache: 'no-store'});
  if (response.ok) { restore(await response.json()); connect(); }
  else {
    paired(false); $('connection').textContent = '未配对';
    if (/^[0-9]{8}$/.test(fragment || '')) await pair(fragment);
    else if (response.status !== 401) $('pairError').textContent = '电脑服务暂未就绪，请稍后重试';
  }
} catch { paired(false); $('connection').textContent = '无法连接'; $('pairError').textContent = '电脑服务暂不可达'; }
